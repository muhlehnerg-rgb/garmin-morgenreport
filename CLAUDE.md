# Technische Übergabe für Claude Code

Lies vor Änderungen zuerst `README.md` und `gpt_action/README.md`. Dieses Projekt
erzeugt einen Garmin-Morgenreport, versendet ihn und stellt den neuesten Report
über eine geschützte GPT Action für einen persönlichen Fitnesscoach bereit.

## Nicht verhandelbare Sicherheitsregeln

- Niemals `.env`, Garmin-Tokens, Telegram-Tokens, `TRACKER_SECRET`,
  `ACTION_API_KEY`, `GITHUB_ACTIONS_TOKEN_V2` oder GitHub Secrets ausgeben oder
  committen.
- Secrets weder zum Debuggen in Exceptions noch in URLs, Screenshots oder Tests
  anzeigen. In Beispielen ausschließlich erkennbare Platzhalter verwenden.
- Die GPT Action darf ausschließlich den fest verdrahteten Morgenreport-Workflow
  starten und dessen Status lesen. Keine freien Repository-, Workflow- oder
  Firestore-Pfade und keine sonstigen GitHub-Schreibaktionen hinzufügen.
- `startMorgenreport` und `startHeutigeAktivitaetenAktualisierung` benötigen
  `confirmed: true` und dürfen laut GPT-Anweisungen nur nach ausdrücklicher
  Benutzeraufforderung verwendet werden.
- Fehlende Gesundheitswerte als `None`/`null` erhalten, niemals zu 0 umdeuten.
- Keine bestehende Änderung des Benutzers verwerfen und nie mit Force pushen.

## Relevanter Datenfluss

1. `morgenreport.py` liest Garmin, alle Aktivitäten des Vortags ohne Typfilter und Gewohnheiten.
2. `erstelle_text()` erzeugt denselben vollständigen Bericht für Datei, Telegram
   und Firestore.
3. `schreibe_morgenreport_firestore()` überschreibt das feste Dokument für den
   aktuellsten Report.
4. `gpt_action/worker.js` authentifiziert den GPT per Bearer-Schlüssel, liest das
   feste Dokument, startet den festen Workflow und filtert dessen Statusantwort.
5. `gpt_action/openapi.yaml` beschreibt die Lese-, Start- und Status-Actions.
6. Im Abendmodus liest `morgenreport.py --heutige-aktivitaeten` nur den heutigen
   Garmin-Tag und aktualisiert per Firestore-Update-Mask ausschließlich
   `aktivitaeten_heute`, dessen Datum und dessen Aktualisierungszeit.

## Änderungsregeln

- Änderungen am Firestore-Datenmodell immer synchron in Python, Worker,
  OpenAPI-Schema und Tests durchführen.
- `aktivitaeten_gestern` bleibt eine typunabhängige Liste. Unbekannte oder neue
  Garmin-Aktivitätstypen dürfen nicht herausgefiltert werden.
- Dasselbe gilt für `aktivitaeten_heute`. Der Abendmodus darf keinen Report
  versenden und keinen `.last_sent_date.txt`-Marker schreiben.
- `operationId: getAktuellenMorgenreport` stabil halten, da GPT-Anweisungen darauf
  Bezug nehmen können.
- Auch `startMorgenreport` und `getMorgenreportStatus` stabil halten. Der Start
  muss fest auf `main` und `.github/workflows/morgenreport.yml` begrenzt bleiben.
- `getHeutigeAktivitaeten` und `startHeutigeAktivitaetenAktualisierung` ebenfalls
  stabil halten. Auch dieser Start verwendet ausschließlich denselben festen
  Workflow mit dem booleschen `activities_only`-Eingang.
- Keine medizinischen Diagnosen in die technische Pipeline einbauen. Die Daten
  und regelbasierte Empfehlung werden geliefert; die Coach-Kommunikation gehört
  in die Anweisungen des eigenen GPT.
- Der Firestore-Fehler nach erfolgreichem Telegram-Versand ist derzeit bewusst
  nicht fatal. Dieses Verhalten nur nach Rücksprache ändern.

## Prüfung vor Übergabe

```powershell
python -m unittest discover -s tests -v
node --check gpt_action\worker.js
node --test --test-isolation=none gpt_action\worker.test.mjs
git diff --check
```

Erst nach erfolgreichen Prüfungen committen. Push auf `main` nur nach ausdrücklicher
Bestätigung des Benutzers.
