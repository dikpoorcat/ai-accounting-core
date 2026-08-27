<script setup lang="ts">
import type { BriefCash, BriefData, BriefPosition } from "../../api/brief";
import { fen, formatFen, formatPositiveFen } from "../../utils/money";

const props = defineProps<{
  cash: BriefCash;
  position: BriefPosition;
  unmatched: BriefData["unmatched_bank_activity"];
}>();

const components = [
  ["银行存款", "bank_fen"],
  ["固定资产净值", "fixed_asset_net_fen"],
  ["无形资产净值", "intangible_asset_net_fen"],
  ["其他资产", "other_assets_fen"],
] as const;

function componentRatio(value: string) {
  const total = fen(props.position.assets_fen);
  if (total <= 0n) return 0;
  return Math.max(0, Math.min(100, Number((fen(value) * 10_000n) / total) / 100));
}

function bankStateLabel(state: string) {
  return (
    {
      matched: "已匹配",
      unmatched: "待识别",
      invalid_match: "原匹配已失效",
      pending_late: "迟到流水待处理",
      handled_late: "迟到流水已处理",
    }[state] || "待处理"
  );
}

function formatDate(value: string) {
  const [, month, day] = value.slice(0, 10).split("-");
  return `${Number(month)} 月 ${Number(day)} 日`;
}
</script>

<template>
  <section class="financial-section" aria-labelledby="financial-title">
    <div class="section-heading">
      <div>
        <p class="section-kicker">资金与资产负债</p>
        <h2 id="financial-title">本月资金与财务位置</h2>
      </div>
      <span :class="['equation-status', { error: !position.equation_valid }]">
        {{ position.equation_valid ? "资产负债表平衡" : "资产负债表不平衡" }}
      </span>
    </div>

    <details v-if="unmatched.count" class="pending-bank">
      <summary>
        <span>
          <strong>{{ unmatched.count }} 笔资金动向待识别或处理</strong>
          <small>尚不能当作已确认业务</small>
        </span>
        <span>
          流入 {{ formatFen(unmatched.inflow_fen) }} · 流出 {{ formatFen(unmatched.outflow_fen) }}
        </span>
      </summary>
      <ul>
        <li v-for="item in unmatched.rows" :key="`${item.date}-${item.memo}`">
          <div>
            <small>{{ formatDate(item.date) }} · {{ item.party }}</small>
            <strong>{{ item.memo }}</strong>
          </div>
          <span class="bank-state">{{ bankStateLabel(item.state) }}</span>
          <b>{{ item.direction === "inflow" ? "+" : "−" }}{{ formatFen(item.amount_fen) }}</b>
        </li>
      </ul>
    </details>

    <div class="overview-grid">
      <article class="overview-card cash-card">
        <header>
          <div>
            <p>来自银行流水</p>
            <h3>资金概览</h3>
          </div>
          <span class="state-chip">
            {{ fen(cash.net_fen) > 0n ? "净流入" : fen(cash.net_fen) < 0n ? "净流出" : "无净变动" }}
          </span>
        </header>
        <div class="flow">
          <div>
            <span>本月流入</span>
            <strong>{{ formatFen(cash.inflow_fen) }}</strong>
          </div>
          <span aria-hidden="true">→</span>
          <div class="outflow">
            <span>本月流出</span>
            <strong>{{ formatFen(cash.outflow_fen) }}</strong>
          </div>
        </div>
        <dl class="summary-rows">
          <div>
            <dt>流入 − 流出</dt>
            <dd>
              {{
                fen(cash.net_fen) > 0n
                  ? `净流入 ${formatFen(cash.net_fen)}`
                  : fen(cash.net_fen) < 0n
                    ? `净流出 ${formatPositiveFen(cash.net_fen)}`
                    : `无净变动 ${formatFen(0)}`
              }}
            </dd>
          </div>
          <div :class="{ subdued: !cash.ordinary_count }">
            <dt>当前有效匹配</dt>
            <dd>{{ cash.ordinary_count ? `${cash.matched_count} / ${cash.ordinary_count} 笔` : "本月无普通流水" }}</dd>
          </div>
        </dl>
      </article>

      <article id="position-overview" class="overview-card position-card" tabindex="-1">
        <header>
          <div>
            <p>公司目前有什么、欠什么</p>
            <h3>财务位置</h3>
          </div>
          <strong>资产 {{ formatFen(position.assets_fen) }}</strong>
        </header>
        <div class="components">
          <div
            v-for="([label, key], index) in components"
            :key="key"
            :class="['component-row', { subdued: !fen(position[key]) }]"
          >
            <span>{{ label }}</span>
            <div class="track">
              <span :style="{ width: `${componentRatio(position[key])}%` }" :data-index="index" />
            </div>
            <strong>{{ formatFen(position[key]) }}</strong>
          </div>
        </div>
        <p class="equation">
          资产 {{ formatFen(position.assets_fen) }} = 负债 {{ formatFen(position.liabilities_fen) }} + 所有者权益
          {{ formatFen(position.capital_fen) }} {{ fen(position.cumulative_result_fen) < 0n ? "−" : "+" }} 累计差额
          {{ formatPositiveFen(position.cumulative_result_fen) }}
        </p>
        <details class="proof">
          <summary>查看长期资产原值与累计折旧、摊销</summary>
          <ul>
            <li><span>固定资产原值</span><strong>{{ formatFen(position.fixed_asset_cost_fen) }}</strong></li>
            <li><span>减：累计折旧</span><strong>{{ formatFen(position.accumulated_depreciation_fen) }}</strong></li>
            <li><span>固定资产净值</span><strong>{{ formatFen(position.fixed_asset_net_fen) }}</strong></li>
            <li><span>无形资产原值</span><strong>{{ formatFen(position.intangible_asset_cost_fen) }}</strong></li>
            <li><span>减：累计摊销</span><strong>{{ formatFen(position.accumulated_amortization_fen) }}</strong></li>
            <li><span>无形资产净值</span><strong>{{ formatFen(position.intangible_asset_net_fen) }}</strong></li>
          </ul>
        </details>
      </article>
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
.proof ul {
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
  gap: 12px;
}

.overview-card {
  min-width: 0;
  padding: 15px;
  border: 1px solid var(--brief-line);
  border-radius: 16px;
  background: var(--brief-soft);
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
  margin-top: 10px;
}

.proof summary {
  color: var(--brief-green);
  font-size: 12px;
  font-weight: 750;
  cursor: pointer;
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

  .proof summary {
    display: flex;
    min-height: 44px;
    align-items: center;
  }
}
</style>
