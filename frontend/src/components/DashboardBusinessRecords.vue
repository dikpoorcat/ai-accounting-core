<script setup lang="ts">
import { businessStateLabel } from "../api/dashboardContracts";
import { formatFen } from "../utils/money";
import BusinessStatusDetails from "./BusinessStatusDetails.vue";
defineProps<{ items: unknown[]; period: string; snapshotVersion?: string | null; showBusiness?: boolean }>();
defineEmits<{ changed: [] }>();
function record(value: unknown): Record<string, unknown> { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function text(value: unknown) { return typeof value === "string" ? value : ""; }
function subject(value: unknown) { const item = record(value); return text(item.subject_id) || text(record(item.identity).subject_id) || text(record(item.source_business).subject_id); }
function title(value: unknown) {
  const item = record(value), name = text(item.name);
  const names: Record<string, string> = { net: "个人应付净额", tax: "应缴税款", primary: "业务款项", withheld_tax: "已扣税款", employee_social: "个人社保", employee_housing: "个人公积金", employer_social: "公司社保", employer_housing: "公司公积金" };
  return text(item.label) || text(item.title) || names[name] || "业务记录";
}
function status(value: unknown) {
  const item = record(value);
  if (item.checking === true || item.selection_status === "unestablished" || item.relation_state === "unresolved") return "AI 会计核对中";
  return businessStateLabel(text(item.settlement_status) || text(item.status) || text(item.actual_completion_status));
}
function money(value: unknown) { return value === null || typeof value === "string" ? formatFen(value) : "未提供"; }
</script>

<template>
  <div class="business-records">
    <article v-for="(item, index) in items" :key="index">
      <strong>{{ title(item) }}</strong><p v-if="text(record(item).source_period) || text(record(item).period)">{{ text(record(item).source_period) || text(record(item).period) }}</p>
      <p v-if="status(item)">{{ status(item) }}</p>
      <dl>
        <div v-if="'source_amount_fen' in record(item)"><dt>业务金额</dt><dd>{{ money(record(item).source_amount_fen) }}</dd></div>
        <div v-else-if="'amount_fen' in record(item)"><dt>{{ text(record(item).amount_label) || '金额' }}</dt><dd>{{ money(record(item).amount_fen) }}</dd></div>
        <div v-if="'paid_fen' in record(item)"><dt>实际收付</dt><dd>{{ money(record(item).paid_fen) }}</dd></div>
        <div v-if="'other_settled_fen' in record(item)"><dt>抵销等清偿</dt><dd>{{ money(record(item).other_settled_fen) }}</dd></div>
        <div v-if="'remaining_fen' in record(item)"><dt>剩余金额</dt><dd>{{ money(record(item).remaining_fen) }}</dd></div>
      </dl>
      <BusinessStatusDetails v-if="showBusiness !== false && subject(item)" :subject-id="subject(item)" :period="period" :snapshot-version="snapshotVersion" @changed="$emit('changed')" />
    </article>
    <p v-if="!items.length">没有相关记录。</p>
  </div>
</template>

<style scoped>
article { padding: 12px 0; border-bottom: 1px solid var(--line); min-width: 0; overflow-wrap: anywhere; } p { font-size: 13px; line-height: 1.7; margin: 5px 0; color: var(--muted); } dl { display: flex; flex-wrap: wrap; gap: 12px; margin: 8px 0; } dt { color: var(--muted); font-size: 11px; } dd { margin: 4px 0 0; font-weight: 650; }
</style>
