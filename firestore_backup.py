"""Exportiert die persoenlichen Cockpit-Daten ohne Klartextausgabe im Log."""

from __future__ import annotations

import argparse
import base64
import json
import os
from datetime import date, datetime, time, timezone
from pathlib import Path


FORMAT_VERSION = 1


def json_wert(wert):
    """Wandelt Firestore-Werte verlustarm in JSON-kompatible Werte um."""
    if wert is None or isinstance(wert, (bool, int, float, str)):
        return wert
    if isinstance(wert, (datetime, date, time)):
        return {"__type": "datetime", "value": wert.isoformat()}
    if isinstance(wert, bytes):
        return {"__type": "bytes", "value": base64.b64encode(wert).decode("ascii")}
    if isinstance(wert, (list, tuple)):
        return [json_wert(eintrag) for eintrag in wert]
    if isinstance(wert, dict):
        return {str(k): json_wert(v) for k, v in sorted(wert.items())}
    if hasattr(wert, "latitude") and hasattr(wert, "longitude"):
        return {
            "__type": "geopoint",
            "latitude": wert.latitude,
            "longitude": wert.longitude,
        }
    if hasattr(wert, "path"):
        return {"__type": "document_reference", "path": wert.path}
    raise TypeError(f"Nicht unterstuetzter Firestore-Wert: {type(wert).__name__}")


def bereinige_dokument(pfad, daten):
    """Entfernt Zugangsdaten, die nicht in ein Inhaltsbackup gehoeren."""
    kopie = dict(daten)
    ausgeschlossene_felder = []
    if pfad.endswith("/settings/integrations") and "legacyTrackerKey" in kopie:
        kopie.pop("legacyTrackerKey")
        ausgeschlossene_felder.append(f"{pfad}.legacyTrackerKey")
    return kopie, ausgeschlossene_felder


def sammle_dokument(dokument_ref, dokumente, ausgeschlossen):
    """Liest ein Dokument und seine Unterkollektionen rekursiv."""
    snapshot = dokument_ref.get()
    if snapshot.exists:
        daten, entfernte_felder = bereinige_dokument(dokument_ref.path, snapshot.to_dict())
        dokumente.append({"path": dokument_ref.path, "data": json_wert(daten)})
        ausgeschlossen.extend(entfernte_felder)

    for sammlung in sorted(dokument_ref.collections(), key=lambda eintrag: eintrag.id):
        for unterdokument in sorted(sammlung.stream(), key=lambda eintrag: eintrag.id):
            sammle_dokument(unterdokument.reference, dokumente, ausgeschlossen)


def exportiere_backup(project_id, user_uid, tracker_secret=""):
    """Erzeugt den strukturierten Backup-Inhalt fuer Cockpit und Garmin-Historie."""
    from google.cloud import firestore

    client = firestore.Client(project=project_id)
    dokumente = []
    ausgeschlossen = []

    user_ref = client.collection("users").document(user_uid)
    sammle_dokument(user_ref, dokumente, ausgeschlossen)

    if tracker_secret:
        report_ref = client.collection("tracker").document(f"morgenreport_{tracker_secret}")
        sammle_dokument(report_ref, dokumente, ausgeschlossen)

    dokumente.sort(key=lambda eintrag: eintrag["path"])
    return {
        "formatVersion": FORMAT_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "projectId": project_id,
        "userUid": user_uid,
        "documents": dokumente,
        "excludedFields": sorted(ausgeschlossen),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Cockpit-Daten aus Firestore exportieren")
    parser.add_argument("--project", default=os.environ.get("FIRESTORE_PROJEKT", ""))
    parser.add_argument("--user-uid", default=os.environ.get("FIREBASE_USER_UID", ""))
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.project or not args.user_uid:
        raise SystemExit("FIRESTORE_PROJEKT und FIREBASE_USER_UID muessen gesetzt sein")

    backup = exportiere_backup(
        project_id=args.project,
        user_uid=args.user_uid,
        tracker_secret=os.environ.get("TRACKER_SECRET", ""),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(backup, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"Backup erstellt: {len(backup['documents'])} Dokumente; Klartext wird nun verschluesselt.")


if __name__ == "__main__":
    main()
