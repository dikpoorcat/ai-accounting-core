import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

let number = 0;
async function assetView(fetchAssetsDashboard, query = { asset_filter: "all" }) {
  const key = `assetFilterHarness${++number}`;
  const route = Vue.reactive({ query: { company_id: "co", period: "2026-03", ...query }, hash: "" });
  const context = Vue.ref({ current_company: { company_id: "co" }, periods: [{ key: "2026-03" }, { key: "2026-04" }], default_period: "2026-03" });
  globalThis[key] = { Vue, route, context, fetchAssetsDashboard, appendDashboardCollection };
  const originalDocument = globalThis.document;
  globalThis.document = { addEventListener() {}, removeEventListener() {}, getElementById: () => null };
  const source = readFileSync(new URL("../src/views/AssetsView.vue", import.meta.url), "utf8").match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const imports = `
    const { computed, nextTick, ref, watch } = globalThis.${key}.Vue;
    const { appendDashboardCollection } = globalThis.${key};
    const onMounted = () => {}; const onBeforeUnmount = () => {};
    const { fetchAssetsDashboard } = globalThis.${key};
    const useRoute = () => globalThis.${key}.route;
    const navigate = async target => { globalThis.${key}.route.query = target.query; globalThis.${key}.route.hash = target.hash ?? ''; };
    const useRouter = () => ({ push: navigate, replace: navigate });
    const useDashboardContext = () => ({ context: globalThis.${key}.context, load: async () => globalThis.${key}.context.value, refresh: async () => { throw new Error('local filter must not refresh context'); } });
    const useDashboardSections = (_items, initial) => ({ activeSection: ref(initial), focusSection() {}, positionSection() {} });
    const dashboardErrorMessage = error => error.message;
    const isDashboardSnapshotChanged = error => error.code === 'dashboard_snapshot_changed';
    const fen = BigInt; const formatFen = String;
  `;
  const exported = "\nexport { response, loading, pageLoading, pageErrors, retryCollection, reloadCollection, loadMore, loadAssets, filter, changePeriod, displayMode, listInitialized, changeDisplayMode }; export function activate() { mounted = true; selectedPeriod.value = '2026-03'; }";
  const { outputText } = ts.transpileModule(imports + source + exported, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const scope = Vue.effectScope();
  const view = await scope.run(() => import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`));
  view.activate();
  return { ...view, route, context, cleanup() { scope.stop(); globalThis.document = originalDocument; delete globalThis[key]; } };
}
const collection = id => ({ items: [{ asset_id: id, project_id: id }], page: { total_count: 30, filtered_count: 30, returned_count: 1, has_more: true, next_cursor: "next" } });
const response = (filter = "all", snapshot = "v1", id = filter) => ({ selected_period: { key: "2026-03" }, snapshot_version: snapshot, data: { asset_filter: filter, asset_id: null, project_id: null, ledger_net_fen: "123456", registered_count: 30, collections: { assets: collection(id), projects: collection("kept-project") } } });

const flush = async () => { await Vue.nextTick(); await Vue.nextTick(); await Vue.nextTick(); };

test("asset filtering defaults to active, accepts explicit all and ignores invalid URL selections", async () => {
  for (const [query, expected] of [[{}, "active"], [{ asset_filter: "all" }, "all"], [{ asset_filter: "pending" }, "pending"], [{ asset_filter: "invalid" }, "active"]]) {
    const calls = [];
    const view = await assetView(async (_period, _signal, query) => { calls.push(query); return response(expected); }, query);
    try {
      assert.equal(view.filter.value, expected);
      await view.loadAssets("2026-03");
      assert.equal(calls.at(-1).asset_filter, expected);
      view.filter.value = "all"; await flush();
      assert.equal(view.route.query.asset_filter, "all", "all stays explicit in the URL");
      view.filter.value = "active"; await flush();
      assert.equal(view.filter.value, "active");
    } finally { view.cleanup(); }
  }
});

test("an exact exited asset bypasses the active default without losing its URL filter", async () => {
  const calls = [];
  const view = await assetView(async (_period, _signal, query) => {
    calls.push(query);
    const result = response("all", "v1", "exited-asset");
    result.data.collections.assets.items[0].status = "disposed";
    return result;
  }, { asset_id: "exited-asset" });
  try {
    await view.loadAssets("2026-03");
    assert.equal(view.filter.value, "active");
    assert.equal(calls[0].asset_filter, "all");
    assert.equal(calls[0].asset_id, "exited-asset");
    assert.equal(view.response.value.data.collections.assets.items[0].status, "disposed");
  } finally { view.cleanup(); }
});

test("asset month navigation retains its selected filter and clears exact targets, cursor and hash", async () => {
  for (const selected of [undefined, "active", "all", "fixed", "intangible", "pending", "exited"]) {
    const view = await assetView(async () => response(), { asset_filter: selected, asset_id: "old-asset", project_id: "old-project", cursor: "old-cursor" });
    try {
      view.route.hash = "#asset-card-target";
      view.changePeriod("2026-04"); await flush();
      assert.equal(view.route.query.period, "2026-04");
      assert.equal(view.route.query.asset_filter, selected);
      assert.equal(view.route.query.asset_id, undefined);
      assert.equal(view.route.query.project_id, undefined);
      assert.equal(view.route.query.cursor, undefined);
      assert.equal(view.route.hash, "");
    } finally { view.cleanup(); }
  }
});

test("asset display switching keeps loaded arrays and a running continuation without another request", async () => {
  const calls = [];
  const view = await assetView((_period, signal, query) => new Promise(resolve => calls.push({ signal, query, resolve })));
  try {
    view.response.value = response();
    const assets = view.response.value.data.collections.assets.items;
    const projects = view.response.value.data.collections.projects.items;
    assert.equal(view.displayMode.value, "cards");
    assert.equal(view.listInitialized.value, false);
    const pending = view.loadMore("assets");
    view.changeDisplayMode("list"); await flush();
    assert.equal(view.listInitialized.value, true);
    view.changeDisplayMode("cards"); await flush();
    assert.equal(view.listInitialized.value, true, "list details remain mounted after first use");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].signal.aborted, false);
    assert.equal(view.pageLoading.value.assets, true);
    assert.equal(view.response.value.data.collections.assets.items, assets);
    assert.equal(view.response.value.data.collections.projects.items, projects);
    calls[0].resolve(response("all", "v1", "continued")); await pending;
    assert.equal(view.response.value.data.collections.assets.items, assets);
    assert.equal(assets.length, 2);
    view.changeDisplayMode("list"); await flush();
    assert.equal(calls.length, 1);
    assert.equal(view.response.value.data.collections.assets.page.next_cursor, "next");
  } finally { view.cleanup(); }
});

test("asset type selection only reloads its snapshot-bound collection and preserves whole-company totals", async () => {
  const calls = [];
  const view = await assetView((period, signal, query) => new Promise(resolve => calls.push({ period, signal, query, resolve })));
  try {
    view.response.value = response();
    view.route.query = { ...view.route.query, asset_filter: "fixed" };
    await flush();
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].query, { section: "assets", asset_filter: "fixed", asset_id: undefined, project_id: undefined, expected_version: "v1" });
    assert.equal(view.loading.value, false);
    assert.equal(view.pageLoading.value.assets, true);
    assert.equal(view.response.value.data.ledger_net_fen, "123456");
    const following = response("fixed"); following.data.ledger_net_fen = "999999";
    calls[0].resolve(following); await flush();
    assert.equal(view.response.value.data.collections.assets.items[0].asset_id, "fixed");
    assert.equal(view.response.value.data.collections.projects.items[0].project_id, "kept-project");
    assert.equal(view.response.value.data.ledger_net_fen, "123456");
    assert.equal(view.pageLoading.value.assets, false);
    view.route.hash = "#asset-list-title"; await flush();
    view.route.query = { ...view.route.query, unrelated_display: "cards" }; await flush();
    assert.equal(calls.length, 1);
  } finally { view.cleanup(); }
});

test("rapid filters abort old selection and a local failure retries a replacement first page", async () => {
  const calls = [];
  const view = await assetView((period, signal, query) => new Promise((resolve, reject) => calls.push({ signal, query, resolve, reject })));
  try {
    view.response.value = response();
    view.route.query.asset_filter = "fixed"; await flush();
    view.route.query.asset_filter = "intangible"; await flush();
    assert.equal(calls[0].signal.aborted, true);
    calls[0].resolve(response("fixed")); await flush();
    assert.equal(view.response.value.data.asset_filter, "all");
    calls[1].reject(new Error("读取失败")); await flush();
    assert.equal(view.loading.value, false);
    assert.equal(view.response.value.data.ledger_net_fen, "123456");
    assert.equal(view.pageErrors.value.assets, "读取失败");
    const retry = view.retryCollection("assets");
    assert.equal(calls[2].query.cursor, undefined);
    calls[2].resolve(response("intangible")); await retry;
    assert.equal(view.response.value.data.collections.assets.items.length, 1);
    assert.equal(view.response.value.data.asset_filter, "intangible");
  } finally { view.cleanup(); }
});

test("company and month changes cancel local filtering and prevent an old response crossing selections", async () => {
  for (const field of ["company_id", "period"]) {
    const calls = [];
    const view = await assetView((period, signal, query) => new Promise(resolve => calls.push({ signal, query, resolve })));
    try {
      view.response.value = response();
      view.route.query.asset_filter = "fixed"; await flush();
      view.route.query[field] = field === "period" ? "2026-04" : "other-company";
      assert.equal(calls[0].signal.aborted, true);
      assert.equal(view.response.value, null);
      calls[0].resolve(response("fixed")); await flush();
      assert.notEqual(view.response.value?.data?.asset_filter, "fixed");
    } finally { view.cleanup(); }
  }
});

test("asset and project continuations retain reactive arrays and reject detached late pages", async () => {
  for (const section of ["assets", "projects"]) {
    const calls = [];
    const view = await assetView((period, signal, query) => new Promise(resolve => calls.push({ signal, query, resolve })));
    try {
      view.response.value = response();
      const original = view.response.value.data.collections[section].items;
      const renderedCount = Vue.computed(() => view.response.value.data.collections[section].items.length);
      assert.equal(renderedCount.value, 1);
      const pending = view.loadMore(section), next = response("all", "v1", "next-asset");
      next.data.collections.projects.items = [{ project_id: "next-project" }];
      next.data.collections[section].page.next_cursor = "following";
      calls.at(-1).resolve(next); await pending;
      assert.equal(view.response.value.data.collections[section].items, original);
      assert.equal(renderedCount.value, 2);
      assert.equal(view.response.value.data.collections[section].page.next_cursor, "following");
      const late = view.loadMore(section), request = calls.at(-1);
      view.route.query.company_id = "other-company";
      assert.equal(request.signal.aborted, true);
      request.resolve(response("all", "v1", "late")); await late;
      assert.equal(original.length, 2);
      assert.equal(view.response.value, null);
    } finally { view.cleanup(); }
  }
});
