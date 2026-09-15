<!-- @format -->

<script setup lang="ts">
import { computed } from "vue";
import { RouterLink } from "vue-router";

import {
  type BusinessIssue,
  type PeriodPreparation,
} from "../api/dashboardContracts";
import { fen, formatFen } from "../utils/money";

const props = defineProps<{
  preparation: PeriodPreparation;
  snapshotVersion?: string | null;
  ownerNavigation?: boolean;
}>();
const emit = defineEmits<{ changed: []; focusSettlements: [] }>();

const groups = computed(() =>
  [
    {
      key: "readiness",
      label: "所选月核算条件",
      issues: props.preparation.readiness?.issues ?? [],
    },
    {
      key: "materials",
      label: "当前资料核对",
      issues: props.preparation.current_followups.materials.issues,
    },
    {
      key: "accounting",
      label: "当前核算依据",
      issues: props.preparation.current_followups.accounting.issues,
    },
    {
      key: "requirements",
      label: "当前业务条件",
      issues: props.preparation.current_followups.close_requirements.issues,
    },
    {
      key: "settlements",
      label: "款项来源核对",
      issues: props.preparation.current_followups.settlements.issues ?? [],
    },
    {
      key: "external",
      label: "外部办理依据",
      issues: props.preparation.current_followups.external.fact_issues ?? [],
    },
  ].filter((group) => group.issues.length),
);
const issueCount = computed(() =>
  groups.value.reduce((total, group) => total + group.issues.length, 0),
);
const settlementComplete = computed(() => {
  const settlement = props.preparation.current_followups.settlements;
  return (
    settlement.complete !== false &&
    !(settlement.unestablished_state_selection_count ?? 0) &&
    settlement.remaining_fen !== null &&
    fen(settlement.remaining_fen) === 0n
  );
});
const settlementHeadline = computed(() => {
  const settlement = props.preparation.current_followups.settlements;
  if (!settlement.obligation_count && settlementComplete.value)
    return "暂无相关款项";
  if (settlementComplete.value) return "已结清";
  if (settlement.remaining_fen === null) return "金额待确认";
  return formatFen(settlement.remaining_fen);
});
const externalPendingCount = computed(() => {
  const external = props.preparation.current_followups.external;
  const completed =
    (external.completion_status_counts.completed ?? 0) +
    (external.completion_status_counts.not_applicable ?? 0);
  return Math.max(0, external.obligation_count - completed);
});
const fileFailedCount = computed(
  () => props.preparation.current_followups.file_jobs.status_counts.failed ?? 0,
);
const fileProcessingCount = computed(() => {
  const counts = props.preparation.current_followups.file_jobs.status_counts;
  return (counts.pending ?? 0) + (counts.running ?? 0);
});
const fileHeadline = computed(() => {
  const jobs = props.preparation.current_followups.file_jobs;
  if (!jobs.total_count) return "暂无相关任务";
  if (jobs.issue_count) return `${jobs.issue_count} 项需核对`;
  if (fileFailedCount.value) return `${fileFailedCount.value} 项未成功`;
  if (fileProcessingCount.value) return `${fileProcessingCount.value} 项处理中`;
  return `${jobs.total_count} 项已处理`;
});
const hasFollowup = computed(
  () =>
    !settlementComplete.value ||
    externalPendingCount.value > 0 ||
    fileFailedCount.value > 0 ||
    fileProcessingCount.value > 0 ||
    props.preparation.current_followups.file_jobs.issue_count > 0 ||
    issueCount.value > 0,
);
const followupState = computed(() =>
  hasFollowup.value ? "仍有事项" : "当前无待办",
);
const closureMessage = computed(() => {
  const closure = props.preparation.closure;
  if (closure.state === "exact_close")
    return `${props.preparation.period} 已关账；以下进展不改变所选月封存结果。`;
  if (closure.state === "sealed_by_later_close")
    return `${props.preparation.period} 已由 ${closure.sealing_boundary} 的后续关账封存；以下显示当前进展。`;
  return `${props.preparation.period} 尚未关账；以下事项会持续更新。`;
});

function bank(issue: BusinessIssue) {
  return typeof issue.bank_account_id === "string"
    ? issue.bank_account_id
    : undefined;
}
function focusSettlements() {
  if (props.ownerNavigation && !settlementComplete.value)
    emit("focusSettlements");
}
</script>

<template>
  <section
    :class="['period-preparation', { attention: hasFollowup }]"
    :aria-label="`${preparation.period} 核算与当前后续事项`"
  >
    <header class="preparation-heading">
      <div>
        <p class="preparation-kicker">当前后续事项</p>
        <h2>所选月末核算后，还有什么需要处理？</h2>
        <span>{{ closureMessage }}</span>
      </div>
      <span :class="['followup-state', { attention: hasFollowup }]">{{
        followupState
      }}</span>
    </header>
    <p class="scope-note">
      全公司 · 截至 {{ preparation.as_of }} 的相关后续事项
    </p>

    <div class="followup-cards">
      <component
        :is="ownerNavigation && !settlementComplete ? 'button' : 'article'"
        :class="[
          'followup-card',
          {
            attention: !settlementComplete,
            clickable: ownerNavigation && !settlementComplete,
          },
        ]"
        :type="ownerNavigation && !settlementComplete ? 'button' : undefined"
        @click="focusSettlements"
      >
        <span>收付款跟进</span>
        <strong>{{ settlementHeadline }}</strong>
        <small
          >{{
            preparation.current_followups.settlements.obligation_count
          }}
          项相关款项；包括付款、代付和抵销</small
        >
        <span v-if="ownerNavigation && !settlementComplete" class="card-action"
          >点击查看</span
        >
      </component>
      <article
        :class="['followup-card', { attention: externalPendingCount > 0 }]"
      >
        <span>申报与外部事项</span>
        <strong>{{
          preparation.current_followups.external.obligation_count
            ? `${externalPendingCount} 项待办`
            : "暂无"
        }}</strong>
        <small
          >所选月份相关共
          {{
            preparation.current_followups.external.obligation_count
          }}
          项；按实际完成依据判断</small
        >
      </article>
      <article
        :class="[
          'followup-card',
          {
            attention:
              fileFailedCount > 0 ||
              preparation.current_followups.file_jobs.issue_count > 0,
          },
        ]"
      >
        <span>文件处理</span>
        <strong>{{ fileHeadline }}</strong>
        <small
          >共
          {{
            preparation.current_followups.file_jobs.total_count
          }}
          项；生成成功不代表付款或申报完成</small
        >
      </article>
    </div>

    <p
      v-if="preparation.current_followups.settlements.complete === false"
      class="needs-check"
      role="status"
    >
      当前款项金额尚不能完整建立，已知金额仍需连同未知来源核对。
    </p>
    <p
      v-if="
        preparation.current_followups.settlements
          .unestablished_state_selection_count
      "
      class="needs-check"
    >
      仍有
      {{
        preparation.current_followups.settlements
          .unestablished_state_selection_count
      }}
      组来源尚不能证明已被封存采用，不能据此认定结清。
    </p>
    <p
      v-if="preparation.current_followups.file_jobs.issue_count"
      class="needs-check"
    >
      {{
        preparation.current_followups.file_jobs.issue_count
      }}
      项文件任务结果或引用依据待核对。
    </p>

    <details v-if="groups.length" class="issue-summary" open>
      <summary>{{ issueCount }} 条事项需要核对</summary>
      <div class="issue-groups">
        <details v-for="group in groups" :key="group.key" class="issue-group">
          <summary>
            <strong
              >{{ group.label }} · {{ group.issues.length }} 条核对提示</strong
            ><span class="first-issue">{{
              group.issues[0].message || "相关依据需要核对"
            }}</span>
          </summary>
          <ol>
            <li v-for="(issue, index) in group.issues" :key="index">
              <p>{{ issue.message || "相关依据需要核对，见详细来源。" }}</p>
              <RouterLink
                v-if="bank(issue)"
                :to="{
                  path: '/funds',
                  query: {
                    company_id: preparation.company_id,
                    period: preparation.period,
                    statement_account_id: bank(issue),
                    funds_view: 'bank',
                  },
                  hash: '#bank-details',
                }"
                >查看对应账户流水与对账</RouterLink
              >
            </li>
          </ol>
        </details>
      </div>
    </details>
  </section>
</template>

<style scoped>
.period-preparation {
  margin: 18px 0;
  padding: 19px 20px;
  border: 1px solid var(--brief-line, var(--line));
  border-radius: var(--radius-panel, 14px);
  background: var(--surface);
}

.preparation-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.preparation-kicker {
  margin: 0 0 3px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
}
h2 {
  margin: 0;
  font-size: 18px;
  line-height: 1.35;
}
.preparation-heading div > span {
  display: block;
  margin-top: 4px;
  color: var(--muted);
  font-size: 13px;
}
.followup-state {
  flex: 0 0 auto;
  padding: 5px 10px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 750;
}
.followup-state.attention {
  background: var(--warning-soft);
  color: var(--warning);
}
p,
details {
  font-size: 13px;
  line-height: 1.65;
}
.scope-note {
  margin: 8px 0 12px;
  color: var(--muted);
}
.followup-cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.followup-card {
  position: relative;
  display: flex;
  min-width: 0;
  flex-direction: column;
  padding: 11px 12px;
  border: 1px solid transparent;
  border-radius: var(--brief-control-radius, 9px);
  background: var(--brief-soft, var(--surface-soft));
  color: inherit;
  font: inherit;
  text-align: left;
}
.followup-card.attention > strong {
  color: var(--warning);
}
button.followup-card {
  cursor: pointer;
}
button.followup-card:hover {
  border-color: var(--accent);
}
button.followup-card:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 3px;
}
.followup-card > span {
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
}
.followup-card > strong {
  margin-top: 4px;
  font-size: 16px;
  line-height: 1.25;
  overflow-wrap: anywhere;
}
.followup-card > small {
  margin-top: 4px;
  color: var(--muted);
  font-size: 10px;
  line-height: 1.45;
}
.followup-card > .card-action {
  align-self: flex-start;
  margin-top: 8px;
  color: var(--accent);
  font-weight: 800;
}
.needs-check {
  margin: 9px 0 0;
  padding: 8px 10px;
  border-radius: 8px;
  color: var(--warning);
  background: var(--warning-soft);
}
.issue-summary {
  margin-top: 12px;
  padding-top: 9px;
  border-top: 1px solid var(--line);
}
.issue-summary > summary {
  color: var(--accent);
  font-size: 12px;
  font-weight: 750;
  cursor: pointer;
}
.issue-groups {
  display: grid;
  gap: 8px;
  margin: 10px 0;
}
.issue-group {
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-left: 3px solid var(--warning);
  border-radius: 8px;
}
summary {
  cursor: pointer;
}
summary:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 3px;
}
.first-issue {
  display: block;
  margin: 3px 0 0;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.issue-group[open] .first-issue {
  display: none;
}
li {
  margin: 10px 0;
}
ol {
  padding-left: 22px;
}
a {
  color: var(--accent);
}
pre {
  max-height: 360px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 720px) {
  .period-preparation {
    padding: 16px;
  }
  .preparation-heading {
    display: block;
  }
  .followup-state {
    display: inline-block;
    margin-top: 9px;
  }
  .followup-cards {
    grid-template-columns: 1fr;
  }
  summary {
    min-height: 44px;
  }
}
</style>
