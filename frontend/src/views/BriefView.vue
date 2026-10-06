<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowReactive, shallowRef, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { fetchDeferredBrief, type BriefQuery, type BriefVoucher } from "../api/brief";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import BriefActivityWorkbench from "../components/brief/BriefActivityWorkbench.vue";
import BriefFinancialOverview from "../components/brief/BriefFinancialOverview.vue";
import BriefWorkforceSection from "../components/brief/BriefWorkforceSection.vue";
import BriefOpenItems from "../components/brief/BriefOpenItems.vue";
import CloseReviewPanel from "../components/CloseReviewPanel.vue";
import BusinessStatusDetails from "../components/BusinessStatusDetails.vue";
import { useDashboardSections } from "../composables/useDashboardSections";
import { useDashboardContext } from "../composables/useDashboardContext";
import { fen, formatFen } from "../utils/money";
import { appendDashboardCollection } from "../utils/dashboardCollections";

const route = useRoute(), router = useRouter();
const { context, load: loadContext, refresh: refreshContext } = useDashboardContext();
const response = shallowRef<Awaited<ReturnType<typeof fetchDeferredBrief>> | null>(null);
const voucherPreviewIndex = shallowRef(shallowReactive(new Map<string, BriefVoucher>()));
const focusedVoucherSelection = ref(0);
const loading = ref(false), error = ref(""), updateNotice = ref("");
type BriefSection = NonNullable<BriefQuery["section"]>;
const sectionLoading = ref<Partial<Record<BriefSection, boolean>>>({});
const sectionErrors = ref<Partial<Record<BriefSection, string>>>({});
const pageControllers = new Map<BriefSection, AbortController>();
const showMonthlyReview = ref(false);
let controller: AbortController | null = null, initialized = false, mounted = true, requestGeneration = 0;
const selectedPeriod = computed(() => response.value?.selected_period?.key || "");
const data = computed(() => response.value?.data ?? null);
const reviewRequest = computed(() => !loading.value && data.value?.month_state === "open" ? data.value.owner_review_request : null);
const needsMonthlyReview = computed(() => reviewRequest.value !== null);
const activity = computed(() => data.value?.collections.activity?.items ?? []);
const vouchers = computed(() => data.value?.collections.vouchers?.items ?? []);
const openItems = computed(() => data.value?.collections.open_items?.items ?? []);
const periodOptions = computed(() => context.value?.periods || []);
const briefTitle = computed(() => {
  const month = /\d{4}-(\d{2})/.exec(selectedPeriod.value || queryPeriod() || "")?.[1];
  return month ? `${Number(month)} 月经营简报` : "经营简报";
});
const sectionLinks = computed(() => data.value ? [
  { id: "overview", label: "概览" },
  ...(data.value.financial_position ? [{ id: "financial-overview", label: "资金与资产负债" }] : []),
  { id: "activity", label: "本月发生" },
  ...(data.value.workforce_cost?.has_activity ? [{ id: "workforce", label: "用工成本" }] : []),
  { id: "open-items", label: "待收待付" },
  { id: "owner-tasks", label: "老板待办" },
] : []);
const { activeSection, focusSection, focusSelectedPanel } = useDashboardSections(sectionLinks, "overview");
function queryPeriod() { return typeof route.query.period === "string" ? route.query.period : null; }
function selectionKey() { return JSON.stringify([route.query.company_id, route.query.period, route.query.voucher]); }
function isCurrent(generation: number, selection: string) { return mounted && requestGeneration === generation && selectionKey() === selection; }
function indexVouchers(result: Awaited<ReturnType<typeof fetchDeferredBrief>>) {
  const index = voucherPreviewIndex.value;
  for (const voucher of result.data?.collections.vouchers?.items ?? []) {
    if (!index.has(voucher.voucher_version_id)) index.set(voucher.voucher_version_id, voucher);
  }
  const focused = result.data?.focused_voucher;
  if (focused && !index.has(focused.voucher_version_id)) index.set(focused.voucher_version_id, focused);
}
function invalidateRequests(keepContent = false) {
  requestGeneration += 1; controller?.abort(); controller = null;
  for (const request of pageControllers.values()) request.abort();
  pageControllers.clear(); sectionLoading.value = {}; sectionErrors.value = {};
  voucherPreviewIndex.value = shallowReactive(new Map()); focusedVoucherSelection.value = 0;
  if (!keepContent) response.value = null;
  showMonthlyReview.value = false; loading.value = keepContent;
}
async function loadData(period: string | null, contextGate?: Promise<void>) {
  const generation = ++requestGeneration, selection = selectionKey(), companyId = route.query.company_id;
  controller?.abort();
  for (const request of pageControllers.values()) request.abort();
  pageControllers.clear(); sectionLoading.value = {}; sectionErrors.value = {};
  voucherPreviewIndex.value = shallowReactive(new Map()); focusedVoucherSelection.value = 0;
  const request = new AbortController(); controller = request;
  loading.value = true;
  if (!contextGate) response.value = null;
  showMonthlyReview.value = false; error.value = "";
  try {
    if (typeof companyId !== "string") throw new Error("请先选择公司。");
    const target = typeof route.query.voucher === "string" ? route.query.voucher : undefined;
    const options = target ? /^\d+$/.test(target) ? { voucher_number: Number(target) } : { voucher_version_id: target } : {};
    const mainRequest = fetchDeferredBrief(companyId, period, request.signal, undefined, options);
    const result = contextGate ? (await Promise.all([mainRequest, contextGate]))[0] : await mainRequest;
    if (!isCurrent(generation, selection) || controller !== request) return;
    if (result.data) {
      const { activity, open_items, vouchers } = result.data.collections;
      if (activity) activity.items = shallowReactive(activity.items);
      if (open_items) open_items.items = shallowReactive(open_items.items);
      if (vouchers) vouchers.items = shallowReactive(vouchers.items);
    }
    response.value = result;
    indexVouchers(result);
    if (!result.data) request.abort();
    if (target) focusSection("activity");
  } catch (caught) {
    request.abort();
    if (!isCurrent(generation, selection) || (caught instanceof DOMException && caught.name === "AbortError")) return;
    response.value = null; error.value = dashboardErrorMessage(caught);
  } finally { if (isCurrent(generation, selection) && controller === request) loading.value = false; }
}
async function loadMore(section: BriefSection) {
  const current = response.value, page = current?.data?.collections[section]?.page;
  if (!current?.data || !page?.has_more || !page.next_cursor || sectionLoading.value[section]) return false;
  const generation = requestGeneration, selection = selectionKey(), request = new AbortController();
  pageControllers.set(section, request); sectionLoading.value[section] = true; sectionErrors.value[section] = "";
  const valid = () => isCurrent(generation, selection) && pageControllers.get(section) === request;
  try {
    const next = await fetchDeferredBrief(current.read_context.company_id, selectedPeriod.value, request.signal, current.snapshot_version, { section, cursor: page.next_cursor });
    if (!valid() || !next.data || !response.value?.data) return false;
    const latest = response.value, before = latest.data!;
    if (section === "activity" && next.data.collections.activity) {
      const collection = next.data.collections.activity;
      const previous = before.collections.activity;
      if (!previous) return false;
      response.value = { ...latest, data: { ...before, collections: { ...before.collections, activity: appendDashboardCollection(previous, collection) } } };
    } else if (section === "open_items" && next.data.collections.open_items) {
      const collection = next.data.collections.open_items;
      const previous = before.collections.open_items;
      if (!previous) return false;
      response.value = { ...latest, data: { ...before, collections: { ...before.collections, open_items: appendDashboardCollection(previous, collection) } } };
    } else if (section === "vouchers" && next.data.collections.vouchers) {
      const collection = next.data.collections.vouchers;
      const previous = before.collections.vouchers;
      if (!previous) return false;
      response.value = { ...latest, data: { ...before, collections: { ...before.collections, vouchers: appendDashboardCollection(previous, collection) } } };
    } else {
      return false;
    }
    indexVouchers(next);
    return true;
  } catch (caught) {
    if (!valid()) return false;
    if (isDashboardSnapshotChanged(caught)) { await refreshChanged(); }
    else sectionErrors.value[section] = dashboardErrorMessage(caught);
    return false;
  } finally { if (valid()) { sectionLoading.value[section] = false; pageControllers.delete(section); } }
}
async function loadAllVouchers() {
  const generation = requestGeneration, selection = selectionKey(), cursors = new Set<string>();
  while (isCurrent(generation, selection)) {
    const page = response.value?.data?.collections.vouchers?.page;
    if (!page?.has_more || !page.next_cursor || sectionLoading.value.vouchers) return;
    if (cursors.has(page.next_cursor)) { sectionErrors.value.vouchers = "凭证分页没有推进，请刷新后重新读取。"; return; }
    cursors.add(page.next_cursor);
    if (!await loadMore("vouchers")) return;
  }
}
function paginationScope() { return JSON.stringify([selectionKey(), response.value?.snapshot_version, requestGeneration]); }
function pausePages(section: BriefSection, scope: string) {
  if (scope !== paginationScope()) return;
  pageControllers.get(section)?.abort(); pageControllers.delete(section); sectionLoading.value[section] = false;
}
function openVoucher(voucherVersionId: string) {
  const current = response.value;
  if (!current?.data || !selectedPeriod.value) return;
  const voucher = voucherPreviewIndex.value.get(voucherVersionId);
  if (!voucher) { sectionErrors.value.vouchers = "未找到这项业务的对应凭证，请刷新后重新查看。"; return; }
  sectionErrors.value.vouchers = "";
  response.value = { ...current, data: { ...current.data, focused_voucher: voucher } };
  focusedVoucherSelection.value += 1;
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
    const company = route.query.company_id, period = queryPeriod();
    if (typeof company === "string" && period && context.value?.current_company?.company_id === company && context.value.periods.some(item => item.key === period)) {
      const contextGate = refreshContext().then(fresh => {
        if (fresh.current_company?.company_id !== company || !fresh.periods.some(item => item.key === period)) throw new Error("当前公司或期间已变化，请重新选择。");
      });
      await loadData(period, contextGate);
    } else {
      await refreshContext();
      if (isCurrent(generation, selection)) await loadData(queryPeriod());
    }
  } catch (caught) { if (isCurrent(generation, selection)) { response.value = null; loading.value = false; error.value = dashboardErrorMessage(caught); } }
}
async function initialize() {
  initialized = true;
  const generation = requestGeneration, selection = selectionKey();
  try {
    const loadedContext = await loadContext();
    if (!isCurrent(generation, selection)) return;
    const requested = queryPeriod(), target = requested || loadedContext.default_period;
    if (!requested && target) { await router.replace({ query: { ...route.query, period: target } }); return; }
    await loadData(target);
  } catch (caught) { if (isCurrent(generation, selection)) { error.value = dashboardErrorMessage(caught); loading.value = false; } }
}
function changePeriod(value: string) { void router.push({ query: { company_id: route.query.company_id, period: value }, hash: "" }); }
watch(() => [route.query.company_id, route.query.period, route.query.voucher], (value, previous) => {
  if (value.some((item, index) => item !== previous[index])) invalidateRequests();
}, { flush: "sync" });
watch(() => [context.value?.current_company?.company_id, route.query.period, route.query.voucher], (value, previous) => {
  if (initialized && value[0] && value.some((item, index) => item !== previous[index])) void loadData(queryPeriod());
});
onMounted(() => void initialize());
onBeforeUnmount(() => { mounted = false; invalidateRequests(); });
</script>

<template>
  <section class="brief-page" @click="focusSelectedPanel">
    <DashboardModuleHeader :title="briefTitle" :options="periodOptions" :selected="selectedPeriod" :period-status="data?.month_state" :loading="loading" select-label="查看月份" @change="changePeriod" @refresh="refresh">
      <template #navigation><DashboardSectionNav v-if="data" v-show="!loading" :items="sectionLinks" :active="activeSection" label="经营简报区段" @select="focusSection" /></template>
    </DashboardModuleHeader>
    <div v-if="error" class="state-panel error" role="alert"><h2>经营简报加载失败</h2><p>{{ error }}</p><button type="button" @click="loadData(queryPeriod())">重新加载</button></div>
    <div v-else-if="loading" class="state-panel" role="status">正在读取经营简报…</div>
    <div v-else-if="!data" class="state-panel"><h2>还没有可查看的月份</h2><p>录入公司业务后，即可按月份查看经营情况。</p></div>
    <div v-if="data" v-show="!loading && !error" class="brief-content" :data-month-state="data.month_state" :data-owner-review-required="needsMonthlyReview">
      <p v-if="updateNotice" role="status">{{ updateNotice }}</p>
      <section id="overview" class="section-anchor" tabindex="-1" aria-label="经营概览">
        <div class="brief-hero">
        <p class="scope">{{ response?.selected_period?.label }}期末 · 全公司</p>
        <div class="hero"><span>本月账面盈亏</span><strong :class="{ loss: data.position.month_result_fen !== null && fen(data.position.month_result_fen) < 0n }">{{ formatFen(data.position.month_result_fen) }}</strong><p>收入 {{ formatFen(data.position.month_revenue_fen) }} · 费用 {{ formatFen(data.position.month_expense_fen) }}</p><p v-if="!data.position.complete">仅已确认部分 · AI 会计核对中</p></div>
        <div class="kpi-grid">
          <button class="kpi funds" type="button" @click="router.push({ name: 'funds', query: { company_id: route.query.company_id, period: selectedPeriod } })"><span>月末账面资金</span><strong>{{ formatFen(data.funds_overview.total_fen) }}</strong><small>银行、现金与支付平台</small></button>
          <button class="kpi receivable" type="button" @click="focusSection('open-items')"><span>月末待收</span><strong>{{ formatFen(data.open_items.receivable_fen) }}</strong><small>{{ data.open_items.receivable_count }} 项</small></button>
          <button class="kpi payable" type="button" @click="focusSection('open-items')"><span>月末待付</span><strong>{{ formatFen(data.open_items.payable_fen) }}</strong><small>{{ data.open_items.payable_count }} 项</small></button>
        </div>
        </div>
        <div v-if="data.management_commentary_details.current" class="note"><strong>经营说明</strong><p>{{ data.management_commentary_details.current.text }}</p></div>
        <details v-if="data.management_commentary_details.status === 'stale' && data.management_commentary_details.latest" class="note"><summary>之前的经营说明已过期</summary><p>{{ data.management_commentary_details.latest.text }}</p></details>
        <div v-for="note in data.management_commentary_details.supplements" :key="note.id" class="note"><strong>关账后的补充说明</strong><p>{{ note.text }}</p></div>
        <div v-if="data.risks.length" class="risks" aria-label="风险与核对进展">
          <h3>风险与核对进展</h3><article v-for="risk in data.risks" :key="risk.key"><strong>{{ risk.title }}</strong><p>{{ risk.impact }} · {{ risk.status === 'ai_reviewing' ? 'AI 会计核对中' : '需要关注' }}</p></article>
        </div>
        <BriefFinancialOverview v-if="data.financial_position" id="financial-overview" class="section-anchor selectable-section" :funds="data.funds_overview" :position="data.financial_position" />
      </section>
      <section id="activity" class="section-anchor selectable-section" tabindex="-1">
        <BriefActivityWorkbench :groups="data.activity_groups" :items="activity" :activity-count="data.activity_count" :focused-activity="data.focused_activity" :vouchers="vouchers" :voucher-preview-index="voucherPreviewIndex" :voucher-count="data.voucher_count" :focused-voucher="data.focused_voucher" :focused-voucher-selection="focusedVoucherSelection" :vouchers-loading="sectionLoading.vouchers" :vouchers-error="sectionErrors.vouchers" :vouchers-has-more="data.collections.vouchers?.page.has_more" :period="selectedPeriod" :snapshot-version="response?.snapshot_version" @request-voucher="openVoucher" @more-vouchers="loadMore('vouchers')" @all-vouchers="loadAllVouchers" @changed="refreshChanged">
          <template #pagination><DashboardPagination automatic :active="!loading && activeSection === 'activity'" :scope="paginationScope()" @pause="pausePages('activity', $event)" compact item-label="项业务" :page="data.collections.activity?.page" :loaded="activity.length" :loading="sectionLoading.activity" :error="sectionErrors.activity" @retry="loadMore('activity')" @more="loadMore('activity')" /></template>
        </BriefActivityWorkbench>
      </section>
      <section v-if="data.workforce_cost?.has_activity" id="workforce" class="section-anchor" tabindex="-1">
        <BriefWorkforceSection :workforce="data.workforce_cost" :period-label="response?.selected_period?.short_label || ''" />
      </section>
      <section id="open-items" class="section-anchor selectable-section" tabindex="-1">
        <BriefOpenItems :open-items="data.open_items" :items="openItems" :period-label="response?.selected_period?.short_label || ''" :period-status="response?.selected_period?.status || ''" :period="selectedPeriod" :snapshot-version="response?.snapshot_version" @changed="refreshChanged" />
        <DashboardPagination automatic :active="!loading && activeSection === 'open-items'" :scope="paginationScope()" @pause="pausePages('open_items', $event)" compact item-label="项往来" :page="data.collections.open_items?.page" :loaded="openItems.length" :loading="sectionLoading.open_items" :error="sectionErrors.open_items" @retry="loadMore('open_items')" @more="loadMore('open_items')" />
      </section>
      <section id="owner-tasks" class="section-anchor selectable-section tasks" tabindex="-1" aria-labelledby="tasks-title">
        <h2 id="tasks-title">老板待办</h2>
        <article v-for="task in data.owner_tasks" :key="task.key" data-section-focus tabindex="-1">
          <strong>{{ task.title }} · {{ task.object }}</strong>
          <p>{{ task.period }}<template v-if="task.amount_status === 'known'"> · {{ formatFen(task.amount_fen) }}</template><template v-else-if="task.amount_status === 'unknown'"> · 金额待确认</template><template v-if="task.deadline"> · 截止 {{ task.deadline }}</template></p>
          <p>{{ task.impact }}</p><p class="next-step">{{ task.next_step }}</p>
          <BusinessStatusDetails v-if="task.subject_id" :subject-id="task.subject_id" :period="selectedPeriod" :snapshot-version="response?.snapshot_version" @changed="refreshChanged" />
        </article>
        <article v-if="needsMonthlyReview" class="owner-review-request" data-section-focus tabindex="-1">
          <strong>核对本月业务 · {{ selectedPeriod }}</strong>
          <p>查看本月经营金额和已入账业务，确认后告知 AI 会计。</p>
          <button type="button" :aria-expanded="showMonthlyReview" @click="showMonthlyReview = !showMonthlyReview">{{ showMonthlyReview ? '收起本次核对' : '查看本次核对内容' }}</button>
          <CloseReviewPanel v-if="showMonthlyReview && reviewRequest && typeof route.query.company_id === 'string'" :company-id="route.query.company_id" :period="selectedPeriod" :preview-digest="reviewRequest.preview_digest" />
        </article>
        <p v-if="!data.owner_tasks.length && !needsMonthlyReview" data-section-focus tabindex="-1">目前没有明确需要您处理的事项。</p>
      </section>
    </div>
  </section>
</template>

<style scoped>
.brief-content { display: contents; }
.brief-page {
  --brief-page: var(--background);
  --brief-anchor-offset: 78px;
  --brief-surface: var(--surface);
  --brief-soft: var(--surface-soft);
  --brief-metric-surface: var(--brief-soft);
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
  --brief-panel-radius: var(--radius-panel);
  --brief-control-radius: var(--radius-control);
  --brief-shadow: none;
  --brief-overlay-shadow: var(--shadow-overlay);
  display: grid;
  gap: 32px;
  min-width: 0;
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

.selectable-section {
  outline: none;
}

.section-anchor { min-width: 0; scroll-margin-top: var(--brief-anchor-offset); }
#overview { display: grid; gap: 20px; }
.scope { margin: 0; color: var(--brief-muted); font-size: 11px; }
.brief-hero {
  display: grid;
  gap: 24px;
  min-height: 198px;
  padding: 25px 28px;
  border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line));
  border-radius: 20px;
  background:
    radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%),
    linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface)));
}
.hero { display: grid; min-width: 0; gap: 8px; }
.hero > span { color: var(--brief-muted); font-size: 12px; font-weight: 750; }
.hero > strong { font-size: clamp(31px, 4vw, 42px); color: var(--brief-green); line-height: 1.15; letter-spacing: -0.04em; overflow-wrap: anywhere; }
.hero > strong.loss { color: var(--brief-red); }
.hero p { margin: 0; color: var(--brief-muted); font-size: 12px; line-height: 1.6; }
.kpi-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px 28px; margin-top: 8px; }
.kpi {
  --kpi-accent: var(--brief-green);
  --kpi-accent-soft: var(--brief-green-soft);
  position: relative;
  display: grid;
  min-width: 0;
  grid-template-rows: auto auto 1fr;
  align-content: start;
  gap: 6px;
  padding: 0 0 0 18px;
  border: 0;
  border-left: 1px solid var(--brief-line);
  border-radius: var(--brief-control-radius);
  background: transparent;
  color: var(--brief-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background-color 160ms ease, border-color 160ms ease;
}
.kpi:first-child { border-left: 0; }
.kpi.receivable { --kpi-accent: var(--brief-blue); --kpi-accent-soft: var(--brief-blue-soft); }
.kpi.payable { --kpi-accent: var(--brief-amber); --kpi-accent-soft: var(--brief-amber-soft); }
.kpi::before { position: absolute; top: 2px; bottom: 2px; left: 0; width: 3px; border-radius: 999px; background: var(--kpi-accent); opacity: 0; transform: scaleY(0.4); transition: opacity 160ms ease, transform 160ms ease; content: ""; }
.kpi:hover, .kpi:focus-visible { border-color: color-mix(in srgb, var(--kpi-accent) 18%, transparent); background: color-mix(in srgb, var(--kpi-accent-soft) 70%, transparent); }
.kpi:hover::before, .kpi:focus-visible::before { opacity: 0.85; transform: scaleY(1); }
.kpi:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.kpi > span { color: var(--brief-muted); font-size: 12px; font-weight: 750; }
.kpi > strong { margin: 4px 0; color: var(--kpi-accent); font-size: clamp(20px, 2vw, 26px); line-height: 1.15; letter-spacing: -0.025em; overflow-wrap: anywhere; }
.kpi > small { color: var(--brief-muted); font-size: 11px; line-height: 1.45; }
.note { padding: 20px 24px; border: 1px solid var(--brief-line); border-radius: var(--brief-panel-radius); background: var(--brief-surface); overflow-wrap: anywhere; }
.note > strong { color: var(--brief-muted); font-size: 11px; font-weight: 650; }
.note p { margin: 6px 0 0; font-size: 15px; line-height: 1.75; white-space: pre-line; }
.note summary { color: var(--brief-muted); font-size: 13px; cursor: pointer; }
.risks { padding: 14px 16px; border-radius: var(--brief-control-radius); background: color-mix(in srgb, var(--brief-amber-soft) 38%, var(--brief-surface)); }
.risks h3 { margin: 0 0 9px; font-size: 12px; font-weight: 650; }
.risks article { padding: 9px 0; }
.risks strong { color: var(--brief-amber); font-size: 12px; }
.risks p { margin: 4px 0 0; color: var(--brief-muted); font-size: 11px; line-height: 1.55; }
.tasks h2 { margin: 0 0 16px; font-size: 22px; letter-spacing: -0.03em; }
.tasks > article { display: grid; gap: 6px; margin: 10px 0; padding: 14px 16px; border: 1px solid var(--brief-line); border-radius: var(--brief-control-radius); background: color-mix(in srgb, var(--brief-amber-soft) 38%, var(--brief-surface)); }
.tasks strong { font-size: 12px; }
.tasks p { margin: 0; color: var(--brief-muted); font-size: 12px; line-height: 1.6; }
.tasks > p { padding: 16px; border: 1px solid var(--brief-line); border-radius: var(--brief-control-radius); background: var(--brief-surface); }
.tasks .next-step { color: var(--brief-green); font-weight: 650; }
.tasks button, .state-panel button { justify-self: start; min-height: 40px; padding: 0 13px; border: 1px solid var(--brief-line); border-radius: var(--brief-control-radius); background: var(--brief-surface); color: var(--brief-green); font: inherit; font-size: 12px; font-weight: 750; cursor: pointer; }
.tasks button:hover { background: var(--brief-green-soft); }
article { min-width: 0; overflow-wrap: anywhere; }
.state-panel { padding: 28px; border: 1px solid var(--brief-line); border-radius: var(--brief-panel-radius); background: var(--brief-surface); }
.state-panel h2, .state-panel p { margin-top: 0; }
.state-panel.error { border-color: var(--brief-red); }
@media (max-width: 1199px) { .brief-page { --brief-anchor-offset: 124px; } }
@media (max-width: 760px) {
  .brief-page { width: min(calc(100% - 24px), 1320px); gap: 28px; padding: 16px 0 24px; }
  .brief-hero { padding: 20px; border-radius: 17px; }
  .kpi-grid { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .kpi { padding: 13px 0 13px 14px; border-left: 0; }
  .kpi + .kpi { border-top: 1px solid var(--brief-line); }
  .note { padding: 16px; }
}
@media (max-width: 720px) { .brief-page { --brief-anchor-offset: 174px; } }
@media (prefers-reduced-motion: reduce) { .kpi, .kpi::before { transition: none; } }
</style>
