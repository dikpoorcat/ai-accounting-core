import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";

let sequence = 0;
async function harness(component, exported, suppliedProps = {}) {
  const key = `t6DetailHarness${++sequence}`;
  const route = Vue.reactive({ query: { company_id: "company-a" } });
  const props = Vue.reactive({ subjectId: "business-a", period: "2026-09", snapshotVersion: "business-v1", settlementView: "historical", ...suppliedProps });
  const calls = [], events = [], cleanup = [];
  globalThis[key] = { Vue, route, props, events, cleanup, fetch: (...args) => new Promise((resolve, reject) => calls.push({ args, resolve, reject })) };
  const source = readFileSync(new URL(`../src/components/${component}.vue`, import.meta.url), "utf8")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
    .replace(/import[\s\S]*?from "[^"]+";/g, "");
  const prefix = `
    const environment = globalThis.${key};
    const { ref, computed, watch } = environment.Vue;
    const onBeforeUnmount = callback => environment.cleanup.push(callback);
    const useRoute = () => environment.route;
    const defineProps = () => environment.props;
    const defineEmits = () => (...args) => environment.events.push(args);
    const fetchBusinessStatus = environment.fetch, fetchAssetsDashboard = environment.fetch, fetchEmployeesDashboard = environment.fetch;
    const fetchLocalTrace = environment.fetch, fetchVoucherTrace = environment.fetch;
    const dashboardErrorMessage = error => error.message, localErrorMessage = error => error.message;
    const isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';
  `;
  const { outputText } = ts.transpileModule(prefix + source + `\nexport { ${exported} };`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  });
  const instance = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  return { ...instance, route, props, calls, events, unmount: () => cleanup.forEach(callback => callback()) };
}

function collection(id, cursor = `${id}-next`, version) {
  return { items: [{ id }], page: { total_count: 3, filtered_count: 3, returned_count: 1, has_more: cursor !== null, next_cursor: cursor, ...(version ? { collection_version: version } : {}) } };
}
function statusResult(collections = {
  events: collection("event-first"), source_history: collection("source-first"), settlement_events: collection("settlement-first"), file_jobs: collection("file-first", "file-cursor-v1", "files-v1"),
}) {
  return { snapshot_version: "business-v1", data: { marker: "original-summary", collections } };
}
const changedError = () => Object.assign(new Error("snapshot changed"), { code: "dashboard_snapshot_changed" });
async function businessHarness() {
  const instance = await harness("BusinessStatusDetails", "load, data, responseVersion, collectionStates, error, loading");
  const initial = instance.load();
  instance.calls[0].resolve(statusResult()); await initial;
  return instance;
}

test("business detail collections continue concurrently and merge into the latest successful data", async () => {
  const view = await businessHarness();
  const events = view.load("events"), sources = view.load("source_history");
  assert.equal(view.calls.length, 3);
  assert.equal(view.collectionStates.value.events.loading, true);
  assert.equal(view.collectionStates.value.source_history.loading, true);
  assert.equal(view.loading.value, false, "a collection must not take over main-read loading");
  view.calls[2].resolve(statusResult({ source_history: collection("source-second", null) })); await sources;
  view.calls[1].resolve(statusResult({ events: collection("event-second", null) })); await events;
  assert.deepEqual(view.data.value.collections.events.items.map(item => item.id), ["event-first", "event-second"]);
  assert.deepEqual(view.data.value.collections.source_history.items.map(item => item.id), ["source-first", "source-second"]);
  assert.equal(view.data.value.marker, "original-summary");
  assert.deepEqual(view.events, []);
  view.unmount();
});

test("ordinary detail continuation failure retains its page and retries the same cursor locally", async () => {
  const view = await businessHarness();
  const pending = view.load("settlement_events");
  view.calls[1].reject(new Error("temporary settlement failure")); await pending;
  assert.equal(view.error.value, "");
  assert.match(view.collectionStates.value.settlement_events.error, /temporary/);
  assert.deepEqual(view.data.value.collections.settlement_events.items, [{ id: "settlement-first" }]);
  const retry = view.load("settlement_events");
  assert.equal(view.calls[2].args[3].cursor, view.calls[1].args[3].cursor);
  assert.equal(view.calls[2].args[3].expected_version, "business-v1");
  view.calls[2].resolve(statusResult({ settlement_events: collection("settlement-second", null) })); await retry;
  assert.equal(view.collectionStates.value.settlement_events.error, "");
  assert.equal(view.data.value.collections.settlement_events.items.length, 2);
  assert.deepEqual(view.events, []);
  view.unmount();
});

test("file collection version change restarts only file_jobs without cursor under the original business version", async () => {
  const view = await businessHarness();
  const pending = view.load("file_jobs");
  assert.equal(view.calls[1].args[3].cursor, "file-cursor-v1");
  view.calls[1].reject(changedError()); await Vue.nextTick();
  assert.equal(view.calls.length, 3);
  assert.equal(view.calls[2].args[3].section, "file_jobs");
  assert.equal(view.calls[2].args[3].cursor, undefined);
  assert.equal(view.calls[2].args[3].expected_version, "business-v1");
  assert.equal(view.collectionStates.value.file_jobs.restart, true, "old file page is unavailable during recovery");
  view.calls[2].resolve(statusResult({ file_jobs: collection("replacement-file", null, "files-v2") })); await pending;
  assert.deepEqual(view.data.value.collections.file_jobs.items, [{ id: "replacement-file" }]);
  assert.equal(view.data.value.collections.file_jobs.page.collection_version, "files-v2");
  assert.deepEqual(view.data.value.collections.events.items, [{ id: "event-first" }]);
  assert.equal(view.responseVersion.value, "business-v1");
  assert.deepEqual(view.events, []);
  view.unmount();
});

test("if the cursor-free file retry also rejects the business version, all detail data is invalidated", async () => {
  const view = await businessHarness();
  const files = view.load("file_jobs"), events = view.load("events");
  view.calls[1].reject(changedError()); await Vue.nextTick();
  assert.equal(view.calls[3].args[3].cursor, undefined);
  assert.equal(view.calls[3].args[3].expected_version, "business-v1");
  view.calls[3].reject(changedError()); await files;
  assert.equal(view.data.value, null);
  assert.equal(view.responseVersion.value, "");
  assert.equal(view.calls[2].args[2].aborted, true);
  assert.deepEqual(view.events, [["changed"]]);
  view.calls[2].resolve(statusResult({ events: collection("late-event", null) })); await events;
  assert.equal(view.data.value, null, "a late sibling cannot revive an invalidated business snapshot");
  view.unmount();
});

test("source history continuation retries its old cursor, and snapshot changes clear accumulated records", async () => {
  const view = await harness("DashboardSourceHistory", "load, collection, retryMore, error", { endpoint: "assets", section: "source_history", entityId: "asset-a" });
  const first = view.load(); view.calls[0].resolve({ data: { collections: { source_history: collection("first-source") } } }); await first;
  const more = view.load(true); view.calls[1].reject(new Error("temporary source failure")); await more;
  assert.equal(view.retryMore.value, true);
  assert.equal(view.collection.value.items.length, 1);
  const retry = view.load(view.retryMore.value);
  assert.equal(view.calls[2].args[2].cursor, "first-source-next");
  view.calls[2].resolve({ data: { collections: { source_history: collection("second-source", "third-cursor") } } }); await retry;
  assert.deepEqual(view.collection.value.items.map(item => item.id), ["first-source", "second-source"]);
  const stale = view.load(true); view.calls[3].reject(changedError()); await stale;
  assert.equal(view.collection.value, null);
  assert.deepEqual(view.events, [["changed"]]);
  view.unmount();
});

test("voucher retry keeps the failed exact target without adding another back-history entry", async () => {
  const view = await harness("brief/VoucherTrace", "load, retry, back, history, error, trace", { calculationId: "original-calculation" });
  const first = view.load(); view.calls[0].resolve({ marker: "original" }); await first;
  const related = view.load({ voucherVersionId: "exact-related-version" });
  view.calls[1].reject(new Error("temporary trace failure")); await related;
  assert.equal(view.history.value.length, 1);
  view.retry();
  assert.equal(view.calls[2].args[1], "exact-related-version");
  assert.equal(view.history.value.length, 1);
  view.calls[2].resolve({ marker: "related" }); await Vue.nextTick();
  assert.equal(view.trace.value.marker, "related");
  view.back();
  assert.equal(view.calls[3].args[1], "original-calculation");
  assert.equal(view.history.value.length, 0);
  view.calls[3].resolve({ marker: "original-again" }); await Vue.nextTick();
  view.unmount();
});
