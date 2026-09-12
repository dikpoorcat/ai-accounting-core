<script setup lang="ts">
import { computed } from "vue";
import { RouterLink } from "vue-router";
import { businessStateLabel, type BusinessIssue, type PeriodPreparation } from "../api/dashboardContracts";
import { formatFen } from "../utils/money";
import BusinessStatusDetails from "./BusinessStatusDetails.vue";
const props = defineProps<{ preparation: PeriodPreparation; snapshotVersion?: string | null }>();
defineEmits<{ changed: [] }>();
const frozenFields = [["readiness", "核算准备"], ["inventories", "资料清单"], ["material_coverage", "资料覆盖"], ["previous_close_digest", "前次封存依据"]] as const;
const groups = computed(() => [
  { key: "readiness", label: "所选月核算条件", issues: props.preparation.readiness?.issues ?? [] },
  { key: "materials", label: "当前资料核对", issues: props.preparation.current_followups.materials.issues },
  { key: "accounting", label: "当前核算依据", issues: props.preparation.current_followups.accounting.issues },
  { key: "requirements", label: "当前业务条件", issues: props.preparation.current_followups.close_requirements.issues },
  { key: "settlements", label: "款项来源核对", issues: props.preparation.current_followups.settlements.issues ?? [] },
  { key: "external", label: "外部办理依据", issues: props.preparation.current_followups.external.fact_issues ?? [] },
].filter(group => group.issues.length));
const pendingSubject = computed(() => {
  const pending = props.preparation.current_followups.accounting.pending_subject_id;
  return typeof pending === "string" ? pending : undefined;
});
function bank(issue: BusinessIssue) { return typeof issue.bank_account_id === "string" ? issue.bank_account_id : undefined; }
function collectionLink(section: string) {
  return { path: "/", query: { company_id: props.preparation.company_id, period: props.preparation.period, section }, hash: `#brief-${section}` };
}
</script>
<template>
  <section class="period-preparation" :aria-label="`${preparation.period} 核算与当前后续事项`">
    <header class="preparation-heading">
      <div>
        <h2>{{ preparation.period }} · 所选月末核算</h2>
        <p v-if="preparation.closure.state === 'exact_close'">已关账 · 保留该月封存结果</p>
        <p v-else-if="preparation.closure.state === 'sealed_by_later_close'">由 {{ preparation.closure.sealing_boundary }} 后续关账封存；没有该月独立准备记录。</p>
        <p v-else>未关账 · 展示截至所选月末的正式记录</p>
      </div>
      <details class="frozen-details"><summary>查看封存依据</summary>
        <p v-if="preparation.frozen_readiness?.status === 'ready'"><span v-for="[key, label] in frozenFields" :key="key">{{ label }}：{{ businessStateLabel(preparation.frozen_readiness[key]?.status) }}；</span></p>
        <p v-else>该月没有可单独展示的封存准备记录。</p>
      </details>
    </header>
    <h3>所选期间相关的当前跟进</h3>
    <p class="scope-note">全公司 · 截至 {{ preparation.as_of }} 的相关后续事项 · 不改变所选月封存结果</p>
    <div class="followup-statuses">
      <span>资料：{{ businessStateLabel(preparation.current_followups.materials.status) }}</span>
      <span>核算：{{ businessStateLabel(preparation.current_followups.accounting.status) }}</span>
      <span>业务条件：{{ businessStateLabel(preparation.current_followups.close_requirements.status) }}</span>
      <span>款项：{{ businessStateLabel(preparation.current_followups.settlements.status) }}</span>
    </div>
    <p v-if="preparation.current_followups.settlements.complete === false" class="needs-check" role="status">当前款项金额尚不能完整建立，已知金额仍需连同未知来源核对。</p>
    <p v-if="preparation.current_followups.settlements.unestablished_state_selection_count" class="needs-check">仍有 {{ preparation.current_followups.settlements.unestablished_state_selection_count }} 组来源尚不能证明已被封存采用，不能据此认定结清。</p>
    <p v-if="preparation.current_followups.file_jobs.issue_count" class="needs-check">{{ preparation.current_followups.file_jobs.issue_count }} 项文件任务的来源待核对。<RouterLink :to="collectionLink('file_jobs')">查看任务明细</RouterLink></p>
    <p v-if="preparation.current_followups.external.obligation_count" class="scope-note">外部办理：{{ businessStateLabel(preparation.current_followups.external.status) }} · {{ preparation.current_followups.external.obligation_count }} 项义务 <RouterLink :to="collectionLink('external_followups')">查看办理明细</RouterLink></p>
    <div v-if="groups.length" class="issue-groups">
      <details v-for="group in groups" :key="group.key" class="issue-group">
        <summary><strong>{{ group.label }} · {{ group.issues.length }} 条问题</strong><span class="first-issue">{{ group.issues[0].message || '相关依据需要核对' }}</span></summary>
        <ol><li v-for="(issue, index) in group.issues" :key="index">
          <p>{{ issue.message || '相关依据需要核对，见详细来源。' }}</p>
          <BusinessStatusDetails v-if="issue.subject_id" :subject-id="issue.subject_id" :period="preparation.period" :snapshot-version="snapshotVersion" summary-label="查看相关依据" @changed="$emit('changed')" />
          <RouterLink v-if="bank(issue)" :to="{ path: '/funds', query: { company_id: preparation.company_id, period: preparation.period, statement_account_id: bank(issue), funds_view: 'bank' }, hash: '#bank-details' }">查看对应账户流水与对账</RouterLink>
          <details><summary>来源标识与问题详情</summary><pre>{{ JSON.stringify(issue, null, 2) }}</pre></details>
        </li></ol>
      </details>
    </div>
    <BusinessStatusDetails v-if="pendingSubject" :subject-id="pendingSubject" :period="preparation.period" :snapshot-version="snapshotVersion" summary-label="查看待更正业务依据" @changed="$emit('changed')" />
    <details class="followup-details"><summary>查看相关款项、外部办理与文件任务</summary>
      <dl>
        <div><dt>资料清单</dt><dd>{{ preparation.current_followups.materials.inventory_count }} 份</dd></div>
        <div><dt>尚未发布业务</dt><dd>{{ preparation.current_followups.accounting.unpublished_count }} 项</dd></div>
        <div><dt>款项义务</dt><dd>{{ preparation.current_followups.settlements.obligation_count }} 项</dd></div>
        <div><dt>当前已付款</dt><dd>{{ formatFen(preparation.current_followups.settlements.paid_fen) }}</dd></div>
        <div><dt>代付、抵销等</dt><dd>{{ formatFen(preparation.current_followups.settlements.other_settled_fen) }}</dd></div>
        <div><dt>当前未结金额</dt><dd>{{ formatFen(preparation.current_followups.settlements.remaining_fen) }}</dd></div>
      </dl>
      <RouterLink :to="collectionLink('settlement_events')">查看相关清偿明细</RouterLink>
      <p>外部办理：{{ businessStateLabel(preparation.current_followups.external.status) }} · {{ preparation.current_followups.external.obligation_count }} 项义务 <RouterLink :to="collectionLink('external_followups')">查看办理明细</RouterLink></p>
      <p>文件任务 {{ preparation.current_followups.file_jobs.total_count }} 项，其中 {{ preparation.current_followups.file_jobs.issue_count }} 项任务的来源待核对 <RouterLink :to="collectionLink('file_jobs')">查看任务明细</RouterLink></p>
      <p class="scope-note">明细分批读取；上述业务、义务和任务数量不等于待办总数。</p>
    </details>
    <details class="technical-details"><summary>技术状态与完整投影</summary><pre>{{ JSON.stringify(preparation, null, 2) }}</pre></details>
  </section>
</template>
<style scoped>
.period-preparation { margin: 18px 0; padding: 18px 20px; border: 1px solid var(--line); border-radius: 14px; background: var(--surface); }
.preparation-heading { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
h2, h3 { margin: 0 0 5px; font-size: 16px; } h3 { margin-top: 14px; }
p { margin: 5px 0; } p, dl, details { font-size: 13px; line-height: 1.65; }
.scope-note, .technical-details { color: var(--muted); }
.followup-statuses { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 10px 0; font-size: 13px; }
.needs-check { color: var(--warning); background: var(--warning-soft); padding: 8px 10px; border-radius: 8px; }
.issue-groups { display: grid; gap: 8px; margin: 12px 0; }
.issue-group { padding: 10px 12px; border: 1px solid var(--line); border-left: 3px solid var(--warning); border-radius: 8px; }
summary { cursor: pointer; } summary:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
.first-issue { display: block; margin: 3px 0 0; color: var(--muted); overflow-wrap: anywhere; }
.issue-group[open] .first-issue { display: none; } li { margin: 10px 0; } ol { padding-left: 22px; }
.followup-details, .technical-details { margin-top: 12px; }
dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px 20px; } dt { color: var(--muted); } dd { margin: 0; }
a { color: var(--accent); } pre { max-height: 360px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 720px) { .period-preparation { padding: 14px; } .preparation-heading { display: block; } .frozen-details { margin-top: 6px; } dl { grid-template-columns: repeat(2, minmax(0, 1fr)); } summary { min-height: 44px; } }
</style>
