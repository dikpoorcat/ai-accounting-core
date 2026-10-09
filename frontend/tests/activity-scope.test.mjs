import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

let sequence = 0;
async function harness(component, suppliedProps, exported) {
  const key = `activityScope${++sequence}`, calls = [], events = [], cleanup = [];
  const route = Vue.reactive({ query: { company_id: "company-a" } });
  const props = Vue.reactive(suppliedProps);
  globalThis[key] = { Vue, props, route, calls, events, cleanup, appendDashboardCollection };
  const source = readFileSync(new URL(`../src/components/${component}.vue`, import.meta.url), "utf8")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const prefix = `const env = globalThis.${key}; const { ref, shallowRef, shallowReactive, computed, watch } = env.Vue;
    const { appendDashboardCollection } = env; const useRoute = () => env.route;
    const defineProps = () => env.props; const withDefaults = (value, defaults) => { for (const [key, fallback] of Object.entries(defaults)) if (value[key] === undefined) value[key] = fallback; return value; };
    const defineEmits = () => (...args) => env.events.push(args); const onBeforeUnmount = fn => env.cleanup.push(fn);
    const fetchBusinessStatus = (...args) => new Promise((resolve, reject) => env.calls.push({ args, resolve, reject }));
    const fetchBriefGroup = fetchBusinessStatus; const dashboardErrorMessage = error => error.message;
    const isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';`;
  const { outputText } = ts.transpileModule(prefix + source + `\nexport { ${exported} };`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const view = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  return { ...view, props, route, calls, events, unmount: () => { cleanup.forEach(fn => fn()); delete globalThis[key]; } };
}
const tick = async () => { await Vue.nextTick(); await Promise.resolve(); };
const page = (cursor = null) => ({ total_count: cursor ? 2 : 1, filtered_count: cursor ? 2 : 1, returned_count: 1, has_more: Boolean(cursor), next_cursor: cursor });
const member = { key: "part-key", group_key: "batch-group", subject_id: "payment", voucher_version_id: "voucher-a", detail_scope_category: "payroll" };
const groupResult = () => ({ data: { section: "activity", group_key: "batch-group", collections: { members: { items: [member], page: page() }, vouchers: { items: [], page: page() } } } });
const statusResult = () => ({ snapshot_version: "snapshot", data: { detail_scope: { category: "payroll", voucher_version_id: "voucher-a", amount_fen: "100", amount_label: "实际付款" }, collections: { settlement_events: { items: [{ id: "first" }], page: page("next") } } } });

test("batch member details become selected only after explicit expansion and accepted member read", async () => {
  for (const isBatch of [true, false]) {
    const view = await harness("brief/BriefGroupMembers", { section: "activity", groupKey: "batch-group", period: "2026-09", snapshotVersion: "snapshot", refreshGeneration: 0, expanded: false, isBatch }, "data, selected");
    try {
      assert.equal(view.calls.length, 0); assert.equal(view.selected.value, "");
      view.props.expanded = true; await tick(); assert.equal(view.calls.length, 1);
      assert.equal(view.selected.value, ""); view.calls[0].resolve(groupResult()); await tick();
      assert.equal(view.selected.value, isBatch ? "part-key" : "");
      view.props.expanded = false; await tick(); assert.equal(view.selected.value, "");
      view.props.expanded = true; await tick(); assert.equal(view.calls.length, 1);
      assert.equal(view.selected.value, isBatch ? "part-key" : "");
    } finally { view.unmount(); }
  }
});

test("late batch member responses after collapse, refresh or group changes never select details", async () => {
  for (const field of ["expanded", "refreshGeneration", "groupKey"]) {
    const view = await harness("brief/BriefGroupMembers", { section: "activity", groupKey: "batch-group", period: "2026-09", snapshotVersion: "snapshot", refreshGeneration: 0, expanded: true, isBatch: true }, "data, selected");
    try {
      if (field === "expanded") view.props.expanded = false;
      if (field === "refreshGeneration") view.props.refreshGeneration++;
      if (field === "groupKey") view.props.groupKey = "another-group";
      await tick(); assert.equal(view.calls[0].args[5].aborted, true);
      view.calls[0].resolve(groupResult()); await tick();
      assert.equal(view.data.value, null); assert.equal(view.selected.value, "");
    } finally { view.unmount(); }
  }
});

test("scoped detail initial and subsequent reads carry exact category, voucher and view", async () => {
  const view = await harness("BusinessStatusDetails", { subjectId: "payment", period: "2026-09", snapshotVersion: "snapshot", settlementView: "historical", presentation: "brief", expanded: false, activityContext: { ...member }, refreshGeneration: 0 }, "data, loadMore");
  try {
    assert.equal(view.calls.length, 0); view.props.expanded = true; await tick();
    for (const request of [view.calls[0]]) assert.deepEqual(request.args[3], { expected_version: "snapshot", settlement_view: "historical", detail_scope_category: "payroll", voucher_version_id: "voucher-a", limit: 20 });
    view.calls[0].resolve(statusResult()); await tick();
    const pending = view.loadMore(); assert.equal(view.calls[1].args[3].detail_scope_category, "payroll"); assert.equal(view.calls[1].args[3].voucher_version_id, "voucher-a");
    view.props.expanded = false; await tick(); assert.equal(view.calls[1].args[2].aborted, true);
    view.calls[1].resolve(statusResult()); await pending; assert.equal(view.data.value.collections.settlement_events.items.length, 1);
  } finally { view.unmount(); }
});

test("scoped first responses cannot write back after collapse, category, voucher or refresh changes", async () => {
  for (const field of ["expanded", "detail_scope_category", "voucher_version_id", "refreshGeneration"]) {
    const view = await harness("BusinessStatusDetails", { subjectId: "payment", period: "2026-09", snapshotVersion: "snapshot", settlementView: "current", presentation: "brief", expanded: true, activityContext: { ...member }, refreshGeneration: 0 }, "data");
    try {
      if (field === "expanded") view.props.expanded = false;
      else if (field === "refreshGeneration") view.props.refreshGeneration++;
      else view.props.activityContext[field] = "changed";
      await tick(); assert.equal(view.calls[0].args[2].aborted, true);
      view.calls[0].resolve(statusResult()); await tick(); assert.equal(view.data.value, null);
      assert.equal(view.calls.length, 1);
    } finally { view.unmount(); }
  }
});
