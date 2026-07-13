import os
import unittest
from unittest.mock import Mock, patch

import morgenreport


class LoginTests(unittest.TestCase):
    @patch.dict(os.environ, {"GITHUB_ACTIONS": "true"})
    @patch("morgenreport.Garmin")
    def test_ci_fordert_keinen_mfa_code_an(self, garmin_cls):
        garmin_cls.return_value.login.side_effect = RuntimeError("token expired")

        with self.assertRaisesRegex(morgenreport.GarminLoginError, "GARMIN_TOKENS_B64"):
            morgenreport.login()

        self.assertEqual(garmin_cls.call_count, 1)


class TelegramTests(unittest.TestCase):
    @patch("morgenreport.requests.post")
    def test_telegram_prueft_http_status(self, post):
        antwort = Mock()
        antwort.raise_for_status.side_effect = RuntimeError("HTTP 401")
        post.return_value = antwort

        with patch.object(morgenreport, "TELEGRAM_BOT_TOKEN", "token"), \
             patch.object(morgenreport, "TELEGRAM_CHAT_ID", "chat"):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                morgenreport.sende_telegram("Test")

        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["timeout"], 20)


class ArgumentTests(unittest.TestCase):
    def test_dry_run_argument(self):
        self.assertTrue(morgenreport.parse_args(["--dry-run"]).dry_run)
        self.assertFalse(morgenreport.parse_args([]).dry_run)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.daten = {
            "datum": "2026-07-13",
            "body_battery": 50,
            "ruhepuls": 55,
            "schlafdauer_h": 7.0,
            "schlaf_score": 75,
            "tief_min": 60,
            "leicht_min": 240,
            "rem_min": 90,
            "wach_min": 20,
            "hrv": 40,
            "stress_avg": 30,
            "schritte": 8000,
            "spo2": 97,
            "atemfrequenz": 14,
            "tr_score": 60,
            "tr_level": "MEDIUM",
            "int_min_woche": 80,
            "vo2max": 45,
        }

    @patch("morgenreport.schreibe_morgenreport_firestore")
    @patch("morgenreport.sende_telegram")
    @patch("morgenreport.sende_email")
    @patch("morgenreport.speichern")
    @patch("morgenreport.hole_gewohnheiten", side_effect=RuntimeError("offline"))
    @patch("morgenreport.hole_daten")
    @patch("morgenreport.login")
    def test_dry_run_versendet_nichts(
        self, login, hole_daten, _gewohnheiten, _speichern,
        sende_email, sende_telegram, firestore
    ):
        hole_daten.return_value = self.daten

        self.assertEqual(morgenreport.main(["--dry-run"]), 0)

        sende_email.assert_not_called()
        sende_telegram.assert_not_called()
        firestore.assert_not_called()

    @patch("morgenreport.schreibe_morgenreport_firestore")
    @patch("morgenreport.sende_telegram", side_effect=RuntimeError("Telegram aus"))
    @patch("morgenreport.sende_email", side_effect=RuntimeError("E-Mail aus"))
    @patch("morgenreport.speichern")
    @patch("morgenreport.hole_gewohnheiten", side_effect=RuntimeError("offline"))
    @patch("morgenreport.hole_daten")
    @patch("morgenreport.login")
    def test_kein_erfolgreicher_versand_ist_fehler(
        self, login, hole_daten, _gewohnheiten, _speichern,
        _email, _telegram, firestore
    ):
        hole_daten.return_value = self.daten

        with self.assertRaisesRegex(RuntimeError, "kein Versandkanal"):
            morgenreport.main([])

        firestore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
