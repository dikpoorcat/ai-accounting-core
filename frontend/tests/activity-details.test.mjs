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
const obligation = (changes = {}) => ({ key: "salary", name: "net", direction: "payable", category_key: "payroll_payables", source_period: "2026-09", source_amount_fen: "800000", paid_fen: "600000", other_settled_fen: "0", remaining_fen: "200000", settlement_status: "partial", ...changes });
const activity = (changes = {}) => ({ key: "occurrence", subject_id: "salary-business", voucher_version_id: "occurrence", group: "payroll", party: "张某", title: "工资", description: "9月工资", date: null, recognition: { period: "2026-09" }, state: "已入账", amount_label: "税前工资", amount_fen: "800000", ...changes });
function scenario(items = [obligation(), obligation({ key: "tax", name: "tax", source_amount_fen: "12345", paid_fen: "0", remaining_fen: "12345", settlement_status: "open" })]) {
  const data = structuredClone(samples.business_status.response.data);
  data.identity.kind = "payroll";
  data.latest_source = { deleted: false, period: "2026-10" };
  data.current_business_result = { amount_fen: "9999999", amount_label: "当前业务结果金额", posting_period: "2026-10" };
  data.settlements = { cutoff_period: "2026-09", status: "established", checking: false, obligations: items };
  data.current_followups.settlements = { ...data.settlements, cutoff_period: "2026-10", obligations: structuredClone(items) };
  data.collections.settlement_events.items = [{ id: "tax-payment", name: "tax", mode: "payment", posting_period: "2026-10", direction: -1, relation_state: "resolved", signed_amount_fen: "-12345" }];
  data.collections.settlement_events.page = { total_count: 1, filtered_count: 1, returned_count: 1, has_more: false, next_cursor: null };
  return data;
}

test("activity details only add information to the exact occurrence row", async (t) => {
  const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), configFile: false,
    optimizeDeps: { noDiscovery: true }, plugins: [{ name: "seed-activity-detail", enforce: "pre", transform(code, id) {
      if (id.replaceAll("\\", "/").endsWith("/src/components/BusinessStatusDetails.vue")) return code.replace("ref<BusinessStatusData | null>(null)", "ref(globalThis.activityDetailScenario)");
    } }, vue()], server: { middlewareMode: true, hmr: false, ws: false }, appType: "custom" });
  try {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: {} }] });
    await router.push("/?company_id=co&period=2026-09");
    const { default: component } = await server.ssrLoadModule("/src/components/BusinessStatusDetails.vue");
    async function render(data = scenario(), activityContext = activity()) {
      globalThis.activityDetailScenario = data;
      const app = createSSRApp(component, { subjectId: activityContext.subject_id, period: "2026-09", presentation: "brief", activityContext, expanded: true }); app.use(router);
      return renderToString(app);
    }
    await t.test("salary, tax and current result never replace the clicked amount", async () => {
      const html = await render();
      const main = html.split("整笔业务的款项进度")[0];
      assert.match(main, /事项说明：9月工资/);
      assert.doesNotMatch(main, /本次事项|张某|税前工资|按月确认|owner-activity-summary/);
      assert.match(html, /整笔业务的款项进度 · 截至2026年9月末/);
      assert.match(html, /实发工资[\s\S]*还需支付[\s\S]*¥2,000\.00/);
      assert.match(html, /个人所得税[\s\S]*¥123\.45/);
      assert.doesNotMatch(html, /当前业务结果金额|该金额入账月|业务对象：|后续进展|尚未结算|salary-business/);
      assert.match(html, /这笔业务的相关收付[\s\S]*含本业务其他款项[\s\S]*更正原实际收付款/);
    });
    await t.test("different occurrences of the same business retain separate signed values", async () => {
      for (const amount of ["600000", "-800000", "0", null, "9007199254740993"]) {
        const html = await render(scenario(), activity({ key: `event-${amount}`, amount_fen: amount, state: amount === "-800000" ? "更正原业务" : "已入账" }));
        const main = html.split("整笔业务的款项进度")[0];
        assert.doesNotMatch(main, /本次事项|税前工资|更正原业务|重新付款|重新收款/);

      }
    });
    await t.test("income confirmation and actual payment preserve explicit amount meaning", async () => {
      for (const amount_label of ["含税收入确认额", "实际收付款", "整批确认成本", "内部划转金额"]) {
        const html = await render(scenario([]), activity({ amount_label, date: "2026-09-15" }));
        assert(!html.split("这笔业务的相关收付")[0].includes(amount_label)); assert.doesNotMatch(html, /业务时间：2026-09-15/);
        assert.doesNotMatch(html, /整笔业务的款项进度|已结清|还需支付/);
      }
    });
    await t.test("only exact duplicate purposes and unambiguous row objects disappear", async () => {
      const data = scenario(); const profile = name => ({ values: { display_name: name } });
      data.display_profiles = { business: { values: { purpose: "9月工资", note: "另外记录的用途" } }, employees: [profile("张某"), profile("张小某")], assets: [profile("设备")], fund_accounts: [profile("工资账户")] };
      const html = await render(data); assert.doesNotMatch(html, /用途／备注：9月工资|员工：张某/);
      assert.match(html, /用途／备注：另外记录的用途|员工：张小某/); assert.match(html, /相关资产：设备/); assert.match(html, /相关账户：工资账户/);
      data.display_profiles.counterparties = [profile("张某")]; const roles = await render(data);
      assert.match(roles, /员工：张某、张小某/); assert.match(roles, /往来方：张某/);
    });
    await t.test("a full occurrence description moves into details and deduplicates only visible complete texts", async () => {
      const data = scenario([]); data.display_profiles.business.values = { purpose: "9月工资", note: "9月工资补充用途" };
      const html = await render(data);
      assert.equal((html.match(/事项说明：9月工资/g) ?? []).length, 1);
      assert.doesNotMatch(html, /用途／备注：9月工资<\/p>/); assert.match(html, /用途／备注：9月工资补充用途/);
      assert.doesNotMatch(await render(data, activity({ description: "工资" })), /事项说明：工资<\/p>/);
      data.display_profiles.employees = [{ entity_id: "employee-a", values: { display_name: "张某" } }, { entity_id: "employee-b", values: { display_name: "张某" } }];
      assert.match(await render(data), /员工：张某、张某/);
    });
    await t.test("later business withdrawal is retained only when it adds to the historical row", async () => {
      const data = scenario(); data.latest_source.deleted = true;
      const html = await render(data); assert.match(html, /这笔业务目前已撤回；原行保留本次发生记录/);
      const alreadyWithdrawn = await render(data, activity({ state: "业务已撤回" }));
      assert.doesNotMatch(alreadyWithdrawn, /这笔业务目前已撤回/);
    });
    await t.test("receivables and deposits use explicit collection direction", async () => {
      for (const category_key of ["customer_receivables", "refundable_deposit_receivables"]) {
        const html = await render(scenario([obligation({ direction: "receivable", category_key, name: "primary" })]));
        assert.match(html, /还需收回/); assert.match(html, /原应收/); assert.match(html, /已收/); assert.doesNotMatch(html, /还需支付|原应付/);
      }
    });
    await t.test("advances distinguish refunds from offsets", async () => {
      const html = await render(scenario([obligation({ direction: "receivable", category_key: "supplier_advances", paid_fen: "10000", other_settled_fen: "590000" })]));
      assert.match(html, /尚未冲抵/); assert.match(html, /已冲抵.*¥5,900\.00/); assert.match(html, /已退回.*¥100\.00/);
      assert.doesNotMatch(html, /还需收回|原应收/);
    });
    await t.test("unknown direction does not default to payable or settled", async () => {
      const html = await render(scenario([obligation({ direction: "unknown", category_key: "unknown", remaining_fen: null, paid_fen: null, settlement_status: "settled" })]));
      assert.match(html, /未结金额[\s\S]*待核对/); assert.match(html, /收付分类待核对/); assert.match(html, /AI 会计核对中/);
      assert.doesNotMatch(html, /还需支付|已结清/);
    });
    await t.test("later settlement and closed-period correction preserve historical progress by key", async () => {
      const data = scenario();
      data.frozen_adoption = { amount_fen: "123", amount_label: "关账结果", close_period: "2026-09" };
      data.current_followups.settlements.obligations = [obligation({ key: "tax", name: "tax", remaining_fen: "999" }), obligation({ remaining_fen: "0", paid_fen: "800000", settlement_status: "settled" })];
      const html = await render(data);
      const [history, latest] = html.split("后续进展 ·");
      assert.match(history, /还需支付[\s\S]*¥2,000\.00/); assert.doesNotMatch(history, /关账结果/);
      assert.match(latest, /截至最新月份（2026年10月末）/); assert.match(latest, /实发工资[\s\S]*已结清[\s\S]*¥0\.00/);
    });
    await t.test("missing latest obligation stays uncertain and new obligations are labelled", async () => {
      const data = scenario([obligation()]);
      data.current_followups.settlements.obligations = [obligation({ key: "new-tax", name: "tax" })];
      const html = await render(data); assert.match(html, /最新进度待核对/); assert.match(html, /后续新增款项/);
    });
    await t.test("no details still preserves known occurrence for loading failures", async () => {
      const html = await render(null); assert.doesNotMatch(html, /本次事项|¥8,000\.00|款项进度/);
    });
    await t.test("normal unpaid amounts are amber and over-settlement is explicit", async () => {
      const normal = await render(scenario([obligation({ settlement_status: "open" })]));
      assert.match(normal, /class="[^"]*pending[^"]*business-state/); assert.doesNotMatch(normal, /business-state attention|催收|到期|建议付款/);
      const excess = await render(scenario([obligation({ remaining_fen: "-100", settlement_status: "over_settled" })]));
      assert.match(excess, /存在超额结算/);
    });
  } finally { await server.close(); delete globalThis.activityDetailScenario; }
});
