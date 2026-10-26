import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";
import { createServer } from "vite";
import { validateReviewMaterialsResponse } from "../src/api/generated/reviewMaterials.js";
import { validateReviewMaterialsQuery } from "../src/api/generated/reviewMaterialsQuery.js";
import { validateSourceContentQuery } from "../src/api/generated/sourceContentQuery.js";
import { validateSourceTextContent } from "../src/api/generated/sourceTextContent.js";
import { validateSourceFileReceipt } from "../src/api/generated/sourceFileReceipt.js";

const examples = JSON.parse(readFileSync(new URL("../../docs/design/examples/review-materials-cases.json", import.meta.url), "utf8"));
const samples = examples.examples;
const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
  optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
const api = await server.ssrLoadModule("/src/api/reviewMaterials.ts");
after(() => server.close());

test("review browser types and validators use the unchanged source contract examples", () => {
  for (const [validate, value] of [[validateReviewMaterialsResponse, samples.response],
    [validateReviewMaterialsQuery, samples.current_query], [validateReviewMaterialsQuery, samples.frozen_query],
    [validateSourceContentQuery, samples.content_query], [validateSourceTextContent, samples.statement_content],
    [validateSourceFileReceipt, samples.file_receipt]]) assert.equal(validate(value), true, JSON.stringify(validate.errors));
  const validators = { ReviewMaterialsQuery: validateReviewMaterialsQuery, SourceContentQuery: validateSourceContentQuery };
  for (const item of examples.negative_cases) assert.equal(validators[item.definition](item.value), false, item.id);
  for (const change of [value => value.sources[0].available = 1, value => value.sources[0].extra = "no",
    value => value.sources[0].available = false, value => value.page.returned_count = true]) {
    const value = structuredClone(samples.response); change(value);
    assert.equal(validateReviewMaterialsResponse(value), false);
  }
});

test("deep links require exact explicit identities and paired results, never company fallback", () => {
  const selected = new URLSearchParams(samples.current_query);
  assert.deepEqual(api.parseMaterialsQuery(selected), samples.current_query);
  for (const bad of ["", `company_id=${samples.current_query.company_id}`, `${selected}&company_id=duplicate`,
    `${selected}&path=../../other`, `${selected}&result_id=${samples.frozen_query.result_id}`,
    `${selected}&generation=1e0`, `${selected}&generation=9007199254740993`, `${selected}&limit=0x10`]) {
    assert.throws(() => api.parseMaterialsQuery(new URLSearchParams(bad)), error => error.code === "invalid_command");
  }
});

test("list requests preserve selection and validate full page identity and continuation", async () => {
  const oldFetch = globalThis.fetch;
  try {
    let value = structuredClone(samples.response);
    globalThis.fetch = async (path, options) => {
      const url = new URL(path, "http://synthetic.invalid");
      assert.equal(url.pathname, "/api/review/materials");
      for (const [key, expected] of Object.entries(samples.frozen_query)) assert.equal(url.searchParams.get(key), String(expected));
      assert.equal(options.credentials, "same-origin"); assert.equal(options.cache, "no-store"); assert.equal(options.redirect, "error");
      return new Response(JSON.stringify(value));
    };
    const signal = new AbortController().signal;
    await api.fetchMaterials(samples.frozen_query, signal);
    value.selection = Object.fromEntries(Object.entries(value.selection).reverse());
    await api.fetchMaterials(samples.frozen_query, signal, samples.response);
    for (const change of [item => item.company_id = "10000000-0000-4000-8000-000000000002",
      item => item.business_id = "50000000-0000-4000-8000-000000000002", item => item.selection.generation++,
      item => item.page.collection_version = "other", item => item.page.returned_count++,
      item => item.page.has_more = true, item => item.sources.reverse()]) {
      value = structuredClone(samples.response); change(value);
      await assert.rejects(api.fetchMaterials(samples.frozen_query, signal), error => error.code === "REVIEW_RESPONSE_MISMATCH");
    }
    for (const change of [item => item.database_id = "20000000-0000-4000-8000-000000000002",
      item => item.snapshot_version = item.page.collection_version = "other", item => item.page.total_count++]) {
      value = structuredClone(samples.response); change(value);
      await assert.rejects(api.fetchMaterials(samples.frozen_query, signal, samples.response), error => error.code === "REVIEW_RESPONSE_MISMATCH");
    }
    value = structuredClone(samples.response);
    value.sources = Array.from({ length: 20 }, (_, index) => ({ ...value.sources[0], source_id: `40000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}` }));
    Object.assign(value.page, { total_count: 25, filtered_count: 20, returned_count: 20 });
    await assert.rejects(api.fetchMaterials(samples.frozen_query, signal), error => error.code === "REVIEW_RESPONSE_MISMATCH");
  } finally { globalThis.fetch = oldFetch; }
});

test("statement content binds company/database/source and actual UTF8 bytes", async () => {
  const oldFetch = globalThis.fetch;
  try {
    let value = structuredClone(samples.statement_content);
    globalThis.fetch = async path => {
      const url = new URL(path, "http://synthetic.invalid");
      assert.equal(url.pathname, `/api/review/sources/${samples.content_query.source_id}/content`);
      assert.equal(url.searchParams.get("expected_version"), samples.response.snapshot_version);
      assert.equal(url.searchParams.get("result_id"), samples.frozen_query.result_id);
      return new Response(JSON.stringify(value));
    };
    const read = () => api.fetchSourceText(samples.frozen_query, samples.response, samples.response.sources[0], new AbortController().signal);
    assert.equal(await read(), samples.statement_content.text);
    for (const change of [item => item.database_id = "20000000-0000-4000-8000-000000000002",
      item => item.source_id = samples.file_receipt.source_id, item => item.text += "x"]) {
      value = structuredClone(samples.statement_content); change(value);
      await assert.rejects(read(), error => error.code === "REVIEW_RESPONSE_MISMATCH");
    }
  } finally { globalThis.fetch = oldFetch; }
});

test("file responses bind the real receipt and bytes before a download is offered", async () => {
  const oldFetch = globalThis.fetch;
  try {
    const receipt = samples.file_receipt, row = samples.response.sources[1];
    const original = {
      "Content-Type": "application/octet-stream", "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(receipt.file_name)}`,
      "Content-Length": String(receipt.byte_size), "X-Content-Type-Options": "nosniff",
      "X-Accounting-Company-Id": receipt.company_id, "X-Accounting-Database-Id": receipt.database_id,
      "X-Accounting-Source-Id": receipt.source_id, "X-Content-SHA256": receipt.sha256,
    };
    let headers = original, raw = "synthetic-file-v1\n";
    globalThis.fetch = async (_path, options) => {
      assert.equal(options.credentials, "same-origin"); assert.equal(options.redirect, "error");
      return new Response(raw, { headers });
    };
    const read = () => api.fetchSourceFile(samples.frozen_query, samples.response, row, new AbortController().signal);
    assert.equal((await read()).receipt.file_name, receipt.file_name);
    for (const [name, value] of [["Content-Disposition", "inline; filename=unsafe.html"], ["Content-Type", "text/html"],
      ["X-Accounting-Source-Id", samples.content_query.source_id], ["X-Content-SHA256", "0".repeat(64)],
      ["X-Accounting-Database-Id", "20000000-0000-4000-8000-000000000002"], ["Content-Length", "01"], ["X-Content-Type-Options", ""]]) {
      headers = { ...original, [name]: value };
      await assert.rejects(read(), error => error.code === "REVIEW_RESPONSE_MISMATCH");
    }
    headers = original; raw += "extra";
    await assert.rejects(read(), error => error.code === "REVIEW_RESPONSE_MISMATCH");
    raw = "synthetic-file-v2\n";
    await assert.rejects(read(), error => error.code === "REVIEW_RESPONSE_MISMATCH");
    globalThis.fetch = async () => new Response(JSON.stringify({ code: "dashboard_snapshot_changed", message: "资料已变化" }), { status: 409 });
    await assert.rejects(read(), error => error.code === "dashboard_snapshot_changed");
  } finally { globalThis.fetch = oldFetch; }
});

test("contract generation includes sources without importing source validators on default pages", () => {
  for (const file of ["BriefView.vue", "FundsView.vue", "EmployeesView.vue", "AssetsView.vue", "ReportsView.vue"]) {
    assert.doesNotMatch(readFileSync(new URL(`../src/views/${file}`, import.meta.url), "utf8"), /reviewMaterials|\/api\/review/);
  }
});
