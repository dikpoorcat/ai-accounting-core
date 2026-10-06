<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchBusinessStatus, type BusinessStatusData } from "../api/businessStatus";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import { businessStateLabel } from "../api/dashboardContracts";
import { localBusinessName } from "../api/localKernel";
import DashboardPagination from "./DashboardPagination.vue";
import DashboardBusinessRecords from "./DashboardBusinessRecords.vue";
import { fen, formatFen } from "../utils/money";
import { appendDashboardCollection } from "../utils/dashboardCollections";
import type { BriefActivityRow } from "../api/brief";
import type { FundMovement } from "../api/funds";

interface BriefStatusContext {
  obligationKey: string; categoryKey: string;
  cutoffPeriod: string; currentCutoffPeriod: string; status: string;
  direction: "receivable" | "payable"; party: string; description?: string;
  sourceAmountFen?: string | null; paidFen?: string | null;
  otherSettledFen?: string | null; outstandingFen?: string | null;
  currentStatus?: string | null; currentOutstandingFen?: string | null; selectedPeriodClosed?: boolean;
}
const props = withDefaults(defineProps<{
  subjectId: string; period: string; snapshotVersion?: string | null;
  settlementView?: "historical" | "current"; summaryLabel?: string;
  presentation?: "default" | "brief" | "funds"; briefContext?: BriefStatusContext;
  activityContext?: BriefActivityRow;
  fundsContext?: FundMovement;
  expanded?: boolean; hideSummary?: boolean;
}>(), { presentation: "default", expanded: undefined });
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
const panelOpen = computed(() => props.expanded ?? openedPanel.value);
const isOpenItem = computed(() => props.presentation === "brief" && Boolean(props.briefContext));
const isActivity = computed(() => props.presentation === "brief" && Boolean(props.activityContext));
const isFunds = computed(() => props.presentation === "funds" && Boolean(props.fundsContext));
const isOwnerDetail = computed(() => isOpenItem.value || isActivity.value || isFunds.value);
const fundsDescription = computed(() => {
  const item = props.fundsContext;
  return item?.display_summary && ![item.list_summary || item.type, item.party].includes(item.display_summary) ? item.display_summary : "";
});
const activityDescription = computed(() => {
  const item = props.activityContext;
  return item?.description && ![item.party, item.title].includes(item.description) ? item.description : "";
});
const ownerPurposes = computed(() => {
  const context = props.activityContext || props.briefContext;
  const rowTexts = isFunds.value
    ? [props.fundsContext?.party, props.fundsContext?.list_summary || props.fundsContext?.type, fundsDescription.value]
    : [context?.party, isActivity.value ? props.activityContext?.title : context?.description, activityDescription.value];
  return purposes.value.filter(value => !rowTexts.includes(value));
});
type Obligation = BusinessStatusData["settlements"]["obligations"][number];
const activityObjects = computed(() => {
  const profiles = data.value?.display_profiles;
  return [
    { label: "员工", profiles: profiles?.employees },
    { label: "往来方", profiles: profiles?.counterparties },
    { label: "相关资产", profiles: profiles?.assets },
    { label: "相关账户", profiles: profiles?.fund_accounts },
  ].map(group => {
    const members = (group.profiles ?? []).filter(item => !isFunds.value || group.label !== "相关账户" || item.entity_id !== props.fundsContext?.account_id);
    const identities = new Set<string>();
    const names = members.filter(item => {
      if (!item.entity_id) return true;
      if (identities.has(item.entity_id)) return false;
      identities.add(item.entity_id); return true;
    }).flatMap(item => item.values.display_name ? [item.values.display_name] : []);
    return { label: group.label, names };
  })
    .filter(group => group.names.length)
    .map((group, _, groups) => ({ ...group, names: group.names.filter(name => name !== (isFunds.value ? props.fundsContext?.party : props.activityContext?.party) || groups.reduce((count, other) => count + other.names.filter(value => value === name).length, 0) > 1) }))
    .filter(group => group.names.length);
});
const activityFollowups = computed(() => {
  if (!data.value || !showCurrent.value || !currentSettlements.value) return [];
  const historical = new Map(data.value.settlements.obligations.map(item => [item.key, item]));
  const latest = new Map(currentSettlements.value.obligations.map(item => [item.key, item]));
  return [...new Set([...historical.keys(), ...latest.keys()])].flatMap(key => {
    const before = historical.get(key), after = latest.get(key);
    return !before || !after || (["source_amount_fen", "paid_fen", "other_settled_fen", "remaining_fen", "settlement_status", "direction", "category_key"] as const).some(field => before[field] !== after[field])
      ? [{ key, before, after, name: (after || before)?.name || "" }] : [];
  });
});
function activityLabels(item: Obligation) {
  if (item.category_key === "supplier_advances") return { original: "预付金额", paid: "已退回", other: "已冲抵", remaining: "尚未冲抵" };
  if (item.direction === "receivable") return { original: "原应收", paid: "已收", other: "抵销、代付等已处理", remaining: "还需收回" };
  if (item.direction === "payable") return { original: "原应付", paid: "已付", other: "抵销、代付等已处理", remaining: "还需支付" };
  return { original: "款项总额", paid: "实际收付", other: "其他已处理", remaining: "未结金额" };
}
function activityState(item: Obligation, checking: boolean) {
  if (item.settlement_status === "withdrawn") return "业务已撤回";
  if (item.settlement_status === "reversed") return "原业务已更正";
  if (checking || item.direction === "unknown" || item.category_key === "unknown" || item.remaining_fen === null) return "AI 会计核对中";
  if (fen(item.remaining_fen) < 0n || item.settlement_status === "over_settled") return "存在超额结算";
  if (item.settlement_status === "settled" && fen(item.remaining_fen) === 0n) return item.category_key === "supplier_advances" ? "已处理完毕" : "已结清";
  if (item.settlement_status === "partial") return item.category_key === "supplier_advances" ? "部分冲抵或退回" : item.direction === "receivable" ? "部分收回" : "部分支付";
  if (item.settlement_status === "open") return item.category_key === "supplier_advances" ? "待冲抵" : item.direction === "receivable" ? "待收回" : "待支付";
  return "AI 会计核对中";
}
const isAdvance = computed(() => props.briefContext?.categoryKey === "supplier_advances");
const selectedObligation = computed(() => data.value?.settlements.obligations.find(item => item.key === props.briefContext?.obligationKey));
const currentObligation = computed(() => currentSettlements.value?.obligations.find(item => item.key === props.briefContext?.obligationKey));
const selectedProgress = computed(() => {
  const item = selectedObligation.value, context = props.briefContext;
  return {
    amount: item ? item.source_amount_fen : context?.sourceAmountFen,
    paid: item ? item.paid_fen : context?.paidFen,
    other: item ? item.other_settled_fen : context?.otherSettledFen,
    remaining: item ? item.remaining_fen : context?.outstandingFen,
    status: item ? item.settlement_status : context?.status,
    cutoff: data.value?.settlements.cutoff_period || context?.cutoffPeriod || props.period,
  };
});
const latestProgress = computed(() => {
  const item = currentObligation.value, context = props.briefContext;
  return {
    remaining: item ? item.remaining_fen : context?.currentOutstandingFen,
    status: item ? item.settlement_status : context?.currentStatus,
    cutoff: currentSettlements.value?.cutoff_period || context?.currentCutoffPeriod,
  };
});
const showLatestProgress = computed(() => latestProgress.value.cutoff !== selectedProgress.value.cutoff && (
  latestProgress.value.remaining !== selectedProgress.value.remaining
  || latestProgress.value.status !== selectedProgress.value.status
  || (currentObligation.value && selectedObligation.value && (
    currentObligation.value.source_amount_fen !== selectedObligation.value.source_amount_fen
    || currentObligation.value.paid_fen !== selectedObligation.value.paid_fen
    || currentObligation.value.other_settled_fen !== selectedObligation.value.other_settled_fen
  ))
));
const originalLabel = computed(() => isAdvance.value ? "预付金额" : props.briefContext?.direction === "receivable" ? "原应收" : "原应付");
const paidLabel = computed(() => props.briefContext?.direction === "receivable" ? "已收" : "已付");
const remainingLabel = computed(() => isAdvance.value ? "尚未冲抵" : props.briefContext?.direction === "receivable" ? "还需收回" : "还需支付");
const relatedObjects = computed(() => {
  const profiles = data.value?.display_profiles;
  const partyLabel = props.briefContext?.categoryKey === "customer_receivables" ? "客户" : ["supplier_advances", "supplier_payables"].includes(props.briefContext?.categoryKey || "") ? "供应商" : "相关往来方";
  return [
    { label: partyLabel, profiles: profiles?.counterparties },
    { label: "员工", profiles: profiles?.employees },
    { label: "相关资产", profiles: profiles?.assets },
    { label: "相关账户", profiles: profiles?.fund_accounts },
  ].map(group => ({ label: group.label, names: [...new Set((group.profiles ?? []).map(item => item.values.display_name).filter((name): name is string => Boolean(name) && (name !== props.briefContext?.party || group.label !== (props.briefContext?.categoryKey === "payroll_payables" ? "员工" : partyLabel))))] })).filter(group => group.names.length);
});
function ownerMoney(value: string | null | undefined) { return value === null || value === undefined ? "待核对" : formatFen(value); }
function periodText(value: string | undefined) { return value ? value.replace(/^(\d{4})-0?(\d{1,2})$/, "$1年$2月") : "月份待核对"; }
function progressState(status: string | null | undefined, remaining: string | null | undefined, checking: boolean) {
  if (status === "withdrawn") return "业务已撤回";
  if (status === "reversed") return "原业务已更正";
  if (checking || remaining === null || remaining === undefined || !status) return "AI 会计核对中";
  if (status === "settled" && fen(remaining) === 0n) return isAdvance.value ? "已处理完毕" : "已结清";
  if (status === "over_settled" || fen(remaining) < 0n) return "存在超额结算";
  if (status === "partial") return isAdvance.value ? "部分冲抵或退回" : props.briefContext?.direction === "receivable" ? "部分收回" : "部分支付";
  if (status === "open") return isAdvance.value ? "待冲抵" : props.briefContext?.direction === "receivable" ? "待收回" : "待支付";
  return "AI 会计核对中";
}
function progressTone(status: string | null | undefined, remaining: string | null | undefined, checking: boolean) {
  if (status && ["withdrawn", "reversed"].includes(status)) return "withdrawn";
  if (checking || status === "checking" || remaining == null || status === "over_settled" || fen(remaining) < 0n) return "attention";
  if (status === "settled" && fen(remaining) === 0n) return "settled";
  if (status === "open" || status === "partial") return "pending";
  return "withdrawn";
}
function activityTone(item: Obligation, checking: boolean) {
  return progressTone(item.settlement_status, item.remaining_fen, checking || item.direction === "unknown" || item.category_key === "unknown");
}
function movementPurpose(name: string) {
  const kind = data.value?.identity.kind;
  const netLabel = kind === "annual_bonus" ? "实发奖金" : kind === "payroll" || kind === "payroll_bounded" ? "实发工资" : kind === "labor" || kind === "labor_accrual" ? "实发劳务款" : "个人实发款";
  const labels: Record<string, string> = { net: netLabel, tax: "个人所得税", withheld_tax: "代扣个人所得税", primary: "业务款项", employee_social: "个人社保", employee_housing: "个人公积金", employer_social: "公司社保", employer_housing: "公司公积金" };
  return labels[name] || "相关款项";
}
function selection() { return JSON.stringify([route.query.company_id, props.subjectId, props.period, props.snapshotVersion, props.settlementView, props.briefContext?.obligationKey, props.activityContext?.key, props.fundsContext?.id, props.fundsContext?.account_id]); }
function invalidate() {
  generation += 1; controller?.abort(); pageController?.abort(); controller = null; pageController = null;
  data.value = null; loading.value = false; moreLoading.value = false; error.value = ""; moreError.value = ""; notice.value = ""; responseVersion.value = "";
  if (isOpenItem.value) openedPanel.value = false;
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
    const previous = latest.collections.settlement_events;
    if (!previous) return;
    data.value = { ...latest, collections: { settlement_events: appendDashboardCollection(previous, next) } };
  } catch (caught) { if (valid()) { if (isDashboardSnapshotChanged(caught)) changed(); else if (!(caught instanceof DOMException && caught.name === "AbortError")) moreError.value = dashboardErrorMessage(caught); } }
  finally { if (valid()) { moreLoading.value = false; pageController = null; } }
}
function paginationScope() { return `${selection()}:${generation}`; }
function pausePages(scope: string) {
  if (scope !== paginationScope()) return;
  pageController?.abort(); pageController = null; moreLoading.value = false;
}
function loadOnExpansion() { if (panelOpen.value && !data.value && !loading.value && !notice.value) void load(); }
function toggle(event: Event) {
  if (props.expanded !== undefined) return;
  openedPanel.value = (event.target as HTMLDetailsElement).open;
  loadOnExpansion();
}
function movementLabel(item: NonNullable<BusinessStatusData["collections"]["settlement_events"]>["items"][number]) {
  const labels: Record<string, string> = { payment: "实际收付款", accepted: "已确认代付款", offset: "款项抵销", advance: "员工垫付款" };
  const label = labels[item.mode] || "款项处理";
  return item.direction < 0 ? `更正原${label}` : label;
}
watch(selection, invalidate, { flush: "sync" });
watch(() => props.expanded, loadOnExpansion, { immediate: true });
onBeforeUnmount(() => { mounted = false; invalidate(); });
</script>

<template>
  <details class="business-status-details" :class="{ 'compact-status-details': presentation !== 'default' }" :open="panelOpen" @toggle="toggle">
    <summary class="business-detail-trigger" :class="{ 'compact-status-trigger': presentation !== 'default', 'hidden-summary': hideSummary }" :aria-hidden="hideSummary || undefined" :tabindex="hideSummary ? -1 : undefined"><span>{{ summaryLabel || '业务详情' }}</span><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg></summary>
    <p v-if="notice" class="business-detail-state" :class="{ 'compact-status-panel': presentation !== 'default' }" role="status">{{ notice }}</p>
    <p v-if="loading" class="business-detail-state" :class="{ 'compact-status-panel': presentation !== 'default' }" role="status">正在读取业务详情…</p>
    <p v-else-if="error" class="business-detail-state error" :class="{ 'compact-status-panel': presentation !== 'default' }" role="alert">{{ error }} <button type="button" @click="load">重新读取</button></p>
    <section v-else-if="data" class="business-detail-panel" :class="{ 'compact-status-panel': presentation !== 'default' }">
      <template v-if="isActivity || isFunds">
        <p v-if="isFunds && data.latest_source.deleted">这笔业务目前已撤回；原行保留资金变动记录。</p>
        <p v-else-if="!isFunds && data.latest_source.deleted && !activityContext?.state.includes('撤回')">这笔业务目前已撤回；原行保留本次发生记录。</p>
        <p v-if="isFunds && fundsDescription">事项说明：{{ fundsDescription }}</p>
        <p v-if="isActivity && activityDescription">事项说明：{{ activityDescription }}</p>
        <p v-for="purpose in ownerPurposes" :key="purpose">用途／备注：{{ purpose }}</p>
        <p v-for="group in activityObjects" :key="group.label">{{ group.label }}：{{ group.names.join('、') }}</p>
        <template v-if="data.settlements.obligations.length">
          <h4>{{ isFunds ? '款项进度' : '整笔业务的款项进度' }} · 截至{{ periodText(data.settlements.cutoff_period) }}末</h4>
          <p v-if="data.settlements.checking" class="checking">AI 会计核对中，已知金额暂不能代表完整结果。</p>
          <div class="owner-activity-obligations">
            <article v-for="item in data.settlements.obligations" :key="item.key" class="owner-activity-obligation">
              <header><div class="owner-item-heading"><h4>{{ movementPurpose(item.name) }}</h4><p v-if="item.source_period && item.source_period !== period">业务所属月份：{{ periodText(item.source_period) }}</p></div><span class="business-state" :class="activityTone(item, data.settlements.checking)">{{ activityState(item, data.settlements.checking) }}</span></header>
              <div class="owner-item-progress">
                <div class="owner-item-balance"><span>{{ activityLabels(item).remaining }}</span><strong>{{ ownerMoney(item.remaining_fen) }}</strong></div>
                <dl class="owner-item-amounts"><div><dt>{{ activityLabels(item).original }}</dt><dd>{{ ownerMoney(item.source_amount_fen) }}</dd></div><div><dt>{{ activityLabels(item).paid }}</dt><dd>{{ ownerMoney(item.paid_fen) }}</dd></div><div v-if="item.other_settled_fen !== '0' || item.category_key === 'supplier_advances'"><dt>{{ activityLabels(item).other }}</dt><dd>{{ ownerMoney(item.other_settled_fen) }}</dd></div></dl>
              </div>
              <p v-if="item.direction === 'unknown' || item.category_key === 'unknown'" class="checking">这项款项的收付分类待核对。</p>
            </article>
          </div>
        </template>
        <template v-if="activityFollowups.length && currentSettlements">
          <h4>后续进展 · 截至最新月份（{{ periodText(currentSettlements.cutoff_period) }}末）</h4>
          <p v-if="currentSettlements.checking" class="checking">AI 会计核对中，当前收付结果尚不能完整确认。</p>
          <div v-for="change in activityFollowups" :key="change.key" class="owner-item-latest">
            <div><h4>{{ movementPurpose(change.name) }}{{ !change.before ? ' · 后续新增款项' : '' }}</h4><span class="business-state" :class="change.after ? activityTone(change.after, currentSettlements.checking) : 'attention'">{{ change.after ? activityState(change.after, currentSettlements.checking) : '最新进度待核对' }}</span></div>
            <div v-if="change.after"><span>{{ activityLabels(change.after).remaining }}</span><strong>{{ ownerMoney(change.after.remaining_fen) }}</strong><p>{{ activityLabels(change.after).original }} {{ ownerMoney(change.after.source_amount_fen) }} · {{ activityLabels(change.after).paid }} {{ ownerMoney(change.after.paid_fen) }}</p><p v-if="change.after.other_settled_fen !== '0' || change.after.category_key === 'supplier_advances'">{{ activityLabels(change.after).other }} {{ ownerMoney(change.after.other_settled_fen) }}</p></div>
          </div>
        </template>
      </template>
      <template v-else-if="isOpenItem && briefContext">
        <div v-if="(selectedObligation?.source_period && selectedObligation.source_period !== period) || ownerPurposes.length || relatedObjects.length" class="owner-item-description">
          <p v-if="selectedObligation?.source_period && selectedObligation.source_period !== period">业务所属月份：{{ periodText(selectedObligation.source_period) }}</p>
          <p v-for="purpose in ownerPurposes" :key="purpose">用途／备注：{{ purpose }}</p>
          <p v-for="group in relatedObjects" :key="group.label">{{ group.label }}：{{ group.names.join('、') }}</p>
        </div>
        <h4>款项拆解 · 截至{{ periodText(selectedProgress.cutoff) }}末{{ briefContext.selectedPeriodClosed ? '（关账时）' : '' }}</h4>
        <dl class="owner-item-amounts">
          <div><dt>{{ originalLabel }}</dt><dd>{{ ownerMoney(selectedProgress.amount) }}</dd></div>
          <div><dt>{{ isAdvance ? '已退回' : paidLabel }}</dt><dd>{{ ownerMoney(selectedProgress.paid) }}</dd></div>
          <div v-if="selectedProgress.other !== '0' || isAdvance"><dt>{{ isAdvance ? '已冲抵' : '抵销、代付等已处理' }}</dt><dd>{{ ownerMoney(selectedProgress.other) }}</dd></div>
        </dl>
        <p v-if="data.latest_source.deleted">业务已撤回；所选月末余额保留历史口径。</p>
        <p v-if="!selectedObligation" class="checking">这项款项的详情进度待核对，原行及拆解保留列表已确认的金额。</p>
        <p v-else-if="data.settlements.checking || selectedProgress.remaining == null" class="checking">AI 会计核对中，详情进度暂不能完整确认；原行保留列表金额。</p>
        <div v-if="showLatestProgress" class="owner-item-latest">
          <div><h4>后续进展 · 截至最新月份（{{ periodText(latestProgress.cutoff) }}末）</h4><span class="business-state" :class="progressTone(latestProgress.status, latestProgress.remaining, Boolean(currentSettlements?.checking) || !currentObligation)">{{ progressState(latestProgress.status, latestProgress.remaining, Boolean(currentSettlements?.checking) || !currentObligation) }}</span></div>
          <div><span>{{ remainingLabel }}</span><strong>{{ ownerMoney(latestProgress.remaining) }}</strong></div>
        </div>
      </template>
      <template v-else>
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
      </template>
      <template v-if="collection?.page.total_count">
        <h4>{{ isFunds ? '相关款项处理' : isOwnerDetail ? '这笔业务的相关收付' : '实际清偿记录' }}</h4>
        <p v-if="isOwnerDetail">含本业务其他款项 · 截至{{ periodText(data.settlement_view === 'current' ? currentSettlements?.cutoff_period : data.settlements.cutoff_period) }}末</p>
        <ul><li v-for="item in collection.items" :key="item.id"><span>{{ isOwnerDetail ? periodText(item.posting_period) : item.posting_period }}<template v-if="isOwnerDetail"> · {{ movementPurpose(item.name) }}</template> · {{ movementLabel(item) }}<template v-if="isFunds && item.relation_state === 'unresolved'"> · AI 会计核对中</template></span><strong>{{ item.relation_state === 'unresolved' && !isFunds ? 'AI 会计核对中' : isOwnerDetail ? ownerMoney(item.signed_amount_fen) : formatFen(item.signed_amount_fen) }}</strong></li></ul>
        <DashboardPagination automatic :active="panelOpen" :scope="paginationScope()" @pause="pausePages" :compact="isOwnerDetail" :page="collection.page" :loaded="collection.items.length" :loading="moreLoading" :error="moreError" @more="loadMore" @retry="loadMore" />
      </template>
      <p v-else-if="isOwnerDetail">{{ isFunds ? '这笔业务暂无相关款项处理记录。' : '这笔业务暂无相关收付记录。' }}</p>
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
.business-detail-trigger.hidden-summary { display: none; }
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
.business-state.settled { background: var(--accent-soft); color: var(--accent); }
.business-state.pending { background: var(--warning-soft); color: var(--warning); }
.compact-status-panel .business-state.attention { border: 1px solid var(--warning); }
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
li:last-child { border-bottom: 0; }
li span { color: var(--muted); }
button { border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px; background: var(--surface); color: var(--text); font: inherit; font-size: 12px; cursor: pointer; }
.compact-status-details { display: contents; }
.compact-status-trigger { white-space: nowrap; }
.compact-status-details::details-content { display: contents; }
.compact-status-details:not([open]) > :not(summary) { display: none; }
.compact-status-panel { grid-column: 1 / -1; cursor: default; }
.owner-item-heading { min-width: 0; display: grid; gap: 4px; }
.owner-activity-obligations { display: grid; gap: 16px; }
.owner-activity-obligation { display: grid; min-width: 0; gap: 8px; }
.owner-item-description { display: grid; gap: 3px; }
.owner-item-progress { display: grid; grid-template-columns: minmax(180px, 1fr) minmax(0, 2fr); gap: 20px; padding: 16px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface); }
.owner-item-balance { min-width: 0; display: grid; gap: 5px; }
.owner-item-balance > span, .owner-item-latest span { color: var(--muted); font-size: 12px; }
.owner-item-balance > strong { font-size: 26px; font-weight: 800; }
.owner-item-amounts { align-content: center; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
.owner-item-latest { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 12px 16px; border-left: 3px solid var(--line); background: var(--surface); }
.owner-item-latest > div { min-width: 0; display: grid; gap: 4px; }
.owner-item-latest .business-state { justify-self: start; }
.owner-item-latest strong { font-size: 18px; }
@media (max-width: 720px) {
  .owner-item-progress { grid-template-columns: minmax(0, 1fr); gap: 12px; padding: 12px; }
  .owner-item-amounts { grid-template-columns: minmax(0, 1fr); }
  .owner-item-latest { flex-direction: column; align-items: flex-start; }
  header, li { flex-direction: column; align-items: flex-start; gap: 5px; }
  .business-detail-trigger { min-height: 44px; }
  .business-detail-panel, .business-detail-state { padding: 12px; }
  dl { grid-template-columns: minmax(0, 1fr); }
  dl > div, dl > div:first-child { padding: 8px 0; border-top: 1px solid var(--line); border-left: 0; }
  dl > div:first-child { padding-top: 0; border-top: 0; }
}
@media (prefers-reduced-motion: reduce) { .business-detail-trigger svg { transition: none; } }
</style>
