import { AUTOMATIC_DASHBOARD_PAGE_LIMIT } from "./helpers/dashboardPageLimit.mjs";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as Vue from "vue";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";
import { briefCategoryIndex } from "./helpers/briefCategoryIndex.mjs";
import { groupContributionMembers, contributionProgressRows } from "./helpers/briefOpenItemGrouping.mjs";
import { rendererNode } from "./helpers/rendererNode.mjs";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const flush = async () => { await Vue.nextTick(); await Vue.nextTick(); };
let sequence = 0;
const page = (more = false) => ({ total_count: 2, filtered_count: 2, returned_count: 1, has_more: more, next_cursor: more ? "next" : null });
function overview(version = "v1") {
  const result = structuredClone(samples.brief.response);
  result.snapshot_version = version;
  result.data.open_items = { ...result.data.open_items, group_count: 1, total_count: 2, payable_count: 2, payable_fen: "100",
    categories: [{ key: "supplier_payables", label: "待付款", direction: "payable", count: 2, group_count: 1, outstanding_fen: "100" }] };
  result.data.collections.open_items = { items: [{ group_key: "open-a", category_key: "supplier_payables", party: "供货方", description: "费用", member_count: 2, status: "open", current_status: "open", outstanding_fen: "100", current_outstanding_fen: "100" }], page: { ...page(), total_count: 1, filtered_count: 1 } };
  return result;
}
function members(key, more = false, section = "open_items") {
  return { data: { section, collections: { members: { items: section === "activity" ? [] : [{ id: key, group_key: "open-a", subject_id: null, description: key, date: null, recognition: { period: "2026-01", label: "按月确认" }, status: "open", outstanding_fen: "100", contribution_group_key: null }], page: page(more) }, vouchers: { items: [] } } } };
}
function renderer() {
  const make = rendererNode;
  const root = make("root");
  const host = Vue.createRenderer({ createElement: make, createText: () => make("text"), createComment: () => make("comment"),
    insert(node, target, anchor) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); const at = anchor ? target.children.indexOf(anchor) : -1; target.children.splice(at < 0 ? target.children.length : at, 0, node); node.parent = target; },
    remove(node) { node.parent?.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
    setText() {}, setElementText(node) { node.children = []; }, parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp(node, key, previous, value) { node.props[key] = value; },
  });
  return { root, host };
}
async function harness() {
  const initial = overview(), env = { states: {}, groups: [], main: [], contexts: [], calls: [] };
  env.route = Vue.reactive({ query: { company_id: initial.read_context.company_id, period: initial.selected_period.key } });
  const context = { current_company: { company_id: env.route.query.company_id }, periods: [{ key: env.route.query.period }] };
  env.context = Vue.ref(context);
  const pending = queue => (...args) => new Promise((resolve, reject) => queue.push({ args, resolve, reject }));
  const modules = {
    vue: { ...Vue, onMounted() {} },
    "vue-router": { useRoute: () => env.route, useRouter: () => ({ push() {}, replace() {} }) },
    useDashboardContext: { useDashboardContext: () => ({ context: env.context, load: async () => context, refresh: pending(env.contexts) }) },
    useDashboardSections: { useDashboardSections: () => ({ activeSection: Vue.ref("overview"), focusSection() {}, focusSelectedPanel() {} }) },
    brief: { fetchDeferredBrief: pending(env.main) }, briefGroup: { fetchBriefGroup: pending(env.calls) },
    client: { dashboardErrorMessage: error => error.message, isDashboardSnapshotChanged: error => error.code === "dashboard_snapshot_changed" },
    dashboardCollections: { appendDashboardCollection }, briefCategoryIndex: { briefCategoryIndex },
    briefOpenItems: { groupContributionMembers, contributionProgressRows, payrollMonthLabel: value => value },
    money: { fen: value => BigInt(value ?? 0), formatFen: value => String(value ?? "待核对") },
    dashboardContracts: { AUTOMATIC_DASHBOARD_PAGE_LIMIT, businessStateLabel: value => value },
  };
  const annotations = {
    BriefView: "{ response, loading, refreshGeneration, refreshCurrent, error }",
    BriefOpenItems: "{ expandedItemId, selectedCategoryKey }",
    BriefActivityWorkbench: "{ expandedBusinessKey, selectedGroup }",
    BriefGroupMembers: "{ data, loading, error, loadMore, props }",
  };
  const files = { BriefView: "views/BriefView.vue", BriefOpenItems: "components/brief/BriefOpenItems.vue", BriefActivityWorkbench: "components/brief/BriefActivityWorkbench.vue", BriefGroupMembers: "components/brief/BriefGroupMembers.vue", DashboardPagination: "components/DashboardPagination.vue" };
  const compiled = {};
  env.capture = (name, state) => { if (name === "BriefGroupMembers") env.groups.push(state); else env.states[name] = state; };
  async function component(name) {
    if (compiled[name]) return compiled[name];
    let source = readFileSync(new URL(`../src/${files[name]}`, import.meta.url), "utf8");
    if (annotations[name]) source = source.replace("</script>", `Probe.capture(${JSON.stringify(name)}, ${annotations[name]});\n</script>`);
    const { descriptor } = parse(source), key = `briefRefresh${++sequence}`, bindings = {};
    let output = ts.transpileModule(compileScript(descriptor, { id: key, inlineTemplate: true }).content, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
    for (const [, , location] of output.matchAll(/import\s+([\s\S]*?)\s+from\s+["']([^"']+)["'];?/g)) {
      const base = location.split("/").at(-1).replace(/\.vue$/, "");
      bindings[location] = modules[location] ?? modules[base] ?? { default: files[base] ? await component(base) : { render: () => null } };
    }
    output = output.replace(/import\s+([\s\S]*?)\s+from\s+["']([^"']+)["'];?/g, (_, names, location) => `const ${names.trim().startsWith("{") ? names.replace(/\bas\b/g, ":") : `{ default: ${names.trim()} }`} = Probe.bindings[${JSON.stringify(location)}];`);
    globalThis[key] = { capture: env.capture, bindings };
    compiled[name] = (await import(`data:text/javascript;base64,${Buffer.from(`const Probe = globalThis.${key};\n${output}\n//# sourceURL=brief-refresh-${name}-${key}.mjs`).toString("base64")}`)).default;
    delete globalThis[key];
    return compiled[name];
  }
  const { host, root } = renderer(), app = host.createApp(await component("BriefView"));
  app.component("RouterLink", { render: () => null }); app.mount(root);
  env.states.BriefView.response.value = initial; await flush();
  return { ...env, context, openGroup() { env.states.BriefOpenItems.expandedItemId.value = "open-a"; },
    openState() { return env.groups.findLast(group => group.props.section === "open_items"); }, close() { app.unmount(); } };
}

test("same-snapshot refresh pauses old members and resumes one correct request with automatic continuation", async () => {
  const h = await harness();
  try {
    h.openGroup(); await flush(); const old = h.calls[0];
    const before = h.states.BriefView.refreshGeneration.value;
    const refresh = h.states.BriefView.refreshCurrent(true); await flush();
    assert.equal(h.states.BriefView.refreshGeneration.value, before + 2);
    assert.equal(old.args[5].aborted, true); assert.equal(h.calls.length, 1);
    await h.openState().loadMore(); assert.equal(h.calls.length, 1, "pending main blocks manual and automatic member reads");
    old.reject(Object.assign(new Error("old snapshot"), { code: "dashboard_snapshot_changed" })); await flush();
    assert.equal(h.main.length, 1); assert.equal(h.contexts.length, 1, "late old failure cannot restart main refresh");
    h.main[0].resolve(overview()); await flush(); assert.equal(h.calls.length, 1, "main still waits for context");
    h.contexts[0].resolve(h.context); await refresh; await flush();
    assert.equal(h.states.BriefOpenItems.expandedItemId.value, "open-a");
    assert.equal(h.calls.length, 2); assert.equal(h.calls[1].args[4], "v1"); assert.equal(h.calls[1].args[6].cursor, undefined);
    h.calls[1].resolve(members("first", true)); await flush();
    assert.equal(h.calls.length, 3); assert.equal(h.calls[2].args[6].cursor, "next", "expanded group still loads every member");
    h.calls[2].resolve(members("second")); await flush();
    assert.deepEqual(h.openState().data.value.collections.members.items.map(item => item.id), ["first", "second"]);
    assert.equal(h.main.length, 1); assert.equal(h.contexts.length, 1);
  } finally { h.close(); }
});

test("changed snapshot keeps original collapse behavior and manual reopening reads only the new version", async () => {
  const h = await harness();
  try {
    h.openGroup(); await flush(); const old = h.calls[0];
    const refresh = h.states.BriefView.refreshCurrent(true); await flush();
    old.resolve(members("late")); await flush(); assert.equal(h.openState().data.value, null);
    h.main[0].resolve(overview("v2")); h.contexts[0].resolve(h.context); await refresh; await flush();
    assert.equal(h.states.BriefOpenItems.expandedItemId.value, ""); assert.equal(h.calls.length, 1);
    h.openGroup(); await flush(); assert.equal(h.calls.length, 2); assert.equal(h.calls[1].args[4], "v2");
    h.calls[1].resolve(members("new")); await flush();
    assert.equal(h.openState().data.value.collections.members.items[0].id, "new"); assert.equal(h.main.length, 1);
  } finally { h.close(); }
});

test("refresh during automatic member continuation cancels the old cursor and resumes one fresh chain", async () => {
  const h = await harness();
  try {
    h.openGroup(); await flush(); h.calls[0].resolve(members("original", true)); await flush();
    assert.equal(h.calls.length, 2); const old = h.calls[1]; assert.equal(old.args[6].cursor, "next");
    const refresh = h.states.BriefView.refreshCurrent(true); await flush();
    assert.equal(old.args[5].aborted, true); assert.equal(h.calls.length, 2);
    old.resolve(members("late")); await flush(); assert.equal(h.openState().data.value, null);
    h.main[0].resolve(overview()); h.contexts[0].resolve(h.context); await refresh; await flush();
    assert.equal(h.calls.length, 3); assert.equal(h.calls[2].args[6].cursor, undefined);
    h.calls[2].resolve(members("refreshed")); await flush();
    assert.deepEqual(h.openState().data.value.collections.members.items.map(item => item.id), ["refreshed"]);
    assert.equal(h.main.length, 1); assert.equal(h.contexts.length, 1);
  } finally { h.close(); }
});

test("activity refresh still closes expanded groups and does not resume their old reads", async () => {
  const h = await harness();
  try {
    const item = overview().data.collections.activity.items[0];
    h.states.BriefActivityWorkbench.selectedGroup.value = item.group; await flush();
    h.states.BriefActivityWorkbench.expandedBusinessKey.value = item.group_key;
    await flush(); const old = h.calls[0];
    const refresh = h.states.BriefView.refreshCurrent(true); await flush();
    assert.equal(h.states.BriefActivityWorkbench.expandedBusinessKey.value, ""); assert.equal(old.args[5].aborted, true);
    h.main[0].resolve(overview()); h.contexts[0].resolve(h.context); await refresh; await flush();
    assert.equal(h.calls.length, 1);
  } finally { h.close(); }
});

test("company change rejects old main and members; failed refresh and member retry remain recoverable", async () => {
  const h = await harness();
  try {
    h.openGroup(); await flush(); const old = h.calls[0];
    const refresh = h.states.BriefView.refreshCurrent(true); await flush();
    h.route.query.company_id = "another-company"; await flush();
    old.resolve(members("late")); h.main[0].resolve(overview()); h.contexts[0].resolve(h.context); await refresh; await flush();
    assert.equal(h.states.BriefView.response.value, null); assert.equal(h.calls.length, 1);
  } finally { h.close(); }
  const retry = await harness();
  try {
    retry.openGroup(); await flush(); retry.calls[0].resolve(members("original")); await flush();
    const failed = retry.states.BriefView.refreshCurrent(true); await flush();
    retry.main[0].reject(new Error("main failed")); retry.contexts[0].resolve(retry.context); await failed; await flush();
    assert.equal(retry.calls.length, 1); assert.equal(retry.states.BriefView.error.value, "main failed");
    const successful = retry.states.BriefView.refreshCurrent(true); await flush();
    retry.main[1].resolve(overview()); retry.contexts[1].resolve(retry.context); await successful; await flush();
    retry.openGroup(); await flush(); assert.equal(retry.calls.length, 2);
    retry.calls[1].reject(new Error("members failed")); await flush(); assert.equal(retry.openState().error.value, "members failed");
    const last = retry.openState().loadMore(); retry.calls[2].resolve(members("retry")); await last; await flush();
    assert.equal(retry.openState().data.value.collections.members.items[0].id, "retry"); assert.equal(retry.main.length, 2);
  } finally { retry.close(); }
});
