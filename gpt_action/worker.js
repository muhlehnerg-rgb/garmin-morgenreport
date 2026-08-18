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
const GITHUB_WORKFLOW_URL =
  "https://github.com/muhlehnerg-rgb/garmin-morgenreport/actions/workflows/morgenreport.yml";
const GITHUB_API_VERSION = "2026-03-10";

const LONG_TERM_FIELDS = {
  week: "langzeit_wochen",
  month: "langzeit_monate",
  quarter: "langzeit_quartale",
  year: "langzeit_jahre",
};
const LONG_TERM_LIMITS = { week: 104, month: 120, quarter: 80, year: 30 };
const LONG_TERM_METRICS = new Set([
  "score", "body_battery", "hrv", "ruhepuls", "schlafdauer_h",
  "schlaf_score", "tief_min", "rem_min", "leicht_min", "wach_min",
  "stress_avg", "schritte", "spo2", "atemfrequenz", "tr_score", "vo2max",
  "kalorien_gesamt", "kalorien_aktiv", "distanz_km", "puls_min", "puls_max",
  "body_battery_min", "body_battery_max", "body_battery_geladen",
  "body_battery_verbraucht", "aktiv_min", "hochaktiv_min", "sitzend_min",
  "intensitaet_mod_min", "intensitaet_vig_min", "stockwerke_auf",
  "stockwerke_ab", "fluessigkeit_ml", "gewicht_kg", "bmi",
  "koerperfett_pct", "fitnessalter", "ausdauer_score",
  "aktivitaeten_anzahl", "aktivitaeten_dauer_min",
  "aktivitaeten_distanz_km", "aktivitaeten_kalorien",
]);
const DEFAULT_LONG_TERM_METRICS = [
  "schlafdauer_h", "schlaf_score", "hrv", "ruhepuls", "body_battery_max",
  "stress_avg", "schritte", "intensitaet_mod_min", "intensitaet_vig_min",
  "vo2max", "aktivitaeten_anzahl", "aktivitaeten_dauer_min",
];

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
  const {
    schlafhistorie_28_tage: _historie,
    langzeit_metadaten: _langzeitMeta,
    langzeit_wochen: _langzeitWochen,
    langzeit_monate: _langzeitMonate,
    langzeit_quartale: _langzeitQuartale,
    langzeit_jahre: _langzeitJahre,
    ...report
  } = result.report;
  return json({ report });
}

/** Liefert einen begrenzten Ausschnitt der rollierenden Schlafhistorie. */
async function loadSchlafhistorie(url, env) {
  const rawDays = url.searchParams.get("tage");
  const days = rawDays === null ? 28 : Number(rawDays);
  if (!Number.isInteger(days) || days < 7 || days > 28) {
    return json({ error: "tage must be an integer between 7 and 28" }, 400);
  }

  const result = await loadFirestoreDocument(env);
  if (result.error) return result.error;
  const stored = Array.isArray(result.report.schlafhistorie_28_tage)
    ? result.report.schlafhistorie_28_tage
    : [];
  const data = stored
    .filter((entry) => entry && typeof entry.datum === "string")
    .sort((a, b) => a.datum.localeCompare(b.datum))
    .slice(-days);

  return json({
    tage_angefragt: days,
    tage_gefunden: data.length,
    von: data[0]?.datum ?? null,
    bis: data.at(-1)?.datum ?? null,
    daten: data,
  });
}

function validIsoDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
}

function selectMetrics(source, metrics) {
  if (!source || typeof source !== "object" || Array.isArray(source)) return {};
  return Object.fromEntries(
    metrics.filter((metric) => Object.hasOwn(source, metric))
      .map((metric) => [metric, source[metric]]),
  );
}

function compactLongTermPeriod(period, metrics) {
  return {
    periode: period.periode ?? null,
    von: period.von ?? null,
    bis: period.bis ?? null,
    tage_gefunden: period.tage_gefunden ?? 0,
    mittelwerte: selectMetrics(period.mittelwerte, metrics),
    summen: selectMetrics(period.summen, metrics),
    minima: selectMetrics(period.minima, metrics),
    maxima: selectMetrics(period.maxima, metrics),
    werte_verfuegbar: selectMetrics(period.werte_verfuegbar, metrics),
    aktivitaetstypen: period.aktivitaetstypen ?? {},
    kategorien: period.kategorien ?? {},
  };
}

/** Returns bounded long-term aggregates without exposing private daily records. */
async function loadLangzeitvergleich(url, env) {
  const level = url.searchParams.get("ebene") || "month";
  if (!Object.hasOwn(LONG_TERM_FIELDS, level)) {
    return json({ error: "ebene must be week, month, quarter or year" }, 400);
  }
  const from = url.searchParams.get("von");
  const to = url.searchParams.get("bis");
  if ((from && !validIsoDate(from)) || (to && !validIsoDate(to)) || (from && to && from > to)) {
    return json({ error: "von and bis must be valid YYYY-MM-DD values" }, 400);
  }

  const rawMetrics = url.searchParams.get("kennzahlen");
  const metrics = rawMetrics
    ? [...new Set(rawMetrics.split(",").map((item) => item.trim()).filter(Boolean))]
    : DEFAULT_LONG_TERM_METRICS;
  if (metrics.length < 1 || metrics.length > 12 || metrics.some((item) => !LONG_TERM_METRICS.has(item))) {
    return json({ error: "kennzahlen must contain 1 to 12 supported values" }, 400);
  }

  const result = await loadFirestoreDocument(env);
  if (result.error) return result.error;
  const stored = Array.isArray(result.report[LONG_TERM_FIELDS[level]])
    ? result.report[LONG_TERM_FIELDS[level]]
    : [];
  const periods = stored
    .filter((period) => period && typeof period.von === "string" && typeof period.bis === "string")
    .filter((period) => (!from || period.bis >= from) && (!to || period.von <= to))
    .sort((a, b) => a.von.localeCompare(b.von));

  if (periods.length > LONG_TERM_LIMITS[level]) {
    return json({ error: "Requested range is too large; narrow von and bis" }, 400);
  }
  return json({
    ebene: level,
    von: periods[0]?.von ?? null,
    bis: periods.at(-1)?.bis ?? null,
    perioden_gefunden: periods.length,
    kennzahlen: metrics,
    datenbasis: result.report.langzeit_metadaten ?? null,
    daten: periods.map((period) => compactLongTermPeriod(period, metrics)),
  });
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
        return_run_details: true,
      }),
    },
  );

  if (!dispatchResponse.ok) {
    return json({ error: "Garmin workflow could not be started" }, 502);
  }

  // Laufdetails werden ausdrücklich angefordert. Falls GitHub trotzdem nur 204
  // liefert, darf der GPT nicht mit einer ungültigen Statusabfrage in eine
  // erneute Start-/Freigabeschleife geraten.
  let dispatch = {};
  if (dispatchResponse.status !== 204) {
    try {
      dispatch = await dispatchResponse.json();
    } catch {
      dispatch = {};
    }
  }

  const runId = dispatch.workflow_run_id ?? dispatch.id ?? null;
  return json(
    {
      status: runId === null ? "started_without_tracking" : "started",
      run_id: runId,
      run_url: dispatch.html_url ?? GITHUB_WORKFLOW_URL,
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

    // Nur die dokumentierten Kombinationen aus Methode und Pfad zulassen.
    // Freie Repository-, Workflow-, Firestore- oder Datumsparameter existieren
    // absichtlich nicht.
    const isReadRoute = request.method === "GET" && url.pathname === "/morgenreport";
    const isSleepHistoryReadRoute =
      request.method === "GET" && url.pathname === "/schlafhistorie";
    const isLongTermReadRoute =
      request.method === "GET" && url.pathname === "/langzeitvergleich";
    const isStartRoute =
      request.method === "POST" && url.pathname === "/morgenreport/start";
    const isStatusRoute =
      request.method === "GET" && url.pathname === "/morgenreport/status";
    const isTodayActivitiesReadRoute =
      request.method === "GET" && url.pathname === "/aktivitaeten/heute";
    const isTodayActivitiesStartRoute =
      request.method === "POST" && url.pathname === "/aktivitaeten/heute/start";
    if (
      !isReadRoute && !isSleepHistoryReadRoute && !isLongTermReadRoute && !isStartRoute && !isStatusRoute &&
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
    if (isSleepHistoryReadRoute) return loadSchlafhistorie(url, env);
    if (isLongTermReadRoute) return loadLangzeitvergleich(url, env);
    if (isStartRoute) return startMorgenreport(request, env);
    if (isTodayActivitiesReadRoute) return loadHeutigeAktivitaeten(env);
    if (isTodayActivitiesStartRoute) return startHeutigeAktivitaeten(request, env);
    return getMorgenreportStatus(url, env);
  },
};
