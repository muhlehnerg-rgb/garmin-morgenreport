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

Der Worker stellt ausschließlich `GET /morgenreport` bereit. Er erlaubt keine
Datums-, Dokument- oder Schreibparameter. Damit kann das GPT nur den aktuellsten
Report lesen und keine anderen Firestore-Daten durchsuchen oder verändern.

## Dateien

- `worker.js`: Laufzeitcode für Cloudflare; prüft Authentifizierung, liest das
  feste Firestore-Dokument und übersetzt Firestore-Typen in gewöhnliches JSON.
- `openapi.yaml`: Vertrag zwischen ChatGPT und Worker. Nach dem Deployment muss
  genau einmal die Platzhalteradresse durch die echte Worker-Adresse ersetzt werden.
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

Diese Geheimnisse sind unabhängig von Garmin-Passwort und Telegram-Bot-Token.
Ein Widerruf von `ACTION_API_KEY` beeinträchtigt den Morgenreport-Versand nicht.

## Deployment in Cloudflare

1. In **Workers & Pages** eine Worker-Anwendung erstellen.
2. Den Inhalt von `worker.js` als Worker-Code einsetzen und deployen.
3. Unter **Settings -> Variables and Secrets** beide Werte als **Secret** anlegen.
4. Nochmals deployen, damit die neue Worker-Version die Secrets erhält.
5. Die ausgegebene `https://...workers.dev`-Adresse notieren.
6. Diese Adresse in `openapi.yaml` bei `servers[0].url` einsetzen.

## Einrichtung im eigenen GPT

1. Im GPT-Editor **Actions -> Create new action** öffnen.
2. Authentication auf **API key** und **Bearer** stellen.
3. Als Schlüssel ausschließlich den Wert von `ACTION_API_KEY` eintragen.
4. Den vollständigen Inhalt von `openapi.yaml` als Schema einfügen.
5. `getAktuellenMorgenreport` in der Vorschau testen.
6. In den GPT-Anweisungen festlegen, dass vor jeder Tagesanalyse die Action
   aufgerufen und das zurückgegebene Datum geprüft wird.

## Erwartetes Verhalten und Fehler

- `200`: JSON mit `{ "report": { ... } }`.
- `401`: Bearer-Schlüssel im GPT stimmt nicht mit `ACTION_API_KEY` überein.
- `404`: falscher Pfad oder falsche HTTP-Methode.
- `500`: mindestens ein Worker-Secret fehlt.
- `502`: Worker erreicht Firestore nicht oder das Dokument ist nicht lesbar.

Der GPT sollte nie behaupten, aktuelle Daten zu analysieren, wenn `datum` nicht
dem heutigen Datum entspricht. `null` bedeutet fehlender Messwert, nicht null
Punkte und nicht Messwert 0.

## Wartung

Wenn Firestore-Felder ergänzt oder umbenannt werden, diese Stellen gemeinsam ändern:

1. `schreibe_morgenreport_firestore()` in `morgenreport.py`
2. `decodeFirestoreValue()` in `worker.js`, falls ein neuer Datentyp hinzukommt
3. Antwortschema in `openapi.yaml`
4. Tests in `tests/test_morgenreport.py`

Nach Änderungen zuerst `python -m unittest discover -s tests -v` und
`node --check gpt_action/worker.js` ausführen. Keine echten Secrets in Tests,
Screenshots, Fehlermeldungen, Commits oder Chat-Nachrichten kopieren.
