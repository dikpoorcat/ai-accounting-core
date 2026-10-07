import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";
import { validateDashboardBusinessStatusResponse } from "../src/api/generated/dashboardBusinessStatus.js";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));

function laborContext() {
  const labor = structuredClone(samples.employees_labor_sources.response.data.collections.labor_sources.items[0]);
  Object.assign(labor, { net_fen: "450000", booked_tax_fen: "50000" });
  Object.assign(labor.obligations[0], { amount_fen: "450000", paid_fen: "100000", other_settled_fen: "50000", remaining_fen: "300000" });
  return labor;
}

function scenario(context = laborContext()) {
  const data = structuredClone(samples.business_status.response.data);
  Object.assign(data.identity, { subject_id: context.subject_id, kind: "labor_accrual" });
  data.latest_source = { deleted: false, period: context.period };
  data.current_business_result = { amount_fen: context.gross_fen, amount_label: "劳务确认毛额", posting_period: context.cutoff_period };
  data.display_profiles = { employees: [], counterparties: [], assets: [], fund_accounts: [] };
  const [obligation] = context.obligations;
  data.settlements = { cutoff_period: context.cutoff_period, status: "established", checking: false, obligations: [{
    key: obligation.key, name: obligation.name, direction: "payable", category_key: "labor_payables", source_period: context.period,
    source_amount_fen: obligation.amount_fen, paid_fen: obligation.paid_fen, other_settled_fen: obligation.other_settled_fen,
    remaining_fen: obligation.remaining_fen, settlement_status: "partial",
  }] };
  data.current_followups.settlements = structuredClone(data.settlements);
  data.collections.settlement_events = { items: [], page: { total_count: 0, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null } };
  return data;
}

test("personal labor details remove only facts already displayed for the exact source and cutoff", async t => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false, optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-labor-detail", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/components/BusinessStatusDetails.vue"))
        return code.replace("ref<BusinessStatusData | null>(null)", "ref(globalThis.laborDetailScenario)");
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
    await router.push("/?company_id=co&period=2026-01");
    const { default: component } = await server.ssrLoadModule("/src/components/BusinessStatusDetails.vue");
    async function render(data = scenario(), context = laborContext(), extraProps = {}) {
      assert(validateDashboardBusinessStatusResponse({ ...samples.business_status.response, data }), JSON.stringify(validateDashboardBusinessStatusResponse.errors));
      globalThis.laborDetailScenario = data;
      const app = createSSRApp(component, { subjectId: context.subject_id, period: context.cutoff_period,
        settlementView: "historical", presentation: "labor", laborContext: context, expanded: true, hideSummary: true, ...extraProps });
      app.use(router);
      return renderToString(app);
    }
    await t.test("exact displayed obligations and current or frozen gross amounts are omitted", async () => {
      const data = scenario();
      const html = await render(data);
      assert.doesNotMatch(html, /¥5,000\.00|¥4,500\.00|¥1,000\.00|¥500\.00|¥3,000\.00/);
      data.frozen_adoption = { amount_fen: "500000", amount_label: "劳务确认毛额", close_period: "2026-01" };
      assert.doesNotMatch(await render(data), /¥5,000\.00|¥4,500\.00/);
      assert.match(await render(data, laborContext(), { presentation: "default" }), /¥5,000\.00|¥4,500\.00/);
      for (const kind of ["labor", "labor_accrual", "labor_project_cost"]) {
        const context = laborContext(); context.capitalized = kind === "labor_project_cost";
        context.obligations[0].key = `${kind}:${context.subject_id}:net`;
        const data = scenario(context); data.identity.kind = kind;
        data.settlements.obligations[0].category_key = context.capitalized ? "other_payables" : "labor_payables";
        data.current_business_result.amount_label = context.capitalized ? "资本化劳务确认毛额" : "劳务确认毛额";
        assert.doesNotMatch(await render(data, context), /¥5,000\.00|¥4,500\.00/, `${kind} retains its known accounting meaning`);
      }
    });
    await t.test("distinct keys, names or periods retain full progress and changed amounts show only the new cells", async () => {
      const distinctSources = [
        ["key", "different-net-obligation"], ["name", "tax"], ["source_period", "2025-12"],
      ];
      for (const [field, value] of distinctSources) {
        const data = scenario(); data.settlements.obligations[0][field] = value;
        const html = await render(data);
        for (const amount of ["¥4,500.00", "¥1,000.00", "¥500.00", "¥3,000.00"])
          assert(html.includes(amount), `${field} retains ${amount}`);
      }
      for (const [field, value, displayed] of [
        ["source_amount_fen", "450001", "¥4,500.01"], ["paid_fen", "100001", "¥1,000.01"],
        ["other_settled_fen", "50001", "¥500.01"], ["remaining_fen", "300001", "¥3,000.01"],
      ]) {
        const data = scenario(); data.settlements.obligations[0][field] = value;
        const html = await render(data);
        assert(html.includes(displayed), `${field} retains its changed amount`);
        assert.doesNotMatch(html, /¥4,500\.00|¥1,000\.00|¥500\.00|¥3,000\.00/, `${field} does not repeat matching amount cells`);
      }
      const differentCutoff = scenario(); differentCutoff.settlements.cutoff_period = "2026-02";
      assert.match(await render(differentCutoff), /¥4,500\.00/, "a later cutoff is distinct");
      const differentSubject = scenario(); differentSubject.identity.subject_id = "another-labor-business";
      assert.match(await render(differentSubject), /¥4,500\.00/, "a distinct subject is never a displayed match");
      assert.match(await render(scenario(), laborContext(), { subjectId: "another-labor-business" }), /¥4,500\.00/, "a distinct requested subject is never a displayed match");
    });
    await t.test("different gross amount, source or accounting period and nonlabor results stay visible", async () => {
      for (const mode of ["current", "frozen"]) {
        const data = scenario();
        if (mode === "frozen") data.frozen_adoption = { amount_fen: "500001", amount_label: "劳务确认毛额", close_period: "2026-01" };
        else data.current_business_result.amount_fen = "500001";
        assert.match(await render(data), /¥5,000\.01/, `${mode} different gross amount`);
        const differentPeriod = scenario();
        if (mode === "frozen") differentPeriod.frozen_adoption = { amount_fen: "500000", amount_label: "劳务确认毛额", close_period: "2026-02" };
        else differentPeriod.current_business_result.posting_period = "2026-02";
        assert.match(await render(differentPeriod), /¥5,000\.00/, `${mode} different accounting period`);
      }
      const differentSource = scenario(); differentSource.latest_source.period = "2025-12";
      assert.match(await render(differentSource), /¥5,000\.00/, "different source period");
      const differentKind = scenario(); differentKind.identity.kind = "expense";
      assert.match(await render(differentKind), /¥5,000\.00/, "nonlabor gross is not assumed to be the displayed labor amount");
      const differentMeaning = scenario(); differentMeaning.current_business_result.amount_label = "业务确认金额";
      assert.match(await render(differentMeaning), /¥5,000\.00/, "equal amounts with a different accounting meaning remain visible");
    });
    await t.test("later changed settlements remain distinct from the displayed historical progress", async () => {
      const data = scenario();
      data.current_followups.settlements.cutoff_period = "2026-02";
      Object.assign(data.current_followups.settlements.obligations[0], { paid_fen: "450000", other_settled_fen: "0", remaining_fen: "0", settlement_status: "settled" });
      const html = await render(data);
      assert.match(html, /后续进展[\s\S]*2026年2月[\s\S]*已结清/);
      assert.match(html, /¥4,500\.00/); assert.match(html, /¥0\.00/);
      assert.doesNotMatch(html, /补充款项进度|¥1,000\.00|¥500\.00|¥3,000\.00/, "identical historical details remain deduplicated");
    });
    await t.test("related people deduplicate by matching identity and name while retaining distinct or unknown identities", async () => {
      const context = laborContext();
      const profile = (id, name) => {
        const item = structuredClone(samples.business_status.response.data.display_profiles.counterparties[0]);
        item.entity_id = id; item.values.display_name = name; return item;
      };
      const data = scenario();
      data.display_profiles.employees = [profile(context.person_id, context.name), profile("other-person", context.name)];
      data.display_profiles.counterparties = [profile(context.person_id, context.name), profile("other-party", context.name)];
      const html = await render(data, context);
      assert.equal((html.match(new RegExp(context.name, "g")) ?? []).length, 2, "each distinct related identity remains even with the same name");
      data.display_profiles.employees = [profile(context.person_id, "另一份姓名"), profile("", context.name)];
      data.display_profiles.counterparties = [];
      const conflicting = await render(data, context);
      assert.match(conflicting, /另一份姓名/);
      assert(conflicting.includes(context.name), "unknown identity remains visible despite matching text");
    });
    await t.test("unknown amounts and correction states are retained, with exact related payment events", async () => {
      const context = laborContext(); context.obligations[0].amount_fen = null; context.obligations[0].remaining_fen = null;
      const unknown = scenario(context); unknown.settlements.checking = true;
      const html = await render(unknown, context);
      assert.match(html, /AI 会计核对中/); assert.match(html, /待核对/);
      assert.doesNotMatch(html, /¥1,000\.00|¥500\.00/, "confirmed amount cells stay deduplicated while unknown cells remain");
      for (const [state, label] of [["withdrawn", "业务已撤回"], ["reversed", "原业务已更正"], ["over_settled", "存在超额结算"]]) {
        const data = scenario(); data.settlements.obligations[0].settlement_status = state;
        const html = await render(data);
        assert(html.includes(label), `${state} remains visible`);
        assert.doesNotMatch(html, /¥4,500\.00|¥1,000\.00|¥500\.00|¥3,000\.00/, "exceptional status does not restore identical amount cells");
      }
      for (const changes of [{ direction: "unknown", category_key: "unknown" }, { direction: "receivable" }, { category_key: "supplier_payables" }]) {
        const data = scenario(); Object.assign(data.settlements.obligations[0], changes);
        const html = await render(data);
        for (const amount of ["¥4,500.00", "¥1,000.00", "¥500.00", "¥3,000.00"])
          assert(html.includes(amount), `${JSON.stringify(changes)} retains ${amount}`);
        if (changes.direction === "unknown") assert.match(html, /AI 会计核对中/);
      }
      const capitalizedContext = laborContext(); capitalizedContext.capitalized = true;
      assert.match(await render(scenario(), capitalizedContext), /¥4,500\.00/, "capitalization mismatch keeps distinct progress visible");
      const data = scenario();
      data.collections.settlement_events = { items: [{ id: "labor-payment", subject_id: "payment", source_subject_id: "labor",
        posting_period: "2026-01", direction: 1, signed_amount_fen: "9007199254740993", relation_state: "unresolved", kind: "cash_payment", name: "net", mode: "payment" }],
        page: { total_count: 1, filtered_count: 1, returned_count: 1, has_more: false, next_cursor: null } };
      const event = await render(data);
      assert.match(event, /AI 会计核对中/); assert.match(event, /¥90,071,992,547,409\.93/);
    });
  } finally { await server.close(); delete globalThis.laborDetailScenario; }
});
