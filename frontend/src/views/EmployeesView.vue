<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import {
  fetchEmployeesDashboard,
  type EstablishedEmployeeItem,
  type EmployeesDashboardResponse,
  type PayrollSource,
  type EmployeesQuery,
} from "../api/employees";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import PeriodPreparation from "../components/PeriodPreparation.vue";
import DashboardSourceHistory from "../components/DashboardSourceHistory.vue";
import DashboardBusinessRecords from "../components/DashboardBusinessRecords.vue";
import BusinessStatusDetails from "../components/BusinessStatusDetails.vue";
import VoucherTrace from "../components/brief/VoucherTrace.vue";
import { localBusinessName } from "../api/localKernel";
import { useDashboardContext } from "../composables/useDashboardContext";
import { useDashboardSections } from "../composables/useDashboardSections";
import { fen, formatFen, formatPositiveFen } from "../utils/money";

type EmployeeFilter = "all" | "in_period" | "payroll" | "no_payroll" | "ended" | "unknown";

const route = useRoute();
const router = useRouter();
const { context, load: loadContext, refresh: refreshContext } = useDashboardContext();
const response = ref<EmployeesDashboardResponse | null>(null);
const loading = ref(false);
const error = ref("");
const filter = computed<EmployeeFilter>({
  get: () => ["all", "in_period", "payroll", "no_payroll", "ended", "unknown"].includes(String(route.query.employee_filter))
    ? route.query.employee_filter as EmployeeFilter : "all",
  set: value => { void router.push({ query: { ...route.query, employee_filter: value === "all" ? undefined : value } }); },
});
const displayMode = ref<"cards" | "list">("cards");
let controller: AbortController | null = null;
let initialized = false;
let mounted = true;
let requestGeneration = 0;
const pageLoading = ref<Record<string, boolean>>({});
const pageErrors = ref<Record<string, string>>({});
const pageControllers = new Map<string, AbortController>();
const updateNotice = ref("");
function pageKey(section: string, employeeId?: string) { return `${section}:${employeeId ?? ""}`; }
function clearPageRequests() {
  pageControllers.forEach(request => request.abort());
  pageControllers.clear(); pageLoading.value = {}; pageErrors.value = {};
}

const employees = computed(() => response.value?.data?.employees ?? null);
const data = computed(() => response.value?.data ?? null);
const workforce = computed(() => response.value?.data?.workforce_cost ?? null);
const sectionLinks = computed(() => {
  if (!employees.value || !response.value?.selected_period) return [];
  return [
    { id: "employees-overview", label: "概览" },
    { id: "employees-readiness", label: "核对事项" },
    { id: "employee-list-title", label: "员工明细" },
    ...(workforce.value?.personal_labor.items.length ? [{ id: "labor-title", label: "个人劳务" }] : []),
  ];
});
const { activeSection, focusSection } = useDashboardSections(sectionLinks, "employees-overview");
const periodOptions = computed(() => context.value?.periods ?? []);
const selectedPeriodKey = computed(
  () => response.value?.selected_period?.key ?? routePeriod() ?? "",
);
const employerContribution = computed(() =>
  employees.value
    ? employees.value.employer_social_insurance_fen === null || employees.value.employer_housing_fund_fen === null ? null : fen(employees.value.employer_social_insurance_fen) +
      fen(employees.value.employer_housing_fund_fen)
    : null,
);
const reconciliationDifference = computed(() =>
  employees.value
    ? [employees.value.ledger_cost_fen, employees.value.controlled_cost_fen, employees.value.settlement_adjustment_fen].some(value => value === null) ? null : fen(employees.value.ledger_cost_fen) -
      fen(employees.value.controlled_cost_fen) -
      fen(employees.value.settlement_adjustment_fen)
    : null,
);
const costNote = computed(() => {
  const data = employees.value;
  if (!data) return "";
  if (!data.breakdown_available) {
    return data.breakdown_reason || "账面成本暂时无法按员工维度拆分";
  }
  if (reconciliationDifference.value === null) return "现有来源尚不能完整核对逐人明细与账面成本";
  if (!data.detail_reconciled) {
    return `逐人明细与账面成本相差 ${formatPositiveFen(reconciliationDifference.value)}`;
  }
  if (fen(data.settlement_adjustment_fen) !== 0n) {
    return "含结算调整 · 明细与账面一致";
  }
  return "明细与账面一致";
});
const attentionItems = computed(() => {
  const data = employees.value;
  if (!data) return [];
  const items: string[] = [];
  if (!data.breakdown_available) {
    items.push(data.breakdown_reason || "员工薪酬成本暂时不能完整拆分到每个人。");
  } else if (!data.detail_reconciled) {
    items.push(
      `逐人薪酬明细及成本调整与账面记录相差 ${formatPositiveFen(reconciliationDifference.value)}。`,
    );
  }
  return items;
});
const filteredEmployees = computed(() => employees.value?.items ?? []);
const employeeListColumns = [
  { key: "salary", label: "应发工资", amount: (item: EstablishedEmployeeItem) => item.gross_salary_fen === null ? null : fen(item.gross_salary_fen) },
  { key: "bonus", label: "全年一次性奖金", amount: (item: EstablishedEmployeeItem) => item.annual_bonus_fen === null ? null : fen(item.annual_bonus_fen) },
  { key: "company-insurance", label: "公司社保", amount: (item: EstablishedEmployeeItem) => item.employer_social_insurance_fen === null ? null : fen(item.employer_social_insurance_fen) },
  { key: "company-fund", label: "公司公积金", amount: (item: EstablishedEmployeeItem) => item.employer_housing_fund_fen === null ? null : fen(item.employer_housing_fund_fen) },
  {
    key: "personal-contribution",
    label: "个人社保公积金",
    amount: (item: EstablishedEmployeeItem) => item.employee_social_insurance_fen === null || item.employee_housing_fund_fen === null ? null : fen(item.employee_social_insurance_fen) + fen(item.employee_housing_fund_fen),
  },
  { key: "tax", label: "工资扣税（入账）", amount: (item: EstablishedEmployeeItem) => item.individual_income_tax_fen === null ? null : fen(item.individual_income_tax_fen) },
  { key: "deductions", label: "个人扣减合计", amount: (item: EstablishedEmployeeItem) => item.personal_deduction_fen === null ? null : fen(item.personal_deduction_fen) },
  { key: "net", label: "应付净薪", amount: (item: EstablishedEmployeeItem) => item.net_salary_fen === null ? null : fen(item.net_salary_fen) },
];
const visibleListColumns = computed(() => employeeListColumns);
const employeeListStyle = computed(() => ({
  "--employee-list-columns": visibleListColumns.value.length
    ? `minmax(160px, 1.4fr) repeat(${visibleListColumns.value.length}, minmax(100px, 1fr)) 16px`
    : "minmax(160px, 1fr) 16px",
  "--employee-list-min-width": `${240 + visibleListColumns.value.length * 112}px`,
}));
const filterLabel = computed(
  () =>
    ({
      all: "全部已登记员工",
      in_period: "已确认在册",
      payroll: "本月有工资记录",
      no_payroll: "本月暂无工资记录",
      ended: "已确认不在册",
      unknown: "在册状态未确认",
    })[filter.value],
);

function routePeriod(): string | null {
  return typeof route.query.period === "string" ? route.query.period : null;
}

async function loadPeriod(periodKey: string | null) {
  const generation = ++requestGeneration;
  const selection = selectionKey();
  controller?.abort();
  clearPageRequests();
  response.value = null;
  controller = new AbortController();
  const activeController = controller;
  loading.value = true;
  error.value = "";
  try {
    const result = await fetchEmployeesDashboard(periodKey, activeController.signal, { employee_filter: filter.value });
    if (!isCurrent(generation, selection) || controller !== activeController) return;
    response.value = result;
    updateNotice.value = "";
    const resolvedPeriod = result.selected_period?.key ?? null;
    if (resolvedPeriod && routePeriod() !== resolvedPeriod) {
      await router.replace({ query: { ...route.query, period: resolvedPeriod } });
    }
  } catch (caught: unknown) {
    if (!isCurrent(generation, selection) || controller !== activeController) return;
    const message = dashboardErrorMessage(caught);
    if (message) error.value = message;
  } finally {
    if (isCurrent(generation, selection) && controller === activeController) loading.value = false;
  }
}

async function initialize() {
  initialized = true;
  const generation = requestGeneration, selection = selectionKey();
  try {
    const dashboardContext = await loadContext();
    if (!isCurrent(generation, selection)) return;
    initialized = true;
    await loadPeriod(routePeriod() ?? dashboardContext.default_period);
  } catch (caught: unknown) {
    if (!isCurrent(generation, selection)) return;
    error.value = dashboardErrorMessage(caught);
    initialized = true;
  }
}

function selectPeriod(value: string) {
  void router.push({ query: { company_id: route.query.company_id, period: value } });
}

async function refresh() {
  invalidateRequests();
  const generation = requestGeneration, selection = selectionKey();
  try {
    await refreshContext();
    if (isCurrent(generation, selection)) await loadPeriod(routePeriod());
  } catch (caught: unknown) {
    if (isCurrent(generation, selection)) error.value = dashboardErrorMessage(caught);
  }
}

function selectionKey() { return JSON.stringify([route.query.company_id, route.query.period, filter.value]); }
function isCurrent(generation: number, selection: string) { return mounted && generation === requestGeneration && selection === selectionKey(); }
function invalidateRequests() {
  requestGeneration += 1;
  controller?.abort(); controller = null;
  clearPageRequests();
  response.value = null; loading.value = false;
}

async function loadMore(section: EmployeesQuery["section"] = "employees", employeeId?: string) {
  section = section ?? "employees";
  const key = pageKey(section, employeeId);
  const current = response.value;
  const employee = employeeId ? current?.data?.employees.items.find(item => item.employee_id === employeeId) : undefined;
  const page = section === "payroll_sources" ? employee && employee.selection_status !== "unestablished" ? employee.payroll_source_page : undefined : current?.data?.collections[section ?? "employees"]?.page;
  if (!current?.data || !page?.has_more || !page.next_cursor || pageLoading.value[key]) return;
  const generation = requestGeneration, selection = selectionKey();
  const request = new AbortController(); pageControllers.set(key, request); pageLoading.value[key] = true; pageErrors.value[key] = "";
  try {
    const next = await fetchEmployeesDashboard(routePeriod(), request.signal, { section, employee_id: employeeId, employee_filter: filter.value, cursor: page.next_cursor, expected_version: current.snapshot_version });
    if (!isCurrent(generation, selection) || pageControllers.get(key) !== request || !next.data || response.value?.snapshot_version !== current.snapshot_version) return;
    if (next.snapshot_version !== current.snapshot_version) { updateNotice.value = "资料已更新，正在重新读取。"; await refresh(); return; }
    const latest = response.value;
    if (!latest.data) return;
    const collection = next.data.collections[section!];
    const collections = section === "payroll_sources" ? latest.data.collections : { ...latest.data.collections, [section!]: { ...collection, items: [...(latest.data.collections[section!]?.items ?? []), ...collection.items] } };
    let items = latest.data.employees.items;
    if (section === "employees") items = [...items, ...next.data.employees.items];
    if (section === "payroll_sources") items = items.map(item => item.employee_id !== employeeId || item.selection_status === "unestablished" ? item : { ...item, payroll_sources: [...item.payroll_sources, ...collection.items as PayrollSource[]], payroll_source_page: collection.page });
    response.value = { ...latest, data: { ...latest.data, collections, employees: { ...latest.data.employees, items },
      ...(section === "labor_sources" ? { workforce_cost: { ...latest.data.workforce_cost, personal_labor: { ...latest.data.workforce_cost.personal_labor, items: [...latest.data.workforce_cost.personal_labor.items, ...next.data.workforce_cost.personal_labor.items] } } } : {}),
    } };
  } catch (caught) {
    if (!isCurrent(generation, selection) || pageControllers.get(key) !== request) return;
    if (isDashboardSnapshotChanged(caught)) { updateNotice.value = "资料已更新，正在重新读取。"; await refresh(); }
    else pageErrors.value[key] = dashboardErrorMessage(caught);
  } finally { if (isCurrent(generation, selection) && pageControllers.get(key) === request) { pageLoading.value[key] = false; pageControllers.delete(key); } }
}

function companyContribution(item: EstablishedEmployeeItem) {
  return item.employer_social_insurance_fen === null || item.employer_housing_fund_fen === null
    ? null
    : fen(item.employer_social_insurance_fen) + fen(item.employer_housing_fund_fen);
}

function obligationLabel(name: string) {
  return ({ net: "个人应付净额", primary: "期初应付款", tax: "应缴个税", withheld_tax: "已扣个税", employee_social: "个人社保", employee_housing: "个人公积金", employer_social: "公司社保", employer_housing: "公司公积金" } as Record<string, string>)[name] ?? "其他应付款";
}

function payrollObligationLabel(source: PayrollSource, name: string) {
  return obligationLabel(name === "primary" && source.component ? source.component : name);
}

function precisionLabel(value: string | null) {
  return value ? `${value}${value.length === 7 ? "（按月确认）" : ""}` : "未提供";
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
  () => [route.query.company_id, route.query.period, filter.value],
  (value, previous) => {
    if (value.every((item, index) => item === previous[index])) return;
    invalidateRequests();
  },
  { flush: "sync" },
);
watch(
  () => [context.value?.current_company?.company_id, route.query.period, filter.value] as const,
  ([orgId]) => {
    if (initialized && orgId && orgId === route.query.company_id) void loadPeriod(routePeriod());
  },
);

onMounted(() => void initialize());
onBeforeUnmount(() => { mounted = false; invalidateRequests(); });
</script>

<template>
  <section class="employees-page">
    <DashboardModuleHeader
      title="员工与薪酬概览"
      :options="periodOptions"
      :selected="selectedPeriodKey"
      :loading="loading"
      select-label="员工查看月份"
      @change="selectPeriod"
      @refresh="refresh"
    />

    <DashboardSectionNav
      v-if="sectionLinks.length"
      :items="sectionLinks"
      :active="activeSection"
      label="员工内容导航"
      floating
      @select="focusSection"
    />

    <p v-if="updateNotice" class="muted" role="status">{{ updateNotice }}</p>
    <div v-if="loading && !response" class="state-panel" role="status">
      <strong>正在加载员工与薪酬数据…</strong>
      <span>正在读取所选月份的工资记录。</span>
    </div>

    <div v-else-if="error" class="state-panel error" role="alert">
      <strong>员工数据加载失败</strong>
      <span>{{ error }}</span>
      <button type="button" @click="refresh">重试</button>
    </div>

    <div v-else-if="response && !response.data" class="state-panel">
      <strong>还没有可查看的员工月份</strong>
      <span>开始记账后，可在这里按月查看员工与薪酬信息。</span>
    </div>

    <template v-else-if="employees && response?.selected_period">
      <section id="employees-overview" class="people-hero" aria-labelledby="people-headcount-label" tabindex="-1">
        <div>
          <span id="people-headcount-label">本月有工资记录</span>
          <strong class="people-headcount">{{ employees.payroll_count }}<small>人</small></strong>
          <p class="muted">
            已登记 {{ employees.registered_count }} 人 · {{ employees.in_period_count === null ? "在册人数未提供" : `已确认在册 ${employees.in_period_count} 人` }}
            <span v-if="employees.unknown_period_count"> · 在册状态未确认 {{ employees.unknown_period_count }} 人</span>
          </p>
        </div>
        <div class="people-cost">
          <span>本月员工薪酬成本</span>
          <strong>{{ formatFen(employees.ledger_cost_fen) }}</strong>
          <small>{{ costNote }}</small>
          <details v-if="employees.settlement_adjustment_fen === null || fen(employees.settlement_adjustment_fen) !== 0n" class="cost-details">
            <summary>查看成本组成</summary>
            <p>逐人薪酬 {{ formatFen(employees.controlled_cost_fen) }} · 结算产生的成本调整 {{ formatFen(employees.settlement_adjustment_fen) }}</p>
          </details>
        </div>
      </section>

      <section class="people-kpi-grid" aria-label="员工薪酬核心指标">
        <article class="people-kpi">
          <span>本月应发工资</span>
          <strong>{{ formatFen(employees.gross_salary_fen) }}</strong>
          <small v-if="employees.annual_bonus_fen === null || fen(employees.annual_bonus_fen)">
            另有全年一次性奖金 {{ formatFen(employees.annual_bonus_fen) }}
          </small>
        </article>
        <article class="people-kpi">
          <span>公司承担社保公积金</span>
          <strong>{{ formatFen(employerContribution) }}</strong>
        </article>
        <article class="people-kpi">
          <span>个人社保公积金及个税</span>
          <strong>{{ formatFen(employees.personal_deduction_fen) }}</strong>
          <small>其中入账个税 {{ formatFen(employees.individual_income_tax_fen) }}</small>
        </article>
        <article class="people-kpi">
          <span>工资应付净额</span>
          <strong>{{ formatFen(employees.net_salary_fen) }}</strong>
        </article>
      </section>

      <p class="scope-label">全公司 · 本月入账 · 未按员工筛选</p>
      <section id="employees-readiness" aria-label="核对事项" tabindex="-1">
        <PeriodPreparation v-if="data" :preparation="data.period_preparation" :snapshot-version="response?.snapshot_version" @changed="refresh" />

        <section v-if="attentionItems.length" class="panel attention-panel" aria-labelledby="attention-title">
          <div class="section-heading">
            <h2 id="attention-title">薪酬核对事项</h2>
            <span class="attention-count">{{ attentionItems.length }} 项</span>
          </div>
          <ul>
            <li v-for="item in attentionItems" :key="item">{{ item }}</li>
          </ul>
        </section>
      </section>

      <section class="panel employee-section" aria-labelledby="employee-list-title">
        <div class="section-heading">
          <div>
            <h2 id="employee-list-title" tabindex="-1">员工明细</h2>
          </div>
          <strong>{{ employees.registered_count }} 人已登记</strong>
        </div>
        <div class="employee-toolbar">
          <p class="muted">{{ filterLabel }} · 当前筛选共 {{ data?.collections.employees?.page.filtered_count }} 人，已加载 {{ filteredEmployees.length }} 人</p>
          <div class="employee-toolbar-controls">
            <select v-model="filter" class="control" aria-label="筛选员工">
              <option value="unknown">在册状态未确认</option>
              <option value="in_period">已确认在册</option>
              <option value="payroll">本月有工资记录</option>
              <option value="no_payroll">本月暂无工资记录</option>
              <option value="ended">已确认不在册</option>
              <option value="all">全部已登记员工</option>
            </select>
            <div class="display-mode-switch" role="group" aria-label="员工明细显示模式">
              <button type="button" :aria-pressed="displayMode === 'cards'" @click="displayMode = 'cards'">
                卡片
              </button>
              <button type="button" :aria-pressed="displayMode === 'list'" @click="displayMode = 'list'">
                列表
              </button>
            </div>
          </div>
        </div>
        <p v-if="employees.unestablished_count" class="muted">完整范围内 {{ employees.unestablished_count }} 项员工来源的冻结采用未建立，相关金额保持未知。</p>

        <div v-if="!filteredEmployees.length" class="empty-result">
          {{ filter === 'all' ? '本月没有已登记的员工记录。' : '当前筛选条件下没有员工记录。' }}
          <button v-if="filter !== 'all'" type="button" class="control" @click="filter = 'all'">查看全部员工</button>
        </div>
        <div v-else class="employee-results" :class="{ 'list-results': displayMode === 'list' }"
          :tabindex="displayMode === 'list' ? 0 : undefined" aria-label="员工明细记录">
          <div class="employee-grid" :class="{ 'employee-list': displayMode === 'list' }"
            :style="displayMode === 'list' ? employeeListStyle : undefined">
            <div v-if="displayMode === 'list'" class="employee-list-header">
              <span>员工</span>
              <span v-for="column in visibleListColumns" :id="`employee-column-${column.key}`" :key="column.key">
                {{ column.label }}
              </span>
              <span aria-hidden="true"></span>
            </div>
            <template v-for="item in filteredEmployees" :key="item.employee_id">
            <article v-if="item.selection_status === 'unestablished'" class="employee-card">
              <div v-if="displayMode === 'list'" class="employee-list-summary">
                <div><h3>{{ item.name || '姓名未提供' }}</h3><span>冻结采用未建立</span></div>
                <strong v-for="column in visibleListColumns" :key="column.key" :data-label="column.label">未建立</strong>
                <span aria-hidden="true"></span>
              </div>
              <DashboardBusinessRecords :items="[item]" :period="selectedPeriodKey" :snapshot-version="response.snapshot_version" :show-business="false" />
            </article>
            <details v-else class="employee-card">
              <summary class="employee-card-summary">
                <div v-if="displayMode === 'list'" class="employee-list-summary">
                  <div class="employee-list-identity">
                    <span
                      class="employee-status"
                      :class="item.wage_tax_scope"
                      role="img"
                      :aria-label="`核算情形：${item.wage_tax_scope_label}`"
                      :title="`核算情形：${item.wage_tax_scope_label}`"
                    ></span>
                    <div class="employee-name">
                      <span>{{ item.code }}</span>
                      <h3>{{ item.name }}</h3>
                    </div>
                  </div>
                  <div v-for="column in visibleListColumns" :key="column.key" :data-label="column.label" :aria-describedby="`employee-column-${column.key}`">
                    <strong>{{ formatFen(column.amount(item)) }}</strong>
                  </div>
                  <span class="employee-list-expand" aria-hidden="true">⌄</span>
                </div>
                <template v-else>
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
                    <span v-if="item.employment_start_date">入职 {{ precisionLabel(item.employment_start_date) }}</span>
                    <span v-if="item.employment_end_date">离职 {{ precisionLabel(item.employment_end_date) }}</span>
                    <span v-if="item.has_payroll_activity">
                      {{ item.batch_count }} 笔工资记录<span v-if="item.payroll_periods.length">
                        · 归属期 {{ item.payroll_periods.join("、") }}</span
                      >
                    </span>
                    <span v-else>本月暂无工资记录</span>
                  </div>

                  <div class="employee-pay-grid">
                    <div><span>应发工资</span><strong>{{ formatFen(item.gross_salary_fen) }}</strong></div>
                    <div v-if="item.annual_bonus_fen === null || fen(item.annual_bonus_fen)"><span>全年一次性奖金</span><strong>{{ formatFen(item.annual_bonus_fen) }}</strong></div>
                    <div><span>公司社保公积金</span><strong>{{ formatFen(companyContribution(item)) }}</strong></div>
                    <div><span>个人扣减合计</span><strong>{{ formatFen(item.personal_deduction_fen) }}</strong></div>
                    <div><span>应付净薪</span><strong>{{ formatFen(item.net_salary_fen) }}</strong></div>
                  </div>

                  <div class="employee-status-row">
                    <span class="employee-status" :class="[item.period_state, item.wage_tax_scope]">
                      {{ item.wage_tax_scope_label }}
                    </span>
                    <span>展开查看付款和薪酬明细</span>
                  </div>
                </template>
              </summary>

              <div class="employee-profile">
                <h3>本月付款概况</h3>
                <dl class="employee-profile-grid">
                  <div><dt>本月公司实际支付工资</dt><dd>{{ formatFen(item.direct_net_payments_fen) }}</dd></div>
                  <div><dt>本月代付、抵销等</dt><dd>{{ formatFen(item.other_net_settlements_fen) }}</dd></div>
                  <div><dt>本月已处理应付工资合计</dt><dd>{{ formatFen(item.recorded_net_payments_fen) }}</dd></div>
                </dl>
                <p class="muted">本月支付可包含以前月份的工资，各月份余额见下方详情。</p>
                <h3>按工资来源期查看款项</h3>
                <p class="scope-label">来源期款项 · 截至所选月末</p>
                <details v-for="source in item.payroll_sources" :key="source.source_id" class="tax-details">
                  <summary>{{ source.period }} · {{ source.label }} · 查看月末款项</summary>
                  <p v-for="(issue, issueIndex) in source.issues ?? []" :key="`issue-${issueIndex}`" class="source-issue">{{ issue.message || '本来源款项尚需核对，请查看精确依据。' }}</p>
                  <p v-if="source.opening_period" class="muted">期初接续月份 {{ source.opening_period }} · {{ obligationLabel(source.component ?? "primary") }}</p>
                  <dl v-for="obligation in source.obligations" :key="obligation.key" class="employee-profile-grid">
                    <div><dt>{{ payrollObligationLabel(source, obligation.name) }}</dt><dd>{{ formatFen(obligation.amount_fen) }}</dd></div>
                    <div><dt>公司已付款</dt><dd>{{ formatFen(obligation.paid_fen) }}</dd></div>
                    <div><dt>代付、抵销等</dt><dd>{{ formatFen(obligation.other_settled_fen) }}</dd></div>
                    <div><dt>月末未结金额</dt><dd>{{ formatFen(obligation.remaining_fen) }}</dd></div>
                  </dl>
                  <div v-for="movement in source.movements" :key="movement.id">
                    <p>{{ movement.date || `${movement.period}（按月确认）` }} · {{ movement.label }}{{ movement.reversal ? "（冲正）" : "" }} · {{ payrollObligationLabel(source, movement.obligation) }} {{ formatFen(movement.amount_fen) }}</p>
                    <p v-if="movement.relation_state === 'unresolved'">清偿关系尚未确认，未计入已结金额。</p>
                    <details><summary>查看精确来源业务</summary><p>来源业务：{{ localBusinessName(movement.source_business?.kind) }}</p><VoucherTrace v-if="movement.source_calculation_id" :calculation-id="movement.source_calculation_id" /><VoucherTrace v-if="movement.calculation_id" :calculation-id="movement.calculation_id" /></details>
                  </div>
                  <p v-if="source.movements_page" class="scope-label">相关来源历史清偿 · 截至所选月末 · 共 {{ source.movements_page.total_count }} 项，已加载 {{ source.movements.length }} 项</p>
                  <p class="muted">含关联来源明细；本来源金额见上方汇总。</p>
                  <BusinessStatusDetails v-if="source.movements_page?.has_more && source.subject_id" :subject-id="source.subject_id" :period="selectedPeriodKey" :snapshot-version="response.snapshot_version" settlement-view="historical" @changed="refresh" />
                  <details v-if="source.kind === 'payroll' || source.kind === 'payroll_bounded'" class="tax-details">
                    <summary>查看实际申报记录</summary>
                    <p class="muted">申报记录与工资实际扣税、税款缴纳分别查看。</p>
                    <p v-if="!source.declarations.length" class="muted">暂无该税期的申报记录，不能据此判断是否已经申报。</p>
                    <p v-if="source.declarations.length > 1" class="muted">该税期有多份申报记录，逐项展示供核对。</p>
                    <div v-for="declaration in source.declarations" :key="declaration.fact_id"><p>{{ declaration.tax_period }} 税期已申报 {{ formatFen(declaration.declared_tax_fen) }} · {{ declaration.date || `${declaration.recording_period} 登记` }}{{ declaration.recorded_later ? "（所选月份之后补充）" : "" }}</p><details><summary>查看采用的申报记录</summary><p>数据库现有记录，第 {{ declaration.revision }} 版 · {{ declaration.fact_id }}</p></details></div>
                  </details>
                  <details v-if="source.kind === 'payroll' || source.kind === 'payroll_bounded'" class="tax-details">
                    <summary>查看代发依据</summary>
                    <p v-if="!source.disbursements.length" class="muted">本来源没有已记录的代发方案。</p>
                    <dl v-for="basis in source.disbursements" :key="basis.calculation_id" class="employee-profile-grid">
                      <div><dt>代发方案登记月份</dt><dd>{{ basis.recording_period }}{{ basis.needs_review ? " · 依据变化，需复核" : " · 已确认方案" }}</dd></div>
                      <div v-if="!basis.matches_displayed_wage"><dt>工资来源说明</dt><dd>该方案采用后来更新的工资记录，请结合更正月份查看。</dd></div>
                      <div><dt>按申报额确定的拟发金额</dt><dd>{{ formatFen(basis.target_net_fen) }}</dd></div>
                      <div><dt>方案保留差额</dt><dd>{{ formatFen(basis.held_fen) }}</dd></div>
                    </dl>
                    <p v-if="source.disbursements.length" class="muted">代发方案表示拟发金额，实际付款以上方记录为准。</p>
                  </details>
                </details>
                <DashboardPagination :page="item.payroll_source_page" :loaded="item.payroll_sources.length" :loading="pageLoading[pageKey('payroll_sources', item.employee_id)]" :error="pageErrors[pageKey('payroll_sources', item.employee_id)]" @more="loadMore('payroll_sources', item.employee_id)" @retry="loadMore('payroll_sources', item.employee_id)" />
                <DashboardSourceHistory endpoint="employees" section="settlement_events" :entity-id="item.employee_id" :period="selectedPeriodKey" :snapshot-version="response.snapshot_version" title="当前后续事项 · 查看精确关联的清偿事件" @changed="refresh" />
                <details class="tax-details">
                  <summary>查看薪酬构成</summary>
                  <dl class="employee-profile-grid">
                    <div><dt>公司社保</dt><dd>{{ formatFen(item.employer_social_insurance_fen) }}</dd></div>
                    <div><dt>公司公积金</dt><dd>{{ formatFen(item.employer_housing_fund_fen) }}</dd></div>
                    <div><dt>个人社保</dt><dd>{{ formatFen(item.employee_social_insurance_fen) }}</dd></div>
                    <div><dt>个人公积金</dt><dd>{{ formatFen(item.employee_housing_fund_fen) }}</dd></div>
                    <div><dt>工资扣税（入账金额）</dt><dd>{{ formatFen(item.individual_income_tax_fen) }}</dd></div>
                    <div><dt>个人扣减合计</dt><dd>{{ formatFen(item.personal_deduction_fen) }}</dd></div>
                  </dl>
                </details>
                <details v-if="item.tax_details?.length" class="tax-details">
                  <summary>查看工资扣税依据</summary>
                  <p class="muted">分别显示规则计算、已有实际扣税记录和工资入账采用的金额。</p>
                  <dl class="employee-profile-grid">
                    <div><dt>工资核算使用的报税工资</dt><dd>{{ formatFen(item.tax_reported_salary_fen) }}</dd></div>
                  </dl>
                  <div v-for="detail in item.tax_details" :key="detail.calculation_id" class="tax-detail">
                    <p>{{ detail.period }}{{ detail.reversal ? " · 冲正" : "" }}</p>
                    <dl class="employee-profile-grid">
                      <div><dt>按规则计算个税</dt><dd>{{ formatFen(detail.calculated_tax_fen) }}</dd></div>
                      <div><dt>实际扣税记录</dt><dd>{{ formatFen(detail.actual_withholding_tax_fen) }}</dd></div>
                      <div><dt>工资入账采用的个税</dt><dd>{{ formatFen(detail.booked_tax_fen) }}</dd></div>
                    </dl>
                    <details><summary>查看记录标识</summary><p>{{ detail.calculation_id }} · {{ detail.actual_withholding_fact_id || "暂无实际扣税记录" }}</p></details>
                  </div>
                </details>
                <details class="tax-details">
                  <summary>查看人员资料与工资设置</summary>
                  <template v-if="displayMode === 'list'">
                    <div class="employee-meta">
                      <span>{{ item.period_state_label }}</span>
                      <span v-if="item.employment_start_date">入职 {{ precisionLabel(item.employment_start_date) }}</span>
                      <span v-if="item.employment_end_date">离职 {{ precisionLabel(item.employment_end_date) }}</span>
                      <span v-if="item.has_payroll_activity">
                        {{ item.batch_count }} 笔工资记录<span v-if="item.payroll_periods.length"> · 归属期 {{ item.payroll_periods.join("、") }}</span>
                      </span>
                      <span v-else>本月暂无工资记录</span>
                    </div>
                    <dl class="employee-profile-grid employee-list-facts">
                      <div><dt>本月公司成本</dt><dd>{{ formatFen(item.company_cost_fen) }}</dd></div>
                      <div class="employee-declaration-detail"><dt>所得适用范围</dt><dd>{{ item.wage_tax_scope_label }}</dd></div>
                    </dl>
                  </template>
                  <p v-if="item.period_state === 'unknown'" class="muted">已有资料尚不能确认本月是否在册，工资记录单独展示。</p>
                  <dl v-if="item.profile_available" class="employee-profile-grid">
                    <div>
                      <dt>社保</dt>
                      <dd>{{ participationLabel(item.social_insurance_participating, item.social_insurance_base_fen, "参保", "未参保") }}</dd>
                    </div>
                    <div>
                      <dt>住房公积金</dt>
                      <dd>{{ participationLabel(item.housing_fund_participating, item.housing_fund_base_fen, "参缴", "未参缴") }}</dd>
                    </div>
                    <div><dt>个税扣缴起点</dt><dd>{{ precisionLabel(item.tax_withholding_start_date) }}</dd></div>
                    <div><dt>费用归属</dt><dd>{{ item.expense_areas.join("、") || "未设置" }}</dd></div>
                    <div><dt>员工档案状态</dt><dd>{{ item.record_status === "active" ? "启用" : item.record_status === "inactive" ? "停用" : "未提供" }}</dd></div>
                  </dl>
                  <p v-else class="muted">
                    暂无本月有效的工资设置资料，已有工资金额仍按记账记录展示。
                  </p>
                  <p v-if="!item.profile_available && item.expense_areas.length" class="muted">费用归属：{{ item.expense_areas.join("、") }}</p>
                </details>
              </div>
            </details>
            </template>
          </div>
        </div>
      </section>

      <DashboardPagination :page="data?.collections.employees?.page" :loaded="filteredEmployees.length" :loading="pageLoading[pageKey('employees')]" :error="pageErrors[pageKey('employees')]" @more="loadMore()" @retry="loadMore()" />

      <section v-if="workforce?.personal_labor.items.length" id="labor-sources" class="panel employee-section">
        <div class="section-heading"><h2 id="labor-title" tabindex="-1">个人劳务</h2></div>
        <p class="scope-label">本月费用 {{ formatFen(workforce.personal_labor.total_fen) }} · 资产或项目 {{ formatFen(workforce.capitalized_labor_fen) }}</p>
        <details v-for="labor in workforce.personal_labor.items" :key="labor.source_id" class="employee-card">
          <summary>{{ labor.name }} · {{ labor.period }} · {{ labor.capitalized ? "计入资产或项目" : "计入费用" }} · 劳务报酬 {{ formatFen(labor.gross_fen) }}</summary>
          <div class="employee-profile">
            <p v-for="(issue, issueIndex) in labor.issues ?? []" :key="`issue-${issueIndex}`" class="source-issue">{{ issue.message || '本来源款项尚需核对，请查看精确依据。' }}</p>
            <dl v-for="obligation in labor.obligations" :key="obligation.key" class="employee-profile-grid">
              <div><dt>{{ obligationLabel(obligation.name) }}</dt><dd>{{ formatFen(obligation.amount_fen) }}</dd></div>
              <div><dt>公司已付款</dt><dd>{{ formatFen(obligation.paid_fen) }}</dd></div>
              <div><dt>代付、抵销等</dt><dd>{{ formatFen(obligation.other_settled_fen) }}</dd></div>
              <div><dt>月末未结金额</dt><dd>{{ formatFen(obligation.remaining_fen) }}</dd></div>
            </dl>
            <div v-for="movement in labor.movements" :key="movement.id">
              <p>{{ movement.date || `${movement.period}（按月确认）` }} · {{ movement.label }}{{ movement.reversal ? "（冲正）" : "" }} · {{ formatFen(movement.amount_fen) }}</p>
              <p v-if="movement.relation_state === 'unresolved'">清偿关系尚未确认，未计入已结金额。</p>
              <details><summary>查看精确来源业务</summary><p>来源业务：{{ localBusinessName(movement.source_business?.kind) }}</p><VoucherTrace v-if="movement.source_calculation_id" :calculation-id="movement.source_calculation_id" /><VoucherTrace v-if="movement.calculation_id" :calculation-id="movement.calculation_id" /></details>
            </div>
            <p v-if="labor.movements_page" class="scope-label">相关来源历史清偿 · 截至所选月末 · 共 {{ labor.movements_page.total_count }} 项，已加载 {{ labor.movements.length }} 项</p>
            <p class="muted">含关联来源明细；本来源金额见上方汇总。</p>
            <BusinessStatusDetails v-if="labor.movements_page?.has_more && labor.subject_id" :subject-id="labor.subject_id" :period="selectedPeriodKey" :snapshot-version="response.snapshot_version" settlement-view="historical" @changed="refresh" />
            <details class="tax-details">
              <summary>查看劳务扣税依据</summary>
              <p>{{ labor.withholding_label }}</p>
              <dl class="employee-profile-grid">
                <div><dt>按规则计算税额</dt><dd>{{ labor.theoretical_tax_fen === null ? "暂无测算记录" : formatFen(labor.theoretical_tax_fen) }}</dd></div>
                <div><dt>入账采用的扣税额</dt><dd>{{ formatFen(labor.booked_tax_fen) }}</dd></div>
              </dl>
            </details>
          </div>
        </details>
        <DashboardPagination :page="data?.collections.labor_sources?.page" :loaded="workforce.personal_labor.items.length" :loading="pageLoading[pageKey('labor_sources')]" :error="pageErrors[pageKey('labor_sources')]" @more="loadMore('labor_sources')" @retry="loadMore('labor_sources')" />
      </section>

      <details class="panel identity-note">
        <summary>查看人员与金额说明</summary>
        <p>{{ employees.identity_note }}</p>
        <p v-if="employees.profile_missing_count">{{ employees.profile_missing_count }} 名已确认在册人员暂无本月有效的工资设置资料。</p>
        <p>本月金额按入账月份汇总，可能包含以前月份的工资调整，所属月份可在员工详情中查看。</p>
        <p>工资应付净额与实际付款分别展示；个人劳务中计入资产或项目的金额，不重复计入本月人员费用。</p>
      </details>
    </template>
  </section>
</template>

<style scoped>
summary:focus-visible,
.employee-results:focus-visible,
[tabindex="-1"]:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
[id] { scroll-margin-top: var(--dashboard-section-offset, 72px); }
.scope-label { margin: 10px 0; color: var(--muted); font-size: 12px; }
.source-issue { color: var(--warning); }
.employee-profile h3 { margin: 16px 0 10px; font-size: 14px; }
.employee-profile-grid dd, .employee-pay-grid strong, .people-cost strong, .people-kpi strong { overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
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

.state-panel.error {
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
  padding: 23px 25px;
  border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line));
  border-radius: 20px;
  background:
    radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%),
    linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface)));
  box-shadow: var(--shadow-soft);
}

.people-hero > div,
.employee-name,
.employee-results,
.employee-profile-grid > div {
  min-width: 0;
  overflow-wrap: anywhere;
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

.employee-toolbar-controls,
.display-mode-switch {
  display: flex;
  align-items: center;
  gap: 10px;
}

.display-mode-switch {
  flex-shrink: 0;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface-soft);
}

.display-mode-switch button {
  min-height: 32px;
  padding: 0 12px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  font: inherit;
  cursor: pointer;
}

.display-mode-switch button[aria-pressed="true"] {
  background: var(--accent);
  color: var(--surface);
}

.display-mode-switch button:focus-visible,
.employee-card-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}

.employee-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: 10px;
}

.employee-grid.employee-list {
  grid-template-columns: minmax(0, 1fr);
  min-width: var(--employee-list-min-width);
  gap: 0;
}

.list-results {
  overflow-x: auto;
}

.employee-list-header,
.employee-list-summary {
  display: grid;
  grid-template-columns: var(--employee-list-columns);
  align-items: center;
  gap: 12px;
}

.employee-list-header {
  padding: 10px 17px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
}

.employee-list .employee-card {
  border-radius: 0;
  border-width: 0 0 1px;
  background: var(--surface);
}

.employee-list .employee-card-summary {
  padding: 12px 17px;
}

.employee-list > article > .employee-list-summary {
  padding: 12px 17px;
}

.employee-list .employee-profile-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.employee-list .employee-list-facts {
  margin-bottom: 16px;
}

.employee-list-summary > div {
  display: grid;
  min-width: 0;
  gap: 4px;
  overflow-wrap: anywhere;
}

.employee-list-summary > .employee-list-identity {
  display: flex;
  align-items: center;
  gap: 9px;
}

.employee-list-header > span:not(:first-child):not(:last-child),
.employee-list-summary > div:not(:first-child),
.employee-list-summary > strong {
  text-align: right;
}

.employee-list .employee-declaration-detail {
  grid-column: span 3;
}

.employee-list .employee-status.not_applicable::before {
  background: var(--muted);
}

.employee-list .employee-status.mixed::before {
  background: var(--info);
}

.employee-list-summary strong,
.employee-list-summary h3 {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.employee-list-expand {
  color: var(--muted);
  font-size: 20px;
}

.employee-list .employee-card[open] .employee-list-expand {
  transform: rotate(180deg);
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

.employee-card > summary:not(.employee-card-summary) {
  padding: 16px;
  cursor: pointer;
  overflow-wrap: anywhere;
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
  min-width: 0;
  justify-items: end;
  overflow-wrap: anywhere;
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
  grid-template-columns: repeat(4, minmax(0, 1fr));
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

.employee-status.contributions_only::before {
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

.tax-details {
  min-width: 0;
  padding-top: 12px;
  border-top: 1px solid var(--line);
  overflow-wrap: anywhere;
}

.tax-details > summary,
.identity-note > summary,
.cost-details > summary {
  cursor: pointer;
  color: var(--accent);
  font-size: 12px;
  font-weight: 750;
}

.tax-details > .employee-profile-grid {
  margin-top: 12px;
}

.cost-details {
  margin-top: 8px;
  font-size: 12px;
  overflow-wrap: anywhere;
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

@media (max-width: 720px) {
  .employee-grid.employee-list { min-width: 0; }
  .employee-list-header { display: none; }
  .employee-list-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .employee-list-summary > .employee-list-identity, .employee-list-summary > div:first-child { grid-column: 1 / -1; }
  .employee-list-summary > div[data-label]::before, .employee-list-summary > strong[data-label]::before { display: block; content: attr(data-label); color: var(--muted); font-size: 11px; font-weight: normal; }
  .employee-list-summary > div[data-label]::before, .employee-list-summary > strong[data-label]::before { text-align: left; }
  .employee-list .employee-profile { margin-inline: 12px; }
  .employee-list .employee-card { border: 1px solid var(--line); border-radius: 12px; margin-bottom: 8px; }
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

  .employee-toolbar-controls {
    width: 100%;
    flex-wrap: wrap;
  }

  .display-mode-switch button {
    min-height: 44px;
  }

  .employee-amount {
    justify-items: start;
    white-space: normal;
  }

  .employee-list .employee-profile-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .employee-list .employee-declaration-detail {
    grid-column: 1 / -1;
  }
}

@media (max-width: 520px) {
  .employee-pay-grid,
  .employee-profile-grid {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
