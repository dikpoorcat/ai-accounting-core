<script setup lang="ts">
import { businessStateLabel } from "../api/dashboardContracts";
import { localBusinessName } from "../api/localKernel";
import { formatFen } from "../utils/money";
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
function state(value: unknown) { const item = record(value); return text(item.relation_state) || text(item.settlement_status) || text(item.status) || text(item.selection_status) || text(record(item.review).status); }
function actualCompletion(value: unknown) {
  const status = text(record(value).actual_completion_status);
  return status === "completed" ? "已办理" : status === "pending" ? "尚未办理" : businessStateLabel(status);
}
function issues(value: unknown) { const item = record(value); return ["issues", "fact_issues", "source_issues", "contract_issues"].flatMap(key => Array.isArray(item[key]) ? item[key] as unknown[] : []).concat(item.result_issue ? [item.result_issue] : []); }
function candidates(value: unknown) { const item = record(value); return Array.isArray(item.candidate_selections) ? item.candidate_selections as unknown[] : []; }
function payrollConfirmation(value: unknown) {
  const confirmation = record(record(value).payroll_confirmation);
  return text(confirmation.mode) ? confirmation : null;
}
function payrollConfirmationMode(value: unknown) {
  return text(record(value).mode) === "explicit_no_change" ? "负责人确认全员无变化" : "负责人确认本月工资方案";
}
function evidence(value: unknown) {
  const items = record(value).evidence;
  return Array.isArray(items) ? items.filter((item): item is string => typeof item === "string") : [];
}
function money(value: unknown) { return value === null || typeof value === "string" ? formatFen(value) : "未提供"; }
function hasAmount(value: unknown) { return Object.prototype.hasOwnProperty.call(record(value), "amount_fen"); }
</script>

<template>
  <div class="business-records">
    <article v-for="(item, index) in items" :key="index">
      <strong>{{ title(item) }}</strong>
      <p v-if="text(record(item).period)">{{ text(record(item).period) }}</p>
      <template v-if="text(record(item).actual_completion_status)">
        <p>{{ actualCompletion(item) }}</p>
        <p>账务核对：{{ businessStateLabel(text(record(item).basis_review_status)) }}</p>
      </template>
      <p v-else-if="state(item)">{{ businessStateLabel(state(item)) }}</p>
      <p v-if="record(item).source_business">来源业务：{{ localBusinessName(text(record(record(item).source_business).kind)) }}</p>
      <template v-if="record(item).selection_status === 'unestablished'">
        <strong>冻结采用未建立</strong>
        <p>相关金额尚未建立。候选来源保留供核对，不能作为已采用结果。</p>
        <div v-for="(selection, selectionIndex) in candidates(item)" :key="`selection-${selectionIndex}`">
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
      <p v-if="hasAmount(item)">{{ text(record(item).amount_label) || "金额" }} {{ money(record(item).amount_fen) }}</p>
      <details v-if="payrollConfirmation(item)" class="payroll-confirmation">
        <summary>查看工资确认依据</summary>
        <p><strong>{{ payrollConfirmationMode(payrollConfirmation(item)) }}</strong></p>
        <p>确认事实：{{ text(record(payrollConfirmation(item)).confirmation_subject_id) }} · 第 {{ record(payrollConfirmation(item)).confirmation_revision }} 版</p>
        <p>精确事实 ID：<code>{{ text(record(payrollConfirmation(item)).confirmation_fact_id) }}</code></p>
        <template v-if="evidence(payrollConfirmation(item)).length">
          <p>原始依据：</p>
          <ul><li v-for="proof in evidence(payrollConfirmation(item))" :key="proof"><code>{{ proof }}</code></li></ul>
        </template>
      </details>
      <BusinessStatusDetails v-if="showBusiness !== false && subject(item)" :subject-id="subject(item)" :period="period" :snapshot-version="snapshotVersion" @changed="$emit('changed')" />
      <details><summary>查看精确来源与问题</summary><pre>{{ JSON.stringify(item, null, 2) }}</pre></details>
    </article>
    <p v-if="!items.length">本页没有记录。</p>
  </div>
</template>

<style scoped>
article { padding: 12px 0; border-bottom: 1px solid var(--line, #dde5df); } p, details { font-size: 13px; line-height: 1.7; } pre { max-height: 360px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; } code { overflow-wrap: anywhere; }
</style>
