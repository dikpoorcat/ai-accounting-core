import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const terminal = state => ({
  schema_version: 2, company_id: "company-a", database_id: "database-a", period: "2026-01",
  state, preview_digest: null, close_digest: null,
  reason: state === "covered" ? "no_exact_period_manifest" : "preview_missing",
  covered_by: state === "covered" ? { period: "2026-02", digest: "covering-close" } : null,
  owner_review: null,
});
async function browserHarness(run) {
  const previousWindow = globalThis.window, previousFetch = globalThis.fetch;
  globalThis.window = { location: { origin: "http://dashboard.invalid", search: "?company_id=company-a" }, dispatchEvent() {} };
  const queued = [];
  globalThis.fetch = (url, options) => new Promise(resolve => queued.push({ url, options, resolve }));
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try {
    const api = await server.ssrLoadModule("/src/api/closeReview.ts");
    await run(api, queued);
  } finally {
    await server.close(); globalThis.fetch = previousFetch;
    if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow;
  }
}

test("a fixed owner review accepts terminal transitions without following another preview", async () => {
  await browserHarness(async ({ fetchCloseReview }, queued) => {
    const fixed = fetchCloseReview("company-a", "2026-01", undefined, { previewDigest: "fixed-preview" });
    const latest = fetchCloseReview("company-a", "2026-01");
    assert.equal(queued.length, 2);
    assert.match(queued[0].url, /preview_digest=fixed-preview/);
    assert.doesNotMatch(queued[0].url, /section=|cursor=|limit=/);
    queued[1].resolve(new Response(JSON.stringify(terminal("covered")), { status: 200 }));
    assert.equal((await latest).state, "covered");
    queued[0].resolve(new Response(JSON.stringify(terminal("unprepared")), { status: 200 }));
    assert.equal((await fixed).owner_review, null);
  });
});
test("owner review keeps company, period and exact digest request binding", async () => {
  await browserHarness(async ({ fetchCloseReview }, queued) => {
    for (const changes of [
      { company_id: "company-b" }, { period: "2026-02" },
      { state: "prepared", preview_digest: "different-preview" },
    ]) {
      const request = fetchCloseReview("company-a", "2026-01", undefined, { previewDigest: "fixed-preview" });
      queued.at(-1).resolve(new Response(JSON.stringify({ ...terminal("prepared"), ...changes }), { status: 200 }));
      await assert.rejects(request, error => error.code === "DASHBOARD_SCHEMA_MISMATCH");
    }
    const invalidated = fetchCloseReview("company-a", "2026-01", undefined, { previewDigest: "fixed-preview" });
    queued.at(-1).resolve(new Response(JSON.stringify({ ...terminal("stale"), preview_digest: "fixed-preview" }), { status: 200 }));
    assert.equal((await invalidated).state, "stale");
  });
});
