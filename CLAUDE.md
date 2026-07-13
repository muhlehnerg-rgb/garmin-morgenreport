# Technische Übergabe für Claude Code

Lies vor Änderungen zuerst `README.md` und `gpt_action/README.md`. Dieses Projekt
erzeugt einen Garmin-Morgenreport, versendet ihn und stellt den neuesten Report
über eine geschützte GPT Action für einen persönlichen Fitnesscoach bereit.

## Nicht verhandelbare Sicherheitsregeln

- Niemals `.env`, Garmin-Tokens, Telegram-Tokens, `TRACKER_SECRET`,
  `ACTION_API_KEY` oder GitHub Secrets ausgeben oder committen.
- Secrets weder zum Debuggen in Exceptions noch in URLs, Screenshots oder Tests
  anzeigen. In Beispielen ausschließlich erkennbare Platzhalter verwenden.
- Die GPT Action bleibt read-only. Keine Schreibroute und keinen frei wählbaren
  Firestore-Pfad hinzufügen.
- Fehlende Gesundheitswerte als `None`/`null` erhalten, niemals zu 0 umdeuten.
- Keine bestehende Änderung des Benutzers verwerfen und nie mit Force pushen.

## Relevanter Datenfluss

1. `morgenreport.py` liest Garmin und Gewohnheiten.
2. `erstelle_text()` erzeugt denselben vollständigen Bericht für Datei, Telegram
   und Firestore.
3. `schreibe_morgenreport_firestore()` überschreibt das feste Dokument für den
   aktuellsten Report.
4. `gpt_action/worker.js` authentifiziert den GPT per Bearer-Schlüssel, liest nur
   dieses Dokument und normalisiert die Firestore-REST-Typen.
5. `gpt_action/openapi.yaml` beschreibt dem eigenen GPT die lesende Action.

## Änderungsregeln

- Änderungen am Firestore-Datenmodell immer synchron in Python, Worker,
  OpenAPI-Schema und Tests durchführen.
- `operationId: getAktuellenMorgenreport` stabil halten, da GPT-Anweisungen darauf
  Bezug nehmen können.
- Keine medizinischen Diagnosen in die technische Pipeline einbauen. Die Daten
  und regelbasierte Empfehlung werden geliefert; die Coach-Kommunikation gehört
  in die Anweisungen des eigenen GPT.
- Der Firestore-Fehler nach erfolgreichem Telegram-Versand ist derzeit bewusst
  nicht fatal. Dieses Verhalten nur nach Rücksprache ändern.

## Prüfung vor Übergabe

```powershell
python -m unittest discover -s tests -v
node --check gpt_action\worker.js
git diff --check
```

Erst nach erfolgreichen Prüfungen committen. Push auf `main` nur nach ausdrücklicher
Bestätigung des Benutzers.
