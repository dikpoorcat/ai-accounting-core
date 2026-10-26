import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as Vue from "vue";
import { createServer } from "vite";
import { rendererNode } from "./helpers/rendererNode.mjs";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const flush = async () => { await Vue.nextTick(); await Vue.nextTick(); };
let sequence = 0;

function hostRenderer() {
  const make = rendererNode;
  const root = make("root");
  const renderer = Vue.createRenderer({
    createElement: tag => make(tag), createText: text => make("#text", text), createComment: text => make("#comment", text),
    insert(node, target, anchor) {
      if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
      const position = anchor ? target.children.indexOf(anchor) : -1;
      target.children.splice(position < 0 ? target.children.length : position, 0, node); node.parent = target;
    },
    remove(node) { node.parent?.children.splice(node.parent.children.indexOf(node), 1); node.parent = null; },
    setText(node, text) { node.text = text; }, setElementText(node, text) { node.text = text; node.children = []; },
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp(node, key, _previous, value) { node.props[key] = value; }, querySelector() { return root; },
  });
  const nodes = () => { const result = []; const visit = node => { result.push(node); node.children.forEach(visit); }; visit(root); return result; };
  const text = node => node.text + node.children.map(text).join("");
  return { renderer, root, nodes, text };
}

const specifications = {
  Funds: { sample: "cash_funds", collection: "movements", identity: "id", rowClass: "book-activity-row", pageArgument: "book" },
  Employees: { sample: "employees_focused", collection: "employees", identity: "employee_id", rowClass: "employee-card", pageArgument: "employees" },
  Assets: { sample: "asset_payment_summary", collection: "assets", identity: "asset_id", rowClass: "asset-card", pageArgument: "assets" },
};
function payloadFor(name, total) {
  const spec = specifications[name], response = structuredClone(samples[spec.sample].response);
  if (name === "Employees") { response.data.employee_id = null; response.data.employee_filter = "all"; }
  if (name === "Assets") { response.data.asset_id = null; response.data.asset_filter = "all"; }
  if (name === "Funds") { response.data.movement_account_id = null; response.data.movement_account_type = null; }
  const template = response.data.collections[spec.collection].items[0];
  assert(template, `${name} has a real backend row template`);
  const row = index => ({ ...structuredClone(template), [spec.identity]: `growth-${index}`,
    ...(name === "Funds" ? { list_summary: `增长流水 ${index}`, display_summary: `增长流水 ${index}`, summary: `增长流水 ${index}` } : { name: `增长对象 ${index}` }) });
  const collection = (start, count) => ({ items: Array.from({ length: count }, (_, offset) => row(start + offset)), page: {
    total_count: total, filtered_count: total, returned_count: count, has_more: start + count < total,
    next_cursor: start + count < total ? `growth-${start + count}` : null,
  } });
  response.data.collections[spec.collection] = collection(0, 20);
  return { response, collection };
}

async function mountPage(server, name, response, shallow) {
  const spec = specifications[name], key = `pageGrowth${++sequence}`;
  const env = { modules: {}, response, tracks: [], calls: [], state: null };
  const route = Vue.reactive({ query: { company_id: response.read_context.company_id, period: response.selected_period.key,
    employee_filter: "all", asset_filter: "all" } });
  env.modules.vue = { ...Vue, onMounted() {}, onRenderTracked: Vue.onRenderTracked };
  env.modules["vue-router"] = { useRoute: () => route, useRouter: () => ({ push() {}, replace() {} }) };
  env.modules["../composables/useDashboardContext"] = { useDashboardContext: () => ({
    context: Vue.ref(samples.company_with_period.response), loading: Vue.ref(false), error: Vue.ref(""), load: async () => null, refresh: async () => null,
  }) };
  env.modules["../composables/useDashboardSections"] = { useDashboardSections: () => ({
    activeSection: Vue.ref(name === "Funds" ? "bank-details" : name === "Assets" ? "asset-list-title" : "employee-list-title"),
    focusSection() {}, focusSelectedPanel() {}, positionSection() {},
  }) };
  const viewSource = readFileSync(new URL(`../src/views/${name}View.vue`, import.meta.url), "utf8");
  let source = viewSource.replace('</script>', `
    VueGrowth.onRenderTracked(event => VueGrowth.env.tracks.push(event));
    ${name === "Assets" ? "mounted = true;" : ""}
    VueGrowth.env.state = { ${name === "Funds" ? "funds, snapshotVersion, selectedPeriod, responsePeriod, selectedDetailView, expandedMovementId" : "response, displayMode"}, ${name === "Assets" ? "listInitialized," : ""} loadMore, activeSection };
    </script>`);
  if (name === "Funds") source = source.replace('const initializing = ref(true)', 'const initializing = ref(false)')
    .replace('const funds = shallowRef<FundsData | null>(null)', `const funds = ${shallow ? "shallowRef" : "ref"}(VueGrowth.env.response.data)`)
    .replace('const snapshotVersion = ref("")', 'const snapshotVersion = ref(VueGrowth.env.response.snapshot_version)')
    .replace('const responsePeriod = ref("")', 'const responsePeriod = ref(VueGrowth.env.response.selected_period.key)')
    .replaceAll('{ immediate: true }', '{ immediate: false }');
  else source = source.replace(/const response = (?:ref|shallowRef)<[^;\n]+>\(null\)/, `const response = ${shallow ? "shallowRef" : "ref"}(VueGrowth.env.response)`);
  source = source.replace('const selectedPeriod = ref("")', 'const selectedPeriod = ref(VueGrowth.env.response.selected_period.key)');
  // Shallow candidate uses reactive collection arrays and immutable backend rows.
  // This is an executable candidate of the same SFC, not a replacement renderer.
  const { descriptor } = parse(source);
  let compiled = ts.transpileModule(compileScript(descriptor, { id: key, inlineTemplate: true }).content,
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
  const imports = [...compiled.matchAll(/import\s+([\s\S]*?)\s+from\s+["']([^"']+)["'];?/g)];
  for (const [, _binding, location] of imports) {
    if (env.modules[location]) continue;
    if (location.endsWith(".vue")) env.modules[location] = { default: { render: () => null } };
    else env.modules[location] = { ...await server.ssrLoadModule(`/src/views/${location}`) };
    if (location.startsWith("../api/")) {
      for (const method of ["fetchFundsDashboard", "fetchEmployeesDashboard", "fetchAssetsDashboard"]) {
        if (method in env.modules[location]) env.modules[location][method] = (...args) => new Promise((resolve, reject) => env.calls.push({ args, resolve, reject }));
      }
    }
  }
  env.modules.vue.shallowRef = Vue.shallowRef;
  if (shallow) env.modules["../utils/dashboardCollections"].prepareDashboardCollections(response.data);
  compiled = compiled.replace(/import\s+([\s\S]*?)\s+from\s+["']([^"']+)["'];?/g, (_, bindings, location) => {
    const declaration = bindings.trim().startsWith("{") ? bindings.replace(/\bas\b/g, ":") : `{ default: ${bindings.trim()} }`;
    return `const ${declaration} = VueGrowth.env.modules[${JSON.stringify(location)}];`;
  });
  compiled = `const VueGrowth = { env: globalThis.${key}, onRenderTracked: globalThis.${key}.modules.vue.onRenderTracked };\n${compiled}`;
  globalThis[key] = env;
  const { default: component } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
  delete globalThis[key];
  const host = hostRenderer(), app = host.renderer.createApp(component);
  app.component("RouterLink", { render: () => null }); app.mount(host.root); await flush();
  const rows = () => host.nodes().filter(node => node.props.class?.split(" ").includes(spec.rowClass));
  return { ...env, ...host, rows, close: () => app.unmount() };
}

test("Funds, Employees and Assets render 100/200/400 rows and preserve loaded state with a shallow candidate", async t => {
  const savedWindow = globalThis.window, savedDocument = globalThis.document, savedElement = globalThis.Element;
  globalThis.Element = class {};
  globalThis.window = { location: { origin: "http://offline.invalid" }, addEventListener() {}, removeEventListener() {} };
  globalThis.document = { addEventListener() {}, removeEventListener() {}, querySelector() { return null; } };
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true }, server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    for (const name of Object.keys(specifications)) await t.test(name, async () => {
      for (const total of [100, 200, 400]) {
        const tracked = {};
        for (const shallow of [false, true]) {
          const spec = specifications[name], { response, collection } = payloadFor(name, total);
          const mounted = await mountPage(server, name, response, shallow);
          try {
            assert.equal(mounted.rows().length, 20);
            const originalNode = mounted.rows()[0];
            if (originalNode.tag === "details") originalNode.open = true;
            const currentCollection = () => (name === "Funds" ? mounted.state.funds.value : mounted.state.response.value.data).collections[spec.collection];
            const originalItems = currentCollection().items;
            const nestedTracked = () => {
              const rows = new Set(originalItems.map(row => Vue.toRaw(row)));
              return mounted.tracks.filter(event => rows.has(event.target)).length;
            };
            tracked[shallow] = nestedTracked();
            if (name === "Funds") {
              originalNode.props.onClick({ target: originalNode, currentTarget: originalNode }); await flush();
              assert.equal(originalNode.props["aria-expanded"], true, "the actual row click expands business progress");
            }
            let totalRowTracks = 0;
            for (let start = 20; start < total; start += 20) {
              mounted.tracks.length = 0;
              const pending = mounted.state.loadMore(spec.pageArgument);
              assert.equal(mounted.calls.at(-1).args.at(-1).cursor, `growth-${start}`);
              const next = structuredClone(samples[spec.sample].response);
              next.data.collections = { [spec.collection]: collection(start, 20) };
              mounted.calls.at(-1).resolve(next); await pending; await flush();
              assert.strictEqual(currentCollection().items, originalItems, "continuation appends to the same array");
              assert.equal(mounted.rows().length, start + 20);
              assert.strictEqual(mounted.rows()[0], originalNode, "earlier native detail nodes stay mounted");
              if (originalNode.tag === "details") assert.equal(originalNode.open, true, "continuation preserves native expansion");
              if (name === "Funds") assert.equal(originalNode.props["aria-expanded"], true, "continuation preserves explicit business expansion");
              totalRowTracks += nestedTracked();
            }
            assert.equal(currentCollection().items.length, total);
            assert(mounted.text(mounted.root).includes(name === "Funds" ? `增长流水 ${total - 1}` : `增长对象 ${total - 1}`));
            if (name !== "Funds") {
              mounted.state.displayMode.value = "list";
              if (name === "Assets") mounted.state.listInitialized.value = true;
              await flush();
              const detail = mounted.nodes().find(node => node.tag === "details");
              assert(detail, "list details remain available"); detail.open = true;
              mounted.state.displayMode.value = "cards"; await flush();
              assert(mounted.nodes().includes(detail), "switching display preserves loaded native details");
              assert.equal(detail.open, true);
              mounted.state.displayMode.value = "list"; await flush();
              assert(mounted.nodes().includes(detail));
            }
            mounted.state.activeSection.value = "overview"; await flush();
            assert.strictEqual(mounted.rows()[0], originalNode, "hidden loaded modules retain their rows");
            if (shallow) assert.equal(totalRowTracks, 0, "immutable row fields do not add nested Vue dependencies");
            else assert(totalRowTracks > total, "deep row dependency work grows during real SFC updates");
            t.diagnostic(JSON.stringify({ name, total, shallow, initialRowTracks: tracked[shallow], continuationRowTracks: totalRowTracks, loaded: currentCollection().items.length }));
          } finally { mounted.close(); }
        }
        assert(tracked.false > 100, `${name}/${total}: baseline actually tracks nested row fields`);
        assert.equal(tracked.true, 0, `${name}/${total}: shallow candidate avoids those nested dependencies`);
      }
    });
  } finally { await server.close(); globalThis.window = savedWindow; globalThis.document = savedDocument; globalThis.Element = savedElement; }
});
