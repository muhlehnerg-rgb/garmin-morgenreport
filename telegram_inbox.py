"""Deterministische Telegram-Inbox fuer das persoenliche Cockpit."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import requests


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FIRESTORE_PROJEKT = os.environ.get("FIRESTORE_PROJEKT", "gewohnheitstracker-3b30a")
FIREBASE_USER_UID = os.environ.get("FIREBASE_USER_UID", "")
letzter_schritt = "Start"

PREFIXE = {
    "aufgabe": ("task", "Aufgabe"),
    "idee": ("idea", "Idee"),
    "merke": ("note", "Notiz"),
}


def parse_inbox_text(text):
    """Liest ein bekanntes Praefix und gibt Typ, Bezeichnung und Inhalt zurueck."""
    if not isinstance(text, str) or ":" not in text:
        return None
    praefix, inhalt = text.split(":", 1)
    typ = PREFIXE.get(praefix.strip().casefold())
    inhalt = inhalt.strip()
    if not typ or not inhalt:
        return None
    return {"type": typ[0], "label": typ[1], "content": inhalt}


def hole_neue_nachrichten(api_url):
    response = requests.get(
        f"{api_url}/getUpdates",
        params={"timeout": 20, "allowed_updates": '["message"]'},
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


def sende_antwort(api_url, chat_id, text):
    response = requests.post(
        f"{api_url}/sendMessage",
        data={"chat_id": chat_id, "text": text},
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


def verarbeite_updates(updates, client, user_uid, erlaubte_chat_id, api_url):
    gespeichert = 0
    ignoriert = 0
    for update in updates:
        nachricht = update.get("message") or {}
        chat_id = nachricht.get("chat", {}).get("id")
        text = nachricht.get("text")
        if str(chat_id) != str(erlaubte_chat_id):
            ignoriert += 1
            continue
        eintrag = parse_inbox_text(text)
        if not eintrag:
            sende_antwort(
                api_url,
                chat_id,
                "Bitte beginne mit Aufgabe:, Idee: oder Merke:.",
            )
            continue
        wurde_erstellt = speichere_eintrag(client, user_uid, update, nachricht, eintrag)
        if wurde_erstellt:
            gespeichert += 1
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
    letzter_schritt = "Telegram-Abruf"
    updates = hole_neue_nachrichten(api_url)
    if not updates:
        print("Keine neuen Telegram-Updates.")
        return

    letzter_schritt = "Firestore-Verbindung"
    client = firestore.Client(project=FIRESTORE_PROJEKT)
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
