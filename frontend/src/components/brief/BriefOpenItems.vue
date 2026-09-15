<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

import type { BriefOpenCategory, BriefOpenItem, BriefOpenItems } from "../../api/brief";
import { fen, formatFen } from "../../utils/money";
import { businessStateLabel } from "../../api/dashboardContracts";
import BusinessStatusDetails from "../BusinessStatusDetails.vue";

const props = defineProps<{
  openItems: BriefOpenItems;
  periodLabel: string;
  periodStatus: string;
  period: string;
  snapshotVersion?: string | null;
  focusRequest?: number;
}>();
defineEmits<{ changed: [] }>();

const isClosed = computed(() => props.periodStatus === "closed");
const visibleCategories = computed(() => props.openItems.categories.filter((item) => item.count));
const selectedCategoryKey = ref("");
const selectedCategory = computed(
  () => visibleCategories.value.find((item) => item.key === selectedCategoryKey.value) || null,
);
const root = ref<HTMLElement | null>(null);
const focusedCategoryKeys = ref<string[]>([]);
const focusedItemIds = ref<string[]>([]);
const focusActive = ref(false);
let focusTimer: ReturnType<typeof setTimeout> | null = null;

function categoryLabel(label: string) {
  if (!isClosed.value) return label;
  return label.replace(/^待收回/, "应收").replace(/^待收/, "应收").replace(/^待付/, "应付");
}

function openStateLabel(direction: "receivable" | "payable", item: BriefOpenItem) {
  if (item.current_status === "open") return direction === "receivable" ? "当前待收" : "当前待付";
  if (item.current_status === "partial") {
    return direction === "receivable" ? "当前部分收回" : "当前部分支付";
  }
  if (item.current_status === "settled") {
    return direction === "receivable" ? "当前已收回" : "当前已支付";
  }
  if (item.current_status) return `当前${businessStateLabel(item.current_status)}`;
  if (item.status !== "partial" && item.status !== "open") return businessStateLabel(item.status);
  if (isClosed.value) return direction === "receivable" ? "关账时待收" : "关账时待付";
  if (direction === "receivable") {
    return item.status === "partial" ? "部分收回" : "待收回";
  }
  return item.status === "partial" ? "部分支付" : "尚未支付";
}

function statusClass(item: BriefOpenItem) {
  if (item.current_status === "settled") return "status-settled";
  if (!item.current_status && isClosed.value && (item.status === "partial" || item.status === "open")) {
    return "status-historical";
  }
  return "";
}

function outstandingLabel(direction: "receivable" | "payable") {
  if (isClosed.value) return direction === "receivable" ? "应收" : "应付";
  return direction === "receivable" ? "待收" : "待付";
}

function settledLabel(direction: "receivable" | "payable") {
  return direction === "receivable" ? "已收" : "已付";
}

function sourcePeriodLabel(value: string | null | undefined) {
  if (!value) return "";
  const matched = /^(\d{4})-(\d{2})$/.exec(value);
  return matched ? `${matched[1]} 年 ${Number(matched[2])} 月` : value;
}

function hasSettlementProgress(item: BriefOpenItem) {
  return item.status === "partial" && (
    (item.source_amount_fen !== null && item.source_amount_fen !== undefined)
    || fen(item.paid_fen) !== 0n
    || fen(item.other_settled_fen) !== 0n
  );
}

function settlementProgress(item: BriefOpenItem, direction: "receivable" | "payable") {
  if (!hasSettlementProgress(item)) return "";
  const parts: string[] = [];
  if (item.source_amount_fen !== null && item.source_amount_fen !== undefined) {
    parts.push(`原金额 ${formatFen(item.source_amount_fen)}`);
  }
  if (fen(item.paid_fen) !== 0n) parts.push(`${settledLabel(direction)} ${formatFen(item.paid_fen)}`);
  if (fen(item.other_settled_fen) !== 0n) parts.push(`抵销等 ${formatFen(item.other_settled_fen)}`);
  return parts.join(" · ");
}

function categorySummary(category: BriefOpenCategory) {
  return `${category.count} ${category.unit}`;
}

function selectCategory(key: string) {
  selectedCategoryKey.value = key;
}

function isCurrentlyOutstanding(item: BriefOpenItem) {
  const hasCurrentProjection = Object.prototype.hasOwnProperty.call(item, "current_status")
    || Object.prototype.hasOwnProperty.call(item, "current_outstanding_fen");
  if (hasCurrentProjection) {
    if (item.current_status === "settled") return false;
    if (item.current_status === "open" || item.current_status === "partial") return true;
    return item.current_outstanding_fen !== null
      && item.current_outstanding_fen !== undefined
      && fen(item.current_outstanding_fen) !== 0n;
  }
  return (item.status === "open" || item.status === "partial")
    && (item.outstanding_fen === null || fen(item.outstanding_fen) !== 0n);
}

function clearFocusHighlight() {
  focusedCategoryKeys.value = [];
  focusedItemIds.value = [];
  focusActive.value = false;
  focusTimer = null;
}

async function revealCurrentOutstanding() {
  if (focusTimer) clearTimeout(focusTimer);
  const categories = visibleCategories.value.filter(category => category.items.some(isCurrentlyOutstanding));
  const firstCategory = categories[0] ?? visibleCategories.value[0];
  focusActive.value = true;
  focusedCategoryKeys.value = categories.map(category => category.key);
  focusedItemIds.value = firstCategory?.items.filter(isCurrentlyOutstanding).map(item => item.id) ?? [];
  if (firstCategory) selectedCategoryKey.value = firstCategory.key;
  await nextTick();
  const highlightedRow = root.value?.querySelector<HTMLElement>(".open-event-row.focus-highlight");
  const target = highlightedRow ?? root.value;
  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  target?.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
  focusTimer = setTimeout(clearFocusHighlight, 2200);
}

watch(visibleCategories, (categories) => {
  if (!categories.some((item) => item.key === selectedCategoryKey.value)) {
    selectedCategoryKey.value = categories[0]?.key || "";
  }
}, { immediate: true });

watch(() => props.focusRequest, (request, previous) => {
  if (request && request !== previous) void revealCurrentOutstanding();
});

onBeforeUnmount(() => {
  if (focusTimer) clearTimeout(focusTimer);
});
</script>

<template>
  <section ref="root" :class="['brief-section', 'open-items', { 'focus-highlight': focusActive }]" aria-labelledby="open-items-title">
    <div class="section-heading">
      <div>
        <h2 id="open-items-title">
          待收与待付
        </h2>
        <p>
          截至 {{ periodLabel }}末 · {{ isClosed ? `${openItems.total_count} 项关账时有余额` : `${openItems.total_count} 项未完全结清` }}
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

    <details v-if="openItems.issues?.length" class="open-item-issues">
      <summary>{{ openItems.issues.length }} 条来源信息需要核对</summary>
      <ul>
        <li v-for="(issue, index) in openItems.issues" :key="index">
          <p>{{ issue.message || '这笔款项的来源尚待核对。' }}</p>
          <BusinessStatusDetails
            v-if="issue.subject_id"
            :subject-id="issue.subject_id"
            :period="period"
            :snapshot-version="snapshotVersion"
            summary-label="核对依据"
            presentation="brief"
            @changed="$emit('changed')"
          />
        </li>
      </ul>
    </details>
    <div v-if="visibleCategories.length" class="open-workbench">
      <nav class="open-index" aria-label="待收待付分类">
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
            <small>{{ categorySummary(category) }}</small>
          </span>
          <b>{{ formatFen(category.outstanding_fen) }}</b>
        </button>
      </nav>

      <section v-if="selectedCategory" :class="['open-detail', selectedCategory.direction]" :aria-label="`${categoryLabel(selectedCategory.label)}明细`" aria-live="polite">
        <div class="list-columns" aria-hidden="true">
          <span>来源期间</span><span>对象与事项</span><span>状态</span><span class="column-money">{{ outstandingLabel(selectedCategory.direction) }}金额</span><span class="column-action">依据</span>
        </div>
        <ul class="open-event-list" aria-label="待收待付明细">
          <li
            v-for="item in selectedCategory.items"
            :key="item.id"
            :class="['open-event-row', { 'focus-highlight': focusedItemIds.includes(item.id) }]"
          >
            <span class="open-event-reference">
              {{ sourcePeriodLabel(item.source_period) || "来源期间未标注" }}
            </span>
            <span class="open-event-copy">
              <strong>{{ item.party }}</strong>
              <small v-if="item.description && item.description !== item.party">{{ item.description }}</small>
              <small v-if="hasSettlementProgress(item)" class="settlement-progress">
                {{ settlementProgress(item, selectedCategory.direction) }}
              </small>
            </span>
            <span :class="['status', statusClass(item)]">{{ openStateLabel(selectedCategory.direction, item) }}</span>
            <span class="open-event-money">
              <small>{{ outstandingLabel(selectedCategory.direction) }}</small>
              <b>{{ formatFen(item.outstanding_fen) }}</b>
            </span>
            <div v-if="item.source_business?.subject_id" class="open-event-source">
              <BusinessStatusDetails
                :subject-id="item.source_business.subject_id"
                :period="period"
                :snapshot-version="snapshotVersion"
                summary-label="核对依据"
                presentation="brief"
                :brief-context="{
                  direction: selectedCategory.direction,
                  party: item.party,
                  description: item.description,
                  sourcePeriod: item.source_period,
                  sourceAmountFen: item.source_amount_fen,
                  paidFen: item.paid_fen,
                  otherSettledFen: item.other_settled_fen,
                  outstandingFen: item.outstanding_fen,
                  currentStatus: item.current_status,
                  currentOutstandingFen: item.current_outstanding_fen,
                  selectedPeriodClosed: isClosed,
                }"
                @changed="$emit('changed')"
              />
            </div>
          </li>
        </ul>
      </section>
    </div>
    <p v-else-if="openItems.complete !== false && !openItems.unestablished_count" class="empty">
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
  --list-columns: 100px minmax(110px, 1fr) 88px 144px 80px;
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
  padding: 12px 20px;
}

.list-columns {
  display: grid;
  grid-template-columns: var(--list-columns);
  gap: 12px;
  align-items: center;
  min-height: 30px;
  padding: 0 4px 8px;
  border-bottom: 1px solid var(--brief-line);
  color: var(--brief-muted);
  font-size: 11px;
}

.column-money,
.column-action {
  text-align: right;
}

.open-event-list {
  overflow: visible;
  margin: 0;
  padding: 0;
  list-style: none;
}

.open-event-list > li + li {
  border-top: 1px solid var(--brief-line);
}

.open-event-row {
  position: relative;
  display: grid;
  min-height: 76px;
  grid-template-columns: var(--list-columns);
  gap: 12px;
  align-items: center;
  padding: 14px 4px;
  background: transparent;
  transition: background 140ms ease;
}

.open-event-row > .status {
  justify-self: start;
}

.open-event-row:hover,
.open-event-row:focus-within {
  z-index: 4;
  background: var(--brief-soft);
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

.open-event-reference {
  min-width: 0;
  overflow: hidden;
  color: var(--brief-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.open-event-copy {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.open-event-copy strong,
.open-event-copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.open-event-copy strong {
  font-size: 14px;
}

.open-event-copy small {
  color: var(--brief-muted);
  font-size: 11px;
}

.open-event-copy .settlement-progress {
  color: var(--brief-text);
}

.open-event-money {
  display: grid;
  min-width: 0;
  gap: 2px;
  justify-items: end;
  text-align: right;
}

.open-event-money small {
  color: var(--brief-muted);
  font-size: 10px;
}

.open-event-money b {
  font-size: 14px;
  white-space: nowrap;
}

.open-detail.receivable .open-event-money b {
  color: var(--brief-blue);
}

.open-detail.payable .open-event-money b {
  color: var(--brief-amber);
}

.open-event-source {
  display: contents;
}

@media (prefers-reduced-motion: reduce) {
  .open-index button.focus-highlight,
  .open-event-row.focus-highlight {
    animation: none;
    box-shadow: inset 0 0 0 2px var(--brief-green);
    background: color-mix(in srgb, var(--brief-green-soft) 76%, var(--brief-surface));
  }
}

.open-event-source :deep(.compact-status-details) {
  display: contents;
}

.open-event-source :deep(.compact-status-trigger) {
  grid-column: 5;
  justify-self: end;
}

.open-event-source :deep(.compact-status-panel) {
  grid-column: 1 / -1;
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
  white-space: nowrap;
}

.status-historical {
  background: var(--brief-soft);
  color: var(--brief-muted);
  font-weight: 600;
}

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

  .list-columns {
    display: none;
  }

  .open-event-row {
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 6px 12px;
    padding: 12px 2px;
  }

  .open-event-reference {
    grid-row: 1;
    grid-column: 1;
  }

  .open-event-row > .status {
    grid-row: 1;
    grid-column: 2;
    justify-self: end;
  }

  .open-event-copy {
    grid-row: 2;
    grid-column: 1 / -1;
  }

  .open-event-money {
    grid-row: 3;
    grid-column: 1;
    justify-items: start;
    text-align: left;
  }

  .open-event-source :deep(.compact-status-trigger) {
    grid-row: 3;
    grid-column: 2;
    justify-self: end;
  }
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

  .list-columns {
    display: none;
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
    padding: 4px 12px;
  }

  .open-event-row {
    min-height: 82px;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 5px 10px;
    padding: 9px 10px;
  }

  .open-event-reference {
    grid-row: 1;
    grid-column: 1;
  }

  .open-event-copy {
    grid-row: 2;
    grid-column: 1 / -1;
  }

  .open-event-row > .status {
    grid-row: 1;
    grid-column: 2;
    justify-self: end;
  }

  .open-event-money {
    grid-row: 3;
    grid-column: 1;
    justify-items: start;
    text-align: left;
  }

  .open-event-source :deep(.compact-status-trigger) {
    grid-row: 3;
    grid-column: 2;
    justify-self: end;
  }

  .open-event-source :deep(.compact-status-panel) {
    grid-row: 4;
    grid-column: 1 / -1;
  }
}
</style>
