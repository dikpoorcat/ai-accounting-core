import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as Vue from "vue";
import { createServer } from "vite";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const flush = async () => { await Vue.nextTick(); await Vue.nextTick(); };
let sequence = 0;
function renderer(onElement) {
  const make = (tag, text = "") => ({ tag, text, props: {}, style: {}, children: [], parent: null });
  const root = make("root");
  const host = Vue.createRenderer({
    createElement: tag => { onElement?.(tag); return make(tag); }, createText: text => make("text", text), createComment: text => make("comment", text),
    insert(node, parent, anchor) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); const at = anchor ? parent.children.indexOf(anchor) : -1; parent.children.splice(at < 0 ? parent.children.length : at, 0, node); node.parent = parent; },
    remove(node) { node.parent?.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
    setText(node, text) { node.text = text; }, setElementText(node, text) { node.text = text; node.children = []; },
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp(node, key, _old, value) { node.props[key] = value; }, querySelector() { return root; },
  });
  const nodes = () => { const result = []; const visit = node => { result.push(node); node.children.forEach(visit); }; visit(root); return result; };
  return { root, host, nodes };
}

async function mount(server, name, { eager = false, section = "activity", withoutMemo = false, realBriefApi = false } = {}) {
  const env = { states: {}, details: [], detailCalls: [], pageCalls: [], moneyCalls: [], events: [], unmounted: 0 };
  const initial = structuredClone(samples.cash_funds.response);
  env.route = Vue.reactive({ query: { company_id: initial.read_context.company_id, period: initial.selected_period.key }, hash: "" });
  const modules = {
    vue: { ...Vue, onMounted() {} },
    "vue-router": { useRoute: () => env.route, useRouter: () => ({ replace: async () => {}, push: async () => {} }) },
    useDashboardContext: { useDashboardContext: () => {
      const context = { current_company: { company_id: env.route.query.company_id }, periods: [{ key: env.route.query.period }] };
      return { context: Vue.ref(context), loading: Vue.ref(false), error: Vue.ref(""), load: async () => {}, refresh: async () => realBriefApi ? context : undefined };
    } },
    useDashboardSections: { useDashboardSections: () => ({ activeSection: Vue.ref("bank-details"), focusSection() {}, focusSelectedPanel() {}, positionSection() {}, lockSectionSync() {} }) },
  };
  const paths = { BriefView: "views/BriefView.vue", FundsView: "views/FundsView.vue", BriefGroupMembers: "components/brief/BriefGroupMembers.vue", BusinessStatusDetails: "components/BusinessStatusDetails.vue", BriefActivityWorkbench: "components/brief/BriefActivityWorkbench.vue" };
  const annotations = { BriefView: "{ response, error, loadData, vouchersReady, loadAllVouchers, loadMore, activeSection, pausePages, paginationScope, sectionLoading, sectionErrors, invalidateRequests, refreshCurrent, voucherPreviewIndex }", FundsView: "{ funds, snapshotVersion, selectedPeriod, selectedDetailView, selectedAccount, selectedBankAccount, activeSection, expandedMovementId, openedMovementIds, invalidateRequests, loadMore, loadFunds }", BriefGroupMembers: "{ data, props, selected, openedDetails, expandedPart, contributions, toggle, togglePart, loadMore }", BusinessStatusDetails: "{ data, props, loadMore }", BriefActivityWorkbench: "{ mode, voucherDisplayMode, selectedVoucher, progressVoucher, selectVoucher, changeVoucherPage, selectVoucherMode, toggleVoucherDisplayMode, retryVouchers, closeProgress }" };
  const cache = {};
  async function component(componentName) {
    if (cache[componentName]) return cache[componentName];
    const path = paths[componentName], key = `lazyDetail${++sequence}`;
    let source = readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
    if (withoutMemo) source = source.replace(/ v-memo="[\s\S]*?"/g, "");
    if (eager) source = source.replace(/ && openedMovementIds\.has\(item.id\)/g, "")
      .replace(/ && openedDetails\.has\(item\.(?:id|key)\)/g, "")
      .replace(' v-if="openedDetails.has(part.members[0]!.id)"', "");
    if (componentName === "FundsView") source = source.replace("const initializing = ref(true)", "const initializing = ref(false)");
    source = source.replace("</script>", `Probe.capture(${JSON.stringify(componentName)}, ${annotations[componentName]});\n</script>`);
    const { descriptor } = parse(source);
    let output = ts.transpileModule(compileScript(descriptor, { id: key, inlineTemplate: true }).content,
      { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
    const bindings = {};
    for (const [, , location] of output.matchAll(/import\s+([\s\S]*?)\s+from\s+["']([^"']+)["'];?/g)) {
      const base = location.split("/").at(-1).replace(/\.(?:vue|ts)$/, "");
      if (modules[location] || modules[base]) bindings[location] = modules[location] ?? modules[base];
      else if (location.endsWith(".vue")) bindings[location] = { default: base === "BusinessStatusDetails" || (name === "BriefView" && base === "BriefActivityWorkbench") ? await component(base) : { render: () => null } };
      else {
        bindings[location] = { ...await server.ssrLoadModule(`/src/${path.substring(0, path.lastIndexOf("/") + 1)}${location}`) };
        if (base === "money" && componentName !== "BusinessStatusDetails") {
          for (const method of ["formatFen", "formatPositiveFen"]) if (bindings[location][method]) {
            const original = bindings[location][method];
            bindings[location][method] = value => { env.moneyCalls.push(String(value)); return original(value); };
          }
        }
        for (const method of ["fetchDeferredBrief", "fetchFundsDashboard", "fetchBriefGroup", "fetchBusinessStatus"]) if (method in bindings[location]) {
          if (realBriefApi && method === "fetchDeferredBrief") continue;
          bindings[location][method] = (...args) => {
            if (method === "fetchDeferredBrief") env.events.push({ type: "fetch", cursor: args[4]?.cursor, loaded: env.states.BriefView?.response.value?.data?.collections.vouchers?.items.length });
            return new Promise((resolve, reject) => (method === "fetchBusinessStatus" ? env.detailCalls : env.pageCalls).push({ args, resolve, reject }));
          };
        }
      }
    }
    output = output.replace(/import\s+([\s\S]*?)\s+from\s+["']([^"']+)["'];?/g, (_, names, location) => `const ${names.trim().startsWith("{") ? names.replace(/\bas\b/g, ":") : `{ default: ${names.trim()} }`} = Probe.bindings[${JSON.stringify(location)}];`);
    globalThis[key] = { bindings, capture(type, state) { if (type === "BusinessStatusDetails") { env.details.push(state); Vue.onBeforeUnmount(() => env.unmounted++); } else env.states[type] = state; } };
    cache[componentName] = (await import(`data:text/javascript;base64,${Buffer.from(`const Probe = globalThis.${key};\n${output}`).toString("base64")}`)).default;
    delete globalThis[key]; return cache[componentName];
  }
  const props = Vue.reactive({ section, groupKey: "group-a", expanded: false, period: env.route.query.period, snapshotVersion: initial.snapshot_version, refreshGeneration: 0 });
  if (name === "BriefActivityWorkbench") {
    delete props.section; delete props.groupKey; delete props.expanded;
    Object.assign(props, { groups: [], items: [], activityCount: 0, vouchers: Vue.shallowReactive([]), voucherCount: 520, vouchersHasMore: true });
  }
  const { root, host, nodes } = renderer(name === "BriefView" ? tag => { if (tag === "article") env.events.push({ type: "row-created" }); } : undefined);
  const rootComponent = await component(name);
  const app = host.createApp(name === "FundsView" || name === "BriefView" ? rootComponent : { render: () => Vue.h(rootComponent, props) });
  app.component("RouterLink", { props: ["to"], setup(link, { slots }) { return () => Vue.h("a", { to: link.to }, slots.default?.()); } }); app.mount(root); await flush();
  const state = env.states[name];
  if (name === "FundsView") {
    state.selectedPeriod.value = env.route.query.period; state.snapshotVersion.value = initial.snapshot_version;
    const { prepareDashboardCollections } = await server.ssrLoadModule("/src/utils/dashboardCollections.ts");
    initial.data.collections.movements.items = []; prepareDashboardCollections(initial.data); state.funds.value = initial.data;
  }
  return { ...env, get unmounted() { return env.unmounted; }, props, state, initial, root, nodes, close: () => app.unmount() };
}
const movement = index => ({ id: `movement-${index}`, subject_id: `business-${index}`, account_id: "bank-a", account_type: "bank", list_summary: `记录 ${index}`, display_summary: `记录 ${index}`, type: "付款", party: "对象", date: "2026-01-02", direction: "outflow", amount_fen: "100", signed_amount_fen: "-100", correction: false, internal_transfer: false });
function voucherContinuation(start, count, cursor) {
  const response = structuredClone(samples.brief_vouchers.response), collection = response.data.collections.vouchers;
  const original = collection.items[0];
  collection.items = Array.from({ length: count }, (_, index) => ({ ...structuredClone(original), voucher_version_id: `continuation-${start + index}`, number: String(start + index + 1), summary: `完整摘要 ${start + index}`, business_amount_fen: String(50000 + start + index) }));
  Object.assign(collection.page, { total_count: 1005, filtered_count: 1005, returned_count: count, has_more: !!cursor, next_cursor: cursor });
  return response;
}
async function voucherMonth(server) {
  const h = await mount(server, "BriefView"), response = structuredClone(samples.brief.response);
  const first = voucherContinuation(0, 20, "cursor-20").data.collections.vouchers;
  first.items = Vue.shallowReactive(first.items);
  response.data.collections.vouchers = first; response.data.voucher_count = 1005;
  h.state.activeSection.value = "activity"; h.state.response.value = response; h.state.vouchersReady.value = true;
  await flush(); const child = h.states.BriefActivityWorkbench;
  child.mode.value = "voucher"; child.voucherDisplayMode.value = "all"; await flush();
  h.events.length = 0; h.moneyCalls.length = 0;
  return { h, child, items: first.items };
}

test("real voucher all dispatches the known continuation before new DOM and retains cancel/retry boundaries", async t => {
  const saved = { window: globalThis.window, document: globalThis.document, Element: globalThis.Element };
  globalThis.window = { location: { origin: "http://offline.invalid" }, addEventListener() {}, removeEventListener() {} };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, querySelector() { return null; }, getElementById() { return null; } };
  globalThis.Element = class {};
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    {
      const { h, items } = await voucherMonth(server);
      try {
        const all = h.state.loadAllVouchers();
        assert.equal(h.pageCalls.length, 1); assert.equal(h.pageCalls[0].args[4].limit, 500);
        h.events.length = 0;
        h.pageCalls[0].resolve(voucherContinuation(20, 500, "cursor-520")); await flush();
        assert.equal(h.events[0].type, "fetch"); assert.equal(h.events[0].cursor, "cursor-520");
        assert.equal(h.events[0].loaded, 20, "next HTTP dispatch precedes current page append");
        assert.equal(h.events.filter(event => event.type === "row-created").length, 500);
        assert.equal(items.length, 520); assert.equal(h.pageCalls.length, 2);
        assert.strictEqual(h.pageCalls[0].args[2], h.pageCalls[1].args[2], "one cancellable chain controller");
        assert.equal(h.moneyCalls.filter(value => Number(value) >= 50000 && Number(value) < 50020).length, 0);
        h.events.length = 0; h.moneyCalls.length = 0;
        h.pageCalls[1].resolve(voucherContinuation(520, 485, null)); await all; await flush();
        assert.equal(h.events.filter(event => event.type === "row-created").length, 485);
        assert.equal(h.moneyCalls.filter(value => Number(value) >= 50000 && Number(value) < 50520).length, 0);
        assert.equal(items.length, 1005);
        assert.equal(h.nodes().filter(node => node.tag === "article" && node.props.class?.includes("voucher-card")).length, 1005);
        assert.deepEqual(items.map(item => item.voucher_version_id), Array.from({ length: 1005 }, (_, index) => `continuation-${index}`));
        items[0].summary = "同一行原地更新摘要"; h.state.sectionLoading.value.vouchers = true; await flush();
        h.state.sectionLoading.value.vouchers = false; await flush();
        const text = node => node.text + node.children.map(text).join("");
        assert(text(h.root).includes("同一行原地更新摘要"));
        await h.state.loadAllVouchers(); assert.equal(h.pageCalls.length, 2, "no terminal-page prefetch");
        t.diagnostic(JSON.stringify({ loaded: 1005, continuationRequests: 2, newRowCreates: [500, 485], oldRowFormats: 0 }));
      } finally { h.close(); }
    }
    for (const stop of ["paged", "business", "section", "company", "month", "refresh", "unmount"]) {
      const { h, child, items } = await voucherMonth(server); let closed = false;
      try {
        const all = h.state.loadAllVouchers(); h.pageCalls[0].resolve(voucherContinuation(20, 500, "cursor-520")); await flush();
        const pending = h.pageCalls[1]; assert.equal(items.length, 520);
        if (stop === "paged") child.toggleVoucherDisplayMode();
        else if (stop === "business") child.mode.value = "business";
        else if (stop === "section") h.state.activeSection.value = "overview";
        else if (stop === "company") h.route.query.company_id = "other-company";
        else if (stop === "month") h.route.query.period = "2026-02";
        else if (stop === "refresh") void h.state.refreshCurrent(true);
        else { h.close(); closed = true; }
        await flush(); assert.equal(pending.args[2].aborted, true, stop);
        if (stop === "paged") {
          assert.strictEqual(h.state.response.value.data.collections.vouchers.items, items);
          child.toggleVoucherDisplayMode(); assert.equal(h.pageCalls[2].args[4].cursor, "cursor-520");
          assert.notStrictEqual(pending.args[2], h.pageCalls[2].args[2]);
        }
        pending.resolve(voucherContinuation(520, 485, null)); await all; await flush();
        assert.equal(items.length, 520, `${stop}: late response does not append`);
        if (stop === "paged") {
          assert.equal(h.state.sectionLoading.value.vouchers, true, "old loop cannot clear the resumed session");
          h.pageCalls[2].resolve(voucherContinuation(520, 485, null)); await flush(); assert.equal(items.length, 1005);
        }
      } finally { if (!closed) h.close(); }
    }
    {
      const { h, child, items } = await voucherMonth(server);
      try {
        const all = h.state.loadAllVouchers(); h.pageCalls[0].resolve(voucherContinuation(20, 500, "cursor-520")); await flush();
        h.pageCalls[1].reject(new Error("合成读取失败")); await all; await flush();
        assert.equal(items.length, 520); assert.match(h.state.sectionErrors.value.vouchers, /加载失败/);
        child.retryVouchers(); assert.equal(h.pageCalls[2].args[4].cursor, "cursor-520");
        h.pageCalls[2].resolve(voucherContinuation(520, 485, null)); await flush(); assert.equal(items.length, 1005);
      } finally { h.close(); }
    }
    for (const failure of ["cursor", "scope", "snapshot"]) {
      const { h, items } = await voucherMonth(server);
      try {
        const all = h.state.loadAllVouchers(), invalid = voucherContinuation(20, 500, failure === "cursor" ? "cursor-20" : "cursor-520");
        if (failure === "scope") invalid.scope = "activity";
        if (failure === "snapshot") invalid.snapshot_version = "different-snapshot";
        h.pageCalls[0].resolve(invalid); await all; await flush();
        assert.equal(h.pageCalls.length, 1, failure);
        assert.equal(items.length, failure === "cursor" ? 520 : 20, failure);
        if (failure === "cursor") assert.match(h.state.sectionErrors.value.vouchers, /没有推进/);
      } finally { h.close(); }
    }
    {
      const { h, items } = await voucherMonth(server), raw = Vue.toRaw(h.state.voucherPreviewIndex.value), set = raw.set;
      try {
        const all = h.state.loadAllVouchers();
        raw.set = () => { throw new Error("合成页面应用失败"); };
        h.pageCalls[0].resolve(voucherContinuation(20, 500, "cursor-520")); await all;
        assert.equal(h.pageCalls.length, 2, "known continuation was dispatched before the failing apply");
        assert.equal(h.pageCalls[1].args[2].aborted, true, "application failure cancels the queued HTTP");
        assert.equal(items.length, 520); assert(h.state.sectionErrors.value.vouchers);
        h.pageCalls[1].reject(new Error("取消后的迟到拒绝"));
        await flush(); await new Promise(resolve => setImmediate(resolve));
        assert.equal(items.length, 520, "cancelled continuation cannot write back or reject unobserved");
      } finally { raw.set = set; h.close(); }
    }
  } finally { await server.close(); Object.assign(globalThis, saved); }
});
const member = (section, index) => ({ ...movement(index), key: `member-${index}`, id: `member-${index}`, detail_scope_key: section === "activity" ? `member-${index}` : null, voucher_version_id: `voucher-${index}`, group_key: "group-a", title: "付款", description: `记录 ${index}`, recognition: { period: "2026-01", label: "按月确认" }, state: "已入账", status: "open", outstanding_fen: "100", contribution_group_key: null });
function detailResponse(version, more = false) {
  const result = structuredClone(samples.business_status.response); result.snapshot_version = version;
  result.data.collections.settlement_events.page.has_more = more;
  result.data.collections.settlement_events.page.next_cursor = more ? "detail-next" : null;
  return result;
}

test("B29P reserve expense without a progress scope has no entry or hidden detail, while its net wage part stays scoped", async () => {
  const saved = { window: globalThis.window, document: globalThis.document, Element: globalThis.Element };
  globalThis.window = { location: { origin: "http://offline.invalid" }, addEventListener() {}, removeEventListener() {} };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, querySelector() { return null; }, getElementById() { return null; } };
  globalThis.Element = class {};
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  const h = await mount(server, "BriefGroupMembers");
  try {
    h.props.expanded = true; await flush();
    // Actual B29P meanings: one gross bank payment 1000000 = net wages 830000 + reserve 170000.
    const reserve = { ...member("activity", 0), subject_id: "gross-payment", voucher_version_id: "gross-voucher", amount_fen: "170000", title: "支付备用金费用", description: "支付备用金费用", detail_scope_key: null };
    const wages = { ...member("activity", 1), subject_id: "gross-payment", voucher_version_id: "gross-voucher", group_key: "wage-group", amount_fen: "830000", title: "支付工资", description: "支付工资" };
    h.pageCalls[0].resolve({ data: { section: "activity", group_key: h.props.groupKey, collections: { members: { items: [reserve], page: { has_more: false, next_cursor: null } }, vouchers: { items: [] } } } });
    await flush();
    const text = node => node.text + node.children.map(text).join("");
    assert.equal(h.nodes().filter(node => node.tag === "button" && text(node) === "业务进展").length, 0);
    h.state.toggle(reserve.key); await flush();
    assert.equal(h.details.length, 0, "a stale selection cannot even mount unscoped reserve progress");
    assert.equal(h.detailCalls.length, 0);
    h.props.groupKey = "wage-group"; await flush();
    h.pageCalls[1].resolve({ data: { section: "activity", group_key: h.props.groupKey, collections: { members: { items: [wages], page: { has_more: false, next_cursor: null } }, vouchers: { items: [] } } } });
    await flush();
    const entries = h.nodes().filter(node => node.tag === "button" && text(node) === "业务进展");
    assert.equal(entries.length, 1, "the separate 830000 net-wage group keeps its exact scope");
    entries[0].props.onClick(); await flush();
    assert.equal(h.details.length, 1); assert.equal(h.detailCalls.length, 1);
    assert.equal(h.detailCalls[0].args[3].detail_scope_key, wages.detail_scope_key);
    assert.equal(h.detailCalls[0].args[3].voucher_version_id, wages.voucher_version_id);
    h.detailCalls[0].resolve(detailResponse("net-progress")); await flush();
  } finally { h.close(); await server.close(); Object.assign(globalThis, saved); }
});

test("the rendered brief error retry uses one explicit retry POST and read-only GET polling", async () => {
  const saved = { window: globalThis.window, document: globalThis.document, Element: globalThis.Element, fetch: globalThis.fetch };
  const response = structuredClone(samples.deferred_brief.response);
  const company = response.read_context.company_id, period = response.selected_period.key;
  globalThis.window = { location: { origin: "http://offline.invalid", search: `?company_id=${company}` }, addEventListener() {}, removeEventListener() {} };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, querySelector() { return null; }, getElementById() { return null; } };
  globalThis.Element = class {};
  const calls = [], base = { schema_version: 6, company_id: company, period, model_version: null };
  globalThis.fetch = async (target, options = {}) => {
    const url = new URL(target, window.location.origin), method = options.method ?? "GET";
    calls.push({ path: url.pathname, method, body: options.body });
    const payload = calls.length === 1 ? { ...base, state: "failed", reason: "corrupt" }
      : calls.length === 2 ? { ...base, state: "preparing", retry_after_ms: 100 }
      : { ...base, state: "ready", model_version: response.snapshot_version, response };
    return new Response(JSON.stringify(payload));
  };
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  const h = await mount(server, "BriefView", { realBriefApi: true });
  try {
    await h.state.loadData(period); await flush();
    assert(h.state.error.value, "the real generated delivery parser must surface failed state");
    const text = node => node.text + node.children.map(text).join("");
    const button = h.nodes().find(node => node.tag === "button" && text(node) === "重新加载");
    assert(button, "retry comes from the rendered error panel");
    await button.props.onClick(); await flush();
    assert.deepEqual(calls.map(({ path, method }) => [method, path]), [
      ["GET", "/api/dashboard/brief"], ["POST", "/api/dashboard/brief/retry"], ["GET", "/api/dashboard/brief"],
    ]);
    assert.deepEqual(JSON.parse(calls[1].body), { company_id: company, period, limit: "20" });
    assert.equal(h.state.error.value, "");
    assert.equal(h.state.response.value.snapshot_version, response.snapshot_version);
    assert.equal(h.detailCalls.length, 0);
  } finally { h.close(); await server.close(); Object.assign(globalThis, saved); }
});

test("real hidden business detail instances grow only with explicit opens and preserve lifecycle", async t => {
  const saved = { window: globalThis.window, document: globalThis.document, Element: globalThis.Element };
  globalThis.window = { location: { origin: "http://offline.invalid" }, addEventListener() {}, removeEventListener() {} };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, querySelector() { return null; }, getElementById() { return null; } };
  globalThis.Element = class {};
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    for (const name of ["FundsView", "BriefGroupMembers"]) for (const eager of [true, false]) {
      const h = await mount(server, name, { eager });
      try {
        for (const total of [20, 100, 200, 400, 520]) {
          if (name === "FundsView") h.state.funds.value.collections.movements.items.push(...Array.from({ length: total - h.state.funds.value.collections.movements.items.length }, (_, offset) => movement(h.state.funds.value.collections.movements.items.length + offset)));
          else {
            h.props.expanded = true; await flush();
            const start = h.state.data.value?.collections.members.items.length ?? 0;
            if (start) void h.state.loadMore();
            h.pageCalls.at(-1).resolve({ data: { section: "activity", collections: { members: { items: Array.from({ length: total - start }, (_, offset) => member("activity", start + offset)), page: { has_more: true, next_cursor: `next-${total}` } }, vouchers: { items: [] } } } });
          }
          await flush();
          assert.equal(h.details.length, eager ? total : 0);
          assert.equal(h.detailCalls.length, 0, "loaded rows never request unopened progress");
          const rows = h.nodes().filter(node => name === "FundsView" ? node.props.class?.split(" ").includes("book-activity-row") : node.tag === "li");
          assert.equal(rows.length, total, "all business rows remain displayed");
        }
        t.diagnostic(JSON.stringify({ name, eager, rows: 520, mountedDetails: h.details.length, requests: h.detailCalls.length }));
        if (eager) continue;
        const open = key => name === "FundsView"
          ? h.nodes().find(node => node.props.class?.split(" ").includes("book-activity-row") && node.props["aria-expanded"] === false).props.onClick({})
          : h.state.toggle(key);
        open("member-0"); await flush();
        assert.equal(h.details.length, 1); assert.equal(h.detailCalls.length, 1, "click immediately reads the requested progress");
        const first = h.details[0], initialCall = h.detailCalls[0];
        initialCall.resolve(detailResponse(h.initial.snapshot_version, true)); await flush();
        const initialEvents = [...first.data.value.collections.settlement_events.items];
        const pending = first.loadMore(); assert.equal(h.detailCalls.length, 2);
        if (name === "FundsView") {
          const row = h.nodes().find(node => node.props.class?.split(" ").includes("book-activity-row") && node.props["aria-expanded"]);
          row.props.onKeydown({ key: "Enter", target: row, currentTarget: row, preventDefault() {} });
        } else h.state.toggle("member-0");
        await flush(); assert.equal(h.detailCalls[1].args[2].aborted, true); assert.equal(h.unmounted, 0);
        const stale = detailResponse(h.initial.snapshot_version); stale.data.collections.settlement_events.items[0].id = "late-collapsed";
        h.detailCalls[1].resolve(stale); await pending; await flush();
        assert.deepEqual(first.data.value.collections.settlement_events.items, initialEvents, "cancelled continuation cannot append a late result");
        const retainedData = first.data.value;
        open("member-0"); await flush(); assert.equal(h.details.length, 1); assert.equal(h.detailCalls.length, 2); assert.strictEqual(first.data.value, retainedData);
        if (name === "FundsView") {
          const background = first.loadMore(); const continuation = h.detailCalls.at(-1);
          h.state.activeSection.value = "funds-overview"; await flush(); assert.equal(first.props.expanded, true, "module switch preserves original expanded detail");
          assert.equal(continuation.args[2].aborted, false, "expanded detail keeps its permitted background continuation");
          h.state.invalidateRequests(true); await flush(); assert.equal(h.unmounted, 0); assert.equal(first.props.expanded, true, "same-snapshot refresh retains progress");
          assert.equal(continuation.args[2].aborted, false);
          h.state.selectedDetailView.value = "bank"; await flush(); assert.equal(first.props.expanded, false); assert.equal(h.unmounted, 0, "view switch keeps the cached instance");
          assert.equal(continuation.args[2].aborted, true); continuation.resolve(stale); await background;
          h.state.selectedDetailView.value = "book"; open("member-0"); await flush(); assert.equal(h.detailCalls.length, 3);
          h.state.snapshotVersion.value = "changed";
        } else h.props.refreshGeneration++;
        await flush(); assert.equal(h.unmounted, 1, "changed business scope discards opened detail cache");
      } finally { h.close(); }
    }
    for (const grouped of [false, true]) {
      const h = await mount(server, "BriefGroupMembers", { section: "open_items" });
      try {
        if (grouped) h.props.openSummary = { cutoff_period: "2026-01", current_cutoff_period: "2026-01" };
        h.props.expanded = true; await flush();
        const items = Array.from({ length: grouped ? 3 : 2 }, (_, index) => ({ ...member("open_items", index),
          ...(grouped ? { contribution_group_key: "employee-a", payroll_period: "2026-01", contribution_component: index === 2 ? "employer_social" : "employee_social", category_key: "payroll_payables", current_status: "open", source_amount_fen: "100", paid_fen: "0", other_settled_fen: "0", current_outstanding_fen: "100" } : {}) }));
        h.pageCalls[0].resolve({ data: { section: "open_items", collections: { members: { items, page: { has_more: false, next_cursor: null } }, vouchers: { items: [] } } } }); await flush();
        assert.equal(h.details.length, 0, "ordinary and contribution members do not mount unopened progress");
        if (grouped) h.state.togglePart(`${h.state.contributions.value[0].id}:employee_social`);
        h.state.toggle("member-0"); await flush(); assert.equal(h.details.length, 1); assert.equal(h.detailCalls.length, 1);
        h.detailCalls[0].resolve(detailResponse(h.initial.snapshot_version)); await flush();
        h.state.toggle("member-0"); await flush(); assert.equal(h.unmounted, 0);
        h.state.toggle("member-0"); await flush(); assert.equal(h.detailCalls.length, 1, "contribution and ordinary progress reuse valid reads");
        if (grouped) {
          h.state.toggle("member-2"); await flush(); assert.equal(h.details.length, 2); assert.equal(h.detailCalls.length, 2, "single contribution part mounts only when explicitly opened");
        }
        const last = h.detailCalls.at(-1);
        h.props.expanded = false; await flush(); assert.equal(h.unmounted, h.details.length);
        assert.equal(last.args[2].aborted, grouped, "group collapse cancels an unfinished single-part read");
        if (grouped) last.resolve(detailResponse(h.initial.snapshot_version));
        h.props.expanded = true; await flush();
        assert.equal(h.details.length, grouped ? 2 : 1, "cached group reopening never remounts member progress until an explicit click");
        assert.equal(h.state.selected.value, ""); assert.equal(h.state.openedDetails.value.size, 0);
      } finally { h.close(); }
    }
  } finally { await server.close(); Object.assign(globalThis, saved); }
});

test("real Vue row work on continuation and final-page state", async t => {
  const saved = { window: globalThis.window, document: globalThis.document, Element: globalThis.Element };
  globalThis.window = { location: { origin: "http://offline.invalid" }, addEventListener() {}, removeEventListener() {} };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, querySelector() { return null; }, getElementById() { return null; } };
  globalThis.Element = class {};
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  const voucher = index => {
    const row = structuredClone(samples.brief_vouchers.response.data.collections.vouchers.items[0]);
    return { ...row, voucher_version_id: `voucher-${index}`, number: String(index + 1), subject_id: `business-${index}`, summary: `凭证摘要 ${index}`, business_amount_fen: String(50000 + index), has_business_progress: true };
  };
  try {
    for (const name of ["BriefActivityWorkbench", "FundsView"]) for (const withoutMemo of [true, false]) {
      const h = await mount(server, name, { withoutMemo });
      try {
        const items = name === "FundsView" ? h.state.funds.value.collections.movements.items : h.props.vouchers;
        if (name === "BriefActivityWorkbench") { h.state.mode.value = "voucher"; h.state.voucherDisplayMode.value = "all"; }
        const observations = [];
        let previous = 0;
        for (const total of [20, 100, 200, 400, 520]) {
          h.moneyCalls.length = 0;
          items.push(...Array.from({ length: total - previous }, (_, offset) => {
            const index = previous + offset;
            return name === "FundsView" ? { ...movement(index), amount_fen: String(50000 + index), signed_amount_fen: String(-50000 - index) } : voucher(index);
          })); await flush();
          const oldRowFormats = h.moneyCalls.filter(value => Number(value) >= 50000 && Number(value) < 50000 + previous).length;
          assert.equal(oldRowFormats, withoutMemo ? previous : 0, "unchanged old main amounts only reformat in the unmemoized control");
          observations.push({ total, oldRowFormats }); previous = total;
        }
        h.moneyCalls.length = 0;
        if (name === "BriefActivityWorkbench") h.props.vouchersHasMore = false;
        else {
          const current = h.state.funds.value, collection = current.collections.movements;
          h.state.funds.value = { ...current, collections: { ...current.collections, movements: { ...collection, page: { ...collection.page, has_more: false, next_cursor: null } } } };
        }
        await flush();
        const finalStateRowFormats = h.moneyCalls.filter(value => Number(value) >= 50000 && Number(value) < 50520).length;
        assert.equal(finalStateRowFormats, withoutMemo ? 520 : 0);
        t.diagnostic(JSON.stringify({ name, withoutMemo, observations, finalStateRowFormats }));
        const text = node => node.text + node.children.map(text).join("");
        const rowClass = name === "FundsView" ? "book-activity-row" : "voucher-card";
        const rows = () => h.nodes().filter(node => node.props.class?.split(" ").includes(rowClass));
        const originalNode = rows()[0], first = items[0];
        assert.equal(rows().length, 520);
        const update = async () => {
          if (name === "FundsView") h.state.funds.value = { ...h.state.funds.value };
          else h.props.vouchersLoading = !h.props.vouchersLoading;
          await flush(); assert.strictEqual(rows()[0], originalNode, "existing row DOM survives memo invalidation");
        };
        if (name === "FundsView") {
          first.party = "更新对象"; first.list_summary = "完整更新用途"; first.amount_fen = "7654321"; first.subject_id = "changed-business";
          await update(); assert(text(originalNode).includes("更新对象")); assert(text(originalNode).includes("完整更新用途")); assert(text(originalNode).includes("−¥76,543.21"));
          first.correction = true; first.signed_amount_fen = "-12345"; await update(); assert(text(originalNode).includes("更正原业务")); assert(text(originalNode).includes("−¥123.45"));
          originalNode.props.onClick({}); await flush(); assert.equal(h.detailCalls.at(-1).args[1], "changed-business", "cached handler sees same-object identity changes");
          const pending = h.detailCalls.at(-1);
          h.state.selectedAccount.value = "bank:changed-account"; await flush(); assert.equal(pending.args[2].aborted, true);
          pending.resolve(detailResponse(h.initial.snapshot_version)); await flush();
        } else {
          first.summary = "完整更新摘要"; first.type = "更新类型"; first.state = "冲正"; first.reverses_version_id = "original"; first.business_amount_label = "更新金额"; first.business_amount_fen = "7654321"; first.recognition.label = "更新月确认";
          await update();
          for (const value of ["完整更新摘要", "更新类型", "冲正", "更新金额", "¥76,543.21", "本凭证用于冲销原记录"]) assert(text(originalNode).includes(value), value);
          first.has_business_progress = false; await update();
          assert(!originalNode.children.some(node => node.props.class === "voucher-progress-button"));
          assert(text(originalNode).includes("—"));
          await h.state.selectVoucher(first.voucher_version_id); await flush();
          assert(originalNode.props.class.includes("is-open"));
          assert(text(originalNode).includes("借贷合计"));
          const line = first.lines[0];
          line.account = "更新分录科目"; line.debit_fen = "12300"; line.credit_fen = "0";
          line.parties = [{ id: "party-a", name: "更新甲方", amount_fen: "12000" }, { id: "party-b", name: "更新乙方", amount_fen: "300" }];
          const asset = { asset_id: "changed-asset", name: "更新资产", code: "A-new", asset_type: "fixed" };
          first.asset_members.push(asset); await update();
          for (const value of ["更新分录科目", "¥123.00", "更新甲方", "更新乙方", "更新资产"]) assert(text(originalNode).includes(value), value);
          const replacement = { ...first, summary: "相同凭证身份的新行", lines: structuredClone(first.lines) };
          items[0] = replacement; await update(); assert(text(originalNode).includes("相同凭证身份的新行"));
          replacement.has_business_progress = true; await update(); h.state.progressVoucher.value = replacement; await flush();
          const visit = node => [node, ...node.children.flatMap(visit)];
          const progressButton = visit(originalNode).find(node => node.props.class === "voucher-progress-button");
          assert.equal(progressButton.props["aria-expanded"], true);
          h.state.closeProgress(); await flush(); assert.equal(progressButton.props["aria-expanded"], false);
          h.props.focusedVoucher = items[100]; await flush(); assert.equal(rows()[100].props.id, "selected-voucher"); assert.equal(rows()[0].props.id, undefined);
          h.props.snapshotVersion = "next-snapshot"; await flush();
          assert.equal(h.state.selectedVoucher.value, ""); assert.equal(h.state.progressVoucher.value, null);
        }
      } finally { h.close(); }
    }
  } finally { await server.close(); Object.assign(globalThis, saved); }
});
