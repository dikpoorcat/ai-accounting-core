<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowReactive, shallowRef, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchBriefGroup, type BriefGroupResponse, type BriefGroupSection } from "../../api/briefGroup";
import type { BriefActivityRow, BriefOpenItem, BriefOpenItems, BriefVoucher } from "../../api/brief";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../../api/client";
import { businessStateLabel } from "../../api/dashboardContracts";
import { formatFen } from "../../utils/money";
import { appendDashboardCollection } from "../../utils/dashboardCollections";
import { groupContributionMembers, contributionProgressRows, payrollMonthLabel } from "../../utils/briefOpenItems";
import BusinessStatusDetails from "../BusinessStatusDetails.vue";
import DashboardPagination from "../DashboardPagination.vue";
import BriefVoucherPreview from "./BriefVoucherPreview.vue";

const props = defineProps<{
  section: BriefGroupSection; groupKey: string; expanded: boolean; period: string; isBatch?: boolean;
  snapshotVersion?: string | null; refreshGeneration?: number; focusedActivity?: BriefActivityRow | null;
  voucherIndex?: ReadonlyMap<string, BriefVoucher>; openSummary?: BriefOpenItems;
  direction?: "receivable" | "payable"; periodClosed?: boolean;
}>();
const emit = defineEmits<{ vouchers: [items: BriefVoucher[]]; requestVoucher: [id: string]; changed: [] }>();
const route = useRoute();
const data = shallowRef<BriefGroupResponse["data"] | null>(null);
const loading = ref(false), error = ref(""), selected = ref(""), preview = ref("");
let request: AbortController | null = null, generation = 0, mounted = true;
function selection() { return JSON.stringify([route.query.company_id, props.period, props.snapshotVersion, props.refreshGeneration, props.section, props.groupKey, props.isBatch]); }
function cancel() { generation++; request?.abort(); request = null; loading.value = false; }
function invalidate() { cancel(); data.value = null; preview.value = ""; selected.value = ""; error.value = ""; }
async function loadMore() {
  if (!props.expanded || loading.value || !props.snapshotVersion || typeof route.query.company_id !== "string") return;
  const page = data.value?.collections.members.page;
  if (page && !page.has_more) return;
  const current = ++generation, scope = selection(), controller = new AbortController();
  request = controller; loading.value = true; error.value = "";
  const valid = () => mounted && current === generation && scope === selection() && request === controller;
  try {
    const result = await fetchBriefGroup(route.query.company_id, props.period, props.section, props.groupKey, props.snapshotVersion, controller.signal, { cursor: page?.next_cursor ?? undefined });
    if (!valid()) return;
    const incoming = result.data;
    const previous = data.value;
    if (previous && previous.section === incoming.section) {
      // Both responses have passed the section, group and snapshot checks.
      if (previous.section === "activity" && incoming.section === "activity") incoming.collections.members = appendDashboardCollection(previous.collections.members, incoming.collections.members);
      if (previous.section === "open_items" && incoming.section === "open_items") incoming.collections.members = appendDashboardCollection(previous.collections.members, incoming.collections.members);
    } else incoming.collections.members.items = shallowReactive(incoming.collections.members.items);
    data.value = incoming;
    if (!previous && props.isBatch && incoming.section === "activity" && props.expanded) selected.value = incoming.collections.members.items[0]?.key ?? "";
    emit("vouchers", incoming.collections.vouchers.items);
  } catch (caught) {
    if (valid()) { if (isDashboardSnapshotChanged(caught)) { invalidate(); emit("changed"); } else error.value = dashboardErrorMessage(caught); }
  } finally { if (valid()) { loading.value = false; request = null; } }
}
const activityMembers = computed(() => {
  const members = data.value?.section === "activity" ? data.value.collections.members.items : [];
  const focused = props.focusedActivity;
  return props.section === "activity" && focused?.group_key === props.groupKey && !members.some(item => item.key === focused.key) ? [focused, ...members] : members;
});
const openMembers = computed(() => data.value?.section === "open_items" ? data.value.collections.members.items : []);
const page = computed(() => data.value?.collections.members.page);
const loaded = computed(() => data.value?.collections.members.items.length ?? 0);
const contributions = computed(() => {
  if (!props.openSummary || page.value?.has_more || !openMembers.value.length) return [];
  return groupContributionMembers(openMembers.value);
});
function amount(value: string | null) { return value == null ? "待核对" : formatFen(value); }
function itemKey(item: BriefOpenItem) { return item.id; }
function toggle(key: string) { selected.value = selected.value === key ? "" : key; }
function openContext(item: BriefOpenItem) {
  return { obligationKey: item.id, categoryKey: item.category_key, cutoffPeriod: props.openSummary?.cutoff_period ?? props.period,
    currentCutoffPeriod: props.openSummary?.current_cutoff_period ?? props.period, status: item.status, direction: props.direction ?? "payable",
    party: item.party, description: item.description, sourceAmountFen: item.source_amount_fen, paidFen: item.paid_fen,
    otherSettledFen: item.other_settled_fen, outstandingFen: item.outstanding_fen, currentStatus: item.current_status,
    currentOutstandingFen: item.current_outstanding_fen, selectedPeriodClosed: props.periodClosed };
}
watch(selection, () => { invalidate(); if (props.expanded) void loadMore(); }, { flush: "sync" });
watch(() => props.expanded, expanded => {
  preview.value = "";
  if (expanded) {
    if (props.isBatch && data.value?.section === "activity") selected.value = data.value.collections.members.items[0]?.key ?? "";
    void loadMore();
  } else { cancel(); selected.value = ""; }
}, { immediate: true });
onBeforeUnmount(() => { mounted = false; cancel(); });
</script>
<template>
  <section v-if="expanded" class="group-members dashboard-business-expansion" aria-label="业务记录" @click.stop @keydown.stop>
    <p v-if="!data && loading" role="status">正在读取业务记录…</p>
    <div v-for="row in contributions" :key="row.id" class="contribution-summary">
      <strong>{{ payrollMonthLabel(row.payroll_period!) }} · 社保与公积金</strong>
      <p>截至{{ payrollMonthLabel(openSummary!.cutoff_period) }}末{{ periodClosed ? '（关账时）' : '' }}</p>
      <table><thead><tr><th>款项</th><th>原应付</th><th>实际已付</th><th>抵销／代付</th><th>月末待付</th></tr></thead>
        <tbody><tr v-for="part in contributionProgressRows(row)" :key="part.component"><th>{{ part.label }}</th>
          <template v-if="part.present"><td data-label="原应付">{{ amount(part.sourceAmountFen) }}</td><td data-label="实际已付">{{ amount(part.paidFen) }}</td><td data-label="抵销／代付">{{ amount(part.otherSettledFen) }}</td><td data-label="月末待付"><strong>{{ amount(part.outstandingFen) }}</strong><small v-for="notice in part.notices" :key="notice">{{ notice }}</small></td></template>
          <td v-else colspan="4">该月末未列待付款项</td>
        </tr></tbody>
      </table>
      <template v-if="contributionProgressRows(row).some(part => part.changed)">
        <p>后续进展 · 截至{{ payrollMonthLabel(openSummary!.current_cutoff_period) }}末</p>
        <div v-for="part in contributionProgressRows(row).filter(part => part.changed)" :key="part.component" class="contribution-part"><span>{{ part.label }}</span><span>{{ businessStateLabel(part.currentStatus) }}</span><strong>{{ amount(part.currentOutstandingFen) }}</strong><small v-for="notice in part.currentNotices" :key="notice">{{ notice }}</small></div>
      </template>
    </div>
    <ul>
      <li v-for="item in activityMembers" :key="item.key" :class="{ highlighted: item.key === focusedActivity?.key }">
        <div class="member-row"><small>{{ item.date || item.recognition.label }}</small><span>{{ item.description || item.title }}<small v-if="item.party" class="member-purpose">{{ item.party }}</small></span><span class="member-state">{{ item.state }}</span><b>{{ amount(item.amount_fen) }}</b>
          <BriefVoucherPreview :voucher="item.voucher_version_id ? voucherIndex?.get(item.voucher_version_id) : undefined" :active="preview === item.key" @preview="value => preview = value ? item.key : preview === item.key ? '' : preview" @open="$emit('requestVoucher', $event)" />
          <button v-if="item.subject_id" type="button" :aria-expanded="selected === item.key" @click="toggle(item.key)">业务进展</button>
        </div>
        <BusinessStatusDetails v-if="item.subject_id" :subject-id="item.subject_id" :period="period" :snapshot-version="snapshotVersion" :refresh-generation="refreshGeneration" :activity-context="item" :expanded="expanded && selected === item.key" hide-summary presentation="brief" @changed="$emit('changed')" />
      </li>
      <li v-for="item in openMembers" :key="item.id">
        <div class="member-row"><small>{{ item.date || item.recognition.label }}</small><span>{{ item.description }}<small v-if="item.purpose" class="member-purpose">{{ item.purpose }}</small></span><span class="member-state">{{ businessStateLabel(item.status) }}</span><b>{{ amount(item.outstanding_fen) }}</b>
          <BriefVoucherPreview :voucher="item.voucher_version_id ? voucherIndex?.get(item.voucher_version_id) : undefined" :active="preview === item.id" @preview="value => preview = value ? item.id : preview === item.id ? '' : preview" @open="$emit('requestVoucher', $event)" />
          <button v-if="item.subject_id" type="button" :aria-expanded="selected === itemKey(item)" @click="toggle(itemKey(item))">业务进展</button>
        </div>
        <BusinessStatusDetails v-if="item.subject_id" :subject-id="item.subject_id" :period="period" :snapshot-version="snapshotVersion" :refresh-generation="refreshGeneration" :brief-context="openContext(item)" :expanded="expanded && selected === item.id" hide-summary presentation="brief" @changed="$emit('changed')" />
      </li>
    </ul>
    <DashboardPagination automatic compact :active="expanded" :scope="selection()" item-label="笔记录" :page="page" :loaded="loaded" :loading="loading" :error="error" @pause="cancel" @more="loadMore" @retry="loadMore" />
  </section>
</template>
<style scoped>
.group-members { grid-column:1 / -1; min-width:0; padding:12px 14px; border:1px solid var(--line); border-radius:10px; background:var(--surface-soft); cursor:default; }
ul { margin:0; padding:0; list-style:none; }
li + li { border-top:1px solid var(--line); }
.member-row { display:grid; grid-template-columns:100px minmax(0,1fr) auto 120px auto auto; align-items:center; gap:12px; padding:10px 0; font-size:12px; }
.member-row > b { text-align:right; }
small, .member-state { color:var(--muted); }
.member-purpose { display:block; margin-top:3px; font-size:11px; overflow-wrap:anywhere; }
button { border:0; background:transparent; color:var(--accent); font:inherit; padding:6px; cursor:pointer; }
.contribution-summary { margin-bottom:12px; font-size:12px; }
.contribution-summary p { margin:10px 0; color:var(--muted); }
.contribution-summary table { width:100%; border-collapse:collapse; }
.contribution-summary th, .contribution-summary td { padding:8px 6px; text-align:right; border-bottom:1px solid var(--line); }
.contribution-summary th:first-child { text-align:left; }
.contribution-summary td small { display:block; }
.contribution-part { display:flex; justify-content:space-between; gap:12px; padding:6px 0; }
.highlighted { background:var(--accent-soft); }
@media(max-width:1024px) { .member-row { grid-template-columns:minmax(0,1fr) auto auto; } .member-row > span:nth-child(2) { grid-column:1 / -1; grid-row:2; } .member-state { grid-column:1; } }
@media(max-width:760px) { .contribution-summary table, .contribution-summary tbody, .contribution-summary tr { display:block; } .contribution-summary thead { display:none; } .contribution-summary tr { padding:8px 0; border-bottom:1px solid var(--line); } .contribution-summary th, .contribution-summary td { display:flex; justify-content:space-between; padding:4px 0; border:0; } .contribution-summary td[data-label]::before { content:attr(data-label); color:var(--muted); } }
</style>
