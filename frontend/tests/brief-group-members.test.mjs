import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ts from "typescript";
import * as Vue from "vue";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

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
    const groupContributionMembers = () => [];
    ${source}
    return { data, loading, error, selected, loadMore, cancel, activityMembers, openMembers };
  }`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  environment.appendDashboardCollection = appendDashboardCollection;
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
      const next = state.loadMore(); assert.equal(state.requests[1].args[6].cursor, "next");
      state.requests[1].resolve(response(section, ["two"])); await next;
      assert.equal(state.data.value.collections.members.items, items); assert.equal(items.length, 2);
      state.selected.value = section === "activity" ? "one" : "one";
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
