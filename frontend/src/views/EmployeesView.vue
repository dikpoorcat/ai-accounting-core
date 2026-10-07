<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import {
  fetchEmployeesDashboard,
  type EstablishedEmployeeItem,
  type EmployeesDashboardResponse,
  type EmployeesQuery,
  type PersonalLaborItem,
} from "../api/employees";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import BusinessStatusDetails from "../components/BusinessStatusDetails.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { useDashboardSections } from "../composables/useDashboardSections";
import { cashFlowClass, fen, formatFen } from "../utils/money";
import { appendDashboardCollection } from "../utils/dashboardCollections";

type EmployeeFilter = "all" | "in_period" | "payroll" | "no_payroll" | "ended" | "unknown";

const route = useRoute();
const router = useRouter();
const { context, load: loadContext, refresh: refreshContext } = useDashboardContext();
const response = ref<EmployeesDashboardResponse | null>(null);
const loading = ref(false);
const error = ref("");
const displayMode = ref<"cards" | "list">("cards");
const filter = computed<EmployeeFilter>({
  get: () => ["all", "in_period", "payroll", "no_payroll", "ended", "unknown"].includes(String(route.query.employee_filter)) ? route.query.employee_filter as EmployeeFilter : "in_period",
  set: value => { void router.push({ query: { ...route.query, employee_filter: value === "in_period" ? undefined : value, employee_id: undefined } }); },
});
const focusedEmployeeId = computed(() => typeof route.query.employee_id === "string" ? route.query.employee_id : "");
let controller: AbortController | null = null;
let initialized = false;
let mounted = true;
let requestGeneration = 0;
const pageLoading = ref<Record<string, boolean>>({});
const pageErrors = ref<Record<string, string>>({});
const pageControllers = new Map<string, AbortController>();
const updateNotice = ref("");
function pageKey(section: string) { return `${section}:`; }
function clearPageRequests() {
  pageControllers.forEach(request => request.abort());
  pageControllers.clear(); pageLoading.value = {}; pageErrors.value = {};
}

const employees = computed(() => response.value?.data?.employees ?? null);
const data = computed(() => response.value?.data ?? null);
const workforce = computed(() => response.value?.data?.workforce_cost ?? null);
const personalLaborItems = computed(() => data.value?.collections.labor_sources?.items ?? []);
const sectionLinks = computed(() => {
  if (!employees.value || !response.value?.selected_period) return [];
  return [
    { id: "employees-overview", label: "概览" },
    { id: "employee-list-title", label: "员工明细" },
    ...(personalLaborItems.value.length ? [{ id: "labor-title", label: "个人劳务" }] : []),
  ];
});
const { activeSection, focusSection, focusSelectedPanel } = useDashboardSections(sectionLinks, "employees-overview");
const periodOptions = computed(() => context.value?.periods ?? []);
const selectedPeriodKey = computed(
  () => response.value?.selected_period?.key ?? routePeriod() ?? "",
);
const filteredEmployees = computed(() => data.value?.collections.employees?.items ?? []);
const employeeListColumns = [
  { key: "gross", label: "应发工资", amount: (item: EstablishedEmployeeItem) => item.gross_salary_fen },
  { key: "bonus", label: "全年一次性奖金", amount: (item: EstablishedEmployeeItem) => item.annual_bonus_fen },
  { key: "employer-social", label: "公司社保", amount: (item: EstablishedEmployeeItem) => item.employer_social_insurance_fen },
  { key: "employer-housing", label: "公司公积金", amount: (item: EstablishedEmployeeItem) => item.employer_housing_fund_fen },
  {
    key: "employee-contribution", label: "个人社保公积金",
    amount: (item: EstablishedEmployeeItem) => item.employee_social_insurance_fen === null || item.employee_housing_fund_fen === null
      ? null : fen(item.employee_social_insurance_fen) + fen(item.employee_housing_fund_fen),
  },
  { key: "tax", label: "工资扣税（入账）", amount: (item: EstablishedEmployeeItem) => item.individual_income_tax_fen },
  { key: "deductions", label: "个人扣减合计", amount: (item: EstablishedEmployeeItem) => item.personal_deduction_fen },
  { key: "net", label: "应付净薪", amount: (item: EstablishedEmployeeItem) => item.net_salary_fen },
];
const laborListColumns = [
  { key: "gross", label: "劳务报酬", amount: (item: PersonalLaborItem) => item.gross_fen },
  { key: "tax", label: "扣税（入账）", amount: (item: PersonalLaborItem) => item.booked_tax_fen },
  { key: "net", label: "应付净额", amount: (item: PersonalLaborItem) => item.net_fen },
];
const filterLabel = computed(
  () => focusedEmployeeId.value ? "已定位员工" :
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

async function loadPeriod(periodKey: string | null, contextGate?: Promise<void>) {
  const generation = ++requestGeneration;
  const selection = selectionKey();
  controller?.abort();
  clearPageRequests();
  if (!contextGate) response.value = null;
  controller = new AbortController();
  const activeController = controller;
  loading.value = true;
  error.value = "";
  try {
    const request = fetchEmployeesDashboard(periodKey, activeController.signal, { employee_filter: focusedEmployeeId.value ? "all" : filter.value, employee_id: focusedEmployeeId.value || undefined });
    const result = contextGate ? (await Promise.all([request, contextGate]))[0] : await request;
    if (!isCurrent(generation, selection) || controller !== activeController) return;
    response.value = result;
    loading.value = false;
    await nextTick();
    if (isCurrent(generation, selection) && focusedEmployeeId.value) {
      document.getElementById("employee-card-target")?.focus({ preventScroll: true });
      document.getElementById("employee-card-target")?.scrollIntoView({ block: "start", behavior: "smooth" });
    }
    updateNotice.value = "";
    const resolvedPeriod = result.selected_period?.key ?? null;
    if (resolvedPeriod && routePeriod() !== resolvedPeriod) {
      await router.replace({ query: { ...route.query, period: resolvedPeriod } });
    }
  } catch (caught: unknown) {
    if (!isCurrent(generation, selection) || controller !== activeController) return;
    response.value = null;
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
  void router.push({ query: { company_id: route.query.company_id, period: value, employee_filter: route.query.employee_filter } });
}

function refresh() { return refreshCurrent(true); }
function refreshChanged() {
  updateNotice.value = "资料已更新，正在重新读取。";
  return refreshCurrent(false);
}
async function refreshCurrent(keepContent: boolean) {
  invalidateRequests(keepContent);
  loading.value = true;
  const generation = requestGeneration, selection = selectionKey();
  try {
    const period = routePeriod();
    const company = route.query.company_id;
    if (typeof company === "string" && period && context.value?.current_company?.company_id === company
      && context.value.periods.some((item) => item.key === period)) {
      const contextGate = refreshContext().then((fresh) => {
        if (fresh.current_company?.company_id !== company || !fresh.periods.some((item) => item.key === period))
          throw new Error("当前公司或期间已变化，请重新选择。");
      });
      await loadPeriod(period, contextGate);
    } else {
      await refreshContext();
      if (isCurrent(generation, selection)) await loadPeriod(routePeriod());
    }
  } catch (caught: unknown) {
    if (isCurrent(generation, selection)) { response.value = null; loading.value = false; error.value = dashboardErrorMessage(caught); }
  }
}

function selectionKey() { return JSON.stringify([route.query.company_id, route.query.period, filter.value, focusedEmployeeId.value]); }
function isCurrent(generation: number, selection: string) { return mounted && generation === requestGeneration && selection === selectionKey(); }
function invalidateRequests(keepContent = false) {
  requestGeneration += 1;
  controller?.abort(); controller = null;
  clearPageRequests();
  if (!keepContent) response.value = null;
  loading.value = keepContent;
}

async function loadMore(section: EmployeesQuery["section"] = "employees") {
  section = section ?? "employees";
  const key = pageKey(section);
  if (section === "employees" && pageErrors.value[key] && !filteredEmployees.value.length) return loadEmployeeList();
  const current = response.value;
  const page = current?.data?.collections[section]?.page;
  if (!current?.data || !page?.has_more || !page.next_cursor || pageLoading.value[key]) return;
  const generation = requestGeneration, selection = selectionKey();
  const request = new AbortController(); pageControllers.set(key, request); pageLoading.value[key] = true; pageErrors.value[key] = "";
  try {
    const next = await fetchEmployeesDashboard(routePeriod(), request.signal, { section, employee_id: focusedEmployeeId.value || undefined, employee_filter: focusedEmployeeId.value ? "all" : filter.value, cursor: page.next_cursor, expected_version: current.snapshot_version });
    if (!isCurrent(generation, selection) || pageControllers.get(key) !== request || !next.data || response.value?.snapshot_version !== current.snapshot_version) return;
    if (next.snapshot_version !== current.snapshot_version) { await refreshChanged(); return; }
    const latest = response.value;
    if (!latest.data) return;
    const collection = next.data.collections[section!];
    if (!collection) return;
    const previous = latest.data.collections[section!];
    if (!previous) return;
    const collections = { ...latest.data.collections, [section!]: appendDashboardCollection(previous, collection) };
    response.value = { ...latest, data: { ...latest.data, collections } };
  } catch (caught) {
    if (!isCurrent(generation, selection) || pageControllers.get(key) !== request) return;
    if (isDashboardSnapshotChanged(caught)) { await refreshChanged(); }
    else pageErrors.value[key] = dashboardErrorMessage(caught);
  } finally { if (isCurrent(generation, selection) && pageControllers.get(key) === request) { pageLoading.value[key] = false; pageControllers.delete(key); } }
}

async function loadEmployeeList() {
  const current = response.value;
  if (!current?.data || loading.value) return;
  const key = pageKey("employees"), generation = requestGeneration, selection = selectionKey();
  pageControllers.get(key)?.abort();
  const request = new AbortController(); pageControllers.set(key, request);
  pageLoading.value[key] = true; pageErrors.value[key] = "";
  const collection = current.data.collections.employees;
  if (collection) response.value = { ...current, data: { ...current.data,
    collections: { ...current.data.collections, employees: { ...collection, items: [] } } } };
  const valid = () => isCurrent(generation, selection) && pageControllers.get(key) === request && response.value?.snapshot_version === current.snapshot_version;
  try {
    const next = await fetchEmployeesDashboard(routePeriod(), request.signal,
      { section: "employees", employee_filter: filter.value, expected_version: current.snapshot_version });
    if (!valid() || !next.data || !response.value?.data || !next.data.collections.employees) return;
    if (next.snapshot_version !== current.snapshot_version) { await refreshChanged(); return; }
    const latest = response.value;
    response.value = { ...latest, data: { ...latest.data!, employee_filter: next.data.employee_filter,
      collections: { ...latest.data!.collections, employees: next.data.collections.employees } } };
  } catch (caught) {
    if (!valid()) return;
    if (isDashboardSnapshotChanged(caught)) await refreshChanged();
    else pageErrors.value[key] = dashboardErrorMessage(caught);
  } finally { if (valid()) { pageLoading.value[key] = false; pageControllers.delete(key); } }
}

function paginationScope() { return JSON.stringify([selectionKey(), response.value?.snapshot_version, requestGeneration]); }
function pausePages(section: string, scope: string) {
  if (scope !== paginationScope()) return;
  const key = pageKey(section); pageControllers.get(key)?.abort(); pageControllers.delete(key); pageLoading.value[key] = false;
}
function companyContribution(item: EstablishedEmployeeItem) {
  return item.employer_social_insurance_fen === null || item.employer_housing_fund_fen === null
    ? null
    : fen(item.employer_social_insurance_fen) + fen(item.employer_housing_fund_fen);
}

function obligationLabel(name: string) {
  return ({ net: "个人应付净额", primary: "期初应付款", tax: "应缴个税", withheld_tax: "已扣个税", employee_social: "个人社保", employee_housing: "个人公积金", employer_social: "公司社保", employer_housing: "公司公积金" } as Record<string, string>)[name] ?? "其他应付款";
}

function laborAmountAlreadyShown(labor: PersonalLaborItem, obligation: PersonalLaborItem["obligations"][number]) {
  const amount = obligation.name === "net" ? labor.net_fen : obligation.name === "tax" ? labor.booked_tax_fen : null;
  return amount !== null && obligation.amount_fen === amount
    && ["labor", "labor_accrual", "labor_project_cost"].some(kind => obligation.key === `${kind}:${labor.source_id}:${obligation.name}`);
}

function precisionLabel(value: string | null) {
  return value ? `${value}${value.length === 7 ? "（按月确认）" : ""}` : "未提供";
}


watch(
  () => [route.query.company_id, route.query.period, filter.value, focusedEmployeeId.value],
  (value, previous) => {
    if (value.every((item, index) => item === previous[index])) return;
    const listOnly = value[0] === previous[0] && value[1] === previous[1] && value[3] === previous[3]
      && !value[3] && response.value !== null && !loading.value;
    if (listOnly) {
      requestGeneration += 1; controller?.abort(); controller = null; clearPageRequests();
    } else invalidateRequests();
  },
  { flush: "sync" },
);
watch(
  () => [context.value?.current_company?.company_id, route.query.period, filter.value, focusedEmployeeId.value] as const,
  ([orgId, period, selectedFilter, employeeId], previous) => {
    if (previous && orgId === previous[0] && period === previous[1] && selectedFilter === previous[2] && employeeId === previous[3]) return;
    if (initialized && orgId && orgId === route.query.company_id) {
      if (previous && orgId === previous[0] && period === previous[1] && employeeId === previous[3]
        && !employeeId && response.value && !loading.value) void loadEmployeeList();
      else void loadPeriod(routePeriod());
    }
  },
);

onMounted(() => void initialize());
onBeforeUnmount(() => { mounted = false; invalidateRequests(); });
</script>

<template>
  <section class="employees-page" @click="focusSelectedPanel">
    <DashboardModuleHeader title="员工与薪酬概览" :options="periodOptions" :selected="selectedPeriodKey" :loading="loading" select-label="员工查看月份" @change="selectPeriod" @refresh="refresh">
      <template #navigation>
        <DashboardSectionNav v-if="sectionLinks.length" v-show="!loading" :items="sectionLinks" :active="activeSection" label="员工内容导航" @select="focusSection" />
      </template>
    </DashboardModuleHeader>
    <div class="employees-content">
      <p v-if="updateNotice" class="muted" role="status">{{ updateNotice }}</p>
      <div v-if="loading" class="state-panel" role="status">正在加载员工与薪酬数据…</div>
      <div v-else-if="error" class="state-panel" role="alert"><strong>员工数据加载失败</strong><p>{{ error }}</p><button class="control" type="button" @click="refresh">重试</button></div>
      <div v-else-if="response && !response.data" class="state-panel">还没有可查看的员工月份</div>
      <div v-if="employees && response?.selected_period" v-show="!loading && !error" class="employee-result">
        <section id="employees-overview" class="people-hero" tabindex="-1">
          <p class="dashboard-hero-eyebrow">{{ response.selected_period.label }} · 全公司</p>
          <div class="people-kpi-grid">
            <article><span>本月员工薪酬成本</span><strong>{{ formatFen(employees.ledger_cost_fen) }}</strong></article>
            <article class="salary-paid"><span>本月公司实际支付工资</span><strong :class="cashFlowClass(employees.direct_net_payments_fen, 'outflow')">{{ formatFen(employees.direct_net_payments_fen) }}</strong></article>
            <article class="salary-outstanding"><span>截至月末未付工资</span><strong>{{ formatFen(employees.outstanding_net_fen) }}</strong></article>
          </div>
          <p class="muted">本月应付净薪 {{ formatFen(employees.net_salary_fen) }} · 本月代付、抵销等 {{ formatFen(employees.other_net_settlements_fen) }}</p>
          <p class="muted">本月付款可包含以前月份工资；月末未付按各月份款项汇总。</p>
          <p class="muted">已登记 {{ employees.registered_count }} 人 · 已确认在册 {{ employees.in_period_count }} 人 · 本月有工资 {{ employees.payroll_count }} 人<span v-if="employees.unknown_period_count"> · 在册状态未确认 {{ employees.unknown_period_count }} 人</span></p>
          <p v-if="employees.checking" class="muted" role="status">部分薪酬资料由 AI 会计核对中，相关未知金额保留。</p>
        </section>
        <section class="panel">
          <div class="section-heading">
            <div><h2 id="employee-list-title" tabindex="-1">员工明细</h2><p class="muted">{{ filterLabel }} · 已加载 {{ filteredEmployees.length }} 人</p></div>
            <div class="people-toolbar">
              <select v-model="filter" class="control" aria-label="筛选员工"><option value="all">全部员工</option><option value="in_period">已确认在册</option><option value="payroll">本月有工资</option><option value="no_payroll">本月暂无工资</option><option value="ended">已确认不在册</option><option value="unknown">在册状态未确认</option></select>
              <div class="display-switch" role="group" aria-label="员工与劳务展示方式">
                <button type="button" :aria-pressed="displayMode === 'cards'" @click="displayMode = 'cards'">卡片</button>
                <button type="button" :aria-pressed="displayMode === 'list'" @click="displayMode = 'list'">列表</button>
              </div>
            </div>
          </div>
          <p v-if="employees.unestablished_count" class="muted">{{ employees.unestablished_count }} 项人员资料由 AI 会计核对中；金额暂无法确定。</p>
          <p v-if="pageLoading[pageKey('employees')] && !filteredEmployees.length" class="muted" role="status">正在读取所选员工清单…</p>
          <p v-else-if="!filteredEmployees.length && !pageErrors[pageKey('employees')]" class="muted">{{ focusedEmployeeId ? '该员工暂无可展示记录。' : '当前范围没有员工记录。' }}</p>
          <div class="employee-results" :class="{ 'list-results': displayMode === 'list' }" :tabindex="displayMode === 'list' ? 0 : undefined" aria-label="员工明细记录">
          <div class="employee-grid" :class="{ 'employee-list': displayMode === 'list' }">
            <div v-if="displayMode === 'list' && filteredEmployees.length" class="employee-list-header">
              <span>员工</span><span v-for="column in employeeListColumns" :id="`employee-column-${column.key}`" :key="column.key">{{ column.label }}</span><span aria-hidden="true"></span>
            </div>
            <template v-for="item in filteredEmployees" :key="item.employee_id">
              <article v-if="item.selection_status === 'unestablished'" :id="focusedEmployeeId === item.employee_id ? 'employee-card-target' : undefined" class="employee-card dashboard-record-card" data-section-focus tabindex="-1"><h3>{{ item.name }}</h3><p class="muted">AI 会计核对中 · 金额暂无法确定</p></article>
              <details v-else :id="focusedEmployeeId === item.employee_id ? 'employee-card-target' : undefined" :open="focusedEmployeeId === item.employee_id" class="employee-card dashboard-record-card" data-section-focus tabindex="-1">
                <summary class="employee-card-summary dashboard-record-card-summary">
                  <div v-if="displayMode === 'list'" class="employee-list-summary">
                    <div class="employee-list-identity"><span class="employee-status" :class="item.wage_tax_scope" role="img" :aria-label="item.wage_tax_scope_label" :title="item.wage_tax_scope_label"></span><div class="employee-name"><h3>{{ item.name }}</h3><p class="muted">{{ item.period_state_label }}</p></div></div>
                    <strong v-for="column in employeeListColumns" :key="column.key" :class="{ 'employee-list-net': column.key === 'net', 'payable-amount': column.key === 'net', 'cost-amount': ['gross', 'bonus', 'employer-social', 'employer-housing'].includes(column.key) }" :data-label="column.label" :aria-labelledby="`employee-column-${column.key}`">{{ formatFen(column.amount(item)) }}</strong>
                    <svg class="employee-list-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
                  </div>
                  <template v-else>
                    <div class="section-heading"><h3 class="employee-card-name"><span class="employee-status" :class="item.wage_tax_scope" role="img" :aria-label="item.wage_tax_scope_label" :title="item.wage_tax_scope_label"></span><span>{{ item.name }}</span></h3><strong class="cost-amount">{{ formatFen(item.company_cost_fen) }}<small>本月公司成本</small></strong></div>
                    <p class="muted">{{ item.period_state_label }}<span v-if="item.employment_start_date"> · 入职 {{ precisionLabel(item.employment_start_date) }}</span><span v-if="item.employment_end_date"> · 离职 {{ precisionLabel(item.employment_end_date) }}</span></p>
                    <div class="amount-grid"><div><span>本月应付净薪</span><strong class="payable-amount">{{ formatFen(item.net_salary_fen) }}</strong></div><div><span>本月实际支付</span><strong :class="cashFlowClass(item.direct_net_payments_fen, 'outflow')">{{ formatFen(item.direct_net_payments_fen) }}</strong></div><div><span>月末未付</span><strong class="payable-amount">{{ formatFen(item.outstanding_net_fen) }}</strong></div></div>
                    <p class="muted">{{ item.has_payroll_activity ? item.payroll_periods.join('、') + ' 工资' : '本月暂无工资记录' }} · 展开查看本月薪酬</p>
                  </template>
                </summary>
                <div class="employee-detail" :class="{ 'dashboard-business-expansion': displayMode === 'list' }">
                  <h3>{{ displayMode === 'list' ? '薪酬补充' : '薪酬明细' }}</h3>
                  <p v-if="displayMode === 'list'" class="employee-detail-meta muted"><span v-if="item.employment_start_date">入职 {{ precisionLabel(item.employment_start_date) }}</span><span v-if="item.employment_end_date">离职 {{ precisionLabel(item.employment_end_date) }}</span><span>{{ item.has_payroll_activity ? '工资所属月份 ' + item.payroll_periods.join('、') : '本月暂无工资记录' }}</span></p>
                  <dl class="amount-grid">
                    <template v-if="displayMode === 'list'">
                      <div><dt>本月公司成本</dt><dd class="cost-amount">{{ formatFen(item.company_cost_fen) }}</dd></div><div><dt>本月实际支付</dt><dd :class="cashFlowClass(item.direct_net_payments_fen, 'outflow')">{{ formatFen(item.direct_net_payments_fen) }}</dd></div><div><dt>月末未付</dt><dd class="payable-amount">{{ formatFen(item.outstanding_net_fen) }}</dd></div>
                    </template>
                    <template v-else>
                      <div><dt>应发工资</dt><dd class="cost-amount">{{ formatFen(item.gross_salary_fen) }}</dd></div><div v-if="item.annual_bonus_fen === null || fen(item.annual_bonus_fen)"><dt>全年一次性奖金</dt><dd class="cost-amount">{{ formatFen(item.annual_bonus_fen) }}</dd></div><div><dt>公司社保公积金</dt><dd class="cost-amount">{{ formatFen(companyContribution(item)) }}</dd></div><div><dt>已扣个税</dt><dd>{{ formatFen(item.individual_income_tax_fen) }}</dd></div>
                    </template>
                    <div><dt>个人社保</dt><dd>{{ formatFen(item.employee_social_insurance_fen) }}</dd></div><div><dt>个人公积金</dt><dd>{{ formatFen(item.employee_housing_fund_fen) }}</dd></div><div><dt>本月代付、抵销等</dt><dd>{{ formatFen(item.other_net_settlements_fen) }}</dd></div>
                  </dl>
                </div>
              </details>
            </template>
          </div>
          </div>
          <DashboardPagination automatic :active="!loading && activeSection === 'employee-list-title'" :scope="paginationScope()" @pause="pausePages('employees', $event)" :page="!filteredEmployees.length && (pageLoading[pageKey('employees')] || pageErrors[pageKey('employees')]) ? undefined : data?.collections.employees?.page" :loaded="filteredEmployees.length" :loading="pageLoading[pageKey('employees')]" :error="pageErrors[pageKey('employees')]" @more="loadMore()" @retry="loadMore()" />
        </section>
        <section v-if="personalLaborItems.length" class="panel">
          <div class="section-heading">
            <div><h2 id="labor-title" tabindex="-1">个人劳务</h2><p class="muted">本月费用 {{ formatFen(workforce?.personal_labor_fen) }} · 资产或项目 {{ formatFen(workforce?.capitalized_labor_fen) }} · 已加载 {{ personalLaborItems.length }} 笔</p></div>
          </div>
          <div class="employee-results" :class="{ 'list-results': displayMode === 'list' }" :tabindex="displayMode === 'list' ? 0 : undefined" aria-label="个人劳务明细记录">
          <div class="employee-grid" :class="{ 'employee-list labor-list': displayMode === 'list' }">
            <div v-if="displayMode === 'list'" class="employee-list-header">
              <span>劳务对象</span><span v-for="column in laborListColumns" :id="`labor-column-${column.key}`" :key="column.key">{{ column.label }}</span><span aria-hidden="true"></span>
            </div>
            <details v-for="labor in personalLaborItems" :key="labor.source_id" class="employee-card dashboard-record-card" data-section-focus tabindex="-1">
              <summary class="employee-card-summary dashboard-record-card-summary">
                <div v-if="displayMode === 'list'" class="employee-list-summary">
                  <div class="employee-list-identity"><span class="employee-status labor" role="img" aria-label="个人劳务" title="个人劳务"></span><div class="employee-name"><h3>{{ labor.name }}</h3><p class="muted">{{ labor.period }} · {{ labor.capitalized ? '计入资产或项目' : '计入本月费用' }}</p></div></div>
                  <strong v-for="column in laborListColumns" :key="column.key" :class="{ 'employee-list-net': column.key === 'net', 'payable-amount': column.key === 'net', 'cost-amount': column.key === 'gross' }" :data-label="column.label" :aria-labelledby="`labor-column-${column.key}`">{{ formatFen(column.amount(labor)) }}</strong>
                  <svg class="employee-list-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
                </div>
                <template v-else>
                  <div class="section-heading"><h3 class="employee-card-name"><span class="employee-status labor" role="img" aria-label="个人劳务" title="个人劳务"></span><span>{{ labor.name }}</span></h3><strong class="cost-amount">{{ formatFen(labor.gross_fen) }}</strong></div><p class="muted">{{ labor.period }} · {{ labor.capitalized ? '计入资产或项目' : '计入本月费用' }}</p><div class="amount-grid"><div><span>已扣个税</span><strong>{{ formatFen(labor.booked_tax_fen) }}</strong></div><div><span>应付净额</span><strong class="payable-amount">{{ formatFen(labor.net_fen) }}</strong></div></div><p class="muted">展开查看付款</p>
                </template>
              </summary>
              <div class="employee-detail" :class="{ 'dashboard-business-expansion': displayMode === 'list' }">
                <p v-if="labor.checking" class="muted">AI 会计核对中</p>
                <section v-for="obligation in labor.obligations" :key="obligation.key" class="labor-payment-detail">
                  <h3>{{ obligationLabel(obligation.name) }}<span v-if="!laborAmountAlreadyShown(labor, obligation)"> · {{ formatFen(obligation.amount_fen) }}</span></h3>
                  <dl class="amount-grid"><div><dt>公司已付</dt><dd :class="cashFlowClass(obligation.paid_fen, 'outflow')">{{ formatFen(obligation.paid_fen) }}</dd></div><div><dt>代付、抵销等</dt><dd>{{ formatFen(obligation.other_settled_fen) }}</dd></div><div><dt>月末未付</dt><dd class="payable-amount">{{ formatFen(obligation.remaining_fen) }}</dd></div></dl>
                </section>
                <BusinessStatusDetails :subject-id="labor.subject_id" :period="selectedPeriodKey" :snapshot-version="response.snapshot_version ?? undefined" settlement-view="historical" presentation="labor" :labor-context="labor" summary-label="查看收付款事项" @changed="refreshChanged" />
              </div>
            </details>
          </div>
          </div>
          <DashboardPagination automatic :active="!loading && activeSection === 'labor-title'" :scope="paginationScope()" @pause="pausePages('labor_sources', $event)" :page="data?.collections.labor_sources?.page" :loaded="personalLaborItems.length" :loading="pageLoading[pageKey('labor_sources')]" :error="pageErrors[pageKey('labor_sources')]" @more="loadMore('labor_sources')" @retry="loadMore('labor_sources')" />
        </section>
      </div>
    </div>
  </section>
</template>

<style scoped>
.employee-result { display: contents; }
.employees-page { width: min(calc(100% - 48px), 1320px); min-height: 100%; margin: 0 auto; padding: 25px 0 46px; }
.employees-content { min-width: 0; }
.people-hero { display: grid; gap: 24px; padding: 25px 28px; border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line)); border-radius: 20px; background: radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%), linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface))); }
.people-hero > .dashboard-hero-eyebrow { margin: 0 0 -12px; }
.people-hero > p.muted { margin: -16px 0 0; font-size: 12px; }
.people-kpi-grid, .amount-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); }
.people-kpi-grid { gap: 20px 28px; margin-top: 8px; align-items: start; }
.people-kpi-grid article, .amount-grid > div { display: grid; min-width: 0; gap: 6px; }
.people-kpi-grid span { color: var(--muted); font-size: 12px; font-weight: 750; }
.people-headline-grid #employees-cost-value { color: var(--cost); }
.people-kpi-grid article { --kpi-accent: var(--cost); }
.people-kpi-grid .salary-paid { --kpi-accent: var(--danger); }
.people-kpi-grid .salary-paid .cash-inflow { color: var(--cash-in); }
.people-kpi-grid .salary-outstanding { --kpi-accent: var(--warning); }
.people-kpi-grid strong { margin: 4px 0; font-size: clamp(20px,2vw,26px); line-height: 1.15; letter-spacing: -.025em; color: var(--kpi-accent); overflow-wrap: anywhere; }
.people-kpi-grid article:first-child strong { color: var(--cost); font-size: clamp(28px,3.2vw,42px); }

.employees-page .cost-amount { color: var(--cost); }
.employees-page .payable-amount { color: var(--warning); }
.employees-page .cash-inflow { color: var(--cash-in); }
.employees-page .cash-outflow { color: var(--danger); }
.muted, .amount-grid span, dt, small { color: var(--muted); font-size: 12px; line-height: 1.5; }
.panel { min-width: 0; margin-top: 40px; }
.panel > h2, .section-heading h2 { font-size: 20px; }
.section-heading { display: flex; align-items: start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.section-heading h2, .section-heading h3 { margin: 0; }
.section-heading strong { display: grid; gap: 4px; }
.people-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; max-width: 100%; margin-left: auto; }
.display-switch { display: grid; flex: 0 0 auto; grid-template-columns: repeat(2,1fr); gap: 3px; padding: 3px; border: 1px solid var(--line); border-radius: 11px; background: var(--surface-soft); }
.display-switch button { min-height: 34px; padding: 0 13px; border: 0; border-radius: 8px; background: transparent; color: var(--muted); font: inherit; font-size: 13px; white-space: nowrap; cursor: pointer; }
.display-switch button[aria-pressed="true"] { background: var(--surface); color: var(--text); }
.display-switch button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.employee-results { min-width: 0; margin-top: 16px; }
.employee-results.list-results { max-width: 100%; container: people-list / inline-size; overflow: hidden; padding-inline: var(--dashboard-list-gutter); border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface); }
.employee-results:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
.employee-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 10px; align-items: start; }
.employee-grid.employee-list { --employee-list-columns: minmax(0,1.4fr) repeat(8,minmax(0,1fr)) 16px; grid-template-columns: minmax(0,1fr); min-width: 0; gap: 0; }
.employee-grid.labor-list { --employee-list-columns: minmax(0,1.4fr) repeat(3,minmax(0,1fr)) 16px; }
.employee-list-header, .employee-list-summary { display: grid; grid-template-columns: var(--employee-list-columns); gap: 10px; align-items: center; }
.employee-list-header { padding: 10px 4px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 11px; line-height: 1.5; }
.employee-list-header > span { min-width: 0; overflow-wrap: anywhere; }
.employee-list-header > span:not(:first-child), .employee-list-summary > strong { text-align: right; }
.employee-list .employee-card { border-radius: 0; border-width: 0 0 1px; background: var(--surface); }
.employee-list .employee-card:hover, .employee-list .employee-card:focus-within, .employee-list .employee-card[open] { border-color: var(--line); }
.employee-list .employee-card:last-child { border-bottom: 0; }
.employee-list .employee-card-summary, .employee-list article.employee-card { min-height: 62px; padding: 10px 4px; transition: background 140ms ease; }
.employee-list .employee-card-summary:hover, .employee-list .employee-card-summary:focus-visible, .employee-list article.employee-card:hover { background: var(--surface-soft); }
.employee-list article.employee-card { cursor: default; }
.employee-list-summary > strong { min-width: 0; font-size: 13px; font-weight: 500; line-height: 1.5; font-variant-numeric: tabular-nums; }
.employee-list-summary > .employee-list-net { font-size: 14px; font-weight: 700; }
.employee-list-identity { display: flex; min-width: 0; align-items: center; gap: 9px; }
.employee-card-name { display: flex; min-width: 0; max-width: 100%; align-items: center; gap: 9px; }
.employee-card-name > span:last-child { min-width: 0; }
.employee-name { display: grid; min-width: 0; gap: 2px; line-height: 1.5; }
.employee-list-summary .employee-list-identity h3, .employee-list article.employee-card h3 { margin: 0; font-size: 14px; font-weight: 700; line-height: 1.5; }
.employee-list-identity p, .employee-list article.employee-card p { margin: 0; font-size: 11px; font-weight: 400; line-height: 1.5; }
.employee-list article.employee-card p { margin-top: 4px; }
.employee-status { display: inline-flex; flex: 0 0 auto; align-items: center; }
.employee-status::before { width: 12px; height: 12px; flex: 0 0 auto; border-radius: 50%; background: var(--muted); content: ""; }
.employee-status.wage_income::before, .employee-status.labor::before { background: var(--accent); }
.employee-status.contributions_only::before { background: var(--warning); }
.employee-status.mixed::before { background: var(--info); }
.employee-list-chevron { width: 12px; height: 16px; justify-self: end; fill: none; stroke: var(--muted); stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; transition: transform 150ms ease; }
.employee-card[open] > summary .employee-list-chevron { transform: rotate(90deg); }
.employee-card { min-width: 0; }
.employee-card-summary, article.employee-card { padding: 16px; cursor: pointer; }
.employee-card-summary h3 { font-size: 16px; }
.employee-card-summary > .section-heading > strong { min-width: 0; max-width: 100%; font-size: 21px; text-align: right; }
.employee-card-summary > .section-heading small { font-size: 11px; font-weight: 400; }
.employee-card-summary > p { margin: 12px 0; }
.employee-card-summary > p:last-child { margin-bottom: 0; }
.amount-grid { gap: 8px; padding: 0; border-radius: 9px; background: var(--surface-soft); }
.amount-grid > div { gap: 3px; padding: 10px; }
.amount-grid span, .amount-grid dt { font-size: 11px; }
.amount-grid strong, .amount-grid dd { font-size: 14px; }
.employee-detail:not(.dashboard-business-expansion) { border-top: 1px solid var(--line); padding: 16px; }
.employee-detail h3 { margin: 0 0 12px; font-size: 13px; font-weight: 600; }
.employee-detail .amount-grid { margin: 0; gap: 14px 24px; padding: 0; background: none; border-radius: 0; }
.employee-detail .amount-grid > div { padding: 0; }
.employee-detail dd { font-weight: 500; }
.employee-detail-meta { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 0 0 16px; }
.employee-list .employee-detail { margin-inline: 4px; margin-bottom: 16px; }
.employee-list .employee-detail h3 { margin-bottom: 10px; font-size: 13px; font-weight: 600; line-height: 1.5; }
.employee-list .employee-detail .amount-grid { grid-template-columns: repeat(4,minmax(0,1fr)); gap: 12px 16px; }
.employee-list .employee-detail dd { font-size: 13px; font-weight: 500; line-height: 1.5; }
.labor-payment-detail + .labor-payment-detail { margin-top: 18px; }
.employee-detail :deep(.business-status-details) { margin-top: 18px; }
dd { margin: 0; }
.control { min-height: 44px; padding: 0 12px; color: var(--text); background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-control); }
.state-panel { display: grid; gap: 7px; padding: 28px; border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface); }
strong, p, h3, dd { overflow-wrap: anywhere; }
strong, dd { font-variant-numeric: tabular-nums; }
[tabindex="-1"] { scroll-margin-top: 76px; }
.employee-card-summary:focus-visible { outline: 2px solid var(--focus); outline-offset: -2px; }
@container people-list (max-width: 1020px) {
  .employee-grid.employee-list { padding-block: 8px; }
  .employee-list-header { display: none; }
  .employee-list .employee-card-summary, .employee-list article.employee-card { padding-block: 14px; }
  .employee-list-summary { grid-template-columns: repeat(2,minmax(0,1fr)); position: relative; }
  .employee-list-identity { grid-column: 1 / -1; padding-right: 24px; }
  .employee-list-summary > strong { align-self: start; text-align: left; }
  .employee-list-summary > strong::before { display: block; content: attr(data-label); margin-bottom: 4px; color: var(--muted); font-size: 11px; font-weight: 400; }
  .employee-list-chevron { position: absolute; top: 0; right: 0; }
  .employee-list .employee-card, .employee-list .employee-card:last-child { border: 1px solid var(--line); border-radius: var(--radius-control); margin-bottom: 8px; }
  .employee-list .employee-card:last-child { margin-bottom: 0; }
}
@media (max-width: 1080px) { .employee-grid { grid-template-columns: 1fr; } }
@media (max-width: 720px) { .employees-page { width: min(calc(100% - 24px), 1320px); padding: 16px 0 24px; } .people-hero { padding: 19px; border-radius: 17px; } .people-kpi-grid, .amount-grid { grid-template-columns: 1fr; } .section-heading { flex-direction: column; } .employee-card-summary > .section-heading > strong { text-align: left; } .control { width: 100%; } }
@media (max-width: 720px) { .people-toolbar { width: 100%; } .display-switch button { min-height: 44px; } }
@media (max-width: 720px) { .employee-detail .amount-grid, .employee-list .employee-detail .amount-grid { grid-template-columns: repeat(2,minmax(0,1fr)); } }
</style>
