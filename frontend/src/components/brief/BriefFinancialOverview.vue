<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import type { DashboardBriefContract } from "../../api/generated/dashboardBrief";
import { cashFlowClass, fen, formatFen, formatPositiveFen } from "../../utils/money";

const props = defineProps<{
  funds: DashboardBriefContract.BriefData["funds_overview"];
  position: DashboardBriefContract.BriefFinancialPosition;
}>();
const route = useRoute();

const components: Array<[string, "bank_fen" | "fixed_asset_net_fen" | "intangible_asset_net_fen" | "other_assets_fen"]> = [
  ["银行存款", "bank_fen"],
  ["固定资产净值", "fixed_asset_net_fen"],
  ["无形资产净值", "intangible_asset_net_fen"],
  ["其他资产", "other_assets_fen"],
];

function magnitude(value: string | null) {
  if (value === null) return 0n;
  const amount = fen(value);
  return amount < 0n ? -amount : amount;
}

const positionScale = computed(() => {
  let positiveMax = 0n;
  for (const [, key] of components) {
    const amount = magnitude(props.position[key]);
    if (amount > positiveMax) positiveMax = amount;
  }
  const negativeMax = magnitude(props.position.liabilities_fen);
  const total = positiveMax + negativeMax;
  return {
    axis: total ? Number((negativeMax * 10_000n) / total) / 100 : 50,
    total,
  };
});

const equityTotal = computed(() => props.position.equity_fen);

function positionBarWidth(value: string | null) {
  if (value === null) return null;
  const total = positionScale.value.total;
  if (!total) return 0;
  return Math.min(100, Number((magnitude(value) * 10_000n) / total) / 100);
}

function hasAssetBreakdown(...values: Array<string | null>) {
  return values.every((value) => value !== null);
}

function hasBankCalculation() {
  const calculation = props.position.bank_calculation;
  if (
    props.position.bank_fen === null
    || !calculation
    || calculation.opening_fen === null
    || calculation.inflow_fen === null
    || calculation.outflow_fen === null
  ) return false;
  return (
    fen(calculation.opening_fen) + fen(calculation.inflow_fen) - fen(calculation.outflow_fen)
    === fen(props.position.bank_fen)
  );
}

function hasLiabilityCalculation() {
  const calculation = props.position.liability_calculation;
  if (
    !calculation
    || calculation.current_fen === null
    || calculation.non_current_fen === null
    || props.position.liabilities_fen === null
  ) return false;
  return (
    fen(calculation.current_fen) + fen(calculation.non_current_fen)
    === fen(props.position.liabilities_fen)
  );
}

function hasOtherAssetsCalculation() {
  return (
    props.position.other_assets_fen !== null
    && props.funds.cash_fen !== null
    && props.funds.payment_platform_fen !== null
  );
}

function remainingOtherAssets() {
  if (
    props.position.other_assets_fen === null
    || props.funds.cash_fen === null
    || props.funds.payment_platform_fen === null
  ) return null;
  return fen(props.position.other_assets_fen) - fen(props.funds.cash_fen) - fen(props.funds.payment_platform_fen);
}

</script>

<template>
  <section class="financial-section" aria-labelledby="financial-title">
    <div class="section-heading">
      <div>
        <h2 id="financial-title">资金与资产负债</h2>
      </div>
      <span v-if="position.equation_valid !== true" :class="['equation-status', { error: position.equation_valid === false }]">
        {{ position.equation_valid === null ? '资产负债金额尚不能完整确认' : '资产负债金额需要核对' }}
      </span>
    </div>

    <div class="overview-grid">
      <article class="overview-card cash-card selectable-card" data-section-focus tabindex="-1">
        <div class="cash-body">
          <header>
            <div>
              <p>银行、现金及公司支付平台 · 已扣除账户互转</p>
              <h3>本月公司收付款</h3>
            </div>
            <div class="cash-actions">
              <span class="state-chip">
                {{ funds.net_change_fen === null ? "资金变动尚不能确认" : fen(funds.net_change_fen) > 0n ? "资金增加" : fen(funds.net_change_fen) < 0n ? "资金减少" : "资金无净变动" }}
              </span>
              <RouterLink class="bank-details-link" :to="{ name: 'funds', query: route.query, hash: '#bank-details' }">查看流水明细</RouterLink>
            </div>
          </header>
          <div class="flow">
            <div>
              <span>对外收款</span>
              <strong :class="cashFlowClass(funds.inflow_fen, 'inflow')">{{ formatFen(funds.inflow_fen) }}</strong>
            </div>
            <div>
              <span>对外付款</span>
              <strong :class="cashFlowClass(funds.outflow_fen, 'outflow')">{{ formatFen(funds.outflow_fen) }}</strong>
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
        </div>
      </article>

      <article id="position-overview" class="overview-card position-card selectable-card" data-section-focus tabindex="-1">
        <div class="position-body">
          <header>
            <div>
              <p>所选月末的资产与负债</p>
              <h3>月末资产与负债</h3>
            </div>
            <span class="balance-insight">
              <button type="button" class="balance-trigger" aria-describedby="balance-tooltip">
                资产 {{ position.assets_fen === null ? '无法完整建立' : formatFen(position.assets_fen) }}
              </button>
              <span id="balance-tooltip" class="balance-tooltip" role="tooltip">
                <strong class="balance-tooltip-title">平衡关系</strong>
                <span v-if="position.equation_valid !== null" class="equation">
                  <span class="equation-line">
                    <span class="equation-term asset-term">
                      <span>资产</span>
                      <strong>{{ formatFen(position.assets_fen) }}</strong>
                    </span>
                    <span class="equation-operator">=</span>
                    <span class="equation-term liability-term">
                      <span>负债</span>
                      <strong>{{ formatFen(position.liabilities_fen) }}</strong>
                    </span>
                    <span class="equation-operator">+</span>
                    <span class="equation-term equity-term">
                      <span>所有者权益</span>
                      <strong>{{ formatFen(equityTotal) }}</strong>
                    </span>
                  </span>
                  <span class="equation-line">
                    <span class="equation-term equity-term">
                      <span>所有者权益</span>
                      <strong>{{ formatFen(equityTotal) }}</strong>
                    </span>
                    <span class="equation-operator">=</span>
                    <span class="equation-term">
                      <span>资本及公积</span>
                      <strong>{{ formatFen(position.capital_fen) }}</strong>
                    </span>
                    <span class="equation-operator">{{ fen(position.cumulative_result_fen) < 0n ? "−" : "+" }}</span>
                    <span :class="['equation-term', fen(position.cumulative_result_fen) < 0n ? 'liability-term' : 'asset-term']">
                      <span>{{ position.cumulative_result_fen === null ? "累计盈亏" : fen(position.cumulative_result_fen) < 0n ? "未弥补亏损" : "未分配利润" }}</span>
                      <strong>{{ formatPositiveFen(position.cumulative_result_fen) }}</strong>
                    </span>
                  </span>
                </span>
                <span v-else class="equation-unavailable">
                  资产与负债金额尚不能完整确认，AI 会计核对中。
                </span>
                <span v-if="position.issues?.length" class="position-issues">
                  <span v-for="(issue, index) in position.issues" :key="index">
                    {{ issue.message }}<template v-if="issue.amount_fen != null"> · {{ formatFen(issue.amount_fen) }}</template>
                  </span>
                </span>
              </span>
            </span>
          </header>
          <div class="components" :style="{ '--position-axis': `${positionScale.axis}%` }">
            <div
              v-for="([label, key], index) in components"
              :key="key"
              :class="['component-row', { subdued: position[key] !== null && !fen(position[key]) }]"
            >
              <span>{{ label }}</span>
              <div v-if="positionBarWidth(position[key]) !== null" class="track">
                <span :style="{ width: `${positionBarWidth(position[key])}%` }" :data-index="index" />
              </div>
              <span class="component-value">
                <button
                  v-if="key === 'bank_fen'"
                  type="button"
                  class="component-value-trigger"
                  aria-describedby="bank-asset-tooltip"
                >
                  {{ formatFen(position[key]) }}
                </button>
                <button
                  v-else-if="key === 'fixed_asset_net_fen'"
                  type="button"
                  class="component-value-trigger"
                  aria-describedby="fixed-asset-tooltip"
                >
                  {{ formatFen(position[key]) }}
                </button>
                <button
                  v-else-if="key === 'intangible_asset_net_fen'"
                  type="button"
                  class="component-value-trigger"
                  aria-describedby="intangible-asset-tooltip"
                >
                  {{ formatFen(position[key]) }}
                </button>
                <button
                  v-else-if="key === 'other_assets_fen'"
                  type="button"
                  class="component-value-trigger"
                  aria-describedby="other-assets-tooltip"
                >
                  {{ formatFen(position[key]) }}
                </button>
                <strong v-else>{{ formatFen(position[key]) }}</strong>
                <span
                  v-if="key === 'bank_fen'"
                  id="bank-asset-tooltip"
                  class="component-tooltip"
                  role="tooltip"
                >
                  <strong class="balance-tooltip-title">银行存款</strong>
                  <span v-if="hasBankCalculation()" class="component-calculation">
                    <span class="equation-term">
                      <span>期初余额</span>
                      <strong>{{ formatFen(position.bank_calculation?.opening_fen) }}</strong>
                    </span>
                    <span class="equation-operator">+</span>
                    <span class="equation-term">
                      <span>本月流入</span>
                      <strong :class="cashFlowClass(position.bank_calculation?.inflow_fen, 'inflow')">{{ formatFen(position.bank_calculation?.inflow_fen) }}</strong>
                    </span>
                    <span class="equation-operator">−</span>
                    <span class="equation-term">
                      <span>本月流出</span>
                      <strong :class="cashFlowClass(position.bank_calculation?.outflow_fen, 'outflow')">{{ formatFen(position.bank_calculation?.outflow_fen) }}</strong>
                    </span>
                    <span class="calculation-total">
                      <span class="equation-operator">=</span>
                      <span class="equation-term asset-term">
                        <span>期末余额</span>
                        <strong>{{ formatFen(position.bank_fen) }}</strong>
                      </span>
                    </span>
                  </span>
                  <span v-else class="equation-unavailable">
                    期初及本月收支构成暂不能完整建立，当前期末余额为
                    <strong>{{ formatFen(position.bank_fen) }}</strong>。
                  </span>
                </span>
                <span
                  v-if="key === 'fixed_asset_net_fen'"
                  id="fixed-asset-tooltip"
                  class="component-tooltip"
                  role="tooltip"
                >
                  <strong class="balance-tooltip-title">固定资产净值</strong>
                  <span v-if="hasAssetBreakdown(position.fixed_asset_cost_fen, position.accumulated_depreciation_fen, position.fixed_asset_net_fen)" class="component-calculation">
                    <span class="equation-term">
                      <span>原值</span>
                      <strong>{{ formatFen(position.fixed_asset_cost_fen) }}</strong>
                    </span>
                    <span class="equation-operator">−</span>
                    <span class="equation-term liability-term">
                      <span>累计折旧</span>
                      <strong>{{ formatFen(position.accumulated_depreciation_fen) }}</strong>
                    </span>
                    <span class="calculation-total">
                      <span class="equation-operator">=</span>
                      <span class="equation-term asset-term">
                        <span>净值</span>
                        <strong>{{ formatFen(position.fixed_asset_net_fen) }}</strong>
                      </span>
                    </span>
                  </span>
                  <span v-else class="equation-unavailable">原值、累计折旧与净值暂不能完整建立。</span>
                </span>
                <span
                  v-if="key === 'intangible_asset_net_fen'"
                  id="intangible-asset-tooltip"
                  class="component-tooltip"
                  role="tooltip"
                >
                  <strong class="balance-tooltip-title">无形资产净值</strong>
                  <span v-if="hasAssetBreakdown(position.intangible_asset_cost_fen, position.accumulated_amortization_fen, position.intangible_asset_net_fen)" class="component-calculation">
                    <span class="equation-term">
                      <span>原值</span>
                      <strong>{{ formatFen(position.intangible_asset_cost_fen) }}</strong>
                    </span>
                    <span class="equation-operator">−</span>
                    <span class="equation-term liability-term">
                      <span>累计摊销</span>
                      <strong>{{ formatFen(position.accumulated_amortization_fen) }}</strong>
                    </span>
                    <span class="calculation-total">
                      <span class="equation-operator">=</span>
                      <span class="equation-term asset-term">
                        <span>净值</span>
                        <strong>{{ formatFen(position.intangible_asset_net_fen) }}</strong>
                      </span>
                    </span>
                  </span>
                  <span v-else class="equation-unavailable">原值、累计摊销与净值暂不能完整建立。</span>
                </span>
                <span
                  v-if="key === 'other_assets_fen'"
                  id="other-assets-tooltip"
                  class="component-tooltip"
                  role="tooltip"
                >
                  <strong class="balance-tooltip-title">其他资产</strong>
                  <span v-if="hasOtherAssetsCalculation()" class="component-calculation">
                    <span class="equation-term asset-term">
                      <span>库存现金</span>
                      <strong>{{ formatFen(funds.cash_fen) }}</strong>
                    </span>
                    <span class="equation-operator">+</span>
                    <span class="equation-term asset-term">
                      <span>支付平台</span>
                      <strong>{{ formatFen(funds.payment_platform_fen) }}</strong>
                    </span>
                    <span class="equation-operator">+</span>
                    <span class="equation-term asset-term">
                      <span>其余资产</span>
                      <strong>{{ formatFen(remainingOtherAssets()) }}</strong>
                    </span>
                    <span class="calculation-total">
                      <span class="equation-operator">=</span>
                      <span class="equation-term asset-term">
                        <span>其他资产</span>
                        <strong>{{ formatFen(position.other_assets_fen) }}</strong>
                      </span>
                    </span>
                  </span>
                  <span v-else class="equation-unavailable">库存现金、支付平台与其余资产的构成暂不能完整建立。</span>
                </span>
              </span>
            </div>
            <div :class="['component-row', 'liability-row', { subdued: position.liabilities_fen !== null && !fen(position.liabilities_fen) }]">
              <span>负债</span>
              <div v-if="positionBarWidth(position.liabilities_fen) !== null" class="track liability-track">
                <span :style="{ width: `${positionBarWidth(position.liabilities_fen)}%` }" />
              </div>
              <span class="component-value liability-value">
                <button type="button" class="component-value-trigger" aria-describedby="liability-tooltip">
                  {{ formatFen(position.liabilities_fen) }}
                </button>
                <span id="liability-tooltip" class="component-tooltip" role="tooltip">
                  <strong class="balance-tooltip-title">负债</strong>
                  <span v-if="hasLiabilityCalculation()" class="component-calculation">
                    <span class="equation-term liability-term">
                      <span>流动负债</span>
                      <strong>{{ formatFen(position.liability_calculation?.current_fen) }}</strong>
                    </span>
                    <span class="equation-operator">+</span>
                    <span class="equation-term liability-term">
                      <span>非流动负债</span>
                      <strong>{{ formatFen(position.liability_calculation?.non_current_fen) }}</strong>
                    </span>
                    <span class="calculation-total">
                      <span class="equation-operator">=</span>
                      <span class="equation-term liability-term">
                        <span>负债合计</span>
                        <strong>{{ formatFen(position.liabilities_fen) }}</strong>
                      </span>
                    </span>
                  </span>
                  <span v-else class="equation-unavailable">流动负债与非流动负债的构成暂不能完整建立。</span>
                </span>
              </span>
            </div>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.financial-section {
  min-width: 0;
  padding: 0;
}

.financial-section .cash-inflow { color: var(--brief-green); }
.financial-section .cash-outflow { color: var(--brief-amber); }

.section-heading,
.overview-card header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.section-heading {
  align-items: flex-end;
  margin-bottom: 14px;
  padding: 0 2px;
}

.overview-card header p {
  margin: 0 0 4px;
  color: var(--brief-muted);
  font-size: 12px;
  font-weight: 500;
}

h2,
h3,
p {
  margin-top: 0;
}

h2 {
  margin-bottom: 0;
  font-size: 22px;
  letter-spacing: -0.025em;
}

h3 {
  margin-bottom: 0;
}

.equation-status,
.state-chip {
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

.overview-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: stretch;
  gap: 12px;
}

.overview-card {
  min-width: 0;
  padding: 15px;
  border: 1px solid var(--brief-line);
  border-radius: var(--brief-panel-radius, 14px);
  background: var(--brief-surface);
}

.cash-card,
.position-card {
  display: flex;
  flex-direction: column;
  padding: 0;
}

.position-card {
  position: relative;
}

.cash-body,
.position-body {
  display: block;
  padding: 15px;
  border-radius: inherit;
}

.position-body {
  display: flex;
  flex: 1;
  flex-direction: column;
}

.cash-actions {
  display: flex;
  flex: none;
  align-items: center;
  gap: 5px;
}

.bank-details-link {
  display: inline-flex;
  min-height: 26px;
  align-items: center;
  padding: 0 7px;
  border-radius: 999px;
  color: var(--brief-green);
  font-size: 11px;
  font-weight: 700;
  text-decoration: none;
  white-space: nowrap;
}

.bank-details-link:hover,
.bank-details-link:focus-visible {
  background: var(--brief-green-soft);
  outline: none;
}

.overview-card header > strong {
  font-size: 14px;
  white-space: nowrap;
}

.flow {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  align-items: stretch;
  margin: 13px 0 9px;
  border-radius: var(--brief-control-radius, 9px);
  background: var(--brief-metric-surface);
}

.flow > div {
  display: grid;
  min-width: 0;
  gap: 4px;
  padding: 12px;
  color: var(--brief-text);
}

.flow span,
.summary-rows dt {
  color: var(--brief-muted);
  font-size: 11px;
}

.flow strong {
  font-variant-numeric: tabular-nums;
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
}

.summary-rows dd {
  margin: 0;
  font-size: 13px;
  font-weight: 800;
}

.component-row.subdued > span:first-child,
.component-row.subdued > .track,
.component-row.subdued > .component-value > .component-value-trigger,
.component-row.subdued > .component-value > strong {
  opacity: 0.56;
}

.component-row.subdued > .component-value > .component-value-trigger:hover,
.component-row.subdued > .component-value > .component-value-trigger:focus-visible {
  opacity: 1;
}

.components {
  --component-gap: 9px;
  --component-label-width: 86px;
  --component-value-width: 104px;

  display: grid;
  gap: 9px;
  margin: 13px 0 0;
  padding: 2px 0;
}

.component-row {
  display: grid;
  min-height: 17px;
  grid-template-columns: var(--component-label-width) minmax(60px, 1fr) var(--component-value-width);
  gap: var(--component-gap);
  align-items: center;
  font-size: 12px;
}

.component-row > span {
  color: var(--brief-muted);
}

.component-row > strong {
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
}

.track {
  position: relative;
  height: 8px;
}

.track > span {
  position: absolute;
  top: 0;
  bottom: 0;
  left: var(--position-axis);
  min-width: 1px;
  border-radius: 0 999px 999px 0;
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

.liability-track > span {
  right: calc(100% - var(--position-axis));
  left: auto;
  border-radius: 999px 0 0 999px;
  background: var(--brief-red);
}

.liability-row > span,
.liability-row > strong {
  color: var(--brief-red);
}

.liability-value .component-value-trigger,
.liability-value > strong {
  color: var(--brief-red);
}

.liability-value .component-value-trigger:hover,
.liability-value .component-value-trigger:focus-visible {
  background: color-mix(in srgb, var(--brief-red) 8%, transparent);
  color: var(--brief-red);
}

.balance-insight {
  position: relative;
  display: inline-flex;
  flex: none;
}

.balance-trigger {
  margin: -3px -6px;
  padding: 3px 6px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--brief-text);
  font: inherit;
  font-size: 14px;
  font-weight: 800;
  white-space: nowrap;
  cursor: help;
}

.balance-trigger:hover,
.balance-trigger:focus-visible {
  background: var(--brief-green-soft);
  color: var(--brief-green);
  outline: none;
}

.balance-tooltip {
  position: absolute;
  top: calc(100% + 9px);
  right: 0;
  z-index: 30;
  display: grid;
  width: min(480px, calc(100vw - 64px));
  gap: 12px;
  padding: 16px;
  border: 1px solid color-mix(in srgb, var(--brief-green) 20%, var(--brief-line));
  border-radius: 12px;
  background: var(--brief-surface);
  box-shadow: var(--brief-overlay-shadow, var(--shadow-overlay));
  opacity: 0;
  pointer-events: none;
  text-align: left;
  transform: translateY(-5px);
  transition: opacity 140ms ease, transform 140ms ease, visibility 140ms ease;
  visibility: hidden;
}

.balance-tooltip::after {
  position: absolute;
  top: -6px;
  right: 24px;
  width: 10px;
  height: 10px;
  border-top: 1px solid color-mix(in srgb, var(--brief-green) 20%, var(--brief-line));
  border-left: 1px solid color-mix(in srgb, var(--brief-green) 20%, var(--brief-line));
  background: var(--brief-surface);
  content: "";
  transform: rotate(45deg);
}

.balance-insight:hover .balance-tooltip,
.balance-insight:focus-within .balance-tooltip {
  opacity: 1;
  transform: translateY(0);
  visibility: visible;
}

.component-value {
  position: relative;
  display: flex;
  grid-column: 3;
  min-width: 0;
  justify-content: flex-end;
  font-variant-numeric: tabular-nums;
}

.component-value > strong,
.component-value-trigger {
  color: var(--brief-text);
  font: inherit;
  font-weight: 700;
  white-space: nowrap;
}

.component-value-trigger {
  margin: -2px -5px;
  padding: 2px 5px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  cursor: help;
}

.component-value-trigger:hover,
.component-value-trigger:focus-visible {
  background: var(--brief-green-soft);
  color: var(--brief-green);
  outline: none;
}

.component-tooltip {
  position: absolute;
  top: calc(100% + 7px);
  right: 0;
  z-index: 28;
  display: grid;
  width: min(360px, calc(100vw - 64px));
  gap: 12px;
  padding: 16px;
  border: 1px solid color-mix(in srgb, var(--brief-green) 18%, var(--brief-line));
  border-radius: 12px;
  background: var(--brief-surface);
  box-shadow: var(--brief-overlay-shadow, var(--shadow-overlay));
  opacity: 0;
  color: var(--brief-text);
  pointer-events: none;
  text-align: left;
  transform: translateY(-4px);
  transition: opacity 120ms ease, transform 120ms ease, visibility 120ms ease;
  visibility: hidden;
}

.component-calculation,
.calculation-total {
  display: grid;
  grid-template-columns: 14px minmax(0, 1fr);
  align-items: center;
  gap: 6px;
}

.component-calculation .equation-term {
  grid-column: 2;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  text-align: left;
}

.component-calculation .equation-term > strong {
  max-width: 145px;
  text-align: right;
}

.component-calculation .equation-operator {
  grid-column: 1;
}

.calculation-total {
  grid-column: 1 / -1;
  margin-top: 4px;
  padding-top: 10px;
  border-top: 1px solid var(--brief-line);
}

.component-value:hover .component-tooltip,
.component-value:focus-within .component-tooltip {
  opacity: 1;
  transform: translateY(0);
  visibility: visible;
}

.equation {
  display: grid;
  gap: 14px;
  margin: 0;
}

.balance-tooltip-title {
  color: var(--brief-text);
  font-size: 14px;
  font-weight: 700;
}

.equation-line {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 14px minmax(0, 1fr) 14px minmax(0, 1fr);
  align-items: center;
  gap: 5px;
}

.equation-line + .equation-line {
  padding-top: 14px;
  border-top: 1px solid var(--brief-line);
}

.equation-term {
  display: grid;
  min-width: 0;
  gap: 5px;
  padding: 9px 5px;
  border-radius: 8px;
  background: var(--brief-soft);
  color: var(--brief-text);
  text-align: center;
}

.equation-term > span {
  font-size: 12px;
  font-weight: 500;
}

.equation-term > strong {
  font-size: 14px;
  font-weight: 750;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.asset-term {
  background: color-mix(in srgb, var(--brief-green-soft) 65%, var(--brief-surface));
  color: var(--brief-green);
}

.liability-term {
  background: color-mix(in srgb, var(--brief-red-soft) 65%, var(--brief-surface));
  color: var(--brief-red);
}

.equity-term {
  background: color-mix(in srgb, var(--brief-blue-soft) 65%, var(--brief-surface));
  color: var(--brief-blue);
}

.equation-operator {
  color: var(--brief-muted);
  font-size: 16px;
  text-align: center;
}

.equation-unavailable {
  color: var(--brief-muted);
  font-size: 13px;
  line-height: 1.65;
}

.position-issues {
  display: grid;
  gap: 6px;
  margin: 0;
  padding: 9px 11px;
  border-radius: 10px;
  background: var(--brief-amber-soft);
  color: var(--brief-amber);
  font-size: 12px;
  line-height: 1.6;
}

@media (max-width: 900px) {
  .overview-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 560px) {
  .section-heading,
  .overview-card header {
    align-items: flex-start;
    flex-direction: column;
  }

  .component-row {
    grid-template-columns: 72px minmax(0, 1fr) minmax(0, 110px);
    gap: 7px;
  }

  .component-row > strong,
  .component-value > strong {
    grid-column: auto;
    font-size: 11px;
  }

  .components {
    --component-gap: 7px;
    --component-label-width: 72px;
    --component-value-width: 110px;
  }

  .equation-line {
    grid-template-columns: 14px minmax(0, 1fr);
    gap: 6px;
  }

  .equation-term {
    grid-column: 2;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    text-align: left;
  }

  .equation-term > strong {
    max-width: 145px;
    text-align: right;
  }

  .equation-operator {
    grid-column: 1;
  }

  .balance-tooltip {
    right: auto;
    left: 0;
  }

  .balance-tooltip::after {
    right: auto;
    left: 24px;
  }

}
@media (max-width: 560px) {
  .cash-actions { flex-wrap: wrap; min-width: 0; }
  .balance-trigger, .component-value-trigger, .component-value > strong, .summary-rows dd, .flow strong { white-space: normal; overflow-wrap: anywhere; }
  .component-tooltip { width: min(360px, calc(100vw - 72px)); }
}
</style>
