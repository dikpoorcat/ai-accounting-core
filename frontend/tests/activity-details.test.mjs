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
  data.collections.settlement_events.items = [{ id: "tax-payment", party: "", name: "tax", purpose_label: "个人所得税", mode: "payment", posting_period: "2026-10", direction: -1, relation_state: "resolved", signed_amount_fen: "-12345" }];
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
    function relatedRows(html) {
      const records = html.split("这笔业务的相关收付")[1].match(/<ul[^>]*>([\s\S]*?)<\/ul>/)[1];
      return [...records.matchAll(/<li[^>]*>([\s\S]*?)<\/li>/g)].map(match => match[1]);
    }
    await t.test("scoped reserve expense displays its signed amount without inventing absent settlement records", async () => {
      const data = scenario([]);
      data.identity.kind = "payroll_reserve_payment";
      data.detail_scope = { voucher_version_id: "reserve-voucher", category: "expense_supplier", amount_fen: "-9007199254740993", amount_label: "费用金额" };
      data.collections.settlement_events = { items: [], page: { total_count: 0, filtered_count: 0, returned_count: 0, has_more: false, next_cursor: null } };
      const html = await render(data, activity({ detail_scope_category: "expense_supplier", voucher_version_id: "reserve-voucher", description: "费用", title: "费用" }));
      assert.match(html, /费用金额[\s\S]*−¥90,071,992,547,409\.93/);
      assert.doesNotMatch(html, /暂无相关收付记录|当前业务结果金额|本业务其他款项/);
    });
    await t.test("payment events display their exact server purpose instead of inferring from payment kind", async () => {
      const data = scenario([]); data.identity.kind = "payment";
      data.collections.settlement_events.items = ["实发工资", "实发奖金", "实发劳务款", "代收款", "代付款"].map((purpose_label, index) => ({
        id: `purpose-${index}`, party: "同名对象", name: index < 3 ? "net" : index === 3 ? "collection" : "remittance", purpose_label,
        mode: "payment", posting_period: "2026-09", direction: 1, relation_state: "resolved", signed_amount_fen: "100",
      }));
      data.collections.settlement_events.page = { total_count: 5, filtered_count: 5, returned_count: 5, has_more: false, next_cursor: null };
      const rows = relatedRows(await render(data));
      for (const [index, purpose] of ["实发工资", "实发奖金", "实发劳务款", "代收款", "代付款"].entries()) assert(rows[index].includes(purpose));
      assert.doesNotMatch(rows.join(""), /个人实发款|remittance/);
    });
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
    await t.test("mixed collection keeps the business payment and all three collected payments", async () => {
      const data = scenario([]);
      data.identity.kind = "payment";
      data.display_profiles = {};
      const amounts = ["11000", "2200", "3300", "4400"];
      data.collections.settlement_events.items = amounts.map((signed_amount_fen, index) => ({
        id: `synthetic-collection-${index}`, party: "合成收款对象", name: index === 0 ? "primary" : "collection", purpose_label: index === 0 ? "业务款项" : "代收款", mode: "payment",
        posting_period: "2026-09", direction: 1, relation_state: "resolved", signed_amount_fen,
      }));
      data.collections.settlement_events.page = { total_count: 4, filtered_count: 4, returned_count: 4, has_more: false, next_cursor: null };
      const context = activity({ subject_id: "synthetic-mixed-collection", group: "income_customer", party: "合成收款对象", title: "混合收款", description: "业务款及代收款", amount_label: "实际收付款", amount_fen: "20900" });
      const html = await render(data, context);
      const records = html.split("这笔业务的相关收付")[1].match(/<ul[^>]*>([\s\S]*?)<\/ul>/)[1];
      const rows = [...records.matchAll(/<li[^>]*>([\s\S]*?)<\/li>/g)].map(match => match[1]);
      assert.equal(rows.length, 4);
      assert.match(rows[0], /业务款项[\s\S]*¥110\.00/);
      for (const [index, amount] of ["22", "33", "44"].entries()) {
        assert.match(rows[index + 1], new RegExp(`代收款[\\s\\S]*¥${amount}\\.00`));
      }
      assert.doesNotMatch(records, /相关款项|synthetic-collection|primary|collection/);

      data.collections.settlement_events.items[0].name = "synthetic-unknown-purpose";
      data.collections.settlement_events.items[0].purpose_label = "相关款项";
      const unknown = (await render(data, context)).split("这笔业务的相关收付")[1];
      assert.match(unknown, /相关款项[\s\S]*¥110\.00/);
      assert.doesNotMatch(unknown, /synthetic-unknown-purpose/);
    });
    await t.test("batch payroll payment relates each employee name to their own amount and purpose", async () => {
      const data = scenario([]);
      data.identity.kind = "payment";
      const employees = [
        { party: "测试员工甲", amount: "12001", displayed: "120.01" },
        { party: "测试员工乙", amount: "23402", displayed: "234.02" },
        { party: "测试员工丙", amount: "34503", displayed: "345.03" },
        { party: "测试员工丁", amount: "45604", displayed: "456.04" },
      ];
      data.display_profiles.employees = employees.toReversed().map((employee, index) => ({ entity_id: `synthetic-profile-${index}`, values: { display_name: employee.party } }));
      data.collections.settlement_events.items = employees.map((employee, index) => ({
        id: `synthetic-payroll-event-${index}`, source_subject_id: `synthetic-payroll-source-${index}`,
        party: employee.party, name: "net", purpose_label: "实发工资", mode: "payment", posting_period: "2026-09", direction: 1,
        relation_state: "resolved", signed_amount_fen: employee.amount,
      }));
      data.collections.settlement_events.page = { total_count: 4, filtered_count: 4, returned_count: 4, has_more: false, next_cursor: null };
      const context = activity({ subject_id: "synthetic-payroll-batch", party: employees.map(employee => employee.party).join("、"), title: "工资付款", description: "整批工资付款", amount_label: "实际收付款", amount_fen: "115510" });
      const rows = relatedRows(await render(data, context));
      assert.equal(rows.length, 4);
      for (const [index, employee] of employees.entries()) {
        assert(rows[index].includes(employee.party));
        assert.match(rows[index], /实发工资[\s\S]*实际收付款/);
        assert(rows[index].includes(`¥${employee.displayed}`));
        for (const other of employees.filter(item => item !== employee)) assert(!rows[index].includes(other.party));
      }
      assert.doesNotMatch(rows.join(""), /synthetic-|net|payroll/);
    });
    await t.test("same employee names and same payment amounts still retain separate event rows", async () => {
      const data = scenario([]);
      data.collections.settlement_events.items = ["first", "second"].map(id => ({
        id: `synthetic-same-name-${id}`, source_subject_id: `synthetic-distinct-source-${id}`,
        party: "测试同名员工", name: "net", purpose_label: "实发工资", mode: "payment", posting_period: "2026-09", direction: 1,
        relation_state: "resolved", signed_amount_fen: "8765",
      }));
      data.collections.settlement_events.page = { total_count: 2, filtered_count: 2, returned_count: 2, has_more: false, next_cursor: null };
      const rows = relatedRows(await render(data));
      assert.equal(rows.length, 2);
      for (const row of rows) assert.match(row, /测试同名员工[\s\S]*实发工资[\s\S]*¥87\.65/);
      assert.doesNotMatch(rows.join(""), /synthetic-|first|second/);
    });
    await t.test("related payment names preserve corrections and explicit missing-name wording", async () => {
      const data = scenario([]);
      data.collections.settlement_events.items = [
        { id: "synthetic-correction", party: "测试冲正员工", direction: -1, signed_amount_fen: "-5678" },
        { id: "synthetic-missing-name", party: "员工姓名未提供", direction: 1, signed_amount_fen: "6789" },
        { id: "synthetic-no-party", party: "", direction: 1, signed_amount_fen: "7890" },
      ].map(item => ({ name: "net", mode: "payment", posting_period: "2026-09", relation_state: "resolved", ...item }));
      data.collections.settlement_events.page = { total_count: 3, filtered_count: 3, returned_count: 3, has_more: false, next_cursor: null };
      const rows = relatedRows(await render(data));
      assert.equal(rows.length, 3);
      assert.match(rows[0], /测试冲正员工[\s\S]*更正原实际收付款[\s\S]*−¥56\.78/);
      assert.match(rows[1], /员工姓名未提供[\s\S]*¥67\.89/);
      assert.match(rows[2], /对象未提供[\s\S]*¥78\.90/);
      assert.doesNotMatch(rows.join(""), /synthetic-|重新付款|重新收款/);
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
