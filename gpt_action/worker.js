const FIRESTORE_BASE =
  "https://firestore.googleapis.com/v1/projects/gewohnheitstracker-3b30a/databases/(default)/documents";

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function decodeFirestoreValue(value) {
  if ("stringValue" in value) return value.stringValue;
  if ("integerValue" in value) return Number(value.integerValue);
  if ("doubleValue" in value) return value.doubleValue;
  if ("booleanValue" in value) return value.booleanValue;
  if ("nullValue" in value) return null;
  return null;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method !== "GET" || url.pathname !== "/morgenreport") {
      return json({ error: "Not found" }, 404);
    }

    const expected = `Bearer ${env.ACTION_API_KEY}`;
    if (!env.ACTION_API_KEY || request.headers.get("authorization") !== expected) {
      return json({ error: "Unauthorized" }, 401);
    }

    if (!env.TRACKER_SECRET) {
      return json({ error: "Server configuration incomplete" }, 500);
    }

    const documentUrl =
      `${FIRESTORE_BASE}/tracker/morgenreport_${encodeURIComponent(env.TRACKER_SECRET)}`;
    const firestoreResponse = await fetch(documentUrl, {
      headers: { accept: "application/json" },
    });

    if (!firestoreResponse.ok) {
      return json({ error: "Morgenreport could not be loaded" }, 502);
    }

    const document = await firestoreResponse.json();
    const report = Object.fromEntries(
      Object.entries(document.fields || {}).map(([key, value]) => [
        key,
        decodeFirestoreValue(value),
      ]),
    );

    return json({ report });
  },
};
