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
- `TRACKER_SECRET` (nur fuer den aktuellen read-only GPT-Spiegel)

Erforderliche Repository-Variable:

- `FIRESTORE_USER_UID`: Firebase-UID des Hauptkontos

Firestore wird über GitHub OIDC und das dedizierte Google-Servicekonto
`github-garmin-morgenreport@gewohnheitstracker-3b30a.iam.gserviceaccount.com`
angesprochen. Dadurch liegen keine langlebigen Firestore-Zugangsdaten im
Repository. Der private Morgenreport samt vollständiger Historie bleibt unter dem
Benutzerpfad. Für den kostenlosen Cloudflare-Worker werden der aktuelle Report
und ein kompakter, rollierender Ausschnitt der letzten 28 Tage zusätzlich in ein
anonym lesbares, aber nicht anonym beschreibbares Legacy-Dokument gespiegelt.
Der Ausschnitt enthält nur Trendwerte und Aktivitäten, keine Notizen oder
subjektive Einträge. Er wird nach jedem Morgenreport und nach einer
Schlaf-Nachsynchronisierung automatisch erneuert. Damit entstehen keine neuen
laufenden Dienste oder zusätzlichen Kosten; der bewusst akzeptierte Nachteil ist
die geringere Vertraulichkeit dieses begrenzten Spiegels.

## Garmin-Historie und einmaliger Rückimport

Der vollständige Morgenreport pflegt automatisch eine rollierende Historie der
letzten 28 Tage. Fehlende Tage werden aus Garmin Connect nachgeladen. Dabei gibt
es zwei getrennte Ebenen:

- `users/{uid}/health/morning_report/history/{datum}` enthält normalisierte
  Tageswerte für Trends, unter anderem Schlaf, HRV, Stress, Bewegung,
  Trainingsstatus, Intensität, Fitnessalter und verfügbare Körperwerte.
- `users/{uid}/health/garmin_raw/days/{datum}/sources/{quelle}` archiviert die
  unveränderte JSON-Antwort jeder erfolgreichen Garmin-Tagesquelle. Große
  Antworten werden verlustfrei als `gzip+base64` und bei Bedarf in Teilstücke
  gespeichert. Das Tagesmanifest nennt erfolgreiche und nicht verfügbare Quellen.
  Rohdaten werden 90 Tage aufbewahrt, damit der Speicherverbrauch im kostenlosen
  Firebase-Rahmen begrenzt bleibt; die kleinen normalisierten Tageswerte bleiben
  für langfristige Vergleiche erhalten.

Der öffentliche GPT-Spiegel enthält nur die normalisierten Analysewerte, niemals
die umfangreichen Rohantworten. Da auch Körper- und Trainingswerte im begrenzten
Spiegel enthalten sein können, gilt weiterhin das bewusst akzeptierte
Vertraulichkeitsrisiko der anonym lesbaren Garmin-Brücke.

Ein einmaliger Rückimport kann ohne Versand eines neuen Reports gestartet werden:

```powershell
python morgenreport.py --garmin-rueckimport --tage 28
```

In GitHub Actions steht dafür beim manuellen Start der Schalter
`history_backfill` bereit. Bereits vollständige Tage werden übersprungen. Quellen,
die das jeweilige Garmin-Gerät nicht liefert, bleiben fehlend und werden nicht als
Messwert 0 gespeichert. Nach dem ersten Rückimport wächst die Historie mit jedem
Morgenreport automatisch weiter.

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
# Backup wiederherstellen

Das taegliche Cockpit-Backup wird verschluesselt als GitHub-Artefakt gespeichert. Vor einer Wiederherstellung immer zuerst den Dry-Run ausfuehren:

```powershell
python firestore_restore.py --project gewohnheitstracker-3b30a --user-uid Q3hX1JXrnwV3nJa5wS7RQTrlCQi1 --input cockpit.json.enc --private-key .backup_keys/cockpit_private.pem --dry-run
```

Ein echter Restore verlangt zusaetzlich `--apply --confirm WIEDERHERSTELLEN`. Der Integrationsschluessel unter `settings/integrations` wird bewusst nicht ueberschrieben.
