# Garmin-Morgenreport

Erstellt morgens einen Report aus Garmin-Connect-Daten, speichert ihn lokal und versendet ihn per E-Mail und/oder Telegram. Der Report enthält alle von Garmin erfassten Aktivitäten des Vortags ohne Typfilter, beispielsweise Wandern, Laufen, Radfahren, Krafttraining oder Yoga. Optional werden Gewohnheiten aus Firestore gelesen und die Dashboard-Kachel aktualisiert.

## Lokale Einrichtung

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Danach die Werte in `.env` eintragen. Die Datei ist durch `.gitignore` ausgeschlossen und darf nicht committed werden.

Beim ersten Start oder nach Ablauf der Garmin-Anmeldung fragt das Programm lokal nach dem MFA-Code und aktualisiert `.garmin_tokens/garmin_tokens.json`.

```powershell
python morgenreport.py
```

Alternativ startet `morgenreport_starten.bat` das Programm mit derselben `.env`-Konfiguration.

## Sicherer Testmodus

```powershell
python morgenreport.py --dry-run
```

Der Testmodus liest Garmin-Daten und speichert den Report lokal. E-Mail, Telegram und Firestore werden nicht aufgerufen.

## Heutige Aktivitäten am Abend aktualisieren

Der getrennte Abendmodus liest ausschließlich die Aktivitäten des heutigen Tages
und aktualisiert dafür drei Felder im bestehenden Firestore-Dokument. Er erstellt
keinen zweiten Morgenreport, versendet weder Telegram noch E-Mail und verändert
keinen Versandmarker:

```powershell
python morgenreport.py --heutige-aktivitaeten
```

Der Fitnesscoach-GPT kann denselben Modus nach ausdrücklicher Bestätigung über
seine Action starten. Nach Abschluss kann er die heutigen Aktivitäten samt Datum
und Aktualisierungszeit abrufen und direkt auswerten.

## GitHub Actions

Erforderliche Repository-Secrets:

- `GARMIN_TOKENS_B64`
- `GARMIN_EMAIL`, `GARMIN_PASSWORD`
- `ANTHROPIC_API_KEY` (für den separaten Telegram-Coach)
- `GMAIL_ADRESSE`, `GMAIL_APP_PASSWORT`, `MORGENREPORT_EMPFAENGER`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `TRACKER_SECRET`

Der Workflow verwendet `GARMIN_TOKENS_B64` als Startwert und als Passwort für einen verschlüsselten Token-Cache. Nach einem erfolgreichen Lauf wird ein erneuertes Garmin-Token verschlüsselt für den nächsten Lauf gespeichert.

Wenn das Start- oder Refresh-Token vollständig ungültig ist, bricht GitHub Actions mit einer klaren Fehlermeldung ab. MFA wird ausschließlich lokal abgefragt.

Um ein frisches Start-Token zu hinterlegen:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes('.garmin_tokens\garmin_tokens.json'))
```

Die Ausgabe als neuen Wert von `GARMIN_TOKENS_B64` speichern. Vorhandene `garmin-tokens-...`-Caches müssen danach in GitHub Actions gelöscht werden, weil sie noch mit dem alten Wert verschlüsselt wurden.

## Tests

```powershell
python -m unittest discover -s tests -v
```
