<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { dashboardErrorMessage } from "../api/client";
import {
  fetchEmployeesDashboard,
  type EmployeeDashboardItem,
  type EmployeesDashboardResponse,
} from "../api/employees";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { fen, formatFen, formatPositiveFen } from "../utils/money";

type EmployeeFilter = "all" | "in_period" | "payroll" | "no_payroll" | "ended";

const route = useRoute();
const router = useRouter();
const { context, load: loadContext } = useDashboardContext();
const response = ref<EmployeesDashboardResponse | null>(null);
const loading = ref(false);
const error = ref("");
const filter = ref<EmployeeFilter>("in_period");
let controller: AbortController | null = null;
let initialized = false;

const employees = computed(() => response.value?.data?.employees ?? null);
const periodOptions = computed(() => context.value?.periods ?? []);
const selectedPeriodKey = computed(
  () => response.value?.selected_period?.key ?? routePeriod() ?? "",
);
const employerContribution = computed(() =>
  employees.value
    ? fen(employees.value.employer_social_insurance_fen) +
      fen(employees.value.employer_housing_fund_fen)
    : 0n,
);
const reconciliationDifference = computed(() =>
  employees.value
    ? fen(employees.value.ledger_cost_fen) -
      fen(employees.value.controlled_cost_fen) -
      fen(employees.value.settlement_adjustment_fen)
    : 0n,
);
const costNote = computed(() => {
  const data = employees.value;
  if (!data) return "";
  if (!data.breakdown_available) {
    return data.breakdown_reason || "账面成本暂时无法按员工维度拆分";
  }
  if (!data.detail_reconciled) {
    return `逐人明细与账面成本相差 ${formatPositiveFen(reconciliationDifference.value)}`;
  }
  if (fen(data.settlement_adjustment_fen) !== 0n) {
    return `逐人工资 ${formatFen(data.controlled_cost_fen)} + 结算差额 ${formatFen(data.settlement_adjustment_fen)}，与账面相符`;
  }
  return "逐人已过账工资与账面职工薪酬成本相符";
});
const attentionItems = computed(() => {
  const data = employees.value;
  if (!data) return [];
  const items: string[] = [];
  if (data.profile_missing_count) {
    items.push(
      `${data.profile_missing_count} 名本月核算范围内员工在月末没有有效工资核算配置。未据此推断社保、公积金或个税口径。`,
    );
  }
  if (data.declaration_attention_count) {
    items.push(
      `${data.declaration_attention_count} 名员工存在已过账但尚未记录个税申报完成状态的工资行。`,
    );
  }
  if (!data.breakdown_available) {
    items.push(data.breakdown_reason || "账面职工薪酬成本暂时不能拆分到逐人工资明细。");
  } else if (!data.detail_reconciled) {
    items.push(
      `逐人工资批次及结算差额与账面职工薪酬成本尚未完全勾稽，相差 ${formatPositiveFen(reconciliationDifference.value)}。`,
    );
  }
  return items;
});
const filteredEmployees = computed(() => {
  const items = employees.value?.items ?? [];
  if (filter.value === "in_period") return items.filter((item) => item.in_period);
  if (filter.value === "payroll") return items.filter((item) => item.has_payroll_activity);
  if (filter.value === "no_payroll") {
    return items.filter((item) => item.in_period && !item.has_payroll_activity);
  }
  if (filter.value === "ended") return items.filter((item) => item.period_state === "ended");
  return items;
});
const filterLabel = computed(
  () =>
    ({
      all: "全部已登记员工",
      in_period: "本月核算范围内",
      payroll: "本月有已过账工资",
      no_payroll: "本月暂无已过账工资",
      ended: "本月前已结束核算",
    })[filter.value],
);

function routePeriod(): string | null {
  return typeof route.query.period === "string" ? route.query.period : null;
}

async function loadPeriod(periodKey: string | null, force = false) {
  if (!force && response.value?.selected_period?.key === periodKey) return;
  controller?.abort();
  controller = new AbortController();
  const activeController = controller;
  loading.value = true;
  error.value = "";
  try {
    const result = await fetchEmployeesDashboard(periodKey, activeController.signal);
    if (controller !== activeController) return;
    response.value = result;
    const resolvedPeriod = result.selected_period?.key ?? null;
    if (resolvedPeriod && routePeriod() !== resolvedPeriod) {
      await router.replace({ query: { ...route.query, period: resolvedPeriod } });
    }
  } catch (caught: unknown) {
    if (controller !== activeController) return;
    const message = dashboardErrorMessage(caught);
    if (message) error.value = message;
  } finally {
    if (controller === activeController) loading.value = false;
  }
}

async function initialize() {
  try {
    const dashboardContext = await loadContext();
    initialized = true;
    await loadPeriod(routePeriod() ?? dashboardContext.default_period);
  } catch (caught: unknown) {
    error.value = dashboardErrorMessage(caught);
    initialized = true;
  }
}

function selectPeriod(value: string) {
  void router.push({ query: { ...route.query, period: value } });
}

function refresh() {
  void loadPeriod(selectedPeriodKey.value || null, true);
}

function employeeHasAttention(item: EmployeeDashboardItem) {
  return (item.in_period && !item.profile_available) || item.declaration_state === "not_declared";
}

function participationLabel(
  participating: boolean | null,
  base: string | null,
  participatingText: string,
  absentText: string,
) {
  if (participating === null) return "未设置";
  return participating ? `${participatingText} · 基数 ${formatFen(base)}` : absentText;
}

watch(
  () => route.query.org_id,
  (value, previous) => {
    if (!initialized || value === previous) return;
    controller?.abort();
    response.value = null;
    loading.value = false;
  },
);
watch(
  () => [context.value?.current_company.org_id, route.query.period] as const,
  ([orgId], [previousOrgId]) => {
    if (initialized && orgId) void loadPeriod(routePeriod(), orgId !== previousOrgId);
  },
);

onMounted(() => void initialize());
onBeforeUnmount(() => controller?.abort());
</script>

<template>
  <section class="employees-page">
    <DashboardModuleHeader
      eyebrow="员工"
      title="员工与薪酬概览"
      description="按月查看工资核算人数、职工薪酬成本和逐人薪酬构成。页面只读，不提供员工档案管理、审批或发薪操作。"
      :options="periodOptions"
      :selected="selectedPeriodKey"
      :loading="loading"
      select-label="员工查看月份"
      @change="selectPeriod"
      @refresh="refresh"
    />

    <div v-if="loading && !response" class="state-panel" role="status">
      <strong>正在加载员工与薪酬数据…</strong>
      <span>正在读取所选会计期间的已过账工资事实。</span>
    </div>

    <div v-else-if="error" class="state-panel error" role="alert">
      <strong>员工数据加载失败</strong>
      <span>{{ error }}</span>
      <button type="button" @click="refresh">重试</button>
    </div>

    <div v-else-if="response && !response.data" class="state-panel">
      <strong>还没有可查看的员工月份</strong>
      <span>生成首个会计期间后，这里会出现只读员工与薪酬信息。</span>
    </div>

    <template v-else-if="employees && response?.selected_period">
      <section class="people-hero" aria-labelledby="people-headcount-label">
        <div>
          <p class="eyebrow">
            {{ response.selected_period.label }} · 工资核算与正式账簿口径
          </p>
          <span id="people-headcount-label">本月工资核算日期范围内</span>
          <strong class="people-headcount">{{ employees.in_period_count }}<small>人</small></strong>
          <p class="muted">
            已登记 {{ employees.registered_count }} 人 · 本月有已过账工资
            {{ employees.payroll_count }} 人 · 本月暂无已过账工资
            {{ employees.without_payroll_count }} 人
          </p>
        </div>
        <div class="people-cost">
          <span>本月账面职工薪酬成本</span>
          <strong>{{ formatFen(employees.ledger_cost_fen) }}</strong>
          <small>{{ costNote }}</small>
        </div>
      </section>

      <section class="people-kpi-grid" aria-label="员工薪酬核心指标">
        <article class="people-kpi">
          <span>已过账工资毛额</span>
          <strong>{{ formatFen(employees.gross_salary_fen) }}</strong>
          <small v-if="fen(employees.annual_bonus_fen)">
            另含全年一次性奖金 {{ formatFen(employees.annual_bonus_fen) }}
          </small>
          <small v-else>本月无已过账全年一次性奖金</small>
        </article>
        <article class="people-kpi">
          <span>公司承担社保公积金</span>
          <strong>{{ formatFen(employerContribution) }}</strong>
          <small>计入公司职工薪酬成本</small>
        </article>
        <article class="people-kpi">
          <span>个人社保公积金及个税</span>
          <strong>{{ formatFen(employees.personal_deduction_fen) }}</strong>
          <small>其中个人所得税 {{ formatFen(employees.individual_income_tax_fen) }}</small>
        </article>
        <article class="people-kpi">
          <span>工资到手金额</span>
          <strong>{{ formatFen(employees.net_salary_fen) }}</strong>
          <small>来自已过账工资批次，不代表银行已付款</small>
        </article>
      </section>

      <section v-if="attentionItems.length" class="panel attention-panel" aria-labelledby="attention-title">
        <div class="section-heading">
          <div>
            <p class="eyebrow">需要核对</p>
            <h2 id="attention-title">员工核算关注事项</h2>
          </div>
          <span class="attention-count">{{ attentionItems.length }} 项</span>
        </div>
        <ul>
          <li v-for="item in attentionItems" :key="item">{{ item }}</li>
        </ul>
      </section>

      <section class="panel employee-section" aria-labelledby="employee-list-title">
        <div class="section-heading">
          <div>
            <p class="eyebrow">逐人查看 · 仅展示工资核算事实</p>
            <h2 id="employee-list-title">员工明细</h2>
          </div>
          <strong>{{ employees.registered_count }} 人已登记</strong>
        </div>
        <div class="employee-toolbar">
          <p class="muted">{{ filterLabel }} · 显示 {{ filteredEmployees.length }} 人</p>
          <select v-model="filter" class="control" aria-label="筛选员工">
            <option value="in_period">本月核算范围内</option>
            <option value="payroll">本月有已过账工资</option>
            <option value="no_payroll">本月暂无已过账工资</option>
            <option value="ended">本月前已结束核算</option>
            <option value="all">全部已登记员工</option>
          </select>
        </div>

        <div v-if="!filteredEmployees.length" class="empty-result">
          当前筛选条件下没有员工记录。
        </div>
        <div v-else class="employee-grid">
          <details
            v-for="item in filteredEmployees"
            :key="item.code"
            class="employee-card"
            :class="{ attention: employeeHasAttention(item) }"
          >
            <summary class="employee-card-summary">
              <div class="employee-card-head">
                <div class="employee-name">
                  <span>{{ item.code }}</span>
                  <h3>{{ item.name }}</h3>
                </div>
                <div class="employee-amount">
                  <span>本月公司成本</span>
                  <strong>{{ formatFen(item.company_cost_fen) }}</strong>
                </div>
              </div>

              <div class="employee-meta">
                <span>{{ item.period_state_label }}</span>
                <span>
                  核算日期 {{ item.employment_start_date }} 至
                  {{ item.employment_end_date || "未设结束日" }}
                </span>
                <span v-if="item.has_payroll_activity">
                  {{ item.batch_count }} 个已过账工资批次<span v-if="item.payroll_periods.length">
                    · 归属期 {{ item.payroll_periods.join("、") }}</span
                  >
                </span>
                <span v-else>本月暂无已过账工资</span>
              </div>

              <div class="employee-pay-grid">
                <div><span>工资毛额</span><strong>{{ formatFen(item.gross_salary_fen) }}</strong></div>
                <div><span>报税工资</span><strong>{{ formatFen(item.tax_reported_salary_fen) }}</strong></div>
                <div><span>全年一次性奖金</span><strong>{{ formatFen(item.annual_bonus_fen) }}</strong></div>
                <div><span>公司社保</span><strong>{{ formatFen(item.employer_social_insurance_fen) }}</strong></div>
                <div><span>公司公积金</span><strong>{{ formatFen(item.employer_housing_fund_fen) }}</strong></div>
                <div>
                  <span>个人社保公积金</span>
                  <strong>{{ formatFen(fen(item.employee_social_insurance_fen) + fen(item.employee_housing_fund_fen)) }}</strong>
                </div>
                <div><span>个人所得税</span><strong>{{ formatFen(item.individual_income_tax_fen) }}</strong></div>
                <div><span>个人扣减合计</span><strong>{{ formatFen(item.personal_deduction_fen) }}</strong></div>
                <div><span>到手金额</span><strong>{{ formatFen(item.net_salary_fen) }}</strong></div>
              </div>

              <div class="employee-status-row">
                <span class="employee-status" :class="[item.period_state, item.declaration_state]">
                  {{ item.declaration_label }}
                </span>
                <span>
                  {{ item.expense_areas.length ? `费用归属：${item.expense_areas.join("、")}` : "暂无费用归属" }}
                </span>
              </div>
            </summary>

            <div class="employee-profile">
              <dl v-if="item.profile_available" class="employee-profile-grid">
                <div>
                  <dt>社保</dt>
                  <dd>{{ participationLabel(item.social_insurance_participating, item.social_insurance_base_fen, "参保", "未参保") }}</dd>
                </div>
                <div>
                  <dt>住房公积金</dt>
                  <dd>{{ participationLabel(item.housing_fund_participating, item.housing_fund_base_fen, "参缴", "未参缴") }}</dd>
                </div>
                <div><dt>个税扣缴起始日</dt><dd>{{ item.tax_withholding_start_date || "未设置" }}</dd></div>
                <div>
                  <dt>居民身份口径</dt>
                  <dd>{{ item.resident_employee === null ? "未设置" : item.resident_employee ? "居民个人" : "非居民个人" }}</dd>
                </div>
                <div><dt>费用归属</dt><dd>{{ item.expense_areas.join("、") || "未设置" }}</dd></div>
                <div><dt>员工档案状态</dt><dd>{{ item.record_status === "active" ? "启用" : "停用" }}</dd></div>
              </dl>
              <p v-else class="muted">
                当前月份末未找到有效的工资核算配置；页面不会据此推断参保、缴存或税务口径。
              </p>
            </div>
          </details>
        </div>
      </section>

      <section class="panel identity-note">
        {{ employees.identity_note }} 工资到手金额来自已过账工资批次，不表示银行已经付款。
      </section>
    </template>
  </section>
</template>

<style scoped>
.employees-page {
  width: min(calc(100% - 48px), 1320px);
  min-height: 100%;
  margin: 0 auto;
  padding: 25px 0 46px;
}

.muted,
small {
  color: var(--muted);
}

.eyebrow {
  margin: 0 0 5px;
  color: var(--accent);
  font-size: 11px;
  font-weight: 850;
  letter-spacing: 0.08em;
}

.panel,
.state-panel {
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--surface);
  box-shadow: var(--shadow-soft);
}

.state-panel {
  display: grid;
  gap: 7px;
  padding: 28px;
  border-radius: 18px;
}

.state-panel span {
  color: var(--muted);
}

.state-panel.error,
.employee-card.attention {
  border-color: color-mix(in srgb, var(--warning) 52%, var(--line));
}

.state-panel button,
.control {
  min-height: 40px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface);
  color: var(--text);
  font: inherit;
}

.state-panel button {
  width: max-content;
  margin-top: 5px;
  padding: 0 14px;
  border: 0;
  background: var(--accent);
  color: var(--surface);
  cursor: pointer;
}

.people-hero {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(300px, 0.75fr);
  gap: 25px;
  min-height: 198px;
  padding: 23px 25px;
  border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line));
  border-radius: 20px;
  background:
    radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%),
    linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface)));
  box-shadow: var(--shadow-soft);
}

.people-hero p {
  margin-block: 7px 0;
  font-size: 12px;
}

.people-hero > div:first-child > span {
  color: var(--muted);
  font-size: 12px;
}

.people-headcount {
  display: block;
  margin: 7px 0 3px;
  color: var(--accent);
  font-size: clamp(36px, 4.5vw, 48px);
  line-height: 1;
  letter-spacing: -0.04em;
}

.people-headcount small {
  margin-left: 8px;
  font-size: 16px;
  letter-spacing: 0;
}

.people-cost {
  display: grid;
  align-content: center;
  align-self: stretch;
  padding: 14px;
  border: 1px solid color-mix(in srgb, var(--line) 82%, transparent);
  border-radius: 15px;
  background: color-mix(in srgb, var(--surface) 83%, transparent);
}

.people-cost span,
.people-cost small,
.people-kpi span,
.people-kpi small {
  display: block;
}

.people-cost span,
.people-kpi span {
  color: var(--muted);
  font-size: 12px;
}

.people-cost small,
.people-kpi small {
  font-size: 11px;
}

.people-cost strong {
  display: block;
  margin: 7px 0;
  color: var(--accent);
  font-size: 27px;
}

.people-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-top: 10px;
}

.people-kpi {
  position: relative;
  display: grid;
  min-width: 0;
  min-height: 126px;
  align-content: space-between;
  gap: 4px;
  overflow: hidden;
  padding: 15px 16px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--surface);
  box-shadow: var(--shadow-soft);
}

.people-kpi::before {
  position: absolute;
  top: 0;
  right: 0;
  left: 0;
  height: 3px;
  background: var(--accent);
  content: "";
}

.people-kpi:nth-child(1)::before {
  background: var(--info);
}

.people-kpi:nth-child(2)::before {
  background: var(--gold);
}

.people-kpi:nth-child(3)::before {
  background: var(--warning);
}

.people-kpi strong {
  display: block;
  margin: 4px 0;
  color: var(--accent);
  font-size: clamp(20px, 2vw, 27px);
  line-height: 1.15;
  letter-spacing: -0.025em;
}

.people-kpi:nth-child(1) strong {
  color: var(--info);
}

.people-kpi:nth-child(2) strong {
  color: var(--gold);
}

.people-kpi:nth-child(3) strong {
  color: var(--warning);
}

.attention-panel,
.employee-section,
.identity-note {
  margin-top: 12px;
  padding: 18px;
}

.attention-panel {
  border-color: color-mix(in srgb, var(--warning) 52%, var(--line));
}

.attention-panel ul {
  display: grid;
  gap: 8px;
  margin: 16px 0 0;
  padding-left: 22px;
}

.section-heading,
.employee-toolbar,
.employee-card-head,
.employee-status-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.section-heading h2,
.employee-name h3 {
  margin: 0;
}

.section-heading h2 {
  font-size: 20px;
}

.section-heading > strong {
  color: var(--muted);
  font-size: 12px;
}

.attention-count {
  padding: 3px 9px;
  border-radius: 999px;
  background: var(--warning-soft);
  color: var(--warning);
  font-size: 11px;
  font-weight: 800;
}

.employee-toolbar {
  align-items: center;
  margin: 14px 0 12px;
}

.employee-toolbar p {
  margin: 0;
}

.employee-toolbar select {
  min-width: 230px;
  padding: 0 12px;
}

.employee-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: 10px;
}

.employee-card {
  min-width: 0;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-soft);
}

.employee-card:hover,
.employee-card:focus-within,
.employee-card[open] {
  border-color: color-mix(in srgb, var(--accent) 48%, var(--line));
}

.employee-card-summary {
  display: block;
  padding: 16px;
  border-radius: inherit;
  cursor: pointer;
  list-style: none;
}

.employee-card-summary::-webkit-details-marker {
  display: none;
}

.employee-name,
.employee-amount {
  display: grid;
  gap: 3px;
}

.employee-name span,
.employee-amount span,
.employee-meta,
.employee-status-row {
  color: var(--muted);
  font-size: 12px;
}

.employee-amount {
  justify-items: end;
  white-space: nowrap;
}

.employee-amount strong {
  font-size: 21px;
}

.employee-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  margin: 12px 0;
}

.employee-pay-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px;
  overflow: hidden;
  padding: 1px;
  border-radius: 9px;
  background: var(--line);
}

.employee-pay-grid > div {
  display: grid;
  min-width: 0;
  gap: 3px;
  padding: 10px;
  background: var(--surface);
}

.employee-pay-grid span {
  color: var(--muted);
  font-size: 11px;
}

.employee-pay-grid strong {
  overflow-wrap: anywhere;
  font-size: 14px;
}

.employee-status-row {
  align-items: center;
  margin-top: 12px;
}

.employee-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.employee-status::before {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--accent);
  content: "";
}

.employee-status.not_started::before,
.employee-status.ended::before,
.employee-status.none::before {
  background: var(--muted);
}

.employee-status.not_declared::before {
  background: var(--warning);
}

.employee-profile {
  margin: 0 16px 16px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

.employee-profile-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 14px;
  margin: 0;
}

.employee-profile > p {
  margin: 0;
}

.employee-profile-grid div {
  display: grid;
  gap: 2px;
}

.employee-profile-grid dt {
  color: var(--muted);
  font-size: 11px;
}

.employee-profile-grid dd {
  margin: 0;
  font-size: 13px;
  font-weight: 700;
}

.empty-result,
.identity-note {
  color: var(--muted);
}

.empty-result {
  padding: 24px;
  border: 1px dashed var(--line);
  border-radius: 12px;
  background: var(--surface-soft);
  text-align: center;
}

.identity-note {
  font-size: 13px;
}

@media (max-width: 1080px) {
  .people-kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .employee-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .employees-page {
    width: min(calc(100% - 24px), 1320px);
    padding: 16px 0 24px;
  }

  .people-hero,
  .people-kpi-grid {
    grid-template-columns: 1fr;
  }

  .people-hero {
    gap: 13px;
    padding: 19px;
    border-radius: 17px;
  }

  .employee-toolbar,
  .employee-card-head,
  .employee-status-row {
    align-items: flex-start;
    flex-direction: column;
  }

  .employee-toolbar select {
    width: 100%;
    min-width: 0;
    min-height: 44px;
  }

  .employee-amount {
    justify-items: start;
  }
}

@media (max-width: 520px) {
  .employee-pay-grid,
  .employee-profile-grid {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
