import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createMemoryHistory, createRouter } from "vue-router";

const samples = JSON.parse(readFileSync(new URL("./fixtures/dashboard-contracts.json", import.meta.url), "utf8"));
const obligation = (changes = {}) => ({ key: "selected-pay", name: "net", source_period: "2026-09", source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0", remaining_fen: "200000", settlement_status: "partial", ...changes });
function scenario(changes = {}, currentChanges = {}) {
  const data = structuredClone(samples.business_status.response.data);
  data.identity.kind = "payroll";
  data.latest_source.period = "2026-10";
  data.settlements = { cutoff_period: "2026-09", status: "established", checking: false, obligations: [obligation(changes), obligation({ key: "different-tax", name: "tax", remaining_fen: "12345678" })] };
  data.current_followups.settlements = { ...data.settlements, cutoff_period: "2026-10", obligations: [obligation({ paid_fen: "800000", remaining_fen: "0", settlement_status: "settled", ...currentChanges })] };
  data.collections.settlement_events.items = [
    { id: "payment", name: "net", mode: "payment", posting_period: "2026-09", direction: 1, relation_state: "resolved", signed_amount_fen: "600000" },
    { id: "offset", name: "tax", mode: "offset", posting_period: "2026-09", direction: 1, relation_state: "resolved", signed_amount_fen: "12300" },
    { id: "accepted", name: "employee_social", mode: "accepted", posting_period: "2026-10", direction: 1, relation_state: "resolved", signed_amount_fen: "45600" },
    { id: "advance", name: "employer_social", mode: "advance", posting_period: "2026-10", direction: 1, relation_state: "resolved", signed_amount_fen: "78900" },
    { id: "correction", name: "net", mode: "payment", posting_period: "2026-10", direction: -1, relation_state: "resolved", signed_amount_fen: "-600000" },
  ];
  data.collections.settlement_events.page = { total_count: 5, filtered_count: 5, returned_count: 5, has_more: false, next_cursor: null };
  return data;
}
const context = (changes = {}) => ({ obligationKey: "selected-pay", categoryKey: "payroll_payables", categoryLabel: "待付工资、社保与个税", direction: "payable", party: "张某", description: "9月工资", cutoffPeriod: "2026-09", currentCutoffPeriod: "2026-10", status: "partial", sourceAmountFen: "800000", paidFen: "600000", otherSettledFen: "0", outstandingFen: "200000", currentStatus: "settled", currentOutstandingFen: "0", selectedPeriodClosed: true, ...changes });

test("open item details explain the selected obligation and distinguish historical money from related business records", async (t) => {
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true },
    plugins: [{ name: "seed-owner-detail", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/components/BusinessStatusDetails.vue")) {
        return code.replace("ref<BusinessStatusData | null>(null)", "ref(globalThis.ownerDetailScenario)");
      }
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom",
  });
  try {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
    await router.push("/?company_id=co&period=2026-09");
    const { default: component } = await server.ssrLoadModule("/src/components/BusinessStatusDetails.vue");
    async function render(data, briefContext = context()) {
      globalThis.ownerDetailScenario = data;
      const app = createSSRApp(component, { subjectId: "selected", period: "2026-09", snapshotVersion: "fixture", presentation: "brief", briefContext });
      app.use(router);
      return renderToString(app);
    }
    await t.test("selected payroll amount stays separate from tax and later settlement", async () => {
      const html = await render(scenario());
      assert.doesNotMatch(html, /张某|9月工资|待付工资、社保与个税|owner-item-progress/);
      assert.doesNotMatch(html, /业务所属月份：2026年9月/);
      assert.match(html, /截至2026年9月末（关账时）/);
      const progress = html.match(/<dl class="owner-item-amounts"[^>]*>[\s\S]*?<\/dl>/)[0];
      assert.doesNotMatch(progress, /还需支付|¥2,000\.00/);
      assert.match(progress, /原应付.*¥8,000\.00/);
      assert.match(progress, /已付.*¥6,000\.00/);
      assert.doesNotMatch(progress, /123,456\.78|抵销、代付/);
      assert.match(html, /后续进展 · 截至最新月份（2026年10月末）/);
      assert.match(html, /已结清/);
      assert.doesNotMatch(html, /业务对象：|该金额入账月|以上是当前业务结果|关账月份|calculation_id|selected-pay|different-tax/);
      for (const label of ["这笔业务的相关收付", "含本业务其他款项", "实发工资", "个人所得税", "个人社保", "公司社保", "实际收付款", "款项抵销", "已确认代付款", "员工垫付款", "更正原实际收付款"]) assert(html.includes(label), label);
    });
    await t.test("receivable and deposit use collection wording", async () => {
      for (const categoryKey of ["customer_receivables", "refundable_deposit_receivables"]) {
        const html = await render(scenario(), context({ categoryKey, direction: "receivable", description: "可退保证金" }));
        assert.match(html, /原应收/); assert.match(html, /已收/);
        assert.doesNotMatch(html, /还需支付|原应付/);
      }
    });
    await t.test("supplier advances separate refunded money from offset amounts", async () => {
      const html = await render(scenario({ paid_fen: "10000", other_settled_fen: "590000" }), context({ categoryKey: "supplier_advances", direction: "receivable", categoryLabel: "待冲抵供应商预付款", description: "供应商预付款" }));
      assert.match(html, /尚未冲抵/); assert.match(html, /预付金额/); assert.match(html, /已退回.*¥100\.00/); assert.doesNotMatch(html, /已全部冲抵/); assert.match(html, /已冲抵.*¥5,900\.00/);
      assert.doesNotMatch(html, /还需收回|原应收/);
    });
    await t.test("unknown amounts stay unknown even when the list has previous known values", async () => {
      const html = await render(scenario({ source_amount_fen: null, paid_fen: null, remaining_fen: null, other_settled_fen: null }, { remaining_fen: null }));
      const progress = html.match(/<dl class="owner-item-amounts"[^>]*>[\s\S]*?<\/dl>/)[0];
      assert.equal((progress.match(/待核对/g) ?? []).length, 3);
      assert.doesNotMatch(progress, /¥0\.00|¥2,000\.00/);
      assert.match(html, /AI 会计核对中/);
    });
    await t.test("unknown detail balance is explicitly checked even if original and paid amounts are known", async () => {
      const html = await render(scenario({ remaining_fen: null }, { remaining_fen: null }));
      assert.match(html.split("后续进展")[0], /详情进度暂不能完整确认/);
      assert.match(html, /原应付.*¥8,000\.00/); assert.match(html, /已付.*¥6,000\.00/);
    });
    await t.test("missing exact obligation uses only the selected list amounts", async () => {
      const data = scenario(); data.settlements.obligations.shift(); data.current_followups.settlements.obligations = [];
      const html = await render(data);
      assert.match(html, /详情进度待核对/); assert.match(html, /原应付.*¥8,000\.00/); assert.match(html, /已付.*¥6,000\.00/);
      assert.doesNotMatch(html, /123,456\.78|业务所属月份：2026年10月/);
    });
    await t.test("closed-period correction never replaces the selected historical amount", async () => {
      const data = scenario({}, { source_amount_fen: "850000", paid_fen: "600000", remaining_fen: "250000", settlement_status: "partial" });
      const html = await render(data);
      const progress = html.match(/<dl class="owner-item-amounts"[^>]*>[\s\S]*?<\/dl>/)[0];
      assert.match(progress, /¥8,000\.00/); assert.doesNotMatch(progress, /¥8,500\.00|¥2,000\.00|¥2,500\.00/);
      assert.match(html, /后续进展[\s\S]*¥2,500\.00/);
    });
    await t.test("unchanged later progress is omitted and large integer money remains exact", async () => {
      const data = scenario({ source_amount_fen: "9007199254740993", remaining_fen: "9007199254740993" }, { source_amount_fen: "9007199254740993", remaining_fen: "9007199254740993", paid_fen: "600000", settlement_status: "partial" });
      const html = await render(data);
      assert.match(html, /¥90,071,992,547,409\.93/); assert.doesNotMatch(html, /后续进展/);
    });
    await t.test("exact duplicate text is removed while another role and month stay visible", async () => {
      const data = scenario({ source_period: "2026-08" });
      const profile = name => ({ values: { display_name: name } });
      data.display_profiles = { business: { values: { purpose: "9月工资", note: "另外记录的用途" } }, employees: [profile("张某"), profile("张小某")], counterparties: [profile("张某")], assets: [profile("设备")], fund_accounts: [profile("工资账户")] };
      const html = await render(data);
      assert.doesNotMatch(html, /用途／备注：9月工资/); assert.match(html, /用途／备注：另外记录的用途/);
      assert.match(html, /员工：张小某/); assert.match(html, /相关往来方：张某/);
      assert.match(html, /相关资产：设备/); assert.match(html, /相关账户：工资账户/);
      assert.match(html, /业务所属月份：2026年8月/);
      assert.doesNotMatch(html, /完整总计|筛选总计|本次筛选已全部加载/);
    });
    await t.test("a settled obligation with an unknown balance is not described as settled", async () => {
      const html = await render(scenario({}, { remaining_fen: null, settlement_status: "settled" }));
      const latest = html.match(/<div class="owner-item-latest"[^>]*>[\s\S]*?<\/div><\/div>/)[0];
      assert.match(latest, /AI 会计核对中/); assert.doesNotMatch(latest, /已结清|¥0\.00/);
    });
  } finally { await server.close(); delete globalThis.ownerDetailScenario; }
});
