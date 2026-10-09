<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import type { BriefOpenDisplayGroup, BriefOpenItems, BriefVoucher } from "../../api/brief";
import { fen, formatFen } from "../../utils/money";
import { businessStateLabel } from "../../api/dashboardContracts";
import BriefGroupMembers from "./BriefGroupMembers.vue";
const props = defineProps<{ openItems: BriefOpenItems; items: BriefOpenDisplayGroup[]; periodLabel: string; periodStatus: string; period: string; snapshotVersion?: string | null; refreshGeneration?: number; voucherIndex?: ReadonlyMap<string, BriefVoucher>; focusRequest?: number }>();
defineEmits<{ changed: []; vouchers: [items: BriefVoucher[]]; requestVoucher: [id: string] }>();
const route = useRoute(), expandedItemId = ref(""), selectedCategoryKey = ref("");
const isClosed = computed(() => props.periodStatus === "closed");
const visibleCategories = computed(() => props.openItems.categories.filter(category => category.count).map(category => ({ ...category, items: props.items.filter(item => item.category_key === category.key) })));
const selectedCategory = computed(() => visibleCategories.value.find(category => category.key === selectedCategoryKey.value) ?? null);
const categoryNeedsMore = computed(() => !!selectedCategory.value && new Set(selectedCategory.value.items.map(item => item.group_key)).size < selectedCategory.value.group_count);
function canExpand(_item: BriefOpenDisplayGroup) { return true; }
function toggleItem(item: BriefOpenDisplayGroup, event: Event) {
  if (typeof Element !== "undefined" && event.target instanceof Element && event.target.closest("button, a, .group-members")) return;
  expandedItemId.value = expandedItemId.value === item.group_key ? "" : item.group_key;
}
function itemKeydown(item: BriefOpenDisplayGroup, event: KeyboardEvent) { if (event.target === event.currentTarget && ["Enter", " "].includes(event.key)) { event.preventDefault(); toggleItem(item, event); } }
function categoryLabel(label: string) { return isClosed.value ? label.replace(/^待缴税费/, "应付税费").replace(/^待收回/, "应收").replace(/^待收/, "应收").replace(/^待付/, "应付") : label; }
function outstandingLabel(direction: "receivable" | "payable", key?: string) { if (key === "supplier_advances") return isClosed.value ? "关账时待冲抵" : "待冲抵"; return isClosed.value ? direction === "receivable" ? "应收" : "应付" : direction === "receivable" ? "待收" : "待付"; }
function openStateLabel(direction: "receivable" | "payable", item: BriefOpenDisplayGroup) {
  const status = item.current_status || item.status, prefix = item.current_status ? "当前" : isClosed.value ? "关账时" : "";
  if (item.category_key === "supplier_advances") return prefix + ({ open: "待冲抵", partial: "部分冲抵或退回", settled: "已处理完毕" }[status] ?? businessStateLabel(status));
  if (status === "open") return prefix + (direction === "receivable" ? item.current_status || isClosed.value ? "待收" : "待收回" : "尚未支付");
  if (status === "partial") return prefix + (direction === "receivable" ? "部分收回" : "部分支付");
  if (status === "settled") return prefix + (direction === "receivable" ? "已收回" : "已支付");
  return prefix + businessStateLabel(status);
}
function statusClass(item: BriefOpenDisplayGroup) { const status = item.current_status || item.status; return !item.current_status && isClosed.value && ["open", "partial"].includes(status) ? "status-historical" : status === "settled" ? "status-settled" : ["checking", "over_settled"].includes(status) ? "status-checking" : ""; }
function selectCategory(key: string) { selectedCategoryKey.value = key; }
function amountLabel(value: string | null) { return value == null ? "待核对" : formatFen(value); }
const root = ref<HTMLElement | null>(null), focusedCategoryKeys = ref<string[]>([]), focusedItemIds = ref<string[]>([]), focusActive = ref(false);
let focusTimer: ReturnType<typeof setTimeout> | null = null;
function isCurrentlyOutstanding(item: BriefOpenDisplayGroup) { return item.current_status ? item.current_status !== "settled" && (item.current_outstanding_fen == null || fen(item.current_outstanding_fen) !== 0n) : item.outstanding_fen == null || fen(item.outstanding_fen) !== 0n; }
async function revealCurrentOutstanding() {
  if (focusTimer) clearTimeout(focusTimer);
  const categories = visibleCategories.value.filter(category => category.items.some(isCurrentlyOutstanding));
  const first = categories[0] ?? visibleCategories.value[0];
  focusActive.value = true; focusedCategoryKeys.value = categories.map(category => category.key);
  focusedItemIds.value = first?.items.filter(isCurrentlyOutstanding).map(item => item.group_key) ?? [];
  if (first) selectedCategoryKey.value = first.key;
  await nextTick(); root.value?.querySelector<HTMLElement>(".open-event-row.focus-highlight")?.scrollIntoView({ block: "center" });
  focusTimer = setTimeout(() => { focusedCategoryKeys.value = []; focusedItemIds.value = []; focusActive.value = false; }, 2200);
}
watch(visibleCategories, categories => { if (!categories.some(category => category.key === selectedCategoryKey.value)) selectedCategoryKey.value = categories[0]?.key ?? ""; }, { immediate: true });
watch(() => [route.query.company_id, props.period, props.snapshotVersion, selectedCategoryKey.value], () => expandedItemId.value = "", { flush: "sync" });
watch(() => props.focusRequest, (value, previous) => { if (value && value !== previous) void revealCurrentOutstanding(); });
onBeforeUnmount(() => { if (focusTimer) clearTimeout(focusTimer); });
</script>


<template>
  <section ref="root" :class="['brief-section', 'open-items', { 'focus-highlight': focusActive }]" aria-labelledby="open-items-title">
    <div class="section-heading">
      <div>
        <h2 id="open-items-title">
          应收应付
        </h2>
        <p>
          截至 {{ periodLabel }}末 · {{ openItems.group_count }} 项 · {{ openItems.total_count }} 笔记录 · {{ isClosed ? '关账时有余额' : '未完全结清' }}
        </p>
      </div>
      <div v-if="openItems.total_count" class="heading-balances" :aria-label="isClosed ? '关账时点往来汇总' : '期末往来汇总'">
        <span v-if="openItems.receivable_count" class="receivable">
          <small>{{ outstandingLabel('receivable') }}</small>
          <strong>{{ formatFen(openItems.receivable_fen) }}</strong>
        </span>
        <span v-if="openItems.payable_count" class="payable">
          <small>{{ outstandingLabel('payable') }}</small>
          <strong>{{ formatFen(openItems.payable_fen) }}</strong>
        </span>
      </div>
    </div>

    <p v-if="openItems.complete === false" class="open-item-issues" role="status">AI 会计核对中，已知余额暂不能代表全部款项。</p>
    <div v-if="visibleCategories.length" class="open-workbench" data-section-focus tabindex="-1">
      <nav class="open-index" aria-label="应收应付分类">
        <span class="category-heading">款项分类</span>
        <button
          v-for="category in visibleCategories"
          :key="category.key"
          type="button"
          :class="[category.direction, { 'focus-highlight': focusedCategoryKeys.includes(category.key) }]"
          :aria-current="selectedCategoryKey === category.key ? 'true' : undefined"
          @click="selectCategory(category.key)"
        >
          <span>
            <strong>{{ categoryLabel(category.label) }}</strong>
            <small>{{ category.group_count }} 项 · {{ category.count }} 笔</small>
          </span>
          <b>{{ formatFen(category.outstanding_fen) }}</b>
        </button>
      </nav>

      <section v-if="selectedCategory" :class="['open-detail', selectedCategory.direction]" :aria-label="`${categoryLabel(selectedCategory.label)}明细`" aria-live="polite">
        <div class="list-columns business-list-columns" aria-hidden="true">
          <span>对象</span><span>事项</span><span>状态</span><span class="column-money">{{ outstandingLabel(selectedCategory.direction, selectedCategory.key) }}金额</span><span></span>
        </div>
        <ul class="open-event-list" aria-label="应收应付明细">
          <li
            v-for="item in selectedCategory.items"
            :key="item.group_key"
            :class="['open-event-row', 'business-list-row', { 'focus-highlight': focusedItemIds.includes(item.group_key), expandable: canExpand(item) }]"
            :role="canExpand(item) ? 'button' : undefined"
            :tabindex="canExpand(item) ? 0 : undefined"
            :aria-expanded="canExpand(item) ? expandedItemId === item.group_key : undefined"
            :aria-label="canExpand(item) ? `${item.party}，${item.description}，${expandedItemId === item.group_key ? '收起' : '展开'}业务详情` : undefined"
            @click="toggleItem(item, $event)"
            @keydown="itemKeydown(item, $event)"
          >
            <span class="open-event-copy business-list-object"><strong>{{ item.party }}</strong></span>
            <span class="open-event-matter business-list-matter">
              <span>{{ item.description || "未提供事项说明" }}</span>
              <small>{{ item.member_count }} 笔记录</small>
            </span>
            <span :class="['status', 'business-list-state', statusClass(item)]">{{ openStateLabel(selectedCategory.direction, item) }}</span>
            <span class="open-event-money business-list-money">
              <b>{{ amountLabel(item.outstanding_fen) }}</b>
            </span>
            <svg v-if="canExpand(item)" class="row-chevron business-list-arrow" :class="{ expanded: expandedItemId === item.group_key }" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg><span v-else class="business-list-arrow-space" aria-hidden="true"></span>
            <BriefGroupMembers section="open_items" :group-key="item.group_key" :expanded="expandedItemId === item.group_key" :period="period" :snapshot-version="snapshotVersion" :refresh-generation="refreshGeneration" :voucher-index="voucherIndex" :open-summary="openItems" :direction="selectedCategory.direction" :period-closed="isClosed" @vouchers="$emit('vouchers', $event)" @request-voucher="$emit('requestVoucher', $event)" @changed="$emit('changed')" />
          </li>
        </ul>
        <slot name="pagination" :needs-more="categoryNeedsMore" />
      </section>
    </div>
    <p v-else-if="openItems.complete !== false" class="empty">
      {{ isClosed ? "该月关账时没有应收或应付余额。" : "期末没有未完全结清的应收或应付事项。" }}
    </p>
  </section>
</template>

<style scoped>
.brief-section {
  padding: 0;
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
}

.section-heading {
  align-items: flex-end;
  margin-bottom: 14px;
  padding: 0 2px;
}

h2,
h3,
p {
  margin-top: 0;
}

h2 {
  margin-bottom: 3px;
  font-size: 22px;
  letter-spacing: -0.025em;
}

h3 {
  margin-bottom: 0;
}

.section-heading p {
  margin-bottom: 0;
  color: var(--brief-muted);
  font-size: 13px;
}

.heading-balances {
  display: flex;
  flex: none;
  align-items: flex-end;
  gap: 18px;
}

.heading-balances > span {
  display: grid;
  gap: 2px;
  justify-items: end;
  white-space: nowrap;
}

.heading-balances small {
  color: var(--brief-muted);
  font-size: 11px;
}

.heading-balances strong {
  font-size: 17px;
}

.heading-balances .receivable strong {
  color: var(--brief-blue);
}

.heading-balances .payable strong {
  color: var(--brief-amber);
}

.open-item-issues {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-left: 3px solid var(--brief-amber);
  border-radius: 9px;
  background: var(--brief-amber-soft);
  font-size: 12px;
}

.open-item-issues > summary {
  color: var(--brief-amber);
  font-weight: 760;
  cursor: pointer;
}

.open-item-issues ul {
  display: grid;
  gap: 8px;
  margin: 9px 0 0;
  padding-left: 20px;
}

.open-item-issues li,
.open-item-issues p {
  margin: 0;
}

.open-workbench {
  --business-list-columns: minmax(0, 1fr) minmax(0, 1fr) 110px 144px 12px;
  display: grid;
  min-width: 0;
  grid-template-columns: 280px minmax(0, 1fr);
  align-items: stretch;
  border: 1px solid var(--brief-line);
  border-radius: 14px;
  background: var(--brief-surface);
}

.open-index {
  display: grid;
  min-width: 0;
  align-content: start;
  gap: 3px;
  padding: 12px 10px;
  border-right: 1px solid var(--brief-line);
  border-radius: 14px 0 0 14px;
  background: var(--brief-surface);
}

.category-heading {
  display: flex;
  min-height: 30px;
  align-items: center;
  padding: 0 12px 7px;
  color: var(--brief-muted);
  font-size: 11px;
  letter-spacing: 0.04em;
}

.open-index button {
  --category-accent: var(--brief-green);
  --category-soft: var(--brief-green-soft);
  position: relative;
  display: grid;
  width: 100%;
  min-width: 0;
  min-height: 46px;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 5px 10px;
  align-items: center;
  padding: 11px 12px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: transparent;
  color: var(--brief-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 140ms ease, border-color 140ms ease;
}

.open-index button::before {
  position: absolute;
  top: 12px;
  bottom: 12px;
  left: -1px;
  width: 2px;
  border-radius: 999px;
  background: transparent;
  content: "";
}

.open-index button:hover {
  background: var(--brief-surface);
}

.open-index button[aria-current="true"] {
  border-color: color-mix(in srgb, var(--category-accent) 16%, transparent);
  background: color-mix(in srgb, var(--category-soft) 54%, var(--brief-surface));
}

.open-index button[aria-current="true"]::before {
  background: var(--category-accent);
}

.open-index button:focus-visible {
  outline: 2px solid var(--category-accent);
  outline-offset: 2px;
}

.open-index button strong {
  min-width: 0;
  white-space: nowrap;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.5;
}

.open-index button[aria-current="true"] strong {
  font-weight: 750;
}

.open-index button.receivable {
  --category-accent: var(--brief-blue);
  --category-soft: var(--brief-blue-soft);
}

.open-index button.payable {
  --category-accent: var(--brief-amber);
  --category-soft: var(--brief-amber-soft);
}

.open-index button span {
  display: contents;
}

.open-index button small {
  color: var(--brief-muted);
  font-size: 11px;
  text-align: right;
  white-space: nowrap;
}

.open-index button b {
  grid-column: 1 / -1;
  color: var(--category-accent);
  font-size: 15px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.open-detail {
  min-width: 0;
  padding: 12px var(--dashboard-list-gutter);
}

.column-money {
  text-align: right;
}

.open-event-list {
  overflow: visible;
  margin: 0;
  padding: 0;
  list-style: none;
}

@keyframes open-item-focus-pulse {
  0%, 100% { box-shadow: none; }
  18%, 52%, 86% {
    box-shadow: inset 0 0 0 2px var(--brief-green);
    background: color-mix(in srgb, var(--brief-green-soft) 76%, var(--brief-surface));
  }
}

.open-index button.focus-highlight,
.open-event-row.focus-highlight {
  animation: open-item-focus-pulse 1.8s ease both;
}

.open-event-row.focus-highlight {
  z-index: 5;
}

.open-detail.receivable .open-event-money b {
  color: var(--brief-blue);
}

.open-detail.payable .open-event-money b {
  color: var(--brief-amber);
}

@media (prefers-reduced-motion: reduce) {
  .open-index button.focus-highlight,
  .open-event-row.focus-highlight {
    animation: none;
    box-shadow: inset 0 0 0 2px var(--brief-green);
    background: color-mix(in srgb, var(--brief-green-soft) 76%, var(--brief-surface));
  }
}

.status {
  display: inline-flex;
  min-height: 23px;
  align-items: center;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
  font-size: 11px;
  font-weight: 750;
  white-space: normal;
  max-width: 100%;
}

.status-historical {
  background: var(--brief-soft);
  color: var(--brief-muted);
  font-weight: 600;
}

.status-checking { border: 1px solid var(--brief-amber); }

.status-settled {
  background: var(--brief-green-soft);
  color: var(--brief-green);
}

.empty {
  margin: 0;
  padding: 24px;
  border: 1px dashed var(--brief-line);
  border-radius: 12px;
  background: var(--brief-surface);
  color: var(--brief-muted);
  text-align: center;
}

@media (min-width: 761px) and (max-width: 1199px) {
  .open-workbench {
    grid-template-columns: 264px minmax(0, 1fr);
  }
}

@media (max-width: 1199px) {
  .open-event-row > .status { grid-column: 1; justify-self: start; }
}

@media (max-width: 760px) {
  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 7px;
  }

  .heading-balances {
    width: 100%;
    justify-content: flex-start;
  }

  .heading-balances > span {
    justify-items: start;
  }

  .open-workbench {
    grid-template-columns: minmax(0, 1fr);
  }

  .open-index {
    gap: 3px;
    padding: 10px;
    border-right: 0;
    border-bottom: 1px solid var(--brief-line);
    border-radius: 14px 14px 0 0;
  }

  .open-index button {
    min-height: 44px;
    padding: 9px 12px;
  }

  .open-detail {
    padding: 4px var(--dashboard-list-gutter);
  }

}
</style>
