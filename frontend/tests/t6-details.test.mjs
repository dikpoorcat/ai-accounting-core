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
    const withDefaults = (value, defaults) => { for (const [key, fallback] of Object.entries(defaults)) if (value[key] === undefined) value[key] = fallback; return value; };
    const defineEmits = () => (...args) => environment.events.push(args);
    const fetchBusinessStatus = environment.fetch, fetchAssetsDashboard = environment.fetch, fetchEmployeesDashboard = environment.fetch;
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
function statusResult(cursor = "settlement-next", id = "settlement-first") {
  return { snapshot_version: "business-v1", data: { marker: "original-summary", collections: { settlement_events: collection(id, cursor) } } };
}
const changedError = () => Object.assign(new Error("snapshot changed"), { code: "dashboard_snapshot_changed" });
async function businessHarness() {
  const instance = await harness("BusinessStatusDetails", "load, loadMore, data, responseVersion, moreError, moreLoading, error, loading");
  const initial = instance.load();
  instance.calls[0].resolve(statusResult()); await initial;
  return instance;
}

test("business settlement continuation appends records under the exact scope and version", async () => {
  const view = await businessHarness();
  const pending = view.loadMore();
  assert.equal(view.moreLoading.value, true);
  assert.equal(view.loading.value, false);
  assert.equal(view.calls[1].args[3].section, "settlement_events");
  assert.equal(view.calls[1].args[3].settlement_view, "historical");
  assert.equal(view.calls[1].args[3].expected_version, "business-v1");
  assert.equal(view.calls[1].args[3].limit, 20);
  view.calls[1].resolve(statusResult(null, "settlement-second")); await pending;
  assert.deepEqual(view.data.value.collections.settlement_events.items.map(item => item.id), ["settlement-first", "settlement-second"]);
  assert.equal(view.data.value.marker, "original-summary");
  assert.deepEqual(view.events, []);
  view.unmount();
});

test("ordinary settlement continuation failure retains its page and retries the same cursor", async () => {
  const view = await businessHarness();
  const pending = view.loadMore();
  view.calls[1].reject(new Error("temporary settlement failure")); await pending;
  assert.equal(view.error.value, "");
  assert.match(view.moreError.value, /temporary/);
  assert.deepEqual(view.data.value.collections.settlement_events.items, [{ id: "settlement-first" }]);
  const retry = view.loadMore();
  assert.equal(view.calls[2].args[3].cursor, view.calls[1].args[3].cursor);
  view.calls[2].resolve(statusResult(null, "settlement-second")); await retry;
  assert.equal(view.moreError.value, "");
  assert.equal(view.data.value.collections.settlement_events.items.length, 2);
  assert.deepEqual(view.events, []);
  view.unmount();
});

test("settlement snapshot changes invalidate all prior detail without silently following another version", async () => {
  const view = await businessHarness();
  const pending = view.loadMore();
  view.calls[1].reject(changedError()); await pending;
  assert.equal(view.data.value, null);
  assert.equal(view.responseVersion.value, "");
  assert.equal(view.calls.length, 2);
  assert.deepEqual(view.events, [["changed"]]);
  view.unmount();
});

test("company changes abort detail continuation and reject late results", async () => {
  const view = await businessHarness();
  const pending = view.loadMore();
  view.route.query.company_id = "company-b";
  assert.equal(view.calls[1].args[2].aborted, true);
  assert.equal(view.data.value, null);
  view.calls[1].resolve(statusResult(null, "late")); await pending;
  assert.equal(view.data.value, null);
  assert.equal(view.moreLoading.value, false);
  view.unmount();
});

test("inline business details read only on expansion and reject a late response after their row scope changes", async () => {
  for (const field of ["subjectId", "period", "snapshotVersion"]) {
    const view = await harness("BusinessStatusDetails", "toggle, data, loading");
    assert.equal(view.calls.length, 0);
    view.toggle({ target: { open: true } });
    assert.equal(view.calls.length, 1);
    assert.equal(view.calls[0].args[0], "2026-09");
    assert.equal(view.calls[0].args[1], "business-a");
    assert.deepEqual(view.calls[0].args[3], { expected_version: "business-v1", settlement_view: "historical", limit: 20 });
    view.props[field] = `${view.props[field]}-changed`;
    assert.equal(view.calls[0].args[2].aborted, true);
    view.calls[0].resolve(statusResult());
    await Promise.resolve(); await Vue.nextTick();
    assert.equal(view.data.value, null);
    assert.equal(view.loading.value, false);
    view.unmount();
  }
});

test("fund and employee rows bind explicit business identity, selected period and snapshot to lazy details", () => {
  for (const [name, subjects, period, version] of [
    ["Funds", ["item.subject_id"], "selectedPeriod", "snapshotVersion"],
    ["Employees", ["labor.subject_id"], "selectedPeriodKey", "response.snapshot_version ?? undefined"],
  ]) {
    const source = readFileSync(new URL(`../src/views/${name}View.vue`, import.meta.url), "utf8");
    const tags = source.match(/<BusinessStatusDetails\b[^>]*\/>/g) ?? [];
    for (const subject of subjects) {
      const matches = tags.filter(tag => tag.includes(`:subject-id="${subject}"`));
      assert.ok(matches.length, `${name}: ${subject}`);
      for (const tag of matches) {
        assert.ok(tag.includes(`:period="${period}"`));
        assert.ok(tag.includes(`:snapshot-version="${version}"`));
        assert.ok(tag.includes('settlement-view="historical"'));
        assert.ok(tag.includes('@changed="refreshChanged"'));
      }
    }
    if (name === "Funds") assert.equal(tags.filter(tag => tag.includes(':subject-id="item.subject_id"')).length, 2);
  }
});

test("owner business contract rejects retired event source and file collections", async () => {
  const { validateDashboardBusinessStatusResponse } = await import("../src/api/generated/dashboardValidators.js");
  const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
  for (const section of ["events", "source_history", "file_jobs"]) {
    const response = structuredClone(samples.business_status.response);
    response.data.collections[section] = collection("private");
    assert.equal(validateDashboardBusinessStatusResponse(response), false, section);
  }
});
