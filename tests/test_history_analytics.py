import unittest
from datetime import date

from history_analytics import aggregate_history, period_key


class HistoryAnalyticsTests(unittest.TestCase):
    def test_periodenschluessel_beachtet_iso_jahreswechsel(self):
        self.assertEqual(period_key(date(2027, 1, 1), "week"), "2026-W53")
        self.assertEqual(period_key(date(2026, 8, 18), "month"), "2026-08")
        self.assertEqual(period_key(date(2026, 8, 18), "quarter"), "2026-Q3")
        self.assertEqual(period_key(date(2026, 8, 18), "year"), "2026")

    def test_aggregate_ignorieren_fehlende_werte_aber_behalten_echte_null(self):
        days = [
            {
                "datum": "2026-08-01",
                "schlafdauer_h": 7.0,
                "hrv": 40,
                "schritte": 0,
                "tr_level": "LOW",
                "aktivitaeten_gestern": [],
            },
            {
                "datum": "2026-08-02",
                "schlafdauer_h": None,
                "hrv": 50,
                "schritte": 10000,
                "tr_level": "HIGH",
                "aktivitaeten_gestern": [{
                    "typ": "running",
                    "dauer_min": 30,
                    "distanz_km": 5.2,
                    "kalorien": 320,
                }],
            },
        ]

        month = aggregate_history(days)["month"][0]

        self.assertEqual(month["mittelwerte"]["schlafdauer_h"], 7.0)
        self.assertEqual(month["werte_verfuegbar"]["schlafdauer_h"], 1)
        self.assertEqual(month["mittelwerte"]["hrv"], 45.0)
        self.assertEqual(month["mittelwerte"]["schritte"], 5000.0)
        self.assertEqual(month["summen"]["schritte"], 10000)
        self.assertEqual(month["summen"]["aktivitaeten_anzahl"], 1)
        self.assertEqual(month["summen"]["aktivitaeten_dauer_min"], 30)
        self.assertEqual(month["aktivitaetstypen"], {"running": 1})
        self.assertEqual(month["kategorien"]["tr_level"], {"HIGH": 1, "LOW": 1})

    def test_aggregate_sortieren_perioden_und_verwerfen_ungueltige_tage(self):
        result = aggregate_history([
            {"datum": "2026-02-01", "hrv": 50},
            {"datum": "kein-datum", "hrv": 999},
            {"datum": "2026-01-01", "hrv": 40},
        ])

        self.assertEqual(
            [item["periode"] for item in result["month"]],
            ["2026-01", "2026-02"],
        )
        self.assertEqual(result["year"][0]["tage_gefunden"], 2)


if __name__ == "__main__":
    unittest.main()
