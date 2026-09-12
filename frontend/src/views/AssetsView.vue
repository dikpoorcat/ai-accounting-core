<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import {
  fetchAssetsDashboard,
  type AssetItem,
  type EstablishedAssetItem,
  type AssetsDashboardResponse,
  type FixedAssetItem,
  type AssetsQuery,
} from "../api/assets";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import PeriodPreparation from "../components/PeriodPreparation.vue";
import DashboardSourceHistory from "../components/DashboardSourceHistory.vue";
import DashboardBusinessRecords from "../components/DashboardBusinessRecords.vue";
import BusinessStatusDetails from "../components/BusinessStatusDetails.vue";
import VoucherTrace from "../components/brief/VoucherTrace.vue";
import { localBusinessName } from "../api/localKernel";
import { useDashboardContext } from "../composables/useDashboardContext";
import { useDashboardSections } from "../composables/useDashboardSections";
import { fen, formatFen, formatPositiveFen } from "../utils/money";

const filters = [
  { value: "all", label: "全部资产卡片" },
  { value: "active", label: "当前在用" },
  { value: "fixed", label: "固定资产" },
  { value: "intangible", label: "无形资产" },
  { value: "pending", label: "待启用资产" },
  { value: "exited", label: "已退出" },
] as const;

type AssetFilter = (typeof filters)[number]["value"];

interface DetailRow {
  label: string;
  value: string;
}

const route = useRoute();
const router = useRouter();
const { context, load: loadContext, refresh: refreshContext } = useDashboardContext();
const selectedPeriod = ref("");
const response = ref<AssetsDashboardResponse | null>(null);
const loading = ref(false);
const errorMessage = ref("");
const filter = computed<AssetFilter>({
  get: () => filters.find(item => item.value === route.query.asset_filter)?.value ?? "all",
  set: value => { void router.push({ query: { ...route.query, asset_filter: value === "all" ? undefined : value } }); },
});
let mounted = false;
let activeController: AbortController | null = null;
let requestGeneration = 0;
const pageControllers = new Map<string, AbortController>();
const pageLoading = ref<Record<string, boolean>>({});
const pageErrors = ref<Record<string, string>>({});
const updateNotice = ref("");
function clearPageRequests() {
  pageControllers.forEach(request => request.abort());
  pageControllers.clear(); pageLoading.value = {}; pageErrors.value = {};
}

const periodOptions = computed(() => context.value?.periods ?? []);
const data = computed(() => response.value?.data ?? null);
const countQualifier = computed(() => data.value?.unestablished_count ? "已确认 " : "");
const selectedPeriodView = computed(() => response.value?.selected_period ?? null);
const allItems = computed<AssetItem[]>(() => {
  if (!data.value) return [];
  return data.value.collections.assets.items;
});
const filteredItems = computed(() => allItems.value);
const filterLabel = computed(
  () => filters.find((item) => item.value === filter.value)?.label ?? "全部资产卡片",
);
const attentionItems = computed(() => {
  const assets = data.value;
  if (!assets) return [];
  const alerts: string[] = [];
  if (assets.reconciled === null) alerts.push("部分资产来源尚不能确认，账面价值与明细暂无法完整核对。请查看资产卡片中的候选依据及下方具体问题。");
  if (!assets.reconciled) {
    if (assets.differences.cost_fen !== null && fen(assets.differences.cost_fen)) {
      alerts.push(
        `资产及项目明细的成本与账面记录相差 ${formatPositiveFen(assets.differences.cost_fen)}。`,
      );
    }
    if (assets.differences.accumulated_fen !== null && fen(assets.differences.accumulated_fen)) {
      alerts.push(
        `资产明细的累计折旧摊销与账面记录相差 ${formatPositiveFen(assets.differences.accumulated_fen)}。`,
      );
    }
    if (assets.differences.net_fen !== null && fen(assets.differences.net_fen)) {
      alerts.push(
        `资产及项目明细的账面价值与账面记录相差 ${formatPositiveFen(assets.differences.net_fen)}。`,
      );
    }
  }
  if (assets.ledger_net_fen !== null && fen(assets.ledger_net_fen) < 0n) {
    alerts.push("期末长期资产账面净值为负数，请核对资产原值与累计折旧摊销。");
  }
  return alerts;
});
const sectionLinks = computed(() => data.value && selectedPeriodView.value ? [
  { id: "assets-overview", label: "概览" },
  { id: "assets-checks", label: "核对事项" },
  { id: "asset-movements-title", label: "本月变动" },
  { id: "asset-list-title", label: "资产卡片" },
  { id: "asset-projects-title", label: "项目投入" },
] : []);
const { activeSection, focusSection, positionSection } = useDashboardSections(sectionLinks, "assets-overview");

function routePeriod(): string | null {
  const value = route.query.period;
  return typeof value === "string" ? value : null;
}

async function synchronizePeriod(force = false) {
  const generation = requestGeneration, selection = selectionKey();
  try {
    const currentContext = await loadContext();
    if (!isCurrent(generation, selection)) return;
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
    if (isCurrent(generation, selection)) errorMessage.value = dashboardErrorMessage(error);
  }
}

async function loadAssets(period: string) {
  const generation = ++requestGeneration, selection = selectionKey();
  activeController?.abort();
  clearPageRequests();
  const controller = new AbortController();
  activeController = controller;
  selectedPeriod.value = period;
  response.value = null;
  errorMessage.value = "";
  loading.value = true;
  try {
    const result = await fetchAssetsDashboard(period, controller.signal, { asset_filter: filter.value });
    if (isCurrent(generation, selection) && activeController === controller) { response.value = result; updateNotice.value = ""; }
    await nextTick();
    if (isCurrent(generation, selection) && activeController === controller && route.hash === "#assets-attention-title") {
      const heading = document.getElementById(route.hash.slice(1));
      if (heading) { positionSection(heading); heading.focus({ preventScroll: true }); }
    }
  } catch (error: unknown) {
    if (isCurrent(generation, selection) && activeController === controller) errorMessage.value = dashboardErrorMessage(error);
  } finally {
    if (isCurrent(generation, selection) && activeController === controller) {
      loading.value = false;
      activeController = null;
    }
  }
}

function changePeriod(value: string) {
  if (!value || value === routePeriod()) return;
  void router.push({ query: { company_id: route.query.company_id, period: value } });
}

async function refresh() {
  invalidateRequests();
  const generation = requestGeneration, selection = selectionKey();
  try {
    await refreshContext();
    if (isCurrent(generation, selection)) await synchronizePeriod(true);
  } catch (error: unknown) {
    if (isCurrent(generation, selection)) errorMessage.value = dashboardErrorMessage(error);
  }
}

function selectionKey() { return JSON.stringify([route.query.company_id, route.query.period, filter.value]); }
function isCurrent(generation: number, selection: string) { return mounted && generation === requestGeneration && selection === selectionKey(); }
function invalidateRequests() {
  requestGeneration += 1;
  activeController?.abort(); activeController = null;
  clearPageRequests();
  response.value = null; loading.value = false;
}

async function loadMore(section: AssetsQuery["section"] = "assets") {
  section = section ?? "assets";
  const current = response.value;
  const page = current?.data?.collections[section ?? "assets"]?.page;
  if (!current?.data || !page?.has_more || !page.next_cursor || pageLoading.value[section]) return;
  const generation = requestGeneration, selection = selectionKey();
  const request = new AbortController(); pageControllers.set(section, request); pageLoading.value[section] = true; pageErrors.value[section] = "";
  try {
    const next = await fetchAssetsDashboard(selectedPeriod.value, request.signal, { section, asset_filter: filter.value, cursor: page.next_cursor, expected_version: current.snapshot_version });
    if (!isCurrent(generation, selection) || pageControllers.get(section) !== request || response.value?.snapshot_version !== current.snapshot_version || !next.data) return;
    if (next.snapshot_version !== current.snapshot_version) { updateNotice.value = "资料已更新，正在重新读取。"; await refresh(); return; }
    const latest = response.value;
    if (!latest.data) return;
    response.value = { ...latest, data: { ...latest.data,
      ...(section === "assets" ? { fixed: { ...latest.data.fixed, items: [...latest.data.fixed.items, ...next.data.fixed.items] }, intangible: { ...latest.data.intangible, items: [...latest.data.intangible.items, ...next.data.intangible.items] } } : {}),
      ...(section === "projects" ? { projects: [...latest.data.projects, ...next.data.projects] } : {}),
      collections: { ...latest.data.collections, [section!]: { ...next.data.collections[section!], items: [...latest.data.collections[section!].items, ...next.data.collections[section!].items] } },
    } };
  } catch (caught) {
    if (!isCurrent(generation, selection) || pageControllers.get(section) !== request) return;
    if (isDashboardSnapshotChanged(caught)) { updateNotice.value = "资料已更新，正在重新读取。"; await refresh(); }
    else pageErrors.value[section] = dashboardErrorMessage(caught);
  } finally { if (isCurrent(generation, selection) && pageControllers.get(section) === request) { pageLoading.value[section] = false; pageControllers.delete(section); } }
}

function isFixedAsset(item: EstablishedAssetItem): item is FixedAssetItem {
  return item.asset_type === "fixed";
}

function assetTypeLabel(item: EstablishedAssetItem) {
  return isFixedAsset(item) ? "固定资产" : "无形资产";
}

function availabilityLabel(item: EstablishedAssetItem) {
  if (item.status === "pending_activation") return "";
  if (isFixedAsset(item)) {
    return item.in_service_date ? `开始使用 ${dateLabel(item.in_service_date)}` : "";
  }
  return item.available_for_use_date ? `可供使用 ${dateLabel(item.available_for_use_date)}` : "";
}

function dateLabel(value: string) {
  return value.length === 7 ? `${value}（按月确认）` : value;
}

function settlementTitle(item: EstablishedAssetItem) {
  return ({
    "本资产结算": "本项资产付款",
    "本验收批次结算": "整批付款",
    "成本来源结算（不分摊为本资产付款）": "项目来源付款",
  } as Record<string, string>)[item.settlement_scope] ?? item.settlement_scope;
}

function obligationLabel(name: string) {
  return ({ net: "应付个人款项", tax: "应缴个税" } as Record<string, string>)[name] ?? "应付金额";
}

function chargeLabel(item: EstablishedAssetItem, current = false) {
  if (isFixedAsset(item)) return current ? "本月折旧" : "累计折旧";
  return current ? "本月摊销" : "累计摊销";
}

function chargeProgress(item: EstablishedAssetItem) {
  if (item.cost_fen === null || item.accumulated_charge_fen === null) return null;
  const cost = fen(item.cost_fen);
  const accumulated = fen(item.accumulated_charge_fen);
  if (cost <= 0n || accumulated <= 0n) return 0;
  const basisPoints = (accumulated * 10_000n) / cost;
  return Number(basisPoints > 10_000n ? 10_000n : basisPoints) / 100;
}

function pendingCost() {
  const current = data.value;
  return !current || current.pending_fixed_cost_fen === null || current.pending_intangible_cost_fen === null ? null : fen(current.pending_fixed_cost_fen) + fen(current.pending_intangible_cost_fen);
}

function exitInformation(item: EstablishedAssetItem) {
  return isFixedAsset(item) ? item.disposal : item.retirement;
}

function chargeNote(item: EstablishedAssetItem) {
  if (item.status === "pending_activation") return "当前状态为待启用。";
  const exit = exitInformation(item);
  if (exit) {
    return `退出日期 ${dateLabel(exit.date)} · 退出前账面价值 ${formatFen(exit.book_value_fen)}`;
  }
  return item.latest_charge_period
    ? `最近记录折旧或摊销的月份：${item.latest_charge_period}`
    : "暂无折旧或摊销记录";
}

function assetDetails(item: EstablishedAssetItem): DetailRow[] {
  const rows: DetailRow[] = [
    { label: "取得来源", value: item.source_label },
  ];
  if (item.acquisition_reference) rows.push({ label: "购置凭证", value: item.acquisition_reference });
  if (item.source_parties) rows.push({ label: item.source_party_label, value: item.source_parties });
  if (isFixedAsset(item)) {
    if (item.disposal) {
      rows.push(
        {
          label: "退出方式",
          value: item.disposal.kind === "sale" ? "出售" : "报废",
        },
        { label: "退出凭证", value: item.disposal.reference || "未展示" },
        { label: item.disposal.kind === "sale" ? "出售应收金额" : "报废回收金额", value: formatFen(item.disposal.gross_proceeds_fen) },
        {
          label: "处置损益",
          value: item.disposal.gain_fen === null || item.disposal.loss_fen === null ? "尚不能完整确认" : fen(item.disposal.gain_fen)
            ? `收益 ${formatFen(item.disposal.gain_fen)}`
            : fen(item.disposal.loss_fen)
              ? `损失 ${formatFen(item.disposal.loss_fen)}`
              : formatFen(0),
        },
      );
    }
  } else {
    if (item.rights_description && item.rights_description !== "未提供") rows.push({ label: "权利内容", value: item.rights_description });
    if (item.retirement) {
      rows.push({ label: "退役凭证", value: item.retirement.reference || "未展示" });
    }
  }
  return rows;
}

function accountingDetails(item: EstablishedAssetItem): DetailRow[] {
  const pending = item.status === "pending_activation";
  const rows: DetailRow[] = [];
  if (item.benefit_area_label || !pending) rows.push({ label: "费用归属", value: item.benefit_area_label || "未提供" });
  if (item.useful_life_months !== null || !pending) rows.push({ label: isFixedAsset(item) ? "折旧期限" : "摊销期限", value: item.useful_life_months === null ? "未提供" : `${item.useful_life_months} 个月` });
  if (isFixedAsset(item)) {
    if (item.depreciation_method_label || !pending) rows.push({ label: "折旧方法", value: item.depreciation_method_label || "未提供" });
    if (item.residual_value_fen !== null || !pending) rows.push({ label: "预计净残值", value: formatFen(item.residual_value_fen) });
    if (item.rounding_policy_label) rows.push({ label: "整分处理", value: item.rounding_policy_label });
  } else {
    if (item.life_basis_label && item.life_basis_label !== "未提供") rows.push({ label: "期限依据", value: item.life_basis_label });
    if (item.life_basis_explanation && item.life_basis_explanation !== "未提供") rows.push({ label: "期限说明", value: item.life_basis_explanation });
  }
  return rows;
}

onMounted(() => {
  mounted = true;
  void synchronizePeriod();
});

watch(
  () => [route.query.company_id, route.query.period, filter.value],
  (value, previous) => {
    if (value.every((item, index) => item === previous[index])) return;
    invalidateRequests();
  },
  { flush: "sync" },
);
watch(
  () => [context.value?.current_company?.company_id, route.query.period, filter.value] as const,
  ([orgId], [previousOrgId]) => {
    if (mounted && orgId && orgId === route.query.company_id) void synchronizePeriod(orgId !== previousOrgId);
  },
);

onBeforeUnmount(() => {
  mounted = false;
  invalidateRequests();
});
</script>

<template>
  <section class="assets-page">
    <div class="assets-content">
      <DashboardModuleHeader
        title="长期资产概览"
        :options="periodOptions"
        :selected="selectedPeriod"
        :loading="loading"
        select-label="资产查看月份"
        @change="changePeriod"
        @refresh="refresh"
      />
      <DashboardSectionNav v-if="sectionLinks.length" :items="sectionLinks" :active="activeSection" label="资产内容导航" floating @select="focusSection" />

      <p v-if="updateNotice" class="note" role="status">{{ updateNotice }}</p>
      <section v-if="loading && !data" class="state-panel" aria-live="polite">
        <strong>正在加载资产数据…</strong>
        <span>正在读取所选月份的资产明细。</span>
      </section>

      <section v-else-if="errorMessage" class="state-panel error" role="alert">
        <strong>资产数据加载失败</strong>
        <span>{{ errorMessage }}</span>
        <button type="button" @click="refresh">重试</button>
      </section>

      <section v-else-if="!selectedPeriodView || !data" class="state-panel">
        <strong>还没有可查看的资产月份</strong>
        <span>开始记账后，可在这里按月查看资产信息。</span>
      </section>

      <template v-else>
        <section id="assets-overview" class="assets-hero" tabindex="-1" aria-labelledby="assets-total-label">
          <div>
            <p class="eyebrow">
              {{ selectedPeriodView.label }}期末 · 全公司
            </p>
            <span id="assets-total-label">期末长期资产账面价值</span>
            <strong class="assets-total">{{ formatFen(data.ledger_net_fen) }}</strong>
            <p class="hero-note">
              在用固定资产净值 {{ formatFen(data.fixed.active_net_fen) }} · 在用无形资产净值
              {{ formatFen(data.intangible.active_net_fen) }} · {{ countQualifier || '共 ' }}{{ data.active_count }} 项在用
            </p>
            <p v-if="pendingCost() === null || pendingCost() || data.project_cost_fen === null || fen(data.project_cost_fen)" class="hero-note">
              另含待启用资产 {{ formatFen(pendingCost()) }}
              <span v-if="data.project_cost_fen === null || fen(data.project_cost_fen)"> · 尚未计入资产卡片的项目投入 {{ formatFen(data.project_cost_fen) }}</span>
            </p>
          </div>
          <div class="reconciliation" :class="{ attention: !data.reconciled }">
            <span>资产明细与账面记录</span>
            <strong>{{ data.reconciled === null ? "尚不能完整核对" : data.reconciled ? "核对一致" : "存在差异" }}</strong>
            <small v-if="!data.reconciled">
              <a href="#assets-attention-title">查看核对说明</a> · <a href="#asset-list-title">查看卡片与候选依据</a>
            </small>
            <details class="reconciliation-details">
              <summary>查看核对说明</summary>
              <p>范围包括在用、待启用资产及尚未计入资产卡片的项目投入。</p>
              <p>账面成本 {{ formatFen(data.ledger_cost_fen) }} · 明细成本 {{ formatFen(data.card_cost_fen) }}</p>
              <p>账面累计折旧摊销 {{ formatFen(data.ledger_accumulated_fen) }} · 明细累计折旧摊销 {{ formatFen(data.card_accumulated_fen) }}</p>
              <p>账面价值 {{ formatFen(data.ledger_net_fen) }} · 明细价值 {{ formatFen(data.card_net_fen) }}</p>
            </details>
          </div>
        </section>

        <section class="kpi-grid" aria-label="资产核心指标">
          <article class="kpi">
            <span>资产及项目账面成本</span>
            <strong>{{ formatFen(data.ledger_cost_fen) }}</strong>
            <small>
              扣除累计折旧摊销前的金额
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
            <span>待启用资产</span>
            <strong>{{ formatFen(pendingCost()) }}</strong>
            <small>固定 {{ countQualifier }}{{ data.pending_fixed_count }} 项 · 无形 {{ countQualifier }}{{ data.pending_intangible_count }} 项</small>
          </article>
        </section>

        <p v-if="data.unestablished_count" class="note" role="status">全公司有 {{ data.unestablished_count }} 项资产来源的冻结采用尚未建立；在用、待启用及本月变动数量仅列已确认部分，不代表完整数量。</p>

        <section id="assets-checks" class="assets-checks" tabindex="-1" aria-label="资产核对事项">
          <PeriodPreparation :preparation="data.period_preparation" :snapshot-version="response?.snapshot_version" @changed="refresh" />

          <section v-if="attentionItems.length" class="panel attention-panel" aria-labelledby="assets-attention-title">
            <div class="section-heading">
              <div><h2 id="assets-attention-title" tabindex="-1">资产关注事项</h2></div>
              <span class="attention-count">{{ attentionItems.length }} 条提示</span>
            </div>
            <ul><li v-for="item in attentionItems" :key="item">{{ item }}</li></ul>
          </section>
        </section>

        <section class="panel">
          <div class="section-heading">
            <div><h2 id="asset-movements-title" tabindex="-1">资产变动摘要</h2></div>
          </div>
          <div class="movement-grid">
            <article>
              <span>本月新增</span><strong>{{ countQualifier }}{{ data.month_acquired_count }} 项</strong>
              <small>
                新增卡片原值 {{ formatFen(data.month_acquired_fen) }} · 本月启用资产
                {{ countQualifier }}{{ data.month_activated_count }} 项
              </small>
            </article>
            <article v-if="data.month_cost_adjustment_fen === null || fen(data.month_cost_adjustment_fen) !== 0n">
              <span>以前取得资产的成本调整</span><strong>{{ formatFen(data.month_cost_adjustment_fen) }}</strong>
              <small>本月对原资产成本的调整，不计为新增资产</small>
            </article>
            <article>
              <span>本月退出</span><strong>{{ countQualifier }}{{ data.month_exited_count }} 项</strong>
              <small>已出售、报废或退役的资产卡片</small>
            </article>
            <article>
              <span>当前在用</span><strong>{{ countQualifier }}{{ data.active_count }} 项</strong>
              <small>
                固定 {{ countQualifier }}{{ data.fixed.active_count }} 项 · 无形 {{ countQualifier }}{{ data.intangible.active_count }} 项
              </small>
            </article>
          </div>
        </section>

        <section class="panel">
          <div class="section-heading">
            <div><h2 id="asset-list-title" tabindex="-1">资产明细</h2></div>
            <strong>已识别 {{ data.registered_count }} 项卡片身份</strong>
          </div>
          <div class="asset-toolbar">
            <p>{{ filterLabel }} · 当前筛选共 {{ data.collections.assets.page.filtered_count }} 项，已加载 {{ filteredItems.length }} 项</p>
            <p v-if="data.unestablished_count && ['active', 'pending', 'exited'].includes(filter)">状态筛选仅列已确认匹配项；全公司尚未确认的资产来源仍需单独核对。</p>
            <select v-model="filter" class="control" aria-label="筛选资产">
              <option v-for="item in filters" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
          </div>

          <p v-if="data.unestablished_count">完整范围内 {{ data.unestablished_count }} 项资产来源的冻结采用未建立，相关金额保持未知。</p>
          <div v-if="filteredItems.length" class="asset-grid">
            <template v-for="item in filteredItems" :key="item.asset_id">
            <article v-if="item.selection_status === 'unestablished'" class="asset-card">
              <DashboardBusinessRecords :items="[item]" :period="selectedPeriod" :snapshot-version="response!.snapshot_version" :show-business="false" />
            </article>
            <details v-else
              class="asset-card"
              :class="item.status"
            >
              <summary class="asset-card-summary">
                <div class="asset-card-head">
                  <div class="asset-name">
                    <span>{{ assetTypeLabel(item) }} · {{ item.code }}</span><h3>{{ item.name }}</h3>
                  </div>
                  <div class="book-value">
                    <span>期末账面价值</span><strong>{{ formatFen(item.book_value_fen) }}</strong>
                  </div>
                </div>
                <div class="asset-meta">
                  <span>{{ item.category_label }}</span>
                  <span>取得 {{ item.recognition_label }}</span>
                  <span v-if="availabilityLabel(item)">{{ availabilityLabel(item) }}</span>
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
                <div v-if="chargeProgress(item) !== null"
                  class="progress"
                  role="progressbar"
                  aria-label="累计折旧或摊销占资产原值比例"
                  aria-valuemin="0"
                  aria-valuemax="100"
                  :aria-valuenow="chargeProgress(item) ?? undefined"
                >
                  <span :style="{ width: `${chargeProgress(item)}%` }"></span>
                </div>
                <div class="status-row">
                  <span class="asset-status" :class="item.status">{{ item.status_label }}</span>
                  <span>展开查看来源与付款</span>
                </div>
              </summary>
              <dl class="asset-detail">
                <div v-for="row in assetDetails(item)" :key="row.label">
                  <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
                </div>
              </dl>
              <DashboardSourceHistory class="asset-source-history" endpoint="assets" section="source_history" :entity-id="item.asset_id" :period="selectedPeriod" :snapshot-version="response!.snapshot_version" title="查看本项资产的来源历史" @changed="refresh" />
              <DashboardSourceHistory class="asset-source-history" endpoint="assets" section="settlement_events" :entity-id="item.asset_id" :period="selectedPeriod" :snapshot-version="response!.snapshot_version" title="当前后续事项 · 关联清偿事件" @changed="refresh" />
              <details class="accounting-detail">
                <summary>查看折旧摊销与核算说明</summary>
                <p>{{ chargeNote(item) }}</p>
                <dl v-if="accountingDetails(item).length" class="asset-detail">
                  <div v-for="row in accountingDetails(item)" :key="row.label"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></div>
                </dl>
              </details>
              <div v-if="item.settlements.length" class="settlement-detail">
                <h3>{{ settlementTitle(item) }}</h3>
                <p v-if="item.settlement_scope === '本验收批次结算'">以下为整批资产的付款记录，不能作为本项资产的单独已付金额。</p>
                <p v-else-if="item.settlement_scope === '成本来源结算（不分摊为本资产付款）'">以下按项目成本来源查看付款，未分摊为本项资产的已付金额。</p>
                <details v-for="source in item.settlements" :key="source.source_id">
                  <summary>{{ source.label }} · 查看付款记录</summary>
                  <p v-for="(issue, issueIndex) in source.issues ?? []" :key="`issue-${issueIndex}`" class="source-issue">{{ issue.message || '本来源款项尚需核对，请查看精确依据。' }}</p>
                  <p v-for="obligation in source.obligations" :key="obligation.key">{{ obligationLabel(obligation.name) }} {{ formatFen(obligation.amount_fen) }} · 公司实际付款 {{ formatFen(obligation.paid_fen) }} · 代付、抵销等 {{ formatFen(obligation.other_settled_fen) }} · 月末未结金额 {{ formatFen(obligation.remaining_fen) }}</p>
                  <div v-for="movement in source.movements" :key="movement.id">
                    <p>{{ movement.date || `${movement.period}（按月确认）` }} · {{ movement.label }}{{ movement.reversal ? "（冲正）" : "" }} · {{ movement.party }} · {{ formatFen(movement.amount_fen) }}</p>
                    <p v-if="movement.relation_state === 'unresolved'">清偿关系尚未确认，未计入已结金额。</p>
                    <details><summary>查看精确来源业务</summary><p>来源业务：{{ localBusinessName(movement.source_business?.kind) }}</p><VoucherTrace v-if="movement.source_calculation_id" :calculation-id="movement.source_calculation_id" /><VoucherTrace v-if="movement.calculation_id" :calculation-id="movement.calculation_id" /></details>
                  </div>
                  <p v-if="source.movements_page">相关历史清偿（含关联来源，截至所选月末） · 完整总计 {{ source.movements_page.total_count }} 项 · 已加载 {{ source.movements.length }} 项</p>
                  <p>明细包含关联来源；本来源付款及未结金额以上方款项汇总为准。</p>
                  <BusinessStatusDetails v-if="source.movements_page?.has_more && source.subject_id" :subject-id="source.subject_id" :period="selectedPeriod" :snapshot-version="response!.snapshot_version" settlement-view="historical" @changed="refresh" />
                </details>
              </div>
              <div v-if="exitInformation(item)?.settlement.obligations.length" class="settlement-detail">
                <h3>处置款项收回</h3>
                <p v-for="obligation in exitInformation(item)?.settlement.obligations" :key="obligation.key">处置应收 {{ formatFen(obligation.amount_fen) }} · 已收款 {{ formatFen(obligation.paid_fen) }} · 尚未收回 {{ formatFen(obligation.remaining_fen) }}</p>
                <div v-for="movement in exitInformation(item)?.settlement.movements" :key="movement.id">
                  <p>{{ movement.date || `${movement.period}（按月确认）` }} · {{ movement.label }} · {{ formatFen(movement.amount_fen) }}</p>
                  <p v-if="movement.relation_state === 'unresolved'">清偿关系尚未确认，未计入已结金额。</p>
                  <details><summary>查看精确来源业务</summary><p>来源业务：{{ localBusinessName(movement.source_business?.kind) }}</p><VoucherTrace v-if="movement.source_calculation_id" :calculation-id="movement.source_calculation_id" /><VoucherTrace v-if="movement.calculation_id" :calculation-id="movement.calculation_id" /></details>
                </div>
                <p v-if="exitInformation(item)?.settlement.movements_page">相关历史清偿（含关联来源，截至所选月末） · 完整总计 {{ exitInformation(item)!.settlement.movements_page.total_count }} 项 · 已加载 {{ exitInformation(item)!.settlement.movements.length }} 项</p>
                <p>明细包含关联来源；本来源付款及未结金额以上方款项汇总为准。</p>
                <BusinessStatusDetails v-if="exitInformation(item)?.settlement.movements_page?.has_more && exitInformation(item)?.settlement.subject_id" :subject-id="exitInformation(item)!.settlement.subject_id" :period="selectedPeriod" :snapshot-version="response!.snapshot_version" settlement-view="historical" @changed="refresh" />
              </div>
            </details>
            </template>
          </div>
          <div v-else class="empty-filter">{{ filter === 'all' ? '本月没有资产卡片，项目投入另列。' : '当前筛选条件下没有资产卡片。' }} <button v-if="filter !== 'all'" class="control" type="button" @click="filter = 'all'">查看全部资产</button></div>
          <DashboardPagination :page="data.collections.assets?.page" :loaded="filteredItems.length" :loading="pageLoading.assets" :error="pageErrors.assets" @more="loadMore()" @retry="loadMore()" />
        </section>

        <section class="panel">
          <div class="section-heading"><div><h2 id="asset-projects-title" tabindex="-1">尚未计入资产卡片的项目投入</h2></div><strong>{{ formatFen(data.project_cost_fen) }}</strong></div>
          <p class="note">项目来源独立展示；整批结算不分摊为单卡付款。</p>
          <p v-if="!data.projects.length" class="note">本月没有可展示的项目来源。</p>
          <details v-for="project in data.projects" :key="project.source_id" class="asset-card project-card">
            <summary class="project-summary">
              <span class="project-copy"><strong>{{ project.label }}</strong><span>{{ project.period }}<template v-if="project.party"> · {{ project.party }}</template></span><small>展开查看来源与付款</small></span>
              <span class="project-value"><span>剩余项目成本</span><strong>{{ formatFen(project.remaining_fen) }}</strong></span>
            </summary>
            <div class="settlement-detail">
              <p v-for="(issue, issueIndex) in project.settlement.issues ?? []" :key="`issue-${issueIndex}`" class="source-issue">{{ issue.message || '本项目来源款项尚需核对，请查看精确依据。' }}</p>
              <p>该来源已计入项目成本 {{ formatFen(project.cost_fen) }}，付款情况单独列示。</p>
              <p v-for="obligation in project.settlement.obligations" :key="obligation.key">{{ obligationLabel(obligation.name) }} {{ formatFen(obligation.amount_fen) }} · 公司实际付款 {{ formatFen(obligation.paid_fen) }} · 代付、抵销等 {{ formatFen(obligation.other_settled_fen) }} · 月末未结金额 {{ formatFen(obligation.remaining_fen) }}</p>
              <div v-for="movement in project.settlement.movements" :key="movement.id">
                <p>{{ movement.date || `${movement.period}（按月确认）` }} · {{ movement.label }} · {{ formatFen(movement.amount_fen) }}</p>
                <p v-if="movement.relation_state === 'unresolved'">清偿关系尚未确认，未计入已结金额。</p>
                <details><summary>查看精确来源业务</summary><p>来源业务：{{ localBusinessName(movement.source_business?.kind) }}</p><VoucherTrace v-if="movement.source_calculation_id" :calculation-id="movement.source_calculation_id" /><VoucherTrace v-if="movement.calculation_id" :calculation-id="movement.calculation_id" /></details>
              </div>
              <p v-if="project.settlement.movements_page">相关历史清偿（含关联来源，截至所选月末） · 完整总计 {{ project.settlement.movements_page.total_count }} 项 · 已加载 {{ project.settlement.movements.length }} 项</p>
              <p>明细包含关联来源；本来源付款及未结金额以上方款项汇总为准。</p>
              <BusinessStatusDetails v-if="project.settlement.movements_page?.has_more && project.settlement.subject_id" :subject-id="project.settlement.subject_id" :period="selectedPeriod" :snapshot-version="response!.snapshot_version" settlement-view="historical" @changed="refresh" />
            </div>
          </details>
          <DashboardPagination :page="data.collections.projects?.page" :loaded="data.projects.length" :loading="pageLoading.projects" :error="pageErrors.projects" @more="loadMore('projects')" @retry="loadMore('projects')" />
        </section>

      </template>
    </div>
  </section>
</template>

<style scoped>
summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
[id][tabindex="-1"] { scroll-margin-top: 76px; }
.assets-checks { min-width: 0; }
.asset-source-history { margin: 0 16px 12px; min-width: 0; font-size: 12px; overflow-wrap: anywhere; }
.asset-source-history :deep(summary) { min-height: 36px; align-content: center; cursor: pointer; color: var(--accent); }
.asset-source-history :deep(summary:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
.project-card { margin-top: 10px; }
.project-summary { display: grid; grid-template-columns: minmax(0, 1fr) minmax(150px, auto); align-items: center; gap: 12px 24px; }
.project-copy, .project-value { display: grid; min-width: 0; gap: 4px; }
.project-copy > span, .project-value > span { color: var(--muted); font-size: 12px; }
.project-copy > small { color: var(--accent); font-size: 11px; }
.project-value { justify-items: end; font-variant-numeric: tabular-nums; }
.project-value strong { color: var(--gold); font-size: 18px; }
.source-issue { color: var(--warning); }
.assets-total, .kpi strong, .book-value strong, .value-grid strong { overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.asset-card > summary:not(.asset-card-summary) { min-height: 44px; padding: 16px; overflow-wrap: anywhere; cursor: pointer; }
.reconciliation-details { min-width: 0; margin-top: 10px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.reconciliation-details summary, .accounting-detail > summary { color: var(--accent); font-size: 12px; font-weight: 750; cursor: pointer; }
.accounting-detail { margin: 0 16px 16px; padding-top: 12px; border-top: 1px solid var(--line); font-size: 12px; overflow-wrap: anywhere; }
.accounting-detail > p { color: var(--muted); }
.accounting-detail > .asset-detail { margin: 12px 0 0; padding-top: 0; border-top: 0; }
.settlement-detail { padding: 16px; border-top: 1px solid var(--line); overflow-wrap: anywhere; font-size: 12px; }
.settlement-detail h3 { font-size: 14px; }
.assets-page { min-height: 100%; }
.assets-content { width: min(calc(100% - 48px), 1320px); margin: 0 auto; padding: 25px 0 46px; }
.state-panel, .panel { border: 1px solid var(--line); border-radius: 16px; background: var(--surface); box-shadow: var(--shadow-soft); }
.state-panel { display: grid; gap: 7px; padding: 28px; }
.state-panel span, .state-panel button { color: var(--muted); }
.state-panel.error { border-color: var(--danger); }
.state-panel button { width: fit-content; min-height: 40px; margin-top: 8px; padding: 0 14px; border: 0; border-radius: 10px; background: var(--accent); color: var(--surface); cursor: pointer; }
.assets-hero { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(300px, .75fr); gap: 25px; min-height: 198px; padding: 23px 25px; border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line)); border-radius: 20px; background: radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%), linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface))); box-shadow: var(--shadow-soft); }
.eyebrow { margin: 0 0 5px; color: var(--accent); font-size: 11px; font-weight: 850; letter-spacing: .08em; }
.assets-hero > div:first-child > span { color: var(--muted); font-size: 12px; }
.assets-total { display: block; margin: 7px 0 3px; color: var(--gold); font-size: clamp(31px, 4vw, 42px); line-height: 1.1; letter-spacing: -.035em; }
.hero-note { margin: 8px 0 0; color: var(--muted); font-size: 12px; }
.reconciliation { display: grid; align-content: center; align-self: stretch; padding: 14px; border: 1px solid color-mix(in srgb, var(--line) 82%, transparent); border-radius: 15px; background: color-mix(in srgb, var(--surface) 83%, transparent); }
.reconciliation.attention { border-color: var(--warning); }
.reconciliation span, .reconciliation strong, .reconciliation small { display: block; }
.reconciliation span { margin-bottom: 5px; color: var(--muted); font-size: 11px; }
.reconciliation strong { color: var(--accent); font-size: 20px; }
.reconciliation small { margin-top: 6px; color: var(--muted); font-size: 11px; }
.kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
.kpi { position: relative; display: grid; min-width: 0; min-height: 126px; align-content: space-between; gap: 4px; overflow: hidden; padding: 15px 16px; border: 1px solid var(--line); border-radius: 16px; background: var(--surface); box-shadow: var(--shadow-soft); }
.kpi::before { position: absolute; top: 0; right: 0; left: 0; height: 3px; background: var(--accent); content: ""; }
.kpi:nth-child(1)::before { background: var(--gold); }
.kpi:nth-child(2)::before { background: var(--info); }
.kpi:nth-child(4)::before { background: var(--warning); }
.kpi span, .kpi small, .movement-grid span, .movement-grid small { color: var(--muted); }
.kpi span, .movement-grid span { display: block; font-size: 12px; font-weight: 750; }
.kpi small, .movement-grid small { font-size: 11px; }
.kpi strong { display: block; margin: 4px 0; color: var(--accent); font-size: clamp(20px, 2vw, 27px); line-height: 1.15; letter-spacing: -.025em; }
.kpi:nth-child(1) strong { color: var(--gold); }
.kpi:nth-child(2) strong { color: var(--info); }
.kpi:nth-child(4) strong { color: var(--warning); }
.panel { margin-top: 12px; padding: 18px; }
.attention-panel { border-color: color-mix(in srgb, var(--warning) 52%, var(--line)); }
.attention-panel ul { display: grid; gap: 8px; margin: 14px 0 0; padding-left: 20px; color: var(--muted); }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.section-heading h2, .asset-name h3 { margin: 0; }
.section-heading h2 { font-size: 20px; }
.section-heading > strong { color: var(--muted); font-size: 12px; }
.attention-count { padding: 3px 9px; border-radius: 999px; background: var(--warning-soft); color: var(--warning); font-size: 11px; font-weight: 800; }
.movement-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: 7px; margin-top: 14px; }
.movement-grid article { padding: 14px; border-radius: 11px; background: var(--surface-soft); }
.movement-grid strong { display: block; margin: 5px 0 3px; font-size: 20px; }
.asset-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin: 14px 0 12px; }
.asset-toolbar p { margin: 0; color: var(--muted); }
.control { min-height: 38px; padding: 0 12px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); color: var(--text); }
.asset-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; gap: 10px; }
.asset-card { padding: 0; border: 1px solid var(--line); border-radius: 12px; background: var(--surface-soft); }
.asset-card:hover, .asset-card:focus-within, .asset-card[open] { border-color: color-mix(in srgb, var(--accent) 48%, var(--line)); }
.asset-card-summary { display: block; padding: 16px; border-radius: inherit; cursor: pointer; list-style: none; }
.asset-card-summary::-webkit-details-marker { display: none; }
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
.asset-status.pending_activation::before { background: var(--info); }
.asset-status.disposed::before, .asset-status.retired::before { background: var(--muted); }
.asset-detail { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px 14px; margin: 0 16px 16px; padding-top: 12px; border-top: 1px solid var(--line); }
.asset-detail div { display: grid; gap: 2px; }
.asset-detail dt { color: var(--muted); font-size: 11px; }
.asset-detail dd { margin: 0; overflow-wrap: anywhere; font-size: 13px; font-weight: 700; }
.empty-filter { padding: 24px; border-radius: 12px; background: var(--surface-soft); color: var(--muted); text-align: center; }
.note { color: var(--muted); font-size: 13px; }
@media (max-width: 900px) { .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .asset-grid { grid-template-columns: 1fr; } }
@media (max-width: 720px) { .assets-content { width: min(calc(100% - 24px), 1320px); padding: 16px 0 24px; } .assets-hero, .kpi-grid, .movement-grid, .value-grid, .project-summary { grid-template-columns: 1fr; } .assets-hero { gap: 13px; padding: 19px; border-radius: 17px; } .asset-toolbar, .asset-card-head, .status-row { align-items: flex-start; flex-direction: column; } .control { width: 100%; min-height: 44px; } .book-value, .project-value { justify-items: start; white-space: normal; } .asset-detail { grid-template-columns: 1fr; } .section-heading { flex-wrap: wrap; gap: 10px; } }
</style>
