import unittest
from unittest.mock import ANY, Mock, patch

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
        self.assertEqual(
            telegram_inbox.parse_inbox_text("Gedanke: Weniger planen")["label"],
            "Gedanke",
        )

    def test_telegram_befehle_werden_ohne_praefix_gespeichert(self):
        self.assertEqual(
            telegram_inbox.parse_inbox_text("/aufgabe Firestore-Regeln pruefen"),
            {
                "type": "task",
                "label": "Aufgabe",
                "content": "Firestore-Regeln pruefen",
            },
        )
        self.assertEqual(
            telegram_inbox.parse_inbox_text("/notiz Spaziergang half")['type'],
            "note",
        )
        self.assertEqual(
            telegram_inbox.parse_inbox_text("/gedanke Mehr Pausen")['type'],
            "note",
        )
        self.assertEqual(
            telegram_inbox.parse_inbox_text("/kaufen Hafermilch")['type'],
            "buy",
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

    @patch("telegram_inbox.sende_projekt_auswahl")
    @patch("telegram_inbox.speichere_eintrag", return_value=True)
    def test_aufgabenbefehl_zeigt_projektwahl(self, speichern, projektwahl):
        updates = [
            {
                "update_id": 42,
                "message": {
                    "message_id": 10,
                    "date": 1,
                    "chat": {"id": 123},
                    "text": "/aufgabe Button testen",
                },
            }
        ]
        gespeichert, ignoriert = telegram_inbox.verarbeite_updates(
            updates,
            Mock(),
            "uid",
            "123",
            "https://telegram.invalid",
        )
        self.assertEqual((gespeichert, ignoriert), (1, 0))
        speichern.assert_called_once()
        projektwahl.assert_called_once_with(
            "https://telegram.invalid", 123, ANY, "uid", "telegram_42"
        )


class TelegramProjektwahlTests(unittest.TestCase):
    def test_projekt_tastatur_enthaelt_aktive_projekte_und_ohne_projekt(self):
        tastatur = telegram_inbox.projekt_tastatur(
            "telegram_42",
            [{"id": "projekt_1", "name": "Morgenreport"}],
        )
        self.assertEqual(
            tastatur["inline_keyboard"],
            [
                [{"text": "Morgenreport", "callback_data": "ip:telegram_42:projekt_1"}],
                [{"text": "Ohne Projekt", "callback_data": "ip:telegram_42:none"}],
            ],
        )


if __name__ == "__main__":
    unittest.main()
