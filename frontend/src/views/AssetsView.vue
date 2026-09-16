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
  type UnestablishedAssetItem,
} from "../api/assets";
import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import BusinessStatusDetails from "../components/BusinessStatusDetails.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { useDashboardSections } from "../composables/useDashboardSections";
import { fen, formatFen, formatPositiveFen } from "../utils/money";

const filters = [
  { value: "all", label: "全部资产" },
  { value: "active", label: "当前在用" },
  { value: "fixed", label: "固定资产" },
  { value: "intangible", label: "无形资产" },
  { value: "pending", label: "待启用资产" },
  { value: "exited", label: "已退出" },
] as const;

type AssetFilter = (typeof filters)[number]["value"];

interface AssetPaymentSummary {
  label: string;
  value: string;
  detail: string;
  tone: "settled" | "attention" | "neutral";
}

const route = useRoute();
const router = useRouter();
const { context, load: loadContext, refresh: refreshContext } = useDashboardContext();
const selectedPeriod = ref("");
const response = ref<AssetsDashboardResponse | null>(null);
const loading = ref(false);
const errorMessage = ref("");
const focusedAssetId = computed(() => typeof route.query.asset_id === "string" ? route.query.asset_id : "");
const filter = computed<AssetFilter>({
  get: () => filters.find(item => item.value === route.query.asset_filter)?.value ?? "all",
  set: value => { void router.push({
    query: { ...route.query, asset_filter: value === "all" ? undefined : value, asset_id: undefined },
    hash: "",
  }); },
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
  () => focusedAssetId.value
    ? "已定位资产卡片"
    : filters.find((item) => item.value === filter.value)?.label ?? "全部资产卡片",
);
const attentionItems = computed(() => {
  const assets = data.value;
  if (!assets) return [];
  const alerts: string[] = [];
  if (assets.reconciled === null) alerts.push("部分资产资料尚未确认，因此资产数量和金额可能不完整；由 AI 会计核对。");
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
  ...(attentionItems.value.length ? [{ id: "assets-checks", label: "资产核对" }] : []),
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
    const result = await fetchAssetsDashboard(period, controller.signal, {
      asset_filter: filter.value,
      asset_id: focusedAssetId.value || undefined,
    });
    if (isCurrent(generation, selection) && activeController === controller) { response.value = result; updateNotice.value = ""; }
    await nextTick();
    if (isCurrent(generation, selection) && activeController === controller && route.hash === "#assets-attention-title") {
      const heading = document.getElementById(route.hash.slice(1));
      if (heading) { positionSection(heading); heading.focus({ preventScroll: true }); }
    }
    if (isCurrent(generation, selection) && activeController === controller && route.hash === "#asset-card-target") {
      const card = document.getElementById("asset-card-target");
      if (card) {
        positionSection(card);
        card.focus({ preventScroll: true });
      }
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

function selectionKey() { return JSON.stringify([route.query.company_id, route.query.period, filter.value, focusedAssetId.value]); }
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

function unresolvedAssetTypeLabel(item: UnestablishedAssetItem) {
  if (item.asset_type === "fixed") return "固定资产";
  if (item.asset_type === "intangible") return "无形资产";
  return "资产类型待确认";
}

function assetDisplayName(item: { name: string | null }) {
  const name = item.name?.trim();
  return !name || name === "未提供资产名称" ? "资产名称待补充" : name;
}

function assetNameNeedsAttention(item: { name: string | null }) {
  const name = item.name?.trim();
  return !name || name === "未提供资产名称";
}

function assetCategoryLabel(item: EstablishedAssetItem) {
  return item.category_label && item.category_label !== assetTypeLabel(item) ? item.category_label : "";
}

function monthEventLabel(item: EstablishedAssetItem) {
  if (item.month_exited) return "本月退出";
  if (item.month_acquired && item.month_activated) return "本月新增并启用";
  if (item.month_acquired) return "本月新增";
  if (item.month_activated) return "本月启用";
  return "";
}

function assetTimeline(item: EstablishedAssetItem) {
  const exit = exitInformation(item);
  if (exit) {
    const action = isFixedAsset(item) && item.disposal?.kind === "sale" ? "出售" : isFixedAsset(item) ? "报废" : "退役";
    return `${dateLabel(exit.date)} 已${action} · 取得于 ${item.recognition_label}`;
  }
  if (item.status === "pending_activation") return `${item.recognition_label} 取得 · 尚未启用`;
  const useDate = isFixedAsset(item) ? item.in_service_date : item.available_for_use_date;
  if (!useDate) return `${item.recognition_label} 取得 · 启用时间待补充`;
  const useLabel = dateLabel(useDate);
  const useAction = isFixedAsset(item) ? "投入使用" : "可供使用";
  return useLabel === item.recognition_label
    ? `${useLabel} 取得并${useAction}`
    : `${item.recognition_label} 取得 · ${useLabel} ${useAction}`;
}

function chargeVerb(item: EstablishedAssetItem) {
  return isFixedAsset(item) ? "折旧" : "摊销";
}

function chargeProgressText(item: EstablishedAssetItem) {
  if (item.status === "pending_activation") return `启用后开始${chargeVerb(item)}`;
  const progress = chargeProgress(item);
  if (progress === null) return "价值构成待核对";
  const value = Number.isInteger(progress) ? progress.toFixed(0) : progress.toFixed(1);
  return `已${chargeVerb(item)} ${value}%`;
}

function paymentScopeLabel(item: EstablishedAssetItem) {
  return ({
    "本资产结算": "本项付款",
    "本验收批次结算": "整批付款",
    "成本来源结算（不分摊为本资产付款）": "项目来源款项",
  } as Record<string, string>)[item.settlement_scope] ?? "相关款项";
}

function assetPaymentSummary(item: EstablishedAssetItem): AssetPaymentSummary {
  const obligations = new Map<string, EstablishedAssetItem["settlements"][number]["obligations"][number]>();
  let issueCount = 0;
  for (const source of item.settlements) {
    issueCount += source.issues?.length ?? 0;
    for (const obligation of source.obligations) obligations.set(obligation.key, obligation);
  }
  const rows = [...obligations.values()];
  const label = paymentScopeLabel(item);
  if (!rows.length) {
    return {
      label,
      value: issueCount ? `${issueCount} 项待核对` : "未列付款事项",
      detail: issueCount ? "付款依据需要 AI 会计确认" : "点击查看取得来源",
      tone: issueCount ? "attention" : "neutral",
    };
  }
  const totals = rows.reduce((current, row) => ({
    amount: current.amount + fen(row.amount_fen),
    paid: current.paid + fen(row.paid_fen),
    other: current.other + fen(row.other_settled_fen),
    remaining: current.remaining + fen(row.remaining_fen),
  }), { amount: 0n, paid: 0n, other: 0n, remaining: 0n });
  const details: string[] = [];
  if (totals.paid) details.push(`公司已付 ${formatFen(totals.paid)}`);
  if (totals.other) details.push(`抵销等 ${formatFen(totals.other)}`);
  if (!details.length) details.push(`相关应付 ${formatFen(totals.amount)}`);
  if (issueCount) details.push(`${issueCount} 项关系待核对`);
  return {
    label,
    value: totals.remaining ? `月末待付 ${formatFen(totals.remaining)}` : issueCount ? `${issueCount} 项待核对` : "月末已结清",
    detail: details.join(" · "),
    tone: totals.remaining || issueCount ? "attention" : "settled",
  };
}

function dateLabel(value: string) {
  return value.length === 7 ? `${value}（按月确认）` : value;
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

onMounted(() => {
  mounted = true;
  void synchronizePeriod();
});

watch(
  () => [route.query.company_id, route.query.period, filter.value, focusedAssetId.value],
  (value, previous) => {
    if (value.every((item, index) => item === previous[index])) return;
    invalidateRequests();
  },
  { flush: "sync" },
);
watch(
  () => [context.value?.current_company?.company_id, route.query.period, filter.value, focusedAssetId.value] as const,
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
      >
        <template #navigation>
          <DashboardSectionNav v-if="sectionLinks.length" :items="sectionLinks" :active="activeSection" label="资产内容导航" @select="focusSection" />
        </template>
      </DashboardModuleHeader>


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
          <p class="dashboard-hero-eyebrow">
              {{ selectedPeriodView.label }}期末 · 全公司
            </p>
          <div>

            <span id="assets-total-label">期末长期资产账面价值</span>
            <strong class="assets-total dashboard-hero-title">{{ formatFen(data.ledger_net_fen) }}</strong>
            <p class="dashboard-hero-note">
              在用固定资产净值 {{ formatFen(data.fixed.active_net_fen) }} · 在用无形资产净值
              {{ formatFen(data.intangible.active_net_fen) }} · {{ countQualifier || '共 ' }}{{ data.active_count }} 项在用
            </p>
            <p v-if="pendingCost() === null || pendingCost() || data.project_cost_fen === null || fen(data.project_cost_fen)" class="dashboard-hero-note">
              另含待启用资产 {{ formatFen(pendingCost()) }}
              <span v-if="data.project_cost_fen === null || fen(data.project_cost_fen)"> · 尚未计入资产卡片的项目投入 {{ formatFen(data.project_cost_fen) }}</span>
            </p>
          </div>
          <div class="reconciliation" :class="{ attention: !data.reconciled }">
            <span>资产明细与账面记录</span>
            <strong>{{ data.reconciled === null ? "尚不能完整核对" : data.reconciled ? "核对一致" : "存在差异" }}</strong>
            <small v-if="!data.reconciled">
              <a href="#assets-attention-title">查看需要关注的事项</a> · <a href="#asset-list-title">查看相关资产卡片</a>
            </small>
            <details class="reconciliation-details">
              <summary>查看核对说明</summary>
              <p>范围包括在用、待启用资产及尚未计入资产卡片的项目投入。</p>
              <p>账面成本 {{ formatFen(data.ledger_cost_fen) }} · 明细成本 {{ formatFen(data.card_cost_fen) }}</p>
              <p>账面累计折旧摊销 {{ formatFen(data.ledger_accumulated_fen) }} · 明细累计折旧摊销 {{ formatFen(data.card_accumulated_fen) }}</p>
              <p>账面价值 {{ formatFen(data.ledger_net_fen) }} · 明细价值 {{ formatFen(data.card_net_fen) }}</p>
            </details>
          </div>
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
          </section>

        <p v-if="data.unestablished_count" class="note" role="status">全公司有 {{ data.unestablished_count }} 项资产资料尚未确认；在用、待启用及本月变动数量仅列已确认部分，不代表完整数量。</p>

        <section
          v-if="attentionItems.length"
          id="assets-checks"
          class="assets-checks panel attention-panel"
          tabindex="-1"
          aria-labelledby="assets-attention-title"
        >
          <div class="section-heading">
            <div><h2 id="assets-attention-title" tabindex="-1">资产关注事项</h2></div>
            <span class="attention-count">{{ attentionItems.length }} 条提示</span>
          </div>
          <ul><li v-for="item in attentionItems" :key="item">{{ item }}</li></ul>
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
            <div>
              <h2 id="asset-list-title" tabindex="-1">资产明细</h2>
              <p class="list-caption"><strong>{{ data.unestablished_count ? "已确认" : "共" }} {{ data.registered_count }}</strong> 项资产 · {{ filterLabel }} · 已加载 {{ filteredItems.length }} 项</p>
            </div>
            <div class="asset-toolbar">
              <p v-if="data.unestablished_count && ['active', 'pending', 'exited'].includes(filter)">当前筛选只显示资料已确认的资产；待确认项目会另行提示。</p>
              <select v-model="filter" class="control" aria-label="筛选资产">
                <option v-for="item in filters" :key="item.value" :value="item.value">
                  {{ item.label }}
                </option>
              </select>
            </div>
          </div>

          <p v-if="data.unestablished_count">另有 {{ data.unestablished_count }} 项资产资料尚未确认，暂不计入资产数量和金额。</p>
          <div v-if="filteredItems.length" class="asset-grid">
            <template v-for="item in filteredItems" :key="item.asset_id">
            <article
              v-if="item.selection_status === 'unestablished'"
              :id="focusedAssetId === item.asset_id ? 'asset-card-target' : undefined"
              class="asset-card asset-unestablished dashboard-record-card"
              tabindex="-1"
            >
              <div class="asset-card-summary">
                <div class="asset-card-topline">
                  <span class="asset-classification">{{ unresolvedAssetTypeLabel(item) }}</span>
                </div>
                <div class="asset-card-head">
                  <div class="asset-name">
                    <h3>{{ assetDisplayName(item) }}</h3>
                    <span class="asset-status needs-attention">资料待确认</span>
                  </div>
                  <div class="book-value unknown">
                    <span>所选月末还值</span>
                    <strong>暂无法确定</strong>
                  </div>
                </div>
                <p class="asset-unestablished-note">该项资料尚未确认，暂不计入资产数量和金额；由 AI 会计核对。</p>
              </div>
            </article>
            <article v-else
              :id="focusedAssetId === item.asset_id ? 'asset-card-target' : undefined"
              class="asset-card dashboard-record-card"
              :class="item.status"
              tabindex="-1"
            >
              <div class="asset-card-summary">
                <div class="asset-card-topline">
                  <span class="asset-classification">
                    {{ assetTypeLabel(item) }}<template v-if="assetCategoryLabel(item)"> · {{ assetCategoryLabel(item) }}</template> · {{ item.code }}
                  </span>
                </div>
                <div class="asset-card-head">
                  <div class="asset-name">
                    <h3 :class="{ 'needs-attention': assetNameNeedsAttention(item) }">{{ assetDisplayName(item) }}</h3>
                    <div class="asset-badges">
                      <span class="asset-status" :class="item.status">{{ item.status_label }}</span>
                      <span v-if="monthEventLabel(item)" class="asset-event">{{ monthEventLabel(item) }}</span>
                    </div>
                    <p class="asset-timeline">{{ assetTimeline(item) }}</p>
                  </div>
                  <div class="book-value">
                    <span>所选月末还值</span>
                    <strong>{{ formatFen(item.book_value_fen) }}</strong>
                    <small>{{ chargeProgressText(item) }}</small>
                  </div>
                </div>
                <div class="owner-value-grid">
                  <div>
                    <span>取得成本</span>
                    <strong>{{ formatFen(item.cost_fen) }}</strong>
                  </div>
                  <div>
                    <span>{{ chargeLabel(item) }}</span>
                    <strong>{{ formatFen(item.accumulated_charge_fen) }}</strong>
                    <small>本月{{ chargeVerb(item) }} {{ formatFen(item.month_charge_fen) }}</small>
                  </div>
                  <div class="payment-state" :class="assetPaymentSummary(item).tone">
                    <span>{{ assetPaymentSummary(item).label }}</span>
                    <strong>{{ assetPaymentSummary(item).value }}</strong>
                    <small>{{ assetPaymentSummary(item).detail }}</small>
                  </div>
                </div>
              </div>
            </article>
            </template>
          </div>
          <div v-else class="empty-filter">{{ filter === 'all' ? '本月没有资产，项目投入另列。' : '当前筛选条件下没有资产。' }} <button v-if="filter !== 'all'" class="control" type="button" @click="filter = 'all'">查看全部资产</button></div>
          <DashboardPagination :page="data.collections.assets?.page" :loaded="filteredItems.length" :loading="pageLoading.assets" :error="pageErrors.assets" @more="loadMore()" @retry="loadMore()" />
        </section>

        <section class="panel">
          <div class="section-heading"><div><h2 id="asset-projects-title" tabindex="-1">尚未计入资产卡片的项目投入</h2></div><strong>{{ formatFen(data.project_cost_fen) }}</strong></div>
          <p class="note">项目来源独立展示；整批结算不分摊为单卡付款。</p>
          <p v-if="!data.projects.length" class="note">本月没有可展示的项目来源。</p>
          <details v-for="project in data.projects" :key="project.source_id" class="asset-card dashboard-record-card project-card">
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
.project-card { margin-top: 10px; }
.project-summary { display: grid; grid-template-columns: minmax(0, 1fr) minmax(150px, auto); align-items: center; gap: 12px 24px; }
.project-copy, .project-value { display: grid; min-width: 0; gap: 4px; }
.project-copy > span, .project-value > span { color: var(--muted); font-size: 12px; }
.project-copy > small { color: var(--accent); font-size: 11px; }
.project-value { justify-items: end; font-variant-numeric: tabular-nums; }
.project-value strong { color: var(--gold); font-size: 18px; }
.source-issue { color: var(--warning); }
.assets-total, .kpi strong, .book-value strong, .owner-value-grid strong { overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.asset-card > summary:not(.asset-card-summary) { min-height: 44px; padding: 16px; overflow-wrap: anywhere; cursor: pointer; }
.reconciliation-details { min-width: 0; margin-top: 10px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.reconciliation-details summary { color: var(--accent); font-size: 12px; font-weight: 750; cursor: pointer; }
.settlement-detail { padding: 16px; border-top: 1px solid var(--line); overflow-wrap: anywhere; font-size: 12px; }
.settlement-detail h3 { font-size: 14px; }
.assets-page { min-height: 100%; }
.assets-content { width: min(calc(100% - 48px), 1320px); margin: 0 auto; padding: 25px 0 46px; }
.state-panel, .panel { border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface);  }
.state-panel { display: grid; gap: 7px; padding: 28px; }
.state-panel span, .state-panel button { color: var(--muted); }
.state-panel.error { border-color: var(--danger); }
.state-panel button { width: fit-content; min-height: 40px; margin-top: 8px; padding: 0 14px; border: 0; border-radius: var(--radius-control); background: var(--accent); color: var(--surface); cursor: pointer; }
.assets-hero { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px 40px; min-height: 198px; padding: 25px 28px; border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line)); border-radius: 20px; background: radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%), linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface)));  }
.assets-hero > div > span { color: var(--muted); font-size: 12px; font-weight: 750; }
.assets-total { color: var(--gold); }
.reconciliation { display: grid; align-content: start; align-self: stretch; padding: 0; border: 0; border-radius: 0; background: transparent; }
.reconciliation.attention strong { color: var(--warning); }
.reconciliation span, .reconciliation strong, .reconciliation small { display: block; }
.reconciliation span { margin-bottom: 5px; color: var(--muted); font-size: 11px; }
.reconciliation strong { color: var(--accent); font-size: 20px; }
.reconciliation small { margin-top: 6px; color: var(--muted); font-size: 11px; }
.kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 20px 28px; margin-top: 8px;
  overflow: visible;

  border: 0;

  border-radius: 0;

  background: transparent;

  grid-column: 1 / -1;
}
.kpi { position: relative; display: grid; min-width: 0; min-height: 0; align-content: start; gap: 6px; overflow: hidden; padding: 0; border: 0; border-radius: 0; background: transparent;
  border-left: 0;

  grid-template-rows: auto auto 1fr;
}
.kpi span, .kpi small, .movement-grid span, .movement-grid small { color: var(--muted); }
.kpi span, .movement-grid span { display: block; font-size: 12px; font-weight: 750; }
.kpi small, .movement-grid small { font-size: 11px; }
.kpi strong { display: block; margin: 4px 0; color: var(--text); font-size: clamp(20px, 2vw, 26px); line-height: 1.15; letter-spacing: -.025em;
  font-variant-numeric: tabular-nums;

  overflow-wrap: anywhere;
}
.panel { margin-top: 40px; padding: 0;
  border: 0;

  background: transparent;
}
.attention-panel { border-color: color-mix(in srgb, var(--warning) 52%, var(--line)); }
.attention-panel ul { display: grid; gap: 8px; margin: 14px 0 0; padding-left: 20px; color: var(--muted); }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.section-heading h2, .asset-name h3 { margin: 0; }
.section-heading h2 { font-size: 20px; }
.section-heading > strong { color: var(--muted); font-size: 12px; }
.attention-count { padding: 3px 9px; border-radius: 999px; background: var(--warning-soft); color: var(--warning); font-size: 11px; font-weight: 800; }
.movement-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: 8px; margin-top: 14px;
  padding: 12px;

  border: 1px solid var(--line);

  border-radius: var(--radius-panel);

  background: var(--surface);
}
.movement-grid article { padding: 14px; }
.movement-grid strong { display: block; margin: 5px 0 3px; font-size: 20px; }
.asset-toolbar { display: flex; align-items: center; flex: none; justify-content: flex-end; gap: 14px; margin: 0; }
/* 与小字同组的标题行：下对齐，并与下方卡片保持 16px 间距。 */
.section-heading:has(.list-caption) { align-items: flex-end; margin-bottom: 16px; }
.asset-toolbar p { margin: 0; color: var(--muted); }
/* 与资金、员工页标题下那行小字同一套：13px / --muted / 行高 1.5。 */
.list-caption { margin: 3px 0 0; color: var(--muted); font-size: 13px; line-height: 1.5; }
.list-caption strong { font-weight: inherit; }
.control { min-height: 38px; padding: 0 12px; border: 1px solid var(--line); border-radius: var(--radius-control); background: var(--surface); color: var(--text); }
.asset-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; gap: 14px; }
.asset-card:target { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 12%, transparent); }
.asset-card.pending_activation { border-style: dashed; border-color: var(--accent); }
.asset-card.disposed, .asset-card.retired { opacity: .82; }
.asset-card-summary { min-height: 228px; padding: 17px 18px 18px; }
.asset-card-topline { display: flex; align-items: center; gap: 12px; margin-bottom: 11px; }
.asset-classification { min-width: 0; color: var(--muted); font-size: 11px; font-weight: 720; }
.asset-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.asset-name { display: grid; min-width: 0; gap: 7px; }
.asset-name h3 { overflow-wrap: anywhere; font-size: 20px; line-height: 1.2; }
.asset-name h3.needs-attention { color: var(--warning); }
.asset-badges { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.asset-status, .asset-event { display: inline-flex; width: fit-content; align-items: center; min-height: 22px; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 780; white-space: nowrap; }
.asset-status { background: var(--accent-soft); color: var(--accent); }
.asset-status.pending_activation { background: var(--info-soft); color: var(--info); }
.asset-status.disposed, .asset-status.retired { background: var(--surface-soft); color: var(--muted); }
.asset-status.needs-attention { background: var(--warning-soft); color: var(--warning); }
.asset-event { background: color-mix(in srgb, var(--gold) 12%, var(--surface)); color: var(--gold); }
.asset-timeline { margin: 0; color: var(--muted); font-size: 11px; line-height: 1.45; }
.book-value { display: grid; min-width: 140px; justify-items: end; gap: 2px; text-align: right; white-space: nowrap; }
.book-value span, .book-value small { color: var(--muted); font-size: 11px; }
.book-value strong { color: var(--text); font-size: 22px; line-height: 1.2; }
.book-value small { color: var(--accent); font-weight: 720; }
.book-value.unknown strong { color: var(--warning); font-size: 17px; }
.owner-value-grid { display: grid; grid-template-columns: .85fr .9fr 1.35fr; margin-top: 16px; overflow: hidden; border: 1px solid color-mix(in srgb, var(--line) 82%, transparent); border-radius: 11px; background: var(--surface-soft); }
.owner-value-grid > div { display: grid; min-width: 0; align-content: start; gap: 3px; padding: 11px 12px; }
.owner-value-grid > div + div { border-left: 1px solid var(--line); }
.owner-value-grid span, .owner-value-grid small { color: var(--muted); font-size: 10.5px; line-height: 1.35; }
.owner-value-grid strong { color: var(--text); font-size: 14px; line-height: 1.35; }
.owner-value-grid .payment-state.settled strong { color: var(--accent); }
.owner-value-grid .payment-state.attention strong { color: var(--warning); }
.asset-unestablished { border-color: color-mix(in srgb, var(--warning) 46%, var(--line)); }
.asset-unestablished .asset-card-summary { min-height: 190px; background: linear-gradient(135deg, color-mix(in srgb, var(--warning-soft) 42%, var(--surface)), var(--surface)); }
.asset-unestablished-note { margin: 16px 0 0; padding: 11px 12px; border-radius: 10px; background: color-mix(in srgb, var(--warning-soft) 60%, var(--surface)); color: var(--muted); font-size: 12px; line-height: 1.5; }
.empty-filter { padding: 24px; border-radius: var(--radius-control); background: var(--surface-soft); color: var(--muted); text-align: center; }
.note { color: var(--muted); font-size: 13px; }
@media (max-width: 900px) { .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .asset-grid { grid-template-columns: 1fr; } }
@media (max-width: 720px) { .assets-content { width: min(calc(100% - 24px), 1320px); padding: 16px 0 24px; } .assets-hero, .kpi-grid, .movement-grid, .project-summary, .owner-value-grid { grid-template-columns: 1fr; } .assets-hero { gap: 13px; padding: 19px; border-radius: 17px; } .asset-toolbar, .asset-card-head { align-items: flex-start; flex-direction: column; } .asset-card-summary { min-height: 0; } .control { width: 100%; min-height: 44px; } .book-value, .project-value { min-width: 0; justify-items: start; text-align: left; white-space: normal; } .owner-value-grid > div + div { border-top: 1px solid var(--line); border-left: 0; } .section-heading { flex-wrap: wrap; gap: 10px; } }

.assets-hero > .dashboard-hero-eyebrow { grid-column: 1 / -1; margin: 0 0 -12px; }

.assets-hero .kpi-grid > * { min-height: 0; padding: 0; border: 0; background: transparent; }
.assets-hero .kpi-grid strong { font-variant-numeric: tabular-nums; }
@media (max-width: 760px) {
  .assets-hero .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
}
</style>
