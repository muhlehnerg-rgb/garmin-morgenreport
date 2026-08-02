"""Deterministische Telegram-Inbox fuer das persoenliche Cockpit."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import requests


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FIRESTORE_PROJEKT = os.environ.get("FIRESTORE_PROJEKT", "gewohnheitstracker-3b30a")
FIRESTORE_DATENBANK = os.environ.get("FIRESTORE_DATENBANK", "default")
FIREBASE_USER_UID = os.environ.get("FIREBASE_USER_UID", "")
letzter_schritt = "Start"

PREFIXE = {
    "aufgabe": ("task", "Aufgabe"),
    "kaufen": ("buy", "Einkauf"),
    "idee": ("idea", "Idee"),
    "gedanke": ("note", "Gedanke"),
    "merke": ("note", "Gedanke"),
    "notiz": ("note", "Gedanke"),
}

TELEGRAM_BEFEHLE = [
    {"command": "aufgabe", "description": "Aufgabe erfassen"},
    {"command": "kaufen", "description": "Einkauf erfassen"},
    {"command": "idee", "description": "Idee erfassen"},
    {"command": "gedanke", "description": "Gedanke erfassen"},
    {"command": "notiz", "description": "Gedanke speichern"},
    {"command": "abend", "description": "Abendabschluss oeffnen"},
    {"command": "hilfe", "description": "Kurze Hilfe"},
]


def parse_inbox_text(text):
    """Liest ein bekanntes Praefix und gibt Typ, Bezeichnung und Inhalt zurueck."""
    if not isinstance(text, str):
        return None

    bereinigt = text.strip()
    if bereinigt.startswith("/"):
        befehl, _, inhalt = bereinigt[1:].partition(" ")
        befehl = befehl.split("@", 1)[0].casefold()
        typ = PREFIXE.get(befehl)
        inhalt = inhalt.strip()
        if typ and inhalt:
            return {"type": typ[0], "label": typ[1], "content": inhalt}
        return None

    if ":" not in bereinigt:
        return None
    praefix, inhalt = bereinigt.split(":", 1)
    typ = PREFIXE.get(praefix.strip().casefold())
    inhalt = inhalt.strip()
    if not typ or not inhalt:
        return None
    return {"type": typ[0], "label": typ[1], "content": inhalt}


def hole_neue_nachrichten(api_url):
    response = requests.get(
        f"{api_url}/getUpdates",
        params={"timeout": 20, "allowed_updates": '["message", "callback_query"]'},
        timeout=30,
    )
    response.raise_for_status()
    antwort = response.json()
    if not antwort.get("ok"):
        raise RuntimeError(f"Telegram API: {antwort.get('description', 'unbekannter Fehler')}")
    return antwort.get("result", [])


def bestaetige_nachrichten(api_url, letzte_update_id):
    response = requests.get(
        f"{api_url}/getUpdates",
        params={"offset": letzte_update_id + 1, "timeout": 0},
        timeout=15,
    )
    response.raise_for_status()


def sende_antwort(api_url, chat_id, text, reply_markup=None):
    daten = {"chat_id": chat_id, "text": text}
    if reply_markup:
        daten["reply_markup"] = reply_markup
    response = requests.post(
        f"{api_url}/sendMessage",
        json=daten,
        timeout=20,
    )
    response.raise_for_status()
    antwort = response.json()
    if not antwort.get("ok"):
        raise RuntimeError(f"Telegram API: {antwort.get('description', 'unbekannter Fehler')}")


def speichere_eintrag(client, user_uid, update, nachricht, eintrag):
    """Speichert einen Telegram-Update idempotent unter der Benutzer-UID."""
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import firestore

    update_id = update["update_id"]
    dokument = (
        client.collection("users")
        .document(user_uid)
        .collection("inbox")
        .document(f"telegram_{update_id}")
    )
    erstellt_am = datetime.fromtimestamp(nachricht.get("date", 0), tz=timezone.utc)
    daten = {
        "schemaVersion": 1,
        "source": "telegram",
        "type": eintrag["type"],
        "content": eintrag["content"],
        "originalText": nachricht.get("text", ""),
        "status": "inbox",
        "projectId": None,
        "createdAt": erstellt_am,
        "receivedAt": firestore.SERVER_TIMESTAMP,
        "telegram": {
            "updateId": update_id,
            "messageId": nachricht.get("message_id"),
        },
    }
    try:
        dokument.create(daten)
        return True
    except AlreadyExists:
        return False


def richte_befehlsmenue_ein(api_url):
    """Hinterlegt die persoenlichen Kurzbefehle im Telegram-Eingabemenue."""
    response = requests.post(
        f"{api_url}/setMyCommands",
        json={"commands": TELEGRAM_BEFEHLE},
        timeout=20,
    )
    response.raise_for_status()
    antwort = response.json()
    if not antwort.get("ok"):
        raise RuntimeError(f"Telegram API: {antwort.get('description', 'unbekannter Fehler')}")


def ist_telegram_befehl(text, befehl):
    if not isinstance(text, str):
        return False
    erstes_wort = text.strip().split(maxsplit=1)[0].casefold() if text.strip() else ""
    return erstes_wort.split("@", 1)[0] == f"/{befehl}"


def lade_aktive_projekte(client, user_uid):
    return sorted(
        (
            {"id": dokument.id, **dokument.to_dict()}
            for dokument in client.collection("users").document(user_uid).collection("projects").stream()
            if dokument.to_dict().get("status") == "active"
        ),
        key=lambda projekt: projekt.get("name", "").casefold(),
    )


def projekt_tastatur(inbox_id, projekte):
    """Baut eine kompakte Inline-Auswahl fuer aktive Projekte."""
    zeilen = []
    aktuelle_zeile = []
    for projekt in projekte:
        callback_data = f"ip:{inbox_id}:{projekt['id']}"
        if len(callback_data.encode("utf-8")) > 64:
            continue
        aktuelle_zeile.append({"text": projekt.get("name", "Projekt")[:40], "callback_data": callback_data})
        if len(aktuelle_zeile) == 2:
            zeilen.append(aktuelle_zeile)
            aktuelle_zeile = []
    if aktuelle_zeile:
        zeilen.append(aktuelle_zeile)
    zeilen.append([{"text": "Ohne Projekt", "callback_data": f"ip:{inbox_id}:none"}])
    return {"inline_keyboard": zeilen}


def sende_projekt_auswahl(api_url, chat_id, client, user_uid, inbox_id):
    projekte = lade_aktive_projekte(client, user_uid)
    if not projekte:
        sende_antwort(api_url, chat_id, "Als Aufgabe gespeichert. Es gibt aktuell keine aktiven Projekte.")
        return
    sende_antwort(
        api_url,
        chat_id,
        "Welchem Projekt soll die Aufgabe zugeordnet werden?",
        projekt_tastatur(inbox_id, projekte),
    )


def beantworte_callback(api_url, callback_id, text=None):
    daten = {"callback_query_id": callback_id}
    if text:
        daten["text"] = text
    response = requests.post(f"{api_url}/answerCallbackQuery", json=daten, timeout=20)
    response.raise_for_status()


def bearbeite_nachricht(api_url, chat_id, message_id, text):
    response = requests.post(
        f"{api_url}/editMessageText",
        json={"chat_id": chat_id, "message_id": message_id, "text": text},
        timeout=20,
    )
    response.raise_for_status()


def verarbeite_projekt_callback(update, client, user_uid, erlaubte_chat_id, api_url):
    callback = update.get("callback_query") or {}
    nachricht = callback.get("message") or {}
    chat_id = nachricht.get("chat", {}).get("id")
    daten = callback.get("data", "")
    if str(chat_id) != str(erlaubte_chat_id) or not daten.startswith("ip:"):
        return False

    teile = daten.split(":", 2)
    if len(teile) != 3:
        beantworte_callback(api_url, callback.get("id"), "Ungueltige Auswahl.")
        return True

    _, inbox_id, projekt_id = teile
    inbox_ref = client.collection("users").document(user_uid).collection("inbox").document(inbox_id)
    inbox = inbox_ref.get()
    if not inbox.exists:
        beantworte_callback(api_url, callback.get("id"), "Die Aufgabe wurde nicht gefunden.")
        return True

    projekt_name = "Ohne Projekt"
    if projekt_id != "none":
        projekt = client.collection("users").document(user_uid).collection("projects").document(projekt_id).get()
        if not projekt.exists or projekt.to_dict().get("status") != "active":
            beantworte_callback(api_url, callback.get("id"), "Projekt ist nicht mehr aktiv.")
            return True
        projekt_name = projekt.to_dict().get("name", "Projekt")

    from google.cloud import firestore

    inbox_ref.update({"projectId": None if projekt_id == "none" else projekt_id, "updatedAt": firestore.SERVER_TIMESTAMP})
    beantworte_callback(api_url, callback.get("id"), "Projekt gespeichert.")
    bearbeite_nachricht(api_url, chat_id, nachricht.get("message_id"), f"Projekt: {projekt_name}. Die Aufgabe wartet in deiner Inbox.")
    return True


def verarbeite_updates(updates, client, user_uid, erlaubte_chat_id, api_url):
    gespeichert = 0
    ignoriert = 0
    for update in updates:
        if update.get("callback_query"):
            if not verarbeite_projekt_callback(update, client, user_uid, erlaubte_chat_id, api_url):
                ignoriert += 1
            continue
        nachricht = update.get("message") or {}
        chat_id = nachricht.get("chat", {}).get("id")
        text = nachricht.get("text")
        if str(chat_id) != str(erlaubte_chat_id):
            ignoriert += 1
            continue
        if ist_telegram_befehl(text, "abend"):
            sende_antwort(api_url, chat_id, "Abendabschluss: https://gewohnheitstracker-3b30a.web.app/?abend=1")
            continue
        if ist_telegram_befehl(text, "hilfe") or (ist_telegram_befehl(text, "aufgabe") and not parse_inbox_text(text)):
            sende_antwort(api_url, chat_id, "Nutze /aufgabe Titel, /idee Text oder /gedanke Text.")
            continue
        eintrag = parse_inbox_text(text)
        if not eintrag:
            sende_antwort(
                api_url,
                chat_id,
                "Bitte beginne mit Aufgabe:, Idee: oder Gedanke:.",
            )
            continue
        wurde_erstellt = speichere_eintrag(client, user_uid, update, nachricht, eintrag)
        if wurde_erstellt:
            gespeichert += 1
            if eintrag["type"] == "task" and ist_telegram_befehl(text, "aufgabe"):
                sende_projekt_auswahl(api_url, chat_id, client, user_uid, f"telegram_{update['update_id']}")
            else:
                sende_antwort(api_url, chat_id, f"Als {eintrag['label']} gespeichert.")
    return gespeichert, ignoriert


def main():
    global letzter_schritt
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not FIREBASE_USER_UID:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID und FIREBASE_USER_UID muessen gesetzt sein"
        )
    from google.cloud import firestore

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    letzter_schritt = "Telegram-Befehlsmenue"
    richte_befehlsmenue_ein(api_url)
    letzter_schritt = "Telegram-Abruf"
    updates = hole_neue_nachrichten(api_url)
    if not updates:
        print("Keine neuen Telegram-Updates.")
        return

    letzter_schritt = "Firestore-Verbindung"
    client = firestore.Client(
        project=FIRESTORE_PROJEKT,
        database=FIRESTORE_DATENBANK,
    )
    letzter_schritt = "Inbox-Verarbeitung"
    gespeichert, ignoriert = verarbeite_updates(
        updates,
        client,
        FIREBASE_USER_UID,
        TELEGRAM_CHAT_ID,
        api_url,
    )
    letzte_id = max(update["update_id"] for update in updates)
    letzter_schritt = "Telegram-Abschluss"
    bestaetige_nachrichten(api_url, letzte_id)
    print(f"Telegram-Inbox: {gespeichert} gespeichert, {ignoriert} fremde Updates ignoriert.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(f"Telegram-Inbox fehlgeschlagen bei: {letzter_schritt}")
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            try:
                sende_antwort(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}",
                    TELEGRAM_CHAT_ID,
                    f"Inbox-Fehler bei {letzter_schritt}. Ich pruefe die Verbindung.",
                )
            except requests.RequestException:
                pass
        raise
