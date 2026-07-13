import argparse
import os
import sys
import smtplib
import requests
from email.mime.text import MIMEText
from datetime import date, timedelta
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
# Zugangsschlüssel: Teil der Dokumentpfade, ohne ihn verweigern die Firestore-Regeln jeden Zugriff
TRACKER_SECRET = os.environ.get("TRACKER_SECRET", "")

TOKEN_ORDNER = os.path.join(BASE_DIR, ".garmin_tokens")


class GarminLoginError(RuntimeError):
    """Garmin-Anmeldung konnte nicht ohne Benutzereingabe abgeschlossen werden."""


def login():
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
    try:
        return fn(*args)
    except Exception:
        return default


def hole_daten(client):
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

    # Schlaf
    sleep_dto      = sleep.get("dailySleepDTO", {})
    schlafdauer_h  = round((sleep_dto.get("sleepTimeSeconds") or 0) / 3600, 1)
    schlaf_score   = sleep_dto.get("sleepScores", {}).get("overall", {}).get("value") or 0
    tief_min       = round((sleep_dto.get("deepSleepSeconds") or 0) / 60)
    leicht_min     = round((sleep_dto.get("lightSleepSeconds") or 0) / 60)
    rem_min        = round((sleep_dto.get("remSleepSeconds") or 0) / 60)
    wach_min       = round((sleep_dto.get("awakeSleepSeconds") or 0) / 60)

    # HRV
    hrv = None
    if hrv_data:
        hrv = hrv_data.get("hrvSummary", {}).get("lastNightAvg")

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
        "body_battery":   stats.get("bodyBatteryMostRecentValue") or 0,
        "ruhepuls":       stats.get("restingHeartRate") or 0,
        "schlafdauer_h":  schlafdauer_h,
        "schlaf_score":   schlaf_score,
        "tief_min":       tief_min,
        "leicht_min":     leicht_min,
        "rem_min":        rem_min,
        "wach_min":       wach_min,
        "hrv":            hrv,
        "stress_avg":     stress_avg,
        "schritte":       schritte,
        "spo2":           spo2_avg,
        "atemfrequenz":   atem_avg,
        "tr_score":       tr_score,
        "tr_level":       tr_level,
        "int_min_woche":  int_min_woche,
        "vo2max":         vo2max,
    }


def firestore_wert_lesen(v):
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
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if v is None:
        return {"nullValue": None}
    return {"stringValue": str(v)}


def hole_gewohnheiten():
    if not TRACKER_SECRET:
        raise RuntimeError("TRACKER_SECRET nicht gesetzt")
    resp = requests.get(f"{FIRESTORE_BASIS}/tracker/gewohnheiten_{TRACKER_SECRET}", timeout=15)
    resp.raise_for_status()
    felder = resp.json().get("fields", {})
    liste = felder.get("liste")
    return firestore_wert_lesen(liste) if liste else []


def gewohnheiten_gestern(liste):
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


def schreibe_morgenreport_firestore(daten, score, empfehlung, habit_quote):
    felder = {
        "datum":         daten["datum"],
        "score":         score,
        "empfehlung":    empfehlung,
        "body_battery":  daten["body_battery"],
        "hrv":           daten["hrv"] or 0,
        "ruhepuls":      daten["ruhepuls"],
        "schlafdauer_h": daten["schlafdauer_h"],
        "schlaf_score":  daten["schlaf_score"],
        "habit_quote":   habit_quote if habit_quote is not None else -1,
    }
    if not TRACKER_SECRET:
        raise RuntimeError("TRACKER_SECRET nicht gesetzt")
    body = {"fields": {k: firestore_wert_schreiben(v) for k, v in felder.items()}}
    resp = requests.patch(f"{FIRESTORE_BASIS}/tracker/morgenreport_{TRACKER_SECRET}", json=body, timeout=15)
    resp.raise_for_status()


def berechne_erholung(daten):
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
    if score >= 75:
        return "VOLLES TRAINING", "Alle geplanten Einheiten wie vorgesehen."
    elif score >= 55:
        return "NORMALES TRAINING", "Training wie geplant, auf Körpersignale achten."
    elif score >= 35:
        return "REDUZIERTE INTENSITÄT", "Volumen -20%, Intensität -1 Zone. Kein HIIT heute."
    else:
        return "REGENERATION", "Nur lockeres Gehen, Mobilität oder komplette Pause."


def na(val, einheit=""):
    return f"{val}{einheit}" if val is not None else "n/a"


def erstelle_text(daten, score, gruende, gewohnheiten=None):
    t = "─" * 40
    zeilen = [
        "═" * 40,
        f"  MORGENREPORT  {daten['datum']}",
        "═" * 40,
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
    ordner = os.path.join(BASE_DIR, "reports")
    os.makedirs(ordner, exist_ok=True)
    dateiname = os.path.join(ordner, f"report_{daten['datum']}.txt")
    with open(dateiname, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Report gespeichert: {dateiname}")


def sende_email(text, daten):
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
    parser = argparse.ArgumentParser(description="Garmin-Morgenreport erstellen und versenden")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report erstellen und lokal speichern, aber nichts versenden oder in Firestore schreiben",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    print("Verbinde mit Garmin Connect...")
    client = login()
    print("OK\n")
    print("Lade Garmin-Daten...")
    daten = hole_daten(client)
    score, gruende = berechne_erholung(daten)
    # Empfehlung wird nur noch für die Dashboard-Kachel in Firestore gebraucht
    empfehlung, _ = trainingsempfehlung(score)

    gewohnheiten = None
    habit_quote = None
    try:
        liste = hole_gewohnheiten()
        ergebnisse, habit_quote = gewohnheiten_gestern(liste)
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

    try:
        schreibe_morgenreport_firestore(daten, score, empfehlung, habit_quote)
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
