import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backup_crypto import entschluesseln, erzeuge_schluesselpaar, verschluesseln
from firestore_backup import bereinige_dokument, json_wert


class FirestoreBackupTests(unittest.TestCase):
    def test_json_wert_konvertiert_verschachtelte_werte(self):
        wert = {
            "zeit": datetime(2026, 7, 28, 12, 30, tzinfo=timezone.utc),
            "liste": [True, 3, None],
            "bytes": b"abc",
        }
        ergebnis = json_wert(wert)
        self.assertEqual(ergebnis["zeit"]["value"], "2026-07-28T12:30:00+00:00")
        self.assertEqual(ergebnis["liste"], [True, 3, None])
        self.assertEqual(ergebnis["bytes"]["value"], "YWJj")

    def test_integration_key_wird_nicht_exportiert(self):
        daten, ausgeschlossen = bereinige_dokument(
            "users/uid/settings/integrations",
            {"legacyTrackerKey": "geheim", "schemaVersion": 1},
        )
        self.assertEqual(daten, {"schemaVersion": 1})
        self.assertEqual(
            ausgeschlossen,
            ["users/uid/settings/integrations.legacyTrackerKey"],
        )


class BackupCryptoTests(unittest.TestCase):
    def test_backup_verschluesselung_roundtrip(self):
        with tempfile.TemporaryDirectory() as ordner:
            basis = Path(ordner)
            privat = basis / "private.pem"
            oeffentlich = basis / "public.pem"
            klartext = basis / "backup.json"
            chiffre = basis / "backup.json.enc"
            wiederhergestellt = basis / "restored.json"
            klartext.write_text('{"test": true}', encoding="utf-8")

            erzeuge_schluesselpaar(privat, oeffentlich)
            verschluesseln(klartext, chiffre, oeffentlich)
            entschluesseln(chiffre, wiederhergestellt, privat)

            self.assertNotEqual(chiffre.read_bytes(), klartext.read_bytes())
            self.assertEqual(wiederhergestellt.read_bytes(), klartext.read_bytes())


if __name__ == "__main__":
    unittest.main()
