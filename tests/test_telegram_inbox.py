import unittest
from unittest.mock import Mock, patch

import telegram_inbox


class TelegramInboxParserTests(unittest.TestCase):
    def test_firestore_standarddatenbank_ist_konfiguriert(self):
        self.assertEqual(telegram_inbox.FIRESTORE_DATENBANK, "default")

    def test_bekannte_praefixe_werden_erkannt(self):
        self.assertEqual(
            telegram_inbox.parse_inbox_text("Aufgabe: Firestore-Regeln pruefen"),
            {
                "type": "task",
                "label": "Aufgabe",
                "content": "Firestore-Regeln pruefen",
            },
        )
        self.assertEqual(
            telegram_inbox.parse_inbox_text("  IDEE: Wochenplanung  ")["type"],
            "idea",
        )
        self.assertEqual(
            telegram_inbox.parse_inbox_text("Merke: Spaziergang half")["type"],
            "note",
        )

    def test_unbekannte_oder_leere_eingabe_wird_abgelehnt(self):
        self.assertIsNone(telegram_inbox.parse_inbox_text("Hallo"))
        self.assertIsNone(telegram_inbox.parse_inbox_text("Aufgabe:"))
        self.assertIsNone(telegram_inbox.parse_inbox_text("Termin: morgen"))


class TelegramInboxProcessingTests(unittest.TestCase):
    @patch("telegram_inbox.sende_antwort")
    @patch("telegram_inbox.speichere_eintrag", return_value=True)
    def test_nur_erlaubter_chat_wird_gespeichert(self, speichern, antworten):
        updates = [
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "date": 1,
                    "chat": {"id": 123},
                    "text": "Aufgabe: Testen",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 11,
                    "date": 1,
                    "chat": {"id": 999},
                    "text": "Idee: Fremd",
                },
            },
        ]
        gespeichert, ignoriert = telegram_inbox.verarbeite_updates(
            updates,
            Mock(),
            "uid",
            "123",
            "https://telegram.invalid",
        )
        self.assertEqual((gespeichert, ignoriert), (1, 1))
        speichern.assert_called_once()
        antworten.assert_called_once_with(
            "https://telegram.invalid", 123, "Als Aufgabe gespeichert."
        )

    @patch("telegram_inbox.sende_antwort")
    def test_unbekannter_text_bekommt_nur_hinweis(self, antworten):
        updates = [
            {
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "Hallo"},
            }
        ]
        gespeichert, ignoriert = telegram_inbox.verarbeite_updates(
            updates,
            Mock(),
            "uid",
            "123",
            "https://telegram.invalid",
        )
        self.assertEqual((gespeichert, ignoriert), (0, 0))
        antworten.assert_called_once()


if __name__ == "__main__":
    unittest.main()
