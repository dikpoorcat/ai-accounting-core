<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { fetchDeferredBrief, type BriefQuery, type BriefValidationItem } from "../api/brief";
import { fetchPeriodPreparation, type PeriodPreparationResult } from "../api/periodPreparation";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import PeriodPreparation from "../components/PeriodPreparation.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import BriefActivityWorkbench from "../components/brief/BriefActivityWorkbench.vue";
import BriefFinancialOverview from "../components/brief/BriefFinancialOverview.vue";
import BriefOpenItems from "../components/brief/BriefOpenItems.vue";
import BriefWorkforceSection from "../components/brief/BriefWorkforceSection.vue";
import CloseReviewPanel from "../components/CloseReviewPanel.vue";
import { useDashboardSections } from "../composables/useDashboardSections";
import { useDashboardContext } from "../composables/useDashboardContext";
import { fen, formatFen } from "../utils/money";

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
type CommentaryItem = NonNullable<NonNullable<Awaited<ReturnType<typeof fetchDeferredBrief>>["data"]>["management_commentary_details"]["latest"]>;
const sectionLoading = ref<Partial<Record<BriefSection, boolean>>>({});
const sectionErrors = ref<Partial<Record<BriefSection, string>>>({});
const pageControllers = new Map<BriefSection, AbortController>();
const updateNotice = ref("");
const error = ref("");
const openItemsFocusRequest = ref(0);
const closeReviewRefreshKey = ref(0);
let controller: AbortController | null = null;
let initialized = false;
let mounted = true;
let requestGeneration = 0;
const selectedPeriod = computed(() => response.value?.selected_period?.key || "");
const periodOptions = computed(() => context.value?.periods || []);
const briefTitle = computed(() => {
  const period = selectedPeriod.value || (typeof route.query.period === "string" ? route.query.period : "");
  const month = /^\d{4}-(\d{2})$/.exec(period)?.[1];
  return month ? `${Number(month)} 月经营简报` : "经营简报";
});
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
const vouchers = computed(() => data.value?.collections.vouchers?.items ?? []);
const openItemRecords = computed(() => data.value?.collections.open_items?.items ?? []);
const isClosed = computed(() => response.value?.selected_period?.status === "closed");
const sectionLinks = computed(() => {
  if (!data.value) return [];
  const links = [
    { id: "overview", label: "概览" },
    { id: "activity", label: "业务凭证" },
  ];
  if (data.value?.workforce_cost.has_activity) links.push({ id: "workforce", label: "用工" });
  links.push(
    { id: "finance", label: "资金资产" },
    { id: "open-items", label: "待收待付" },
    { id: "validation", label: "账务与待办" },
  );
  return links;
});
const { activeSection, focusSection } = useDashboardSections(sectionLinks, "overview");

function focusOutstandingItems() {
  focusSection("open-items");
  openItemsFocusRequest.value += 1;
}
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
  return "";
});

const hasOverviewNotes = computed(() => Boolean(takeaway.value
  || (data.value?.management_commentary_details?.status === "stale" && data.value.management_commentary_details.latest)
  || data.value?.management_commentary_details?.supplements.length));

function commentaryValidityLabel(item: CommentaryItem) {
  return ({ current: "与当前账务一致", frozen: "关账时已冻结", stale: "账务变化后已过期", unverifiable: "现有依据无法核验" } as const)[item.content_validity.status];
}

function commentaryValidityReason(item: CommentaryItem) {
  if (!item.content_validity.reason) return "";
  return ({ context_changed: "相关账务或业务说明已变化", missing_basis: "缺少原说明的核验依据", unsupported_contract: "说明依据版本暂不能核验" } as Record<string, string>)[item.content_validity.reason]
    ?? "具体原因保留在核验记录中";
}

function queryPeriod() {
  return typeof route.query.period === "string" ? route.query.period : null;
}

function ownerCheckLabel(item: BriefValidationItem) {
  return ({
    balance: "账面平衡",
    materials: "资料齐全",
    accounting: "业务已处理",
    close_requirements: "月末条件",
  } as Record<string, string>)[item.key] ?? item.label;
}

function ownerCheckText(item: BriefValidationItem) {
  const copy: Record<string, [string, string]> = {
    balance: ["借贷和资产负债关系一致", "凭证或余额关系需要核对"],
    materials: ["本月资料已逐项核对", "还有资料需要补充或确认"],
    accounting: ["应处理业务均已正式入账", "仍有业务未发布或待复核"],
    close_requirements: ["银行与业务条件已满足", "还有月末条件未满足"],
  };
  const message = copy[item.key];
  return message ? message[item.state === "pass" ? 0 : 1] : item.text;
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
      closeReviewRefreshKey.value += 1;
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
    if (!collection) return;
    response.value = { ...latest, data: { ...before,
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

function focusSelectedCard(event: MouseEvent) {
  if (!(event.target instanceof Element)) return;
  const card = event.target.closest<HTMLElement>(".selectable-card");
  if (!card) return;
  if (event.target.closest("button, a, summary, input, select, textarea")) return;
  card.focus({ preventScroll: true });
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
onMounted(() => {
  void initialize();
});
onBeforeUnmount(() => {
  mounted = false;
  invalidateRequests();
});
</script>

<template>
  <section class="brief-page" @click="focusSelectedCard">
    <DashboardModuleHeader
      class="brief-header"
      data-section-header
      :title="briefTitle"
      :options="periodOptions"
      :selected="selectedPeriod"
      :loading="loading"
      select-label="查看月份"
      @change="changePeriod"
      @refresh="refresh"
    >
      <template #navigation>
        <DashboardSectionNav
          v-if="data"
          :items="sectionLinks"
          :active="activeSection"
          label="经营简报区段"
          @select="focusSection"
        />
      </template>
    </DashboardModuleHeader>

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
      <p v-if="updateNotice" role="status" class="update-notice">{{ updateNotice }}</p>
      <section id="overview" class="brief-hero section-anchor" aria-label="本月核心指标" tabindex="-1">
        <div class="brief-hero-topline">
          <p class="dashboard-hero-eyebrow">{{ response?.selected_period?.label }}期末 · 全公司</p>
          <div class="status-rail" aria-label="本月状态">
            <span :class="['status-chip', { error: data.validation.integrity_valid === false, attention: data.validation.integrity_valid === null }]">
              {{ data.validation.integrity_valid === null ? "依据待核对" : data.validation.integrity_valid ? "账面平衡" : "账务异常" }}
            </span>
            <span
              :class="['status-chip', { attention: data.cash.unmatched_count + data.cash.needs_review_count || data.cash.missing_account_count || ['missing', 'partial'].includes(data.cash.coverage_state) }]"
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
          </div>
        </div>
        <div class="hero-metric">
          <h2>本月账面盈亏</h2>
          <strong class="dashboard-hero-title" :class="{ loss: data.position.month_result_fen !== null && fen(data.position.month_result_fen) < 0n }">{{ formatFen(data.position.month_result_fen) }}</strong>
          <p class="dashboard-hero-note">=收入 {{ formatFen(data.position.month_revenue_fen) }} - 费用 {{ formatFen(data.position.month_expense_fen) }}</p>
        </div>
        <section class="kpi-grid" aria-label="资金、资产与往来概况">
          <button class="kpi funds selectable-card" type="button" @click="router.push({ name: 'funds', query: { company_id: route.query.company_id, period: selectedPeriod || undefined }, hash: '#funds-overview' })">
            <span class="kpi-label">月末账面资金</span>
            <strong>{{ formatFen(data.funds_overview.total_fen) }}</strong>
            <small>银行、现金和支付平台的账面余额</small>
          </button>
          <button class="kpi asset selectable-card" type="button" @click="router.push({ name: 'assets', query: { company_id: route.query.company_id, period: selectedPeriod || undefined }, hash: '#assets-overview' })">
            <span class="kpi-label">长期资产净值</span>
            <strong>{{ formatFen(data.long_term_assets.net_fen) }}</strong>
            <small>固定 {{ data.long_term_assets.fixed_active_count }} 项 · 无形 {{ data.long_term_assets.intangible_active_count }} 项</small>
          </button>
          <button class="kpi receivable selectable-card" type="button" @click="focusSection('open-items')">
            <span class="kpi-label">{{ isClosed ? "关账时待收" : "月末待收" }}</span>
            <strong>{{ formatFen(data.open_items.receivable_fen) }}</strong>
            <small>{{ data.open_items.receivable_count }} 项</small>
          </button>
          <button class="kpi payable selectable-card" type="button" @click="focusSection('open-items')">
            <span class="kpi-label">{{ isClosed ? "关账时待付" : "月末待付" }}</span>
            <strong>{{ formatFen(data.open_items.payable_fen) }}</strong>
            <small>{{ data.open_items.payable_count }} 项</small>
          </button>
        </section>
      </section>
      <section v-if="priorities.length || hasOverviewNotes" :class="['cockpit', { 'has-actions': priorities.length, 'has-notes': hasOverviewNotes, 'has-details': priorities.length || hasOverviewNotes }]" aria-label="经营说明与提示">
        <div v-if="hasOverviewNotes" class="cockpit-copy">
          <div v-if="hasOverviewNotes" class="takeaway">
            <template v-if="takeaway">
              <span>经营说明</span>
              <p>{{ takeaway }}</p>
              <small v-if="data.management_commentary_details?.current">{{ commentaryValidityLabel(data.management_commentary_details.current) }}</small>
            </template>
            <details v-if="data.management_commentary_details?.status === 'stale' && data.management_commentary_details.latest">
              <summary>账务已更新，查看之前的经营说明</summary>
              <p>{{ data.management_commentary_details.latest.text }}</p>
              <small>{{ commentaryValidityLabel(data.management_commentary_details.latest) }}<template v-if="commentaryValidityReason(data.management_commentary_details.latest)"> · {{ commentaryValidityReason(data.management_commentary_details.latest) }}</template></small>
            </details>
            <details v-if="data.management_commentary_details?.supplements.length">
              <summary>关账后的补充说明</summary>
              <div v-for="note in data.management_commentary_details.supplements" :key="note.id"><p>{{ note.text }}</p><small>{{ commentaryValidityLabel(note) }}<template v-if="commentaryValidityReason(note)"> · {{ commentaryValidityReason(note) }}</template></small></div>
            </details>
          </div>
        </div>

        <aside v-if="priorities.length" class="action-queue selectable-card" aria-label="需要处理" tabindex="-1">
          <header>
            <span class="queue-title">需要处理</span>
            <span class="queue-count">{{ priorities.length }} 项提示</span>
          </header>
          <div class="priority-list">
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
        </aside>
      </section>


      <p v-if="data.position.complete === false || data.open_items.complete === false || data.open_items.unestablished_count" class="needs-check" role="status">部分来源尚待核对，已知金额也不能视为完整结论。<button type="button" @click="focusSection('validation')">查看依据与问题</button></p>

      <div id="finance" class="brief-content-section section-anchor" tabindex="-1">
        <BriefFinancialOverview
          :funds="data.funds_overview"
          :position="data.position"
          :unmatched="data.unmatched_bank_activity"
        />
      </div>

      <div id="activity" class="brief-content-section section-anchor" tabindex="-1">
        <details v-if="data.adopted_basis?.scope === 'current_voucher_page'" class="trust-proof voucher-page-basis">
          <summary>当前凭证页采用依据 · 政策 {{ data.adopted_basis.policies.length }} 项 · 工资确认 {{ data.adopted_basis.payroll_confirmations.length }} 项 · 原始凭据 {{ data.adopted_basis.evidence.length }} 项</summary>
          <p>以下依据只覆盖当前已加载的凭证页，不代表全月全部凭证的采用依据。</p>
          <ul>
            <li v-for="source in data.adopted_basis.policies" :key="`brief-policy-${source.reference.id}`"><strong>{{ source.label }}</strong> · {{ source.version || '未单列版本号' }} · {{ source.effective_from || '生效日起点未单列' }}<template v-if="source.effective_to"> 至 {{ source.effective_to }}</template><template v-if="source.official_urls.length"> · <a :href="source.official_urls[0]" target="_blank" rel="noreferrer">官方来源</a></template></li>
            <li v-for="source in data.adopted_basis.payroll_confirmations" :key="`brief-payroll-${source.calculation_reference.id}`"><strong>{{ source.label }}</strong> · {{ source.confirmation_references.length }} 项确认事实</li>
            <li v-for="source in data.adopted_basis.evidence" :key="`brief-evidence-${source.id}`"><strong>{{ source.name || '原始凭据' }}</strong><template v-if="source.media_type"> · {{ source.media_type }}</template></li>
          </ul>
          <details><summary>内部校验信息</summary><pre>{{ JSON.stringify({ calculation_ids: data.adopted_basis.calculation_ids, policies: data.adopted_basis.policies.map(item => item.reference), payroll_confirmations: data.adopted_basis.payroll_confirmations.map(item => ({ calculation_reference: item.calculation_reference, confirmation_references: item.confirmation_references })), evidence: data.adopted_basis.evidence.map(item => ({ source_type: item.source_type, id: item.id, revision: item.revision, digest: item.digest })) }, null, 2) }}</pre></details>
        </details>
        <BriefActivityWorkbench
          :groups="data.activity_groups"
          :vouchers="vouchers"
          :voucher-count="data.voucher_count"
          :focused-voucher="data.focused_voucher"
        >
          <template #pagination>
            <DashboardPagination compact item-label="张凭证" :page="data.collections.vouchers?.page" :loaded="vouchers.length" :loading="sectionLoading.vouchers" :error="sectionErrors.vouchers" @retry="loadMore()" @more="loadMore()" />
          </template>
        </BriefActivityWorkbench>
      </div>

      <div v-if="data.workforce_cost.has_activity" id="workforce" class="brief-content-section section-anchor" tabindex="-1">
        <BriefWorkforceSection
          :workforce="data.workforce_cost"
          :period-label="response?.selected_period?.short_label || ''"
        />
      </div>

      <div id="open-items" class="brief-content-section section-anchor" tabindex="-1">
        <BriefOpenItems
          :open-items="data.open_items"
          :items="openItemRecords"
          :period-label="response?.selected_period?.short_label || ''"
          :period-status="response?.selected_period?.status || ''"
          :period="selectedPeriod"
          :snapshot-version="response?.snapshot_version"
          :focus-request="openItemsFocusRequest"
          @changed="refresh"
        />
        <DashboardPagination compact item-label="项往来" :page="data.collections.open_items?.page" :loaded="openItemRecords.length" :loading="sectionLoading.open_items" :error="sectionErrors.open_items" @retry="loadMore('open_items')" @more="loadMore('open_items')" />
      </div>

      <section id="validation" class="monthly-review brief-content-section section-anchor" tabindex="-1" aria-labelledby="monthly-review-title">
        <header class="monthly-review-heading">
          <div>
            <p>月度收尾</p>
            <h2 id="monthly-review-title">{{ response?.selected_period?.short_label }} · 账务与待办</h2>
            <span>先确认账上的数字是否可靠，再看现在还有什么需要处理。</span>
          </div>
          <span class="monthly-review-scope">{{ isClosed ? "当月结果已封存" : "当月仍可继续完善" }}</span>
        </header>

        <div class="monthly-review-grid">
          <CloseReviewPanel
            v-if="typeof route.query.company_id === 'string' && selectedPeriod"
            :company-id="route.query.company_id"
            :period="selectedPeriod"
            :refresh-key="closeReviewRefreshKey"
          />
          <article
            id="validation-checks"
            :class="['trust-footer', 'section-anchor', data.validation.state]"
            tabindex="-1"
          >
            <div class="trust-heading">
              <div>
                <p>账务可靠性</p>
                <h3>这些数字可以放心看吗？</h3>
                <span v-if="preparationStatus !== 'ready'">主数据已显示，完整检查仍在进行。</span>
                <span v-else-if="data.validation.state === 'complete'">凭证、资料、核算与期间条件均已通过。</span>
                <span v-else>{{ data.validation.summary }}，请查看下面标出的项目。</span>
              </div>
              <span :class="['trust-state', data.validation.state]">
                {{ preparationStatus !== 'ready' ? '检查中' : data.validation.state === 'complete' ? "已通过" : "需核对" }}
              </span>
            </div>
            <details :open="data.validation.items.some(item => item.state !== 'pass')"><summary>{{ preparationStatus === 'ready' && data.validation.items.length > 0 && data.validation.items.every(item => item.state === 'pass') ? `${data.validation.items.length} 项关键检查均已通过` : '查看需要核对的检查' }}</summary><div class="checks">
              <article v-for="item in data.validation.items" :key="item.key" :class="item.state">
                <span class="check-mark">
                  {{ item.state === "pass" ? "✓" : item.state === "error" ? "×" : item.state === "pending" ? "!" : "–" }}
                </span>
                <div>
                  <strong>{{ ownerCheckLabel(item) }}</strong>
                  <small>{{ ownerCheckText(item) }}</small>
                </div>
              </article>
            </div></details>
            <details v-if="data.position.issues.length" class="trust-proof" open>
              <summary>财务位置有 {{ data.position.issues.length }} 条来源需要核对</summary>
              <ul><li v-for="(issue, index) in data.position.issues" :key="index">{{ issue.message }}<template v-if="issue.amount_fen != null"> · {{ formatFen(issue.amount_fen) }}</template><details><summary>查看精确来源</summary><pre>{{ JSON.stringify(issue, null, 2) }}</pre></details></li></ul>
            </details>
            <details v-if="data.validation.issues?.length" class="trust-proof" open>
              <summary>{{ isClosed ? '当前仍需完善的核算依据' : '关账前需要处理的核算事项' }}</summary>
              <ul>
                <li v-for="(issue, index) in data.validation.issues" :key="index">{{ issue.message }}</li>
              </ul>
            </details>
            <details
              v-if="data.material_completeness && !data.material_completeness.closed"
              class="trust-proof"
              :open="!data.material_completeness.satisfied"
            >
              <summary>本月资料：{{ data.material_completeness.satisfied ? "已逐项核对" : "还有待处理项目" }}</summary>
              <ul v-if="data.material_completeness.issues.length">
                <li v-for="(issue, index) in data.material_completeness.issues" :key="index">
                  {{ issue.message }}
                  <span v-if="issue.actual_fen != null"> · 实际 {{ formatFen(issue.actual_fen) }}</span>
                  <span v-if="issue.expected_fen != null"> · 应为 {{ formatFen(issue.expected_fen) }}</span>
                  <details><summary>查看核对位置</summary><pre>{{ JSON.stringify(issue, null, 2) }}</pre></details>
                </li>
              </ul>
            </details>
            <details class="trust-proof">
              <summary>查看检查依据</summary>
              <dl>
                <div><dt>正式凭证 / 分录</dt><dd>{{ data.voucher_count }} 张 / {{ data.line_count }} 行</dd></div>
                <div><dt>借方合计 / 贷方合计</dt><dd>{{ formatFen(data.total_debit_fen) }} / {{ formatFen(data.total_credit_fen) }}</dd></div>
                <div><dt>银行当前有效匹配</dt><dd>{{ data.cash.matched_count }} / {{ data.cash.transaction_count }}</dd></div>
                <div><dt>期间状态</dt><dd>{{ statusLabel(response?.selected_period?.status || "") }}</dd></div>
                <div><dt>页面数据生成时间</dt><dd>{{ generatedText() }}</dd></div>
              </dl>
            </details>
          </article>

          <PeriodPreparation
            v-if="preparationStatus === 'ready' && data.period_preparation"
            :preparation="data.period_preparation"
            :snapshot-version="response?.snapshot_version"
            owner-navigation
            @changed="refresh"
            @focus-settlements="focusOutstandingItems"
          />
          <section v-else class="state-panel preparation-state" :role="preparationStatus === 'error' || preparationStatus === 'stale' ? 'alert' : 'status'">
            <strong>{{ preparationStatus === 'stale' ? '待办检查已过期' : preparationStatus === 'error' ? '待办检查读取失败' : '正在检查后续待办' }}</strong>
            <p>{{ preparationError || '账务主数据已显示，后续事项正在读取。' }}</p>
            <button v-if="preparationStatus === 'error'" type="button" @click="loadPreparation">重新检查</button>
            <button v-if="preparationStatus === 'stale'" type="button" @click="refresh">刷新简报</button>
          </section>
        </div>
      </section>

    </template>
  </section>
</template>

<style scoped>
.update-notice { margin: 0 0 16px; color: var(--muted); font-size: 13px; }
.needs-check { padding: 12px; border-radius: 10px; color: var(--warning); background: var(--warning-soft); }
.needs-check button { margin-left: 12px; cursor: pointer; background: transparent; border: 0; text-decoration: underline; }
.monthly-review {
  margin-top: 24px;
}

.monthly-review-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 14px;
  padding: 0 2px;
}

.monthly-review-heading p {
  margin: 0 0 3px;
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

.monthly-review-heading h2 {
  margin: 0;
  font-size: 22px;
}

.monthly-review-heading div > span {
  display: block;
  margin-top: 3px;
  color: var(--brief-muted);
  font-size: 13px;
}

.monthly-review-scope {
  flex: 0 0 auto;
  padding: 5px 10px;
  border-radius: 999px;
  background: var(--brief-green-soft);
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 750;
}

.monthly-review-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: stretch;
  gap: 12px;
}

.monthly-review-grid > * {
  min-width: 0;
}

.monthly-review-grid :deep(.period-preparation) {
  height: auto;
  margin: 0;
}

.preparation-state {
  min-height: 220px;
}

.brief-page {
  --brief-page: var(--background);
  --brief-anchor-offset: 78px;
  --brief-surface: var(--surface);
  --brief-soft: color-mix(in srgb, var(--text) 3%, var(--surface));
  --brief-metric-surface: var(--brief-soft);
  --brief-text: var(--text);
  --brief-muted: var(--muted);
  --brief-line: color-mix(in srgb, var(--text) 12%, var(--surface));
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
  --brief-panel-radius: var(--radius-panel, 14px);
  --brief-control-radius: var(--radius-control, 9px);
  --brief-shadow: none;
  --brief-overlay-shadow: var(--shadow-overlay);
  width: min(calc(100% - 48px), 1320px);
  margin: 0 auto;
  padding: 26px 0 56px;
  color: var(--brief-text);
}

.brief-page :deep(.selectable-card) {
  outline: none;
  transition: border-color 150ms ease;
}

.brief-page :deep(.selectable-card:focus),
.brief-page :deep(.selectable-card:focus-within) {
  border-color: color-mix(in srgb, var(--brief-green) 48%, var(--brief-line));
}

.state-panel {
  padding: 28px;
  border: 1px solid var(--brief-line);
  border-radius: var(--brief-panel-radius);
  background: var(--brief-surface);
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

.brief-page :deep(.section-anchor),
.brief-page :deep(.voucher-card) {
  scroll-margin-top: var(--brief-anchor-offset);
}







.brief-hero {
  display: grid;
  gap: 24px 40px;
  min-height: 198px;
  padding: 25px 28px;
  border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line));
  border-radius: 20px;
  background:
    radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%),
    linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface)));
}

.brief-hero-topline {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px 28px;
  margin-bottom: -12px;
}

.brief-hero-topline > .dashboard-hero-eyebrow {
  flex: none;
  margin: 0;
}

.hero-metric { min-width: 0; }
.hero-metric h2 {
  margin: 0;
  color: var(--brief-muted);
  font-size: 12px;
  font-weight: 750;
}
.hero-metric strong {
  color: var(--brief-green);
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.hero-metric strong.loss { color: var(--brief-red); }
.hero-metric p {
  color: var(--brief-muted);
  line-height: 1.6;
}
.brief-hero .status-rail { justify-content: flex-end; }

.cockpit {
  margin-top: 12px;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 28px;
  padding: 0 2px;
}

.cockpit.has-details {
  padding: 20px 24px;
  border: 1px solid var(--brief-line);
  border-radius: var(--brief-panel-radius);
  background: var(--brief-surface);
}

.cockpit.has-actions.has-notes {
  grid-template-columns: minmax(0, 1.5fr) minmax(300px, 0.85fr);
}

.cockpit-copy {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(0, 1fr);
  align-items: center;
  gap: 18px;
}

.cockpit.has-actions .cockpit-copy {
  grid-template-columns: minmax(0, 1fr);
  align-content: center;
  gap: 14px;
}

.status-rail {
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px 18px;
}

.cockpit.has-details .status-rail {
  justify-content: flex-start;
}

.status-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--brief-muted);
  font-size: 12px;
}

.status-chip::before {
  width: 5px;
  height: 5px;
  flex: none;
  border-radius: 50%;
  background: var(--brief-green);
  content: "";
}

.status-chip.attention { color: var(--brief-amber); }
.status-chip.attention::before { background: var(--brief-amber); }
.status-chip.error { color: var(--brief-red); }
.status-chip.error::before { background: var(--brief-red); }

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

.trust-state.attention {
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.trust-state.error {
  background: var(--brief-red-soft);
  color: var(--brief-red);
}

.takeaway {
  display: grid;
  grid-column: 1 / -1;
  gap: 6px;
  max-width: 80ch;
}

.takeaway > span {
  color: var(--brief-muted);
  font-size: 11px;
}

.takeaway > p {
  margin: 0;
  color: var(--brief-text);
  font-size: 15px;
  line-height: 1.75;
  white-space: pre-line;
  overflow-wrap: anywhere;
}

.takeaway details { font-size: 13px; }
.takeaway summary { cursor: pointer; color: var(--muted); }

.action-queue {
  min-width: 0;
  align-self: start;
  padding: 14px 16px;
  border-radius: 9px;
  background: color-mix(in srgb, var(--brief-amber-soft) 38%, var(--brief-surface));
}

.action-queue header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 13px;
}

.queue-title {
  color: var(--brief-text);
  font-size: 12px;
  font-weight: 650;
}

.queue-count,
.priority-list small {
  color: var(--brief-muted);
  font-size: 11px;
}

.cockpit:not(.has-notes) .priority-list {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 24px;
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
  padding: 9px 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
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
.priority-list .priority-action > div {
  display: grid;
  gap: 4px;
}

.priority-list strong {
  font-size: 12px;
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 20px 28px;
  margin-top: 8px;
  overflow: visible;
  border: 0;
  border-radius: 0;
  background: transparent;
}

.kpi {
  --kpi-accent: var(--brief-green);
  --kpi-accent-soft: var(--brief-green-soft);

  display: grid;
  min-width: 0;
  min-height: 0;
  grid-template-rows: auto auto 1fr;
  gap: 6px;
  align-content: start;
  padding: 0 0 0 12px;
  border: 0;
  border-left: 1px solid var(--brief-line);
  border-radius: var(--brief-control-radius);
  background: transparent;
  color: var(--brief-text);
  font: inherit;
  text-align: left;
}

.kpi:first-child {
  border-left: 0;
}

.kpi.funds {
  --kpi-accent: var(--brief-green);
  --kpi-accent-soft: var(--brief-green-soft);
}

.kpi.asset {
  --kpi-accent: var(--brief-gold);
  --kpi-accent-soft: var(--brief-gold-soft);
}

.kpi.receivable {
  --kpi-accent: var(--brief-blue);
  --kpi-accent-soft: var(--brief-blue-soft);
}

.kpi.payable {
  --kpi-accent: var(--brief-amber);
  --kpi-accent-soft: var(--brief-amber-soft);
}

button.kpi {
  position: relative;
  padding-left: 18px;
  cursor: pointer;
  transition: background-color 160ms ease, border-color 160ms ease;
}

button.kpi:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
  /* 让聚焦轮廓的圆角与悬浮底色一致。 */
  border-radius: calc(var(--brief-control-radius) + 2px);
}

/* 左侧分类色标，替代原来的灰底与右上角箭头，表示该模块可以进入。 */
button.kpi::before {
  position: absolute;
  top: 2px;
  bottom: 2px;
  left: 0;
  width: 3px;
  border-radius: 999px;
  background: var(--kpi-accent);
  opacity: 0;
  transform: scaleY(0.4);
  transition: opacity 160ms ease, transform 160ms ease;
  content: "";
}

button.kpi:hover,
button.kpi:focus-visible {
  border-color: color-mix(in srgb, var(--kpi-accent) 18%, transparent);
  background: color-mix(in srgb, var(--kpi-accent-soft) 70%, transparent);
}

button.kpi:hover::before,
button.kpi:focus-visible::before {
  opacity: 0.85;
  transform: scaleY(1);
}

button.kpi:hover > strong {
  text-decoration-color: color-mix(in srgb, var(--kpi-accent) 55%, transparent);
}

.kpi-label {
  color: var(--brief-muted);
  font-size: 12px;
  font-weight: 750;
}

.kpi > strong {
  overflow-wrap: anywhere;
  font-size: clamp(20px, 2vw, 26px);
  line-height: 1.15;
  letter-spacing: -0.025em;
}

.kpi.asset > strong {
  color: var(--brief-gold);
}

.kpi.funds > strong {
  color: var(--brief-green);
}

.kpi.receivable > strong {
  color: var(--brief-blue);
}

.kpi.payable > strong {
  color: var(--brief-amber);
}

.kpi small {
  color: var(--brief-muted);
  font-size: 11px;
  line-height: 1.45;
}

.kpi small b {
  color: var(--brief-green);
}

.brief-content-section {
  margin-top: 40px;
}

.trust-footer {
  margin: 0;
  padding: 19px 20px;
  border: 1px solid var(--brief-line);
  border-radius: var(--brief-panel-radius);
  background: var(--brief-surface);
}


.trust-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.trust-heading p {
  margin: 0 0 3px;
  color: var(--brief-muted);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

.trust-heading h3 {
  margin: 0;
  font-size: 18px;
}

.trust-heading > div > span {
  color: var(--brief-muted);
  font-size: 13px;
}

.trust-footer > details:first-of-type {
  margin-top: 16px;
  padding-top: 12px;
}

.trust-footer > details:first-of-type > summary {
  color: var(--brief-green);
  font-size: 12px;
  font-weight: 750;
  cursor: pointer;
}

.checks {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 7px;
  margin-top: 9px;
}

.checks article {
  display: grid;
  min-width: 0;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--brief-control-radius);
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
  font-size: 12px;
}

.checks small {
  color: var(--brief-muted);
  font-size: 11px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.trust-proof {
  margin-top: 10px;
  padding-top: 8px;
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

@media (max-width: 1199px) {
  .brief-page {
    --brief-anchor-offset: 124px;
  }

.brief-header :deep(.toolbar) {
    grid-row: 1;
    grid-column: 2;
  }

}

@media (max-width: 720px) {
  .brief-page {
    --brief-anchor-offset: 174px;
  }

.brief-header :deep(h1) {
    font-size: 24px;
  }

.brief-header :deep(.toolbar select) {
    min-width: 0;
    width: 0;
    flex: 1;
  }

.brief-header :deep(.period-status) {
    flex: none;
    padding: 3px 7px;
    font-size: 11px;
  }
}

@media (max-width: 1080px) {
  .cockpit.has-actions.has-notes {
    grid-template-columns: minmax(0, 1.3fr) minmax(275px, 0.8fr);
  }

  .cockpit-copy {
    grid-template-columns: minmax(0, 1fr);
    gap: 12px;
  }

  .kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 20px 24px;
  }

  /* 两列时不再用左侧分隔线区分板块。 */
  .kpi {
    padding-left: 14px;
    border-left: 0;
  }

  .checks {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .monthly-review-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .brief-hero { padding: 20px; border-radius: 17px; }

  .brief-hero-topline {
    flex-direction: column;
    gap: 8px;
  }

  .brief-hero .status-rail { justify-content: flex-start; }

  .brief-page {
    width: min(calc(100% - 24px), 1320px);
    padding: 16px 0 24px;
  }

  .cockpit,
  .cockpit.has-actions.has-notes {
    grid-template-columns: 1fr;
    gap: 13px;
  }

  .cockpit.has-details {
    padding: 16px;
  }

  .takeaway {
    grid-template-columns: 1fr;
    gap: 3px;
  }

  .cockpit:not(.has-notes) .priority-list {
    grid-template-columns: minmax(0, 1fr);
  }

  .action-queue {
    padding: 12px 14px;
  }

  .kpi-grid {
    grid-template-columns: 1fr;
    gap: 0;
  }

  .kpi {
    padding-top: 13px;
    padding-bottom: 13px;
  }

  .kpi + .kpi {
    border-top: 1px solid var(--brief-line);
  }

  .monthly-review-heading {
    display: block;
  }

  .monthly-review-scope {
    display: inline-block;
    margin-top: 9px;
  }

  .trust-footer {
    padding: 16px;
  }

  .trust-heading {
    flex-direction: column;
    gap: 8px;
  }

  .trust-proof dl {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .checks {
    grid-template-columns: 1fr;
  }

  .checks small {
    white-space: normal;
    overflow-wrap: anywhere;
  }

  .trust-proof summary {
    display: flex;
    min-height: 44px;
    align-items: center;
  }
}

@media (max-width: 430px) {
  .kpi > strong {
    font-size: 23px;
  }
}

.brief-hero .kpi-grid strong { font-variant-numeric: tabular-nums; }
</style>
