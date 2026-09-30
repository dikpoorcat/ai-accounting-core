import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import * as Vue from "vue";
import { parse } from "@vue/compiler-sfc";
import ts from "typescript";

const source = readFileSync(new URL("../src/components/CloseReviewPanel.vue", import.meta.url), "utf8");
const script = parse(source).descriptor.scriptSetup.content.replace(/import[\s\S]*?from "[^"]+";/g, "");
let sequence = 0;

async function panelHarness(prefetch) {
  const calls = [], unmount = [];
  const props = Vue.reactive({ companyId: "a", period: "2026-02", refreshKey: 1, prefetch });
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
      const mergeCloseReviewSection = (current, collections, result, section) => ({
        bindingChanged: false, collections: { ...collections, [section]: result.collection },
      });
      const dashboardErrorMessage = error => error.message;
      const isDashboardSnapshotChanged = error => error.code === "dashboard_snapshot_changed";
      const businessStateLabel = value => value, formatFen = value => String(value);
      ${script}
      return { response, pinnedDigest, loading, error, loadSection };
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
  owner_review: { accounting_summary: {} }, collection: null,
});

test("prefetched review summary is consumed once and detail remains pinned to its digest", async () => {
  const prefetch = {
    companyId: "a", period: "2026-02",
    result: Promise.resolve({ status: "fulfilled", value: prepared() }),
  };
  const h = await panelHarness(prefetch);
  try {
    await flush();
    assert.equal(h.calls.length, 0, "the panel must not duplicate a prefetched summary request");
    assert.equal(h.response.value.state, "prepared");
    assert.equal(h.pinnedDigest.value, "preview-a");
    const detail = h.loadSection("evidence");
    assert.equal(h.calls.length, 1);
    assert.deepEqual(h.calls[0].args[3], { previewDigest: "preview-a", section: "evidence", cursor: undefined });
    h.calls[0].resolve({ ...prepared(), collection: { section: "evidence", items: [], page: { has_more: false, next_cursor: null } } });
    await detail;
    assert.equal(h.pinnedDigest.value, "preview-a");
  } finally { h.close(); }
});

test("review failure is shown without a duplicate request, and a mismatched prefetch is ignored", async () => {
  const h = await panelHarness({
    companyId: "a", period: "2026-02",
    result: Promise.resolve({ status: "rejected", reason: new Error("核对读取失败") }),
  });
  try {
    await flush();
    assert.equal(h.error.value, "核对读取失败");
    assert.equal(h.response.value, null);
    assert.equal(h.calls.length, 0);
    h.props.companyId = "b";
    h.props.period = "2026-03";
    await flush();
    assert.equal(h.calls.length, 1, "the old company's prefetch cannot serve the new selection");
    assert.deepEqual(h.calls[0].args.slice(0, 2), ["b", "2026-03"]);
    h.calls[0].resolve(prepared("b", "2026-03", "preview-b"));
    await flush();
    assert.equal(h.response.value.company_id, "b");
  } finally { h.close(); }
});

test("late old-company prefetch cannot replace a new company's review", async () => {
  let resolveOld;
  const h = await panelHarness({
    companyId: "a", period: "2026-02",
    result: new Promise(resolve => { resolveOld = resolve; }),
  });
  try {
    await flush();
    h.props.companyId = "b";
    h.props.period = "2026-03";
    h.props.prefetch = null;
    await flush();
    assert.equal(h.calls.length, 1);
    resolveOld({ status: "fulfilled", value: prepared() });
    await flush();
    assert.equal(h.response.value, null);
    h.calls[0].resolve(prepared("b", "2026-03", "preview-b"));
    await flush();
    assert.equal(h.response.value.company_id, "b");
    assert.equal(h.pinnedDigest.value, "preview-b");
  } finally { h.close(); }
});
