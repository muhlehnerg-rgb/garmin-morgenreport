# GPT Action für den Fitnesscoach

## Zweck und Architektur

Der eigene Fitnesscoach-GPT soll den Morgenreport selbst laden können. Eine GPT
Action kann jedoch nur eine öffentlich erreichbare HTTPS-API aufrufen. Deshalb
dient ein kleiner Cloudflare Worker als eng begrenzte, authentifizierte
Zwischenschicht:

```text
Garmin Connect
    -> morgenreport.py im GitHub-Workflow
    -> festes Dokument in Firestore
    -> GET /morgenreport im Cloudflare Worker
    -> Action des persönlichen Fitnesscoach-GPT
```

Für einen Report auf Zuruf gibt es zusätzlich den umgekehrten Weg:

```text
Fitnesscoach-GPT
    -> POST /morgenreport/start im Cloudflare Worker
    -> fest konfigurierter workflow_dispatch bei GitHub
    -> morgenreport.py aktualisiert Firestore
    -> GET /morgenreport/status prüft den Abschluss
    -> GET /morgenreport lädt den neuen Bericht
```

Der Worker akzeptiert keine freien Repository-, Workflow-, Firestore- oder
Dokumentparameter. Dadurch kann das GPT ausschließlich den Morgenreport-Workflow
starten und keine anderen GitHub-Aktionen oder Firestore-Daten erreichen.

## Dateien

- `worker.js`: Laufzeitcode für Cloudflare; prüft Authentifizierung, liest das
  feste Firestore-Dokument, startet den festen Workflow und liefert dessen Status.
- `openapi.yaml`: Vertrag zwischen ChatGPT und Worker. Nach dem Deployment muss
  genau einmal die Platzhalteradresse durch die echte Worker-Adresse ersetzt werden.
- `worker.test.mjs`: isolierte Tests für Authentifizierung, Bestätigung,
  GitHub-Aufrufe, Statusfilterung und Firestore-Decodierung.
- `../morgenreport.py`: schreibt Einzelwerte und `report_text` in Firestore.
- `../tests/test_morgenreport.py`: schützt den Firestore-Datenvertrag vor
  unbeabsichtigten Änderungen.

## Geheimnisse und ihre Aufgaben

In Cloudflare als Typ **Secret**, niemals als Klartextvariable, Quellcode oder
GitHub-Datei speichern:

- `ACTION_API_KEY`: neuer Zufallswert ausschließlich für die Verbindung
  ChatGPT -> Worker. Dieser Wert wird später auch im GPT-Editor bei der
  Bearer-Authentifizierung eingetragen.
- `TRACKER_SECRET`: bestehender Wert, mit dem der Worker das richtige
  Firestore-Dokument findet. Dieser Wert wird niemals im GPT hinterlegt.
- `GITHUB_ACTIONS_TOKEN_V2`: Fine-grained Personal Access Token, eingeschränkt auf
  das Repository `garmin-morgenreport` und **Actions: Read and write**. Der Token
  wird nur vom Worker an GitHub gesendet und niemals im GPT hinterlegt.

Diese Geheimnisse sind unabhängig von Garmin-Passwort und Telegram-Bot-Token.
Ein Widerruf von `ACTION_API_KEY` beeinträchtigt den Morgenreport-Versand nicht.

## GitHub-Token sicher erstellen

1. GitHub **Settings -> Developer settings -> Personal access tokens ->
   Fine-grained tokens** öffnen.
2. Einen kurzen, eindeutigen Namen und ein Ablaufdatum wählen.
3. Bei **Repository access** nur `garmin-morgenreport` auswählen.
4. Unter **Repository permissions** ausschließlich **Actions: Read and write**
   aktivieren. Automatisch erforderliche Metadaten-Leserechte bleiben bestehen.
5. Token erzeugen und unmittelbar als Cloudflare-Secret
   `GITHUB_ACTIONS_TOKEN_V2` eintragen.
6. Den Token nicht in Chat, Notizen, `.env`, GitHub Secrets oder Quellcode kopieren.

Nach Ablauf oder Widerruf kann der GPT weiterhin vorhandene Reports lesen; nur
der Start- und Statusaufruf funktionieren dann bis zur Erneuerung nicht.

## Deployment in Cloudflare

1. In **Workers & Pages** eine Worker-Anwendung erstellen.
2. Den Inhalt von `worker.js` als Worker-Code einsetzen und deployen.
3. Unter **Settings -> Variables and Secrets** alle drei Werte als **Secret** anlegen.
4. Nochmals deployen, damit die neue Worker-Version die Secrets erhält.
5. Die ausgegebene `https://...workers.dev`-Adresse notieren.
6. Diese Adresse in `openapi.yaml` bei `servers[0].url` einsetzen.

## Einrichtung im eigenen GPT

1. Im GPT-Editor **Actions -> Create new action** öffnen.
2. Authentication auf **API key** und **Bearer** stellen.
3. Als Schlüssel ausschließlich den Wert von `ACTION_API_KEY` eintragen.
4. Den vollständigen Inhalt von `openapi.yaml` als Schema einfügen.
5. `getAktuellenMorgenreport`, `startMorgenreport` und
   `getMorgenreportStatus` in der Vorschau testen.
6. In den GPT-Anweisungen festlegen:
   - Vor jeder Tagesanalyse den aktuellen Report laden und dessen Datum prüfen.
   - `startMorgenreport` nur nach ausdrücklicher Aufforderung oder Bestätigung
     durch Gerald mit `{ "confirmed": true }` aufrufen.
   - Die zurückgegebene `run_id` mit `getMorgenreportStatus` prüfen.
   - Erst bei `status=completed` und `conclusion=success` den neuen Report laden.
   - Bei einem fehlgeschlagenen Lauf transparent den GitHub-Link ausgeben und
     niemals behaupten, der Report sei aktualisiert worden.

## Erwartetes Verhalten und Fehler

- `200`: JSON mit `{ "report": { ... } }`.
- `202`: GitHub-Workflow wurde angenommen; Antwort enthält nach Möglichkeit
  `run_id` und `run_url`.
- `400`: Startbestätigung oder numerische `run_id` fehlt.
- `401`: Bearer-Schlüssel im GPT stimmt nicht mit `ACTION_API_KEY` überein.
- `404`: falscher Pfad oder falsche HTTP-Methode.
- `500`: das für den jeweiligen Endpunkt erforderliche Worker-Secret fehlt.
- `502`: Worker erreicht Firestore beziehungsweise GitHub nicht oder der externe
  Dienst lehnt den Aufruf ab. Interne Fehlermeldungen werden nicht weitergegeben.

Der GPT sollte nie behaupten, aktuelle Daten zu analysieren, wenn `datum` nicht
dem heutigen Datum entspricht. `null` bedeutet fehlender Messwert, nicht null
Punkte und nicht Messwert 0.

## Wartung

Wenn Firestore-Felder ergänzt oder umbenannt werden, diese Stellen gemeinsam ändern:

1. `schreibe_morgenreport_firestore()` in `morgenreport.py`
2. `decodeFirestoreValue()` in `worker.js`, falls ein neuer Datentyp hinzukommt
3. Antwortschema in `openapi.yaml`
4. Tests in `tests/test_morgenreport.py` und `gpt_action/worker.test.mjs`

Nach Änderungen zuerst `python -m unittest discover -s tests -v` und
`node --test --test-isolation=none gpt_action/worker.test.mjs` ausführen. Die
deaktivierte Test-Isolation vermeidet gesperrte Unterprozesse in der Windows-
Arbeitsumgebung. `node --check
gpt_action/worker.js` bleibt ein schneller zusätzlicher Syntaxcheck. Keine echten
Secrets in Tests, Screenshots, Fehlermeldungen, Commits oder Chat-Nachrichten
kopieren.
