"""Erzeugt und verteilt den täglichen Garmin-Morgenreport.

Dieses Modul ist die zentrale Pipeline des Projekts: Es authentifiziert sich bei
Garmin Connect, normalisiert mehrere Garmin-Endpunkte, ergänzt Gewohnheiten aus
Firestore, berechnet einen transparenten Erholungsscore und verteilt denselben
Bericht an Datei, Telegram/E-Mail sowie Firestore für die GPT Action.

Zugangsdaten werden ausschließlich aus Umgebungsvariablen beziehungsweise der
lokalen, von Git ausgeschlossenen .env-Datei geladen.
"""

import argparse
import os
import sys
import smtplib
import requests
import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest
from email.mime.text import MIMEText
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from garminconnect import Garmin
from dotenv import load_dotenv

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
_FIRESTORE_CREDENTIALS = None

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
    sleep_dto      = sleep.get("dailySleepDTO", {})
    schlafdauer_h  = round((sleep_dto.get("sleepTimeSeconds") or 0) / 3600, 1)
    schlaf_score   = sleep_dto.get("sleepScores", {}).get("overall", {}).get("value") or 0
    tief_min       = round((sleep_dto.get("deepSleepSeconds") or 0) / 60)
    leicht_min     = round((sleep_dto.get("lightSleepSeconds") or 0) / 60)
    rem_min        = round((sleep_dto.get("remSleepSeconds") or 0) / 60)
    wach_min       = round((sleep_dto.get("awakeSleepSeconds") or 0) / 60)

    hrv = None
    if hrv_data:
        hrv = hrv_data.get("hrvSummary", {}).get("lastNightAvg")

    daten = {
        "datum":          tag,
        "body_battery":   stats.get("bodyBatteryMostRecentValue") or 0,
        "ruhepuls":       stats.get("restingHeartRate") or 0,
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


def hole_daten(client):
    """Lädt Garmin-Messwerte und vereinheitlicht sie in einem flachen Dictionary.

    Schlaf- und Erholungswerte beziehen sich auf heute beziehungsweise die letzte
    Nacht. Stress und Schritte werden für gestern geladen, weil der heutige Tag am
    Morgen noch unvollständig wäre. Die flache Struktur ist der gemeinsame Vertrag
    für Scoreberechnung, Textausgabe, Tests und Firestore.
    """
    today = date.today().isoformat()
    gestern = (date.today() - timedelta(days=1)).isoformat()

    stats      = sicher(client.get_stats, today, default={})
    sleep      = sicher(client.get_sleep_data, today, default={})
    hrv_data   = sicher(client.get_hrv_data, today, default={})
    stress     = sicher(client.get_stress_data, gestern, default={})
    steps      = sicher(client.get_steps_data, gestern, default=[])
    spo2       = sicher(client.get_spo2_data, today, default={})
    resp       = sicher(client.get_respiration_data, today, default={})
    readiness  = sicher(client.get_training_readiness, today, default={})
    intensity  = sicher(client.get_weekly_intensity_minutes, today, default={})
    metrics    = sicher(client.get_max_metrics, today, default=[])
    aktivitaeten_gestern = hole_aktivitaeten(client, gestern)

    schlaf_recovery = extrahiere_schlaf_recovery_daten(today, stats, sleep, hrv_data)

    # Stress
    stress_avg = None
    if stress:
        stress_avg = stress.get("avgStressLevel")

    # Schritte
    schritte = None
    if steps and isinstance(steps, list) and len(steps) > 0:
        total = sum(s.get("steps", 0) for s in steps if isinstance(s, dict))
        schritte = total if total > 0 else None

    # SpO2
    spo2_avg = None
    if spo2:
        spo2_avg = spo2.get("averageSpO2")

    # Atemfrequenz
    atem_avg = None
    if resp:
        atem_avg = resp.get("avgWakingRespirationValue")

    # Training Readiness
    tr_score = None
    tr_level = None
    if readiness:
        if isinstance(readiness, list) and len(readiness) > 0:
            tr_score = readiness[0].get("score")
            tr_level = readiness[0].get("level")
        elif isinstance(readiness, dict):
            tr_score = readiness.get("score")
            tr_level = readiness.get("level")

    # Intensitätsminuten (Woche)
    int_min_woche = None
    if intensity:
        mod  = intensity.get("weeklyModerateIntensityMinutes") or 0
        vig  = intensity.get("weeklyVigorousIntensityMinutes") or 0
        if mod or vig:
            int_min_woche = mod + vig * 2  # WHO-Formel: intensive Minuten zählen doppelt

    # VO2 Max
    vo2max = None
    if metrics and isinstance(metrics, list):
        for m in metrics:
            v = m.get("generic", {}).get("vo2MaxPreciseValue")
            if v:
                vo2max = round(v, 1)
                break

    return {
        "datum":          today,
        "body_battery":   schlaf_recovery["body_battery"],
        "ruhepuls":       schlaf_recovery["ruhepuls"],
        "schlafdauer_h":  schlaf_recovery["schlafdauer_h"],
        "schlaf_score":   schlaf_recovery["schlaf_score"],
        "tief_min":       schlaf_recovery["tief_min"],
        "leicht_min":     schlaf_recovery["leicht_min"],
        "rem_min":        schlaf_recovery["rem_min"],
        "wach_min":       schlaf_recovery["wach_min"],
        "hrv":            schlaf_recovery["hrv"],
        "stress_avg":     stress_avg,
        "schritte":       schritte,
        "spo2":           spo2_avg,
        "atemfrequenz":   atem_avg,
        "tr_score":       tr_score,
        "tr_level":       tr_level,
        "int_min_woche":  int_min_woche,
        "vo2max":         vo2max,
        "aktivitaeten_gestern": aktivitaeten_gestern,
        "sleep_data_incomplete": schlaf_recovery["sleep_data_incomplete"],
    }


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


def hole_gewohnheiten():
    """Lädt die Tracker-Konfiguration für die Auswertung des Vortags.

    Der Zugriff erfolgt mit einem kurzlebigen Google-IAM-Token. HTTP-Fehler werden
    nicht verborgen; main() behandelt Gewohnheiten bewusst als optionale Ergänzung.
    """
    resp = requests.get(
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
    resp = requests.get(
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

    Der Schreibzugriff verwendet Google-IAM mit kurzlebigen Zugangsdaten. Es gibt
    keinen dauerhaft gültigen Firestore-Schlüssel mehr im Workflow.
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
    # Die Firestore-REST-API erwartet pro Wert einen expliziten Typ. Die zentrale
    # Hilfsfunktion hält diese technische Darstellung aus der Fachlogik heraus.
    body = {"fields": {k: firestore_wert_schreiben(v) for k, v in felder.items()}}

    # PATCH aktualisiert das feste "aktueller Report"-Dokument. Ein Timeout
    # verhindert, dass ein gestörter Firestore-Aufruf den Workflow endlos blockiert.
    resp = requests.patch(
        firestore_user_url("health", "morning_report"),
        headers=firestore_auth_headers(),
        json=body,
        timeout=15,
    )

    # HTTP-Fehler müssen sichtbar werden; andernfalls könnte der Workflow Erfolg
    # melden, obwohl der GPT am nächsten Morgen noch veraltete Daten erhält.
    resp.raise_for_status()

    schreibe_tageshistorie_firestore(daten, score, empfehlung, habit_quote, habit_ergebnisse)


def tageshistorie_felder(daten, score=None, empfehlung=None, habit_quote=None, habit_ergebnisse=None):
    """Reduziert den Report auf strukturierte Felder für Trendanalysen."""
    return {
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


def schreibe_tageshistorie_firestore(daten, score, empfehlung, habit_quote, habit_ergebnisse=None):
    """Schreibt ein Tagesdokument als privates Gedächtnis für spätere Trends."""
    felder = tageshistorie_felder(daten, score, empfehlung, habit_quote, habit_ergebnisse)
    body = {"fields": {k: firestore_wert_schreiben(v) for k, v in felder.items()}}
    resp = requests.patch(
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
    ):
        resp = requests.patch(
            pfad,
            headers=firestore_auth_headers(),
            params=params,
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
    return aktualisiert_am


def daterange(start, ende):
    """Iteriert inklusive Start und Ende über ISO-Datumswerte."""
    tag = start
    while tag <= ende:
        yield tag.isoformat()
        tag += timedelta(days=1)


def hole_historientag_firestore(tag):
    """Lädt ein einzelnes Tageshistorien-Dokument; fehlende Tage ergeben None."""
    resp = requests.get(
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
    """Lädt vorhandene Tageshistorien-Dokumente für einen kompakten Zeitraum."""
    tage = []
    for tag in daterange(start, ende):
        eintrag = hole_historientag_firestore(tag)
        if eintrag:
            tage.append(eintrag)
    return tage


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
        resp = requests.patch(
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
    resp = requests.patch(
        firestore_user_url("health", "morning_report"),
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

    bb = daten["body_battery"]
    if bb >= 75:
        score += 30
    elif bb >= 50:
        score += 22
        gruende.append(f"Body Battery mittelmäßig ({bb})")
    elif bb >= 25:
        score += 12
        gruende.append(f"Body Battery niedrig ({bb})")
    else:
        gruende.append(f"Body Battery sehr niedrig ({bb})")

    ss = daten["schlaf_score"]
    if ss >= 80:
        score += 25
    elif ss >= 60:
        score += 18
        gruende.append(f"Schlaf-Score mäßig ({ss})")
    elif ss >= 40:
        score += 9
        gruende.append(f"Schlaf-Score schlecht ({ss})")
    else:
        gruende.append(f"Schlaf-Score sehr schlecht ({ss})")

    h = daten["schlafdauer_h"]
    if h >= 7.5:
        score += 15
    elif h >= 6.5:
        score += 10
        gruende.append(f"Schlafdauer knapp ({h}h)")
    elif h >= 5.5:
        score += 5
        gruende.append(f"Schlafdauer zu kurz ({h}h)")
    else:
        gruende.append(f"Schlafdauer sehr kurz ({h}h)")

    hrv = daten["hrv"]
    if hrv:
        if hrv >= 50:
            score += 10
        elif hrv >= 35:
            score += 6
            gruende.append(f"HRV leicht reduziert ({hrv})")
        else:
            gruende.append(f"HRV niedrig ({hrv})")

    stress = daten["stress_avg"]
    if stress:
        if stress <= 25:
            score += 10
        elif stress <= 50:
            score += 6
            gruende.append(f"Stresslevel erhöht ({stress})")
        else:
            gruende.append(f"Stresslevel hoch ({stress})")

    tr = daten["tr_score"]
    if tr:
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
    try:
        schreibe_morgenreport_firestore(daten, score, empfehlung, habit_quote, text, habit_ergebnisse)
    except Exception as e:
        print(f"Report konnte nicht in Firestore geschrieben werden: {e}")

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
