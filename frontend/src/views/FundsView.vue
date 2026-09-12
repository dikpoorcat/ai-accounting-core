<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import {
  fetchFundsDashboard,
  fundAccountLabel,
  rememberFundAccounts,
  type BankStatementState,
  type FundsData,
  type FundsQuery,
} from "../api/funds";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import PeriodPreparation from "../components/PeriodPreparation.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import VoucherTrace from "../components/brief/VoucherTrace.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { useDashboardSections } from "../composables/useDashboardSections";
import { fen, formatFen, formatPositiveFen } from "../utils/money";

const route = useRoute();
const router = useRouter();
const {
  context,
  loading: contextLoading,
  error: contextError,
  load: loadContext,
  refresh: refreshContext,
} = useDashboardContext();

const selectedPeriod = ref("");
type PageKind = "book" | "bank" | "investment" | "accounts" | "investment_products";
const pageStates = ref<Record<PageKind, { loading: boolean; error: string }>>({
  book: { loading: false, error: "" }, bank: { loading: false, error: "" },
  investment: { loading: false, error: "" }, accounts: { loading: false, error: "" }, investment_products: { loading: false, error: "" },
});
const pageRequests = new Map<PageKind, AbortController>();
const selectedAccount = ref(routeAccount());
const selectedBankAccount = ref(queryText("statement_account_id"));
const selectedDetailView = ref<"book" | "bank">(queryText("funds_view") === "bank" ? "bank" : "book");
const funds = ref<FundsData | null>(null);
const snapshotVersion = ref("");
const selectedPeriodLabel = ref("");
const responsePeriod = ref("");
const updateNotice = ref("");
const loading = ref(false);
const initializing = ref(true);
const requestError = ref("");
let activeRequest: AbortController | null = null;
let mounted = true;
let requestGeneration = 0;

const periods = computed(() => context.value?.periods ?? []);
const pageError = computed(() => requestError.value || contextError.value);
const detailViews = ["book", "bank"] as const;
const visibleMovements = computed(() => funds.value?.movements ?? []);
const bankAccounts = computed(
  () => funds.value?.accounts.filter((account) => account.type === "bank") ?? [],
);
const accountOptions = computed(() => {
  const options = (funds.value?.accounts ?? []).map(account => ({ value: accountKey(account.type, account.account_id), label: `${account.name}（${account.code}）` }));
  if (selectedAccount.value && !options.some(option => option.value === selectedAccount.value)) {
    const separator = selectedAccount.value.indexOf(":");
    options.push({ value: selectedAccount.value, label: fundAccountLabel(queryText("company_id"), selectedPeriod.value, selectedAccount.value.slice(0, separator), selectedAccount.value.slice(separator + 1)) ?? "所选账户（名称尚未加载）" });
  }
  return options;
});
const bankAccountOptions = computed(() => {
  const options = bankAccounts.value.map(account => ({ value: account.account_id, label: `${account.name}（${account.code}）` }));
  if (selectedBankAccount.value && !options.some(option => option.value === selectedBankAccount.value)) options.push({ value: selectedBankAccount.value, label: fundAccountLabel(queryText("company_id"), selectedPeriod.value, "bank", selectedBankAccount.value) ?? "所选银行账户（名称尚未加载）" });
  return options;
});
const visibleBankRows = computed(() => funds.value?.bank_statement.rows ?? []);
const attentionItems = computed(() => {
  if (!funds.value) return [];
  const items: string[] = [];
  for (const account of funds.value.accounts) {
    if (account.negative_balance) {
      items.push(
        `${account.name}期末账面余额为负数 ${formatFen(account.closing_fen)}。`,
      );
    }
    if (
      ["attention", "pending", "not_configured"].includes(
        account.reconciliation.state,
      )
    ) {
      items.push(`${account.name}：${account.reconciliation.label}。`);
    }
  }
  const statement = funds.value.bank_statement;
  if (statement.unmatched_count) {
    items.push(
      `${statement.unmatched_count} 笔银行流水尚未完成有效匹配。`,
    );
  }
  if (statement.needs_review_count) {
    items.push(`${statement.needs_review_count} 笔流水的资料或核对结果待复核。`);
  }
  if (statement.missing_account_count) {
    items.push(`${statement.missing_account_count} 个银行账户尚未提供本月流水，不能据此判断没有收支。`);
  }
  return items;
});
const bankAttentionCount = computed(() => {
  const statement = funds.value?.bank_statement;
  return statement
    ? statement.unmatched_count + statement.needs_review_count
    : 0;
});
const sectionLinks = computed(() => {
  if (!funds.value) return [];
  const links = [
    { id: "funds-overview", label: "概览" },
    { id: "fund-accounts", label: "账户" },
  ];
  if (funds.value?.investments.products.length) links.push({ id: "fund-investments", label: "货币基金" });
  if (attentionItems.value.length || funds.value?.attention_account_count) links.push({ id: "funds-attention", label: "关注" });
  links.push({ id: "bank-details", label: "资金明细" });
  return links;
});
const { activeSection, focusSection, positionSection, lockSectionSync } =
  useDashboardSections(sectionLinks, "funds-overview");

function accountKey(type: string, id: string) {
  return `${type}:${id}`;
}

function queryText(key: string) { return typeof route.query[key] === "string" ? route.query[key] as string : ""; }
function routeAccount() {
  const type = queryText("movement_account_type"), id = queryText("movement_account_id");
  return ["bank", "cash", "payment_platform"].includes(type) && id ? accountKey(type, id) : "";
}
function voucherTarget(reference: string, period: string) {
  return /^[1-9]\d*$/.test(reference) && /^\d{4}-\d{2}$/.test(period)
    ? { path: "/", query: { company_id: queryText("company_id"), period, voucher: reference } } : null;
}
function cancelPages() {
  for (const request of pageRequests.values()) request.abort();
  pageRequests.clear();
  for (const state of Object.values(pageStates.value)) { state.loading = false; state.error = ""; }
}

function queryFilters(): FundsQuery {
  const separator = selectedAccount.value.indexOf(":");
  const type = selectedAccount.value.slice(0, separator);
  const id = selectedAccount.value.slice(separator + 1);
  return {
    ...(separator > 0 && (type === "bank" || type === "cash" || type === "payment_platform") ? { movement_account_type: type, movement_account_id: id } : {}),
    ...(selectedBankAccount.value ? { statement_account_id: selectedBankAccount.value } : {}),
  };
}

function selectionKey() {
  return JSON.stringify([route.query.company_id, route.query.period, selectedPeriod.value, selectedAccount.value, selectedBankAccount.value]);
}
function isCurrent(generation: number, selection: string) { return mounted && generation === requestGeneration && selectionKey() === selection; }
function invalidateRequests() {
  requestGeneration += 1;
  activeRequest?.abort(); cancelPages();
  activeRequest = null;
  snapshotVersion.value = ""; funds.value = null; responsePeriod.value = ""; loading.value = false;
}

function changeAccountFilters() {
  const filters = queryFilters();
  void router.push({ query: { company_id: route.query.company_id, period: selectedPeriod.value,
    movement_account_type: filters.movement_account_type, movement_account_id: filters.movement_account_id,
    statement_account_id: filters.statement_account_id, funds_view: selectedDetailView.value } });
}

function bankCoverageLabel() {
  const statement = funds.value?.bank_statement;
  if (!statement) return "银行资料尚未读取";
  if (statement.missing_account_count) return `${statement.missing_account_count} 个银行账户尚未提供本月流水`;
  if (["missing", "partial"].includes(statement.coverage_state)) return "流水覆盖或核对状态尚不能完整确认";
  if (statement.coverage_state === "not_applicable") return "暂无公司银行账户";
  if (!statement.transaction_count) return "完整流水已确认，本月无发生";
  return bankAttentionCount.value ? `${bankAttentionCount.value} 笔待核对` : "银行流水均已匹配";
}

function routePeriod(): string | null {
  return typeof route.query.period === "string" ? route.query.period : null;
}

async function revealBankDetails() {
  const generation = requestGeneration, selection = selectionKey();
  if (route.hash !== "#bank-details" || !funds.value) return;
  lockSectionSync();
  selectedDetailView.value = "bank";
  activeSection.value = "bank-details";
  await nextTick();
  if (!isCurrent(generation, selection)) return;
  const section = document.getElementById("bank-details");
  if (section) positionSection(section);
  document.getElementById("fund-detail-tab-bank")?.focus({ preventScroll: true });
}

async function loadFunds(periodKey: string) {
  const generation = ++requestGeneration;
  cancelPages();
  activeRequest?.abort();
  const controller = new AbortController();
  const selection = selectionKey();
  const filters = queryFilters();
  lockSectionSync();
  const sectionToRestore = activeSection.value;
  const shouldRestoreSection = funds.value !== null;
  snapshotVersion.value = "";
  if (funds.value) {
    const emptyPage = { has_more: false, next_cursor: null, total_count: 0 };
    funds.value = {
      ...funds.value,
      movements: [],
      movement_page: emptyPage,
      bank_statement: { ...funds.value.bank_statement, rows: [], page: emptyPage },
      investments: { ...funds.value.investments, events: [], page: emptyPage },
    };
  }
  activeRequest = controller;
  loading.value = true;
  requestError.value = "";
  try {
    const response = await fetchFundsDashboard(periodKey, controller.signal, filters);
    if (response.schema_version !== 2) {
      throw new Error("FUNDS_SCHEMA_MISMATCH");
    }
    const data = response.data;
    if (!isCurrent(generation, selection) || activeRequest !== controller) return;
    funds.value = data;
    if (data) rememberFundAccounts(queryText("company_id"), periodKey, data.accounts);
    snapshotVersion.value = response.snapshot_version;
    responsePeriod.value = response.selected_period?.key ?? "";
    selectedPeriodLabel.value = response.selected_period?.label ?? "";
    await nextTick();
    if (!isCurrent(generation, selection) || activeRequest !== controller) return;
    const targetSection = sectionLinks.value.some((link) => link.id === sectionToRestore)
      ? sectionToRestore
      : sectionToRestore === "funds-attention"
        ? "bank-details"
        : "funds-overview";
    activeSection.value = targetSection;
    if (route.hash === "#bank-details") {
      await revealBankDetails();
    } else if (shouldRestoreSection && targetSection !== "funds-overview") {
      const section = document.getElementById(targetSection);
      if (section) positionSection(section);
    }
  } catch (error: unknown) {
    if (!isCurrent(generation, selection) || activeRequest !== controller) return;
    requestError.value = dashboardErrorMessage(error);
  } finally {
    if (isCurrent(generation, selection) && activeRequest === controller) {
      activeRequest = null;
      loading.value = false;
      initializing.value = false;
      // The current first-page request owns recovery completion, including reads started by the context watcher.
      if (updateNotice.value === "资料已更新，正在重新读取。") {
        updateNotice.value = requestError.value || !funds.value
          ? "资料已更新，请重新读取当前筛选。"
          : "资料已更新，已重新读取当前筛选。";
      }
    }
  }
}

async function loadMore(kind: PageKind) {
  const current = funds.value;
  const section = kind === "book" ? "movements" : kind === "bank" ? "statements" : kind === "investment" ? "investment_events" : kind;
  const page = current?.collections[section]?.page;
  const state = pageStates.value[kind];
  if (!current || !page?.has_more || !page.next_cursor || state.loading || loading.value) return;
  const selection = selectionKey();
  const generation = requestGeneration;
  const version = snapshotVersion.value;
  const request = new AbortController(); pageRequests.set(kind, request); state.loading = true; state.error = "";
  try {
    const next = await fetchFundsDashboard(selectedPeriod.value, request.signal,
      { ...queryFilters(), expected_version: snapshotVersion.value, section, cursor: page.next_cursor });
    if (!isCurrent(generation, selection) || pageRequests.get(kind) !== request || !next.data || !funds.value || snapshotVersion.value !== version) return;
    const latest = funds.value;
    const collection = next.data.collections[section];
    const collections = { ...latest.collections, [section]: { ...collection, items: [...latest.collections[section].items, ...collection.items] } };
    if (kind === "book") funds.value = { ...latest, collections, movements: [...latest.movements, ...next.data.movements], movement_page: next.data.movement_page };
    else if (kind === "bank") funds.value = { ...latest, collections, bank_statement: { ...latest.bank_statement, rows: [...latest.bank_statement.rows, ...next.data.bank_statement.rows], page: next.data.bank_statement.page } };
    else if (kind === "accounts") { funds.value = { ...latest, collections, accounts: [...latest.accounts, ...next.data.accounts] }; rememberFundAccounts(queryText("company_id"), selectedPeriod.value, next.data.accounts); }
    else if (kind === "investment_products") funds.value = { ...latest, collections, investments: { ...latest.investments, products: [...latest.investments.products, ...next.data.investments.products] } };
    else funds.value = { ...latest, collections, investments: { ...latest.investments, events: [...latest.investments.events, ...next.data.investments.events], page: next.data.investments.page } };
  } catch (caught) {
    if (isCurrent(generation, selection) && pageRequests.get(kind) === request) {
      if (isDashboardSnapshotChanged(caught)) { updateNotice.value = "资料已更新，正在重新读取。"; await refresh(); }
      else state.error = dashboardErrorMessage(caught);
    }
  }
  finally { if (isCurrent(generation, selection) && pageRequests.get(kind) === request) { pageRequests.delete(kind); state.loading = false; } }
}

function changePeriod(value: string) {
  void router.push({ query: { company_id: route.query.company_id, period: value || undefined } });
}

async function refresh() {
  invalidateRequests();
  const generation = requestGeneration;
  const selection = selectionKey();
  const period = selectedPeriod.value;
  try {
    await refreshContext();
    if (isCurrent(generation, selection) && period) await loadFunds(period);
  } catch (caught) {
    if (isCurrent(generation, selection)) {
      requestError.value = dashboardErrorMessage(caught);
      if (updateNotice.value === "资料已更新，正在重新读取。") updateNotice.value = "资料已更新，请重新读取当前筛选。";
    }
  }
}

async function retry() {
  if (context.value) {
    await refresh();
    return;
  }
  initializing.value = true;
  const generation = requestGeneration, selection = selectionKey();
  try {
    await loadContext(true);
  } catch {
    // The shared context exposes the user-safe error message.
  } finally {
    if (isCurrent(generation, selection)) initializing.value = false;
  }
}

function formatDate(value: string | null): string {
  if (!value) return "日期未提供";
  if (/^\d{4}-\d{2}$/.test(value)) return `${value} · 按月确认`;
  const parts = value.split("-");
  if (parts.length !== 3) return value;
  return `${Number(parts[1])} 月 ${Number(parts[2])} 日`;
}

function formatSigned(value: string): string {
  const amount = fen(value);
  if (amount > 0n) return `+${formatFen(amount)}`;
  if (amount < 0n) return `−${formatPositiveFen(amount)}`;
  return formatFen(0);
}

function movementAmount(direction: "inflow" | "outflow", value: string) {
  return `${direction === "inflow" ? "+" : "−"}${formatPositiveFen(value)}`;
}

function bankStateLabel(state: BankStatementState): string {
  return {
    matched: "已匹配",
    unmatched: "待匹配",
    needs_review: "资料或核对结果待复核",
  }[state];
}

function selectDetailView(view: "book" | "bank") {
  selectedDetailView.value = view;
  void router.replace({ query: { ...route.query, funds_view: view }, hash: "" });
}

function handleDetailTabKey(event: KeyboardEvent, index: number) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  let nextIndex = index;
  if (event.key === "Home") nextIndex = 0;
  if (event.key === "End") nextIndex = detailViews.length - 1;
  if (event.key === "ArrowLeft") {
    nextIndex = (index - 1 + detailViews.length) % detailViews.length;
  }
  if (event.key === "ArrowRight") nextIndex = (index + 1) % detailViews.length;
  const nextView = detailViews[nextIndex];
  selectDetailView(nextView);
  const generation = requestGeneration, selection = selectionKey();
  void nextTick(() => { if (isCurrent(generation, selection)) document.getElementById(`fund-detail-tab-${nextView}`)?.focus(); });
}

function reconciliationAttention(state: string): boolean {
  return ["attention", "pending", "not_configured"].includes(state);
}

watch(
  () => [route.query.company_id, route.query.period, selectedAccount.value, selectedBankAccount.value],
  (value, previous) => { if (!value.every((item, index) => item === previous[index])) { updateNotice.value = ""; invalidateRequests(); } },
  { flush: "sync" },
);
watch(
  () => route.query.company_id,
  (value, previous) => {
    if (value === previous) return;
    activeRequest?.abort();
    cancelPages();
    snapshotVersion.value = "";
    funds.value = null;
    selectedPeriod.value = "";
    selectedPeriodLabel.value = "";
    selectedAccount.value = "";
    selectedBankAccount.value = "";
    activeSection.value = "funds-overview";
    loading.value = false;
    requestError.value = "";
  },
);

watch(
  () => route.hash,
  () => void revealBankDetails(),
  { immediate: true, flush: "post" },
);
watch(() => route.query.funds_view, () => {
  selectedDetailView.value = queryText("funds_view") === "bank" ? "bank" : "book";
});

watch(
  () => [context.value, route.query.period, route.query.movement_account_type, route.query.movement_account_id, route.query.statement_account_id] as const,
  ([dashboardContext], previous) => {
    if (!mounted || !dashboardContext || dashboardContext.current_company?.company_id !== route.query.company_id) return;
    const previousContext = previous?.[0];
    const companyChanged =
      dashboardContext.current_company?.company_id !== previousContext?.current_company?.company_id;
    const requested = routePeriod();
    const requestedExists = dashboardContext.periods.some(
      (item) => item.key === requested,
    );
    const target = requestedExists ? requested : dashboardContext.default_period;
    if (!target) {
      activeRequest?.abort();
      cancelPages();
      snapshotVersion.value = "";
      selectedPeriod.value = "";
      selectedPeriodLabel.value = "";
      selectedAccount.value = "";
      selectedBankAccount.value = "";
      activeSection.value = "funds-overview";
      funds.value = null;
      return;
    }
    if (requested !== target) {
      void router.replace({ query: { ...route.query, period: target } });
      return;
    }
    const account = routeAccount(), bankAccount = queryText("statement_account_id");
    if (companyChanged || selectedPeriod.value !== target || selectedAccount.value !== account || selectedBankAccount.value !== bankAccount || !funds.value) {
      funds.value = null;
      snapshotVersion.value = "";
      selectedAccount.value = account;
      selectedBankAccount.value = bankAccount;
      selectedPeriod.value = target;
      fundAccountLabel(queryText("company_id"), target, "", "");
      void loadFunds(target);
    }
  },
  { immediate: true },
);

onMounted(async () => {
  const generation = requestGeneration, selection = selectionKey();
  try {
    await loadContext();
  } catch {
    // The shared context exposes the user-safe error message.
  } finally {
    if (isCurrent(generation, selection)) initializing.value = false;
  }
});

onBeforeUnmount(() => {
  mounted = false;
  invalidateRequests();
});
</script>

<template>
  <div class="funds-page">
    <div class="page-content">
      <DashboardModuleHeader
        title="资金总览"
        :options="periods"
        :selected="selectedPeriod"
        :loading="loading || contextLoading"
        select-label="资金查看月份"
        @change="changePeriod"
        @refresh="refresh"
      />

      <p v-if="updateNotice" class="muted" role="status">{{ updateNotice }}</p>

      <section v-if="pageError" class="state-panel error-state" role="alert">
        <strong>资金数据暂时无法读取</strong>
        <p>{{ pageError }}</p>
        <button type="button" @click="retry">重新加载</button>
      </section>

      <section
        v-if="(initializing || loading) && !funds"
        class="state-panel loading-state"
        aria-live="polite"
      >
        <strong>正在读取资金数据…</strong>
        <span>正在读取所选月份的账户余额与收付款。</span>
      </section>

      <section v-else-if="!periods.length" class="state-panel">
        <strong>还没有可查看的资金月份</strong>
        <span>录入首笔资金业务后，即可查看账户余额与收付款。</span>
      </section>

      <section v-else-if="!funds && !pageError" class="state-panel">
        <strong>所选月份暂无资金数据</strong>
        <span>可以选择其他月份或重新加载。</span>
      </section>

      <template v-else-if="funds">
        <DashboardSectionNav
          :items="sectionLinks"
          :active="activeSection"
          label="资金页面区段"
          floating
          @select="focusSection"
        />

        <div class="funds-dashboard" :aria-busy="loading">
        <section
          id="funds-overview"
          class="funds-hero section-anchor"
          aria-labelledby="funds-total-label"
          tabindex="-1"
        >
          <div>
            <p class="eyebrow">截至 {{ selectedPeriodLabel }}末 · 全公司</p>
            <span id="funds-total-label">月末账面资金</span>
            <strong class="funds-total">{{ formatFen(funds.total_fen) }}</strong>
            <p class="muted">
              <template v-if="funds.account_count">
                银行 {{ formatFen(funds.bank_fen) }} · 支付平台
                {{ formatFen(funds.payment_platform_fen) }} · 现金
                {{ formatFen(funds.cash_fen) }} · 共 {{ funds.account_count }} 个资金账户
              </template>
              <template v-else>本月尚无公司资金账户及资金活动</template>
            </p>
          </div>
          <div class="funds-change">
            <span>本月资金增减</span>
            <strong :class="{ loss: fen(funds.net_change_fen) < 0n }">
              {{ formatSigned(funds.net_change_fen) }}
            </strong>
            <small>
              月初账面资金 {{ formatFen(funds.opening_fen) }}
            </small>
          </div>
        </section>

        <section class="kpi-grid" aria-label="资金核心指标">
          <article>
            <span>本月实际收款</span>
            <strong>{{ formatFen(funds.inflow_fen) }}</strong>
            <small>全公司 · 不含公司账户间互转</small>
          </article>
          <article>
            <span>本月实际付款</span>
            <strong>{{ formatFen(funds.outflow_fen) }}</strong>
            <small>全公司 · 不含公司账户间互转</small>
          </article>
        </section>

        <section v-if="funds.fact_issues?.length" class="historical-source-issues" aria-label="历史独立采用说明">
          <p role="status">{{ funds.fact_issues.length }} 组历史资金来源尚不能证明独立封存采用。具体金额与流水核对状态分别见对应区块；作为其他结果的来源，不等于已独立采用。</p>
          <p class="muted">以下保留所选月末独立采用尚未证明的来源与候选，与当前跟进状态分别列示。</p>
          <details v-for="(issue, issueIndex) in funds.fact_issues" :key="issueIndex">
            <summary>查看第 {{ issueIndex + 1 }} 组历史来源与精确候选</summary>
            <p>候选只供核对，不作为已采用金额累计。</p>
            <div v-for="candidate in issue.candidates" :key="candidate.calculation_id">
              <VoucherTrace :calculation-id="candidate.calculation_id" />
            </div>
            <details><summary>未建立原因与原始来源标识</summary><pre>{{ JSON.stringify(issue, null, 2) }}</pre></details>
          </details>
        </section>
        <PeriodPreparation :preparation="funds.period_preparation" :snapshot-version="snapshotVersion" @changed="refresh" />

        <section
          id="fund-accounts"
          class="panel section-panel section-anchor"
          aria-labelledby="fund-accounts-title"
          tabindex="-1"
        >
          <div class="section-heading">
            <div>
              <h2 id="fund-accounts-title">公司资金账户</h2>
            </div>
            <strong>{{ funds.account_count }} 个账户 · 期末 {{ formatFen(funds.total_fen) }}</strong>
          </div>
          <p class="muted">全公司共 {{ funds.account_count }} 个账户，{{ funds.attention_account_count }} 个账户需要核对；以下已加载 {{ funds.accounts.length }} 个账户。</p>
          <div v-if="funds.accounts.length" class="account-grid">
            <article
              v-for="account in funds.accounts"
              :key="accountKey(account.type, account.account_id)"
              class="account-card"
              :class="{
                attention:
                  account.negative_balance ||
                  reconciliationAttention(account.reconciliation.state),
              }"
            >
              <div class="account-head">
                <div>
                  <span>{{ account.type === "bank" ? "银行账户" : account.type === "payment_platform" ? "支付平台" : "现金账户" }} · {{ account.code }}</span>
                  <h3>{{ account.name }}</h3>
                </div>
                <div class="account-balance">
                  <span>期末账面余额</span>
                  <strong :class="{ loss: account.negative_balance }">
                    {{ formatFen(account.closing_fen) }}
                  </strong>
                </div>
              </div>
              <dl class="account-metrics">
                <div><dt>月初</dt><dd>{{ formatFen(account.opening_fen) }}</dd></div>
                <div><dt>本月流入</dt><dd>{{ formatFen(account.inflow_fen) }}</dd></div>
                <div><dt>本月流出</dt><dd>{{ formatFen(account.outflow_fen) }}</dd></div>
              </dl>
              <p v-if="account.negative_balance || reconciliationAttention(account.reconciliation.state)" class="account-status" :class="account.reconciliation.state">
                <template v-if="account.negative_balance">余额为负<template v-if="reconciliationAttention(account.reconciliation.state)"> · </template></template>
                <template v-if="reconciliationAttention(account.reconciliation.state)">{{ account.reconciliation.label }}</template>
              </p>
              <div v-if="account.reconciliation.source_check" class="source-check">
                <p>{{ account.reconciliation.source_check.message }}</p>
                <details><summary>查看账户来源引用与证明</summary><pre>{{ JSON.stringify(account.reconciliation.source_check, null, 2) }}</pre></details>
              </div>
            </article>
          </div>
          <p v-else class="empty">本月暂无已入账的公司资金账户。</p>
          <DashboardPagination :page="funds.collections.accounts?.page" :loaded="funds.accounts.length" :loading="pageStates.accounts.loading" :error="pageStates.accounts.error" @more="loadMore('accounts')" @retry="loadMore('accounts')" />
        </section>

        <section v-if="funds.investments.products.length" id="fund-investments"
          class="panel section-panel section-anchor" tabindex="-1" aria-labelledby="fund-investments-title">
          <div class="section-heading">
            <div><h2 id="fund-investments-title">货币基金</h2></div>
            <strong>期末账面成本 {{ formatFen(funds.investments.closing_cost_fen) }}</strong>
          </div>
          <p class="muted">按账面成本列示，不代表当前市值；未计入上方账户资金。</p>
          <p class="muted">本月确认收益 {{ formatFen(funds.investments.investment_income_fen) }} ·
            实际申购付款 {{ formatFen(funds.investments.actual_payments_fen) }} ·
            实际赎回到账 {{ formatFen(funds.investments.actual_receipts_fen) }}</p>
          <div class="table-wrap" role="region" aria-label="基金产品汇总" tabindex="0">
            <table class="investment-table investment-summary-table">
              <colgroup><col><col class="investment-amount-column"><col class="investment-amount-column"></colgroup>
              <thead><tr><th scope="col">产品</th><th scope="col" class="number">月末账面成本</th><th scope="col" class="number">本月确认收益</th></tr></thead>
              <tbody><tr v-for="item in funds.investments.products" :key="item.fund_id">
                <td>{{ item.name }}</td><td class="number">{{ formatFen(item.closing_cost_fen) }}</td>
                <td class="number">{{ formatFen(item.investment_income_fen) }}</td>
              </tr></tbody></table>
          </div>
          <DashboardPagination :page="funds.collections.investment_products?.page" :loaded="funds.investments.products.length" :loading="pageStates.investment_products.loading" :error="pageStates.investment_products.error" @more="loadMore('investment_products')" @retry="loadMore('investment_products')" />
          <details class="investment-details">
          <summary>查看申购、赎回与收付款明细</summary>
          <p class="muted">确认金额与实际收付款分别列示，确认收益不等于已经到账。</p>
          <div class="table-wrap" role="region" aria-label="基金成本变动" tabindex="0">
            <table class="investment-table investment-cost-table">
              <colgroup><col><col v-for="column in 5" :key="column" class="investment-amount-column"></colgroup>
              <thead><tr><th scope="col">产品</th><th scope="col" class="number">期初成本</th><th scope="col" class="number">申购成本变动</th>
              <th scope="col" class="number">赎回成本变动</th><th scope="col" class="number">期末成本</th><th scope="col" class="number">本月确认收益</th></tr></thead>
              <tbody><tr v-for="item in funds.investments.products" :key="item.fund_id">
                <td>{{ item.name }}<details><summary>查看产品标识</summary>{{ item.fund_id }}</details></td>
                <td class="number">{{ formatFen(item.opening_cost_fen) }}</td>
                <td class="number">{{ formatFen(item.subscription_cost_fen) }}</td>
                <td class="number">{{ formatFen(item.redemption_cost_fen) }}</td>
                <td class="number">{{ formatFen(item.closing_cost_fen) }}</td>
                <td class="number">{{ formatFen(item.investment_income_fen) }}</td>
              </tr></tbody></table>
          </div>
          <h3>本月确认及收付款</h3>
          <div v-if="funds.investments.events.length" class="table-wrap" role="region" aria-label="基金确认及收付款明细" tabindex="0">
            <table class="investment-table investment-events-table">
              <colgroup><col class="date-column"><col><col v-for="column in 4" :key="column" class="investment-amount-column"><col class="reference-column"></colgroup>
              <thead><tr><th scope="col">日期／所属月</th><th scope="col">产品及事项</th><th scope="col" class="number">确认成本</th>
              <th scope="col" class="number">确认赎回净款</th><th scope="col" class="number">确认收益</th><th scope="col" class="number">实际收付款</th><th scope="col">凭证</th></tr></thead>
              <tbody><tr v-for="item in funds.investments.events" :key="item.id">
                <td>{{ formatDate(item.date || item.period) }}</td><td>{{ item.name }} · {{ item.type }}</td>
                <td class="number">{{ item.cost_fen === null ? "—" : formatFen(item.cost_fen) }}</td>
                <td class="number">{{ item.net_proceeds_fen === null ? "—" : formatFen(item.net_proceeds_fen) }}</td>
                <td class="number">{{ item.investment_income_fen === null ? "—" : formatFen(item.investment_income_fen) }}</td>
                <td class="number">{{ item.settlement_fen === null ? "—" : formatFen(item.settlement_fen) }}</td>
                <td><RouterLink v-if="voucherTarget(item.reference, item.period)" :to="voucherTarget(item.reference, item.period)!">查看凭证 {{ item.reference }}</RouterLink><span v-else>凭证定位未提供</span></td>
              </tr></tbody></table>
          </div>
          <p v-else class="empty">{{ loading ? "正在读取基金明细…" : "本月没有已确认的申赎或实际收付款。" }}</p>
          <DashboardPagination :page="funds.collections.investment_events?.page" :loaded="funds.investments.events.length" :loading="pageStates.investment.loading" :error="pageStates.investment.error" @more="loadMore('investment')" @retry="loadMore('investment')" />
          </details>
        </section>

        <section
          v-if="attentionItems.length || funds.attention_account_count"
          id="funds-attention"
          class="panel section-panel attention-panel section-anchor"
          aria-labelledby="funds-attention-title"
          tabindex="-1"
        >
          <div class="section-heading">
            <div>
              <h2 id="funds-attention-title">资金关注事项</h2>
            </div>
            <span class="status-chip attention">{{ funds.attention_account_count ? `全公司 ${funds.attention_account_count} 个账户需核对` : '银行流水需核对' }}</span>
          </div>
          <details>
          <summary>查看已加载账户问题与全公司流水核对情况</summary>
          <p class="muted">账户问题仅包含已加载 {{ funds.accounts.length }} 个账户；流水覆盖与待匹配数量为全公司范围，两类数量不相加。</p>
          <ul class="attention-list">
            <li v-for="(item, index) in attentionItems" :key="`${index}-${item}`">{{ item }}</li>
          </ul>
          </details>
        </section>

        <div class="final-section-space">
        <section
          id="bank-details"
          class="panel section-panel section-anchor"
          aria-labelledby="fund-details-title"
          tabindex="-1"
        >
          <div class="section-heading detail-heading">
            <div>
              <h2 id="fund-details-title">资金明细</h2>
            </div>
            <div class="detail-switch" role="tablist" aria-label="选择资金明细口径">
              <button
                id="fund-detail-tab-book"
                type="button"
                role="tab"
                :aria-selected="selectedDetailView === 'book'"
                aria-controls="fund-detail-panel-book"
                :tabindex="selectedDetailView === 'book' ? 0 : -1"
                @click="selectDetailView('book')"
                @keydown="handleDetailTabKey($event, 0)"
              >
                账面明细
              </button>
              <button
                id="fund-detail-tab-bank"
                type="button"
                role="tab"
                :aria-selected="selectedDetailView === 'bank'"
                aria-controls="fund-detail-panel-bank"
                :aria-label="bankAttentionCount ? `银行流水，${bankAttentionCount} 笔待处理` : '银行流水'"
                :tabindex="selectedDetailView === 'bank' ? 0 : -1"
                @click="selectDetailView('bank')"
                @keydown="handleDetailTabKey($event, 1)"
              >
                银行流水
                <span v-if="bankAttentionCount" class="detail-tab-alert" aria-hidden="true">
                  {{ bankAttentionCount }}
                </span>
              </button>
            </div>
          </div>
          <div
            v-if="selectedDetailView === 'book'"
            id="fund-detail-panel-book"
            role="tabpanel"
            aria-labelledby="fund-detail-tab-book"
            tabindex="0"
          >
            <div class="detail-toolbar">
              <div>
                <strong>{{ loading ? "正在读取…" : `当前账户范围 ${funds.collections.movements?.page.filtered_count ?? funds.movement_page.total_count} 条资金变动` }}</strong>
                <p class="muted">
                  按所选账户查询整月记录；上方金额为全公司汇总。账户互转按转出、转入分别列示。
                </p>
              </div>
              <div class="account-selector">
              <select v-model="selectedAccount" class="control" aria-label="筛选账面资金账户" @change="changeAccountFilters">
                <option value="">全部账户</option>
                <option v-for="option in accountOptions" :key="option.value" :value="option.value">
                  {{ option.label }}
                </option>
              </select>
              <button v-if="funds.collections.accounts?.page.has_more" class="control" :disabled="pageStates.accounts.loading" @click="loadMore('accounts')">{{ pageStates.accounts.loading ? '正在加载账户…' : pageStates.accounts.error ? '重试加载账户' : '继续加载账户选项' }}</button>
              <span v-if="pageStates.accounts.error" role="alert">{{ pageStates.accounts.error }}</span>
              <small>账户选项已加载 {{ funds.accounts.length }} / {{ funds.account_count }}</small>
              </div>
            </div>
            <div v-if="visibleMovements.length" class="table-wrap movement-list" role="region" aria-label="账面资金明细" tabindex="0">
              <table class="book-detail-table movement-table">
                <colgroup><col class="date-column"><col class="account-column"><col><col class="party-column"><col class="direction-column"><col class="amount-column"><col class="reference-column"></colgroup>
                <thead><tr><th scope="col">日期</th><th scope="col">账户</th><th scope="col">业务与摘要</th><th scope="col">往来对象</th><th scope="col">方向</th><th scope="col" class="number">金额</th><th scope="col">凭证</th></tr></thead>
                <tbody>
                  <tr v-for="item in visibleMovements" :key="item.id" class="movement-record">
                    <td data-label="日期">{{ formatDate(item.date) }}</td>
                    <td data-label="账户" class="mobile-wide">{{ item.account_name }}<small class="table-secondary">{{ item.account_code }}</small></td>
                    <td data-label="业务与摘要" class="movement-copy mobile-wide">
                      <strong>{{ item.list_summary || item.type }}</strong>
                      <small v-if="item.internal_transfer" class="table-secondary">账户互转</small>
                      <details v-if="(item.display_summary || item.summary) && (item.display_summary || item.summary) !== (item.list_summary || item.type)" class="movement-detail">
                        <summary>完整摘要</summary>
                        <p>{{ item.display_summary || item.summary }}</p>
                      </details>
                    </td>
                    <td data-label="往来对象" class="mobile-wide">{{ item.party || '不适用' }}</td>
                    <td data-label="方向"><span class="direction" :class="item.direction">{{ item.direction === 'inflow' ? '流入' : '流出' }}</span></td>
                    <td data-label="金额" class="number movement-amount">{{ movementAmount(item.direction, item.amount_fen) }}</td>
                    <td data-label="凭证" class="mobile-wide">
                      <RouterLink v-if="voucherTarget(item.reference, responsePeriod)" :to="voucherTarget(item.reference, responsePeriod)!">查看凭证 {{ item.reference }}</RouterLink>
                      <span v-else>凭证定位未提供</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="empty">{{ loading ? "正在读取资金明细…" : selectedAccount ? "该账户本月没有已入账资金变动。" : "本月没有已入账资金变动。" }}</p>
            <button v-if="!visibleMovements.length && selectedAccount" class="control" @click="selectedAccount = ''; changeAccountFilters()">清除账户筛选</button>
            <DashboardPagination :page="funds.collections.movements?.page" :loaded="funds.movements.length" :loading="pageStates.book.loading" :error="pageStates.book.error" @more="loadMore('book')" @retry="loadMore('book')" />
          </div>

          <div
            v-else
            id="fund-detail-panel-bank"
            role="tabpanel"
            aria-labelledby="fund-detail-tab-bank"
            tabindex="0"
          >
            <div class="detail-toolbar">
              <div>
                <span class="status-chip" :class="{ attention: bankAttentionCount }">
                  {{ bankCoverageLabel() }}
                </span>
                <p class="muted">
                  {{ loading ? "正在读取…" : `当前账户范围 ${funds.collections.statements?.page.filtered_count ?? funds.bank_statement.page.total_count} 笔流水` }} · 按所选账户查询整月记录，保留银行原始摘要。
                </p>
                <details class="bank-coverage-details">
                  <summary>查看全公司流水资料与核对情况</summary>
                  <p class="muted">已提供 {{ funds.bank_statement.provided_account_count }} / {{ funds.bank_statement.expected_account_count }} 个银行账户资料。
                    已提供流水流入 {{ formatFen(funds.bank_statement.inflow_fen) }} · 流出 {{ formatFen(funds.bank_statement.outflow_fen) }}。
                    已匹配 {{ funds.bank_statement.matched_count }} / {{ funds.bank_statement.transaction_count }} 笔。
                    以上金额按已提供流水列示；资料是否齐全与来源核对是否获证分别查看。</p>
                </details>
              </div>
              <div class="account-selector">
              <select v-model="selectedBankAccount" class="control" aria-label="筛选银行流水账户" @change="changeAccountFilters">
                <option value="">全部银行账户</option>
                <option v-for="option in bankAccountOptions" :key="option.value" :value="option.value">
                  {{ option.label }}
                </option>
              </select>
              <button v-if="funds.collections.accounts?.page.has_more" class="control" :disabled="pageStates.accounts.loading" @click="loadMore('accounts')">{{ pageStates.accounts.loading ? '正在加载账户…' : pageStates.accounts.error ? '重试加载账户' : '继续加载账户选项' }}</button>
              <span v-if="pageStates.accounts.error" role="alert">{{ pageStates.accounts.error }}</span>
              <small>账户选项已加载 {{ funds.accounts.length }} / {{ funds.account_count }}</small>
              </div>
            </div>
            <div v-if="visibleBankRows.length" class="table-wrap movement-list" role="region" aria-label="银行流水明细" tabindex="0">
              <table class="bank-detail-table movement-table">
                <colgroup><col class="date-column"><col class="account-column"><col><col class="direction-column"><col class="amount-column"><col class="state-column"></colgroup>
                <thead><tr><th scope="col">日期</th><th scope="col">银行账户</th><th scope="col">对方与银行原始摘要</th><th scope="col">方向</th><th scope="col" class="number">金额</th><th scope="col">匹配状态</th></tr></thead>
                <tbody>
                  <tr v-for="item in visibleBankRows" :key="item.id" class="movement-record" :class="{ 'attention-row': item.state !== 'matched' }">
                    <td data-label="日期">{{ formatDate(item.date) }}</td>
                    <td data-label="银行账户" class="mobile-wide">{{ item.account_name }}<small class="table-secondary">{{ item.account_code }}</small></td>
                    <td data-label="对方与银行原始摘要" class="movement-copy mobile-wide"><strong>{{ item.party || '对方名称未提供' }}</strong><p>{{ item.memo || '原始摘要未提供' }}</p></td>
                    <td data-label="方向"><span class="direction" :class="item.direction">{{ item.direction === 'inflow' ? '流入' : '流出' }}</span></td>
                    <td data-label="金额" class="number movement-amount">{{ movementAmount(item.direction, item.amount_fen) }}</td>
                    <td data-label="匹配状态" class="mobile-wide">{{ bankStateLabel(item.state) }}<p v-if="item.source_check">{{ item.source_check.message }}</p><details v-if="item.source_check" class="source-check"><summary>查看流水来源引用与证明</summary><pre>{{ JSON.stringify(item.source_check, null, 2) }}</pre></details></td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="empty">{{ loading ? "正在读取银行流水…" : selectedBankAccount ? "该账户本月暂无可展示的银行流水，请结合资料提供情况查看。" : "本月没有可展示的银行流水。" }}</p>
            <button v-if="!visibleBankRows.length && selectedBankAccount" class="control" @click="selectedBankAccount = ''; changeAccountFilters()">清除账户筛选</button>
            <DashboardPagination :page="funds.collections.statements?.page" :loaded="funds.bank_statement.rows.length" :loading="pageStates.bank.loading" :error="pageStates.bank.error" @more="loadMore('bank')" @retry="loadMore('bank')" />
          </div>
        </section>
        </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.historical-source-issues { margin: 14px 0; padding: 14px 16px; border-left: 3px solid var(--warning); border-radius: 8px; background: var(--warning-soft); font-size: 13px; overflow-wrap: anywhere; }
.historical-source-issues > p:first-child { color: var(--warning); font-weight: 700; }
.historical-source-issues summary { min-height: 36px; cursor: pointer; }
.historical-source-issues pre { max-height: 320px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.source-check { font-size: 12px; overflow-wrap: anywhere; }
.source-check summary { min-height: 32px; cursor: pointer; }
.source-check pre { max-height: 320px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.funds-page {
  min-height: 100%;
}

.page-content {
  width: min(calc(100% - 48px), 1320px);
  margin: 0 auto;
  padding: 25px 0 46px;
}

.funds-dashboard {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  min-width: 0;
  gap: 12px;
  opacity: 1;
  transition: opacity 160ms ease;
}

.funds-dashboard[aria-busy="true"] {
  opacity: 0.72;
}

.section-anchor {
  scroll-margin-top: 66px;
}

.final-section-space {
  min-height: calc(100vh - 80px);
}

.panel,
.state-panel,
.kpi-grid article {
  min-width: 0;
  border: 1px solid var(--line);
  background: var(--surface);
  box-shadow: var(--shadow-soft);
}

.panel,
.kpi-grid article {
  border-radius: 16px;
}

.state-panel {
  display: grid;
  gap: 7px;
  padding: 28px;
  border-radius: 18px;
}

.state-panel p,
.state-panel span {
  margin: 0;
  color: var(--muted);
}

.state-panel button {
  width: max-content;
  min-height: 40px;
  margin-top: 8px;
  padding: 0 13px;
  border: 0;
  border-radius: 10px;
  background: var(--accent);
  color: var(--surface);
  cursor: pointer;
}

.error-state {
  border-color: var(--danger);
}

.funds-hero {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(300px, 0.75fr);
  gap: 25px;
  min-height: 198px;
  padding: 23px 25px;
  border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line));
  border-radius: 20px;
  background:
    radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%),
    linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface)));
  box-shadow: var(--shadow-soft);
}

.eyebrow {
  margin: 0 0 5px;
  color: var(--accent);
  font-size: 11px;
  font-weight: 850;
  letter-spacing: 0.08em;
}

.funds-hero > div:first-child > span {
  color: var(--muted);
  font-size: 12px;
}

.funds-total {
  display: block;
  margin: 7px 0 3px;
  color: var(--info);
  font-size: clamp(31px, 4vw, 42px);
  line-height: 1.1;
  letter-spacing: -0.035em;
}

.muted {
  color: var(--muted);
}

.funds-hero .muted {
  margin: 8px 0 0;
  font-size: 12px;
}

.funds-change {
  display: grid;
  align-content: center;
  align-self: stretch;
  padding: 14px;
  border: 1px solid color-mix(in srgb, var(--line) 82%, transparent);
  border-radius: 15px;
  background: color-mix(in srgb, var(--surface) 83%, transparent);
}

.funds-change span,
.funds-change small {
  display: block;
  color: var(--muted);
  font-size: 11px;
}

.funds-change strong {
  display: block;
  margin: 8px 0;
  color: var(--accent);
  font-size: 27px;
}

.loss,
.funds-change strong.loss {
  color: var(--danger);
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.kpi-grid article {
  position: relative;
  display: grid;
  min-width: 0;
  min-height: 126px;
  align-content: space-between;
  gap: 4px;
  overflow: hidden;
  padding: 15px 16px;
}

.kpi-grid article::before {
  position: absolute;
  top: 0;
  right: 0;
  left: 0;
  height: 3px;
  background: var(--accent);
  content: "";
}

.kpi-grid article:nth-child(1)::before {
  background: var(--info);
}

.kpi-grid article:nth-child(1) strong {
  color: var(--info);
}

.kpi-grid article:nth-child(2)::before {
  background: var(--danger);
}

.kpi-grid article:nth-child(2) strong {
  color: var(--danger);
}

.investment-details,
.bank-coverage-details {
  margin-top: 14px;
}

summary {
  cursor: pointer;
  color: var(--muted);
}

.investment-details > summary {
  padding: 10px 0;
}

.kpi-grid span,
.kpi-grid small {
  color: var(--muted);
  font-size: 11px;
}

.kpi-grid span {
  font-size: 12px;
  font-weight: 750;
}

.kpi-grid strong {
  font-size: clamp(20px, 2vw, 27px);
  line-height: 1.15;
  letter-spacing: -0.025em;
}

.section-panel {
  padding: 18px;
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 14px;
}

.section-heading h2 {
  margin: 0;
  font-size: 20px;
}

.section-heading > strong {
  color: var(--muted);
  font-size: 12px;
}

.detail-heading {
  align-items: center;
  margin-bottom: 16px;
}

.detail-switch {
  display: inline-grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px;
  padding: 5px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface-soft);
}

.detail-switch button {
  display: inline-flex;
  min-width: 104px;
  min-height: 40px;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 7px 13px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  white-space: nowrap;
}

.detail-switch button[aria-selected="true"] {
  background: var(--surface);
  box-shadow: var(--shadow-soft);
  color: var(--text);
  font-weight: 800;
}

.detail-switch button:focus-visible,
[role="tabpanel"]:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.detail-tab-alert {
  display: inline-grid;
  min-width: 19px;
  min-height: 19px;
  place-items: center;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--warning-soft);
  color: var(--warning);
  font-size: 10px;
  font-weight: 850;
  line-height: 1;
}

.account-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.account-card {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-soft);
}

.account-card.attention {
  border-color: var(--warning);
}

.account-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
}

.account-head span,
.account-balance span {
  color: var(--muted);
  font-size: 11px;
}

.account-head h3 {
  margin: 4px 0 0;
  font-size: 16px;
}

.account-balance {
  text-align: right;
}

.account-balance strong {
  display: block;
  margin-top: 4px;
  font-size: 19px;
}

.account-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px;
  overflow: hidden;
  margin: 14px 0;
  padding: 1px;
  border-radius: 9px;
  background: var(--line);
}

.account-metrics div {
  padding: 9px;
  background: var(--surface);
}

.account-metrics dt {
  color: var(--muted);
  font-size: 11px;
}

.account-metrics dd {
  margin: 4px 0 0;
  overflow-wrap: anywhere;
  font-size: 13px;
  font-weight: 700;
}

.account-status {
  margin: 0;
  padding-top: 11px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
}

.account-status.attention,
.account-status.pending,
.account-status.not_configured {
  color: var(--warning);
}

.attention-panel {
  border-color: color-mix(in srgb, var(--warning) 52%, var(--line));
}

.status-chip {
  display: inline-flex;
  min-height: 25px;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 800;
}

.status-chip.attention {
  background: var(--warning-soft);
  color: var(--warning);
}

.attention-list {
  display: grid;
  gap: 8px;
  margin: 0;
  padding-left: 21px;
}

.detail-toolbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 12px;
}

.detail-toolbar > div {
  min-width: 0;
}

.detail-toolbar strong {
  font-size: 12px;
}

.detail-toolbar p {
  margin: 5px 0 0;
  font-size: 12px;
}

.detail-toolbar .control {
  flex: 0 0 auto;
  max-width: min(100%, 320px);
}

.control {
  min-height: 38px;
  padding: 0 11px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  color: var(--text);
}

.table-wrap {
  min-width: 0;
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 10px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.account-selector { display: grid; min-width: 0; gap: 6px; }
.account-selector select { max-width: 100%; }

.movement-table,
.investment-table {
  table-layout: auto;
}

.book-detail-table { min-width: 1100px; }
.bank-detail-table { min-width: 980px; }
.date-column { width: 90px; }
.account-column { width: 200px; }
.party-column { width: 150px; }
.direction-column { width: 64px; }
.amount-column { width: 128px; }
.reference-column { width: 100px; }
.state-column { width: 110px; }
.investment-amount-column { width: 140px; }
.investment-summary-table { min-width: 500px; }
.investment-cost-table { min-width: 920px; }
.investment-events-table { min-width: 1040px; }

.table-wrap:focus-visible,
.movement-detail > summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.movement-table td,
.investment-table td {
  overflow-wrap: anywhere;
}

.table-secondary {
  display: block;
  margin-top: 4px;
  color: var(--muted);
}

.movement-copy > p,
.movement-detail > p {
  margin: 5px 0 0;
}

.movement-copy > strong,
.movement-amount {
  font-weight: 700;
}

.movement-detail {
  margin-top: 5px;
}

.movement-detail > summary {
  color: var(--accent);
}

.movement-table a,
.investment-table a {
  color: var(--accent);
}

.direction {
  display: inline-flex;
  padding: 2px 6px;
  border-radius: 999px;
  font-size: 11px;
  white-space: nowrap;
}

.direction.inflow { color: var(--accent); background: var(--accent-soft); }
.direction.outflow { color: var(--danger); background: var(--danger-soft); }

th,
td {
  padding: 10px 11px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}

th {
  background: var(--surface-soft);
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}

tbody tr:last-child td {
  border-bottom: 0;
}

.number {
  text-align: right;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.attention-row {
  background: color-mix(in srgb, var(--warning-soft) 46%, var(--surface));
}

.empty {
  margin: 0;
  padding: 20px;
  border-radius: 10px;
  background: var(--surface-soft);
  color: var(--muted);
  text-align: center;
}

@media (max-width: 980px) {
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .account-grid { grid-template-columns: 1fr; }
}

@media (max-width: 760px) {
  .movement-list {
    border: 0;
    border-radius: 0;
    overflow: visible;
  }

  .movement-table,
  .movement-table tbody {
    display: block;
    width: 100%;
    min-width: 0;
  }

  .movement-table colgroup,
  .movement-table thead {
    display: none;
  }

  .movement-table .movement-record {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 10px;
    padding: 14px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--surface);
  }

  .movement-table .attention-row {
    border-color: color-mix(in srgb, var(--warning) 52%, var(--line));
    background: color-mix(in srgb, var(--warning-soft) 46%, var(--surface));
  }

  .movement-table td {
    display: block;
    min-width: 0;
    padding: 0;
    border: 0;
    text-align: left;
    white-space: normal;
  }

  .movement-table td::before {
    display: block;
    margin-bottom: 4px;
    color: var(--muted);
    font-size: 11px;
    font-weight: 400;
    content: attr(data-label);
  }

  .movement-table .mobile-wide {
    grid-column: 1 / -1;
  }

  .movement-table a,
  .movement-detail > summary {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
  }

  .account-selector { width: 100%; }
  .page-content {
    width: min(calc(100% - 24px), 1320px);
    padding: 16px 0 24px;
  }

  .funds-hero,
  .kpi-grid {
    grid-template-columns: 1fr;
  }

  .funds-hero {
    gap: 13px;
    padding: 19px;
    border-radius: 17px;
  }

  .section-heading,
  .detail-toolbar,
  .account-head {
    align-items: stretch;
    flex-direction: column;
  }

  .detail-switch {
    width: 100%;
  }

  .detail-switch button {
    min-width: 0;
  }

  .account-balance {
    text-align: left;
  }

  .account-metrics {
    grid-template-columns: 1fr;
  }

  .detail-toolbar .control {
    width: 100%;
    max-width: none;
    min-height: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .funds-dashboard { transition: none; }
}
</style>
