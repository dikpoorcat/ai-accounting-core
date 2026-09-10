<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import {
  fetchLocalCompanies,
  fetchLocalLedger,
  fetchLocalOverview,
  fetchLocalTrace,
  localBusinessName,
  localErrorMessage,
  type LocalCompany,
  type LocalOverview,
  type LocalTrace,
  type LocalVoucher,
} from "../api/localKernel";
import { formatFen } from "../utils/money";

const accountNames: Record<string, string> = {
  "1001": "库存现金", "1002": "银行存款", "1012": "其他货币资金",
  "1122": "应收账款", "1123": "预付账款", "1221": "其他应收款",
  "122105": "代收代付应收款", "1601": "固定资产", "1602": "累计折旧",
  "1604": "待启用固定资产", "1701": "无形资产", "1702": "累计摊销",
  "189901": "外购无形资产项目成本", "2001": "短期借款", "2202": "应付账款",
  "2203": "合同负债及预收款", "221101": "应付工资", "221102": "应付单位社保",
  "221103": "应付单位公积金", "222101": "应交增值税", "222102": "应交附加税费",
  "222103": "应交个人所得税", "222104": "待转销项税额", "222106": "应交企业所得税",
  "2231": "应付利息", "2241": "其他应付款", "224101": "应付员工款",
  "224102": "代扣个人社保", "224103": "代扣个人公积金", "224104": "应付个人劳务报酬",
  "224105": "代收代付应付款", "2501": "长期借款", "3001": "实收资本",
  "4301": "开发资本化支出", "5001": "主营业务收入", "5401": "主营业务成本",
  "540101": "主营业务成本—职工薪酬", "540102": "主营业务成本—折旧",
  "540103": "主营业务成本—摊销", "540104": "主营业务成本—个人劳务",
  "5403": "税金及附加", "5601": "销售费用", "560101": "销售费用—职工薪酬",
  "560102": "销售费用—折旧", "560103": "销售费用—摊销", "560104": "销售费用—个人劳务",
  "5602": "管理费用", "560201": "管理费用—职工薪酬", "560202": "管理费用—折旧",
  "560203": "管理费用—摊销", "560204": "管理费用—个人劳务", "5603": "财务费用",
  "571101": "资产处置损失", "571102": "无形资产报废损失", "5801": "所得税费用",
  "6301": "营业外收入", "630101": "资产处置收益",
};

const cashflowNames: Record<string, string> = {
  operating: "经营活动", customer_receipts: "客户收付款", operating_payments: "经营付款",
  pass_through_receipts: "代收款", pass_through_payments: "代付款",
  financing_receipts: "股东投入及借款", financing_repayment: "归还股东款",
  asset_acquisition: "购建资产", asset_disposal: "处置资产",
  loan_receipts: "取得借款", loan_repayment: "归还借款本金", interest_payments: "支付利息",
  tax_payments: "税费收付款", payroll: "职工薪酬", labor: "个人劳务",
  other_operating_receipts: "其他经营收入", employee_payments: "职工薪酬收付",
  employee_reimbursement: "员工垫付款报销", owner_reimbursement: "归还负责人垫付款",
};

const valueNames: Record<string, string> = {
  gross_fen: "价税总额", net_sales_fen: "计税销售额", accrued_vat_fen: "确认增值税",
  payable_vat_fen: "应缴增值税", relief_fen: "增值税减免", surtax_fen: "附加税费",
  amount_fen: "金额", cost_fen: "资产原值", carrying_fen: "账面价值",
  consumption_fen: "本月折旧摊销", closing_accumulated_fen: "累计折旧摊销",
  gain_loss_fen: "处置损益", interest_fen: "利息", principal_fen: "本金",
  capitalized_fen: "已确认项目成本", released_fen: "转费用金额",
  cumulative_assessed_fen: "累计所得税确认金额", change_fen: "本次调整金额",
  overpayment_fen: "超付追收金额", net_pay_fen: "应发净额", net_fen: "应发净额",
  gross_salary_fen: "应发工资", gross_wage_fen: "应发工资", gross_pay_fen: "应发薪酬",
  gross_amount_fen: "税前金额", income_tax_fen: "个人所得税",
  tax_fen: "个人所得税", employee_social_fen: "个人社保", employer_social_fen: "单位社保",
  theoretical_tax_fen: "按规则计算的个税", unwithheld_tax_fen: "实际未扣个税差异",
  employee_housing_fen: "个人公积金", employer_housing_fen: "单位公积金",
};

const now = new Date();
const initialQuery = new URLSearchParams(window.location.search);
const initialMonth = initialQuery.get("period") ?? "";
const monthPattern = /^[0-9]{4}-(0[1-9]|1[0-2])$/;
const period = ref(monthPattern.test(initialMonth) ? initialMonth
  : `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`);
const companyId = ref("");
const companies = ref<LocalCompany[]>([]);
const overview = ref<LocalOverview | null>(null);
const vouchers = ref<LocalVoucher[]>([]);
const trace = ref<LocalTrace | null>(null);
const ready = ref(false);
const loadingCompanies = ref(true);
const loading = ref(false);
const ledgerOpen = ref(false);
const loadingLedger = ref(false);
const hasMoreVouchers = ref(false);
const traceOpen = ref(false);
const loadingTrace = ref(false);
const companyError = ref("");
const overviewError = ref("");
const ledgerError = ref("");
const traceError = ref("");
const traceSection = ref<HTMLElement | null>(null);
const theme = ref(document.documentElement.dataset.theme === "dark" ? "dark" : "light");
let companyRequest: AbortController | null = null;
let overviewRequest: AbortController | null = null;
let ledgerRequest: AbortController | null = null;
let traceRequest: AbortController | null = null;

const company = computed(() => companies.value.find((item) => item.id === companyId.value));
const periodLabel = computed(() => `${period.value.slice(0, 4)} 年 ${Number(period.value.slice(5))} 月`);
const debits = computed(() => (overview.value?.accounts ?? []).reduce((sum, row) => sum + BigInt(row.debit), 0n));
const credits = computed(() => (overview.value?.accounts ?? []).reduce((sum, row) => sum + BigInt(row.credit), 0n));
const cashChange = computed(() => (overview.value?.cashflow ?? []).reduce((sum, row) => sum + BigInt(row.amount), 0n));
const pendingCount = computed(() => overview.value?.pending_count ?? overview.value?.pending.length ?? 0);
const evidenceCount = computed(() => new Set(trace.value?.facts.flatMap((item) => item.evidence)).size);
const traceValues = computed(() => Object.entries(trace.value?.calculation.outcome.values ?? {})
  .filter(([key, value]) => valueNames[key] && typeof value === "string" && /^-?\d+$/.test(value))
  .map(([key, value]) => ({ label: valueNames[key], amount: formatFen(String(value)) })));

function accountName(code: string): string {
  return accountNames[code] ?? "其他科目";
}

function cashflowName(code: string): string {
  return cashflowNames[code] ?? "其他现金流项目";
}

function moneyOrDash(amount: string): string {
  return BigInt(amount) === 0n ? "—" : formatFen(amount);
}

function changeTheme(): void {
  theme.value = theme.value === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = theme.value;
}

async function loadCompanies(): Promise<void> {
  companyRequest?.abort();
  const controller = new AbortController();
  companyRequest = controller;
  loadingCompanies.value = true;
  companyError.value = "";
  try {
    const rows = await fetchLocalCompanies(controller.signal);
    if (controller.signal.aborted) return;
    companies.value = rows;
    const requested = initialQuery.get("company_id");
    companyId.value = rows.some((item) => item.id === requested) ? requested! : (rows[0]?.id ?? "");
    ready.value = true;
  } catch (error: unknown) {
    if (!controller.signal.aborted) companyError.value = localErrorMessage(error);
  } finally {
    if (!controller.signal.aborted) loadingCompanies.value = false;
  }
}

async function loadOverview(): Promise<void> {
  overviewRequest?.abort();
  ledgerRequest?.abort();
  traceRequest?.abort();
  overview.value = null;
  vouchers.value = [];
  trace.value = null;
  traceOpen.value = false;
  loadingLedger.value = false;
  hasMoreVouchers.value = false;
  overviewError.value = "";
  ledgerError.value = "";
  if (!ready.value || !companyId.value || !monthPattern.test(period.value)) return;
  const controller = new AbortController();
  overviewRequest = controller;
  loading.value = true;
  const query = new URLSearchParams({ company_id: companyId.value, period: period.value });
  window.history.replaceState(null, "", `${window.location.pathname}?${query}`);
  document.title = `${company.value?.name ?? "本地财务"} · 本地财务工作台`;
  try {
    const result = await fetchLocalOverview(companyId.value, period.value, controller.signal);
    if (!controller.signal.aborted) overview.value = result;
    if (ledgerOpen.value && !controller.signal.aborted) await loadLedger(false);
  } catch (error: unknown) {
    if (!controller.signal.aborted) overviewError.value = localErrorMessage(error);
  } finally {
    if (!controller.signal.aborted) loading.value = false;
  }
}

async function loadLedger(append: boolean): Promise<void> {
  ledgerRequest?.abort();
  const controller = new AbortController();
  ledgerRequest = controller;
  ledgerOpen.value = true;
  loadingLedger.value = true;
  ledgerError.value = "";
  const cursor = append ? (vouchers.value.at(-1)?.number ?? 0) : 0;
  try {
    const rows = await fetchLocalLedger(companyId.value, period.value, cursor, controller.signal);
    if (controller.signal.aborted) return;
    vouchers.value = append ? [...vouchers.value, ...rows] : rows;
    hasMoreVouchers.value = rows.length === 50;
  } catch (error: unknown) {
    if (!controller.signal.aborted) ledgerError.value = localErrorMessage(error);
  } finally {
    if (!controller.signal.aborted) loadingLedger.value = false;
  }
}

async function openTrace(calculationId: string): Promise<void> {
  traceRequest?.abort();
  const controller = new AbortController();
  traceRequest = controller;
  trace.value = null;
  traceOpen.value = true;
  loadingTrace.value = true;
  traceError.value = "";
  await nextTick();
  traceSection.value?.focus({ preventScroll: true });
  traceSection.value?.scrollIntoView({ block: "start" });
  try {
    const result = await fetchLocalTrace(companyId.value, calculationId, controller.signal);
    if (!controller.signal.aborted) trace.value = result;
  } catch (error: unknown) {
    if (!controller.signal.aborted) traceError.value = localErrorMessage(error);
  } finally {
    if (!controller.signal.aborted) loadingTrace.value = false;
  }
}

function closeTrace(): void {
  traceRequest?.abort();
  traceOpen.value = false;
  trace.value = null;
}

watch([companyId, period, ready], () => { void loadOverview(); });
onMounted(() => { void loadCompanies(); });
onBeforeUnmount(() => {
  for (const request of [companyRequest, overviewRequest, ledgerRequest, traceRequest]) request?.abort();
});
</script>

<template>
  <a class="skip-link" href="#local-content">跳到主要内容</a>
  <div class="local-workspace">
    <header class="local-topbar">
      <a class="local-brand" href="#local-content"><span aria-hidden="true">财</span>本地财务工作台</a>
      <div class="local-top-actions">
        <span class="readonly-tag">只读查看</span>
        <button class="local-button quiet" type="button" @click="changeTheme">{{ theme === 'light' ? '深色外观' : '浅色外观' }}</button>
      </div>
    </header>

    <main id="local-content">
      <header class="local-heading">
        <div>
          <p class="local-eyebrow">账务与依据</p>
          <h1>{{ company?.name ?? '本地公司账务' }}</h1>
          <p class="local-description">查看已发布结果，追溯采用的事实与规则。</p>
        </div>
        <div class="local-filters" :aria-busy="loadingCompanies">
          <label>公司<select v-model="companyId" :disabled="loadingCompanies || companies.length === 0">
            <option v-if="companies.length === 0" value="">{{ loadingCompanies ? '正在读取…' : '暂无已登记公司' }}</option>
            <option v-for="item in companies" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select></label>
          <label>核算月份<input v-model="period" type="month" min="0001-01" max="9999-12" :disabled="!companyId"></label>
          <button class="local-button" type="button" :disabled="loading || !companyId" @click="loadOverview">{{ loading ? '读取中…' : '刷新' }}</button>
        </div>
      </header>

      <div v-if="companyError" class="local-error" role="alert"><p>{{ companyError }}</p><button class="local-button" type="button" @click="loadCompanies">重新读取</button></div>
      <div v-else-if="ready && companies.length === 0" class="local-panel local-empty">目录中还没有公司。登记公司后，可在这里查看已发布账务。</div>
      <div v-if="overviewError" class="local-error" role="alert">{{ overviewError }}</div>
      <p v-if="loading" class="local-loading" role="status">正在读取 {{ periodLabel }} 的账务汇总…</p>

      <template v-if="overview">
        <div class="local-period-bar"><h2>{{ periodLabel }}</h2><span v-if="overview.closed !== undefined" class="readonly-tag">{{ overview.closed ? '已关账' : '开放期间' }}</span></div>
        <section class="local-metrics" aria-label="本月汇总">
          <article><span>借方发生额</span><strong>{{ formatFen(debits) }}</strong><small>本月已发布凭证</small></article>
          <article><span>贷方发生额</span><strong>{{ formatFen(credits) }}</strong><small>{{ debits === credits ? '借贷发生额相等' : '请核对借贷差额' }}</small></article>
          <article><span>现金流净变动</span><strong :class="{ negative: cashChange < 0n }">{{ formatFen(cashChange) }}</strong><small>按已发布现金流分类汇总</small></article>
          <article :class="{ attention: pendingCount > 0 }"><span>待更正事项</span><strong>{{ pendingCount }}<small> 项</small></strong><small>最新事实与当前账务待衔接</small></article>
        </section>

        <div class="local-columns">
          <section class="local-panel" aria-labelledby="accounts-heading">
            <div class="local-section-heading"><h2 id="accounts-heading">科目发生额</h2><span>人民币 · 元</span></div>
            <div class="local-table-wrap"><table><thead><tr><th>科目</th><th class="money">借方</th><th class="money">贷方</th></tr></thead><tbody>
              <tr v-for="row in overview.accounts" :key="row.account"><td>{{ accountName(row.account) }}<details class="technical-inline"><summary>科目编号</summary><code>{{ row.account }}</code></details></td><td class="money">{{ moneyOrDash(row.debit) }}</td><td class="money">{{ moneyOrDash(row.credit) }}</td></tr>
              <tr v-if="overview.accounts.length === 0"><td colspan="3" class="local-empty">本月尚无已发布的科目发生额。</td></tr>
            </tbody></table></div>
          </section>
          <section class="local-panel" aria-labelledby="cashflow-heading">
            <div class="local-section-heading"><h2 id="cashflow-heading">现金流分类</h2><span>流入为正，流出为负</span></div>
            <div class="local-table-wrap"><table><thead><tr><th>项目</th><th class="money">净额</th></tr></thead><tbody>
              <tr v-for="row in overview.cashflow" :key="row.category"><td>{{ cashflowName(row.category) }}</td><td class="money" :class="{ negative: BigInt(row.amount) < 0n }">{{ formatFen(row.amount) }}</td></tr>
              <tr v-if="overview.cashflow.length === 0"><td colspan="2" class="local-empty">本月尚无已发布的现金流分类结果。</td></tr>
            </tbody></table></div>
          </section>
        </div>

        <section class="local-panel pending-panel" aria-labelledby="pending-heading">
          <div class="local-section-heading"><h2 id="pending-heading">待更正事项</h2><span>最多展示 50 项</span></div>
          <p v-if="overview.pending.length === 0" class="local-empty">本月没有已标记的待更正事项。</p>
          <ul v-else class="local-pending"><li v-for="item in overview.pending" :key="item.subject_id"><span class="pending-dot" aria-hidden="true"/><div><strong>{{ localBusinessName(item.kind) }}</strong><p>{{ item.causes }} 项来源变化等待核对与处理。</p><details class="technical-inline"><summary>业务标识</summary><code>{{ item.subject_id }}</code></details></div><span class="pending-tag">待处理</span></li></ul>
        </section>

        <section class="local-panel" aria-labelledby="ledger-heading">
          <div class="local-section-heading"><div><h2 id="ledger-heading">本月凭证</h2><p>沿正式计算版本查看当时采用的依据。</p></div><button v-if="!ledgerOpen" class="local-button" type="button" @click="loadLedger(false)">展开凭证</button><span v-else>每次读取 50 份</span></div>
          <p v-if="!ledgerOpen" class="local-empty">需要查看明细时，展开本月凭证。</p>
          <div v-if="ledgerError" class="local-error" role="alert">{{ ledgerError }}<button class="local-button" type="button" @click="loadLedger(vouchers.length > 0)">重试</button></div>
          <div v-if="ledgerOpen" class="local-table-wrap"><table><thead><tr><th>凭证</th><th>业务</th><th class="money">借贷金额</th><th><span class="sr-only">操作</span></th></tr></thead><tbody>
            <tr v-for="row in vouchers" :key="row.id"><td>记账第 {{ row.number }} 号<span v-if="row.reverses_id" class="reversal-label">关联冲正</span></td><td>{{ localBusinessName(row.kind) }}</td><td class="money">{{ formatFen(row.total) }}</td><td class="action-cell"><button class="text-button" type="button" :aria-label="`查看第 ${row.number} 号凭证依据`" @click="openTrace(row.calculation_id)">查看依据 <span aria-hidden="true">↗</span></button></td></tr>
            <tr v-if="!loadingLedger && vouchers.length === 0 && !ledgerError"><td colspan="4" class="local-empty">本月尚无已发布凭证。</td></tr>
          </tbody></table></div>
          <div v-if="loadingLedger || hasMoreVouchers" class="local-pagination"><button class="local-button" type="button" :disabled="loadingLedger" @click="loadLedger(true)">{{ loadingLedger ? '正在读取凭证…' : '读取下一页' }}</button></div>
        </section>

        <section v-if="traceOpen" ref="traceSection" class="local-panel trace-panel" tabindex="-1" aria-labelledby="trace-heading" :aria-busy="loadingTrace">
          <div class="local-section-heading"><div><p class="local-eyebrow">正式计算记录</p><h2 id="trace-heading">{{ trace ? localBusinessName(trace.calculation.kind) : '计算依据' }}</h2></div><button class="local-button quiet" type="button" @click="closeTrace">收起依据</button></div>
          <p v-if="loadingTrace" class="local-empty" role="status">正在读取当时的事实与计算版本…</p>
          <p v-if="traceError" class="local-error" role="alert">{{ traceError }}</p>
          <template v-if="trace">
            <p class="trace-caption">关联 {{ trace.facts.length }} 个事实版本、{{ evidenceCount }} 份证据与 {{ trace.upstream.length }} 个上游计算版本。</p>
            <dl v-if="traceValues.length" class="trace-values"><div v-for="(item, index) in traceValues" :key="index"><dt>{{ item.label }}</dt><dd>{{ item.amount }}</dd></div></dl>
            <h3>凭证分录</h3>
            <div class="local-table-wrap"><table><thead><tr><th>科目</th><th class="money">借方</th><th class="money">贷方</th></tr></thead><tbody><tr v-for="(line, index) in trace.calculation.outcome.lines" :key="index"><td>{{ accountName(line.account) }}</td><td class="money">{{ moneyOrDash(line.debit) }}</td><td class="money">{{ moneyOrDash(line.credit) }}</td></tr><tr v-if="trace.calculation.outcome.lines.length === 0"><td colspan="3" class="local-empty">本次计算没有产生会计分录。</td></tr></tbody></table></div>
            <h3>采用的事实版本</h3>
            <ul class="trace-facts"><li v-for="fact in trace.facts" :key="fact.id"><div><strong>{{ localBusinessName(fact.kind) }}</strong><span>第 {{ fact.revision }} 版 · {{ fact.evidence.length }} 份证据</span></div><details><summary>查看保存内容</summary><pre>{{ JSON.stringify(fact.data, null, 2) }}</pre><details><summary>技术标识与证据摘要</summary><pre>{{ JSON.stringify({ fact_id: fact.id, subject_id: fact.subject_id, evidence: fact.evidence }, null, 2) }}</pre></details></details></li></ul>
            <div v-if="trace.upstream.length" class="trace-upstream"><h3>上游计算</h3><button v-for="(id, index) in trace.upstream" :key="id" class="local-button" type="button" @click="openTrace(id)">查看上游依据 {{ index + 1 }}</button></div>
            <details class="local-technical"><summary>技术信息与完整计算轨迹</summary><pre>{{ JSON.stringify(trace.calculation, null, 2) }}</pre></details>
          </template>
        </section>

        <footer class="local-footer"><p>汇总来自已发布账务；资料是否齐全由独立的资料核对流程确认。</p><details><summary>公司与状态版本</summary><dl><dt>统一社会信用代码</dt><dd>{{ company?.taxpayer_id }}</dd><dt>公司标识</dt><dd>{{ companyId }}</dd><dt>核算版本</dt><dd>{{ overview.epochs.accounting }}</dd><dt>资料版本</dt><dd>{{ overview.epochs.material }}</dd><dt>管理版本</dt><dd>{{ overview.epochs.management }}</dd></dl></details></footer>
      </template>
    </main>
  </div>
</template>

<style scoped>
.local-workspace { min-height: 100vh; }
.local-topbar { display: flex; justify-content: space-between; align-items: center; gap: 20px; padding: 18px max(24px, calc((100vw - 1240px) / 2)); border-bottom: 1px solid var(--line); background: var(--surface); }
.local-brand { display: flex; gap: 11px; align-items: center; color: var(--text); font-weight: 750; text-decoration: none; white-space: nowrap; }
.local-brand > span { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 10px; color: #fff; background: #16754b; }
.local-top-actions { display: flex; align-items: center; gap: 12px; }
.readonly-tag { display: inline-flex; border-radius: 999px; padding: 4px 11px; background: var(--accent-soft); color: var(--accent); font-size: 12px; white-space: nowrap; }
main { width: min(1240px, calc(100% - 48px)); margin: 0 auto; padding: 35px 0 28px; }
.local-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; margin-bottom: 28px; }
.local-eyebrow { margin: 0 0 7px; color: var(--accent); font-size: 12px; font-weight: 750; letter-spacing: .12em; }
h1 { margin: 0; font-size: clamp(25px, 2.6vw, 34px); line-height: 1.25; letter-spacing: -.025em; }
.local-description { margin: 8px 0 0; color: var(--muted); font-size: 14px; }
.local-filters { display: flex; align-items: flex-end; gap: 10px; }
.local-filters label { display: flex; flex-direction: column; gap: 5px; color: var(--muted); font-size: 12px; }
.local-filters select, .local-filters input { min-height: 38px; max-width: 220px; padding: 7px 10px; border: 1px solid var(--line-strong); border-radius: 9px; background: var(--surface); color: var(--text); font: inherit; font-size: 13px; }
.local-button { min-height: 38px; padding: 7px 14px; border: 1px solid var(--line-strong); border-radius: 9px; background: var(--surface); color: var(--text); cursor: pointer; font-size: 13px; white-space: nowrap; }
.local-button:hover:not(:disabled) { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
.local-button:disabled { opacity: .6; cursor: default; }
.local-button.quiet { border-color: transparent; background: transparent; }
.local-error { margin: 16px 0; padding: 16px 20px; border: 1px solid var(--danger); border-radius: 10px; background: var(--danger-soft); color: var(--danger); }
.local-error p { margin: 0 0 10px; }
.local-error button { margin-left: 12px; }
.local-loading { color: var(--muted); padding: 22px 0; }
.local-period-bar { display: flex; gap: 12px; align-items: center; margin-bottom: 14px; }
.local-period-bar h2 { margin: 0; font-size: 17px; font-weight: 650; }
.local-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 22px; }
.local-metrics article { display: flex; flex-direction: column; padding: 22px; border: 1px solid var(--line); border-radius: 13px; background: var(--surface); }
.local-metrics article > span { color: var(--muted); font-size: 13px; }
.local-metrics strong { margin: 9px 0 6px; font-size: clamp(19px, 2vw, 27px); font-weight: 650; letter-spacing: -.025em; overflow-wrap: anywhere; }
.local-metrics small { color: var(--muted); font-size: 12px; font-weight: 400; }
.local-metrics .attention { background: var(--warning-soft); border-color: color-mix(in srgb, var(--warning) 35%, var(--line)); }
.negative { color: var(--danger); }
.local-columns { display: grid; grid-template-columns: 1.3fr 1fr; gap: 20px; align-items: start; }
.local-panel { margin-bottom: 20px; border: 1px solid var(--line); border-radius: 13px; background: var(--surface); overflow: hidden; }
.local-section-heading { display: flex; justify-content: space-between; align-items: center; gap: 18px; padding: 20px 22px 15px; }
.local-section-heading h2 { margin: 0; font-size: 17px; font-weight: 650; }
.local-section-heading > span, .local-section-heading p { color: var(--muted); font-size: 12px; }
.local-section-heading p { margin: 5px 0 0; }
.local-section-heading .local-eyebrow { color: var(--accent); margin: 0 0 5px; }
.local-table-wrap { max-width: 100%; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { color: var(--muted); font-weight: 500; text-align: left; background: var(--surface-soft); }
th, td { padding: 12px 22px; border-bottom: 1px solid var(--line); vertical-align: top; }
tbody tr:last-child td { border-bottom: 0; }
.money { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.local-empty { padding: 26px 22px; text-align: center; color: var(--muted); font-size: 13px; }
.technical-inline { margin-top: 4px; color: var(--muted); font-size: 11px; }
summary { cursor: pointer; color: var(--muted); font-size: 12px; }
code { overflow-wrap: anywhere; }
.local-pending { margin: 0; padding: 0 22px; list-style: none; }
.local-pending li { display: flex; gap: 12px; align-items: flex-start; padding: 17px 0; border-top: 1px solid var(--line); }
.local-pending li > div { flex: 1; }
.local-pending strong { font-size: 14px; font-weight: 600; }
.local-pending p { margin: 4px 0; color: var(--muted); font-size: 13px; }
.pending-dot { width: 7px; height: 7px; margin-top: 8px; border-radius: 50%; background: var(--warning); }
.pending-tag { color: var(--warning); background: var(--warning-soft); padding: 3px 9px; border-radius: 7px; font-size: 12px; }
.action-cell { text-align: right; white-space: nowrap; }
.text-button { padding: 0; border: 0; background: transparent; color: var(--accent); font-size: 13px; cursor: pointer; }
.reversal-label { display: block; color: var(--muted); font-size: 11px; }
.local-pagination { display: flex; justify-content: center; padding: 16px; border-top: 1px solid var(--line); }
.trace-panel { scroll-margin-top: 20px; }
.trace-panel h3 { padding: 0 22px; margin: 22px 0 10px; font-size: 14px; }
.trace-caption { margin: 0; padding: 0 22px 16px; color: var(--muted); font-size: 13px; }
.trace-values { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 14px; margin: 0 22px; padding: 18px; border-radius: 9px; background: var(--surface-soft); }
.trace-values dt { color: var(--muted); font-size: 12px; }
.trace-values dd { margin: 4px 0 0; font-size: 17px; }
.trace-facts { list-style: none; margin: 0; padding: 0 22px; }
.trace-facts li { padding: 14px 0; border-top: 1px solid var(--line); }
.trace-facts li > div { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.trace-facts strong { font-size: 13px; font-weight: 600; }
.trace-facts span { color: var(--muted); font-size: 12px; }
pre { overflow: auto; max-height: 420px; padding: 14px; border-radius: 8px; background: var(--surface-soft); color: var(--muted); font-size: 12px; white-space: pre-wrap; word-break: break-all; }
.trace-upstream { padding: 0 22px 12px; }
.trace-upstream h3 { padding: 0; }
.trace-upstream button { margin: 0 8px 8px 0; }
.local-technical { border-top: 1px solid var(--line); padding: 18px 22px; }
.local-footer { color: var(--muted); font-size: 12px; }
.local-footer dl { display: grid; grid-template-columns: auto 1fr; gap: 6px 18px; }
.local-footer dd { margin: 0; overflow-wrap: anywhere; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
@media (max-width: 1000px) { .local-heading { align-items: flex-start; flex-direction: column; } .local-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 700px) { main { width: calc(100% - 28px); padding-top: 24px; } .local-topbar { padding: 14px; gap: 10px; } .local-brand { font-size: 14px; } .local-top-actions { gap: 4px; } .local-top-actions .readonly-tag { display: none; } .local-filters { flex-wrap: wrap; width: 100%; } .local-filters label:first-child { flex: 1 1 100%; } .local-filters select { max-width: none; width: 100%; } .local-columns { grid-template-columns: 1fr; gap: 0; } .local-metrics { gap: 10px; } .local-metrics article { padding: 16px; } th, td { padding: 11px 14px; } .local-section-heading { padding: 17px 14px 13px; } .local-section-heading > span { text-align: right; } .trace-facts li > div { flex-direction: column; gap: 3px; } }
</style>
