import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { createServer } from "vite";

const terminal = state => ({
  schema_version: 1,
  company_id: "company-a",
  database_id: "database-a",
  period: "2026-01",
  state,
  preview_digest: null,
  close_digest: null,
  reason: state === "covered" ? "no_exact_period_manifest" : "preview_missing",
  covered_by: state === "covered" ? { period: "2026-02", digest: "covering-close" } : null,
  owner_review: null,
  collection: null,
});

test("a fixed detail request accepts explicit unprepared and covered transitions without following a new preview", async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  globalThis.window = {
    location: { origin: "http://dashboard.invalid", search: "?company_id=company-a" },
    dispatchEvent() {},
  };
  const queued = [];
  globalThis.fetch = (url, options) => new Promise(resolve => queued.push({ url, options, resolve }));
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try {
    const { fetchCloseReview } = await server.ssrLoadModule("/src/api/closeReview.ts");
    const staleDetail = fetchCloseReview("company-a", "2026-01", undefined, {
      previewDigest: "fixed-preview", section: "evidence", cursor: "fixed-cursor",
    });
    const latestSummary = fetchCloseReview("company-a", "2026-01");
    assert.equal(queued.length, 2);
    assert.match(queued[0].url, /preview_digest=fixed-preview/);
    assert.match(queued[0].url, /section=evidence/);
    queued[1].resolve(new Response(JSON.stringify(terminal("covered")), { status: 200 }));
    assert.equal((await latestSummary).state, "covered");
    queued[0].resolve(new Response(JSON.stringify(terminal("unprepared")), { status: 200 }));
    const ended = await staleDetail;
    assert.equal(ended.state, "unprepared");
    assert.equal(ended.owner_review, null);
    assert.equal(ended.collection, null);
  } finally {
    await server.close();
    globalThis.fetch = previousFetch;
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test("a prepared-to-closed detail response replaces the main binding and discards preview-bound pages", async () => {
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try {
    const { closeReviewBinding, mergeCloseReviewSection } = await server.ssrLoadModule("/src/api/closeReview.ts");
    const page = { total_count: 1, returned_count: 1, has_more: false, next_cursor: null };
    const prepared = { state: "prepared", preview_digest: "preview-a", close_digest: null };
    const oldEvidence = { section: "evidence", items: [{ key: "preview-item" }], page };
    const oldPolicies = { section: "policies", items: [{ key: "preview-policy" }], page };
    const closedEvidence = { section: "evidence", items: [{ key: "closed-item" }], page };
    const closed = { state: "closed", preview_digest: "preview-a", close_digest: "close-a", collection: closedEvidence };

    assert.equal(closeReviewBinding(prepared), "preview-a");
    assert.equal(closeReviewBinding(closed), "close-a");
    const transitioned = mergeCloseReviewSection(prepared, { evidence: oldEvidence, policies: oldPolicies }, closed, "evidence", true);
    assert.equal(transitioned.bindingChanged, true);
    assert.deepEqual(transitioned.collections.evidence.items.map(item => item.key), ["closed-item"]);
    assert.equal(transitioned.collections.policies, undefined);

    const nextClosed = { ...closed, collection: { ...closedEvidence, items: [{ key: "closed-next" }] } };
    const continued = mergeCloseReviewSection(closed, transitioned.collections, nextClosed, "evidence", true);
    assert.equal(continued.bindingChanged, false);
    assert.deepEqual(continued.collections.evidence.items.map(item => item.key), ["closed-item", "closed-next"]);
  } finally { await server.close(); }
});
