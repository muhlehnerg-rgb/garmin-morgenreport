"""Separater Telegram-Chatbot auf Basis der Anthropic API.

Dieser Bot ist nicht die neue ChatGPT Action. Er verarbeitet Textnachrichten, die
an den Telegram-Bot gesendet wurden, und beantwortet sie mit dem Coach-Prompt.
Die Trennung ist wichtig: Ein Ausfall dieses Bots darf den Morgenreport-Versand
und den Firestore-Datensatz nicht beeinflussen.
"""

import os
import requests
import anthropic
from coach_prompt import COACH_SYSTEM_PROMPT

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def hole_neue_nachrichten():
    """Liest noch nicht bestätigte Telegram-Updates per Long-Polling-API."""
    response = requests.get(f"{API_URL}/getUpdates", timeout=30)
    return response.json().get("result", [])


def bestaetige_nachrichten(letzte_update_id):
    """Verschiebt Telegrams Offset hinter das zuletzt verarbeitete Update.

    Ohne diesen Schritt würden dieselben Nutzernachrichten beim nächsten
    Workflow-Lauf erneut beantwortet.
    """
    requests.get(f"{API_URL}/getUpdates", params={"offset": letzte_update_id + 1}, timeout=30)


def frage_coach(nutzer_text):
    """Sendet genau eine Nutzernachricht mit dem festen Systemprompt an Claude."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=700,
        system=COACH_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": nutzer_text}]
    )
    return message.content[0].text


def sende_antwort(chat_id, text):
    """Schickt die erzeugte Coach-Antwort zurück in den Ursprungs-Chat."""
    requests.post(f"{API_URL}/sendMessage", data={"chat_id": chat_id, "text": text})


def main():
    """Verarbeitet den aktuellen Telegram-Update-Stapel genau einmal."""
    updates = hole_neue_nachrichten()
    if not updates:
        print("Keine neuen Nachrichten.")
        return

    for update in updates:
        nachricht = update.get("message", {})
        text = nachricht.get("text")
        chat_id = nachricht.get("chat", {}).get("id")
        if text and chat_id:
            print(f"Neue Nachricht von {chat_id}: {text}")
            antwort = frage_coach(text)
            sende_antwort(chat_id, antwort)

    letzte_id = max(u["update_id"] for u in updates)
    bestaetige_nachrichten(letzte_id)
    print("Nachrichten verarbeitet und bestätigt.")


if __name__ == "__main__":
    main()
