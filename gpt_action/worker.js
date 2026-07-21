/*
 * HTTPS-Zwischenschicht für die Action des persönlichen Fitnesscoach-GPT.
 *
 * Warum es diesen Worker gibt:
 * ChatGPT Actions können eine normale HTTPS-API mit einem API-Schlüssel aufrufen,
 * sollen aber weder den internen Firestore-Dokumentnamen noch TRACKER_SECRET
 * kennen. Der Worker hält diese Details serverseitig geheim und gibt nur genau
 * wenige fest definierte Endpunkte frei. Garmin- und Telegram-Zugangsdaten werden
 * hier weder benötigt noch gespeichert. Der GitHub-Schlüssel wird ausschließlich
 * als verschlüsseltes Cloudflare-Secret verwendet, um genau einen Workflow zu
 * starten beziehungsweise dessen Status zu lesen.
 *
 * Datenfluss:
 * Lesen:  morgenreport.py -> Firestore -> dieser Worker -> Fitnesscoach-GPT
 * Starten: Fitnesscoach-GPT -> dieser Worker -> GitHub Actions -> morgenreport.py
 * Abends: Fitnesscoach-GPT -> Aktivitätslauf -> Garmin -> Firestore -> GPT
 *
 * ACTION_API_KEY, TRACKER_SECRET und GITHUB_ACTIONS_TOKEN_V2 müssen in Cloudflare
 * als verschlüsselte Secrets angelegt werden. Sie dürfen niemals direkt in
 * dieser Datei, im OpenAPI-Schema oder in Git committed werden.
 */

// Der Projektname ist keine Zugangsinformation. Der geheime Teil des
// Dokumentpfads wird erst weiter unten aus env.TRACKER_SECRET zusammengesetzt.
// Dieses Projekt akzeptiert in der REST-URL die Datenbank-ID `default` ohne
// Klammern; `(default)` liefert hier nachweislich HTTP 404.
const FIRESTORE_BASE =
  "https://firestore.googleapis.com/v1/projects/gewohnheitstracker-3b30a/databases/default/documents";

// Diese Werte sind keine Geheimnisse: Repository und Workflow sind öffentlich
// sichtbar. Die feste Konfiguration verhindert, dass ein GPT über freie Parameter
// beliebige Repositories oder andere Workflows auslösen kann.
const GITHUB_API_BASE =
  "https://api.github.com/repos/muhlehnerg-rgb/garmin-morgenreport";
const GITHUB_WORKFLOW = "morgenreport.yml";
const GITHUB_API_VERSION = "2026-03-10";

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
 * Arrays und Maps werden für die Liste der gestrigen Aktivitäten rekursiv
 * decodiert. Unbekannte Typen werden bewusst zu null, damit keine rohe
 * Firestore-Interna ausgegeben wird.
 */
function decodeFirestoreValue(value) {
  if ("stringValue" in value) return value.stringValue;
  if ("integerValue" in value) return Number(value.integerValue);
  if ("doubleValue" in value) return value.doubleValue;
  if ("booleanValue" in value) return value.booleanValue;
  if ("nullValue" in value) return null;
  if ("arrayValue" in value) {
    return (value.arrayValue.values || []).map(decodeFirestoreValue);
  }
  if ("mapValue" in value) {
    return Object.fromEntries(
      Object.entries(value.mapValue.fields || {}).map(([key, nestedValue]) => [
        key,
        decodeFirestoreValue(nestedValue),
      ]),
    );
  }
  return null;
}

/** Prüft den gemeinsamen Bearer-Schlüssel aller GPT-Action-Endpunkte. */
function isAuthorized(request, env) {
  const expected = `Bearer ${env.ACTION_API_KEY}`;
  return Boolean(
    env.ACTION_API_KEY && request.headers.get("authorization") === expected,
  );
}

/**
 * Erstellt die Header für GitHubs versionierte REST-API.
 * Der Fine-grained Token erhält ausschließlich Zugriff auf dieses Repository
 * und die Berechtigung "Actions: read and write".
 */
function githubHeaders(env) {
  return {
    accept: "application/vnd.github+json",
    authorization: `Bearer ${env.GITHUB_ACTIONS_TOKEN_V2}`,
    "content-type": "application/json",
    "user-agent": "garmin-morgenreport-gpt-worker",
    "x-github-api-version": GITHUB_API_VERSION,
  };
}

/** Liest und decodiert den aktuellsten Report aus dem festen Firestore-Dokument. */
async function loadFirestoreDocument(env) {
  if (!env.TRACKER_SECRET) {
    return { error: json({ error: "Server configuration incomplete" }, 500) };
  }

  const documentUrl =
    `${FIRESTORE_BASE}/tracker/morgenreport_${encodeURIComponent(env.TRACKER_SECRET)}`;
  const firestoreResponse = await fetch(documentUrl, {
    headers: { accept: "application/json" },
  });

  if (!firestoreResponse.ok) {
    return { error: json({ error: "Morgenreport could not be loaded" }, 502) };
  }

  const document = await firestoreResponse.json();
  const report = Object.fromEntries(
    Object.entries(document.fields || {}).map(([key, value]) => [
      key,
      decodeFirestoreValue(value),
    ]),
  );
  return { report };
}

/** Liest und decodiert den aktuellsten vollständigen Morgenreport. */
async function loadMorgenreport(env) {
  const result = await loadFirestoreDocument(env);
  if (result.error) return result.error;
  return json({ report: result.report });
}

/**
 * Gibt nur die separat am Abend aktualisierten Aktivitäten zurück.
 * Andere Gesundheitsfelder werden an dieser Route absichtlich nicht ausgegeben,
 * damit der GPT klar zwischen Morgenreport und Tagesaktivitäten unterscheiden kann.
 */
async function loadHeutigeAktivitaeten(env) {
  const result = await loadFirestoreDocument(env);
  if (result.error) return result.error;
  return json({
    datum: result.report.aktivitaeten_heute_datum ?? null,
    aktualisiert_am: result.report.aktivitaeten_heute_aktualisiert_am ?? null,
    aktivitaeten: result.report.aktivitaeten_heute ?? [],
  });
}

/**
 * Startet den bestehenden workflow_dispatch auf dem main-Branch.
 * `confirmed: true` ist eine zusätzliche technische Hürde. Die GPT-Anweisungen
 * müssen außerdem verlangen, dass der Benutzer den Start ausdrücklich anfordert.
 * Der vollständige Lauf kann Nachrichten versenden; beide Modi verbrauchen
 * GitHub-Actions-Ressourcen.
 */
async function startWorkflow(request, env, activitiesOnly) {
  if (!env.GITHUB_ACTIONS_TOKEN_V2) {
    return json({ error: "Server configuration incomplete" }, 500);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "Invalid JSON body" }, 400);
  }
  if (body?.confirmed !== true) {
    return json({ error: "Explicit confirmation required" }, 400);
  }

  const dispatchResponse = await fetch(
    `${GITHUB_API_BASE}/actions/workflows/${GITHUB_WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: githubHeaders(env),
      body: JSON.stringify({
        ref: "main",
        inputs: { dry_run: false, activities_only: activitiesOnly },
      }),
    },
  );

  if (!dispatchResponse.ok) {
    return json({ error: "Garmin workflow could not be started" }, 502);
  }

  // Aktuelle GitHub-API-Versionen liefern Lauf-ID und URL zurück. Die leere
  // 204-Antwort älterer Versionen bleibt kompatibel, damit ein API-Rollback den
  // eigentlichen Start nicht fälschlich als Fehler meldet.
  let dispatch = {};
  if (dispatchResponse.status !== 204) {
    try {
      dispatch = await dispatchResponse.json();
    } catch {
      dispatch = {};
    }
  }

  return json(
    {
      status: "started",
      run_id: dispatch.workflow_run_id ?? dispatch.id ?? null,
      run_url: dispatch.html_url ?? null,
    },
    202,
  );
}

/** Startet nach ausdrücklicher Bestätigung den vollständigen Morgenreport. */
async function startMorgenreport(request, env) {
  return startWorkflow(request, env, false);
}

/**
 * Startet nach ausdrücklicher Bestätigung nur die Aktualisierung der heutigen
 * Aktivitäten. Dieser Modus sendet weder Telegram noch E-Mail.
 */
async function startHeutigeAktivitaeten(request, env) {
  return startWorkflow(request, env, true);
}

/** Liefert ausschließlich den Status eines zuvor gestarteten Workflow-Laufs. */
async function getMorgenreportStatus(url, env) {
  if (!env.GITHUB_ACTIONS_TOKEN_V2) {
    return json({ error: "Server configuration incomplete" }, 500);
  }

  const runId = url.searchParams.get("run_id") || "";
  if (!/^\d+$/.test(runId)) {
    return json({ error: "Valid run_id required" }, 400);
  }

  const runResponse = await fetch(`${GITHUB_API_BASE}/actions/runs/${runId}`, {
    headers: githubHeaders(env),
  });
  if (!runResponse.ok) {
    return json({ error: "Morgenreport workflow status could not be loaded" }, 502);
  }

  const run = await runResponse.json();
  return json({
    run_id: Number(runId),
    status: run.status ?? null,
    conclusion: run.conclusion ?? null,
    run_url: run.html_url ?? null,
    created_at: run.created_at ?? null,
    updated_at: run.updated_at ?? null,
  });
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

    // Nur die fünf dokumentierten Kombinationen aus Methode und Pfad zulassen.
    // Freie Repository-, Workflow-, Firestore- oder Datumsparameter existieren
    // absichtlich nicht.
    const isReadRoute = request.method === "GET" && url.pathname === "/morgenreport";
    const isStartRoute =
      request.method === "POST" && url.pathname === "/morgenreport/start";
    const isStatusRoute =
      request.method === "GET" && url.pathname === "/morgenreport/status";
    const isTodayActivitiesReadRoute =
      request.method === "GET" && url.pathname === "/aktivitaeten/heute";
    const isTodayActivitiesStartRoute =
      request.method === "POST" && url.pathname === "/aktivitaeten/heute/start";
    if (
      !isReadRoute && !isStartRoute && !isStatusRoute &&
      !isTodayActivitiesReadRoute && !isTodayActivitiesStartRoute
    ) {
      return json({ error: "Not found" }, 404);
    }

    // Im GPT-Editor wird ACTION_API_KEY als Authentifizierung vom Typ "Bearer"
    // hinterlegt. ChatGPT sendet dadurch `Authorization: Bearer <Schlüssel>`.
    // Fehlt das serverseitige Secret oder stimmt der Header nicht exakt überein,
    // werden keinerlei Gesundheitsdaten und keine Konfigurationsdetails geliefert.
    if (!isAuthorized(request, env)) {
      return json({ error: "Unauthorized" }, 401);
    }

    if (isReadRoute) return loadMorgenreport(env);
    if (isStartRoute) return startMorgenreport(request, env);
    if (isTodayActivitiesReadRoute) return loadHeutigeAktivitaeten(env);
    if (isTodayActivitiesStartRoute) return startHeutigeAktivitaeten(request, env);
    return getMorgenreportStatus(url, env);
  },
};
