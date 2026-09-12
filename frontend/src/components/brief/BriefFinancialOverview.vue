<script setup lang="ts">
import { useRoute } from "vue-router";
import type { BriefCash, BriefData, BriefPosition } from "../../api/brief";
import { fen, formatFen, formatPositiveFen } from "../../utils/money";

const props = defineProps<{
  cash: BriefCash;
  funds: BriefData["funds_overview"];
  position: BriefPosition;
  unmatched: BriefData["unmatched_bank_activity"];
}>();
const route = useRoute();

const components = [
  ["银行存款", "bank_fen"],
  ["固定资产净值", "fixed_asset_net_fen"],
  ["无形资产净值", "intangible_asset_net_fen"],
  ["其他资产", "other_assets_fen"],
] as const;

function componentRatio(value: string | null) {
  if (value === null || props.position.assets_fen === null) return null;
  const total = fen(props.position.assets_fen);
  if (total <= 0n) return 0;
  return Math.max(0, Math.min(100, Number((fen(value) * 10_000n) / total) / 100));
}

function bankStateLabel(state: string) {
  return (
    {
      matched: "已匹配",
      unmatched: "待识别",
      needs_review: "资料或核对结果待复核",
    }[state] || "状态尚不能确认"
  );
}

function coverageLabel() {
  if (props.cash.missing_account_count) return `${props.cash.missing_account_count} 个银行账户尚未提供本月流水`;
  return {
    missing: "银行流水覆盖尚不能完整确认",
    partial: "流水覆盖或核对状态尚不能完整确认",
    complete: "本月各银行账户流水已提供",
    not_applicable: "暂无公司银行账户",
  }[props.cash.coverage_state];
}

function formatDate(value: string | null) {
  if (!value) return "日期未提供";
  if (/^\d{4}-\d{2}$/.test(value)) return `${value} · 按月确认`;
  const [, month, day] = value.slice(0, 10).split("-");
  return `${Number(month)} 月 ${Number(day)} 日`;
}
</script>

<template>
  <section class="financial-section" aria-labelledby="financial-title">
    <div class="section-heading">
      <div>
        <h2 id="financial-title">资金与资产负债</h2>
      </div>
      <span v-if="position.equation_valid !== true" :class="['equation-status', { error: position.equation_valid === false }]">
        {{ position.equation_valid === null ? '财务位置无法完整建立' : '资产负债金额需要核对' }}
      </span>
    </div>

    <details v-if="unmatched.count" class="pending-bank">
      <summary>
        <span>
          <strong>{{ unmatched.count }} 笔匹配状态待核对</strong>
          <small>原始流水与已入账业务仍需核对</small>
        </span>
        <span>
          流入 {{ formatFen(unmatched.inflow_fen) }} · 流出 {{ formatFen(unmatched.outflow_fen) }}
        </span>
      </summary>
      <ul>
        <li v-for="item in unmatched.rows" :key="item.id">
          <div>
            <small>{{ formatDate(item.date) }} · {{ item.party }}</small>
            <strong>{{ item.memo }}</strong>
          </div>
          <span class="bank-state">{{ bankStateLabel(item.state) }}</span>
          <b>{{ item.direction === "inflow" ? "+" : "−" }}{{ formatFen(item.amount_fen) }}</b>
        </li>
      </ul>
      <p v-if="unmatched.rows_truncated">当前展示前 {{ unmatched.rows.length }} 笔。
        <RouterLink :to="{ name: 'funds', query: route.query, hash: '#bank-details' }">查看全部银行流水</RouterLink>
      </p>
    </details>

    <div class="overview-grid">
      <article class="overview-card cash-card">
        <header>
          <div>
            <p>银行、现金及公司支付平台 · 已扣除账户互转</p>
            <h3>本月公司收付款</h3>
          </div>
          <span class="state-chip">
            {{ funds.net_change_fen === null ? "资金变动尚不能确认" : fen(funds.net_change_fen) > 0n ? "资金增加" : fen(funds.net_change_fen) < 0n ? "资金减少" : "资金无净变动" }}
          </span>
        </header>
        <div class="flow">
          <div>
            <span>对外收款</span>
            <strong>{{ formatFen(funds.inflow_fen) }}</strong>
          </div>
          <span aria-hidden="true">→</span>
          <div class="outflow">
            <span>对外付款</span>
            <strong>{{ formatFen(funds.outflow_fen) }}</strong>
          </div>
        </div>
        <dl class="summary-rows">
          <div>
            <dt>月末账面资金</dt>
            <dd>{{ formatFen(funds.total_fen) }}</dd>
          </div>
          <div v-if="funds.internal_transfer_fen === null || fen(funds.internal_transfer_fen)">
            <dt>公司账户间调拨</dt><dd>{{ formatFen(funds.internal_transfer_fen) }}</dd>
          </div>
        </dl>
        <details class="bank-proof"><summary>银行流水核对：{{ coverageLabel() }}</summary>
          <p>{{ cash.transaction_count }} 笔流水，{{ cash.matched_count }} 笔已匹配；{{ cash.unmatched_count }} 笔待识别，{{ cash.needs_review_count }} 笔需复核。</p>
          <p>已提供流水流入 {{ formatFen(cash.inflow_fen) }} · 流出 {{ formatFen(cash.outflow_fen) }}</p>
          <RouterLink :to="{ name: 'funds', query: route.query, hash: '#bank-details' }">查看银行流水</RouterLink>
        </details>
      </article>

      <details id="position-overview" class="overview-card position-card" tabindex="-1">
        <summary class="position-summary">
          <header>
            <div>
              <p>所选月末的资产与负债</p>
              <h3>月末资产与负债</h3>
            </div>
            <strong>资产 {{ position.assets_fen === null ? '无法完整建立' : formatFen(position.assets_fen) }}</strong>
          </header>
          <div class="components">
            <div
              v-for="([label, key], index) in components"
              :key="key"
              :class="['component-row', { subdued: position[key] !== null && !fen(position[key]) }]"
            >
              <span>{{ label }}</span>
              <div v-if="componentRatio(position[key]) !== null" class="track">
                <span :style="{ width: `${componentRatio(position[key])}%` }" :data-index="index" />
              </div>
              <strong>{{ formatFen(position[key]) }}</strong>
            </div>
          </div>
          <p>负债 {{ formatFen(position.liabilities_fen) }} · 展开查看金额构成</p>
        </summary>
          <p v-if="position.equation_valid !== null" class="equation">
            资产 {{ formatFen(position.assets_fen) }} = 负债 {{ formatFen(position.liabilities_fen) }} + 所有者权益
            {{ formatFen(position.capital_fen) }} {{ fen(position.cumulative_result_fen) < 0n ? "−" : "+" }} 累计差额
            {{ formatPositiveFen(position.cumulative_result_fen) }}
          </p>
        <p v-else>部分来源尚不能精确归属，暂不判断资产负债等式；已知分项仍列示。</p>
        <ul v-if="position.issues?.length" class="proof">
          <li v-for="(issue, index) in position.issues" :key="index">{{ issue.message }}</li>
        </ul>
        <ul class="proof">
          <li><span>固定资产原值</span><strong>{{ formatFen(position.fixed_asset_cost_fen) }}</strong></li>
          <li><span>减：累计折旧</span><strong>{{ formatFen(position.accumulated_depreciation_fen) }}</strong></li>
          <li><span>固定资产净值</span><strong>{{ formatFen(position.fixed_asset_net_fen) }}</strong></li>
          <li><span>无形资产原值</span><strong>{{ formatFen(position.intangible_asset_cost_fen) }}</strong></li>
          <li><span>减：累计摊销</span><strong>{{ formatFen(position.accumulated_amortization_fen) }}</strong></li>
          <li><span>无形资产净值</span><strong>{{ formatFen(position.intangible_asset_net_fen) }}</strong></li>
        </ul>
      </details>
    </div>
  </section>
</template>

<style scoped>
.financial-section {
  padding: 20px;
  border: 1px solid var(--brief-line);
  border-radius: 20px;
  background: var(--brief-surface);
  box-shadow: var(--brief-shadow);
}

.section-heading,
.overview-card header,
.pending-bank summary,
.pending-bank li {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.section-heading {
  align-items: flex-end;
  margin-bottom: 16px;
}

.section-kicker,
.overview-card header p {
  margin: 0 0 4px;
  color: var(--brief-green);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

h2,
h3,
p {
  margin-top: 0;
}

h2 {
  margin-bottom: 0;
  font-size: 23px;
  letter-spacing: -0.025em;
}

h3 {
  margin-bottom: 0;
}

.equation-status,
.state-chip,
.bank-state {
  display: inline-flex;
  min-height: 26px;
  align-items: center;
  padding: 3px 9px;
  border-radius: 999px;
  background: var(--brief-green-soft);
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 800;
}

.equation-status.error {
  background: var(--brief-red-soft);
  color: var(--brief-red);
}

.pending-bank {
  margin-bottom: 12px;
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--brief-amber) 35%, var(--brief-line));
  border-radius: 13px;
  background: var(--brief-amber-soft);
}

.pending-bank summary {
  align-items: center;
  color: var(--brief-amber);
  cursor: pointer;
}

.pending-bank summary > span:first-child {
  display: grid;
}

.pending-bank summary small {
  font-weight: 500;
}

.pending-bank ul,
.proof {
  display: grid;
  gap: 7px;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}

.pending-bank li {
  align-items: center;
  padding: 10px;
  border-radius: 10px;
  background: var(--brief-surface);
}

.pending-bank li > div {
  display: grid;
  min-width: 0;
}

.pending-bank li small {
  color: var(--brief-muted);
}

.pending-bank li b {
  white-space: nowrap;
}

.bank-state {
  margin-left: auto;
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
}

.overview-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: 12px;
}

.overview-card {
  min-width: 0;
  padding: 15px;
  border: 1px solid var(--brief-line);
  border-radius: 16px;
  background: var(--brief-soft);
}

.position-card {
  padding: 0;
}

.position-card:hover,
.position-card:focus-within,
.position-card[open] {
  border-color: color-mix(in srgb, var(--brief-green) 48%, var(--brief-line));
}

.position-summary {
  display: block;
  padding: 15px;
  border-radius: inherit;
  cursor: pointer;
  list-style: none;
}

.position-summary::-webkit-details-marker {
  display: none;
}

.overview-card header > strong {
  font-size: 14px;
  white-space: nowrap;
}

.flow {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  gap: 10px;
  align-items: center;
  margin: 11px 0 9px;
}

.flow > div {
  display: grid;
  gap: 4px;
  padding: 11px;
  border-radius: 12px;
  background: var(--brief-blue-soft);
  color: var(--brief-blue);
}

.flow > div.outflow {
  background: var(--brief-surface);
  color: var(--brief-text);
}

.flow span,
.summary-rows dt {
  color: var(--brief-muted);
  font-size: 11px;
}

.flow strong {
  font-size: 19px;
}

.summary-rows {
  margin: 0;
}

.summary-rows > div {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 9px 0;
  border-top: 1px solid var(--brief-line);
}

.summary-rows dd {
  margin: 0;
  font-size: 13px;
  font-weight: 800;
}

.subdued {
  opacity: 0.56;
}

.components {
  display: grid;
  gap: 10px;
  margin: 12px 0;
}

.component-row {
  display: grid;
  grid-template-columns: 86px minmax(60px, 1fr) auto;
  gap: 9px;
  align-items: center;
  font-size: 12px;
}

.track {
  height: 7px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--brief-surface);
}

.track > span {
  display: block;
  height: 100%;
  min-width: 1px;
  border-radius: inherit;
  background: var(--brief-green);
}

.track > span[data-index="1"] {
  background: var(--brief-gold);
}

.track > span[data-index="2"] {
  background: var(--brief-blue);
}

.track > span[data-index="3"] {
  background: var(--brief-muted);
}

.equation {
  margin: 0;
  padding: 10px 11px;
  border-radius: 10px;
  background: var(--brief-surface);
  color: var(--brief-muted);
  font-size: 11px;
  line-height: 1.55;
}

.proof {
  margin: 0 15px 15px;
  padding-top: 12px;
  border-top: 1px solid var(--brief-line);
}

.proof li {
  display: flex;
  justify-content: space-between;
  gap: 15px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--brief-line);
  font-size: 12px;
}

@media (max-width: 900px) {
  .overview-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 560px) {
  .financial-section {
    padding: 17px;
    border-radius: 17px;
  }

  .section-heading,
  .overview-card header,
  .pending-bank summary,
  .pending-bank li {
    align-items: flex-start;
    flex-direction: column;
  }

  .bank-state {
    margin-left: 0;
  }

  .component-row {
    grid-template-columns: 80px minmax(50px, 1fr);
  }

  .component-row strong {
    grid-column: 1 / -1;
  }

}
</style>
