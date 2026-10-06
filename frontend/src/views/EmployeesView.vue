<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import {
  fetchEmployeesDashboard,
  type EstablishedEmployeeItem,
  type EmployeesDashboardResponse,
  type EmployeesQuery,
} from "../api/employees";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import BusinessStatusDetails from "../components/BusinessStatusDetails.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { useDashboardSections } from "../composables/useDashboardSections";
import { fen, formatFen } from "../utils/money";
import { appendDashboardCollection } from "../utils/dashboardCollections";

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
  set: value => { void router.push({ query: { ...route.query, employee_filter: value === "all" ? undefined : value, employee_id: undefined } }); },
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
  void router.push({ query: { company_id: route.query.company_id, period: value } });
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
      <template #navigation><DashboardSectionNav v-if="sectionLinks.length" v-show="!loading" :items="sectionLinks" :active="activeSection" label="员工内容导航" @select="focusSection" /></template>
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
            <article><span>本月公司实际支付工资</span><strong>{{ formatFen(employees.direct_net_payments_fen) }}</strong></article>
            <article><span>截至月末未付工资</span><strong>{{ formatFen(employees.outstanding_net_fen) }}</strong></article>
          </div>
          <p class="muted">本月应付净薪 {{ formatFen(employees.net_salary_fen) }} · 本月代付、抵销等 {{ formatFen(employees.other_net_settlements_fen) }}</p>
          <p class="muted">本月付款可包含以前月份工资；月末未付按各月份款项汇总。</p>
          <p class="muted">已登记 {{ employees.registered_count }} 人 · 已确认在册 {{ employees.in_period_count }} 人 · 本月有工资 {{ employees.payroll_count }} 人<span v-if="employees.unknown_period_count"> · 在册状态未确认 {{ employees.unknown_period_count }} 人</span></p>
          <p v-if="employees.checking" class="muted" role="status">部分薪酬资料由 AI 会计核对中，相关未知金额保留。</p>
        </section>
        <section class="panel">
          <div class="section-heading"><div><h2 id="employee-list-title" tabindex="-1">员工明细</h2><p class="muted">{{ filterLabel }} · 已加载 {{ filteredEmployees.length }} 人</p></div>
            <select v-model="filter" class="control" aria-label="筛选员工"><option value="all">全部员工</option><option value="in_period">已确认在册</option><option value="payroll">本月有工资</option><option value="no_payroll">本月暂无工资</option><option value="ended">已确认不在册</option><option value="unknown">在册状态未确认</option></select>
          </div>
          <p v-if="employees.unestablished_count" class="muted">{{ employees.unestablished_count }} 项人员资料由 AI 会计核对中；金额暂无法确定。</p>
          <p v-if="pageLoading[pageKey('employees')] && !filteredEmployees.length" class="muted" role="status">正在读取所选员工清单…</p>
          <p v-else-if="!filteredEmployees.length && !pageErrors[pageKey('employees')]" class="muted">{{ focusedEmployeeId ? '该员工暂无可展示记录。' : '当前范围没有员工记录。' }}</p>
          <div class="employee-grid">
            <template v-for="item in filteredEmployees" :key="item.employee_id">
              <article v-if="item.selection_status === 'unestablished'" :id="focusedEmployeeId === item.employee_id ? 'employee-card-target' : undefined" class="employee-card dashboard-record-card" data-section-focus tabindex="-1"><h3>{{ item.name }}</h3><p class="muted">AI 会计核对中 · 金额暂无法确定</p></article>
              <details v-else :id="focusedEmployeeId === item.employee_id ? 'employee-card-target' : undefined" :open="focusedEmployeeId === item.employee_id" class="employee-card dashboard-record-card" data-section-focus tabindex="-1">
                <summary class="employee-card-summary dashboard-record-card-summary">
                  <div class="section-heading"><h3>{{ item.name }}</h3><strong>{{ formatFen(item.company_cost_fen) }}<small>本月公司成本</small></strong></div>
                  <p class="muted">{{ item.period_state_label }}<span v-if="item.employment_start_date"> · 入职 {{ precisionLabel(item.employment_start_date) }}</span><span v-if="item.employment_end_date"> · 离职 {{ precisionLabel(item.employment_end_date) }}</span></p>
                  <div class="amount-grid"><div><span>本月应付净薪</span><strong>{{ formatFen(item.net_salary_fen) }}</strong></div><div><span>本月实际支付</span><strong>{{ formatFen(item.direct_net_payments_fen) }}</strong></div><div><span>月末未付</span><strong>{{ formatFen(item.outstanding_net_fen) }}</strong></div></div>
                  <p class="muted">{{ item.has_payroll_activity ? item.payroll_periods.join('、') + ' 工资' : '本月暂无工资记录' }} · 展开查看本月薪酬</p>
                </summary>
                <div class="employee-detail">
                  <h3>本月薪酬</h3>
                  <dl class="amount-grid"><div><dt>应发工资</dt><dd>{{ formatFen(item.gross_salary_fen) }}</dd></div><div v-if="fen(item.annual_bonus_fen)"><dt>全年一次性奖金</dt><dd>{{ formatFen(item.annual_bonus_fen) }}</dd></div><div><dt>公司社保公积金</dt><dd>{{ formatFen(companyContribution(item)) }}</dd></div><div><dt>个人社保</dt><dd>{{ formatFen(item.employee_social_insurance_fen) }}</dd></div><div><dt>个人公积金</dt><dd>{{ formatFen(item.employee_housing_fund_fen) }}</dd></div><div><dt>已扣个税</dt><dd>{{ formatFen(item.individual_income_tax_fen) }}</dd></div><div><dt>本月代付、抵销等</dt><dd>{{ formatFen(item.other_net_settlements_fen) }}</dd></div></dl>
                </div>
              </details>
            </template>
          </div>
          <DashboardPagination automatic :active="!loading && activeSection === 'employee-list-title'" :scope="paginationScope()" @pause="pausePages('employees', $event)" :page="!filteredEmployees.length && (pageLoading[pageKey('employees')] || pageErrors[pageKey('employees')]) ? undefined : data?.collections.employees?.page" :loaded="filteredEmployees.length" :loading="pageLoading[pageKey('employees')]" :error="pageErrors[pageKey('employees')]" @more="loadMore()" @retry="loadMore()" />
        </section>
        <section v-if="personalLaborItems.length" class="panel">
          <h2 id="labor-title" tabindex="-1">个人劳务</h2><p class="muted">本月费用 {{ formatFen(workforce?.personal_labor_fen) }} · 资产或项目 {{ formatFen(workforce?.capitalized_labor_fen) }} · 已加载 {{ personalLaborItems.length }} 笔</p>
          <div class="employee-grid"><details v-for="labor in personalLaborItems" :key="labor.source_id" class="employee-card dashboard-record-card" data-section-focus tabindex="-1"><summary class="employee-card-summary dashboard-record-card-summary"><div class="section-heading"><h3>{{ labor.name }}</h3><strong>{{ formatFen(labor.gross_fen) }}</strong></div><p class="muted">{{ labor.period }} · {{ labor.capitalized ? '计入资产或项目' : '计入本月费用' }}</p><div class="amount-grid"><div><span>已扣个税</span><strong>{{ formatFen(labor.booked_tax_fen) }}</strong></div><div><span>应付净额</span><strong>{{ formatFen(labor.net_fen) }}</strong></div></div><p class="muted">展开查看付款</p></summary><div class="employee-detail"><p v-if="labor.checking" class="muted">AI 会计核对中</p><div v-for="obligation in labor.obligations" :key="obligation.key"><strong>{{ obligationLabel(obligation.name) }} {{ formatFen(obligation.amount_fen) }}</strong><p class="muted">公司已付 {{ formatFen(obligation.paid_fen) }} · 代付、抵销等 {{ formatFen(obligation.other_settled_fen) }} · 月末未付 {{ formatFen(obligation.remaining_fen) }}</p></div><BusinessStatusDetails :subject-id="labor.subject_id" :period="selectedPeriodKey" :snapshot-version="response.snapshot_version ?? undefined" settlement-view="historical" summary-label="查看收付款事项" @changed="refreshChanged" /></div></details></div>
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
.people-kpi-grid strong { margin: 4px 0; font-size: clamp(20px,2vw,26px); line-height: 1.15; letter-spacing: -.025em; color: var(--text); }
.people-kpi-grid article:first-child strong { color: var(--accent); font-size: clamp(28px,3.2vw,42px); }
.muted, .amount-grid span, dt, small { color: var(--muted); font-size: 12px; line-height: 1.5; }
.panel { min-width: 0; margin-top: 40px; }
.panel > h2, .section-heading h2 { font-size: 20px; }
.section-heading { display: flex; align-items: start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.section-heading h2, .section-heading h3 { margin: 0; }
.section-heading strong { display: grid; gap: 4px; }
.employee-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 10px; margin-top: 16px; align-items: start; }
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
.employee-detail { border-top: 1px solid var(--line); padding: 16px; }
.employee-detail h3 { font-size: 15px; }
dd { margin: 0; }
.control { min-height: 44px; padding: 0 12px; color: var(--text); background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-control); }
.state-panel { display: grid; gap: 7px; padding: 28px; border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface); }
strong, p, h3, dd { overflow-wrap: anywhere; }
strong, dd { font-variant-numeric: tabular-nums; }
[tabindex="-1"] { scroll-margin-top: 76px; }
summary:focus-visible { outline: none; }
@media (max-width: 900px) { .employee-grid { grid-template-columns: 1fr; } }
@media (max-width: 720px) { .employees-page { width: min(calc(100% - 24px), 1320px); padding: 16px 0 24px; } .people-hero { padding: 19px; border-radius: 17px; } .people-kpi-grid, .amount-grid { grid-template-columns: 1fr; } .section-heading { flex-direction: column; } .employee-card-summary > .section-heading > strong { text-align: left; } .control { width: 100%; } }
</style>
