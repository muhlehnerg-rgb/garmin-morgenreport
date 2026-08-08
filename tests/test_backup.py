import tempfile
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path

from backup_crypto import entschluesseln, erzeuge_schluesselpaar, verschluesseln
from firestore_backup import bereinige_dokument, json_wert
from firestore_restore import firestore_wert, lade_backup, main, restore_backup, validiere_backup


class FakeSnapshot:
    def __init__(self, reference, exists):
        self.reference = reference
        self.exists = exists


class FakeDocument:
    def __init__(self, client, path):
        self.client = client
        self.path = path

    def get(self):
        return FakeSnapshot(self, self.path in self.client.documents)

    def collections(self):
        prefix = self.path + "/"
        depth = len(self.path.split("/"))
        names = {
            path.split("/")[depth]
            for path in self.client.documents
            if path.startswith(prefix) and len(path.split("/")) >= depth + 2
        }
        return [FakeCollection(self.client, prefix + name) for name in sorted(names)]


class FakeCollection:
    def __init__(self, client, path):
        self.client = client
        self.path = path

    def document(self, document_id):
        return FakeDocument(self.client, self.path + "/" + document_id)

    def stream(self):
        depth = len(self.path.split("/"))
        paths = {
            path
            for path in self.client.documents
            if path.startswith(self.path + "/") and len(path.split("/")) == depth + 1
        }
        return [FakeSnapshot(FakeDocument(self.client, path), True) for path in sorted(paths)]


class FakeBatch:
    def __init__(self, client):
        self.client = client
        self.operations = []

    def delete(self, reference):
        self.operations.append(("delete", reference.path, None))

    def set(self, reference, data):
        self.operations.append(("set", reference.path, data))

    def commit(self):
        for operation, path, data in self.operations:
            if operation == "delete":
                self.client.documents.pop(path, None)
            else:
                self.client.documents[path] = data


class FakeFirestoreClient:
    def __init__(self, documents):
        self.documents = dict(documents)

    def collection(self, name):
        return FakeCollection(self, name)

    def document(self, path):
        return FakeDocument(self, path)

    def batch(self):
        return FakeBatch(self)


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

    def test_restore_validiert_projekt_benutzer_und_pfade(self):
        backup = {
            "formatVersion": 1,
            "projectId": "projekt",
            "userUid": "uid",
            "documents": [
                {"path": "users/uid/tasks/a", "data": {"title": "Test"}},
                {"path": "tracker/morgenreport_secret/historie/2026-08-01", "data": {}},
            ],
        }
        self.assertEqual(len(validiere_backup(backup, "projekt", "uid")), 2)
        backup["documents"].append({"path": "users/fremd/tasks/a", "data": {}})
        with self.assertRaisesRegex(ValueError, "nicht erlaubten"):
            validiere_backup(backup, "projekt", "uid")

    def test_restore_konvertiert_zeitstempel_und_bytes(self):
        result = firestore_wert({
            "createdAt": {"__type": "datetime", "value": "2026-08-01T08:00:00+00:00"},
            "payload": {"__type": "bytes", "value": "YWJj"},
        })
        self.assertEqual(result["createdAt"].isoformat(), "2026-08-01T08:00:00+00:00")
        self.assertEqual(result["payload"], b"abc")

    def test_restore_laesst_json_dry_run_ohne_cloud_laden(self):
        with tempfile.TemporaryDirectory() as ordner:
            pfad = Path(ordner) / "backup.json"
            backup = {"formatVersion": 1, "projectId": "projekt", "userUid": "uid", "documents": []}
            pfad.write_text(json.dumps(backup), encoding="utf-8")
            self.assertEqual(lade_backup(pfad), backup)

    def test_restore_ersetzt_den_gesicherten_bereich_vollstaendig(self):
        client = FakeFirestoreClient({
            "users/uid/tasks/alt": {"title": "Alt"},
            "users/uid/projects/projekt": {"name": "Vorher"},
            "users/uid/goals/alt": {"title": "Altes Ziel"},
            "users/uid/settings/integrations": {"legacyTrackerKey": "bleibt"},
            "tracker/morgenreport_secret/historie/alt": {"date": "2026-07-31"},
            "tracker/morgenreport_anders/historie/bleibt": {"date": "2026-07-30"},
        })
        backup = {
            "formatVersion": 1,
            "projectId": "projekt",
            "userUid": "uid",
            "documents": [
                {"path": "users/uid/tasks/neu", "data": {"title": "Neu"}},
                {"path": "users/uid/projects/projekt", "data": {"name": "Danach"}},
                {"path": "users/uid/goals/neu", "data": {"title": "Neues Ziel"}},
                {"path": "users/uid/settings/integrations", "data": {"schemaVersion": 1}},
                {"path": "tracker/morgenreport_secret/historie/neu", "data": {"date": "2026-08-01"}},
            ],
        }

        result = restore_backup(backup, "projekt", "uid", client)

        self.assertEqual(result["documents"], 5)
        self.assertNotIn("users/uid/tasks/alt", client.documents)
        self.assertEqual(client.documents["users/uid/tasks/neu"], {"title": "Neu"})
        self.assertEqual(client.documents["users/uid/projects/projekt"], {"name": "Danach"})
        self.assertNotIn("users/uid/goals/alt", client.documents)
        self.assertEqual(client.documents["users/uid/goals/neu"], {"title": "Neues Ziel"})
        self.assertEqual(
            client.documents["users/uid/settings/integrations"],
            {"legacyTrackerKey": "bleibt"},
        )
        self.assertNotIn("tracker/morgenreport_secret/historie/alt", client.documents)
        self.assertEqual(
            client.documents["tracker/morgenreport_secret/historie/neu"],
            {"date": "2026-08-01"},
        )
        self.assertIn("tracker/morgenreport_anders/historie/bleibt", client.documents)

    def test_restore_apply_verlangt_exakte_bestaetigung(self):
        with tempfile.TemporaryDirectory() as ordner:
            pfad = Path(ordner) / "backup.json"
            pfad.write_text(json.dumps({
                "formatVersion": 1,
                "projectId": "projekt",
                "userUid": "uid",
                "documents": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "WIEDERHERSTELLEN"):
                main([
                    "--project", "projekt",
                    "--user-uid", "uid",
                    "--input", str(pfad),
                    "--apply",
                ])


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
