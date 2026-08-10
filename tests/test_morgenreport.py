import os
import unittest
from datetime import date
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

    def test_schlaf_nachsynchronisieren_argument(self):
        self.assertTrue(
            morgenreport.parse_args(["--schlaf-nachsynchronisieren"]).schlaf_nachsynchronisieren
        )
        self.assertFalse(morgenreport.parse_args([]).schlaf_nachsynchronisieren)

    def test_wochenreview_argument(self):
        self.assertTrue(morgenreport.parse_args(["--wochenreview"]).wochenreview)
        self.assertFalse(morgenreport.parse_args([]).wochenreview)


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

    def test_report_benennt_heutiges_datum_fehlende_werte_und_keine_aktivitaet(self):
        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 7, 24)

        daten = {
            "datum": "2026-07-24", "body_battery": 60, "ruhepuls": 52,
            "schlafdauer_h": 8.0, "schlaf_score": 80, "tief_min": 70,
            "leicht_min": 260, "rem_min": 100, "wach_min": 15, "hrv": 50,
            "stress_avg": 25, "schritte": 9000, "spo2": 97,
            "atemfrequenz": 14, "tr_score": None, "tr_level": None,
            "int_min_woche": None, "vo2max": None,
            "aktivitaeten_gestern": [],
        }

        with patch.object(morgenreport, "date", FixedDate):
            text = morgenreport.erstelle_text(daten, 70, [])

        self.assertIn("Reportdatum: heute, 24. Juli 2026", text)
        self.assertIn(
            "Nicht verfügbar: Trainingsbereitschaft, VO₂max, "
            "wöchentliche Intensitätsminuten.",
            text,
        )
        self.assertIn(
            "Gestern wurde keine separate Garmin-Aktivität aufgezeichnet.",
            text,
        )


class FirestoreTests(unittest.TestCase):
    def setUp(self):
        self.uid_patcher = patch.object(morgenreport, "FIRESTORE_USER_UID", "test-user")
        self.secret_patcher = patch.object(morgenreport, "TRACKER_SECRET", "legacy-test-key")
        self.auth_patcher = patch(
            "morgenreport.firestore_auth_headers",
            return_value={"Authorization": "Bearer test-token"},
        )
        self.uid_patcher.start()
        self.secret_patcher.start()
        self.auth_patcher.start()

    def tearDown(self):
        self.auth_patcher.stop()
        self.secret_patcher.stop()
        self.uid_patcher.stop()

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

        morgenreport.schreibe_morgenreport_firestore(
            daten, 68, "NORMALES TRAINING", None, "Vollständiger Report",
            [("Abendroutine", True)],
        )

        self.assertEqual(patch_request.call_count, 3)
        aktueller_report = patch_request.call_args_list[0]
        legacy_report = patch_request.call_args_list[1]
        historie = patch_request.call_args_list[2]
        fields = aktueller_report.kwargs["json"]["fields"]
        self.assertEqual(fields["report_text"], {"stringValue": "Vollständiger Report"})
        self.assertEqual(fields["stress_avg"], {"integerValue": "30"})
        self.assertEqual(fields["habit_quote"], {"nullValue": None})
        self.assertEqual(
            fields["gewohnheiten"]["arrayValue"]["values"][0]["mapValue"]["fields"]["name"],
            {"stringValue": "Abendroutine"},
        )
        self.assertEqual(fields["sleep_data_incomplete"], {"booleanValue": False})
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
        self.assertIn("/users/test-user/health/morning_report", aktueller_report.args[0])
        self.assertIn("/tracker/morgenreport_legacy-test-key", legacy_report.args[0])
        self.assertIn("/users/test-user/health/morning_report/history/2026-07-13", historie.args[0])
        historie_fields = historie.kwargs["json"]["fields"]
        self.assertNotIn("report_text", historie_fields)
        self.assertEqual(historie_fields["notizen"], {"nullValue": None})
        self.assertEqual(historie_fields["subjektive_energie"], {"nullValue": None})
        self.assertIn("gewohnheiten", historie_fields)

    @patch("morgenreport.requests.patch")
    def test_abendaktualisierung_aendert_nur_heutige_aktivitaetsfelder(self, patch_request):
        patch_request.return_value.raise_for_status.return_value = None
        aktivitaeten = [{"name": "Abendlauf", "typ": "running", "distanz_km": 5.2}]

        with patch("morgenreport.ZoneInfo", return_value=None), \
             patch("morgenreport.datetime") as datetime_mock:
            datetime_mock.now.return_value.isoformat.return_value = "2026-07-21T20:15:00+02:00"
            zeitpunkt = morgenreport.schreibe_heutige_aktivitaeten_firestore(
                "2026-07-21", aktivitaeten
            )

        self.assertEqual(zeitpunkt, "2026-07-21T20:15:00+02:00")
        self.assertEqual(patch_request.call_count, 2)
        self.assertIn("/users/test-user/health/morning_report", patch_request.call_args_list[0].args[0])
        self.assertIn("/tracker/morgenreport_legacy-test-key", patch_request.call_args_list[1].args[0])
        self.assertEqual(
            patch_request.call_args_list[0].kwargs["params"],
            [
                ("updateMask.fieldPaths", "aktivitaeten_heute"),
                ("updateMask.fieldPaths", "aktivitaeten_heute_datum"),
                ("updateMask.fieldPaths", "aktivitaeten_heute_aktualisiert_am"),
            ],
        )
        fields = patch_request.call_args_list[0].kwargs["json"]["fields"]
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

    @patch("morgenreport.requests.patch")
    def test_schlaf_nachsynchronisierung_aendert_nur_schlaf_und_recovery(self, patch_request):
        patch_request.return_value.raise_for_status.return_value = None
        daten = {
            "datum": "2026-07-24", "body_battery": 72, "ruhepuls": 51,
            "schlafdauer_h": 7.4, "schlaf_score": 82, "tief_min": 65,
            "leicht_min": 250, "rem_min": 95, "wach_min": 18, "hrv": 48,
        }

        with patch("morgenreport.ZoneInfo", return_value=None), \
             patch("morgenreport.datetime") as datetime_mock:
            datetime_mock.now.return_value.isoformat.return_value = "2026-07-24T20:15:00+02:00"
            zeitpunkt = morgenreport.schreibe_schlaf_nachsynchronisierung_firestore(daten)

        self.assertEqual(zeitpunkt, "2026-07-24T20:15:00+02:00")
        self.assertEqual(patch_request.call_count, 3)
        for call in patch_request.call_args_list:
            self.assertEqual(
                call.kwargs["params"],
                [
                    ("updateMask.fieldPaths", "body_battery"),
                    ("updateMask.fieldPaths", "hrv"),
                    ("updateMask.fieldPaths", "ruhepuls"),
                    ("updateMask.fieldPaths", "schlafdauer_h"),
                    ("updateMask.fieldPaths", "schlaf_score"),
                    ("updateMask.fieldPaths", "tief_min"),
                    ("updateMask.fieldPaths", "rem_min"),
                    ("updateMask.fieldPaths", "leicht_min"),
                    ("updateMask.fieldPaths", "wach_min"),
                    ("updateMask.fieldPaths", "sleep_data_incomplete"),
                    ("updateMask.fieldPaths", "sleep_nachsynchronisiert_am"),
                ],
            )
        fields = patch_request.call_args_list[0].kwargs["json"]["fields"]
        self.assertEqual(fields["schlafdauer_h"], {"doubleValue": 7.4})
        self.assertEqual(fields["schlaf_score"], {"integerValue": "82"})
        self.assertEqual(fields["sleep_data_incomplete"], {"booleanValue": False})
        self.assertIn("/tracker/morgenreport_legacy-test-key", patch_request.call_args_list[2].args[0])

    def test_wochenreview_berechnet_trends_und_gewohnheiten(self):
        tage = []
        for i in range(7):
            tage.append({
                "datum": f"2026-07-{20 + i:02d}",
                "schlafdauer_h": 6.5 + i * 0.1,
                "schlaf_score": 65 + i * 3,
                "score": 55 + i * 2,
                "body_battery": 50 + i,
                "hrv": 40 + i,
                "schritte": 6000 + i * 500,
                "habit_quote": 60 + i * 5,
                "aktivitaeten_gestern": [{"dauer_min": 30}] if i % 2 == 0 else [],
                "gewohnheiten": [
                    {"name": "Abendroutine", "ok": i >= 4},
                    {"name": "Morgenspaziergang", "ok": True},
                ],
            })

        review = morgenreport.erstelle_wochenreview(tage, date(2026, 7, 26))

        self.assertEqual(review["woche_start"], "2026-07-20")
        self.assertEqual(review["woche_ende"], "2026-07-26")
        self.assertEqual(review["tage_gefunden"], 7)
        self.assertEqual(review["aktivitaeten_anzahl"], 4)
        self.assertEqual(review["trainingsminuten_summe"], 120)
        self.assertEqual(review["schlaf_trend"], "steigend")
        self.assertEqual(review["recovery_trend"], "steigend")
        self.assertEqual(review["staerkste_gewohnheit"]["name"], "Morgenspaziergang")
        self.assertEqual(review["schwaechste_gewohnheit"]["name"], "Abendroutine")
        self.assertIn("Fokus nächste Woche", review["review_text"])

    @patch("morgenreport.requests.patch")
    def test_wochenreview_wird_aktuell_und_archiviert_gespeichert(self, patch_request):
        patch_request.return_value.raise_for_status.return_value = None
        review = {
            "woche_start": "2026-07-20",
            "woche_ende": "2026-07-26",
            "review_text": "WOCHENREVIEW",
        }

        morgenreport.schreibe_wochenreview_firestore(review)

        self.assertEqual(patch_request.call_count, 2)
        self.assertIn("/reviews/aktuell", patch_request.call_args_list[0].args[0])
        self.assertIn(
            "/reviews/2026-07-20_2026-07-26",
            patch_request.call_args_list[1].args[0],
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
