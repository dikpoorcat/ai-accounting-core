import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import { parse } from "@vue/compiler-sfc";
import ts from "typescript";
import { appendDashboardCollection } from "./helpers/dashboardCollections.mjs";

const source = readFileSync(new URL("../src/components/CloseReviewPanel.vue", import.meta.url), "utf8");
const script = parse(source).descriptor.scriptSetup.content.replace(/import[\s\S]*?from "[^"]+";/g, "");
let sequence = 0;

async function panelHarness() {
  const calls = [], unmount = [];
  const props = Vue.reactive({ companyId: "a", period: "2026-02", previewDigest: "a".repeat(64) });
  const environment = {
    Vue, props, unmount, calls,
    fetchCloseReview: (...args) => new Promise((resolve, reject) => calls.push({ args, resolve, reject })),
  };
  const key = `closeReviewPanel${++sequence}`;
  globalThis[key] = environment;
  const { outputText } = ts.transpileModule(`const environment = globalThis.${key};
    export function instantiate() {
      const { computed, ref, shallowRef, watch } = environment.Vue;
      const defineProps = () => environment.props;
      const onBeforeUnmount = callback => environment.unmount.push(callback);
      const fetchCloseReview = environment.fetchCloseReview;
      const dashboardErrorMessage = error => error.message;
      const formatFen = value => String(value);
      ${script}
      return { response, loading, error };
    }`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const module = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  delete globalThis[key];
  const scope = Vue.effectScope();
  const panel = scope.run(() => module.instantiate());
  return { ...panel, props, calls, close() { unmount.forEach(callback => callback()); scope.stop(); } };
}

const flush = async () => { for (let index = 0; index < 6; index++) { await Promise.resolve(); await Vue.nextTick(); } };
const prepared = (companyId = "a", period = "2026-02", digest = "preview-a") => ({
  company_id: companyId, period, state: "prepared", preview_digest: digest, close_digest: null,
  owner_review: { amounts: {}, businesses: [], materials: [], status: "ready", followup_count: 0 },
});

test("owner review is explicitly bound to the requested digest", async () => {
  const h = await panelHarness();
  try {
    await flush();
    assert.equal(h.calls.length, 1);
    assert.deepEqual(h.calls[0].args.slice(0, 2), ["a", "2026-02"]);
    assert.deepEqual(h.calls[0].args[3], { previewDigest: "a".repeat(64) });
    h.calls[0].resolve(prepared());
    await flush();
    assert.equal(h.response.value.state, "prepared");
  } finally { h.close(); }
});

test("late old-selection replies cannot replace a new review", async () => {
  const h = await panelHarness();
  try {
    await flush();
    h.props.companyId = "b";
    h.props.period = "2026-03";
    h.props.previewDigest = "b".repeat(64);
    await flush();
    assert.equal(h.calls[0].args[2].aborted, true);
    h.calls[0].resolve(prepared());
    await flush();
    assert.equal(h.response.value, null);
    const current = h.calls.at(-1);
    assert.deepEqual(current.args.slice(0, 2), ["b", "2026-03"]);
    assert.deepEqual(current.args[3], { previewDigest: "b".repeat(64) });
    current.resolve(prepared("b", "2026-03", "b".repeat(64)));
    await flush();
    assert.equal(h.response.value.company_id, "b");
  } finally { h.close(); }
});

test("stale content and failed reads do not automatically follow a new preview", async () => {
  const h = await panelHarness();
  try {
    await flush();
    h.calls[0].resolve({ ...prepared(), state: "stale" });
    await flush();
    assert.equal(h.response.value.state, "stale");
    assert.equal(h.calls.length, 1);
    h.props.previewDigest = "b".repeat(64);
    await flush();
    assert.equal(h.response.value, null);
    h.calls.at(-1).reject(new Error("核对读取失败"));
    await flush();
    assert.equal(h.error.value, "核对读取失败");
    assert.equal(h.calls.length, 2);
  } finally { h.close(); }
});

test("closing the details cancels the request and ignores its late response", async () => {
  const h = await panelHarness();
  await flush();
  h.close();
  assert.equal(h.calls[0].args[2].aborted, true);
  h.calls[0].resolve(prepared());
  await flush();
  assert.equal(h.response.value, null);
});
async function briefHarness() {
  const briefSource = readFileSync(new URL("../src/views/BriefView.vue", import.meta.url), "utf8");
  const briefScript = parse(briefSource).descriptor.scriptSetup.content.replace(/import[\s\S]*?from "[^"]+";/g, "");
  const route = Vue.reactive({ query: { company_id: "a", period: "2026-02" } });
  const calls = [], unmount = [];
  const context = Vue.ref({ current_company: { company_id: "a" }, periods: [{ key: "2026-02" }] });
  const environment = { Vue, route, calls, unmount, context, appendDashboardCollection };
  const key = `briefReview${++sequence}`;
  globalThis[key] = environment;
  const { outputText } = ts.transpileModule(`const environment = globalThis.${key};
    export function instantiate() {
      const { computed, ref, shallowReactive, shallowRef, watch } = environment.Vue;
      const { appendDashboardCollection } = environment;
      const useRoute = () => environment.route;
      const useRouter = () => ({ replace() {}, push() {} });
      const useDashboardContext = () => ({ context: environment.context, load: async () => environment.context.value, refresh: async () => environment.context.value });
      const useDashboardSections = () => ({ activeSection: ref("overview"), focusSection() {} });
      const fetchDeferredBrief = (...args) => new Promise(resolve => environment.calls.push({ args, resolve }));
      const onMounted = () => {};
      const onBeforeUnmount = callback => environment.unmount.push(callback);
      const dashboardErrorMessage = error => error.message;
      const isDashboardSnapshotChanged = () => false;
      ${briefScript}
      return { response, reviewRequest, needsMonthlyReview, showMonthlyReview, loadData, refresh, sectionLinks };
    }`, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } });
  const module = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
  delete globalThis[key];
  const scope = Vue.effectScope();
  const brief = scope.run(() => module.instantiate());
  return { ...brief, calls, route, close() { unmount.forEach(callback => callback()); scope.stop(); } };
}
const briefResult = (monthState, request = null) => ({ selected_period: { key: "2026-02" }, data: { month_state: monthState, owner_review_request: request, collections: {} } });

test("only an open month with a valid review locator shows a confirmation task", async () => {
  const h = await briefHarness();
  try {
    for (const state of ["closed", "covered", "open"]) {
      const pending = h.loadData("2026-02");
      h.calls.at(-1).resolve(briefResult(state));
      await pending;
      assert.equal(h.needsMonthlyReview.value, false, state);
      assert.equal(h.showMonthlyReview.value, false);
    }
    const pending = h.loadData("2026-02");
    h.calls.at(-1).resolve(briefResult("open", { preview_digest: "a".repeat(64) }));
    await pending;
    assert.equal(h.needsMonthlyReview.value, true);
    assert.equal(h.showMonthlyReview.value, false, "loading the brief must not open or fetch the details");
    assert.equal(h.sectionLinks.value.some(item => item.id === "monthly-review"), false);
  } finally { h.close(); }
});

test("same-selection refresh closes details before obtaining the next preview", async () => {
  const h = await briefHarness();
  try {
    h.response.value = briefResult("open", { preview_digest: "a".repeat(64) });
    h.showMonthlyReview.value = true;
    const pending = h.refresh();
    assert.equal(h.showMonthlyReview.value, false);
    await flush();
    h.calls.at(-1).resolve(briefResult("open", { preview_digest: "b".repeat(64) }));
    await pending;
    assert.equal(h.reviewRequest.value.preview_digest, "b".repeat(64));
    assert.equal(h.showMonthlyReview.value, false, "a new preview must require another explicit view action");
  } finally { h.close(); }
});
