"""Prueft oder restauriert ein verschluesseltes Cockpit-Firestore-Backup."""

from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from backup_crypto import entschluesseln


FIRESTORE_DATENBANK = os.environ.get("FIRESTORE_DATENBANK", "default")
FORMAT_VERSION = 1
CONFIRMATION = "WIEDERHERSTELLEN"


def lade_backup(input_path: Path, private_key: Path | None = None):
    """Liest JSON direkt oder entschluesselt zuvor das Backup-Artefakt."""
    if private_key is None:
        return json.loads(input_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temp_dir:
        plaintext = Path(temp_dir) / "cockpit.json"
        entschluesseln(input_path, plaintext, private_key)
        return json.loads(plaintext.read_text(encoding="utf-8"))


def validiere_backup(backup, project_id: str, user_uid: str):
    if not isinstance(backup, dict) or backup.get("formatVersion") != FORMAT_VERSION:
        raise ValueError("Unbekanntes Backup-Format")
    if backup.get("projectId") != project_id or backup.get("userUid") != user_uid:
        raise ValueError("Backup gehoert nicht zum angegebenen Projekt oder Benutzer")
    documents = backup.get("documents")
    if not isinstance(documents, list) or len(documents) > 20000:
        raise ValueError("Ungueltige Dokumentliste")

    allowed_user_prefix = f"users/{user_uid}"
    seen = set()
    normalized = []
    for entry in documents:
        if not isinstance(entry, dict) or set(entry) != {"path", "data"}:
            raise ValueError("Ungueltiger Dokumenteintrag")
        path = entry["path"]
        data = entry["data"]
        if not isinstance(path, str) or not isinstance(data, dict):
            raise ValueError("Ungueltiger Dokumentpfad oder Dokumentinhalt")
        segments = path.split("/")
        if len(segments) < 2 or len(segments) % 2 != 0 or any(not segment for segment in segments):
            raise ValueError("Ungueltiger Firestore-Dokumentpfad")
        allowed = path == allowed_user_prefix or path.startswith(allowed_user_prefix + "/")
        allowed = allowed or (segments[0] == "tracker" and segments[1].startswith("morgenreport_"))
        if not allowed:
            raise ValueError("Backup enthaelt einen nicht erlaubten Dokumentpfad")
        if path in seen:
            raise ValueError("Backup enthaelt einen Dokumentpfad mehrfach")
        seen.add(path)
        normalized.append({"path": path, "data": data})
    return normalized


def firestore_wert(value, client=None):
    if isinstance(value, list):
        return [firestore_wert(entry, client) for entry in value]
    if not isinstance(value, dict):
        return value
    value_type = value.get("__type")
    if value_type == "datetime":
        return datetime.fromisoformat(value["value"])
    if value_type == "bytes":
        return base64.b64decode(value["value"])
    if value_type == "geopoint":
        from google.cloud.firestore_v1 import GeoPoint
        return GeoPoint(value["latitude"], value["longitude"])
    if value_type == "document_reference":
        if client is None:
            raise ValueError("Dokumentreferenz kann ohne Firestore-Client nicht restauriert werden")
        return client.document(value["path"])
    return {key: firestore_wert(entry, client) for key, entry in value.items()}


def sammle_pfade(document_ref, paths):
    snapshot = document_ref.get()
    if snapshot.exists:
        paths.add(document_ref.path)
    for collection_ref in document_ref.collections():
        for child in collection_ref.stream():
            sammle_pfade(child.reference, paths)


def restore_backup(backup, project_id: str, user_uid: str, client=None):
    """Setzt Benutzerbereich und gesicherte Garmin-Dokumente auf den Backup-Stand."""
    documents = validiere_backup(backup, project_id, user_uid)
    if client is None:
        from google.cloud import firestore
        client = firestore.Client(project=project_id, database=FIRESTORE_DATENBANK)

    desired = {entry["path"]: entry["data"] for entry in documents}
    protected = {f"users/{user_uid}/settings/integrations"}
    current = set()
    sammle_pfade(client.collection("users").document(user_uid), current)
    for path in desired:
        segments = path.split("/")
        if segments[0] == "tracker" and len(segments) >= 2:
            root = client.collection("tracker").document(segments[1])
            if root.path not in current:
                sammle_pfade(root, current)

    operations = []
    for path in sorted(current - set(desired) - protected, key=lambda item: item.count("/"), reverse=True):
        operations.append(("delete", path, None))
    for path, data in sorted(desired.items()):
        if path not in protected:
            operations.append(("set", path, firestore_wert(data, client)))

    for start in range(0, len(operations), 400):
        batch = client.batch()
        for operation, path, data in operations[start:start + 400]:
            reference = client.document(path)
            if operation == "delete":
                batch.delete(reference)
            else:
                batch.set(reference, data)
        batch.commit()
    return {"documents": len(documents), "operations": len(operations), "protected": sorted(protected)}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Cockpit-Firestore-Backup pruefen oder restaurieren")
    parser.add_argument("--project", default=os.environ.get("FIRESTORE_PROJEKT", ""))
    parser.add_argument("--user-uid", default=os.environ.get("FIREBASE_USER_UID", ""))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--private-key", type=Path, help="Bei verschluesselten .enc-Artefakten erforderlich")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="", help=f"Fuer --apply exakt {CONFIRMATION} angeben")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.project or not args.user_uid:
        raise SystemExit("FIRESTORE_PROJEKT und FIREBASE_USER_UID muessen gesetzt sein")
    backup = lade_backup(args.input, args.private_key)
    documents = validiere_backup(backup, args.project, args.user_uid)
    if args.dry_run:
        print(f"Dry-Run erfolgreich: {len(documents)} Dokumente sind gueltig; keine Daten wurden geschrieben.")
        return 0
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"Restore abgebrochen: --confirm {CONFIRMATION} fehlt")
    result = restore_backup(backup, args.project, args.user_uid)
    print(f"Restore abgeschlossen: {result['documents']} Dokumente, {result['operations']} Operationen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
