<script setup lang="ts">
import { businessStateLabel } from "../api/dashboardContracts";
import { localBusinessName } from "../api/localKernel";
import { formatFen } from "../utils/money";
import VoucherTrace from "./brief/VoucherTrace.vue";
import BusinessStatusDetails from "./BusinessStatusDetails.vue";
defineProps<{ items: unknown[]; period: string; snapshotVersion?: string | null; showBusiness?: boolean }>();
defineEmits<{ changed: [] }>();
function record(value: unknown): Record<string, unknown> { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function text(value: unknown) { return typeof value === "string" ? value : ""; }
function subject(value: unknown) { const item = record(value); return text(item.subject_id) || text(record(item.identity).subject_id) || text(record(item.source_business).subject_id); }
function title(value: unknown) {
  const item = record(value), name = text(item.name);
  const names: Record<string, string> = { net: "个人应付净额", tax: "应缴税款", primary: "来源款项", withheld_tax: "已扣税款", employee_social: "个人社保", employee_housing: "个人公积金", employer_social: "公司社保", employer_housing: "公司公积金" };
  return text(item.label) || text(item.title) || names[name] || name || "业务记录";
}
function state(value: unknown) { const item = record(value); return text(item.settlement_status) || text(item.status) || text(item.selection_status) || text(item.completion_status) || text(record(item.review).status); }
function calculation(value: unknown) { const item = record(value); return text(item.calculation_id) || text(item.source_calculation_id); }
function issues(value: unknown) { const item = record(value); return ["issues", "fact_issues", "source_issues", "contract_issues"].flatMap(key => Array.isArray(item[key]) ? item[key] as unknown[] : []).concat(item.result_issue ? [item.result_issue] : []); }
function candidates(value: unknown) { const item = record(value); return Array.isArray(item.candidate_selections) ? item.candidate_selections as unknown[] : []; }
function selectionCandidates(value: unknown) { return Array.isArray(record(value).candidates) ? record(value).candidates as unknown[] : []; }
const amounts = [["source_amount_fen", "来源金额"], ["amount_fen", "金额"], ["paid_fen", "已付款"], ["other_settled_fen", "代付、抵销等"], ["remaining_fen", "未结金额"], ["cost_fen", "账面成本"], ["book_value_fen", "账面价值"], ["company_cost_fen", "公司成本"], ["gross_salary_fen", "应发工资"], ["net_salary_fen", "应付净薪"]] as const;
function money(value: unknown) { return value === null || typeof value === "string" ? formatFen(value) : "未提供"; }
</script>

<template>
  <div class="business-records">
    <article v-for="(item, index) in items" :key="index">
      <strong>{{ title(item) }}</strong>
      <p v-if="text(record(item).period)">{{ text(record(item).period) }}</p>
      <p v-if="state(item)">{{ businessStateLabel(state(item)) }}</p>
      <p v-if="record(item).source_business">来源业务：{{ localBusinessName(text(record(record(item).source_business).kind)) }}</p>
      <template v-if="record(item).selection_status === 'unestablished'">
        <strong>冻结采用未建立</strong>
        <p>相关金额尚未建立。候选来源保留供核对，不能作为已采用结果。</p>
        <div v-for="(selection, selectionIndex) in candidates(item)" :key="`selection-${selectionIndex}`">
          <div v-for="(candidate, candidateIndex) in selectionCandidates(selection)" :key="`candidate-${candidateIndex}`">
            <VoucherTrace v-if="calculation(candidate)" :calculation-id="calculation(candidate)" />
          </div>
          <details><summary>查看本组候选与未建立原因</summary><pre>{{ JSON.stringify(selection, null, 2) }}</pre></details>
        </div>
        <details v-if="record(item).established_card">
          <summary>查看已确认的旧记录</summary>
          <p>该记录保留已证明的旧来源，不表示本次冻结候选已被采用。</p>
          <DashboardBusinessRecords :items="[record(item).established_card]" :period="period" :show-business="false" />
        </details>
      </template>
      <p v-if="text(record(item).message)">{{ text(record(item).message) }}</p>
      <p v-for="(issue, issueIndex) in issues(item)" :key="`issue-${issueIndex}`">{{ text(record(issue).message) || "相关来源含局部问题，需要核对。" }}</p>
      <p v-for="[key, label] in amounts.filter(([key]) => key in record(item))" :key="key">{{ label }} {{ money(record(item)[key]) }}</p>
      <VoucherTrace v-if="calculation(item)" :calculation-id="calculation(item)" :voucher-version-id="text(record(item).voucher_version_id) || undefined" />
      <VoucherTrace v-if="text(record(item).settlement_calculation_id) && text(record(item).settlement_calculation_id) !== calculation(item)" :calculation-id="text(record(item).settlement_calculation_id)" />
      <BusinessStatusDetails v-if="showBusiness !== false && subject(item)" :subject-id="subject(item)" :period="period" :snapshot-version="snapshotVersion" @changed="$emit('changed')" />
      <details><summary>查看精确来源与问题</summary><pre>{{ JSON.stringify(item, null, 2) }}</pre></details>
    </article>
    <p v-if="!items.length">本页没有记录。</p>
  </div>
</template>

<style scoped>
article { padding: 12px 0; border-bottom: 1px solid var(--line, #dde5df); } p, details { font-size: 13px; line-height: 1.7; } pre { max-height: 360px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
