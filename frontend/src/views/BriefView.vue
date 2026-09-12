<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { fetchDeferredBrief, type BriefQuery } from "../api/brief";
import { fetchPeriodPreparation, type PeriodPreparationResult } from "../api/periodPreparation";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import PeriodPreparation from "../components/PeriodPreparation.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import DashboardBusinessRecords from "../components/DashboardBusinessRecords.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import BriefActivityWorkbench from "../components/brief/BriefActivityWorkbench.vue";
import BriefFinancialOverview from "../components/brief/BriefFinancialOverview.vue";
import BriefOpenItems from "../components/brief/BriefOpenItems.vue";
import BriefWorkforceSection from "../components/brief/BriefWorkforceSection.vue";
import { useDashboardSections } from "../composables/useDashboardSections";
import { useDashboardContext } from "../composables/useDashboardContext";
import { fen, formatFen, formatPositiveFen } from "../utils/money";

type PriorityAction = "bank-details" | "validation";
type PriorityItem = {
  title: string;
  note: string;
  state: "error" | "attention" | "neutral";
  action?: PriorityAction;
};

const route = useRoute();
const router = useRouter();
const { context, load: loadContext, refresh: refreshContext } = useDashboardContext();
const response = ref<Awaited<ReturnType<typeof fetchDeferredBrief>> | null>(null);
const preparation = ref<PeriodPreparationResult | null>(null);
const preparationStatus = ref<"pending" | "loading" | "ready" | "error" | "stale">("pending");
const preparationError = ref("");
let preparationController: AbortController | null = null;
let preparationAttempt = 0;
const loading = ref(false);
type BriefSection = NonNullable<BriefQuery["section"]>;
const sectionLoading = ref<Partial<Record<BriefSection, boolean>>>({});
const sectionErrors = ref<Partial<Record<BriefSection, string>>>({});
const pageControllers = new Map<BriefSection, AbortController>();
const updateNotice = ref("");
const error = ref("");
let controller: AbortController | null = null;
let initialized = false;
let mounted = true;
let requestGeneration = 0;
const businessSections = [
  { key: "businesses", label: "正式业务记录（含不产生凭证的结果）" }, { key: "open_items", label: "月末待收待付的完整来源" },
  { key: "settlement_events", label: "相关后续收付款与抵销" }, { key: "external_followups", label: "相关外部办理" }, { key: "file_jobs", label: "相关文件任务" },
] as const;

const selectedPeriod = computed(() => response.value?.selected_period?.key || "");
const periodOptions = computed(() => context.value?.periods || []);
const data = computed(() => {
  const main = response.value?.data;
  if (!main || preparationStatus.value !== "ready" || !preparation.value) return main ?? null;
  const checks = preparation.value.data.brief_checks;
  const attention = main.validation.attention_count + checks.attention_count;
  return { ...main, period_preparation: preparation.value.data.period_preparation, material_completeness: checks.material_completeness,
    validation: { ...main.validation, title: "账务核对", summary: main.validation.integrity_valid === true ? "账务汇总平衡" : main.validation.integrity_valid === null ? "财务位置依据不完整" : "账务汇总需核对",
      state: main.validation.integrity_valid === false ? "error" : attention || main.validation.integrity_valid === null ? "attention" : "complete",
      attention_count: attention, issues: checks.issues, items: [...main.validation.items.filter(item => item.key === "balance"), ...checks.items] },
  };
});
const isClosed = computed(() => response.value?.selected_period?.status === "closed");
const sectionLinks = computed(() => {
  if (!data.value) return [];
  const links = [
    { id: "overview", label: "概览" },
    { id: "open-items", label: "待收待付" },
    { id: "activity", label: "业务凭证" },
  ];
  if (data.value?.workforce_cost.has_activity) links.push({ id: "workforce", label: "用工" });
  links.push(
    { id: "finance", label: "资金资产" },

    { id: "validation", label: "资料核对" },
  );
  return links;
});
const { activeSection, focusSection } = useDashboardSections(sectionLinks, "overview");
const priorities = computed(() => {
  if (!data.value || !response.value?.selected_period) return [];
  const items: PriorityItem[] = [];
  if (data.value.open_items.complete === false || data.value.open_items.unestablished_count) {
    items.push({ title: "待收待付来源尚待核对", note: "已知金额不代表完整结果，查看精确来源与候选", state: "attention", action: "validation" });
  }
  if (data.value.validation.integrity_valid === false) {
    items.push({ title: "账务金额需要核对", note: "查看具体差异及对应记录", state: "error", action: "validation" });
  }
  if (data.value.validation.integrity_valid === null) {
    items.push({ title: "财务位置无法完整建立", note: "查看尚未明确的来源归属", state: "attention", action: "validation" });
  }
  if (data.value.validation.issues?.length) {
    items.push({ title: `${data.value.validation.issues.length} 条当前核算提示`, note: "查看具体检查结果与对应来源", state: "attention", action: "validation" });
  }
  if (data.value.cash.missing_account_count) {
    items.push({ title: `${data.value.cash.missing_account_count} 个银行账户尚未提供本月流水`, note: "不能据此判断没有收支，查看对应账户", state: "attention", action: "bank-details" });
  } else if (["missing", "partial"].includes(data.value.cash.coverage_state)) {
    items.push({ title: "银行流水覆盖尚不能完整确认", note: "查看各账户的资料与来源核对说明", state: "attention", action: "bank-details" });
  }
  if (data.value.cash.unmatched_count) {
    items.push({
      title: `${data.value.cash.unmatched_count} 笔流水待识别`,
      note: "尚不能当作已确认业务",
      state: "attention",
      action: "bank-details",
    });
  }
  if (data.value.cash.needs_review_count) {
    items.push({
      title: `${data.value.cash.needs_review_count} 笔流水匹配需复核`,
      note: "相关流水的匹配状态尚需核对",
      state: "attention",
      action: "bank-details",
    });
  }
  if (data.value.material_completeness?.issues.length) {
    items.push({
      title: `${data.value.material_completeness.issues.length} 条资料核对提示`,
      note: "查看来源文件和具体事项",
      state: "attention",
      action: "validation",
    });
  }
  return items;
});
const takeaway = computed(() => {
  if (!data.value) return "";
  const details = data.value.management_commentary_details;
  if (details?.current) return details.current.text;
  if (data.value.management_commentary) return data.value.management_commentary;
  const result = data.value.position.month_result_fen;
  return `本月已入账收入 ${formatFen(data.value.position.month_revenue_fen)}，费用 ${formatFen(data.value.position.month_expense_fen)}，${result === null ? '账面盈亏尚不能完整建立' : `${fen(result) < 0n ? '账面亏损' : '账面结余'} ${formatPositiveFen(result)}`}。月末账面资金 ${formatFen(data.value.funds_overview.total_fen)}。`;
});

function queryPeriod() {
  return typeof route.query.period === "string" ? route.query.period : null;
}

function resetPreparation() {
  preparationAttempt += 1;
  preparationController?.abort(); preparationController = null;
  preparation.value = null; preparationStatus.value = "pending"; preparationError.value = "";
}

async function loadPreparation() {
  const main = response.value;
  if (!main?.data || !main.selected_period || preparationStatus.value === "stale") return;
  const generation = requestGeneration, selection = selectionKey(), attempt = ++preparationAttempt;
  const readContext = main.read_context, period = main.selected_period.key;
  preparationController?.abort();
  const request = new AbortController(); preparationController = request;
  preparationStatus.value = "loading"; preparationError.value = "";
  const valid = () => isCurrent(generation, selection) && preparationAttempt === attempt && preparationController === request;
  try {
    const result = await fetchPeriodPreparation(readContext, period, request.signal);
    if (!valid()) return;
    preparation.value = result; preparationStatus.value = "ready";
  } catch (caught) {
    if (!valid() || (caught instanceof DOMException && caught.name === "AbortError")) return;
    preparationStatus.value = isDashboardSnapshotChanged(caught) ? "stale" : "error";
    preparationError.value = preparationStatus.value === "stale" ? "资料已变化，本次检查已过期。请刷新简报后重新核对。" : dashboardErrorMessage(caught);
  } finally { if (valid()) preparationController = null; }
}

async function loadData(period: string | null) {
  const generation = ++requestGeneration;
  const selection = selectionKey();
  const companyId = route.query.company_id;
  resetPreparation();
  controller?.abort();
  for (const request of pageControllers.values()) request.abort();
  pageControllers.clear(); sectionLoading.value = {}; sectionErrors.value = {};
  const request = new AbortController();
  controller = request;
  loading.value = true;
  response.value = null;
  error.value = "";
  try {
    if (typeof companyId !== "string") throw new Error("No selected company");
    const target = typeof route.query.voucher === "string" ? route.query.voucher : undefined;
    const result = await fetchDeferredBrief(companyId, period, request.signal, undefined,
      target ? /^\d+$/.test(target) ? { voucher_number: Number(target) } : { voucher_version_id: target } : {});
    if (isCurrent(generation, selection) && controller === request) {
      response.value = result;
      loading.value = false;
      await nextTick();
      if (isCurrent(generation, selection) && controller === request) {
        if (target) focusSection("activity");
        void loadPreparation();
      }
    }
  } catch (caught: unknown) {
    if (!isCurrent(generation, selection) || (caught instanceof DOMException && caught.name === "AbortError")) return;
    error.value = dashboardErrorMessage(caught);
  } finally {
    if (isCurrent(generation, selection) && controller === request) loading.value = false;
  }
}

async function loadMore(section: BriefSection = "vouchers", restart = false) {
  const current = response.value;
  const page = current?.data?.collections[section]?.page;
  if (!current?.data || (!restart && page && (!page.has_more || !page.next_cursor)) || sectionLoading.value[section]) return;
  const generation = requestGeneration, selection = selectionKey();
  const request = new AbortController(); pageControllers.set(section, request);
  sectionLoading.value[section] = true; sectionErrors.value[section] = "";
  const valid = () => isCurrent(generation, selection) && pageControllers.get(section) === request;
  try {
    const next = await fetchDeferredBrief(current.read_context.company_id, selectedPeriod.value, request.signal, current.snapshot_version, { section, cursor: restart ? undefined : page?.next_cursor ?? undefined });
    if (!valid() || !next.data || !response.value?.data) return;
    const latest = response.value, before = latest.data!;
    const collection = next.data.collections[section];
    const groups = (next.data.activity_groups ?? []).map(group => ({ ...group, rows: [...(before.activity_groups.find(item => item.key === group.key)?.rows ?? []), ...group.rows] }));
    for (const group of before.activity_groups) if (!groups.some(item => item.key === group.key)) groups.push(group);
    response.value = { ...latest, data: { ...before,
      ...(section === "vouchers" ? { vouchers: [...before.vouchers, ...next.data.vouchers], voucher_page: next.data.voucher_page, activity_groups: groups } : {}),
      collections: { ...before.collections, [section]: { ...collection, items: [...(restart ? [] : before.collections[section]?.items ?? []), ...collection.items] } },
    } };
  } catch (caught) {
    if (!valid()) return;
    if (isDashboardSnapshotChanged(caught)) {
      updateNotice.value = "资料已更新，正在重新读取。";
      if (section === "file_jobs" && !restart) {
        sectionLoading.value[section] = false;
        if (response.value?.data) { const collections = { ...response.value.data.collections }; delete collections.file_jobs; response.value = { ...response.value, data: { ...response.value.data, collections } }; }
        await loadMore(section, true);
      } else await refresh();
    } else sectionErrors.value[section] = dashboardErrorMessage(caught);
  } finally { if (valid()) { sectionLoading.value[section] = false; pageControllers.delete(section); } }
}

function openBusinessSection(event: Event, section: BriefQuery["section"]) {
  if ((event.target as HTMLDetailsElement).open && !data.value?.collections[section!]) void loadMore(section);
}

async function refresh() {
  invalidateRequests();
  const generation = requestGeneration, selection = selectionKey();
  try {
    await refreshContext();
    if (isCurrent(generation, selection)) await loadData(queryPeriod());
  } catch (caught) { if (isCurrent(generation, selection)) error.value = dashboardErrorMessage(caught); }
}

async function initialize() {
  initialized = true;
  const generation = requestGeneration, selection = selectionKey();
  try {
    const loadedContext = await loadContext();
    if (!isCurrent(generation, selection)) return;
    initialized = true;
    const requested = queryPeriod();
    const target = requested || loadedContext.default_period;
    if (!requested && target) {
      await router.replace({ query: { ...route.query, period: target } });
      return;
    }
    await loadData(target);
  } catch (caught: unknown) {
    if (!isCurrent(generation, selection)) return;
    error.value = dashboardErrorMessage(caught);
    loading.value = false;
  }
}

function changePeriod(value: string) {
  void router.push({ query: { company_id: route.query.company_id, period: value }, hash: "" });
}

function runPriorityAction(action: PriorityAction) {
  if (action === "validation") { focusSection("validation"); return; }
  if (action !== "bank-details") return;
  void router.push({
    name: "funds",
    query: { company_id: route.query.company_id, period: selectedPeriod.value || undefined, funds_view: "bank" },
    hash: "#bank-details",
  });
}

function statusLabel(status: string) {
  return status === "closed" ? "已关账" : "未关账";
}

function generatedText() {
  if (!data.value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(data.value.generated_at));
}

function heroNote() {
  const period = response.value?.selected_period;
  if (!period) return "";
  if (period.status !== "closed") {
    return "截至本月末 · 已入账记录";
  }
  const closedAt = period.closed_at ? `${new Date(period.closed_at).toLocaleString("zh-CN")} ` : "";
  return `${closedAt}完成关账；以下金额反映该月末情况。`;
}

function selectionKey() { return JSON.stringify([route.query.company_id, route.query.period, route.query.voucher]); }
function isCurrent(generation: number, selection: string) { return mounted && requestGeneration === generation && selectionKey() === selection; }
function invalidateRequests() {
  requestGeneration += 1;
  resetPreparation();
  controller?.abort();
  for (const request of pageControllers.values()) request.abort();
  pageControllers.clear(); sectionLoading.value = {}; sectionErrors.value = {};
  controller = null; response.value = null; loading.value = false;
}

watch(
  () => [route.query.company_id, route.query.period, route.query.voucher],
  (value, previous) => {
    if (value.every((item, index) => item === previous[index])) return;
    invalidateRequests();
  },
  { flush: "sync" },
);
watch(
  () => [context.value?.current_company?.company_id, route.query.period, route.query.voucher] as const,
  ([orgId, period, voucher], [previousOrgId, previousPeriod, previousVoucher]) => {
    if (!initialized || !orgId) return;
    if (orgId !== previousOrgId || period !== previousPeriod || voucher !== previousVoucher) {
      void loadData(typeof period === "string" ? period : null);
    }
  },
);
watch(sectionLinks, (links) => {
  if (!links.some((link) => link.id === activeSection.value)) activeSection.value = "overview";
});
async function revealCollection() {
  const key = route.query.section;
  if (typeof key !== "string" || !businessSections.some(item => item.key === key) || !data.value) return;
  const generation = requestGeneration, selection = selectionKey();
  await nextTick();
  if (!isCurrent(generation, selection) || route.query.section !== key) return;
  const detail = document.getElementById(`brief-${key}`) as HTMLDetailsElement | null;
  if (detail) { detail.open = true; detail.scrollIntoView({ block: "start" }); detail.querySelector("summary")?.focus({ preventScroll: true }); }
}
watch(() => [route.query.section, !!data.value], () => { void revealCollection(); });
onMounted(() => {
  void initialize();
});
onBeforeUnmount(() => {
  mounted = false;
  invalidateRequests();
});
</script>

<template>
  <section class="brief-page">
      <DashboardModuleHeader
        title="月度经营与财务概览"
        :options="periodOptions"
      :selected="selectedPeriod"
      :loading="loading"
      select-label="查看月份"
      @change="changePeriod"
      @refresh="refresh"
    />

    <div v-if="error" class="state-panel error" role="alert">
      <h2>经营简报加载失败</h2>
      <p>{{ error }}</p>
      <button type="button" @click="loadData(queryPeriod())">重新加载</button>
    </div>
    <div v-else-if="loading && !data" class="state-panel" role="status">
      正在读取所选月份的经营简报…
    </div>
    <div v-else-if="!data" class="state-panel">
      <h2>还没有可查看的月份</h2>
      <p>录入公司业务后，即可按月份查看经营情况。</p>
    </div>

    <template v-else>
      <DashboardSectionNav
        :items="sectionLinks"
        :active="activeSection"
        label="经营简报区段"
        floating
        @select="focusSection"
      />

      <p v-if="updateNotice" role="status" class="update-notice">{{ updateNotice }}</p>
      <section id="overview" class="cockpit section-anchor" tabindex="-1">
        <div class="cockpit-copy">
          <div class="cockpit-meta">
            <span>{{ response?.selected_period?.short_label }}</span>
            <span>数据生成于 {{ generatedText() }}</span>
          </div>
          <h2>{{ response?.selected_period?.short_label }}经营简报</h2>
          <p class="hero-note">{{ heroNote() }}</p>
          <div class="status-rail" aria-label="本月状态">
            <span :class="['status-chip', { error: data.validation.integrity_valid === false }]">
              {{ data.validation.integrity_valid === null ? "依据待核对" : data.validation.integrity_valid ? "账面平衡" : "账务异常" }}
            </span>
            <span
              :class="['status-chip', { attention: data.cash.unmatched_count + data.cash.needs_review_count }]"
            >
              {{
                data.cash.unmatched_count + data.cash.needs_review_count
                  ? `${data.cash.unmatched_count + data.cash.needs_review_count} 笔匹配状态待核对`
                  : data.cash.missing_account_count ? `${data.cash.missing_account_count} 个账户未提供流水`
                    : ['missing', 'partial'].includes(data.cash.coverage_state) ? '流水覆盖尚不能完整确认'
                    : data.cash.coverage_state === 'not_applicable' ? '无银行账户'
                    : data.cash.transaction_count
                    ? "银行流水已匹配"
                    : "本月无银行流水"
              }}
            </span>
            <span class="status-chip">
              {{ response?.selected_period?.status === "closed" ? "本月已关账" : "本月可继续补录" }}
            </span>
          </div>
          <div class="takeaway">
            <span>经营结论</span>
            <strong>{{ takeaway }}</strong>
            <details v-if="data.management_commentary_details?.status === 'stale' && data.management_commentary_details.latest">
              <summary>账务已更新，查看之前的经营说明</summary>
              <p>{{ data.management_commentary_details.latest.text }}</p>
            </details>
            <details v-if="data.management_commentary_details?.supplements.length">
              <summary>关账后的补充说明</summary>
              <p v-for="note in data.management_commentary_details.supplements" :key="note.id">{{ note.text }}</p>
            </details>
          </div>
        </div>

        <aside class="action-queue" aria-label="需要处理">
          <header>
            <span class="queue-title">需要处理</span>
            <span :class="['queue-count', { healthy: !priorities.length }]">
              {{ priorities.length ? "分类提示" : "核对概况" }}
            </span>
          </header>
          <div v-if="priorities.length" class="priority-list">
            <component
              :is="item.action ? 'button' : 'article'"
              v-for="item in priorities"
              :key="item.title"
              :class="[item.state, { 'priority-action': item.action }]"
              :type="item.action ? 'button' : undefined"
              @click="item.action && runPriorityAction(item.action)"
            >
              <span aria-hidden="true" />
              <div>
                <strong>{{ item.title }}</strong>
                <small>{{ item.note }}</small>
              </div>
            </component>
          </div>
          <div v-else class="healthy-summary">
            <strong>{{ data.validation.title }}</strong>
            <span>{{ data.validation.summary }}。</span>
          </div>
        </aside>
      </section>

      <section class="kpi-grid" aria-label="本月核心指标">
        <article :class="['kpi', 'result', { loss: data.position.month_result_fen !== null && fen(data.position.month_result_fen) < 0n }]"><span class="kpi-label">本月账面盈亏</span><strong>{{ formatFen(data.position.month_result_fen) }}</strong><small>收入 {{ formatFen(data.position.month_revenue_fen) }} · 费用 {{ formatFen(data.position.month_expense_fen) }}</small></article>
        <article class="kpi bank"><span class="kpi-label">月末账面资金</span><strong>{{ formatFen(data.funds_overview.total_fen) }}</strong><small>银行、现金和支付平台的账面余额</small></article>
        <button class="kpi open" type="button" @click="focusSection('open-items')"><span class="kpi-label">月末待收</span><strong>{{ formatFen(data.open_items.receivable_fen) }}</strong><small>{{ data.open_items.receivable_count }} 项来源 · 查看构成 ›</small></button>
        <button class="kpi open" type="button" @click="focusSection('open-items')"><span class="kpi-label">月末待付</span><strong>{{ formatFen(data.open_items.payable_fen) }}</strong><small>{{ data.open_items.payable_count }} 项来源 · 查看构成 ›</small></button>
      </section>
      <p v-if="data.position.complete === false || data.open_items.complete === false || data.open_items.unestablished_count" class="needs-check" role="status">部分来源尚待核对，已知金额也不能视为完整结论。<button type="button" @click="focusSection('validation')">查看依据与问题</button></p>

      <div id="open-items" class="section-anchor" tabindex="-1">
        <BriefOpenItems
          :open-items="data.open_items"
          :period-label="response?.selected_period?.short_label || ''"
          :period-status="response?.selected_period?.status || ''"
          :period="selectedPeriod" :snapshot-version="response?.snapshot_version" @changed="refresh"
        />
      </div>

      <div id="activity" class="section-anchor" tabindex="-1">
        <BriefActivityWorkbench
          :groups="data.activity_groups"
          :vouchers="data.vouchers"
          :voucher-count="data.voucher_count"
          :line-count="data.line_count"
          :focused-voucher="data.focused_voucher"
        />
        <p class="muted">已加载 {{ data.vouchers.length }} / {{ data.voucher_count }} 张凭证；本页汇总按全月计算。</p>
        <DashboardPagination :page="data.collections.vouchers?.page" :loaded="data.vouchers.length" :loading="sectionLoading.vouchers" :error="sectionErrors.vouchers" @retry="loadMore()" @more="loadMore()" />

      </div>

      <div v-if="data.workforce_cost.has_activity" id="workforce" class="section-anchor" tabindex="-1">
        <BriefWorkforceSection
          :workforce="data.workforce_cost"
          :period-key="selectedPeriod"
          :period-label="response?.selected_period?.short_label || ''"
        />
      </div>

      <div id="finance" class="section-anchor" tabindex="-1">
        <BriefFinancialOverview
          :cash="data.cash"
          :funds="data.funds_overview"
          :position="data.position"
          :unmatched="data.unmatched_bank_activity"
        />
      </div>

      <section class="collection-details" aria-label="业务与相关跟进明细"><h2>业务与相关跟进明细</h2>
      <details v-for="section in businessSections" :key="section.key" :id="`brief-${section.key}`" class="brief-section" @toggle="openBusinessSection($event, section.key)">
        <summary>{{ section.label }}</summary>
        <template v-if="data.collections[section.key]">
          <DashboardPagination :page="data.collections[section.key].page" :loaded="data.collections[section.key].items.length" :loading="sectionLoading[section.key]" :error="sectionErrors[section.key]" @retry="loadMore(section.key)" @more="loadMore(section.key)" />
          <DashboardBusinessRecords :items="data.collections[section.key].items" :period="selectedPeriod" :snapshot-version="response?.snapshot_version" @changed="refresh" />
        </template>
        <p v-if="sectionErrors[section.key] && !data.collections[section.key]" role="alert">{{ sectionErrors[section.key] }}</p>
        <button v-if="!data.collections[section.key]" type="button" :disabled="sectionLoading[section.key]" @click="loadMore(section.key)">{{ sectionLoading[section.key] ? "加载中…" : "读取明细" }}</button>
      </details>
      </section>
      <div id="validation" class="final-section-space section-anchor" tabindex="-1">
      <details v-if="data.position.issues.length" class="brief-section"><summary>财务位置来源核对提示 · {{ data.position.issues.length }} 条</summary><ul><li v-for="(issue, index) in data.position.issues" :key="index">{{ issue.message }}<details><summary>查看精确来源</summary><pre>{{ JSON.stringify(issue, null, 2) }}</pre></details></li></ul></details>
      <PeriodPreparation v-if="preparationStatus === 'ready' && data.period_preparation" :preparation="data.period_preparation" :snapshot-version="response?.snapshot_version" @changed="refresh" />
      <section v-else class="state-panel" :role="preparationStatus === 'error' || preparationStatus === 'stale' ? 'alert' : 'status'">
        <strong>{{ preparationStatus === 'stale' ? '准备检查已过期' : preparationStatus === 'error' ? '准备检查读取失败' : '正在核对资料与期间准备' }}</strong>
        <p>{{ preparationError || '主数据已显示，准备检查尚未完成。' }}</p>
        <button v-if="preparationStatus === 'error'" type="button" @click="loadPreparation">重试准备检查</button>
        <button v-if="preparationStatus === 'stale'" type="button" @click="refresh">刷新简报</button>
      </section>
      <footer
        id="validation-checks"
        :class="['trust-footer', 'section-anchor', data.validation.state]"
        tabindex="-1"
      >
        <div class="trust-heading">
          <div>
            <p>资料与账务核对</p>
            <h2>{{ data.validation.title }}</h2>
            <span>{{ data.validation.summary }}。</span>
          </div>
          <span :class="['trust-state', data.validation.state]">
            {{ preparationStatus !== 'ready' ? '准备检查未完成' : data.validation.state === 'complete' ? "本月核对完成" : "需要核对" }}
          </span>
        </div>
        <details :open="data.validation.items.some(item => item.state !== 'pass')"><summary>{{ preparationStatus === 'ready' && data.validation.items.length > 0 && data.validation.items.every(item => item.state === 'pass') ? '本月检查已通过，查看详情' : '查看需要核对的项目' }}</summary><div class="checks">
          <article v-for="item in data.validation.items" :key="item.key" :class="item.state">
            <span class="check-mark">
              {{ item.state === "pass" ? "✓" : item.state === "error" ? "×" : item.state === "pending" ? "!" : "–" }}
            </span>
            <div>
              <strong>{{ item.label }}</strong>
              <small>{{ item.text }}</small>
            </div>
          </article>
        </div></details>
        <details v-if="data.validation.issues?.length" class="trust-proof" open>
          <summary>{{ isClosed ? '当前需要跟进的事项（不改变原关账结论）' : '核算准备与待复核事项' }}</summary>
          <ul>
            <li v-for="(issue, index) in data.validation.issues" :key="index">{{ issue.message }}</li>
          </ul>
        </details>
        <details
          v-if="data.material_completeness && !data.material_completeness.closed"
          class="trust-proof"
          :open="!data.material_completeness.satisfied"
        >
          <summary>关账前资料核对：{{ data.material_completeness.satisfied ? "已逐项核对" : "还有待处理项目" }}</summary>
          <ul v-if="data.material_completeness.issues.length">
            <li v-for="(issue, index) in data.material_completeness.issues" :key="index">
              <strong v-if="issue.location">{{ issue.source_name }} {{ issue.location }}：</strong>{{ issue.message }}
              <span v-if="issue.excerpt"> {{ issue.excerpt }}</span>
              <span v-if="issue.difference_fen != null">差额 {{ formatFen(issue.difference_fen) }} 元</span>
            </li>
          </ul>
          <p v-if="data.material_completeness.company_notes">公司业务说明：{{ data.material_completeness.company_notes.path }}</p>
        </details>
        <details class="trust-proof">
          <summary>查看本月校验依据</summary>
          <dl>
            <div><dt>正式凭证 / 分录</dt><dd>{{ data.voucher_count }} 张 / {{ data.line_count }} 行</dd></div>
            <div><dt>借方合计 / 贷方合计</dt><dd>{{ formatFen(data.total_debit_fen) }} / {{ formatFen(data.total_credit_fen) }}</dd></div>
            <div><dt>银行当前有效匹配</dt><dd>{{ data.cash.matched_count }} / {{ data.cash.transaction_count }}</dd></div>
            <div><dt>期间状态</dt><dd>{{ statusLabel(response?.selected_period?.status || "") }}</dd></div>
            <div><dt>页面数据生成时间</dt><dd>{{ generatedText() }}</dd></div>
          </dl>
        </details>
      </footer>
      </div>
    </template>
  </section>
</template>

<style scoped>
.update-notice { margin: 0 0 16px; color: var(--muted); font-size: 13px; }
.needs-check { padding: 12px; border-radius: 10px; color: var(--warning); background: var(--warning-soft); }
.needs-check button { margin-left: 12px; cursor: pointer; background: transparent; border: 0; text-decoration: underline; }
.collection-details { margin: 20px 0; }
.collection-details > h2 { font-size: 19px; }
.collection-details > details { padding: 14px 18px; margin: 8px 0; scroll-margin-top: 70px; }
.collection-details summary { cursor: pointer; }

.brief-page {
  --brief-page: var(--background);
  --brief-surface: var(--surface);
  --brief-soft: var(--surface-soft);
  --brief-text: var(--text);
  --brief-muted: var(--muted);
  --brief-line: var(--line);
  --brief-line-strong: var(--line-strong);
  --brief-green: var(--accent);
  --brief-green-soft: var(--accent-soft);
  --brief-blue: var(--info);
  --brief-blue-soft: var(--info-soft);
  --brief-gold: var(--gold);
  --brief-gold-soft: var(--gold-soft);
  --brief-amber: var(--warning);
  --brief-amber-soft: var(--warning-soft);
  --brief-red: var(--danger);
  --brief-red-soft: var(--danger-soft);
  --brief-shadow: var(--shadow-soft);
  width: min(calc(100% - 48px), 1320px);
  margin: 0 auto;
  padding: 25px 0 46px;
  color: var(--brief-text);
}

.state-panel {
  padding: 28px;
  border: 1px solid var(--brief-line);
  border-radius: 18px;
  background: var(--brief-surface);
  box-shadow: var(--brief-shadow);
}

.state-panel h2,
.state-panel p {
  margin-top: 0;
}

.state-panel.error {
  border-color: var(--brief-red);
}

.state-panel button {
  min-height: 40px;
  padding: 0 13px;
  border: 0;
  border-radius: 10px;
  background: var(--brief-green);
  color: var(--brief-surface);
  cursor: pointer;
}

.section-anchor {
  scroll-margin-top: 78px;
}

.cockpit {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(300px, 0.75fr);
  gap: 25px;
  min-height: 198px;
  padding: 23px 25px;
  border: 1px solid color-mix(in srgb, var(--brief-green) 20%, var(--brief-line));
  border-radius: 20px;
  background:
    radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--brief-green) 11%, transparent), transparent 32%),
    linear-gradient(125deg, var(--brief-surface), color-mix(in srgb, var(--brief-green-soft) 66%, var(--brief-surface)));
  box-shadow: var(--brief-shadow);
}

.cockpit-meta,
.status-rail {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 13px;
  color: var(--brief-muted);
  font-size: 11px;
}

.cockpit-meta span:first-child {
  color: var(--brief-green);
  font-weight: 850;
  letter-spacing: 0.06em;
}

.cockpit h2 {
  margin: 7px 0 3px;
  font-size: clamp(25px, 2.8vw, 34px);
  letter-spacing: -0.04em;
}

.hero-note {
  margin: 0;
  color: var(--brief-muted);
  font-size: 12px;
}

.status-rail {
  margin-top: 11px;
}

.status-chip,
.queue-count,
.trust-state {
  display: inline-flex;
  min-height: 25px;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--brief-green-soft);
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 800;
}

.status-chip.attention,
.trust-state.attention {
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.status-chip.error,
.trust-state.error {
  background: var(--brief-red-soft);
  color: var(--brief-red);
}

.takeaway {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  margin-top: 13px;
  padding-top: 11px;
  border-top: 1px solid color-mix(in srgb, var(--brief-green) 18%, var(--brief-line));
}

.takeaway span {
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 850;
  white-space: nowrap;
}

.takeaway details { grid-column: 1 / -1; font-size: 13px; }
.takeaway summary { cursor: pointer; color: var(--muted); }

.takeaway strong {
  font-size: 14px;
  line-height: 1.55;
}

.action-queue {
  min-width: 0;
  padding: 14px;
  border: 1px solid color-mix(in srgb, var(--brief-line) 82%, transparent);
  border-radius: 15px;
  background: color-mix(in srgb, var(--brief-surface) 83%, transparent);
}

.action-queue header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 13px;
}

.queue-title,
.healthy-summary span,
.priority-list small {
  color: var(--brief-muted);
  font-size: 11px;
}

.queue-count {
  min-width: 27px;
  justify-content: center;
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.queue-count.healthy {
  background: var(--brief-green-soft);
  color: var(--brief-green);
}

.priority-list {
  display: grid;
  gap: 6px;
  margin-top: 9px;
}

.priority-list article,
.priority-list .priority-action {
  display: grid;
  grid-template-columns: 6px minmax(0, 1fr);
  gap: 9px;
  align-items: center;
  width: 100%;
  padding: 7px 9px;
  border: 0;
  border-radius: 9px;
  background: var(--brief-soft);
  color: inherit;
  font: inherit;
  text-align: left;
}

.priority-list .priority-action {
  cursor: pointer;
}

.priority-list .priority-action:hover {
  background: color-mix(in srgb, var(--brief-soft) 72%, var(--brief-amber-soft));
}

.priority-list .priority-action:focus-visible {
  outline: 2px solid var(--brief-amber);
  outline-offset: 2px;
}

.priority-list article > span,
.priority-list .priority-action > span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--brief-muted);
}

.priority-list article.attention > span,
.priority-list .priority-action.attention > span {
  background: var(--brief-amber);
}

.priority-list article.error > span,
.priority-list .priority-action.error > span {
  background: var(--brief-red);
}

.priority-list article > div,
.priority-list .priority-action > div,
.healthy-summary {
  display: grid;
}

.priority-list strong {
  font-size: 12px;
}

.healthy-summary {
  gap: 4px;
  margin-top: 17px;
  padding: 13px;
  border-radius: 11px;
  background: var(--brief-green-soft);
  color: var(--brief-green);
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-top: 10px;
}

.kpi {
  position: relative;
  display: grid;
  min-width: 0;
  min-height: 126px;
  align-content: space-between;
  gap: 4px;
  overflow: hidden;
  padding: 15px 16px;
  border: 1px solid var(--brief-line);
  border-radius: 16px;
  background: var(--brief-surface);
  color: var(--brief-text);
  box-shadow: var(--brief-shadow);
  font: inherit;
  text-align: left;
}

.kpi::before {
  position: absolute;
  top: 0;
  right: 0;
  left: 0;
  height: 3px;
  background: var(--brief-green);
  content: "";
}

.kpi.bank::before,
.kpi.open::before {
  background: var(--brief-blue);
}

.kpi.asset::before {
  background: var(--brief-gold);
}

.kpi.result.loss::before {
  background: var(--brief-red);
}

button.kpi {
  cursor: pointer;
}

button.kpi:hover,
button.kpi:focus-visible {
  border-color: var(--brief-green);
  transform: translateY(-1px);
}

.kpi-label {
  color: var(--brief-muted);
  font-size: 12px;
  font-weight: 750;
}

.kpi > strong {
  overflow-wrap: anywhere;
  font-size: clamp(20px, 2vw, 27px);
  line-height: 1.15;
  letter-spacing: -0.025em;
}

.kpi.result:not(.loss) > strong {
  color: var(--brief-green);
}

.kpi.result.loss > strong {
  color: var(--brief-red);
}

.kpi.asset > strong {
  color: var(--brief-gold);
}

.kpi.bank > strong,
.kpi.open > strong {
  color: var(--brief-blue);
}

.kpi small {
  color: var(--brief-muted);
  font-size: 11px;
  line-height: 1.4;
}

.kpi small b {
  color: var(--brief-green);
}

.kpi-grid + .section-anchor,
.section-anchor + .section-anchor {
  margin-top: 12px;
}

.final-section-space {
  min-height: calc(100vh - 80px);
}

.trust-footer {
  margin-top: 12px;
  padding: 15px 18px;
  border: 1px solid var(--brief-line);
  border-left: 3px solid var(--brief-green);
  border-radius: 16px;
  background: var(--brief-surface);
  box-shadow: var(--brief-shadow);
}

.trust-footer.attention {
  border-left-color: var(--brief-amber);
}

.trust-footer.error {
  border-left-color: var(--brief-red);
}

.trust-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.trust-heading p {
  margin: 0 0 3px;
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

.trust-heading h2 {
  margin: 0;
  font-size: 20px;
}

.trust-heading > div > span {
  color: var(--brief-muted);
  font-size: 12px;
}

.checks {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 7px;
  margin-top: 9px;
}

.checks article {
  display: grid;
  min-width: 0;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 8px;
  padding: 7px 8px;
  border-radius: 10px;
  background: var(--brief-soft);
}

.check-mark {
  display: grid;
  width: 21px;
  height: 21px;
  place-items: center;
  border-radius: 50%;
  background: var(--brief-green-soft);
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 850;
}

.checks article.pending .check-mark {
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.checks article.error .check-mark {
  background: var(--brief-red-soft);
  color: var(--brief-red);
}

.checks article.neutral .check-mark {
  background: var(--brief-line);
  color: var(--brief-muted);
}

.checks article > div {
  display: grid;
  min-width: 0;
}

.checks strong {
  font-size: 11px;
}

.checks small {
  overflow: hidden;
  color: var(--brief-muted);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trust-proof {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--brief-line);
}

.trust-proof summary {
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 750;
  cursor: pointer;
}

.trust-proof dl {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 18px;
  margin: 10px 0 0;
}

.trust-proof dl > div {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  padding-bottom: 5px;
  border-bottom: 1px solid var(--brief-line);
  font-size: 11px;
}

.trust-proof dt {
  color: var(--brief-muted);
}

.trust-proof dd {
  margin: 0;
  font-weight: 800;
  text-align: right;
}

@media (max-width: 1080px) {
  .cockpit {
    grid-template-columns: minmax(0, 1.3fr) minmax(275px, 0.8fr);
  }

  .kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .checks {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .brief-page {
    width: min(calc(100% - 24px), 1320px);
    padding: 16px 0 24px;
  }

  .cockpit {
    grid-template-columns: 1fr;
    gap: 13px;
    padding: 19px;
    border-radius: 17px;
  }

  .takeaway {
    grid-template-columns: 1fr;
    gap: 3px;
  }

  .action-queue {
    padding: 12px;
  }

  .kpi-grid {
    grid-template-columns: 1fr;
  }

  .kpi {
    min-height: 116px;
    padding: 12px;
  }

  .section-anchor {
    scroll-margin-top: 68px;
  }

  .trust-footer {
    padding: 16px;
  }

  .trust-heading {
    flex-direction: column;
    gap: 8px;
  }

  .checks,
  .trust-proof dl {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .checks small {
    white-space: normal;
  }

  .trust-proof summary {
    display: flex;
    min-height: 44px;
    align-items: center;
  }
}

@media (max-width: 430px) {
  .cockpit h2 {
    font-size: 26px;
  }

  .kpi > strong {
    font-size: 23px;
  }
}
</style>
