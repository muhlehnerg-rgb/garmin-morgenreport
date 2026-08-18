import os
import unittest
from datetime import date
from unittest.mock import ANY, Mock, patch

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

    def test_garmin_rueckimport_argument(self):
        args = morgenreport.parse_args(["--garmin-rueckimport", "--tage", "21"])
        self.assertTrue(args.garmin_rueckimport)
        self.assertEqual(args.tage, 21)


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


class GarminHistorienDatenTests(unittest.TestCase):
    def test_historischer_tag_nutzt_korrekte_zeitraeume_und_neue_formate(self):
        client = Mock(spec=[
            "get_stats", "get_sleep_data", "get_hrv_data", "get_stress_data",
            "get_steps_data", "get_spo2_data", "get_respiration_data",
            "get_training_readiness", "get_weekly_intensity_minutes",
            "get_max_metrics", "get_activities_by_date", "get_hydration_data",
            "get_body_composition", "get_fitnessage_data", "get_endurance_score",
            "get_training_status",
        ])
        client.get_stats.return_value = {
            "bodyBatteryMostRecentValue": 72,
            "restingHeartRate": 51,
            "totalKilocalories": 2200,
            "activeKilocalories": 550,
            "totalDistanceMeters": 8400,
            "moderateIntensityMinutes": 20,
            "vigorousIntensityMinutes": 10,
        }
        client.get_sleep_data.return_value = {"dailySleepDTO": {
            "sleepTimeSeconds": 28800,
            "deepSleepSeconds": 4200,
            "lightSleepSeconds": 16200,
            "remSleepSeconds": 7200,
            "awakeSleepSeconds": 1200,
            "sleepScores": {"overall": {"value": 86}},
        }}
        client.get_hrv_data.return_value = {"hrvSummary": {"lastNightAvg": 52}}
        client.get_stress_data.return_value = {"avgStressLevel": 24}
        client.get_steps_data.return_value = [{"steps": 4000}, {"steps": 3500}]
        client.get_spo2_data.return_value = {"averageSpO2": 97.2}
        client.get_respiration_data.return_value = {"avgWakingRespirationValue": 14.3}
        client.get_training_readiness.return_value = [{"score": 78, "level": "HIGH"}]
        client.get_weekly_intensity_minutes.return_value = [
            {"moderateValue": 30, "vigorousValue": 20}
        ]
        client.get_max_metrics.return_value = {
            "generic": {"vo2MaxPreciseValue": 47.34}
        }
        client.get_activities_by_date.return_value = []
        client.get_hydration_data.return_value = {"valueInML": 2100}
        client.get_body_composition.return_value = {
            "totalAverage": {"weight": 81200, "bmi": 24.1, "bodyFat": 18.2}
        }
        client.get_fitnessage_data.return_value = {"fitnessAge": 37}
        client.get_endurance_score.return_value = {"overallScore": 5100}
        client.get_training_status.return_value = {"trainingStatus": "PRODUCTIVE"}

        daten = morgenreport.hole_tagesdaten(client, "2026-08-18")

        client.get_stress_data.assert_called_once_with("2026-08-17")
        client.get_steps_data.assert_called_once_with("2026-08-17")
        client.get_activities_by_date.assert_called_once_with("2026-08-17", "2026-08-17")
        client.get_weekly_intensity_minutes.assert_called_once_with(
            "2026-08-17", "2026-08-18"
        )
        self.assertEqual(daten["int_min_woche"], 70)
        self.assertEqual(daten["vo2max"], 47.3)
        self.assertEqual(daten["gewicht_kg"], 81.2)
        self.assertEqual(daten["fluessigkeit_ml"], 2100)
        self.assertEqual(daten["fitnessalter"], 37)
        self.assertIn("heart_rates_vortag", daten["_garmin_quellen_fehler"])

    def test_fehlende_schlafwerte_bleiben_none(self):
        daten = morgenreport.extrahiere_schlaf_recovery_daten(
            "2026-08-18",
            {},
            {"dailySleepDTO": {
                "sleepTimeSeconds": 0,
                "deepSleepSeconds": 0,
                "lightSleepSeconds": 0,
                "remSleepSeconds": 0,
                "awakeSleepSeconds": 0,
                "sleepScores": {"overall": {"value": 0}},
            }},
            {},
        )

        for feld in (
            "body_battery", "ruhepuls", "schlafdauer_h", "schlaf_score",
            "tief_min", "leicht_min", "rem_min", "wach_min", "hrv",
        ):
            self.assertIsNone(daten[feld])
        self.assertTrue(daten["sleep_data_incomplete"])

    def test_grosse_rohquelle_wird_verlustfrei_komprimiert(self):
        original = {"werte": [{"zeit": i, "puls": 55 + i % 20} for i in range(30000)]}

        encoding, teile = morgenreport._rohquelle_speicherformat(original)

        self.assertEqual(encoding, "gzip+base64")
        kodiert = "".join(teile)
        text = morgenreport.gzip.decompress(
            morgenreport.base64.b64decode(kodiert)
        ).decode("utf-8")
        self.assertEqual(morgenreport.json.loads(text), original)


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

    @patch("morgenreport.aktualisiere_schlafhistorie_spiegel")
    @patch("morgenreport.requests.patch")
    def test_vollstaendiger_report_wird_gespeichert(self, patch_request, historie_spiegel):
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
        self.assertEqual(fields["fitnessalter"], {"nullValue": None})
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
        historie_spiegel.assert_called_once_with(date(2026, 7, 13))

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
    def test_rueckimport_aktualisiert_nur_garmin_felder(self, patch_request):
        patch_request.return_value.raise_for_status.return_value = None
        daten = {
            "datum": "2026-08-18", "body_battery": 70, "ruhepuls": 52,
            "schlafdauer_h": 7.5, "schlaf_score": 82, "tief_min": 70,
            "leicht_min": 250, "rem_min": 100, "wach_min": 15, "hrv": 48,
            "stress_avg": 27, "schritte": 9000, "spo2": 97,
            "atemfrequenz": 14, "tr_score": 72, "tr_level": "HIGH",
            "int_min_woche": 100, "vo2max": 46,
            "aktivitaeten_gestern": [], "sleep_data_incomplete": False,
        }

        morgenreport.aktualisiere_historientag_garmin_firestore(
            daten, 75, "VOLLES TRAINING"
        )

        params = patch_request.call_args.kwargs["params"]
        masken = {wert for name, wert in params if name == "updateMask.fieldPaths"}
        self.assertIn("schlaf_score", masken)
        self.assertIn("fitnessalter", masken)
        self.assertNotIn("habit_quote", masken)
        self.assertNotIn("gewohnheiten", masken)
        self.assertNotIn("notizen", masken)
        self.assertNotIn("subjektive_energie", masken)

    @patch("morgenreport.aktualisiere_schlafhistorie_spiegel")
    @patch("morgenreport.requests.patch")
    def test_schlaf_nachsynchronisierung_aendert_nur_schlaf_und_recovery(
        self, patch_request, historie_spiegel
    ):
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
        historie_spiegel.assert_called_once_with(date(2026, 7, 24))

    @patch("morgenreport.requests.patch")
    @patch("morgenreport.hole_historie_zeitraum")
    def test_schlafhistorie_wird_kompakt_begrenzt_und_gespiegelt(
        self, hole_historie, patch_request
    ):
        hole_historie.return_value = [
            {
                "datum": f"2026-07-{tag:02d}",
                "schlafdauer_h": 7.0,
                "hrv": None if tag == 3 else 45,
                "fitnessalter": 37,
                "notizen": "privat",
                "aktivitaeten_gestern": [],
            }
            for tag in range(30, 0, -1)
        ]
        patch_request.return_value.raise_for_status.return_value = None

        historie = morgenreport.aktualisiere_schlafhistorie_spiegel(date(2026, 7, 30))

        self.assertEqual(len(historie), 28)
        self.assertEqual(historie[0]["datum"], "2026-07-03")
        self.assertEqual(historie[-1]["datum"], "2026-07-30")
        self.assertIsNone(historie[0]["hrv"])
        self.assertEqual(historie[0]["fitnessalter"], 37)
        self.assertNotIn("notizen", historie[0])
        hole_historie.assert_called_once_with(date(2026, 7, 3), date(2026, 7, 30))
        self.assertEqual(
            patch_request.call_args.kwargs["params"],
            [("updateMask.fieldPaths", "schlafhistorie_28_tage")],
        )
        gespeicherte_historie = morgenreport.firestore_wert_lesen(
            patch_request.call_args.kwargs["json"]["fields"]["schlafhistorie_28_tage"]
        )
        self.assertEqual(gespeicherte_historie, historie)

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

    @patch("morgenreport._patch_firestore_dokument")
    def test_rohquellen_werden_getrennt_und_mit_manifest_gespeichert(self, patch_dokument):
        with patch("morgenreport.ZoneInfo", return_value=None):
            morgenreport.schreibe_garmin_rohquellen_firestore(
                "2026-08-18",
                {"sleep": {"dailySleepDTO": {"sleepTimeSeconds": 28000}}},
                ["blood_pressure"],
            )

        self.assertEqual(patch_dokument.call_count, 2)
        quellaufruf, manifestaufruf = patch_dokument.call_args_list
        self.assertIn("/days/2026-08-18/sources/sleep", quellaufruf.args[0])
        self.assertEqual(quellaufruf.args[1]["encoding"], "json")
        self.assertIn("sleepTimeSeconds", quellaufruf.args[1]["data"])
        self.assertTrue(manifestaufruf.args[0].endswith("/days/2026-08-18"))
        self.assertEqual(manifestaufruf.args[1]["schema_version"], 1)
        self.assertEqual(manifestaufruf.args[1]["fehlgeschlagen"], ["blood_pressure"])

    @patch("morgenreport.hole_garmin_rohmanifest_firestore", return_value={"datum": "2026-05-20"})
    @patch("morgenreport.requests.delete")
    @patch("morgenreport.requests.get")
    def test_abgelaufener_rohtag_wird_mit_teilstuecken_entfernt(
        self, get_request, delete_request, _manifest
    ):
        get_request.return_value.json.return_value = {"documents": [{
            "name": (
                "projects/test/databases/(default)/documents/users/test-user/"
                "health/garmin_raw/days/2026-05-20/sources/heart_rates_vortag"
            ),
            "fields": {"teile": {"integerValue": "2"}},
        }]}
        get_request.return_value.raise_for_status.return_value = None
        delete_request.return_value.status_code = 200
        delete_request.return_value.raise_for_status.return_value = None

        entfernt = morgenreport.loesche_garmin_rohtag_firestore("2026-05-20")

        self.assertTrue(entfernt)
        self.assertEqual(delete_request.call_count, 4)
        urls = [aufruf.args[0] for aufruf in delete_request.call_args_list]
        self.assertTrue(any(url.endswith("/chunks/0001") for url in urls))
        self.assertTrue(any(url.endswith("/chunks/0002") for url in urls))
        self.assertTrue(any(url.endswith("/days/2026-05-20") for url in urls))

    @patch("morgenreport.loesche_garmin_rohtag_firestore", return_value=False)
    @patch("morgenreport.aktualisiere_schlafhistorie_spiegel")
    @patch("morgenreport.schreibe_garmin_rohquellen_firestore")
    @patch("morgenreport.schreibe_tageshistorie_firestore")
    @patch("morgenreport.hole_tagesdaten")
    @patch("morgenreport.hole_garmin_rohmanifest_firestore")
    @patch("morgenreport.hole_historientag_firestore")
    def test_rueckimport_laesst_vollstaendige_tage_aus_und_fuellt_luecken(
        self, hole_historientag, hole_manifest, hole_tagesdaten,
        schreibe_historie, schreibe_roh, aktualisiere_spiegel, loesche_rohtag
    ):
        hole_historientag.side_effect = [{"datum": "2026-08-17"}, None]
        hole_manifest.side_effect = [
            {"schema_version": morgenreport.GARMIN_ROHDATEN_SCHEMA_VERSION}, None
        ]
        hole_tagesdaten.return_value = {
            "datum": "2026-08-18", "body_battery": 70, "schlaf_score": 80,
            "schlafdauer_h": 7.5, "hrv": 50, "stress_avg": 25,
            "tr_score": 70, "_garmin_rohquellen": {"sleep": {}},
            "_garmin_quellen_fehler": [],
        }

        ergebnis = morgenreport.synchronisiere_garmin_historie(
            Mock(), ende=date(2026, 8, 18), tage=2
        )

        hole_tagesdaten.assert_called_once_with(ANY, "2026-08-18")
        schreibe_historie.assert_called_once()
        schreibe_roh.assert_called_once_with("2026-08-18", {"sleep": {}}, [])
        aktualisiere_spiegel.assert_called_once_with(date(2026, 8, 18))
        self.assertEqual(ergebnis["historientage_ergaenzt"], 1)
        self.assertEqual(ergebnis["rohtage_ergaenzt"], 1)
        self.assertEqual(ergebnis["uebersprungen"], 1)
        loesche_rohtag.assert_called_once_with("2026-05-20")


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

    @patch("morgenreport.synchronisiere_garmin_historie")
    @patch("morgenreport.login")
    def test_rueckimport_arbeitet_ohne_reportversand(self, login, synchronisiere):
        synchronisiere.return_value = {
            "historientage_ergaenzt": 20,
            "historientage_aktualisiert": 8,
            "rohtage_ergaenzt": 28,
            "uebersprungen": 0,
        }

        self.assertEqual(
            morgenreport.main(["--garmin-rueckimport", "--tage", "28"]), 0
        )

        synchronisiere.assert_called_once_with(login.return_value, tage=28)


if __name__ == "__main__":
    unittest.main()
