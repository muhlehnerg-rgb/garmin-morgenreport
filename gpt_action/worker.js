/*
 * HTTPS-Zwischenschicht für die Action des persönlichen Fitnesscoach-GPT.
 *
 * Warum es diesen Worker gibt:
 * ChatGPT Actions können eine normale HTTPS-API mit einem API-Schlüssel aufrufen,
 * sollen aber weder den internen Firestore-Dokumentnamen noch TRACKER_SECRET
 * kennen. Der Worker hält diese Details serverseitig geheim und gibt nur genau
 * einen lesenden Endpunkt frei. Garmin-, Telegram- oder GitHub-Zugangsdaten werden
 * hier weder benötigt noch gespeichert.
 *
 * Datenfluss:
 * morgenreport.py -> Firestore -> dieser Worker -> Fitnesscoach-GPT
 *
 * Die beiden Werte ACTION_API_KEY und TRACKER_SECRET müssen in Cloudflare als
 * verschlüsselte Secrets angelegt werden. Sie dürfen niemals direkt in dieser
 * Datei, im OpenAPI-Schema oder in Git committed werden.
 */

// Der Projektname ist keine Zugangsinformation. Der geheime Teil des
// Dokumentpfads wird erst weiter unten aus env.TRACKER_SECRET zusammengesetzt.
// Dieses Projekt akzeptiert in der REST-URL die Datenbank-ID `default` ohne
// Klammern; `(default)` liefert hier nachweislich HTTP 404.
const FIRESTORE_BASE =
  "https://firestore.googleapis.com/v1/projects/gewohnheitstracker-3b30a/databases/default/documents";

/**
 * Erzeugt für Erfolg und Fehler immer dieselbe saubere JSON-Antwortstruktur.
 * `no-store` ist absichtlich gesetzt: Gesundheitsdaten sollen weder bei
 * Cloudflare noch in einem zwischengeschalteten Cache wiederverwendet werden.
 */
function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

/**
 * Firestores REST-API liefert typisierte Werte wie
 * `{ "integerValue": "42" }`. Ein GPT braucht stattdessen gewöhnliches JSON
 * wie `{ "body_battery": 42 }`. Diese Funktion übersetzt genau die Datentypen,
 * die morgenreport.py aktuell schreibt.
 *
 * Wenn später Arrays, Maps oder Zeitstempel in Firestore ergänzt werden, muss
 * diese Funktion entsprechend erweitert und getestet werden. Unbekannte Typen
 * werden bewusst zu null, damit keine rohe Firestore-Interna ausgegeben wird.
 */
function decodeFirestoreValue(value) {
  if ("stringValue" in value) return value.stringValue;
  if ("integerValue" in value) return Number(value.integerValue);
  if ("doubleValue" in value) return value.doubleValue;
  if ("booleanValue" in value) return value.booleanValue;
  if ("nullValue" in value) return null;
  return null;
}

export default {
  /**
   * Zentraler Request-Handler des Cloudflare Workers.
   *
   * Cloudflare übergibt:
   * - request: Methode, URL und Header des GPT-Aufrufs
   * - env: verschlüsselte Worker-Secrets und andere Bindings
   */
  async fetch(request, env) {
    const url = new URL(request.url);

    // Absichtlich nur eine einzige lesende Route zulassen. Dadurch kann diese
    // Action keine Garmin-Daten verändern und keine beliebigen Firestore-Pfade
    // abfragen. Auch POST, PATCH und DELETE enden hier mit 404.
    if (request.method !== "GET" || url.pathname !== "/morgenreport") {
      return json({ error: "Not found" }, 404);
    }

    // Im GPT-Editor wird ACTION_API_KEY als Authentifizierung vom Typ "Bearer"
    // hinterlegt. ChatGPT sendet dadurch `Authorization: Bearer <Schlüssel>`.
    // Fehlt das serverseitige Secret oder stimmt der Header nicht exakt überein,
    // werden keinerlei Gesundheitsdaten und keine Konfigurationsdetails geliefert.
    const expected = `Bearer ${env.ACTION_API_KEY}`;
    if (!env.ACTION_API_KEY || request.headers.get("authorization") !== expected) {
      return json({ error: "Unauthorized" }, 401);
    }

    // TRACKER_SECRET bezeichnet das bereits bestehende Firestore-Dokument. Eine
    // fehlende Konfiguration ist ein Serverfehler und kein "Report nicht gefunden".
    if (!env.TRACKER_SECRET) {
      return json({ error: "Server configuration incomplete" }, 500);
    }

    // encodeURIComponent verhindert, dass ein unerwartetes Zeichen im Secret den
    // Dokumentpfad verändert. Der vollständige Pfad wird nie an den GPT ausgegeben.
    const documentUrl =
      `${FIRESTORE_BASE}/tracker/morgenreport_${encodeURIComponent(env.TRACKER_SECRET)}`;

    // Der Worker liest immer das eine Dokument, das der GitHub-Workflow bei jedem
    // erfolgreichen Morgenreport überschreibt. Deshalb ist keine Datumsabfrage nötig.
    const firestoreResponse = await fetch(documentUrl, {
      headers: { accept: "application/json" },
    });

    // Firestore-Fehler werden absichtlich verallgemeinert. So gelangen weder der
    // geheime Dokumentpfad noch interne Google-Fehlermeldungen zum Aufrufer.
    if (!firestoreResponse.ok) {
      return json({ error: "Morgenreport could not be loaded" }, 502);
    }

    const document = await firestoreResponse.json();

    // Nur das fachliche `fields`-Objekt wird zurückgegeben. Firestore-Metadaten wie
    // Dokumentname, Erstellungszeit und interner Pfad bleiben serverseitig.
    const report = Object.fromEntries(
      Object.entries(document.fields || {}).map(([key, value]) => [
        key,
        decodeFirestoreValue(value),
      ]),
    );

    return json({ report });
  },
};
