<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import {
  fetchAssetsDashboard,
  type AssetItem,
  type AssetsDashboardResponse,
  type FixedAssetItem,
} from "../api/assets";
import { dashboardErrorMessage } from "../api/client";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { fen, formatFen, formatPositiveFen } from "../utils/money";

const filters = [
  { value: "all", label: "全部资产卡片" },
  { value: "active", label: "当前在用" },
  { value: "fixed", label: "固定资产" },
  { value: "intangible", label: "无形资产" },
  { value: "pending", label: "待启用固定资产" },
  { value: "exited", label: "已退出" },
] as const;

type AssetFilter = (typeof filters)[number]["value"];

interface DetailRow {
  label: string;
  value: string;
}

const route = useRoute();
const router = useRouter();
const { context, load: loadContext } = useDashboardContext();
const selectedPeriod = ref("");
const response = ref<AssetsDashboardResponse | null>(null);
const loading = ref(false);
const errorMessage = ref("");
const filter = ref<AssetFilter>("all");
let mounted = false;
let activeController: AbortController | null = null;

const periodOptions = computed(() => context.value?.periods ?? []);
const data = computed(() => response.value?.data ?? null);
const selectedPeriodView = computed(() => response.value?.selected_period ?? null);
const allItems = computed<AssetItem[]>(() => {
  if (!data.value) return [];
  return [...data.value.fixed.items, ...data.value.intangible.items];
});
const filteredItems = computed(() =>
  allItems.value.filter((item) => {
    if (filter.value === "active") return item.status === "active";
    if (filter.value === "fixed") return item.asset_type === "fixed";
    if (filter.value === "intangible") return item.asset_type === "intangible";
    if (filter.value === "pending") return item.status === "pending_activation";
    if (filter.value === "exited") return ["disposed", "retired"].includes(item.status);
    return true;
  }),
);
const filterLabel = computed(
  () => filters.find((item) => item.value === filter.value)?.label ?? "全部资产卡片",
);
const attentionItems = computed(() => {
  const assets = data.value;
  if (!assets) return [];
  const alerts: string[] = [];
  if (!assets.reconciled) {
    if (fen(assets.differences.cost_fen)) {
      alerts.push(
        `在用资产卡片原值与正式账簿相差 ${formatPositiveFen(assets.differences.cost_fen)}。`,
      );
    }
    if (fen(assets.differences.accumulated_fen)) {
      alerts.push(
        `卡片累计折旧摊销与正式账簿相差 ${formatPositiveFen(assets.differences.accumulated_fen)}。`,
      );
    }
    if (fen(assets.differences.net_fen)) {
      alerts.push(
        `资产卡片账面净值与正式账簿相差 ${formatPositiveFen(assets.differences.net_fen)}。`,
      );
    }
  }
  if (fen(assets.ledger_net_fen) < 0n) {
    alerts.push("期末长期资产账面净值为负数，请核对资产原值与累计折旧摊销。");
  }
  return alerts;
});

function routePeriod(): string | null {
  const value = route.query.period;
  return typeof value === "string" ? value : null;
}

async function synchronizePeriod(force = false) {
  try {
    const currentContext = await loadContext();
    if (!currentContext.periods.length) {
      activeController?.abort();
      selectedPeriod.value = "";
      response.value = null;
      errorMessage.value = "";
      loading.value = false;
      return;
    }
    const requested = routePeriod();
    const target = currentContext.periods.some((item) => item.key === requested)
      ? (requested as string)
      : (currentContext.default_period ?? currentContext.periods.at(-1)?.key ?? "");
    if (requested !== target) {
      await router.replace({ query: { ...route.query, period: target } });
      return;
    }
    if (force || selectedPeriod.value !== target || response.value === null) {
      await loadAssets(target);
    }
  } catch (error: unknown) {
    errorMessage.value = dashboardErrorMessage(error);
  }
}

async function loadAssets(period: string) {
  activeController?.abort();
  const controller = new AbortController();
  activeController = controller;
  selectedPeriod.value = period;
  response.value = null;
  errorMessage.value = "";
  loading.value = true;
  try {
    const result = await fetchAssetsDashboard(period, controller.signal);
    if (activeController === controller) response.value = result;
  } catch (error: unknown) {
    if (activeController === controller) errorMessage.value = dashboardErrorMessage(error);
  } finally {
    if (activeController === controller) {
      loading.value = false;
      activeController = null;
    }
  }
}

function changePeriod(value: string) {
  if (!value || value === routePeriod()) return;
  void router.push({ query: { ...route.query, period: value } });
}

async function refresh() {
  try {
    await loadContext(true);
    await synchronizePeriod(true);
  } catch (error: unknown) {
    errorMessage.value = dashboardErrorMessage(error);
  }
}

function isFixedAsset(item: AssetItem): item is FixedAssetItem {
  return item.asset_type === "fixed";
}

function assetTypeLabel(item: AssetItem) {
  return isFixedAsset(item) ? "固定资产" : "无形资产";
}

function availabilityLabel(item: AssetItem) {
  if (isFixedAsset(item)) {
    return item.in_service_date ? `开始使用 ${item.in_service_date}` : "尚未启用";
  }
  return `可供使用 ${item.available_for_use_date}`;
}

function chargeLabel(item: AssetItem, current = false) {
  if (isFixedAsset(item)) return current ? "本月折旧" : "累计折旧";
  return current ? "本月摊销" : "累计摊销";
}

function chargeProgress(item: AssetItem) {
  const cost = fen(item.cost_fen);
  const accumulated = fen(item.accumulated_charge_fen);
  if (cost <= 0n || accumulated <= 0n) return 0;
  const basisPoints = (accumulated * 10_000n) / cost;
  return Number(basisPoints > 10_000n ? 10_000n : basisPoints) / 100;
}

function exitInformation(item: AssetItem) {
  return isFixedAsset(item) ? item.disposal : item.retirement;
}

function chargeNote(item: AssetItem) {
  if (item.status === "pending_activation") return "等待确认达到可使用状态";
  const exit = exitInformation(item);
  if (exit) {
    return `退出日期 ${exit.date} · 退出前账面价值 ${formatFen(exit.book_value_fen)}`;
  }
  return item.latest_charge_period
    ? `最近计提期间 ${item.latest_charge_period}`
    : "尚无已确认折旧或摊销记录";
}

function assetDetails(item: AssetItem): DetailRow[] {
  const rows: DetailRow[] = [
    { label: "供应方", value: item.supplier || "未记录" },
    { label: "结算方式", value: item.settlement_label },
    { label: "购置凭证", value: item.acquisition_reference || "未展示" },
    { label: "付款或到期日", value: item.payment_date || item.due_date || "未设置" },
    { label: "购买价款", value: formatFen(item.purchase_price_fen) },
    { label: "不可抵扣税额", value: formatFen(item.noncreditable_tax_fen) },
    { label: "其他直接成本", value: formatFen(item.other_direct_cost_fen) },
    { label: "受益区域", value: item.benefit_area_label || "尚未确定" },
  ];
  if (isFixedAsset(item)) {
    rows.push(
      { label: "折旧方法", value: item.depreciation_method_label || "尚未启用" },
      {
        label: "使用年限",
        value: item.useful_life_months ? `${item.useful_life_months} 个月` : "尚未确定",
      },
      {
        label: "预计净残值",
        value:
          item.residual_value_fen === null
            ? "尚未确定"
            : formatFen(item.residual_value_fen),
      },
      { label: "折旧组", value: item.depreciation_group_code || "单卡计算" },
    );
    if (item.reimbursing_employee) {
      rows.push({ label: "垫付员工", value: item.reimbursing_employee });
    }
    if (item.disposal) {
      rows.push(
        {
          label: "退出方式",
          value: item.disposal.kind === "sale" ? "出售" : "报废",
        },
        { label: "退出凭证", value: item.disposal.reference || "未展示" },
        { label: "处置收入", value: formatFen(item.disposal.gross_proceeds_fen) },
        {
          label: "处置损益",
          value: fen(item.disposal.gain_fen)
            ? `收益 ${formatFen(item.disposal.gain_fen)}`
            : fen(item.disposal.loss_fen)
              ? `损失 ${formatFen(item.disposal.loss_fen)}`
              : formatFen(0),
        },
      );
    }
  } else {
    rows.push(
      { label: "摊销期限", value: `${item.useful_life_months} 个月` },
      { label: "期限依据", value: item.life_basis_label },
      { label: "权利内容", value: item.rights_description },
      { label: "期限说明", value: item.life_basis_explanation },
    );
    if (item.retirement) {
      rows.push({ label: "退役凭证", value: item.retirement.reference || "未展示" });
    }
  }
  return rows;
}

onMounted(() => {
  mounted = true;
  void synchronizePeriod();
});

watch(
  () => route.query.period,
  () => {
    if (mounted) void synchronizePeriod();
  },
);

onBeforeUnmount(() => {
  mounted = false;
  activeController?.abort();
});
</script>

<template>
  <section class="assets-page">
    <div class="assets-content">
      <DashboardModuleHeader
        eyebrow="资产"
        title="长期资产概览"
        description="按月查看固定资产、无形资产及其折旧摊销和退出情况。页面只读，不提供资产登记、折旧确认、处置或修改操作。"
        :options="periodOptions"
        :selected="selectedPeriod"
        :loading="loading"
        select-label="资产查看月份"
        @change="changePeriod"
        @refresh="refresh"
      />

      <section v-if="loading && !data" class="state-panel" aria-live="polite">
        <strong>正在读取资产账簿与卡片…</strong>
        <span>仅计算当前选择月份，不会加载其他页面的数据。</span>
      </section>

      <section v-else-if="errorMessage" class="state-panel error" role="alert">
        <strong>资产数据加载失败</strong>
        <span>{{ errorMessage }}</span>
        <button type="button" @click="refresh">重试</button>
      </section>

      <section v-else-if="!selectedPeriodView || !data" class="state-panel">
        <strong>还没有可查看的资产月份</strong>
        <span>生成首个会计期间后，这里会出现只读固定资产和无形资产信息。</span>
      </section>

      <template v-else>
        <section class="assets-hero" aria-labelledby="assets-total-label">
          <div>
            <p class="eyebrow">
              {{ selectedPeriodView.label }}期末 · 正式账簿与受控资产卡片
            </p>
            <span id="assets-total-label">期末长期资产账面净值</span>
            <strong class="assets-total">{{ formatFen(data.ledger_net_fen) }}</strong>
            <p class="hero-note">
              固定资产净值 {{ formatFen(data.fixed.active_net_fen) }} · 无形资产净值
              {{ formatFen(data.intangible.active_net_fen) }} · 共 {{ data.active_count }} 项在用
            </p>
          </div>
          <div class="reconciliation" :class="{ attention: !data.reconciled }">
            <span>资产卡片与正式账簿</span>
            <strong>{{ data.reconciliation_label }}</strong>
            <small v-if="data.reconciled">
              {{ data.active_count }} 项在用资产卡片可与账面原值、累计折旧摊销和净值勾稽
            </small>
            <small v-else>
              账面净值与卡片净值相差 {{ formatPositiveFen(data.differences.net_fen) }}
            </small>
          </div>
        </section>

        <section class="kpi-grid" aria-label="资产核心指标">
          <article class="kpi">
            <span>在用资产原值</span>
            <strong>{{ formatFen(data.ledger_cost_fen) }}</strong>
            <small>
              固定 {{ formatFen(data.fixed_asset_cost_fen) }} · 无形
              {{ formatFen(data.intangible_asset_cost_fen) }}
            </small>
          </article>
          <article class="kpi">
            <span>累计折旧与摊销</span>
            <strong>{{ formatFen(data.ledger_accumulated_fen) }}</strong>
            <small>
              折旧 {{ formatFen(data.accumulated_depreciation_fen) }} · 摊销
              {{ formatFen(data.accumulated_amortization_fen) }}
            </small>
          </article>
          <article class="kpi">
            <span>本月折旧与摊销</span>
            <strong>{{ formatFen(data.month_charge_fen) }}</strong>
            <small>
              固定资产折旧 {{ formatFen(data.fixed.month_depreciation_fen) }} · 无形资产摊销
              {{ formatFen(data.intangible.month_amortization_fen) }}
            </small>
          </article>
          <article class="kpi">
            <span>待启用固定资产</span>
            <strong>{{ formatFen(data.pending_fixed_cost_fen) }}</strong>
            <small v-if="data.pending_fixed_count">
              {{ data.pending_fixed_count }} 项尚未确认达到可使用状态
            </small>
            <small v-else>当前没有待启用固定资产卡片</small>
          </article>
        </section>

        <section v-if="attentionItems.length" class="panel attention-panel">
          <div class="section-heading">
            <div><p class="eyebrow">需要核对</p><h2>资产关注事项</h2></div>
            <span class="attention-count">{{ attentionItems.length }} 项</span>
          </div>
          <ul><li v-for="item in attentionItems" :key="item">{{ item }}</li></ul>
        </section>

        <section class="panel">
          <div class="section-heading">
            <div><p class="eyebrow">本月变化</p><h2>资产变动摘要</h2></div>
            <strong>本月计提 {{ formatFen(data.month_charge_fen) }}</strong>
          </div>
          <div class="movement-grid">
            <article>
              <span>本月新增</span><strong>{{ data.month_acquired_count }} 项</strong>
              <small>
                新增卡片原值 {{ formatFen(data.month_acquired_fen) }} · 本月启用固定资产
                {{ data.month_activated_count }} 项
              </small>
            </article>
            <article>
              <span>本月退出</span><strong>{{ data.month_exited_count }} 项</strong>
              <small>已出售、报废或退役的资产卡片</small>
            </article>
            <article>
              <span>当前在用</span><strong>{{ data.active_count }} 项</strong>
              <small>
                固定 {{ data.fixed.active_count }} 项 · 无形 {{ data.intangible.active_count }} 项
              </small>
            </article>
          </div>
        </section>

        <section class="panel">
          <div class="section-heading">
            <div><p class="eyebrow">逐项查看 · 账面口径</p><h2>资产明细</h2></div>
            <strong>{{ data.registered_count }} 项卡片</strong>
          </div>
          <div class="asset-toolbar">
            <p>{{ filterLabel }} · 显示 {{ filteredItems.length }} 项</p>
            <select v-model="filter" class="control" aria-label="筛选资产">
              <option v-for="item in filters" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
          </div>

          <div v-if="filteredItems.length" class="asset-grid">
            <article
              v-for="item in filteredItems"
              :key="`${item.asset_type}-${item.code}`"
              class="asset-card"
              :class="item.status"
            >
              <div class="asset-card-head">
                <div class="asset-name">
                  <span>{{ assetTypeLabel(item) }} · {{ item.code }}</span><h3>{{ item.name }}</h3>
                </div>
                <div class="book-value">
                  <span>期末卡片账面价值</span><strong>{{ formatFen(item.book_value_fen) }}</strong>
                </div>
              </div>
              <div class="asset-meta">
                <span>{{ item.category_label }}</span><span>{{ item.status_label }}</span>
                <span>取得日期 {{ item.acquisition_date }}</span>
                <span>{{ availabilityLabel(item) }}</span>
              </div>
              <div class="value-grid">
                <div><span>资产原值</span><strong>{{ formatFen(item.cost_fen) }}</strong></div>
                <div>
                  <span>{{ chargeLabel(item) }}</span>
                  <strong>{{ formatFen(item.accumulated_charge_fen) }}</strong>
                </div>
                <div>
                  <span>{{ chargeLabel(item, true) }}</span>
                  <strong>{{ formatFen(item.month_charge_fen) }}</strong>
                </div>
              </div>
              <div
                class="progress"
                role="progressbar"
                aria-label="累计折旧或摊销占资产原值比例"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-valuenow="chargeProgress(item)"
              >
                <span :style="{ width: `${chargeProgress(item)}%` }"></span>
              </div>
              <div class="status-row">
                <span class="asset-status" :class="item.status">{{ item.status_label }}</span>
                <span>{{ chargeNote(item) }}</span>
              </div>
              <details class="asset-detail">
                <summary>查看资产卡片信息</summary>
                <dl>
                  <div v-for="row in assetDetails(item)" :key="row.label">
                    <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
                  </div>
                </dl>
              </details>
            </article>
          </div>
          <div v-else class="empty-filter">当前筛选条件下没有资产卡片。</div>
        </section>

        <section class="panel note">
          账面净值来自正式账簿；逐项信息来自受控资产卡片。本页面不估算市场价值，也不推断尚未确认的折旧、摊销或处置。
        </section>
      </template>
    </div>
  </section>
</template>

<style scoped>
.assets-page { height: 100%; overflow-y: auto; }
.assets-content { width: min(calc(100% - 48px), 1280px); margin: 0 auto; padding: 40px 0 64px; }
.state-panel, .panel { border: 1px solid var(--line); border-radius: 15px; background: var(--surface); box-shadow: var(--shadow-soft); }
.state-panel { display: grid; gap: 7px; padding: 28px; }
.state-panel span, .state-panel button { color: var(--muted); }
.state-panel.error { border-color: #bc8147; }
.state-panel button { width: fit-content; margin-top: 8px; padding: 8px 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); cursor: pointer; }
.assets-hero { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(260px, .75fr); gap: 24px; padding: clamp(24px, 4vw, 38px); border-radius: 19px; background: linear-gradient(135deg, #1c6640, #287a4b); color: #fff; box-shadow: var(--shadow-soft); }
.eyebrow { margin: 0 0 6px; color: var(--accent); font-size: 12px; font-weight: 750; letter-spacing: .08em; }
.assets-hero .eyebrow { color: rgb(255 255 255 / 72%); }
.assets-total { display: block; margin: 8px 0 5px; font-size: clamp(36px, 5vw, 58px); line-height: 1.05; letter-spacing: -.045em; }
.hero-note { margin: 0; color: rgb(255 255 255 / 74%); }
.reconciliation { align-self: end; padding: 18px; border: 1px solid rgb(255 255 255 / 18%); border-radius: 13px; background: rgb(255 255 255 / 8%); }
.reconciliation.attention { border-color: #f2c07a; }
.reconciliation span, .reconciliation strong, .reconciliation small { display: block; }
.reconciliation span { margin-bottom: 5px; opacity: .72; font-size: 12px; }
.reconciliation strong { font-size: 20px; }
.reconciliation small { margin-top: 6px; opacity: .76; }
.kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
.kpi { min-height: 128px; padding: 18px; border: 1px solid var(--line); border-radius: 13px; background: var(--surface); }
.kpi span, .kpi small, .movement-grid span, .movement-grid small { color: var(--muted); }
.kpi span, .movement-grid span { display: block; font-size: 13px; }
.kpi strong { display: block; margin: 9px 0 5px; font-size: 24px; }
.panel { margin-top: 16px; padding: 22px; }
.attention-panel { border-color: #bc8147; }
.attention-panel ul { display: grid; gap: 8px; margin: 14px 0 0; padding-left: 20px; color: var(--muted); }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.section-heading h2, .asset-name h3 { margin: 0; }
.section-heading h2 { font-size: 21px; }
.attention-count { padding: 5px 9px; border-radius: 999px; background: #fff1dd; color: #89541e; font-size: 12px; font-weight: 700; }
.movement-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 16px; }
.movement-grid article { padding: 16px; border-radius: 12px; background: var(--background); }
.movement-grid strong { display: block; margin: 5px 0 3px; font-size: 22px; }
.asset-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin: 17px 0 14px; }
.asset-toolbar p { margin: 0; color: var(--muted); }
.control { min-height: 40px; padding: 0 12px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface); color: var(--text); }
.asset-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px; }
.asset-card { padding: 18px; border: 1px solid var(--line); border-radius: 13px; background: var(--background); }
.asset-card.pending_activation { border-style: dashed; border-color: var(--accent); }
.asset-card.disposed, .asset-card.retired { opacity: .82; }
.asset-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.asset-name { display: grid; min-width: 0; gap: 2px; }
.asset-name span, .book-value span { color: var(--muted); font-size: 12px; }
.asset-name h3 { overflow-wrap: anywhere; }
.book-value { display: grid; justify-items: end; gap: 2px; white-space: nowrap; }
.book-value strong { font-size: 21px; }
.asset-meta { display: flex; flex-wrap: wrap; gap: 6px 12px; margin: 11px 0; color: var(--muted); font-size: 12px; }
.value-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; overflow: hidden; padding: 1px; border-radius: 9px; background: var(--line); }
.value-grid div { display: grid; gap: 2px; padding: 10px; background: var(--surface); }
.value-grid span { color: var(--muted); font-size: 11px; }
.value-grid strong { font-size: 14px; }
.progress { height: 6px; overflow: hidden; margin-top: 11px; border-radius: 999px; background: var(--line); }
.progress span { display: block; height: 100%; border-radius: inherit; background: var(--accent); }
.status-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 9px; color: var(--muted); font-size: 12px; }
.asset-status { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.asset-status::before { width: 8px; height: 8px; flex: 0 0 auto; border-radius: 50%; background: var(--accent); content: ""; }
.asset-status.pending_activation::before { background: #4c83c3; }
.asset-status.disposed::before, .asset-status.retired::before { background: var(--muted); }
.asset-detail { margin-top: 10px; }
.asset-detail summary { color: var(--accent); cursor: pointer; font-size: 13px; font-weight: 700; }
.asset-detail dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px 14px; margin: 12px 0 0; }
.asset-detail dl div { display: grid; gap: 2px; }
.asset-detail dt { color: var(--muted); font-size: 11px; }
.asset-detail dd { margin: 0; overflow-wrap: anywhere; font-size: 13px; font-weight: 700; }
.empty-filter { padding: 28px; border-radius: 12px; background: var(--background); color: var(--muted); text-align: center; }
.note { color: var(--muted); font-size: 13px; }
@media (max-width: 980px) { .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .asset-grid { grid-template-columns: 1fr; } }
@media (max-width: 760px) { .assets-content { width: min(calc(100% - 28px), 1280px); padding-top: 28px; } .assets-hero, .kpi-grid, .movement-grid, .value-grid { grid-template-columns: 1fr; } .asset-toolbar, .asset-card-head, .status-row { align-items: flex-start; flex-direction: column; } .book-value { justify-items: start; } .asset-detail dl { grid-template-columns: 1fr; } }
</style>
