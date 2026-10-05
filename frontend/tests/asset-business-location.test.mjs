import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import ts from "typescript";

let sequence = 0;
async function harness(query) {
  const key = `assetLocation${++sequence}`, calls = [], focus = [], cleanup = [];
  const route = Vue.reactive({ query: { company_id: "a", period: "2026-09", ...query }, hash: "" });
  globalThis[key] = { Vue, route, focus, cleanup, fetch: (...args) => new Promise(resolve => calls.push({ args, resolve })) };
  const script = readFileSync(new URL("../src/views/AssetsView.vue", import.meta.url), "utf8")
    .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/import[\s\S]*?from "[^"]+";/g, "");
  const { outputText } = ts.transpileModule(`
    const environment = globalThis.${key};
    export function instantiate() {
      const { computed, ref, watch, nextTick } = environment.Vue;
      const useRoute = () => environment.route, useRouter = () => ({ push() {}, replace() {} });
      const onMounted = () => {}, onBeforeUnmount = callback => environment.cleanup.push(callback);
      const useDashboardContext = () => ({ context: ref(null), load: async () => ({ periods: [] }), refresh() {} });
      const useDashboardSections = () => ({ activeSection: ref(""), focusSection() {}, positionSection() {} });
      const fetchAssetsDashboard = environment.fetch;
      const document = { getElementById: id => ({ focus: () => environment.focus.push(id) }) };
      const dashboardErrorMessage = error => error.message, isDashboardSnapshotChanged = () => false;
      ${script}
      mounted = true;
      return { loadAssets, response };
    }
  `, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const module = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  const scope = Vue.effectScope(), view = scope.run(() => module.instantiate());
  delete globalThis[key];
  return { ...view, calls, route, focus, close() { cleanup.forEach(callback => callback()); scope.stop(); } };
}

test("an exact exited asset overrides the current active filter", async () => {
  const view = await harness({ asset_filter: "active", asset_id: "exited-asset" });
  const pending = view.loadAssets("2026-09");
  assert.equal(view.calls[0].args[2].asset_filter, "all");
  assert.equal(view.calls[0].args[2].asset_id, "exited-asset");
  view.calls[0].resolve({ data: null }); await pending;
  assert.deepEqual(view.focus, ["asset-card-target"]);
  view.close();
});

test("exact project lookup is independent of pagination and changing the target aborts old detail", async () => {
  const view = await harness({ project_id: "project-after-page-20" });
  const old = view.loadAssets("2026-09");
  assert.equal(view.calls[0].args[2].project_id, "project-after-page-20");
  assert.equal(view.calls[0].args[2].cursor, undefined);
  view.route.query.project_id = "replacement-project";
  assert.equal(view.calls[0].args[1].aborted, true);
  view.calls[0].resolve({ data: { marker: "old" } }); await old;
  assert.equal(view.response.value, null);
  assert.deepEqual(view.focus, []);
  const next = view.loadAssets("2026-09");
  assert.equal(view.calls[1].args[2].project_id, "replacement-project");
  view.calls[1].resolve({ data: { marker: "new" } }); await next;
  assert.equal(view.response.value.data.marker, "new");
  assert.deepEqual(view.focus, ["project-card-target"]);
  view.close();
});
