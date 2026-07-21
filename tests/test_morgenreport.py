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

    def test_heutige_aktivitaeten_argument(self):
        self.assertTrue(
            morgenreport.parse_args(["--heutige-aktivitaeten"]).heutige_aktivitaeten
        )
        self.assertFalse(morgenreport.parse_args([]).heutige_aktivitaeten)


class AktivitaetsTests(unittest.TestCase):
    def test_alle_aktivitaetstypen_werden_geladen_und_normalisiert(self):
        client = Mock()
        client.get_activities_by_date.return_value = [
            {
                "activityName": "Abendliches Krafttraining",
                "activityType": {"typeKey": "strength_training"},
                "startTimeLocal": "2026-07-18 18:30:00",
                "duration": 3600,
                "calories": 410,
                "averageHR": 118,
            },
            {
                "activityName": "Morgenwanderung",
                "activityType": {"typeKey": "hiking"},
                "startTimeLocal": "2026-07-18 07:15:00",
                "duration": 5400,
                "distance": 10250,
                "calories": 620,
                "averageHR": 126,
                "maxHR": 154,
                "elevationGain": 480.4,
                "aerobicTrainingEffect": 3.2,
            },
            {
                "activityName": "Unbekannte neue Garmin-Sportart",
                "activityType": {"typeKey": "future_activity_type"},
                "startTimeLocal": "2026-07-18 20:00:00",
            },
        ]

        aktivitaeten = morgenreport.hole_aktivitaeten(client, "2026-07-18")

        client.get_activities_by_date.assert_called_once_with("2026-07-18", "2026-07-18")
        self.assertEqual([a["typ"] for a in aktivitaeten], [
            "hiking", "strength_training", "future_activity_type"
        ])
        self.assertEqual(aktivitaeten[0]["distanz_km"], 10.25)
        self.assertEqual(aktivitaeten[0]["hoehenmeter"], 480)
        self.assertIsNone(aktivitaeten[1]["distanz_km"])
        self.assertEqual(aktivitaeten[2]["name"], "Unbekannte neue Garmin-Sportart")

    def test_report_listet_auch_aktivitaeten_ohne_distanz(self):
        daten = {
            "datum": "2026-07-19", "body_battery": 60, "ruhepuls": 52,
            "schlafdauer_h": 8.0, "schlaf_score": 80, "tief_min": 70,
            "leicht_min": 260, "rem_min": 100, "wach_min": 15, "hrv": 50,
            "stress_avg": 25, "schritte": 9000, "spo2": 97,
            "atemfrequenz": 14, "tr_score": 72, "tr_level": "HIGH",
            "int_min_woche": 120, "vo2max": 46,
            "aktivitaeten_gestern": [
                {
                    "name": "Krafttraining", "typ": "strength_training",
                    "startzeit": "2026-07-18 18:30:00", "dauer_min": 60,
                    "distanz_km": None, "kalorien": 410,
                    "durchschnittspuls": 118, "maximalpuls": None,
                    "hoehenmeter": None, "trainingseffekt_aerob": 2.1,
                    "trainingseffekt_anaerob": 1.4,
                }
            ],
        }

        text = morgenreport.erstelle_text(daten, 70, [])

        self.assertIn("AKTIVITÄTEN GESTERN", text)
        self.assertIn("Krafttraining [Strength Training] um 18:30", text)
        self.assertIn("Dauer: 60 min", text)
        self.assertIn("Kalorien: 410 kcal", text)
        self.assertNotIn("Distanz: 0", text)


class FirestoreTests(unittest.TestCase):
    @patch("morgenreport.requests.patch")
    def test_vollstaendiger_report_wird_gespeichert(self, patch_request):
        patch_request.return_value.raise_for_status.return_value = None
        daten = {
            "datum": "2026-07-13", "body_battery": 50, "ruhepuls": 55,
            "schlafdauer_h": 7.0, "schlaf_score": 75, "tief_min": 60,
            "leicht_min": 240, "rem_min": 90, "wach_min": 20, "hrv": 40,
            "stress_avg": 30, "schritte": 8000, "spo2": 97,
            "atemfrequenz": 14, "tr_score": 60, "tr_level": "MEDIUM",
            "int_min_woche": 80, "vo2max": 45,
            "aktivitaeten_gestern": [{
                "name": "Morgenlauf", "typ": "running", "dauer_min": 42,
                "distanz_km": 7.5, "kalorien": 500, "startzeit": None,
            }],
        }

        with patch.object(morgenreport, "TRACKER_SECRET", "secret"):
            morgenreport.schreibe_morgenreport_firestore(
                daten, 68, "NORMALES TRAINING", None, "Vollständiger Report"
            )

        fields = patch_request.call_args.kwargs["json"]["fields"]
        self.assertEqual(fields["report_text"], {"stringValue": "Vollständiger Report"})
        self.assertEqual(fields["stress_avg"], {"integerValue": "30"})
        self.assertEqual(fields["habit_quote"], {"nullValue": None})
        aktivitaet = fields["aktivitaeten_gestern"]["arrayValue"]["values"][0]
        self.assertEqual(
            aktivitaet["mapValue"]["fields"]["name"],
            {"stringValue": "Morgenlauf"},
        )
        self.assertEqual(
            aktivitaet["mapValue"]["fields"]["distanz_km"],
            {"doubleValue": 7.5},
        )
        self.assertEqual(fields["aktivitaeten_heute"], {"arrayValue": {"values": []}})
        self.assertEqual(
            fields["aktivitaeten_heute_datum"], {"stringValue": "2026-07-13"}
        )
        self.assertEqual(
            fields["aktivitaeten_heute_aktualisiert_am"], {"nullValue": None}
        )

    @patch("morgenreport.requests.patch")
    def test_abendaktualisierung_aendert_nur_heutige_aktivitaetsfelder(self, patch_request):
        patch_request.return_value.raise_for_status.return_value = None
        aktivitaeten = [{"name": "Abendlauf", "typ": "running", "distanz_km": 5.2}]

        with patch.object(morgenreport, "TRACKER_SECRET", "secret"), \
             patch("morgenreport.ZoneInfo", return_value=None), \
             patch("morgenreport.datetime") as datetime_mock:
            datetime_mock.now.return_value.isoformat.return_value = "2026-07-21T20:15:00+02:00"
            zeitpunkt = morgenreport.schreibe_heutige_aktivitaeten_firestore(
                "2026-07-21", aktivitaeten
            )

        self.assertEqual(zeitpunkt, "2026-07-21T20:15:00+02:00")
        self.assertEqual(
            patch_request.call_args.kwargs["params"],
            [
                ("updateMask.fieldPaths", "aktivitaeten_heute"),
                ("updateMask.fieldPaths", "aktivitaeten_heute_datum"),
                ("updateMask.fieldPaths", "aktivitaeten_heute_aktualisiert_am"),
            ],
        )
        fields = patch_request.call_args.kwargs["json"]["fields"]
        self.assertEqual(set(fields), {
            "aktivitaeten_heute",
            "aktivitaeten_heute_datum",
            "aktivitaeten_heute_aktualisiert_am",
        })
        self.assertEqual(
            fields["aktivitaeten_heute"]["arrayValue"]["values"][0]
            ["mapValue"]["fields"]["name"],
            {"stringValue": "Abendlauf"},
        )


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

    @patch("morgenreport.hole_daten")
    @patch("morgenreport.sende_telegram")
    @patch("morgenreport.sende_email")
    @patch("morgenreport.speichern")
    @patch("morgenreport.schreibe_heutige_aktivitaeten_firestore")
    @patch("morgenreport.hole_aktivitaeten")
    @patch("morgenreport.login")
    def test_abendmodus_laesst_report_und_versand_unberuehrt(
        self, login, hole_aktivitaeten, schreibe_heute, speichern,
        sende_email, sende_telegram, hole_daten
    ):
        hole_aktivitaeten.return_value = [{"name": "Radfahrt", "typ": "cycling"}]
        schreibe_heute.return_value = "2026-07-21T20:15:00+02:00"

        self.assertEqual(morgenreport.main(["--heutige-aktivitaeten"]), 0)

        hole_aktivitaeten.assert_called_once()
        schreibe_heute.assert_called_once()
        hole_daten.assert_not_called()
        speichern.assert_not_called()
        sende_email.assert_not_called()
        sende_telegram.assert_not_called()


if __name__ == "__main__":
    unittest.main()
