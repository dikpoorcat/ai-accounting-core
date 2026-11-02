<script setup lang="ts">
import { computed, ref } from "vue";
import type { FundsData, InvestmentFilter } from "../api/funds";
import { cashFlowClass, fen, formatFen } from "../utils/money";
import DashboardPagination from "./DashboardPagination.vue";
import DashboardSelect from "./DashboardSelect.vue";

type InvestmentCollection = NonNullable<FundsData["collections"]["investment_products"]>;
type InvestmentProduct = InvestmentCollection["items"][number];
const props = defineProps<{
  summary: FundsData["investments"];
  collection?: InvestmentCollection;
  filter: InvestmentFilter;
  loading: boolean;
  pageState: { loading: boolean; error: string };
  active: boolean;
  scope: string;
}>();
const emit = defineEmits<{ filter: [value: InvestmentFilter]; more: []; retry: []; pause: [scope: string] }>();
const displayMode = ref<"cards" | "list">("cards");
const products = computed(() => props.collection?.items ?? []);
const showPageCounts = computed(() => products.value.length > 0 || !props.pageState.loading && !props.pageState.error);
const filterOptions = [
  { value: "all", label: "全部" },
  { value: "financial", label: "金融投资" },
  { value: "cooperation", label: "合作项目" },
];
const filterLabel = computed(() => filterOptions.find(option => option.value === props.filter)?.label ?? "全部");

function selectFilter(value: string) {
  if (value === "all" || value === "financial" || value === "cooperation") emit("filter", value);
}
function amountLabel(item: InvestmentProduct) {
  return item.product_type === "cooperation" ? "月末合作权益" : "月末账面金额";
}
function holdingAmount(item: InvestmentProduct) {
  if (item.activity_only) return "—";
  return formatFen(item.product_type === "cooperation" ? item.cooperation_equity_fen : item.closing_cost_fen);
}
function incomeTone(value: string | undefined) {
  return fen(value) > 0n ? "investment-gain" : fen(value) < 0n ? "investment-loss" : "";
}
function metrics(item: InvestmentProduct) {
  if (item.product_type === "cooperation") return [
    { key: "held", label: "代管资金", amount: item.cooperation_held_fen, note: "", tone: "" },
    { key: "operator", label: "经营者待补", amount: item.cooperation_operator_due_fen, note: "", tone: fen(item.cooperation_operator_due_fen) ? "investment-attention" : "" },
    { key: "company", label: "公司待补", amount: item.cooperation_company_due_fen, note: "", tone: fen(item.cooperation_company_due_fen) ? "investment-attention" : "" },
  ];
  return [
    { key: "income", label: "本月确认收益", amount: item.recognized_income_fen, note: "未扣投资增值税", tone: incomeTone(item.recognized_income_fen) },
    { key: "receipts", label: "本月实际到账", amount: item.actual_receipts_fen, note: "含退出本金等实际收款", tone: cashFlowClass(item.actual_receipts_fen, "inflow") },
    { key: "pending", label: "月末待收款", amount: item.pending_receipts_fen,
      note: fen(item.accrued_income_fen) ? `其中待收收益 ${formatFen(item.accrued_income_fen)}` : "", tone: "" },
  ];
}
</script>

<template>
  <section id="fund-investments" class="panel section-panel section-anchor fund-investments" tabindex="-1" aria-labelledby="fund-investments-title">
    <div class="dashboard-record-heading">
      <div>
        <h2 id="fund-investments-title">投资与理财</h2>
        <p class="list-caption">金融投资账面金额 <strong>{{ formatFen(summary.closing_cost_fen) }}</strong><template v-if="collection"> · 共 {{ collection.page.total_count }} 项<template v-if="filter !== 'all'"> · {{ filterLabel }}<template v-if="showPageCounts"> {{ collection.page.filtered_count }} 项</template></template><template v-if="showPageCounts"> · 已加载 {{ products.length }} 项</template></template></p>
      </div>
      <div class="dashboard-record-toolbar">
        <DashboardSelect v-if="filter !== 'all' || collection && collection.page.total_count > 1" id="investment-filter-menu" label="筛选投资" :model-value="filter"
          :options="filterOptions" :scope="scope" :disabled="loading" @change="selectFilter" />
        <div class="dashboard-display-switch" role="group" aria-label="投资展示方式">
          <button type="button" :aria-pressed="displayMode === 'cards'" @click="displayMode = 'cards'">卡片</button>
          <button type="button" :aria-pressed="displayMode === 'list'" @click="displayMode = 'list'">列表</button>
        </div>
      </div>
    </div>

    <p v-if="(loading || pageState.loading) && !products.length" class="investment-empty" role="status">正在读取{{ filterLabel === '全部' ? '投资与理财' : filterLabel }}…</p>
    <div v-if="products.length" class="dashboard-record-results" :class="{ 'is-list': displayMode === 'list' }">
      <div class="dashboard-record-grid investment-grid" :class="{ 'is-list': displayMode === 'list' }">
        <div v-if="displayMode === 'list'" class="dashboard-record-list-header" aria-hidden="true"><span>产品／项目</span><span class="investment-amount-heading">月末账面金额／合作权益</span><span>收益与资金情况</span></div>
        <article v-for="item in products" :key="item.investment_id || item.fund_id" class="investment-card dashboard-record-card" data-section-focus tabindex="-1">
          <div v-if="displayMode === 'cards'" class="dashboard-record-body">
            <div class="dashboard-record-topline"><span class="dashboard-record-classification">{{ item.category_label }}<template v-if="item.holding_label"> · {{ item.holding_label }}</template></span></div>
            <div class="dashboard-record-head">
              <div class="dashboard-record-name"><h3>{{ item.name }}</h3><p v-if="item.quantity_label" class="investment-quantity">{{ item.quantity_label }}</p></div>
              <div class="dashboard-record-value investment-value" :class="{ unknown: item.activity_only || item.closing_cost_fen === null && item.product_type !== 'cooperation' }"><span>{{ amountLabel(item) }}</span><strong>{{ holdingAmount(item) }}</strong></div>
            </div>
            <div class="dashboard-record-metrics">
              <div v-for="metric in metrics(item)" :key="metric.key"><span>{{ metric.label }}</span><strong :class="metric.tone">{{ formatFen(metric.amount) }}</strong><small v-if="metric.note">{{ metric.note }}</small></div>
            </div>
          </div>
          <div v-else class="dashboard-record-list-row investment-list-row">
            <div class="dashboard-record-list-name"><h3>{{ item.name }}</h3><p>{{ item.category_label }}<template v-if="item.holding_label"> · {{ item.holding_label }}</template></p><p v-if="item.quantity_label">{{ item.quantity_label }}</p></div>
            <strong class="dashboard-record-list-value is-primary" :data-label="amountLabel(item)">{{ holdingAmount(item) }}</strong>
            <dl class="investment-list-metrics">
              <div v-for="metric in metrics(item)" :key="metric.key"><dt>{{ metric.label }}</dt><dd :class="metric.tone">{{ formatFen(metric.amount) }}<small v-if="metric.note">{{ metric.note }}</small></dd></div>
            </dl>
          </div>
          <p v-if="item.activity_only || item.product_type !== 'cooperation' && (fen(item.actual_payments_fen) !== 0n || fen(item.actual_loss_fen) !== 0n || item.market_value_fen !== undefined && item.market_as_of)" class="investment-record-notes" :class="{ 'is-list': displayMode === 'list' }">
            <span v-if="item.activity_only">收益及收款记录，未列持仓金额</span>
            <template v-if="item.product_type !== 'cooperation'">
              <span v-if="fen(item.actual_payments_fen) !== 0n">本月实际付款 <b :class="cashFlowClass(item.actual_payments_fen, 'outflow')">{{ formatFen(item.actual_payments_fen) }}</b></span>
              <span v-if="fen(item.actual_loss_fen) !== 0n" class="investment-loss">本月实际投资损失 {{ formatFen(item.actual_loss_fen) }}</span>
              <span v-if="item.market_value_fen !== undefined && item.market_as_of">参考估值 {{ formatFen(item.market_value_fen) }} · {{ item.market_as_of }}，不替代账面金额</span>
            </template>
          </p>
        </article>
      </div>
    </div>
    <p v-else-if="!loading && !pageState.loading && !pageState.error" class="investment-empty">{{ filter === 'all' ? '本月没有可展示的投资与理财记录。' : `本月没有${filterLabel}记录，可切换筛选查看。` }}</p>
    <p class="investment-scope-note">投资账面金额不代表市值，未计入上方账户资金；合作权益与代管资金不计入金融投资账面合计。</p>
    <DashboardPagination automatic compact item-label="项投资" :active="active && !loading" :scope="scope" :page="showPageCounts ? collection?.page : undefined" :loaded="products.length"
      :loading="pageState.loading" :error="pageState.error" @more="emit('more')" @retry="emit('retry')" @pause="emit('pause', $event)" />
  </section>
</template>

<style scoped>
.fund-investments { min-width: 0; }
.investment-grid { --dashboard-record-list-columns: minmax(0, 1.1fr) minmax(0, .85fr) minmax(0, 2fr); }
.investment-quantity { margin: 0; color: var(--muted); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.investment-value { min-width: 0; max-width: 55%; white-space: normal; overflow-wrap: anywhere; }
.investment-gain { color: var(--accent); }
.investment-loss, .cash-outflow { color: var(--danger); }
.cash-inflow { color: var(--cash-in); }
.investment-attention { color: var(--warning); }
.investment-record-notes { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: -4px 18px 16px; color: var(--muted); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.investment-record-notes.is-list { margin: 0 4px 12px; }
.investment-record-notes b { font-weight: 500; }
.investment-scope-note { margin: 12px 0 0; color: var(--muted); font-size: 11px; line-height: 1.5; }
.investment-amount-heading { text-align: right; }
.investment-list-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 0 0 0 14px; }
.investment-list-metrics > div { display: grid; min-width: 0; align-content: start; gap: 3px; }
.investment-list-metrics dt, .investment-list-metrics small { color: var(--muted); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.investment-list-metrics small { display: block; margin-top: 3px; font-weight: 400; }
.investment-list-metrics dd { margin: 0; font-size: 13px; font-weight: 500; line-height: 1.5; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.investment-empty { padding: 24px; border-radius: var(--radius-control); background: var(--surface-soft); color: var(--muted); text-align: center; }
@container dashboard-record-list (max-width: 1020px) {
  .investment-list-row { grid-template-columns: minmax(0, 1fr); }
  .investment-list-metrics { grid-column: 1 / -1; margin: 8px 0 0; }
}
@media (max-width: 720px) {
  .investment-value { max-width: none; }
  .investment-record-notes { margin-inline: 16px; }
  .investment-record-notes.is-list { margin-inline: 4px; }
  .investment-list-metrics { grid-template-columns: minmax(0, 1fr); gap: 10px; }
}
</style>
