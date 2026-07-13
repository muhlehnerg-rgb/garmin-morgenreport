# GPT Action für den Fitnesscoach

Der Cloudflare Worker stellt ausschließlich `GET /morgenreport` bereit. Er liest den
aktuellen Datensatz aus Firestore und verlangt `Authorization: Bearer <API-Key>`.

Benötigte Worker-Secrets:

- `ACTION_API_KEY`: ein neuer, nur für die GPT Action verwendeter Zufallswert
- `TRACKER_SECRET`: derselbe Wert wie im Garmin-Morgenreport

Nach dem Deployment die Worker-Adresse in `openapi.yaml` eintragen. Im GPT-Editor
unter **Actions > Authentication** `API key` und `Bearer` auswählen, dort nur den
Wert von `ACTION_API_KEY` eintragen und anschließend `openapi.yaml` als Schema
einfügen.
