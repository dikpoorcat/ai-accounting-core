import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ts from "typescript";
import * as Vue from "vue";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";
import { groupContributionMembers, contributionProgressRows } from "./helpers/briefOpenItemGrouping.mjs";

let sequence = 0;
async function harness(section = "activity") {
  const source = readFileSync(new URL("../src/components/brief/BriefGroupMembers.vue", import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const environment = { Vue, props: Vue.reactive({ section, groupKey: "group-a", expanded: false, period: "2026-09", snapshotVersion: "v1", refreshGeneration: 0 }), route: Vue.reactive({ query: { company_id: "a" } }), requests: [], events: [], unmount: [] };
  const key = `briefGroup${++sequence}`; globalThis[key] = environment;
  const { outputText } = ts.transpileModule(`const env = globalThis.${key}; export function instantiate() {
    const { computed, ref, shallowReactive, shallowRef, watch } = env.Vue;
    const defineProps = () => env.props, defineEmits = () => (...args) => env.events.push(args), useRoute = () => env.route;
    const onBeforeUnmount = fn => env.unmount.push(fn);
    const fetchBriefGroup = (...args) => new Promise((resolve, reject) => env.requests.push({ args, resolve, reject }));
    const dashboardErrorMessage = error => error.message, isDashboardSnapshotChanged = () => false;
    const appendDashboardCollection = env.appendDashboardCollection;
    const { groupContributionMembers, contributionProgressRows } = env;
    ${source}
    return { data, loading, error, selected, preview, expandedPart, loadMore, cancel, activityMembers, openMembers, contributions, ungroupedOpenMembers, toggle, togglePart };
  }`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  environment.appendDashboardCollection = appendDashboardCollection;
  environment.groupContributionMembers = groupContributionMembers;
  environment.contributionProgressRows = contributionProgressRows;
  const module = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`); delete globalThis[key];
  const scope = Vue.effectScope(); const state = scope.run(() => module.instantiate());
  return { ...environment, ...state, close() { environment.unmount.forEach(fn => fn()); scope.stop(); } };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
function response(section, keys, more = false) {
  return { data: { section, group_key: "group-a", collections: { members: { items: keys.map(key => section === "activity" ? { key, group_key: "group-a", subject_id: `s-${key}` } : { id: key, group_key: "group-a", subject_id: `s-${key}` }), page: { has_more: more, next_cursor: more ? "next" : null } }, vouchers: { items: keys.map(key => ({ voucher_version_id: `v-${key}` })), page: { has_more: false } } } } };
}
test("groups read only on expansion and append bounded members without opening per-member progress", async () => {
  for (const section of ["activity", "open_items"]) {
    const state = await harness(section);
    try {
      assert.equal(state.requests.length, 0);
      state.props.expanded = true; await tick(); assert.equal(state.requests.length, 1);
      assert.deepEqual(state.requests[0].args.slice(0, 5), ["a", "2026-09", section, "group-a", "v1"]);
      state.requests[0].resolve(response(section, ["one"], true)); await tick();
      const items = state.data.value.collections.members.items;
      assert.equal(state.selected.value, ""); assert.deepEqual(state.events.map(event => event[0]), ["vouchers"]);
      state.toggle("one"); assert.equal(state.selected.value, "one");
      const next = state.loadMore(); assert.equal(state.requests[1].args[6].cursor, "next");
      state.requests[1].resolve(response(section, ["two"])); await next;
      assert.equal(state.data.value.collections.members.items, items); assert.equal(items.length, 2);
      assert.equal(state.selected.value, "one", "member continuation preserves the explicitly selected progress");
      state.toggle("two"); assert.equal(state.selected.value, "two", "only the requested member is selected");
      state.toggle("two"); assert.equal(state.selected.value, "");
      state.toggle("one");
      state.props.expanded = false; await tick(); assert.equal(state.selected.value, "");
      state.props.expanded = true; await tick();
      assert.equal(state.selected.value, ""); assert.equal(state.requests.length, 2, "cached reopening does not reread members");
      state.toggle("one");
      state.props.refreshGeneration++; await tick();
      assert.equal(state.selected.value, "", "refresh clears the prior member detail selection");
    } finally { state.close(); }
  }
});
test("collapse cancels members and rejects late responses; reopen can retry", async () => {
  const state = await harness();
  try {
    state.props.expanded = true; await tick(); const old = state.requests[0];
    state.props.expanded = false; await tick(); assert.equal(old.args[5].aborted, true);
    old.resolve(response("activity", ["late"])); await tick(); assert.equal(state.data.value, null); assert.equal(state.events.length, 0);
    state.props.expanded = true; await tick(); assert.equal(state.requests.length, 2);
    state.requests[1].reject(new Error("读取失败")); await tick(); assert.equal(state.error.value, "读取失败");
    const retry = state.loadMore(); state.requests[2].resolve(response("activity", ["success"])); await retry;
    assert.equal(state.error.value, ""); assert.equal(state.data.value.collections.members.items[0].key, "success");
  } finally { state.close(); }
});
test("company and snapshot changes reject old scope replies", async () => {
  const state = await harness();
  try {
    state.props.expanded = true; await tick(); const old = state.requests[0];
    state.route.query.company_id = "b"; await tick(); assert.equal(old.args[5].aborted, true);
    old.resolve(response("activity", ["old"])); await tick(); assert.equal(state.data.value, null);
    const second = state.requests[1]; state.props.snapshotVersion = "v2"; await tick();
    assert.equal(second.args[5].aborted, true); assert.equal(state.requests.at(-1).args[4], "v2");
    second.resolve(response("activity", ["old-snapshot"])); await tick(); assert.equal(state.events.length, 0);
  } finally { state.close(); }
});

const contribution = (id, changes = {}) => ({ id, group_key: "group-a", category_key: "payroll_payables", subject_id: `s-${id}`, contribution_group_key: "employee-a", payroll_period: "2026-09", contribution_component: "employee_social", status: "open", current_status: "open", source_amount_fen: "10000", paid_fen: "0", other_settled_fen: "0", outstanding_fen: "10000", current_outstanding_fen: "10000", ...changes });
function openResponse(items, more = false) {
  const result = response("open_items", [], more);
  result.data.collections.members.items = items;
  return result;
}

test("contribution members stay visible through incomplete pages and retries, then move into their full breakdown", async () => {
  const state = await harness("open_items");
  try {
    state.props.openSummary = { cutoff_period: "2026-09", current_cutoff_period: "2026-10" };
    state.props.expanded = true; await tick();
    const first = contribution("one"), second = contribution("two");
    const ordinary = [contribution("no-employee", { contribution_group_key: null }), contribution("no-month", { payroll_period: null }), contribution("no-part", { contribution_component: null })];
    state.requests[0].resolve(openResponse([first, ...ordinary], true)); await tick();
    assert.deepEqual(state.contributions.value, []);
    assert.deepEqual(state.ungroupedOpenMembers.value.map(item => item.id), ["one", ...ordinary.map(item => item.id)]);
    const next = state.loadMore(); state.requests[1].reject(new Error("续页失败")); await next;
    assert.equal(state.error.value, "续页失败");
    assert.equal(state.ungroupedOpenMembers.value.length, 4, "failed continuation never hides the loaded records");
    const retry = state.loadMore(); state.requests[2].resolve(openResponse([second])); await retry;
    assert.equal(state.error.value, "");
    assert.equal(state.contributions.value.length, 1);
    const part = state.contributions.value[0].parts[0];
    assert.deepEqual(part.members.map(item => item.id), ["one", "two"]);
    assert.equal(part.outstandingFen, "20000");
    assert.deepEqual(state.ungroupedOpenMembers.value.map(item => item.id), ordinary.map(item => item.id));
    assert.equal(state.selected.value, ""); assert.equal(state.expandedPart.value, "");
    assert.equal(state.requests.length, 3, "preparing contribution rows only reads the member pages");
    assert.deepEqual(state.events.map(event => event[0]), ["vouchers", "vouchers"]);
  } finally { state.close(); }
});

test("contribution record expansion is local and clears on collapse or every scope change", async () => {
  const state = await harness("open_items");
  try {
    state.props.openSummary = { cutoff_period: "2026-09", current_cutoff_period: "2026-10" };
    state.props.expanded = true; await tick();
    state.requests[0].resolve(openResponse([contribution("one"), contribution("two")])); await tick();
    const partKey = `${state.contributions.value[0].id}:employee_social`;
    state.selected.value = "one"; state.preview.value = "one"; state.togglePart(partKey);
    assert.equal(state.expandedPart.value, partKey); assert.equal(state.selected.value, ""); assert.equal(state.preview.value, "");
    state.togglePart(partKey); assert.equal(state.expandedPart.value, "");
    state.togglePart(partKey); state.selected.value = "two";
    state.props.expanded = false; await tick();
    assert.equal(state.expandedPart.value, ""); assert.equal(state.selected.value, "");
    state.props.expanded = true; await tick();
    assert.equal(state.requests.length, 1, "local record expansion and reopening completed members do not request progress");
    for (const change of [() => state.props.refreshGeneration++, () => state.props.period = "2026-10", () => state.props.snapshotVersion = "v2", () => state.props.groupKey = "group-b", () => state.route.query.company_id = "b"]) {
      state.expandedPart.value = partKey; state.selected.value = "one"; state.preview.value = "one";
      change(); await tick();
      assert.equal(state.expandedPart.value, ""); assert.equal(state.selected.value, ""); assert.equal(state.preview.value, "");
      assert.equal(state.data.value, null);
    }
    assert.deepEqual(state.events.map(event => event[0]), ["vouchers"]);
  } finally { state.close(); }
});
