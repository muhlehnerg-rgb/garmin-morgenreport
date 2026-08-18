import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

// worker.js ist bewusst eine einzelne, direkt in Cloudflare einsetzbare ES-Modul-
// Datei. Der Data-URL-Import erlaubt Tests ohne package.json oder Bundler und hält
// damit Deployment-Code und getesteten Code identisch.
const workerSource = await readFile(new URL("./worker.js", import.meta.url), "utf8");
const { default: worker } = await import(
  `data:text/javascript;base64,${Buffer.from(workerSource).toString("base64")}`
);

const env = {
  ACTION_API_KEY: "test-action-key",
  TRACKER_SECRET: "test-tracker-key",
  GITHUB_ACTIONS_TOKEN_V2: "test-github-key",
};
const authHeaders = {
  authorization: `Bearer ${env.ACTION_API_KEY}`,
  "content-type": "application/json",
};

async function responseJson(response) {
  return JSON.parse(await response.text());
}

test("unbekannte Routen bleiben gesperrt", async () => {
  const response = await worker.fetch(
    new Request("https://worker.example/beliebig", { headers: authHeaders }),
    env,
  );
  assert.equal(response.status, 404);
});

test("alle bekannten Routen verlangen den Action-Bearer", async () => {
  const requests = [
    new Request("https://worker.example/morgenreport"),
    new Request("https://worker.example/schlafhistorie?tage=28"),
    new Request("https://worker.example/langzeitvergleich?ebene=month"),
    new Request("https://worker.example/morgenreport/status?run_id=1"),
    new Request("https://worker.example/aktivitaeten/heute"),
    new Request("https://worker.example/morgenreport/start", {
      method: "POST",
      body: JSON.stringify({ confirmed: true }),
    }),
    new Request("https://worker.example/aktivitaeten/heute/start", {
      method: "POST",
      body: JSON.stringify({ confirmed: true }),
    }),
  ];
  for (const request of requests) {
    const response = await worker.fetch(request, env);
    assert.equal(response.status, 401);
  }
});

test("Workflow-Start verlangt ausdrueckliche Bestaetigung", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error("GitHub darf ohne Bestaetigung nicht aufgerufen werden");
  };
  try {
    const response = await worker.fetch(
      new Request("https://worker.example/morgenreport/start", {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ confirmed: false }),
      }),
      env,
    );
    assert.equal(response.status, 400);
    assert.deepEqual(await responseJson(response), {
      error: "Explicit confirmation required",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("bestaetigter Start ruft nur den festen Morgenreport-Workflow auf", async () => {
  const originalFetch = globalThis.fetch;
  let capturedUrl;
  let capturedOptions;
  globalThis.fetch = async (url, options) => {
    capturedUrl = String(url);
    capturedOptions = options;
    return Response.json({
      workflow_run_id: 12345,
      html_url: "https://github.com/example/actions/runs/12345",
    });
  };
  try {
    const response = await worker.fetch(
      new Request("https://worker.example/morgenreport/start", {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ confirmed: true }),
      }),
      env,
    );
    assert.equal(response.status, 202);
    assert.match(capturedUrl, /garmin-morgenreport\/actions\/workflows\/morgenreport\.yml\/dispatches$/);
    assert.equal(capturedOptions.method, "POST");
    assert.equal(capturedOptions.headers.authorization, "Bearer test-github-key");
    assert.deepEqual(JSON.parse(capturedOptions.body), {
      ref: "main",
      inputs: { dry_run: false, activities_only: false },
      return_run_details: true,
    });
    assert.deepEqual(await responseJson(response), {
      status: "started",
      run_id: 12345,
      run_url: "https://github.com/example/actions/runs/12345",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("204 ohne Laufdetails startet nicht erneut und markiert fehlende Verfolgung", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, { status: 204 });
  try {
    const response = await worker.fetch(
      new Request("https://worker.example/morgenreport/start", {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ confirmed: true }),
      }),
      env,
    );
    assert.equal(response.status, 202);
    assert.deepEqual(await responseJson(response), {
      status: "started_without_tracking",
      run_id: null,
      run_url: "https://github.com/muhlehnerg-rgb/garmin-morgenreport/actions/workflows/morgenreport.yml",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("bestaetigter Abendstart aktiviert nur den Aktivitaetsmodus", async () => {
  const originalFetch = globalThis.fetch;
  let capturedOptions;
  globalThis.fetch = async (_url, options) => {
    capturedOptions = options;
    return Response.json({ workflow_run_id: 67890 });
  };
  try {
    const response = await worker.fetch(
      new Request("https://worker.example/aktivitaeten/heute/start", {
        method: "POST",
        headers: authHeaders,
        body: JSON.stringify({ confirmed: true }),
      }),
      env,
    );
    assert.equal(response.status, 202);
    assert.deepEqual(JSON.parse(capturedOptions.body), {
      ref: "main",
      inputs: { dry_run: false, activities_only: true },
      return_run_details: true,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Statusroute akzeptiert nur numerische GitHub-Lauf-IDs", async () => {
  const response = await worker.fetch(
    new Request("https://worker.example/morgenreport/status?run_id=abc", {
      headers: authHeaders,
    }),
    env,
  );
  assert.equal(response.status, 400);
});

test("Statusroute gibt nur benoetigte Laufdaten zurueck", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({
    status: "completed",
    conclusion: "success",
    html_url: "https://github.com/example/actions/runs/12345",
    created_at: "2026-07-13T10:00:00Z",
    updated_at: "2026-07-13T10:00:16Z",
    sensitive_internal_value: "wird nicht weitergegeben",
  });
  try {
    const response = await worker.fetch(
      new Request("https://worker.example/morgenreport/status?run_id=12345", {
        headers: authHeaders,
      }),
      env,
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await responseJson(response), {
      run_id: 12345,
      status: "completed",
      conclusion: "success",
      run_url: "https://github.com/example/actions/runs/12345",
      created_at: "2026-07-13T10:00:00Z",
      updated_at: "2026-07-13T10:00:16Z",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Leseroute decodiert weiterhin den Firestore-Bericht", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({
    fields: {
      datum: { stringValue: "2026-07-13" },
      score: { integerValue: "68" },
      hrv: { doubleValue: 59.5 },
      spo2: { nullValue: null },
      aktivitaeten_gestern: {
        arrayValue: {
          values: [{
            mapValue: {
              fields: {
                name: { stringValue: "Morgenlauf" },
                typ: { stringValue: "running" },
                dauer_min: { integerValue: "42" },
                distanz_km: { doubleValue: 7.5 },
                hoehenmeter: { nullValue: null },
              },
            },
          }],
        },
      },
      schlafhistorie_28_tage: {
        arrayValue: { values: [{ mapValue: { fields: {
          datum: { stringValue: "2026-07-12" },
          schlafdauer_h: { doubleValue: 7.1 },
        } } }] },
      },
    },
  });
  try {
    const response = await worker.fetch(
      new Request("https://worker.example/morgenreport", { headers: authHeaders }),
      env,
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await responseJson(response), {
      report: {
        datum: "2026-07-13",
        score: 68,
        hrv: 59.5,
        spo2: null,
        aktivitaeten_gestern: [{
          name: "Morgenlauf",
          typ: "running",
          dauer_min: 42,
          distanz_km: 7.5,
          hoehenmeter: null,
        }],
      },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Schlafhistorie wird sortiert, begrenzt und getrennt ausgegeben", async () => {
  const originalFetch = globalThis.fetch;
  const values = Array.from({ length: 9 }, (_, index) => ({
    mapValue: { fields: {
      datum: { stringValue: `2026-07-${String(9 - index).padStart(2, "0")}` },
      schlafdauer_h: index === 2 ? { nullValue: null } : { doubleValue: 7 + index / 10 },
      schlaf_score: { integerValue: String(80 + index) },
      aktivitaeten_gestern: { arrayValue: { values: [] } },
    } },
  }));
  globalThis.fetch = async () => Response.json({
    fields: { schlafhistorie_28_tage: { arrayValue: { values } } },
  });
  try {
    const response = await worker.fetch(
      new Request("https://worker.example/schlafhistorie?tage=7", { headers: authHeaders }),
      env,
    );
    assert.equal(response.status, 200);
    const body = await responseJson(response);
    assert.equal(body.tage_angefragt, 7);
    assert.equal(body.tage_gefunden, 7);
    assert.equal(body.von, "2026-07-03");
    assert.equal(body.bis, "2026-07-09");
    assert.equal(body.daten.length, 7);
    assert.equal(body.daten[0].datum, "2026-07-03");
    assert.equal(body.daten[4].schlafdauer_h, null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Schlafhistorie lehnt ungueltige Tagesanzahl vor Firestore ab", async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    return Response.json({});
  };
  try {
    const response = await worker.fetch(
      new Request("https://worker.example/schlafhistorie?tage=29", { headers: authHeaders }),
      env,
    );
    assert.equal(response.status, 400);
    assert.equal(fetchCalled, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Langzeitvergleich filtert Zeitraum und Kennzahlen", async () => {
  const originalFetch = globalThis.fetch;
  const period = (key, from, to, sleep, hrv) => ({ mapValue: { fields: {
    periode: { stringValue: key },
    ebene: { stringValue: "month" },
    von: { stringValue: from },
    bis: { stringValue: to },
    tage_gefunden: { integerValue: "28" },
    mittelwerte: { mapValue: { fields: {
      schlafdauer_h: { doubleValue: sleep },
      hrv: { doubleValue: hrv },
      stress_avg: { doubleValue: 30 },
    } } },
    summen: { mapValue: { fields: { schritte: { integerValue: "300000" } } } },
    minima: { mapValue: { fields: { schlafdauer_h: { doubleValue: 4.8 } } } },
    maxima: { mapValue: { fields: { schlafdauer_h: { doubleValue: 8.6 } } } },
    werte_verfuegbar: { mapValue: { fields: {
      schlafdauer_h: { integerValue: "28" },
      hrv: { integerValue: "27" },
    } } },
    aktivitaetstypen: { mapValue: { fields: { running: { integerValue: "4" } } } },
  } } });
  globalThis.fetch = async () => Response.json({ fields: {
    langzeit_metadaten: { mapValue: { fields: {
      von: { stringValue: "2025-12-01" },
      bis: { stringValue: "2026-02-28" },
      tage_gefunden: { integerValue: "90" },
    } } },
    langzeit_monate: { arrayValue: { values: [
      period("2025-12", "2025-12-01", "2025-12-31", 6.8, 44),
      period("2026-01", "2026-01-01", "2026-01-31", 7.1, 48),
      period("2026-02", "2026-02-01", "2026-02-28", 7.3, 51),
    ] } },
  } });
  try {
    const response = await worker.fetch(new Request(
      "https://worker.example/langzeitvergleich?ebene=month&von=2026-01-01&bis=2026-12-31&kennzahlen=schlafdauer_h,hrv",
      { headers: authHeaders },
    ), env);
    assert.equal(response.status, 200);
    const body = await responseJson(response);
    assert.equal(body.perioden_gefunden, 2);
    assert.equal(body.von, "2026-01-01");
    assert.deepEqual(body.kennzahlen, ["schlafdauer_h", "hrv"]);
    assert.deepEqual(body.daten[0].mittelwerte, { schlafdauer_h: 7.1, hrv: 48 });
    assert.deepEqual(body.daten[0].summen, {});
    assert.deepEqual(body.daten[0].werte_verfuegbar, { schlafdauer_h: 28, hrv: 27 });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Langzeitvergleich lehnt unbekannte Kennzahlen vor Firestore ab", async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    return Response.json({});
  };
  try {
    const response = await worker.fetch(new Request(
      "https://worker.example/langzeitvergleich?kennzahlen=geheimwert",
      { headers: authHeaders },
    ), env);
    assert.equal(response.status, 400);
    assert.equal(fetchCalled, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Abend-Leseroute gibt nur heutige Aktivitaeten und Aktualisierungszeit aus", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({
    fields: {
      datum: { stringValue: "2026-07-21" },
      score: { integerValue: "72" },
      aktivitaeten_heute_datum: { stringValue: "2026-07-21" },
      aktivitaeten_heute_aktualisiert_am: {
        stringValue: "2026-07-21T20:15:00+02:00",
      },
      aktivitaeten_heute: {
        arrayValue: { values: [{ mapValue: { fields: {
          name: { stringValue: "Abendlauf" },
          typ: { stringValue: "running" },
          distanz_km: { doubleValue: 5.2 },
        } } }] },
      },
    },
  });
  try {
    const response = await worker.fetch(
      new Request("https://worker.example/aktivitaeten/heute", { headers: authHeaders }),
      env,
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await responseJson(response), {
      datum: "2026-07-21",
      aktualisiert_am: "2026-07-21T20:15:00+02:00",
      aktivitaeten: [{ name: "Abendlauf", typ: "running", distanz_km: 5.2 }],
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});
