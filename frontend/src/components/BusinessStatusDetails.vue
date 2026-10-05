<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchBusinessStatus, type BusinessStatusData } from "../api/businessStatus";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import { businessStateLabel } from "../api/dashboardContracts";
import { localBusinessName } from "../api/localKernel";
import DashboardPagination from "./DashboardPagination.vue";
import DashboardBusinessRecords from "./DashboardBusinessRecords.vue";
import { formatFen } from "../utils/money";

interface BriefStatusContext {
  direction: "receivable" | "payable"; party: string; description?: string;
  sourceAmountFen?: string | null; paidFen?: string | null;
  otherSettledFen?: string | null; outstandingFen?: string | null;
  currentStatus?: string | null; currentOutstandingFen?: string | null; selectedPeriodClosed?: boolean;
}
const props = withDefaults(defineProps<{
  subjectId: string; period: string; snapshotVersion?: string | null;
  settlementView?: "historical" | "current"; summaryLabel?: string;
  presentation?: "default" | "brief"; briefContext?: BriefStatusContext;
}>(), { presentation: "default" });
const emit = defineEmits<{ changed: [] }>();
const route = useRoute();
const data = ref<BusinessStatusData | null>(null), error = ref(""), notice = ref("");
const loading = ref(false), moreLoading = ref(false), moreError = ref(""), openedPanel = ref(false), responseVersion = ref("");
let controller: AbortController | null = null, pageController: AbortController | null = null, generation = 0, mounted = true;
const businessName = computed(() => data.value?.display_profiles.business?.values.display_name || (data.value ? localBusinessName(data.value.identity.kind) : props.briefContext?.party || "业务详情"));
const purposes = computed(() => {
  const values = data.value?.display_profiles.business?.values;
  return [...new Set([values?.purpose, values?.note].filter((item): item is string => Boolean(item)))];
});
const objects = computed(() => {
  const profiles = data.value?.display_profiles;
  return [...new Set([...(profiles?.counterparties ?? []), ...(profiles?.employees ?? []), ...(profiles?.assets ?? []), ...(profiles?.fund_accounts ?? [])].map(item => item.values.display_name).filter((item): item is string => Boolean(item)))];
});
const businessAmount = computed(() => data.value?.frozen_adoption || data.value?.current_business_result);
const currentSettlements = computed(() => data.value?.current_followups.settlements);
const showCurrent = computed(() => currentSettlements.value && currentSettlements.value.cutoff_period !== data.value?.settlements.cutoff_period);
const collection = computed(() => data.value?.collections.settlement_events);
function selection() { return JSON.stringify([route.query.company_id, props.subjectId, props.period, props.snapshotVersion, props.settlementView]); }
function invalidate() {
  generation += 1; controller?.abort(); pageController?.abort(); controller = null; pageController = null;
  data.value = null; loading.value = false; moreLoading.value = false; error.value = ""; moreError.value = ""; notice.value = ""; responseVersion.value = "";
}
function changed() { invalidate(); notice.value = "业务资料已变化，请刷新页面后重新查看。"; emit("changed"); }
async function load() {
  if (loading.value) return;
  const version = ++generation, key = selection(), request = new AbortController();
  controller = request; loading.value = true; error.value = "";
  const valid = () => mounted && generation === version && selection() === key && controller === request;
  try {
    const result = await fetchBusinessStatus(props.period, props.subjectId, request.signal, { expected_version: props.snapshotVersion, settlement_view: props.settlementView ?? "current", limit: 20 });
    if (!valid()) return;
    data.value = result.data; responseVersion.value = result.snapshot_version; notice.value = "";
  } catch (caught) { if (valid()) { if (isDashboardSnapshotChanged(caught)) changed(); else if (!(caught instanceof DOMException && caught.name === "AbortError")) error.value = dashboardErrorMessage(caught); } }
  finally { if (valid()) { loading.value = false; controller = null; } }
}
async function loadMore() {
  const page = collection.value?.page;
  if (!page?.has_more || !page.next_cursor || moreLoading.value || !data.value) return;
  const version = generation, key = selection(), request = new AbortController();
  pageController = request; moreLoading.value = true; moreError.value = "";
  const valid = () => mounted && generation === version && selection() === key && pageController === request;
  try {
    const result = await fetchBusinessStatus(props.period, props.subjectId, request.signal, { section: "settlement_events", cursor: page.next_cursor, expected_version: responseVersion.value, settlement_view: props.settlementView ?? "current", limit: 20 });
    if (!valid() || !data.value || !result.data.collections.settlement_events) return;
    const next = result.data.collections.settlement_events, latest = data.value;
    data.value = { ...latest, collections: { settlement_events: { ...next, items: [...latest.collections.settlement_events?.items ?? [], ...next.items] } } };
  } catch (caught) { if (valid()) { if (isDashboardSnapshotChanged(caught)) changed(); else if (!(caught instanceof DOMException && caught.name === "AbortError")) moreError.value = dashboardErrorMessage(caught); } }
  finally { if (valid()) { moreLoading.value = false; pageController = null; } }
}
function toggle(event: Event) { openedPanel.value = (event.target as HTMLDetailsElement).open; if (openedPanel.value && !data.value && !loading.value && !notice.value) void load(); }
function movementLabel(item: NonNullable<BusinessStatusData["collections"]["settlement_events"]>["items"][number]) {
  if (item.direction < 0) return "更正原清偿";
  return ({ paid: "实际收付款", accepted: "已承接代付款", offset: "抵销清偿", refunded: "退款" } as Record<string, string>)[item.mode] || "清偿记录";
}
watch(selection, invalidate, { flush: "sync" });
onBeforeUnmount(() => { mounted = false; invalidate(); });
</script>

<template>
  <details class="business-status-details" :class="{ 'compact-status-details': presentation === 'brief' }" @toggle="toggle">
    <summary class="business-detail-trigger" :class="{ 'compact-status-trigger': presentation === 'brief' }"><span>{{ summaryLabel || '业务详情' }}</span><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg></summary>
    <p v-if="notice" class="business-detail-state" :class="{ 'compact-status-panel': presentation === 'brief' }" role="status">{{ notice }}</p>
    <p v-if="loading" class="business-detail-state" :class="{ 'compact-status-panel': presentation === 'brief' }" role="status">正在读取业务详情…</p>
    <p v-else-if="error" class="business-detail-state error" :class="{ 'compact-status-panel': presentation === 'brief' }" role="alert">{{ error }} <button type="button" @click="load">重新读取</button></p>
    <section v-else-if="data" class="business-detail-panel" :class="{ 'compact-status-panel': presentation === 'brief' }">
      <header><h3>{{ businessName }}</h3><span class="business-state" :class="{ attention: data.settlements.checking, withdrawn: data.latest_source.deleted }">{{ data.latest_source.deleted ? '业务已撤回' : data.settlements.checking ? 'AI 会计核对中' : businessStateLabel(data.review.status) }}</span></header>
      <p v-if="objects.length">业务对象：{{ objects.join('、') }}</p>
      <p v-for="purpose in purposes" :key="purpose">{{ purpose }}</p>
      <dl class="business-amounts">
        <div v-if="data.frozen_adoption"><dt>关账月份</dt><dd>{{ data.frozen_adoption.close_period }}</dd></div>
        <div v-else><dt>业务所属月</dt><dd>{{ data.latest_source.period }}</dd></div>
        <div v-if="businessAmount"><dt>{{ data.frozen_adoption ? '关账时' : '当前' }}{{ businessAmount.amount_label }}</dt><dd>{{ formatFen(businessAmount.amount_fen) }}</dd></div>
        <div v-if="!data.frozen_adoption && data.current_business_result"><dt>该金额入账月</dt><dd>{{ data.current_business_result.posting_period }}</dd></div>
      </dl>
      <p v-if="!data.frozen_adoption && data.current_business_result">以上是当前业务结果；本月更正或冲回的金额见对应记录。</p>
      <h4>截至 {{ data.settlements.cutoff_period }} 的收付进展</h4>
      <p v-if="data.settlements.checking" class="checking">AI 会计核对中，已知金额暂不能代表完整结果。</p>
      <DashboardBusinessRecords :items="data.settlements.obligations" :period="period" :show-business="false" />
      <template v-if="showCurrent && currentSettlements">
        <h4>截至 {{ currentSettlements.cutoff_period }} 的后续进展</h4>
        <p v-if="currentSettlements.checking" class="checking">AI 会计核对中，当前收付结果尚不能完整确认。</p>
        <DashboardBusinessRecords :items="currentSettlements.obligations" :period="period" :show-business="false" />
      </template>
      <template v-if="collection?.page.total_count">
        <h4>实际清偿记录</h4>
        <ul><li v-for="item in collection.items" :key="item.id"><span>{{ item.posting_period }} · {{ movementLabel(item) }}</span><strong>{{ item.relation_state === 'unresolved' ? 'AI 会计核对中' : formatFen(item.signed_amount_fen) }}</strong></li></ul>
        <DashboardPagination :page="collection.page" :loaded="collection.items.length" :loading="moreLoading" :error="moreError" @more="loadMore" @retry="loadMore" />
      </template>
    </section>
  </details>
</template>

<style scoped>
.business-status-details { min-width: 0; overflow-wrap: anywhere; }
.business-detail-trigger {
  display: inline-flex;
  min-height: 32px;
  align-items: center;
  justify-content: center;
  gap: 3px;
  padding: 5px 6px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--accent);
  font-size: 11px;
  font-weight: 750;
  line-height: 1.45;
  list-style: none;
  cursor: pointer;
}
.business-detail-trigger::-webkit-details-marker { display: none; }
.business-detail-trigger:hover { background: var(--accent-soft); }
.business-detail-trigger:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.business-detail-trigger svg { width: 10px; height: 13px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; transition: transform 150ms ease; }
.business-status-details[open] > .business-detail-trigger svg { transform: rotate(90deg); }
.business-status-details[open] > .business-detail-trigger { background: var(--accent-soft); }
.business-detail-panel, .business-detail-state {
  min-width: 0;
  margin-top: 8px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
  background: var(--surface-soft);
}
.business-detail-panel { display: grid; gap: 12px; }
.business-detail-state.error { color: var(--danger); }
header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
h3, h4, p { min-width: 0; margin: 0; }
h3 { font-size: 15px; }
h4 { font-size: 12px; }
p { color: var(--muted); font-size: 12px; line-height: 1.7; }
.business-state { flex: none; max-width: 100%; padding: 4px 9px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 11px; font-weight: 750; }
.business-state.attention { background: var(--warning-soft); color: var(--warning); }
.business-state.withdrawn { background: var(--surface); color: var(--muted); }
.checking { padding: 9px; border-left: 3px solid var(--warning); background: var(--warning-soft); }
dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; margin: 0; }
dl > div { min-width: 0; padding: 4px 12px; border-left: 1px solid var(--line); }
dl > div:first-child { padding-left: 0; border-left: 0; }
dt { color: var(--muted); font-size: 11px; }
dd { margin: 3px 0 0; font-size: 15px; font-weight: 750; overflow-wrap: anywhere; }
ul { margin: 0; padding: 0; list-style: none; }
li { display: flex; gap: 12px; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 12px; }
li span { color: var(--muted); }
button { border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px; background: var(--surface); color: var(--text); font: inherit; font-size: 12px; cursor: pointer; }
.compact-status-details { display: contents; }
.compact-status-trigger { white-space: nowrap; }
.compact-status-details::details-content { display: contents; }
.compact-status-details:not([open]) > :not(summary) { display: none; }
.compact-status-panel { grid-column: 1 / -1; }
@media (max-width: 720px) {
  header, li { flex-direction: column; align-items: flex-start; gap: 5px; }
  .business-detail-trigger { min-height: 44px; }
  .business-detail-panel, .business-detail-state { padding: 12px; }
  dl { grid-template-columns: minmax(0, 1fr); }
  dl > div, dl > div:first-child { padding: 8px 0; border-top: 1px solid var(--line); border-left: 0; }
  dl > div:first-child { padding-top: 0; border-top: 0; }
}
@media (prefers-reduced-motion: reduce) { .business-detail-trigger svg { transition: none; } }
</style>
