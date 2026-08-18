# GPT Action für den Fitnesscoach

## Zweck und Architektur

Der eigene Fitnesscoach-GPT soll den Morgenreport selbst laden können. Eine GPT
Action kann jedoch nur eine öffentlich erreichbare HTTPS-API aufrufen. Deshalb
dient ein kleiner Cloudflare Worker als eng begrenzte, authentifizierte
Zwischenschicht:

```text
Garmin Connect
    -> morgenreport.py im GitHub-Workflow
    -> privater Datensatz und begrenzter read-only Spiegel in Firestore
    -> GET /morgenreport, /schlafhistorie oder /langzeitvergleich im Worker
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
Die Tageshistorie ist auf die letzten 28 Tage begrenzt. Für längere Vergleiche
liefert `/langzeitvergleich` ausschließlich vorberechnete Wochen-, Monats-,
Quartals- und Jahresaggregate. Pro Aufruf sind höchstens zwölf ausdrücklich
erlaubte Kennzahlen möglich. Private Garmin-Rohantworten, Notizen und subjektive
Einträge werden nicht gespiegelt.

Für eine aktuelle Abendabfrage gibt es einen zweiten, eng begrenzten Modus:

```text
Fitnesscoach-GPT
    -> POST /aktivitaeten/heute/start (nur nach Bestätigung)
    -> derselbe feste GitHub-Workflow mit activities_only=true
    -> Garmin-Aktivitäten des heutigen Tages werden in Firestore aktualisiert
    -> GET /morgenreport/status prüft den Abschluss
    -> GET /aktivitaeten/heute liefert Datum, Aktualisierungszeit und Aktivitäten
```

Dieser Modus sendet ausdrücklich keine Telegram- oder E-Mail-Nachricht und
verändert den morgendlichen Versandmarker nicht.

## Dateien

- `worker.js`: Laufzeitcode fuer Cloudflare; prueft Authentifizierung, liest den
  festen read-only Firestore-Spiegel, startet den festen Workflow und liefert
  dessen Status.
- `openapi.yaml`: Vertrag zwischen ChatGPT und dem Worker.
- `worker.test.mjs`: isolierte Tests fuer Authentifizierung, GitHub-Aufrufe,
  Statusfilterung und Firestore-Decodierung.
- `../morgenreport.py`: schreibt Einzelwerte und `report_text` in Firestore.
  `aktivitaeten_gestern` enthält zusätzlich alle von Garmin gelieferten
  Aktivitäten des Vortags als strukturierte Liste, ohne Typfilter. Der getrennte
  Abendmodus aktualisiert nur `aktivitaeten_heute`, Datum und Zeitstempel. Nach
  jedem vollständigen Report und jeder Schlaf-Nachsynchronisierung wird außerdem
  der kompakte 28-Tage-Ausschnitt `schlafhistorie_28_tage` erneuert. Ein manueller
  Garmin-Rückimport ergänzt fehlende Historientage und archiviert die Rohquellen
  ausschließlich im privaten Benutzerpfad.
- `../history_analytics.py`: berechnet aus privaten Tageswerten deterministische
  Wochen-, Monats-, Quartals- und Jahresaggregate einschließlich Datenabdeckung.
- `../tests/test_morgenreport.py`: schützt den Firestore-Datenvertrag vor
  unbeabsichtigten Änderungen.

## Geheimnisse und ihre Aufgaben

In Cloudflare als Typ **Secret**, niemals als Klartextvariable, Quellcode
oder GitHub-Datei speichern:

- `ACTION_API_KEY`: neuer Zufallswert ausschließlich für die Verbindung
  ChatGPT -> Worker. Dieser Wert wird später auch im GPT-Editor bei der
  Bearer-Authentifizierung eingetragen.
- `TRACKER_SECRET`: bestehender Dokumentbezeichner fuer den read-only
  Firestore-Spiegel. Dieser Wert wird niemals im GPT hinterlegt.
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

1. In **Workers & Pages** den bestehenden Worker `garmin-morgenreport-gpt` öffnen.
2. Unter **Settings -> Variables and Secrets** `ACTION_API_KEY`,
   `TRACKER_SECRET` und `GITHUB_ACTIONS_TOKEN_V2` als **Secret** pflegen.
3. `worker.js` deployen, falls sich der Worker-Code geaendert hat.
4. Die in `openapi.yaml` eingetragene Worker-URL mit einem unautorisierten
   Testaufruf prüfen; erwartet wird HTTP 401 ohne Gesundheitsdaten.

## Einrichtung im eigenen GPT

1. Im GPT-Editor **Actions -> Create new action** öffnen.
2. Authentication auf **API key** und **Bearer** stellen.
3. Als Schlüssel ausschließlich den Wert von `ACTION_API_KEY` eintragen.
4. Den vollständigen Inhalt von `openapi.yaml` als Schema einfügen.
5. `getAktuellenMorgenreport`, `getSchlafhistorie`, `getLangzeitvergleich`, `startMorgenreport`,
   `getHeutigeAktivitaeten`, `startHeutigeAktivitaetenAktualisierung` und
   `getMorgenreportStatus` in der Vorschau testen.
6. In den GPT-Anweisungen festlegen:
   - Vor jeder Tagesanalyse den aktuellen Report laden und dessen Datum prüfen.
   - Bei Fragen nach Schlaf-, Erholungs- oder Trainingstrends über mehrere Tage
     `getSchlafhistorie` mit 7 bis 28 Tagen aufrufen. `tage_gefunden`, `von` und
     `bis` nennen, wenn Daten fehlen oder der Zeitraum nicht vollständig ist.
   - Für längere Zeiträume `getLangzeitvergleich` mit passender Ebene verwenden:
     Wochen für Details, Monate für mittlere Zeiträume, Quartale oder Jahre für
     langfristige Entwicklung. Nur die benötigten Kennzahlen anfordern und die
     jeweiligen Verfügbarkeitszahlen nennen.
   - Erst dann um Garmin-Exporte oder Screenshots bitten, wenn die Historie nicht
     genügend Tage enthält oder die benötigte Kennzahl darin nicht verfügbar ist.
   - `startMorgenreport` nur nach ausdrücklicher Aufforderung oder Bestätigung
     durch Gerald mit `{ "confirmed": true }` aufrufen.
   - Die zurückgegebene `run_id` mit `getMorgenreportStatus` prüfen.
   - Erst bei `status=completed` und `conclusion=success` den neuen Report laden.
   - Wenn `status=started_without_tracking` oder `run_id=null` zurückkommt,
     keine Statusabfrage und keinen zweiten Start ausführen. Gerald transparent
     mitteilen, dass der Workflow gestartet wurde, aber nicht verfolgt werden kann,
     und den zurückgegebenen GitHub-Link anzeigen.
   - Bei einem fehlgeschlagenen Lauf transparent den GitHub-Link ausgeben und
     niemals behaupten, der Report sei aktualisiert worden.
   - Bei der Frage nach heutigen oder abendlichen Aktivitäten zunächst
     `getHeutigeAktivitaeten` aufrufen und Datum sowie `aktualisiert_am` nennen.
   - Wenn Gerald aktuelle Garmin-Daten abrufen möchte, vor dem Start ausdrücklich
     bestätigen lassen. Danach `startHeutigeAktivitaetenAktualisierung` mit
     `{ "confirmed": true }`, die Statusfunktion und abschließend erneut
     `getHeutigeAktivitaeten` verwenden.
   - Ein leeres Aktivitätsarray nicht als endgültig "kein Training" ausgeben,
     ohne zugleich den Zeitpunkt des letzten Garmin-Abrufs zu nennen.

## Erwartetes Verhalten und Fehler

- `200`: JSON mit aktuellem Report, 28-Tage-Historie oder Langzeitaggregaten.
- `202`: GitHub-Workflow wurde angenommen; `status=started` enthält normalerweise
  `run_id` und `run_url`. Bei `status=started_without_tracking` wurde der Workflow
  angenommen, aber GitHub hat keine Lauf-ID zurückgegeben.
- `400`: Startbestätigung oder numerische `run_id` fehlt.
- `401`: Bearer-Schlüssel im GPT stimmt nicht mit `ACTION_API_KEY` überein.
- `404`: falscher Pfad oder falsche HTTP-Methode.
- `500`: das für den jeweiligen Endpunkt erforderliche Worker-Secret fehlt.
- `502`: Der Worker erreicht Firestore beziehungsweise GitHub nicht oder der externe
  Dienst lehnt den Aufruf ab. Interne Fehlermeldungen werden nicht weitergegeben.

Der GPT sollte nie behaupten, aktuelle Daten zu analysieren, wenn `datum` nicht
dem heutigen Datum entspricht. Bei der Historie sind `von`, `bis` und
`tage_gefunden` maßgeblich. `null` bedeutet fehlender Messwert, nicht null Punkte
und nicht Messwert 0. Trends sind persönliche Hinweise und keine medizinische
Diagnose.

## Wartung

Wenn Firestore-Felder ergänzt oder umbenannt werden, diese Stellen gemeinsam ändern:

1. `schreibe_morgenreport_firestore()` in `morgenreport.py`
2. `decodeFirestoreValue()` in `worker.js`, falls ein neuer Datentyp hinzukommt
3. Antwortschema in `openapi.yaml`
4. Tests in `tests/test_morgenreport.py` und `gpt_action/worker.test.mjs`

Die Aktivitätsliste verwendet Firestore-Arrays und verschachtelte Maps. Neue
Garmin-Aktivitätstypen werden nicht einzeln freigeschaltet: Solange Garmin sie im
Tagesabruf liefert, müssen sie ohne Filter in `aktivitaeten_gestern` und
`aktivitaeten_heute` erscheinen.

Nach Änderungen zuerst `python -m unittest discover -s tests -v` und
`node --test --test-isolation=none gpt_action/worker.test.mjs` ausführen. Die
deaktivierte Test-Isolation vermeidet gesperrte Unterprozesse in der Windows-
Arbeitsumgebung. `node --check
gpt_action/worker.js` bleibt ein schneller zusätzlicher Syntaxcheck. Keine echten
Secrets in Tests, Screenshots, Fehlermeldungen, Commits oder Chat-Nachrichten
kopieren.
