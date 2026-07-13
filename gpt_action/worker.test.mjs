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
  GITHUB_ACTIONS_TOKEN: "test-github-key",
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
  const response = await worker.fetch(
    new Request("https://worker.example/morgenreport/start", {
      method: "POST",
      body: JSON.stringify({ confirmed: true }),
    }),
    env,
  );
  assert.equal(response.status, 401);
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
      inputs: { dry_run: false },
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
    },
  });
  try {
    const response = await worker.fetch(
      new Request("https://worker.example/morgenreport", { headers: authHeaders }),
      env,
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await responseJson(response), {
      report: { datum: "2026-07-13", score: 68, hrv: 59.5, spo2: null },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});
