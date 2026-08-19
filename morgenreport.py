"""Erzeugt und verteilt den täglichen Garmin-Morgenreport.

Dieses Modul ist die zentrale Pipeline des Projekts: Es authentifiziert sich bei
Garmin Connect, normalisiert mehrere Garmin-Endpunkte, ergänzt Gewohnheiten aus
Firestore, berechnet einen transparenten Erholungsscore und verteilt denselben
Bericht an Datei, Telegram/E-Mail sowie Firestore für die GPT Action.

Zugangsdaten werden ausschließlich aus Umgebungsvariablen beziehungsweise der
lokalen, von Git ausgeschlossenen .env-Datei geladen.
"""

import argparse
import base64
import gzip
import json
import os
import sys
import smtplib
import time
import requests
import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest
from email.mime.text import MIMEText
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo
from garminconnect import Garmin
from dotenv import load_dotenv
from history_analytics import aggregate_history

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Windows-Konsolen sind oft nicht UTF-8; ohne das crasht print() an ═/✔-Zeichen
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

EMAIL = os.environ.get("GARMIN_EMAIL", "")
PASSWORD = os.environ.get("GARMIN_PASSWORD", "")

GMAIL_ADRESSE = os.environ.get("GMAIL_ADRESSE", "")
GMAIL_APP_PASSWORT = os.environ.get("GMAIL_APP_PASSWORT", "")
EMPFAENGER = os.environ.get("MORGENREPORT_EMPFAENGER", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FIRESTORE_PROJEKT = os.environ.get("FIRESTORE_PROJEKT", "gewohnheitstracker-3b30a")
FIRESTORE_BASIS = f"https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJEKT}/databases/default/documents"
FIRESTORE_USER_UID = os.environ.get("FIRESTORE_USER_UID", "")
TRACKER_SECRET = os.environ.get("TRACKER_SECRET", "")
_FIRESTORE_CREDENTIALS = None
GARMIN_ROHDATEN_SCHEMA_VERSION = 1
GARMIN_ROHDATEN_AUFBEWAHRUNG_TAGE = 90
GARMIN_LANGZEIT_BATCH_TAGE = 28
GARMIN_LANGZEIT_WOCHEN_LIMIT = 104
FIRESTORE_RETRY_STATUS = {408, 429, 500, 502, 503, 504}
FIRESTORE_RETRY_VERSUCHE = 3

TOKEN_ORDNER = os.path.join(BASE_DIR, ".garmin_tokens")

MONATE_DE = {
    1: "Januar",
    2: "Februar",
    3: "März",
    4: "April",
    5: "Mai",
    6: "Juni",
    7: "Juli",
    8: "August",
    9: "September",
    10: "Oktober",
    11: "November",
    12: "Dezember",
}


class GarminLoginError(RuntimeError):
    """Garmin-Anmeldung konnte nicht ohne Benutzereingabe abgeschlossen werden."""


def login():
    """Meldet sich bevorzugt mit dem wiederverwendbaren Garmin-Token an.

    Im unbeaufsichtigten GitHub-Workflow darf niemals eine MFA-Eingabe hängen
    bleiben. Deshalb ist dort ein ungültiges Token ein klarer Fehler. Nur bei
    einem interaktiven lokalen Start folgt der Fallback auf E-Mail, Passwort und
    MFA; das erneuerte Token wird anschließend für den nächsten Lauf gespeichert.
    """
    try:
        client = Garmin()
        client.login(TOKEN_ORDNER)
        return client
    except Exception as token_fehler:
        if os.environ.get("GITHUB_ACTIONS") == "true" or not sys.stdin.isatty():
            raise GarminLoginError(
                "Garmin-Token ungueltig oder abgelaufen. "
                "Lokal neu anmelden und GARMIN_TOKENS_B64 aktualisieren."
            ) from token_fehler

        if not EMAIL or not PASSWORD:
            raise GarminLoginError(
                "GARMIN_EMAIL/GARMIN_PASSWORD fehlen in der lokalen .env-Datei."
            ) from token_fehler

        print("Gespeichertes Garmin-Token ist ungueltig; starte lokale Anmeldung.")
        try:
            client = Garmin(EMAIL, PASSWORD, prompt_mfa=lambda: input("Garmin MFA-Code: ").strip())
            client.login()
            os.makedirs(TOKEN_ORDNER, exist_ok=True)
            client.client.dump(TOKEN_ORDNER)
            return client
        except Exception as login_fehler:
            raise GarminLoginError(
                "Garmin-Anmeldung fehlgeschlagen. MFA-Code und Zugangsdaten pruefen."
            ) from login_fehler


def sicher(fn, *args, default=None):
    """Kapselt optionale Garmin-Endpunkte, ohne den gesamten Report abzubrechen.

    Garmin liefert einzelne Messarten gelegentlich nicht oder ändert deren
    Verfügbarkeit. Für solche Zusatzwerte ist ein fehlender Wert sinnvoller als
    der Ausfall des kompletten Morgenreports. Login- und Versandfehler werden
    dagegen nicht über diese Funktion verschluckt.
    """
    try:
        return fn(*args)
    except Exception:
        return default


def _aktivitaetszahl(value, divisor=1, nachkommastellen=0):
    """Normalisiert optionale Garmin-Zahlen ohne fehlende Werte zu 0 zu machen.

    Garmin liefert Aktivitätswerte je nach Endpunkt als ``int``, ``float`` oder
    gelegentlich als numerischen String. Ungültige beziehungsweise fehlende Werte
    bleiben ``None``. Das ist wichtig, weil etwa eine fehlende Distanz bei
    Krafttraining nicht als tatsächlich zurückgelegte Distanz von 0 km erscheinen
    soll.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        ergebnis = float(value) / divisor
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if nachkommastellen == 0:
        return round(ergebnis)
    return round(ergebnis, nachkommastellen)


def _zahl(value, divisor=1, nachkommastellen=None):
    """Liest optionale Zahlen, ohne fehlende Garmin-Werte in 0 umzuwandeln."""
    if value is None or isinstance(value, bool):
        return None
    try:
        ergebnis = float(value) / divisor
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if nachkommastellen is None:
        return int(ergebnis) if ergebnis.is_integer() else ergebnis
    return round(ergebnis, nachkommastellen)


def _erster_wert(daten, *gesuchte_schluessel):
    """Sucht bekannte Garmin-Schlüssel rekursiv in wechselnden API-Antworten."""
    if isinstance(daten, dict):
        for schluessel in gesuchte_schluessel:
            if schluessel in daten and daten[schluessel] is not None:
                return daten[schluessel]
        for wert in daten.values():
            gefunden = _erster_wert(wert, *gesuchte_schluessel)
            if gefunden is not None:
                return gefunden
    elif isinstance(daten, list):
        for wert in daten:
            gefunden = _erster_wert(wert, *gesuchte_schluessel)
            if gefunden is not None:
                return gefunden
    return None


def _minuten_aus_sekunden(value):
    wert = _zahl(value, 60)
    return round(wert) if wert is not None else None


def normalisiere_aktivitaeten(aktivitaeten):
    """Reduziert beliebige Garmin-Aktivitäten auf stabile, relevante Felder.

    Es wird absichtlich kein Aktivitätstyp gefiltert. Laufen, Radfahren, Wandern,
    Krafttraining, Yoga und Garmins sonstige Typen durchlaufen dieselbe Logik.
    Felder, die bei einem Typ nicht existieren, bleiben ``None`` und der restliche
    Eintrag bleibt trotzdem im Morgenreport erhalten.
    """
    normalisiert = []
    if not isinstance(aktivitaeten, list):
        return normalisiert

    for aktivitaet in aktivitaeten:
        if not isinstance(aktivitaet, dict):
            continue

        typ_daten = aktivitaet.get("activityType") or {}
        if isinstance(typ_daten, dict):
            typ = typ_daten.get("typeKey") or typ_daten.get("typeId")
        else:
            typ = typ_daten
        typ = str(typ) if typ is not None else "unbekannt"

        name = aktivitaet.get("activityName") or typ.replace("_", " ").title()
        normalisiert.append({
            "name": str(name),
            "typ": typ,
            "startzeit": aktivitaet.get("startTimeLocal"),
            "dauer_min": _aktivitaetszahl(aktivitaet.get("duration"), 60, 0),
            "distanz_km": _aktivitaetszahl(aktivitaet.get("distance"), 1000, 2),
            "kalorien": _aktivitaetszahl(aktivitaet.get("calories"), 1, 0),
            "durchschnittspuls": _aktivitaetszahl(aktivitaet.get("averageHR"), 1, 0),
            "maximalpuls": _aktivitaetszahl(aktivitaet.get("maxHR"), 1, 0),
            "hoehenmeter": _aktivitaetszahl(aktivitaet.get("elevationGain"), 1, 0),
            "trainingseffekt_aerob": _aktivitaetszahl(
                aktivitaet.get("aerobicTrainingEffect"), 1, 1
            ),
            "trainingseffekt_anaerob": _aktivitaetszahl(
                aktivitaet.get("anaerobicTrainingEffect"), 1, 1
            ),
        })

    # Älteste Aktivität zuerst ergibt im Morgenreport einen natürlichen Tagesablauf.
    normalisiert.sort(key=lambda eintrag: eintrag.get("startzeit") or "")
    return normalisiert


def hole_aktivitaeten(client, tag):
    """Lädt alle Aktivitäten eines Tages ohne Garmins optionalen Typfilter."""
    roh = sicher(client.get_activities_by_date, tag, tag, default=[])
    return normalisiere_aktivitaeten(roh)


def schlafdaten_unvollstaendig(daten):
    """Erkennt Garmin-Schlafwerte, die morgens offensichtlich noch fehlen."""
    return not daten.get("schlafdauer_h") or not daten.get("schlaf_score")


def extrahiere_schlaf_recovery_daten(tag, stats, sleep, hrv_data):
    """Normalisiert nur Schlaf- und Recovery-Werte aus Garmin-Rohdaten."""
    stats = stats if isinstance(stats, dict) else {}
    sleep_dto = sleep.get("dailySleepDTO", {}) if isinstance(sleep, dict) else {}
    schlafdauer_h = _zahl(sleep_dto.get("sleepTimeSeconds"), 3600, 1)
    if schlafdauer_h == 0:
        schlafdauer_h = None
    sleep_scores = sleep_dto.get("sleepScores") or {}
    overall_score = sleep_scores.get("overall") or {}
    schlaf_score = _zahl(
        overall_score.get("value")
    )
    if schlaf_score == 0:
        schlaf_score = None
    tief_min = _minuten_aus_sekunden(sleep_dto.get("deepSleepSeconds"))
    leicht_min = _minuten_aus_sekunden(sleep_dto.get("lightSleepSeconds"))
    rem_min = _minuten_aus_sekunden(sleep_dto.get("remSleepSeconds"))
    wach_min = _minuten_aus_sekunden(sleep_dto.get("awakeSleepSeconds"))
    if schlafdauer_h is None:
        tief_min = leicht_min = rem_min = wach_min = None

    hrv = None
    if isinstance(hrv_data, dict):
        hrv = _zahl((hrv_data.get("hrvSummary") or {}).get("lastNightAvg"))

    daten = {
        "datum":          tag,
        "body_battery":   _zahl(stats.get("bodyBatteryMostRecentValue")),
        "ruhepuls":       _zahl(stats.get("restingHeartRate")),
        "schlafdauer_h":  schlafdauer_h,
        "schlaf_score":   schlaf_score,
        "tief_min":       tief_min,
        "leicht_min":     leicht_min,
        "rem_min":        rem_min,
        "wach_min":       wach_min,
        "hrv":            hrv,
    }
    daten["sleep_data_incomplete"] = schlafdaten_unvollstaendig(daten)
    return daten


def hole_schlaf_recovery_daten(client, tag=None):
    """Lädt nur die Garmin-Endpunkte, die für die Schlaf-Nachsynchronisierung nötig sind."""
    tag = tag or date.today().isoformat()
    stats = sicher(client.get_stats, tag, default={})
    sleep = sicher(client.get_sleep_data, tag, default={})
    hrv_data = sicher(client.get_hrv_data, tag, default={})
    return extrahiere_schlaf_recovery_daten(tag, stats, sleep, hrv_data)


def _garmin_quelle(client, methode, *args):
    """Ruft eine optionale Garmin-Quelle ab und meldet Fehler ohne Geheimnisse."""
    fn = getattr(client, methode, None)
    if not callable(fn):
        return None, False
    try:
        return fn(*args), True
    except Exception:
        return None, False


def hole_garmin_rohquellen(client, reportdatum):
    """Lädt analysegeeignete Garmin-Tagesquellen für Archiv und Normalisierung.

    Ein Historientag entspricht dem Morgenreport: Schlaf und Erholung enden am
    Reportdatum, Bewegung, Stress und Aktivitäten beziehen sich auf den Vortag.
    Konto-, Geräte- und Profildaten werden bewusst nicht archiviert.
    """
    tag = date.fromisoformat(reportdatum)
    vortag = (tag - timedelta(days=1)).isoformat()
    wochenstart = (tag - timedelta(days=tag.weekday())).isoformat()

    abrufe = (
        ("stats", "get_stats", (reportdatum,)),
        ("stats_vortag", "get_stats", (vortag,)),
        ("sleep", "get_sleep_data", (reportdatum,)),
        ("hrv", "get_hrv_data", (reportdatum,)),
        ("stress_vortag", "get_stress_data", (vortag,)),
        ("steps_vortag", "get_steps_data", (vortag,)),
        ("spo2", "get_spo2_data", (reportdatum,)),
        ("respiration", "get_respiration_data", (reportdatum,)),
        ("training_readiness", "get_training_readiness", (reportdatum,)),
        ("weekly_intensity", "get_weekly_intensity_minutes", (wochenstart, reportdatum)),
        ("max_metrics", "get_max_metrics", (reportdatum,)),
        ("activities_vortag", "get_activities_by_date", (vortag, vortag)),
        ("heart_rates_vortag", "get_heart_rates", (vortag,)),
        ("body_battery", "get_body_battery", (reportdatum, reportdatum)),
        ("body_battery_events", "get_body_battery_events", (reportdatum,)),
        ("floors_vortag", "get_floors", (vortag,)),
        ("hydration_vortag", "get_hydration_data", (vortag,)),
        ("intensity_minutes_vortag", "get_intensity_minutes_data", (vortag,)),
        ("body_composition", "get_body_composition", (reportdatum, reportdatum)),
        ("training_status", "get_training_status", (reportdatum,)),
        ("endurance_score", "get_endurance_score", (reportdatum,)),
        ("fitness_age", "get_fitnessage_data", (reportdatum,)),
        ("all_day_events_vortag", "get_all_day_events", (vortag,)),
        ("blood_pressure", "get_blood_pressure", (reportdatum, reportdatum)),
    )
    quellen = {}
    fehlgeschlagen = []
    for name, methode, args in abrufe:
        wert, erfolgreich = _garmin_quelle(client, methode, *args)
        if erfolgreich:
            quellen[name] = wert
        else:
            fehlgeschlagen.append(name)
    return quellen, fehlgeschlagen


def _intensitaet_woche(intensity):
    eintraege = intensity if isinstance(intensity, list) else [intensity]
    mod_summe = 0
    vig_summe = 0
    gefunden = False
    for eintrag in eintraege:
        if not isinstance(eintrag, dict):
            continue
        mod = _zahl(_erster_wert(
            eintrag, "moderateValue", "weeklyModerateIntensityMinutes"
        ))
        vig = _zahl(_erster_wert(
            eintrag, "vigorousValue", "weeklyVigorousIntensityMinutes"
        ))
        if mod is not None:
            mod_summe += mod
            gefunden = True
        if vig is not None:
            vig_summe += vig
            gefunden = True
    return mod_summe + vig_summe * 2 if gefunden else None


def _normalisiere_gewicht(body_composition):
    gewicht = _zahl(_erster_wert(body_composition, "weight"))
    if gewicht is None:
        return None
    # Garmin Connect liefert Gewicht in diesem Endpunkt üblicherweise in Gramm.
    return round(gewicht / 1000, 1) if gewicht > 500 else round(gewicht, 1)


def normalisiere_garmin_quellen(reportdatum, quellen):
    """Verdichtet Garmin-Rohantworten in stabile Tagesfelder für Auswertungen."""
    stats = quellen.get("stats") if isinstance(quellen.get("stats"), dict) else {}
    stats_vortag = (
        quellen.get("stats_vortag")
        if isinstance(quellen.get("stats_vortag"), dict)
        else {}
    )
    sleep = quellen.get("sleep") if isinstance(quellen.get("sleep"), dict) else {}
    hrv_data = quellen.get("hrv") if isinstance(quellen.get("hrv"), dict) else {}
    stress = quellen.get("stress_vortag")
    steps = quellen.get("steps_vortag")
    spo2 = quellen.get("spo2")
    resp = quellen.get("respiration")
    readiness = quellen.get("training_readiness")
    metrics = quellen.get("max_metrics")
    aktivitaeten = normalisiere_aktivitaeten(quellen.get("activities_vortag"))
    schlaf_recovery = extrahiere_schlaf_recovery_daten(
        reportdatum, stats, sleep, hrv_data
    )

    schritte = None
    if isinstance(steps, list):
        schrittwerte = [
            _zahl(eintrag.get("steps")) for eintrag in steps if isinstance(eintrag, dict)
        ]
        schrittwerte = [wert for wert in schrittwerte if wert is not None]
        schritte = sum(schrittwerte) if schrittwerte else None
    if schritte is None:
        schritte = _zahl(stats_vortag.get("totalSteps"))

    tr_score = _zahl(_erster_wert(readiness, "score"))
    tr_level = _erster_wert(readiness, "level")
    vo2max = _zahl(_erster_wert(metrics, "vo2MaxPreciseValue"), 1, 1)
    body_composition = quellen.get("body_composition")
    intensity_tag = quellen.get("intensity_minutes_vortag")
    floors_tag = quellen.get("floors_vortag")
    heart_rates_tag = quellen.get("heart_rates_vortag")
    stress_avg = _zahl(_erster_wert(stress, "avgStressLevel"))
    if stress_avg is None:
        stress_avg = _zahl(stats_vortag.get("averageStressLevel"))
    intensitaet_mod = _zahl(stats_vortag.get("moderateIntensityMinutes"))
    if intensitaet_mod is None:
        intensitaet_mod = _zahl(_erster_wert(
            intensity_tag, "moderateIntensityMinutes", "moderateValue"
        ))
    intensitaet_vig = _zahl(stats_vortag.get("vigorousIntensityMinutes"))
    if intensitaet_vig is None:
        intensitaet_vig = _zahl(_erster_wert(
            intensity_tag, "vigorousIntensityMinutes", "vigorousValue"
        ))

    return {
        **schlaf_recovery,
        "stress_avg": stress_avg,
        "schritte": schritte,
        "spo2": _zahl(_erster_wert(spo2, "averageSpO2"), 1, 1),
        "atemfrequenz": _zahl(
            _erster_wert(resp, "avgWakingRespirationValue"), 1, 1
        ),
        "tr_score": tr_score,
        "tr_level": str(tr_level) if tr_level is not None else None,
        "int_min_woche": _intensitaet_woche(quellen.get("weekly_intensity")),
        "vo2max": vo2max,
        "aktivitaeten_gestern": aktivitaeten,
        "kalorien_gesamt": _zahl(stats_vortag.get("totalKilocalories")),
        "kalorien_aktiv": _zahl(stats_vortag.get("activeKilocalories")),
        "distanz_km": _zahl(stats_vortag.get("totalDistanceMeters"), 1000, 2),
        "puls_min": _zahl(
            stats_vortag.get("minHeartRate")
            if stats_vortag.get("minHeartRate") is not None
            else _erster_wert(heart_rates_tag, "minHeartRate")
        ),
        "puls_max": _zahl(
            stats_vortag.get("maxHeartRate")
            if stats_vortag.get("maxHeartRate") is not None
            else _erster_wert(heart_rates_tag, "maxHeartRate")
        ),
        "body_battery_min": _zahl(stats.get("bodyBatteryLowestValue")),
        "body_battery_max": _zahl(stats.get("bodyBatteryHighestValue")),
        "body_battery_geladen": _zahl(stats.get("bodyBatteryChargedValue")),
        "body_battery_verbraucht": _zahl(stats.get("bodyBatteryDrainedValue")),
        "aktiv_min": _minuten_aus_sekunden(stats_vortag.get("activeSeconds")),
        "hochaktiv_min": _minuten_aus_sekunden(stats_vortag.get("highlyActiveSeconds")),
        "sitzend_min": _minuten_aus_sekunden(stats_vortag.get("sedentarySeconds")),
        "intensitaet_mod_min": intensitaet_mod,
        "intensitaet_vig_min": intensitaet_vig,
        "stockwerke_auf": _zahl(
            stats_vortag.get("floorsAscended")
            if stats_vortag.get("floorsAscended") is not None
            else _erster_wert(floors_tag, "floorsAscended")
        ),
        "stockwerke_ab": _zahl(
            stats_vortag.get("floorsDescended")
            if stats_vortag.get("floorsDescended") is not None
            else _erster_wert(floors_tag, "floorsDescended")
        ),
        "fluessigkeit_ml": _zahl(_erster_wert(
            quellen.get("hydration_vortag"), "valueInML", "totalHydration"
        )),
        "gewicht_kg": _normalisiere_gewicht(body_composition),
        "bmi": _zahl(_erster_wert(body_composition, "bmi"), 1, 1),
        "koerperfett_pct": _zahl(_erster_wert(
            body_composition, "bodyFat", "percentFat"
        ), 1, 1),
        "fitnessalter": _zahl(_erster_wert(quellen.get("fitness_age"), "fitnessAge")),
        "ausdauer_score": _zahl(_erster_wert(
            quellen.get("endurance_score"), "overallScore", "enduranceScore"
        )),
        "trainingsstatus": (
            str(_erster_wert(quellen.get("training_status"), "trainingStatus"))
            if _erster_wert(quellen.get("training_status"), "trainingStatus") is not None
            else None
        ),
    }


def hole_tagesdaten(client, reportdatum):
    """Lädt einen beliebigen historischen Morgenreport-Tag samt Rohquellen."""
    quellen, fehlgeschlagen = hole_garmin_rohquellen(client, reportdatum)
    daten = normalisiere_garmin_quellen(reportdatum, quellen)
    daten["_garmin_rohquellen"] = quellen
    daten["_garmin_quellen_fehler"] = fehlgeschlagen
    return daten


def hole_daten(client):
    """Lädt Garmin-Messwerte und vereinheitlicht sie in einem flachen Dictionary.

    Schlaf- und Erholungswerte beziehen sich auf heute beziehungsweise die letzte
    Nacht. Stress und Schritte werden für gestern geladen, weil der heutige Tag am
    Morgen noch unvollständig wäre. Die flache Struktur ist der gemeinsame Vertrag
    für Scoreberechnung, Textausgabe, Tests und Firestore.
    """
    return hole_tagesdaten(client, date.today().isoformat())


def firestore_wert_lesen(v):
    """Übersetzt einen typisierten Firestore-REST-Wert rekursiv nach Python.

    Die Tracker-Webapp speichert auch Listen und verschachtelte Maps. Diese
    Übersetzung hält Firestore-spezifische Typwrapper aus der Gewohnheitslogik.
    Unbekannte beziehungsweise Null-Typen werden als None behandelt.
    """
    if "stringValue" in v:
        return v["stringValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return v["doubleValue"]
    if "booleanValue" in v:
        return v["booleanValue"]
    if "arrayValue" in v:
        return [firestore_wert_lesen(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: firestore_wert_lesen(val) for k, val in v["mapValue"].get("fields", {}).items()}
    return None


def firestore_wert_schreiben(v):
    """Übersetzt Python-Werte rekursiv in das Format der Firestore-REST-API.

    bool wird vor int geprüft, weil bool in Python eine Unterklasse von int ist.
    None bleibt ausdrücklich null, damit fehlende Gesundheitswerte nicht fälschlich
    als numerische Null beim Fitnesscoach ankommen. Listen und Maps werden für die
    typunabhängige Aktivitätsliste benötigt.
    """
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if v is None:
        return {"nullValue": None}
    if isinstance(v, list):
        return {"arrayValue": {"values": [firestore_wert_schreiben(x) for x in v]}}
    if isinstance(v, dict):
        return {
            "mapValue": {
                "fields": {k: firestore_wert_schreiben(wert) for k, wert in v.items()}
            }
        }
    return {"stringValue": str(v)}


def firestore_auth_headers():
    """Erzeugt einen kurzlebigen Google-IAM-Bearer für die Firestore REST API."""
    global _FIRESTORE_CREDENTIALS
    if _FIRESTORE_CREDENTIALS is None:
        _FIRESTORE_CREDENTIALS, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/datastore"]
        )
    if not _FIRESTORE_CREDENTIALS.valid:
        _FIRESTORE_CREDENTIALS.refresh(GoogleAuthRequest())
    return {"Authorization": f"Bearer {_FIRESTORE_CREDENTIALS.token}"}


def firestore_user_url(*teile):
    """Baut einen privaten Dokumentpfad für das konfigurierte Hauptkonto."""
    if not FIRESTORE_USER_UID:
        raise RuntimeError("FIRESTORE_USER_UID nicht gesetzt")
    return "/".join([FIRESTORE_BASIS, "users", FIRESTORE_USER_UID, *teile])


def firestore_legacy_report_url():
    """Fester, anonym lesbarer Spiegel fuer den kostenlosen Cloudflare Worker."""
    if not TRACKER_SECRET:
        raise RuntimeError("TRACKER_SECRET nicht gesetzt")
    return f"{FIRESTORE_BASIS}/tracker/morgenreport_{quote(TRACKER_SECRET, safe='')}"


def firestore_request(method, pfad, *, timeout=30, **kwargs):
    """Wiederholt nur voruebergehende Firestore-Netzwerkfehler."""
    request_fn = getattr(requests, method.lower())
    for versuch in range(1, FIRESTORE_RETRY_VERSUCHE + 1):
        try:
            response = request_fn(pfad, timeout=timeout, **kwargs)
        except (requests.Timeout, requests.ConnectionError):
            if versuch >= FIRESTORE_RETRY_VERSUCHE:
                raise
        else:
            if (
                response.status_code not in FIRESTORE_RETRY_STATUS
                or versuch >= FIRESTORE_RETRY_VERSUCHE
            ):
                return response
            response.close()
        time.sleep(2 ** (versuch - 1))

    raise RuntimeError("Firestore-Anfrage konnte nicht ausgefuehrt werden")


def hole_firestore_dokument(pfad):
    """Reads one IAM-protected Firestore document as ordinary Python data."""
    resp = firestore_request("get", pfad, headers=firestore_auth_headers(), timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return {
        key: firestore_wert_lesen(value)
        for key, value in resp.json().get("fields", {}).items()
    }


def schreibe_firestore_dokument(pfad, felder, feldmasken=None):
    """Writes a Firestore document and optionally limits the updated fields."""
    params = None
    if feldmasken:
        params = [("updateMask.fieldPaths", field) for field in feldmasken]
    resp = firestore_request(
        "patch",
        pfad,
        headers=firestore_auth_headers(),
        params=params,
        json={"fields": {
            key: firestore_wert_schreiben(value) for key, value in felder.items()
        }},
        timeout=60,
    )
    resp.raise_for_status()
    return felder


def hole_gewohnheiten():
    """Lädt die Tracker-Konfiguration für die Auswertung des Vortags.

    Der Zugriff erfolgt mit einem kurzlebigen Google-IAM-Token. HTTP-Fehler werden
    nicht verborgen; main() behandelt Gewohnheiten bewusst als optionale Ergänzung.
    """
    resp = firestore_request(
        "get",
        firestore_user_url("tracker", "gewohnheiten"),
        headers=firestore_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    felder = resp.json().get("fields", {})
    liste = felder.get("liste")
    return firestore_wert_lesen(liste) if liste else []


def hole_aktuellen_morgenreport_firestore():
    """Lädt das aktuelle Report-Dokument für abgegrenzte Nachläufe."""
    resp = firestore_request(
        "get",
        firestore_user_url("health", "morning_report"),
        headers=firestore_auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    felder = resp.json().get("fields", {})
    return {k: firestore_wert_lesen(v) for k, v in felder.items()}


def gewohnheiten_gestern(liste):
    """Bewertet sichtbare Gewohnheiten für gestern und berechnet ihre Erfolgsquote.

    Die Regeln spiegeln die Tracker-Webapp: Überschriften, Trenner und ausgeblendete
    Elemente zählen nicht. Numerische Gewohnheiten benötigen ein Zielintervall;
    Haken-Gewohnheiten gelten bei einem wahrheitswertigen Eintrag als erfüllt.
    Rückgabe ist ``(Anzeigezeilen, Quote)`` für Report und Firestore.
    """
    # Quote nach derselben Logik wie die Erfolgsrate in der Tracker-Webapp:
    # Haken-Gewohnheiten plus Zahl-Gewohnheiten mit Min/Max-Ziel,
    # jeweils nur wenn "In Erfolgsrate einbeziehen" nicht deaktiviert ist.
    gestern = (date.today() - timedelta(days=1)).isoformat()
    ergebnisse = []
    zaehler = []
    for g in liste:
        typ = g.get("typ")
        if typ in ("header", "divider", "auswahl") or g.get("ausgeblendet"):
            continue
        in_quote = g.get("inErfolgsrate") is not False
        if typ == "zahl":
            ziel_min = g.get("zielMin")
            ziel_max = g.get("zielMax")
            if ziel_min is None and ziel_max is None:
                continue
            wert = (g.get("eintraege") or {}).get(gestern)
            ok = (wert is not None
                  and (ziel_min is None or wert >= ziel_min)
                  and (ziel_max is None or wert <= ziel_max))
            einheit = f" {g['einheit']}" if g.get("einheit") else ""
            anzeige = f"{g.get('name', '?')} ({wert if wert is not None else '–'}{einheit})"
            ergebnisse.append((anzeige, ok))
        else:
            ok = bool((g.get("eintraege") or {}).get(gestern))
            ergebnisse.append((g.get("name", "?"), ok))
        if in_quote:
            zaehler.append(ok)
    quote = round(sum(zaehler) / len(zaehler) * 100) if zaehler else None
    return ergebnisse, quote


def normalisiere_gewohnheits_ergebnisse(ergebnisse):
    """Speichert Gewohnheitsresultate als stabile Maps statt Python-Tuples."""
    normalisiert = []
    for eintrag in ergebnisse or []:
        if isinstance(eintrag, dict):
            name = eintrag.get("name")
            ok = eintrag.get("ok")
        else:
            name, ok = eintrag
        normalisiert.append({"name": name, "ok": bool(ok)})
    return normalisiert


GARMIN_ANALYSE_ZUSATZFELDER = (
    "kalorien_gesamt",
    "kalorien_aktiv",
    "distanz_km",
    "puls_min",
    "puls_max",
    "body_battery_min",
    "body_battery_max",
    "body_battery_geladen",
    "body_battery_verbraucht",
    "aktiv_min",
    "hochaktiv_min",
    "sitzend_min",
    "intensitaet_mod_min",
    "intensitaet_vig_min",
    "stockwerke_auf",
    "stockwerke_ab",
    "fluessigkeit_ml",
    "gewicht_kg",
    "bmi",
    "koerperfett_pct",
    "fitnessalter",
    "ausdauer_score",
    "trainingsstatus",
)

GARMIN_HISTORIE_AKTUALISIERUNGSFELDER = (
    "datum",
    "score",
    "empfehlung",
    "body_battery",
    "hrv",
    "ruhepuls",
    "schlafdauer_h",
    "schlaf_score",
    "tief_min",
    "rem_min",
    "leicht_min",
    "wach_min",
    "stress_avg",
    "schritte",
    "tr_score",
    "tr_level",
    "int_min_woche",
    "vo2max",
    "spo2",
    "atemfrequenz",
    "aktivitaeten_gestern",
    "sleep_data_incomplete",
    *GARMIN_ANALYSE_ZUSATZFELDER,
)


GARMIN_TAGESBELEG_FELDER = (
    "body_battery", "hrv", "ruhepuls", "schlafdauer_h", "schlaf_score",
    "tief_min", "rem_min", "leicht_min", "wach_min", "stress_avg", "schritte",
    "spo2", "atemfrequenz", "aktivitaeten_gestern", "kalorien_gesamt",
    "kalorien_aktiv", "distanz_km", "puls_min", "puls_max", "body_battery_min",
    "body_battery_max", "body_battery_geladen", "body_battery_verbraucht",
    "aktiv_min", "hochaktiv_min", "sitzend_min", "intensitaet_mod_min",
    "intensitaet_vig_min", "stockwerke_auf", "stockwerke_ab", "fluessigkeit_ml",
)


def hat_garmin_analysewerte(daten):
    """Returns true only when a day has date-specific wearable evidence."""
    for field in GARMIN_TAGESBELEG_FELDER:
        value = daten.get(field)
        if value is not None and value != [] and value != {}:
            return True
    return False


def schreibe_morgenreport_firestore(daten, score, empfehlung, habit_quote, report_text, habit_ergebnisse=None):
    """Speichert den neuesten vollständigen Report für Dashboard und GPT Action.

    Es wird absichtlich immer dasselbe Firestore-Dokument überschrieben. Der
    Fitnesscoach benötigt den aktuellen Tageszustand und keine frei abfragbare
    Gesundheitsdaten-Historie. Dadurch bleibt auch die externe Action klein: Sie
    muss weder Datum noch Dokument-ID vom GPT entgegennehmen.

    Neben den Einzelwerten wird ``report_text`` gespeichert. Die Einzelwerte sind
    für strukturierte Auswertungen geeignet; der Text bewahrt Hinweise,
    Formatierung und Gewohnheiten genau so, wie sie per Telegram versendet wurden.
    Fehlende Messwerte bleiben als Firestore ``null`` erhalten. Sie werden nicht
    künstlich zu 0 oder -1 gemacht, weil ein Coach "nicht gemessen" sonst als
    echten Messwert missverstehen könnte.

    Der Schreibzugriff verwendet Google-IAM mit kurzlebigen Zugangsdaten. Fuer den
    kostenlosen Cloudflare Worker wird derselbe aktuelle Report zusaetzlich in
    ein anonym lesbares, aber nicht anonym beschreibbares Legacy-Dokument gespiegelt.
    """
    # Die Feldnamen bilden zugleich den stabilen Vertrag zur GPT Action. Beim
    # Umbenennen eines Feldes deshalb auch gpt_action/openapi.yaml aktualisieren.
    felder = {
        "datum":         daten["datum"],
        "score":         score,
        "empfehlung":    empfehlung,
        "report_text":   report_text,
        "body_battery":  daten["body_battery"],
        "hrv":           daten["hrv"],
        "ruhepuls":      daten["ruhepuls"],
        "schlafdauer_h": daten["schlafdauer_h"],
        "schlaf_score":  daten["schlaf_score"],
        "tief_min":      daten["tief_min"],
        "rem_min":       daten["rem_min"],
        "leicht_min":    daten["leicht_min"],
        "wach_min":      daten["wach_min"],
        "stress_avg":    daten["stress_avg"],
        "tr_score":      daten["tr_score"],
        "tr_level":      daten["tr_level"],
        "schritte":      daten["schritte"],
        "int_min_woche": daten["int_min_woche"],
        "vo2max":        daten["vo2max"],
        "spo2":          daten["spo2"],
        "atemfrequenz":  daten["atemfrequenz"],
        "habit_quote":   habit_quote,
        "gewohnheiten":  normalisiere_gewohnheits_ergebnisse(habit_ergebnisse),
        "sleep_data_incomplete": daten.get("sleep_data_incomplete", False),
        "sleep_nachsynchronisiert_am": None,
        "aktivitaeten_gestern": daten.get("aktivitaeten_gestern", []),
        # Beim neuen Morgenreport werden mögliche Abenddaten des Vortags bewusst
        # geleert. Dadurch kann der Fitnesscoach nie alte Aktivitäten irrtümlich
        # als Aktivitäten des neuen Tages ausgeben.
        "aktivitaeten_heute": [],
        "aktivitaeten_heute_datum": daten["datum"],
        "aktivitaeten_heute_aktualisiert_am": None,
    }
    felder.update({feld: daten.get(feld) for feld in GARMIN_ANALYSE_ZUSATZFELDER})
    # Die Firestore-REST-API erwartet pro Wert einen expliziten Typ. Die zentrale
    # Hilfsfunktion hält diese technische Darstellung aus der Fachlogik heraus.
    body = {"fields": {k: firestore_wert_schreiben(v) for k, v in felder.items()}}

    # PATCH aktualisiert das feste "aktueller Report"-Dokument. Ein Timeout
    # verhindert, dass ein gestörter Firestore-Aufruf den Workflow endlos blockiert.
    resp = firestore_request(
        "patch",
        firestore_user_url("health", "morning_report"),
        headers=firestore_auth_headers(),
        json=body,
        timeout=15,
    )

    # HTTP-Fehler müssen sichtbar werden; andernfalls könnte der Workflow Erfolg
    # melden, obwohl der GPT am nächsten Morgen noch veraltete Daten erhält.
    resp.raise_for_status()

    legacy_resp = firestore_request(
        "patch",
        firestore_legacy_report_url(),
        headers=firestore_auth_headers(),
        json=body,
        timeout=15,
    )
    legacy_resp.raise_for_status()

    schreibe_tageshistorie_firestore(daten, score, empfehlung, habit_quote, habit_ergebnisse)
    aktualisiere_schlafhistorie_spiegel(date.fromisoformat(daten["datum"]))


def tageshistorie_felder(daten, score=None, empfehlung=None, habit_quote=None, habit_ergebnisse=None):
    """Reduziert den Report auf strukturierte Felder für Trendanalysen."""
    felder = {
        "datum": daten["datum"],
        "score": score,
        "empfehlung": empfehlung,
        "body_battery": daten["body_battery"],
        "hrv": daten["hrv"],
        "ruhepuls": daten["ruhepuls"],
        "schlafdauer_h": daten["schlafdauer_h"],
        "schlaf_score": daten["schlaf_score"],
        "tief_min": daten["tief_min"],
        "rem_min": daten["rem_min"],
        "leicht_min": daten["leicht_min"],
        "wach_min": daten["wach_min"],
        "stress_avg": daten["stress_avg"],
        "schritte": daten["schritte"],
        "tr_score": daten["tr_score"],
        "tr_level": daten["tr_level"],
        "int_min_woche": daten["int_min_woche"],
        "vo2max": daten["vo2max"],
        "spo2": daten["spo2"],
        "atemfrequenz": daten["atemfrequenz"],
        "habit_quote": habit_quote,
        "gewohnheiten": normalisiere_gewohnheits_ergebnisse(habit_ergebnisse),
        "aktivitaeten_gestern": daten.get("aktivitaeten_gestern", []),
        "notizen": None,
        "subjektive_energie": None,
        "sleep_data_incomplete": daten.get("sleep_data_incomplete", False),
        "sleep_nachsynchronisiert_am": None,
    }
    felder.update({feld: daten.get(feld) for feld in GARMIN_ANALYSE_ZUSATZFELDER})
    return felder


def schreibe_tageshistorie_firestore(daten, score, empfehlung, habit_quote, habit_ergebnisse=None):
    """Schreibt ein Tagesdokument als privates Gedächtnis für spätere Trends."""
    felder = tageshistorie_felder(daten, score, empfehlung, habit_quote, habit_ergebnisse)
    body = {"fields": {k: firestore_wert_schreiben(v) for k, v in felder.items()}}
    resp = firestore_request(
        "patch",
        firestore_user_url("health", "morning_report", "history", daten["datum"]),
        headers=firestore_auth_headers(),
        json=body,
        timeout=15,
    )
    resp.raise_for_status()


def schreibe_schlaf_nachsynchronisierung_firestore(daten):
    """Aktualisiert nur Schlaf-/Recovery-Felder im aktuellen Report und Tagesdokument."""
    aktualisiert_am = datetime.now(ZoneInfo("Europe/Vienna")).isoformat(timespec="seconds")
    felder = {
        "body_battery": daten["body_battery"],
        "hrv": daten["hrv"],
        "ruhepuls": daten["ruhepuls"],
        "schlafdauer_h": daten["schlafdauer_h"],
        "schlaf_score": daten["schlaf_score"],
        "tief_min": daten["tief_min"],
        "rem_min": daten["rem_min"],
        "leicht_min": daten["leicht_min"],
        "wach_min": daten["wach_min"],
        "sleep_data_incomplete": schlafdaten_unvollstaendig(daten),
        "sleep_nachsynchronisiert_am": aktualisiert_am,
    }
    body = {"fields": {k: firestore_wert_schreiben(v) for k, v in felder.items()}}
    params = [("updateMask.fieldPaths", feldname) for feldname in felder]
    for pfad in (
        firestore_user_url("health", "morning_report"),
        firestore_user_url("health", "morning_report", "history", daten["datum"]),
        firestore_legacy_report_url(),
    ):
        resp = firestore_request(
            "patch",
            pfad,
            headers=firestore_auth_headers(),
            params=params,
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
    aktualisiere_schlafhistorie_spiegel(date.fromisoformat(daten["datum"]))
    return aktualisiert_am


def daterange(start, ende):
    """Iteriert inklusive Start und Ende über ISO-Datumswerte."""
    tag = start
    while tag <= ende:
        yield tag.isoformat()
        tag += timedelta(days=1)


def hole_historientag_firestore(tag):
    """Lädt ein einzelnes Tageshistorien-Dokument; fehlende Tage ergeben None."""
    resp = firestore_request(
        "get",
        firestore_user_url("health", "morning_report", "history", tag),
        headers=firestore_auth_headers(),
        timeout=15,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    felder = resp.json().get("fields", {})
    return {k: firestore_wert_lesen(v) for k, v in felder.items()}


def hole_historie_zeitraum(start, ende):
    """Loads a date range with one structured query instead of one HTTP call per day."""
    resp = firestore_request(
        "post",
        f"{firestore_user_url('health', 'morning_report')}:runQuery",
        headers=firestore_auth_headers(),
        json={
            "structuredQuery": {
                "from": [{"collectionId": "history"}],
                "where": {
                    "compositeFilter": {
                        "op": "AND",
                        "filters": [
                            {"fieldFilter": {
                                "field": {"fieldPath": "datum"},
                                "op": "GREATER_THAN_OR_EQUAL",
                                "value": {"stringValue": start.isoformat()},
                            }},
                            {"fieldFilter": {
                                "field": {"fieldPath": "datum"},
                                "op": "LESS_THAN_OR_EQUAL",
                                "value": {"stringValue": ende.isoformat()},
                            }},
                        ],
                    }
                },
                "orderBy": [{
                    "field": {"fieldPath": "datum"},
                    "direction": "ASCENDING",
                }],
            }
        },
        timeout=60,
    )
    resp.raise_for_status()
    tage = []
    for result in resp.json():
        fields = result.get("document", {}).get("fields")
        if fields:
            tage.append({key: firestore_wert_lesen(value) for key, value in fields.items()})
    return tage


def hole_garmin_rohmanifest_firestore(tag):
    """Lädt den Status des privaten Rohdatenarchivs für einen Historientag."""
    resp = firestore_request(
        "get",
        firestore_user_url("health", "garmin_raw", "days", tag),
        headers=firestore_auth_headers(),
        timeout=15,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return {
        key: firestore_wert_lesen(value)
        for key, value in resp.json().get("fields", {}).items()
    }


def aktualisiere_historientag_garmin_firestore(daten, score, empfehlung):
    """Erneuert nur Garmin-Felder und bewahrt Notizen, Gewohnheiten und Reviews."""
    vollstaendig = tageshistorie_felder(daten, score, empfehlung)
    felder = {
        feld: vollstaendig.get(feld)
        for feld in GARMIN_HISTORIE_AKTUALISIERUNGSFELDER
    }
    body = {"fields": {k: firestore_wert_schreiben(v) for k, v in felder.items()}}
    resp = firestore_request(
        "patch",
        firestore_user_url("health", "morning_report", "history", daten["datum"]),
        headers=firestore_auth_headers(),
        params=[("updateMask.fieldPaths", feld) for feld in felder],
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    felder = resp.json().get("fields", {})
    return {k: firestore_wert_lesen(v) for k, v in felder.items()}


def _rohquelle_speicherformat(daten):
    """Serialisiert eine Garmin-Antwort verlustfrei und komprimiert große Quellen."""
    text = json.dumps(daten, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text.encode("utf-8")) <= 400_000:
        return "json", [text]
    komprimiert = gzip.compress(text.encode("utf-8"), compresslevel=9)
    kodiert = base64.b64encode(komprimiert).decode("ascii")
    return "gzip+base64", [kodiert[i:i + 700_000] for i in range(0, len(kodiert), 700_000)]


def _patch_firestore_dokument(pfad, felder):
    body = {"fields": {k: firestore_wert_schreiben(v) for k, v in felder.items()}}
    resp = firestore_request(
        "patch",
        pfad,
        headers=firestore_auth_headers(),
        json=body,
        timeout=30,
    )
    resp.raise_for_status()


def schreibe_garmin_rohquellen_firestore(tag, quellen, fehlgeschlagen=None):
    """Archiviert Garmin-Antworten getrennt von abgeleiteten Analysewerten."""
    abgerufen_am = datetime.now(ZoneInfo("Europe/Vienna")).isoformat(timespec="seconds")
    for name, daten in sorted(quellen.items()):
        encoding, teile = _rohquelle_speicherformat(daten)
        quelle_pfad = firestore_user_url(
            "health", "garmin_raw", "days", tag, "sources", name
        )
        felder = {
            "datum": tag,
            "quelle": name,
            "abgerufen_am": abgerufen_am,
            "encoding": encoding,
            "teile": len(teile),
            "data": teile[0] if len(teile) == 1 else None,
        }
        _patch_firestore_dokument(quelle_pfad, felder)
        if len(teile) > 1:
            for index, teil in enumerate(teile, start=1):
                _patch_firestore_dokument(
                    f"{quelle_pfad}/chunks/{index:04d}",
                    {"index": index, "data": teil},
                )

    _patch_firestore_dokument(
        firestore_user_url("health", "garmin_raw", "days", tag),
        {
            "datum": tag,
            "schema_version": GARMIN_ROHDATEN_SCHEMA_VERSION,
            "abgerufen_am": abgerufen_am,
            "quellen": sorted(quellen),
            "fehlgeschlagen": sorted(fehlgeschlagen or []),
        },
    )


def loesche_garmin_rohtag_firestore(tag):
    """Entfernt einen privaten Rohdatentag inklusive möglicher Datenstücke."""
    manifest = hole_garmin_rohmanifest_firestore(tag)
    if manifest is None:
        return False

    headers = firestore_auth_headers()
    quellen_url = firestore_user_url("health", "garmin_raw", "days", tag, "sources")
    resp = firestore_request(
        "get", quellen_url, headers=headers, params={"pageSize": 100}, timeout=30
    )
    resp.raise_for_status()
    for dokument in resp.json().get("documents", []):
        name = dokument.get("name")
        if not name:
            continue
        dokument_url = f"https://firestore.googleapis.com/v1/{name}"
        felder = dokument.get("fields", {})
        teile = firestore_wert_lesen(felder.get("teile", {"integerValue": "1"})) or 1
        for index in range(1, int(teile) + 1):
            if int(teile) <= 1:
                break
            teil_resp = firestore_request(
                "delete",
                f"{dokument_url}/chunks/{index:04d}", headers=headers, timeout=15
            )
            if teil_resp.status_code != 404:
                teil_resp.raise_for_status()
        quell_resp = firestore_request(
            "delete", dokument_url, headers=headers, timeout=15
        )
        if quell_resp.status_code != 404:
            quell_resp.raise_for_status()

    manifest_resp = firestore_request(
        "delete",
        firestore_user_url("health", "garmin_raw", "days", tag),
        headers=headers,
        timeout=15,
    )
    if manifest_resp.status_code != 404:
        manifest_resp.raise_for_status()
    return True


def synchronisiere_garmin_historie(
    client, ende=None, tage=28, vorab_daten=None, start=None, spiegel_ende=None
):
    """Fills normalized history and retains raw sources only for 90 days."""
    ende = ende or date.today()
    start = start or (ende - timedelta(days=tage - 1))
    if start > ende:
        raise ValueError("Startdatum liegt nach dem Enddatum")
    tage = (ende - start).days + 1
    vorab_daten = vorab_daten or {}
    ergebnis = {
        "geprueft": tage,
        "historientage_ergaenzt": 0,
        "historientage_aktualisiert": 0,
        "rohtage_ergaenzt": 0,
        "rohtage_entfernt": 0,
        "uebersprungen": 0,
        "ohne_daten": 0,
    }

    roh_ab = date.today() - timedelta(days=GARMIN_ROHDATEN_AUFBEWAHRUNG_TAGE - 1)
    for tag in daterange(start, ende):
        tag_datum = date.fromisoformat(tag)
        roh_erforderlich = tag_datum >= roh_ab
        historie = hole_historientag_firestore(tag)
        manifest = hole_garmin_rohmanifest_firestore(tag) if roh_erforderlich else None
        roh_aktuell = (
            isinstance(manifest, dict)
            and manifest.get("schema_version") == GARMIN_ROHDATEN_SCHEMA_VERSION
        )
        if historie is not None and (roh_aktuell or not roh_erforderlich):
            ergebnis["uebersprungen"] += 1
            continue

        ist_vorab_daten = tag in vorab_daten
        daten = vorab_daten.get(tag)
        if daten is None:
            daten = hole_tagesdaten(client, tag)

        if not hat_garmin_analysewerte(daten):
            ergebnis["ohne_daten"] += 1
            continue

        score, _ = berechne_erholung(daten)
        empfehlung, _ = trainingsempfehlung(score)
        if historie is None:
            schreibe_tageshistorie_firestore(daten, score, empfehlung, None)
            ergebnis["historientage_ergaenzt"] += 1
        elif not ist_vorab_daten:
            aktualisiere_historientag_garmin_firestore(daten, score, empfehlung)
            ergebnis["historientage_aktualisiert"] += 1

        if roh_erforderlich and not roh_aktuell:
            schreibe_garmin_rohquellen_firestore(
                tag,
                daten.get("_garmin_rohquellen", {}),
                daten.get("_garmin_quellen_fehler", []),
            )
            ergebnis["rohtage_ergaenzt"] += 1

    if ergebnis["historientage_ergaenzt"] or ergebnis["historientage_aktualisiert"]:
        aktualisiere_schlafhistorie_spiegel(spiegel_ende or ende)
    abgelaufen = date.today() - timedelta(days=GARMIN_ROHDATEN_AUFBEWAHRUNG_TAGE)
    try:
        if loesche_garmin_rohtag_firestore(abgelaufen.isoformat()):
            ergebnis["rohtage_entfernt"] = 1
    except Exception:
        # Aufräumen ist nachrangig; ein fertiger Rückimport darf daran nicht scheitern.
        ergebnis["bereinigung_fehler"] = True
    return ergebnis


SCHLAFHISTORIE_FELDER = (
    "datum",
    "score",
    "body_battery",
    "hrv",
    "ruhepuls",
    "schlafdauer_h",
    "schlaf_score",
    "tief_min",
    "rem_min",
    "leicht_min",
    "wach_min",
    "stress_avg",
    "schritte",
    "spo2",
    "atemfrequenz",
    "tr_score",
    "tr_level",
    "int_min_woche",
    "vo2max",
    "sleep_data_incomplete",
    "aktivitaeten_gestern",
    *GARMIN_ANALYSE_ZUSATZFELDER,
)


def kompakter_schlafhistorientag(eintrag):
    """Begrenzt einen privaten Historientag auf die Felder der GPT-Trendanalyse."""
    return {feld: eintrag.get(feld) for feld in SCHLAFHISTORIE_FELDER}


def erstelle_schlafhistorie_spiegel(tage, limit=28):
    """Sortiert, bereinigt und begrenzt die rollierende Historie."""
    gueltige_tage = [
        eintrag for eintrag in tage
        if isinstance(eintrag, dict) and isinstance(eintrag.get("datum"), str)
    ]
    gueltige_tage.sort(key=lambda eintrag: eintrag["datum"])
    return [kompakter_schlafhistorientag(eintrag) for eintrag in gueltige_tage[-limit:]]


def aktualisiere_schlafhistorie_spiegel(ende=None):
    """Spiegelt maximal 28 private Tageswerte in das feste GPT-Dokument."""
    ende = ende or date.today()
    start = ende - timedelta(days=27)
    historie = erstelle_schlafhistorie_spiegel(hole_historie_zeitraum(start, ende))
    feldname = "schlafhistorie_28_tage"
    body = {"fields": {feldname: firestore_wert_schreiben(historie)}}
    resp = firestore_request(
        "patch",
        firestore_legacy_report_url(),
        headers=firestore_auth_headers(),
        params=[("updateMask.fieldPaths", feldname)],
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    return historie


def aktualisiere_langzeitaggregate(start, ende=None):
    """Builds compact long-term series from private normalized daily records."""
    ende = ende or date.today()
    tage = hole_historie_zeitraum(start, ende)
    serien = aggregate_history(tage)
    serien["week"] = serien["week"][-GARMIN_LANGZEIT_WOCHEN_LIMIT:]
    gueltige_tage = sorted(
        item["datum"] for item in tage
        if isinstance(item, dict) and isinstance(item.get("datum"), str)
    )
    metadata = {
        "von": gueltige_tage[0] if gueltige_tage else None,
        "bis": gueltige_tage[-1] if gueltige_tage else None,
        "tage_gefunden": len(gueltige_tage),
        "wochen_limit": GARMIN_LANGZEIT_WOCHEN_LIMIT,
        "aktualisiert_am": datetime.now(ZoneInfo("Europe/Vienna")).isoformat(
            timespec="seconds"
        ),
    }
    felder = {
        "langzeit_metadaten": metadata,
        "langzeit_wochen": serien["week"],
        "langzeit_monate": serien["month"],
        "langzeit_quartale": serien["quarter"],
        "langzeit_jahre": serien["year"],
    }
    masken = list(felder)
    for pfad in (
        firestore_user_url("health", "garmin_aggregates"),
        firestore_legacy_report_url(),
    ):
        schreibe_firestore_dokument(pfad, felder, masken)
    return {"metadata": metadata, **serien}


def hole_langzeitimport_status_firestore():
    """Loads the resumable long-term import checkpoint."""
    return hole_firestore_dokument(firestore_user_url("health", "garmin_import"))


def schreibe_langzeitimport_status_firestore(status):
    """Persists one checkpoint without exposing it through the GPT bridge."""
    return schreibe_firestore_dokument(
        firestore_user_url("health", "garmin_import"), status
    )


def _activity_date(activity):
    for key in ("startTimeLocal", "startTimeGMT", "beginTimestamp", "date"):
        value = activity.get(key) if isinstance(activity, dict) else None
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue
    return None


def ermittle_erstes_garmin_jahr(client, ende=None):
    """Finds the year of the oldest Garmin activity with a one-item query."""
    ende = ende or date.today()
    connectapi = getattr(client, "connectapi", None)
    activities_url = getattr(client, "garmin_connect_activities", None)
    if not callable(connectapi) or not activities_url:
        raise RuntimeError("Garmin-Startdatum nicht automatisch ermittelbar; --von angeben")
    activities = connectapi(
        activities_url,
        params={
            "startDate": "2000-01-01",
            "endDate": ende.isoformat(),
            "start": "0",
            "limit": "1",
            "sortOrder": "asc",
        },
    )
    oldest = _activity_date(activities[0]) if activities else None
    if oldest is None:
        raise RuntimeError("Keine historische Garmin-Aktivität gefunden; --von angeben")
    return date(oldest.year, 1, 1)


def synchronisiere_garmin_langzeit(
    client, start=None, ende=None, batch_tage=GARMIN_LANGZEIT_BATCH_TAGE
):
    """Imports one backwards batch and updates resumable progress and aggregates."""
    ende = ende or date.today()
    status = hole_langzeitimport_status_firestore() or {}
    if start is None and status.get("start_date"):
        start = date.fromisoformat(status["start_date"])
    start = start or ermittle_erstes_garmin_jahr(client, ende)
    if start > ende:
        raise ValueError("Startdatum liegt nach dem Enddatum")

    gleicher_start = status.get("start_date") == start.isoformat()
    if gleicher_start and status.get("status") == "in_progress" and status.get("end_date"):
        ende = date.fromisoformat(status["end_date"])
    if gleicher_start and status.get("next_end_date"):
        naechstes_ende = date.fromisoformat(status["next_end_date"])
    elif gleicher_start and status.get("status") == "completed":
        aggregate = aktualisiere_langzeitaggregate(start, ende)
        refreshed = {
            **status,
            "end_date": ende.isoformat(),
            "total_days": (ende - start).days + 1,
            "processed_days": (ende - start).days + 1,
            "remaining_days": 0,
            "progress_percent": 100.0,
            "updated_at": datetime.now(ZoneInfo("Europe/Vienna")).isoformat(
                timespec="seconds"
            ),
        }
        schreibe_langzeitimport_status_firestore(refreshed)
        return {**refreshed, "aggregate": aggregate["metadata"], "already_completed": True}
    else:
        naechstes_ende = ende

    batch_ende = min(naechstes_ende, ende)
    batch_start = max(start, batch_ende - timedelta(days=batch_tage - 1))
    import_result = synchronisiere_garmin_historie(
        client,
        start=batch_start,
        ende=batch_ende,
        spiegel_ende=ende,
    )
    following_end = batch_start - timedelta(days=1)
    completed = following_end < start
    total_days = (ende - start).days + 1
    remaining_days = 0 if completed else (following_end - start).days + 1
    imported_days = total_days - remaining_days
    new_status = {
        "status": "completed" if completed else "in_progress",
        "start_date": start.isoformat(),
        "end_date": ende.isoformat(),
        "last_batch_from": batch_start.isoformat(),
        "last_batch_to": batch_ende.isoformat(),
        "next_end_date": None if completed else following_end.isoformat(),
        "total_days": total_days,
        "processed_days": imported_days,
        "remaining_days": remaining_days,
        "progress_percent": round(imported_days / total_days * 100, 1),
        "updated_at": datetime.now(ZoneInfo("Europe/Vienna")).isoformat(
            timespec="seconds"
        ),
        "last_result": import_result,
    }
    schreibe_langzeitimport_status_firestore(new_status)
    aggregate = aktualisiere_langzeitaggregate(start, ende)
    return {**new_status, "aggregate": aggregate["metadata"]}


def durchschnitt(werte):
    """Berechnet einen gerundeten Durchschnitt ohne fehlende Werte."""
    zahlen = [wert for wert in werte if isinstance(wert, (int, float)) and wert > 0]
    return round(sum(zahlen) / len(zahlen), 1) if zahlen else None


def trend_label(werte):
    """Vergleicht erste und zweite Wochenhälfte grob und stabil."""
    zahlen = [wert for wert in werte if isinstance(wert, (int, float)) and wert > 0]
    if len(zahlen) < 4:
        return "zu wenig Daten"
    mitte = len(zahlen) // 2
    vorher = sum(zahlen[:mitte]) / len(zahlen[:mitte])
    nachher = sum(zahlen[mitte:]) / len(zahlen[mitte:])
    delta = nachher - vorher
    if abs(delta) < max(vorher * 0.05, 1):
        return "stabil"
    return "steigend" if delta > 0 else "fallend"


def gewohnheits_extreme(tage):
    """Ermittelt stärkste und schwächste Gewohnheit aus gespeicherten Tagesergebnissen."""
    statistik = {}
    for tag in tage:
        for gewohnheit in tag.get("gewohnheiten") or []:
            if isinstance(gewohnheit, dict):
                name = gewohnheit.get("name")
                ok = gewohnheit.get("ok")
            else:
                name, ok = gewohnheit
            eintrag = statistik.setdefault(name, {"ok": 0, "gesamt": 0})
            eintrag["gesamt"] += 1
            if ok:
                eintrag["ok"] += 1
    auswertbar = [
        (name, round(wert["ok"] / wert["gesamt"] * 100), wert["gesamt"])
        for name, wert in statistik.items()
        if wert["gesamt"] > 0
    ]
    if not auswertbar:
        return None, None
    auswertbar.sort(key=lambda eintrag: (eintrag[1], eintrag[2], eintrag[0]))
    schwaechste = auswertbar[0]
    staerkste = auswertbar[-1]
    return (
        {"name": staerkste[0], "quote": staerkste[1], "tage": staerkste[2]},
        {"name": schwaechste[0], "quote": schwaechste[1], "tage": schwaechste[2]},
    )


def summe_aktivitaetsminuten(tage):
    """Summiert Aktivitätsdauer aus der Tageshistorie, soweit Garmin sie geliefert hat."""
    minuten = 0
    for tag in tage:
        for aktivitaet in tag.get("aktivitaeten_gestern") or []:
            dauer = aktivitaet.get("dauer_min")
            if isinstance(dauer, (int, float)):
                minuten += dauer
    return round(minuten)


def wochenreview_empfehlungen(review):
    """Leitet einen Fokus, ein Risiko und eine konkrete Anpassung regelbasiert ab."""
    if review["schlaf_score_avg"] is not None and review["schlaf_score_avg"] < 70:
        return (
            "Schlafrhythmus stabilisieren",
            "Erholung bleibt begrenzt, wenn Schlafqualität unter 70 bleibt.",
            "Vier Abende mit fester Runterfahrzeit einplanen.",
        )
    if review["recovery_trend"] == "fallend":
        return (
            "Belastung dosieren",
            "Fallende Erholung bei weiterem Trainingsdruck.",
            "Maximal zwei intensive Einheiten und einen echten Ruhetag setzen.",
        )
    if review["habit_quote_avg"] is not None and review["habit_quote_avg"] < 70:
        return (
            "Basisgewohnheiten vereinfachen",
            "Zu viele offene Gewohnheiten erzeugen Reibung.",
            "Eine schwache Gewohnheit auf eine Minimalversion reduzieren.",
        )
    return (
        "Stabilität halten",
        "Zu viele neue Ziele könnten ein funktionierendes System verwässern.",
        "Eine bestehende Routine bewusst beibehalten und nur eine Sache verbessern.",
    )


def erstelle_wochenreview(tage, ende=None):
    """Berechnet ein strukturiertes Wochenreview aus Tageshistorie."""
    ende = ende or date.today()
    start = ende - timedelta(days=6)
    staerkste, schwaechste = gewohnheits_extreme(tage)
    review = {
        "woche_start": start.isoformat(),
        "woche_ende": ende.isoformat(),
        "tage_gefunden": len(tage),
        "schlafdauer_avg": durchschnitt([tag.get("schlafdauer_h") for tag in tage]),
        "schlaf_score_avg": durchschnitt([tag.get("schlaf_score") for tag in tage]),
        "schlaf_trend": trend_label([tag.get("schlaf_score") for tag in tage]),
        "recovery_avg": durchschnitt([tag.get("score") for tag in tage]),
        "recovery_trend": trend_label([tag.get("score") for tag in tage]),
        "body_battery_avg": durchschnitt([tag.get("body_battery") for tag in tage]),
        "hrv_avg": durchschnitt([tag.get("hrv") for tag in tage]),
        "schritte_summe": sum(tag.get("schritte") or 0 for tag in tage),
        "aktivitaeten_anzahl": sum(len(tag.get("aktivitaeten_gestern") or []) for tag in tage),
        "trainingsminuten_summe": summe_aktivitaetsminuten(tage),
        "habit_quote_avg": durchschnitt([tag.get("habit_quote") for tag in tage]),
        "staerkste_gewohnheit": staerkste,
        "schwaechste_gewohnheit": schwaechste,
    }
    fokus, risiko, anpassung = wochenreview_empfehlungen(review)
    review.update({
        "fokus_naechste_woche": fokus,
        "risiko_naechste_woche": risiko,
        "konkrete_anpassung": anpassung,
    })
    review["review_text"] = formatiere_wochenreview(review)
    return review


def formatiere_wochenreview(review):
    """Erzeugt einen kompakten Klartext für Firestore, Logs und spätere Coach-Ausgabe."""
    staerkste = review.get("staerkste_gewohnheit") or {}
    schwaechste = review.get("schwaechste_gewohnheit") or {}
    staerkste_text = (
        f"{staerkste.get('name')} ({staerkste.get('quote')}%)"
        if staerkste else "nicht verfügbar"
    )
    schwaechste_text = (
        f"{schwaechste.get('name')} ({schwaechste.get('quote')}%)"
        if schwaechste else "nicht verfügbar"
    )
    return "\n".join([
        f"WOCHENREVIEW {review['woche_start']} bis {review['woche_ende']}",
        "",
        f"Schlaf: Ø {na(review['schlafdauer_avg'], 'h')}, Score Ø {na(review['schlaf_score_avg'])}, Trend {review['schlaf_trend']}",
        f"Erholung: Score Ø {na(review['recovery_avg'])}, Trend {review['recovery_trend']}",
        f"Bewegung: {review['aktivitaeten_anzahl']} Aktivitäten, {review['trainingsminuten_summe']} min, {review['schritte_summe']} Schritte",
        f"Gewohnheiten: Ø {na(review['habit_quote_avg'], '%')}, stärkste: {staerkste_text}, schwächste: {schwaechste_text}",
        "",
        f"Fokus nächste Woche: {review['fokus_naechste_woche']}",
        f"Risiko: {review['risiko_naechste_woche']}",
        f"Konkrete Anpassung: {review['konkrete_anpassung']}",
    ])


def schreibe_wochenreview_firestore(review):
    """Speichert das aktuelle Wochenreview und zusätzlich ein Wochenarchiv."""
    body = {"fields": {k: firestore_wert_schreiben(v) for k, v in review.items()}}
    aktuelle_url = firestore_user_url("health", "morning_report", "reviews", "aktuell")
    archiv_url = firestore_user_url(
        "health", "morning_report", "reviews",
        f"{review['woche_start']}_{review['woche_ende']}",
    )
    for url in (aktuelle_url, archiv_url):
        resp = firestore_request(
            "patch",
            url,
            headers=firestore_auth_headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()


def schreibe_heutige_aktivitaeten_firestore(tag, aktivitaeten):
    """Aktualisiert ausschließlich die heutigen Aktivitäten im Report-Dokument.

    Dieser getrennte Schreibweg ist für die Abendabfrage des Fitnesscoach-GPT
    gedacht. Ein Firestore-Update-Mask begrenzt den PATCH ausdrücklich auf drei
    Felder. Schlaf, Erholungsscore, Gewohnheiten und der morgens versendete Text
    bleiben dadurch unverändert, und es wird keine zweite Nachricht verschickt.
    """
    aktualisiert_am = datetime.now(ZoneInfo("Europe/Vienna")).isoformat(timespec="seconds")
    felder = {
        "aktivitaeten_heute": aktivitaeten,
        "aktivitaeten_heute_datum": tag,
        "aktivitaeten_heute_aktualisiert_am": aktualisiert_am,
    }
    body = {"fields": {k: firestore_wert_schreiben(v) for k, v in felder.items()}}
    params = [("updateMask.fieldPaths", feldname) for feldname in felder]
    for pfad in (
        firestore_user_url("health", "morning_report"),
        firestore_legacy_report_url(),
    ):
        resp = firestore_request(
            "patch",
            pfad,
            headers=firestore_auth_headers(),
            params=params,
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
    return aktualisiert_am


def berechne_erholung(daten):
    """Berechnet einen einfachen, nachvollziehbaren Erholungsscore von 0 bis 100.

    Das ist bewusst ein regelbasiertes Orientierungssignal und keine medizinische
    Bewertung. Body Battery, Schlaf, HRV, Stress und Training Readiness liefern
    gewichtete Teilpunkte. Auffällige Bereiche werden zusätzlich als verständliche
    Hinweise zurückgegeben, damit die Zahl nicht ohne Begründung steht.
    """
    score = 0
    gruende = []

    bb = daten.get("body_battery")
    if bb is None:
        gruende.append("Body Battery nicht verfügbar")
    elif bb >= 75:
        score += 30
    elif bb >= 50:
        score += 22
        gruende.append(f"Body Battery mittelmäßig ({bb})")
    elif bb >= 25:
        score += 12
        gruende.append(f"Body Battery niedrig ({bb})")
    else:
        gruende.append(f"Body Battery sehr niedrig ({bb})")

    ss = daten.get("schlaf_score")
    if ss is None:
        gruende.append("Schlaf-Score nicht verfügbar")
    elif ss >= 80:
        score += 25
    elif ss >= 60:
        score += 18
        gruende.append(f"Schlaf-Score mäßig ({ss})")
    elif ss >= 40:
        score += 9
        gruende.append(f"Schlaf-Score schlecht ({ss})")
    else:
        gruende.append(f"Schlaf-Score sehr schlecht ({ss})")

    h = daten.get("schlafdauer_h")
    if h is None:
        gruende.append("Schlafdauer nicht verfügbar")
    elif h >= 7.5:
        score += 15
    elif h >= 6.5:
        score += 10
        gruende.append(f"Schlafdauer knapp ({h}h)")
    elif h >= 5.5:
        score += 5
        gruende.append(f"Schlafdauer zu kurz ({h}h)")
    else:
        gruende.append(f"Schlafdauer sehr kurz ({h}h)")

    hrv = daten.get("hrv")
    if hrv is not None:
        if hrv >= 50:
            score += 10
        elif hrv >= 35:
            score += 6
            gruende.append(f"HRV leicht reduziert ({hrv})")
        else:
            gruende.append(f"HRV niedrig ({hrv})")

    stress = daten.get("stress_avg")
    if stress is not None:
        if stress <= 25:
            score += 10
        elif stress <= 50:
            score += 6
            gruende.append(f"Stresslevel erhöht ({stress})")
        else:
            gruende.append(f"Stresslevel hoch ({stress})")

    tr = daten.get("tr_score")
    if tr is not None:
        if tr >= 75:
            score += 10
        elif tr >= 50:
            score += 6
            gruende.append(f"Training Readiness mäßig ({tr})")
        else:
            gruende.append(f"Training Readiness niedrig ({tr})")

    return min(score, 100), gruende


def trainingsempfehlung(score):
    """Ordnet den Score einer groben Trainingskategorie für das Dashboard zu.

    Diese Kategorie ersetzt keine subjektive Einschätzung zu Schmerz, Muskelkater
    oder Krankheit. Der eigene Fitnesscoach soll diese Faktoren zusätzlich erfragen.
    """
    if score >= 75:
        return "VOLLES TRAINING", "Alle geplanten Einheiten wie vorgesehen."
    elif score >= 55:
        return "NORMALES TRAINING", "Training wie geplant, auf Körpersignale achten."
    elif score >= 35:
        return "REDUZIERTE INTENSITÄT", "Volumen -20%, Intensität -1 Zone. Kein HIIT heute."
    else:
        return "REGENERATION", "Nur lockeres Gehen, Mobilität oder komplette Pause."


def na(val, einheit=""):
    """Formatiert vorhandene Werte und kennzeichnet fehlende Werte als ``n/a``."""
    return f"{val}{einheit}" if val is not None else "n/a"


def formatiere_aktivitaet(aktivitaet):
    """Erzeugt kompakte Reportzeilen für einen beliebigen Garmin-Aktivitätstyp."""
    typ = (aktivitaet.get("typ") or "unbekannt").replace("_", " ").title()
    name = aktivitaet.get("name") or typ
    startzeit = aktivitaet.get("startzeit")
    uhrzeit = None
    if isinstance(startzeit, str):
        # Garmin verwendet typischerweise ``YYYY-MM-DD HH:MM:SS``; die Behandlung
        # von ``T`` hält die Ausgabe auch für ISO-ähnliche Antworten stabil.
        zeitanteil = startzeit.replace("T", " ").split(" ")[-1]
        if len(zeitanteil) >= 5 and ":" in zeitanteil:
            uhrzeit = zeitanteil[:5]

    titel = f"  • {name} [{typ}]"
    if uhrzeit:
        titel += f" um {uhrzeit}"

    details = []
    for feld, einheit, bezeichnung in (
        ("dauer_min", " min", "Dauer"),
        ("distanz_km", " km", "Distanz"),
        ("kalorien", " kcal", "Kalorien"),
        ("durchschnittspuls", " bpm", "Ø Puls"),
        ("maximalpuls", " bpm", "Max. Puls"),
        ("hoehenmeter", " Hm", "Anstieg"),
        ("trainingseffekt_aerob", "", "TE aerob"),
        ("trainingseffekt_anaerob", "", "TE anaerob"),
    ):
        wert = aktivitaet.get(feld)
        if wert is not None:
            details.append(f"{bezeichnung}: {wert}{einheit}")

    return [titel] + (["    " + " | ".join(details)] if details else [])


def formatiere_reportdatum(datum_iso, heute=None):
    """Formatiert das Reportdatum lesbar und markiert den heutigen Report."""
    try:
        reportdatum = date.fromisoformat(datum_iso)
    except (TypeError, ValueError):
        return str(datum_iso)

    heute = heute or date.today()
    prefix = "heute, " if reportdatum == heute else ""
    monat = MONATE_DE.get(reportdatum.month, reportdatum.strftime("%B"))
    return f"{prefix}{reportdatum.day}. {monat} {reportdatum.year}"


def fehlende_garmin_werte(daten):
    """Nennt zentrale Garmin-Werte, die im aktuellen Abruf nicht verfügbar sind."""
    felder = [
        ("Trainingsbereitschaft", daten.get("tr_score")),
        ("VO₂max", daten.get("vo2max")),
        ("wöchentliche Intensitätsminuten", daten.get("int_min_woche")),
    ]
    return [name for name, wert in felder if wert is None]


def erstelle_text(daten, score, gruende, gewohnheiten=None):
    """Baut den kanonischen Klartextbericht für alle Ausgabekanäle.

    Derselbe Text wird lokal gespeichert, per Telegram/E-Mail gesendet und als
    ``report_text`` in Firestore abgelegt. Dadurch analysiert der GPT genau den
    Bericht, den der Benutzer morgens tatsächlich gesehen hat.
    """
    t = "─" * 40
    zeilen = [
        "═" * 40,
        f"  MORGENREPORT  {daten['datum']}",
        "═" * 40,
        "",
        f"  Reportdatum: {formatiere_reportdatum(daten['datum'])}",
    ]
    fehlende_werte = fehlende_garmin_werte(daten)
    if fehlende_werte:
        zeilen.append(f"  Nicht verfügbar: {', '.join(fehlende_werte)}.")
    zeilen += [
        "",
        "  SCHLAF",
        t,
        f"  Schlafdauer:        {na(daten['schlafdauer_h'], 'h')}",
        f"  Schlaf-Score:       {na(daten['schlaf_score'])}",
        f"  Tiefschlaf:         {na(daten['tief_min'], ' min')}",
        f"  REM-Schlaf:         {na(daten['rem_min'], ' min')}",
        f"  Leichtschlaf:       {na(daten['leicht_min'], ' min')}",
        f"  Wachzeit:           {na(daten['wach_min'], ' min')}",
        "",
        "  ERHOLUNG",
        t,
        f"  Body Battery:       {na(daten['body_battery'])}",
        f"  HRV:                {na(daten['hrv'])}",
        f"  Ruhepuls:           {na(daten['ruhepuls'], ' bpm')}",
        f"  Stresslevel:        {na(daten['stress_avg'])}",
        f"  Training Readiness: {na(daten['tr_score'])} ({na(daten['tr_level'])})",
        "",
        "  AKTIVITÄT",
        t,
        f"  Schritte gestern:   {na(daten['schritte'])}",
        f"  Intensitätsmin/Wo:  {na(daten['int_min_woche'])}",
        f"  VO2 Max:            {na(daten['vo2max'])}",
        "",
    ]
    zeilen += ["  AKTIVITÄTEN GESTERN", t]
    aktivitaeten = daten.get("aktivitaeten_gestern") or []
    if aktivitaeten:
        for aktivitaet in aktivitaeten:
            zeilen.extend(formatiere_aktivitaet(aktivitaet))
    else:
        zeilen.append("  Gestern wurde keine separate Garmin-Aktivität aufgezeichnet.")
    zeilen += [
        "",
        "  GESUNDHEIT",
        t,
        f"  SpO2:               {na(daten['spo2'], '%')}",
        f"  Atemfrequenz:       {na(daten['atemfrequenz'], ' /min')}",
        "",
    ]
    if gewohnheiten:
        ergebnisse, quote = gewohnheiten
        zeilen += ["  GEWOHNHEITEN GESTERN", t]
        for name, ok in ergebnisse:
            zeilen.append(f"  {'✔' if ok else '✗'} {name}")
        if quote is not None:
            zeilen.append(f"  Erfolgsquote: {quote}%")
        zeilen.append("")
    zeilen += [
        t,
        f"  Erholungsscore: {score}/100",
    ]
    if gruende:
        zeilen += ["", "  Hinweise:"]
        for g in gruende:
            zeilen.append(f"   - {g}")
    zeilen.append("═" * 40)
    return "\n".join(zeilen)


def speichern(text, daten):
    """Speichert eine lokale, nach Datum benannte Kopie zur Nachvollziehbarkeit."""
    ordner = os.path.join(BASE_DIR, "reports")
    os.makedirs(ordner, exist_ok=True)
    dateiname = os.path.join(ordner, f"report_{daten['datum']}.txt")
    with open(dateiname, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Report gespeichert: {dateiname}")


def sende_email(text, daten):
    """Versendet den Bericht optional über Gmail mit einem App-Passwort.

    Normale Gmail-Passwörter gehören ausdrücklich nicht in dieses Projekt. Fehlt
    die vollständige optionale Konfiguration, meldet die Funktion einen Fehler,
    den main() isoliert behandelt, sodass Telegram weiterhin funktionieren kann.
    """
    if not GMAIL_ADRESSE or not GMAIL_APP_PASSWORT or not EMPFAENGER:
        raise RuntimeError("GMAIL_ADRESSE/GMAIL_APP_PASSWORT/MORGENREPORT_EMPFAENGER nicht vollstaendig")
    nachricht = MIMEText(text, "plain", "utf-8")
    nachricht["Subject"] = f"Morgenreport {daten['datum']}"
    nachricht["From"] = GMAIL_ADRESSE
    nachricht["To"] = EMPFAENGER

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADRESSE, GMAIL_APP_PASSWORT)
        server.sendmail(GMAIL_ADRESSE, EMPFAENGER, nachricht.as_string())
    print(f"E-Mail gesendet an: {EMPFAENGER}")


def sende_telegram(text):
    """Versendet den Bericht robust in Telegram-kompatiblen Teilnachrichten.

    Telegram begrenzt Nachrichten auf 4096 Zeichen; 4000 lässt etwas Reserve.
    Sowohl HTTP-Status als auch das Telegram-eigene ``ok`` werden geprüft, damit
    ein API-Fehler nicht irrtümlich als erfolgreicher Versand gilt.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID nicht gesetzt")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram erlaubt max. 4096 Zeichen pro Nachricht -> in Teile aufsplitten
    for i in range(0, len(text), 4000):
        teil = text[i:i + 4000]
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": teil},
            timeout=20,
        )
        resp.raise_for_status()
        antwort = resp.json()
        if not antwort.get("ok"):
            raise RuntimeError(f"Telegram API meldet Fehler: {antwort.get('description', 'unbekannt')}")
    print("Telegram-Nachricht gesendet.")


def argparse_iso_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Datum muss YYYY-MM-DD entsprechen") from exc


def parse_args(argv=None):
    """Definiert CLI-Optionen separat, damit sie ohne echten Start testbar sind."""
    parser = argparse.ArgumentParser(description="Garmin-Morgenreport erstellen und versenden")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report erstellen und lokal speichern, aber nichts versenden oder in Firestore schreiben",
    )
    parser.add_argument(
        "--heutige-aktivitaeten",
        action="store_true",
        help="Nur heutige Garmin-Aktivitaeten für die GPT-Abendabfrage aktualisieren",
    )
    parser.add_argument(
        "--schlaf-nachsynchronisieren",
        action="store_true",
        help="Nur bei morgens fehlenden Schlafdaten Schlaf-/Recovery-Werte erneut laden",
    )
    parser.add_argument(
        "--wochenreview",
        action="store_true",
        help="Aus den letzten 7 Tageshistorien ein Wochenreview berechnen und speichern",
    )
    parser.add_argument(
        "--garmin-rueckimport",
        action="store_true",
        help="Fehlende Garmin-Tageswerte und private Rohquellen nach Firestore importieren",
    )
    parser.add_argument(
        "--garmin-langzeitimport",
        action="store_true",
        help="Vollständige Garmin-Historie paketweise und fortsetzbar importieren",
    )
    parser.add_argument(
        "--tage",
        type=int,
        default=28,
        choices=range(7, 29),
        metavar="7..28",
        help="Zeitraum für den Garmin-Rückimport (Standard: 28 Tage)",
    )
    parser.add_argument(
        "--von",
        type=argparse_iso_date,
        help="Optionaler Beginn des Langzeitimports im Format YYYY-MM-DD",
    )
    parser.add_argument(
        "--batch-tage",
        type=int,
        default=GARMIN_LANGZEIT_BATCH_TAGE,
        choices=range(7, 91),
        metavar="7..90",
        help="Tage pro fortsetzbarem Langzeitlauf (Standard: 28)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    """Orchestriert einen vollständigen Reportlauf und gibt bei Erfolg 0 zurück.

    Ein lokaler Report wird immer erzeugt. Im Dry-Run endet die Pipeline vor allen
    externen Schreib- und Versandaktionen. Im Normalbetrieb muss mindestens ein
    Versandkanal erfolgreich sein; erst danach wird der GPT-Datensatz aktualisiert.
    """
    args = parse_args(argv)
    print("Verbinde mit Garmin Connect...")
    client = login()
    print("OK\n")

    if args.garmin_rueckimport:
        if args.dry_run:
            print("TESTMODUS: Garmin-Rückimport wurde ohne Schreibzugriff übersprungen.")
            return 0
        print(f"Starte Garmin-Rückimport für die letzten {args.tage} Tage...")
        sync = synchronisiere_garmin_historie(client, tage=args.tage)
        print(
            "Garmin-Rückimport abgeschlossen: "
            f"{sync['historientage_ergaenzt']} Tageswerte und "
            f"{sync['historientage_aktualisiert']} vorhandene Tage aktualisiert, "
            f"{sync['rohtage_ergaenzt']} Rohdatentage ergänzt; "
            f"{sync['uebersprungen']} bereits vollständig, "
            f"{sync['ohne_daten']} ohne Garmin-Daten."
        )
        return 0

    if args.garmin_langzeitimport:
        if args.dry_run:
            print("TESTMODUS: Garmin-Langzeitimport ohne Schreibzugriff übersprungen.")
            return 0
        start_hinweis = args.von.isoformat() if args.von else "automatisch ab erstem Garmin-Jahr"
        print(
            f"Starte fortsetzbaren Garmin-Langzeitimport ({start_hinweis}, "
            f"{args.batch_tage} Tage pro Lauf)..."
        )
        sync = synchronisiere_garmin_langzeit(
            client, start=args.von, batch_tage=args.batch_tage
        )
        print(
            "Garmin-Langzeitimport: "
            f"{sync['status']}, {sync['processed_days']}/{sync['total_days']} Tage "
            f"({sync['progress_percent']} %), noch {sync['remaining_days']} Tage."
        )
        print(
            "Langzeitvergleich aktualisiert: "
            f"{sync['aggregate']['tage_gefunden']} Tage von "
            f"{sync['aggregate']['von']} bis {sync['aggregate']['bis']}."
        )
        return 0

    if args.schlaf_nachsynchronisieren:
        heute = date.today().isoformat()
        aktueller_report = hole_aktuellen_morgenreport_firestore()
        if aktueller_report.get("datum") != heute:
            print(f"Kein heutiger Morgenreport in Firestore gefunden ({heute}); ueberspringe.")
            return 0
        if not aktueller_report.get("sleep_data_incomplete"):
            print("Schlafdaten waren im Morgenreport bereits vollstaendig; ueberspringe.")
            return 0
        print(f"Lade Garmin-Schlafdaten erneut ({heute})...")
        daten = hole_schlaf_recovery_daten(client, heute)
        if args.dry_run:
            status = "unvollstaendig" if schlafdaten_unvollstaendig(daten) else "vollstaendig"
            print(f"TESTMODUS: Schlafdaten erneut geladen ({status}); Firestore uebersprungen.")
            return 0
        aktualisiert_am = schreibe_schlaf_nachsynchronisierung_firestore(daten)
        print(f"Schlafdaten nachsynchronisiert ({aktualisiert_am}).")
        return 0

    if args.wochenreview:
        ende = date.today()
        start = ende - timedelta(days=6)
        print(f"Lade Historie fuer Wochenreview ({start.isoformat()} bis {ende.isoformat()})...")
        tage = hole_historie_zeitraum(start, ende)
        review = erstelle_wochenreview(tage, ende)
        print(f"\n{review['review_text']}\n")
        if args.dry_run:
            print("TESTMODUS: Wochenreview berechnet; Firestore wurde uebersprungen.")
            return 0
        schreibe_wochenreview_firestore(review)
        print("Wochenreview in Firestore gespeichert.")
        return 0

    # Der Abendmodus hat absichtlich einen sehr kurzen Datenfluss: Garmin lesen
    # und drei abgegrenzte Firestore-Felder aktualisieren. Er erzeugt weder einen
    # Report noch E-Mail/Telegram-Nachrichten oder einen Versandmarker.
    if args.heutige_aktivitaeten:
        heute = date.today().isoformat()
        print(f"Lade Garmin-Aktivitaeten fuer heute ({heute})...")
        aktivitaeten = hole_aktivitaeten(client, heute)
        if args.dry_run:
            print(f"TESTMODUS: {len(aktivitaeten)} Aktivitaet(en) geladen; Firestore uebersprungen.")
            return 0
        aktualisiert_am = schreibe_heutige_aktivitaeten_firestore(heute, aktivitaeten)
        print(
            f"Heutige Aktivitaeten aktualisiert: {len(aktivitaeten)} "
            f"Eintrag/Eintraege ({aktualisiert_am})."
        )
        return 0

    print("Lade Garmin-Daten...")
    daten = hole_daten(client)
    score, gruende = berechne_erholung(daten)
    # Empfehlung wird nur noch für die Dashboard-Kachel in Firestore gebraucht
    empfehlung, _ = trainingsempfehlung(score)

    gewohnheiten = None
    habit_quote = None
    habit_ergebnisse = None
    try:
        liste = hole_gewohnheiten()
        ergebnisse, habit_quote = gewohnheiten_gestern(liste)
        habit_ergebnisse = ergebnisse
        gewohnheiten = (ergebnisse, habit_quote)
    except Exception as e:
        print(f"Gewohnheiten konnten nicht geladen werden: {e}")

    text = erstelle_text(daten, score, gruende, gewohnheiten)
    print(f"\n{text}\n")
    speichern(text, daten)

    if args.dry_run:
        print("TESTMODUS: E-Mail, Telegram und Firestore wurden uebersprungen.")
        return 0

    erfolgreiche_kanaele = []
    try:
        sende_email(text, daten)
        erfolgreiche_kanaele.append("E-Mail")
    except Exception as e:
        print(f"E-Mail konnte nicht gesendet werden: {e}")

    try:
        sende_telegram(text)
        erfolgreiche_kanaele.append("Telegram")
    except Exception as e:
        print(f"Telegram-Nachricht konnte nicht gesendet werden: {e}")

    if not erfolgreiche_kanaele:
        raise RuntimeError(
            "Morgenreport wurde lokal erstellt, aber kein Versandkanal war erfolgreich."
        )

    print(f"Erfolgreiche Versandkanaele: {', '.join(erfolgreiche_kanaele)}")

    # Firestore wird erst nach mindestens einem erfolgreichen Versand aktualisiert.
    # So erscheint im Fitnesscoach kein Report, dessen eigentlicher Tagesversand
    # vollständig fehlgeschlagen ist. Ein Firestore-Fehler verhindert den bereits
    # erfolgreichen Telegram-Versand jedoch nicht nachträglich.
    report_gespeichert = False
    try:
        schreibe_morgenreport_firestore(daten, score, empfehlung, habit_quote, text, habit_ergebnisse)
        report_gespeichert = True
    except Exception as e:
        print(f"Report konnte nicht in Firestore geschrieben werden: {e}")

    if report_gespeichert and "_garmin_rohquellen" in daten:
        try:
            print("Prüfe die Garmin-Historie der letzten 28 Tage auf Lücken...")
            sync = synchronisiere_garmin_historie(
                client,
                vorab_daten={daten["datum"]: daten},
            )
            print(
                "Garmin-Historie synchronisiert: "
                f"{sync['historientage_ergaenzt']} Tageswerte und "
                f"{sync['historientage_aktualisiert']} vorhandene Tage aktualisiert, "
                f"{sync['rohtage_ergaenzt']} Rohdatentage ergänzt; "
                f"{sync['uebersprungen']} bereits vollständig."
            )
        except Exception as e:
            # Der bereits versendete Morgenreport bleibt erfolgreich. Der nächste
            # Lauf setzt die noch fehlenden Historientage automatisch fort.
            print(f"Garmin-Rückimport konnte nicht vollständig abgeschlossen werden: {e}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GarminLoginError as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        raise SystemExit(1)
