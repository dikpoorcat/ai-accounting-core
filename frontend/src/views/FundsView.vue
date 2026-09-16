<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { dashboardErrorMessage, isDashboardSnapshotChanged } from "../api/client";
import {
  fetchFundsDashboard,
  fundAccountDisplayLabel,
  fundAccountDisplayName,
  fundAccountLabel,
  rememberFundAccounts,
  type BankStatementState,
  type FundAccount,
  type FundsData,
  type FundsQuery,
} from "../api/funds";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
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
interface MovementAccountEntry {
  value: string;
  label: string;
  meta: string;
  movementCount: number | null;
}
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
const visibleMovements = computed(() => {
  const movements = funds.value?.movements ?? [];
  if (!selectedAccount.value) return movements;
  return movements.filter(
    (item) => accountKey(item.account_type, item.account_id) === selectedAccount.value,
  );
});
const bankAccounts = computed(
  () => funds.value?.accounts.filter((account) => account.type === "bank") ?? [],
);
const accountOptions = computed(() => {
  const options = (funds.value?.accounts ?? []).map(account => ({ value: accountKey(account.type, account.account_id), label: fundAccountDisplayLabel(account.name, account.code) }));
  if (selectedAccount.value && !options.some(option => option.value === selectedAccount.value)) {
    const separator = selectedAccount.value.indexOf(":");
    options.push({ value: selectedAccount.value, label: fundAccountLabel(queryText("company_id"), selectedPeriod.value, selectedAccount.value.slice(0, separator), selectedAccount.value.slice(separator + 1)) ?? "所选账户（名称尚未加载）" });
  }
  return options;
});
const movementAccountEntries = computed<MovementAccountEntry[]>(() => {
  const entries: MovementAccountEntry[] = (funds.value?.accounts ?? []).map((account) => ({
    value: accountKey(account.type, account.account_id),
    label: fundAccountDisplayName(account.name, account.code),
    meta: `${accountTypeLabel(account.type)}${accountCodeAddsInformation(account.name, account.code) ? ` · ${account.code}` : ""}`,
    movementCount: account.movement_count,
  }));
  if (selectedAccount.value && !entries.some((entry) => entry.value === selectedAccount.value)) {
    entries.unshift({
      value: selectedAccount.value,
      label: accountOptions.value.find((option) => option.value === selectedAccount.value)?.label ?? "所选账户",
      meta: "所选账户 · 完整账户资料正在加载",
      movementCount: null,
    });
  }
  return entries;
});
const visibleMovementCount = computed(
  () => selectedAccount.value
    ? movementAccountEntries.value.find((entry) => entry.value === selectedAccount.value)?.movementCount ?? visibleMovements.value.length
    : funds.value?.movement_count ?? 0,
);
const selectedMovementAccountLabel = computed(
  () => movementAccountEntries.value.find((entry) => entry.value === selectedAccount.value)?.label ?? "全部账户",
);
const bankAccountOptions = computed(() => {
  const options = bankAccounts.value.map(account => ({ value: account.account_id, label: fundAccountDisplayLabel(account.name, account.code) }));
  if (selectedBankAccount.value && !options.some(option => option.value === selectedBankAccount.value)) options.push({ value: selectedBankAccount.value, label: fundAccountLabel(queryText("company_id"), selectedPeriod.value, "bank", selectedBankAccount.value) ?? "所选银行账户（名称尚未加载）" });
  return options;
});
const visibleBankRows = computed(() => {
  const rows = funds.value?.bank_statement.rows ?? [];
  if (!selectedBankAccount.value) return rows;
  return rows.filter((item) => item.account_id === selectedBankAccount.value);
});
const selectedBankAccountLabel = computed(
  () => bankAccountOptions.value.find((option) => option.value === selectedBankAccount.value)?.label ?? "全部银行账户",
);
const visibleBankRowCount = computed(() => {
  if (!selectedBankAccount.value) return funds.value?.bank_statement.transaction_count ?? 0;
  return bankAccounts.value.find((account) => account.account_id === selectedBankAccount.value)?.statement.transaction_count
    ?? visibleBankRows.value.length;
});
const attentionItems = computed(() => {
  if (!funds.value) return [];
  const items: string[] = [];
  for (const account of funds.value.accounts) {
    if (account.negative_balance) {
      items.push(
        `${fundAccountDisplayName(account.name, account.code)}期末账面余额为负数 ${formatFen(account.closing_fen)}。`,
      );
    }
    if (
      ["attention", "pending", "not_configured"].includes(
        account.reconciliation.state,
      )
    ) {
      items.push(`${fundAccountDisplayName(account.name, account.code)}：${account.reconciliation.label}。`);
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
const bankNeedsAttention = computed(() => {
  const statement = funds.value?.bank_statement;
  return Boolean(statement && (
    bankAttentionCount.value ||
    statement.missing_account_count ||
    ["missing", "partial"].includes(statement.coverage_state)
  ));
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
  return {};
}

function selectionKey() {
  return JSON.stringify([route.query.company_id, route.query.period, selectedPeriod.value]);
}
function isCurrent(generation: number, selection: string) { return mounted && generation === requestGeneration && selectionKey() === selection; }
function invalidateRequests() {
  requestGeneration += 1;
  activeRequest?.abort(); cancelPages();
  activeRequest = null;
  snapshotVersion.value = ""; funds.value = null; responsePeriod.value = ""; loading.value = false;
}

function selectMovementAccount(value: string) {
  if (selectedAccount.value === value) return;
  selectedAccount.value = value;
}

/** 摘要卡内“查看核对说明”的内容：只补充摘要结论没有的细节。 */
function bankOwnerNote() {
  const statement = funds.value?.bank_statement;
  if (!statement) return "正在读取本月银行收支。";
  if (statement.coverage_state === "not_applicable") return "本月没有需要查看的公司银行流水。";
  if (statement.missing_account_count) {
    return `还有 ${statement.missing_account_count} 个银行账户未提供本月流水，上述金额仅包含已提供的资料。`;
  }
  if (["missing", "partial"].includes(statement.coverage_state)) {
    return "当前流水资料尚未齐全，上述金额可能不完整；AI 会计正在继续核对。";
  }
  if (bankAttentionCount.value) {
    return `${bankAttentionCount.value} 笔流水由 AI 会计继续核对；如需您补充资料，会另列待办。`;
  }
  if (!statement.transaction_count) return "本月银行流水完整，未发生银行收支。";
  return `本月共 ${statement.transaction_count} 笔银行流水，均已核对。`;
}

/** 摘要卡内的流水结论：先给资料是否齐全，再给是否仍需核对。 */
function bankStatementSummary() {
  const statement = funds.value?.bank_statement;
  if (!statement) return "银行资料尚未读取";
  if (statement.coverage_state === "not_applicable") return "暂无银行流水";
  if (["missing", "partial"].includes(statement.coverage_state)) return "流水资料可能不完整";
  if (!statement.transaction_count) return "本月无银行收支";
  return bankAttentionCount.value ? "部分流水待核对" : "流水已核对";
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
    if (queryText("movement_account_type") || queryText("movement_account_id") || queryText("statement_account_id")) {
      void router.replace({
        query: {
          ...route.query,
          movement_account_type: undefined,
          movement_account_id: undefined,
          statement_account_id: undefined,
        },
        hash: route.hash,
      });
    }
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

function accountTypeLabel(type: FundAccount["type"]): string {
  return {
    bank: "银行账户",
    payment_platform: "支付平台账户",
    cash: "现金账户",
  }[type];
}

function accountCodeAddsInformation(name: string, code: string): boolean {
  return Boolean(code.trim()) && fundAccountDisplayLabel(name, code) !== fundAccountDisplayName(name, code);
}

function accountChangeLabel(value: string): string {
  const amount = fen(value);
  if (amount > 0n) return "本月净增加";
  if (amount < 0n) return "本月净减少";
  return "本月无增减";
}

function accountActivityLabel(account: FundAccount): string {
  if (!account.movement_count) return "本月无账面活动";
  return account.last_activity_date
    ? `最后一笔 ${formatDate(account.last_activity_date)}`
    : "活动日期未提供";
}

function bankConcern(account: FundAccount): string {
  if (account.type !== "bank") return "";
  if (account.statement.coverage_state === "missing") {
    return "本月银行流水尚未提供，暂不能确认账面收支是否完整";
  }
  const parts: string[] = [];
  if (
    account.reconciliation.difference_fen !== undefined &&
    account.reconciliation.difference_fen !== null &&
    fen(account.reconciliation.difference_fen) !== 0n
  ) {
    parts.push(`账面与银行流水相差 ${formatPositiveFen(account.reconciliation.difference_fen)}`);
  }
  if (account.statement.unmatched_count) parts.push(`${account.statement.unmatched_count} 笔流水待匹配`);
  if (account.statement.needs_review_count) parts.push(`${account.statement.needs_review_count} 笔流水待复核`);
  if (account.statement.coverage_state === "partial") parts.push("本月流水覆盖尚未完整确认");
  if (!parts.length && reconciliationAttention(account.reconciliation.state)) {
    parts.push(account.reconciliation.label);
  }
  return parts.join("，");
}

function accountOwnerState(account: FundAccount): { label: string; detail: string; tone: "ok" | "attention" | "neutral" } {
  const bankIssue = bankConcern(account);
  if (account.closing_fen === null) {
    return {
      label: "余额待确认",
      detail: `月初余额依据尚未建立${bankIssue ? `；${bankIssue}` : ""}，请让 AI 会计核对。`,
      tone: "attention",
    };
  }
  if (account.negative_balance) {
    const relatedBankIssue = account.type === "bank" && account.statement.coverage_state === "missing"
      ? "，且本月银行流水未提供"
      : bankIssue
        ? `；${bankIssue}`
        : "";
    return {
      label: "余额需要核查",
      detail: `账面余额为负${relatedBankIssue}；请让 AI 会计核查。`,
      tone: "attention",
    };
  }
  if (bankIssue) {
    return {
      label: account.statement.coverage_state === "missing"
        ? "待补银行流水"
        : account.statement.unmatched_count || account.statement.needs_review_count
          ? "有流水待核对"
          : "本月待对账",
      detail: `${bankIssue}。`,
      tone: "attention",
    };
  }
  if (!account.name.trim() || account.name === "未提供账户名称") {
    return {
      label: "账户名称待补充",
      detail: "请让 AI 会计补充账户名称，方便区分账户和核对流水。",
      tone: "attention",
    };
  }
  if (account.active === false) {
    return {
      label: "账户已停用",
      detail: "卡片保留所选月末余额和当月资金活动。",
      tone: "neutral",
    };
  }
  if (account.type === "bank" && account.reconciliation.state === "complete") {
    return {
      label: "本月已对账",
      detail: account.statement.transaction_count
        ? `${account.statement.matched_count} 笔银行流水已全部与账面匹配。`
        : "完整银行流水已确认，本月无发生。",
      tone: "ok",
    };
  }
  return {
    label: "目前无需处理",
    detail: account.movement_count ? `本月有 ${account.movement_count} 笔账面资金活动。` : "本月没有账面资金活动。",
    tone: "ok",
  };
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

/** 银行流水的收支方向只用于列内提示，避免在摘要列重复整列含义。 */
function bankDirectionLabel(direction: "inflow" | "outflow"): string {
  return direction === "inflow" ? "流入" : "流出";
}

function bankMatchLabel(state: BankStatementState): string {
  return {
    matched: "已匹配",
    unmatched: "待匹配",
    needs_review: "需复核",
  }[state];
}

function bankMatchMark(state: BankStatementState): string {
  return { matched: "✓", unmatched: "!", needs_review: "?" }[state];
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
  () => [route.query.company_id, route.query.period],
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
  () => [context.value, route.query.period] as const,
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
    if (companyChanged || selectedPeriod.value !== target || !funds.value) {
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

watch(
  () => funds.value?.accounts,
  () => {
    if (!funds.value || !selectedAccount.value || movementAccountEntries.value.some((entry) => entry.value === selectedAccount.value)) return;
    selectedAccount.value = movementAccountEntries.value[0]?.value ?? "";
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
      >
        <template #navigation>
          <DashboardSectionNav v-if="funds"
          :items="sectionLinks"
          :active="activeSection"
          label="资金页面区段"

          @select="focusSection"
        />
        </template>
      </DashboardModuleHeader>

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


        <div class="funds-dashboard" :aria-busy="loading">
        <section
          id="funds-overview"
          class="funds-hero section-anchor"
          aria-labelledby="funds-total-label"
          tabindex="-1"
        >
          <p class="dashboard-hero-eyebrow">{{ selectedPeriodLabel }}期末 · 全公司</p>
          <div>

            <span id="funds-total-label">月末账面资金</span>
            <strong class="funds-total dashboard-hero-title">{{ formatFen(funds.total_fen) }}</strong>
            <p class="muted dashboard-hero-note">
              <template v-if="funds.account_count">
                银行 {{ formatFen(funds.bank_fen) }} · 支付平台
                {{ formatFen(funds.payment_platform_fen) }} · 现金
                {{ formatFen(funds.cash_fen) }} · 共 {{ funds.account_count }} 个资金账户
              </template>
              <template v-else>本月尚无公司资金账户及资金活动</template>
            </p>
          </div>
          <div class="reconciliation" :class="{ attention: bankNeedsAttention }">
            <span>银行流水与账面记录</span>
            <strong>{{ bankStatementSummary() }}</strong>
            <details class="reconciliation-details">
              <summary>查看核对说明</summary>
              <p>已匹配流水 {{ funds.bank_statement.matched_count }} / {{ funds.bank_statement.transaction_count }} 笔</p>
              <p>{{ bankOwnerNote() }}</p>
            </details>
          </div>
          <div class="flow-panel">
          <dl class="flow-summary">
            <div><dt>本月实际收款</dt><dd>{{ formatFen(funds.inflow_fen) }}</dd></div>
            <div><dt>本月实际付款</dt><dd>{{ formatFen(funds.outflow_fen) }}</dd></div>
            <div><dt>月初账面资金</dt><dd>{{ formatFen(funds.opening_fen) }}</dd></div>
            <div><dt>本月资金增减</dt><dd :class="{ loss: fen(funds.net_change_fen) < 0n }">{{ formatSigned(funds.net_change_fen) }}</dd></div>
          </dl>
          <p class="flow-note">实际收付款不含公司账户间互转</p>
          </div>
        </section>

        <section v-if="funds.fact_issues?.length" class="historical-source-issues" aria-label="历史独立采用说明">
          <p role="status">{{ funds.fact_issues.length }} 组历史资金来源尚不能证明独立封存采用。具体金额与流水核对状态分别见对应区块；作为其他结果的来源，不等于已独立采用。</p>
          <p class="muted">以下保留所选月末独立采用尚未证明的来源与候选，与当前跟进状态分别列示。</p>
          <details v-for="(issue, issueIndex) in funds.fact_issues" :key="issueIndex">
            <summary>查看第 {{ issueIndex + 1 }} 组历史来源与精确候选</summary>
            <p>候选只供核对，不作为已采用金额累计。</p>
            <details><summary>未建立原因与原始来源标识</summary><pre>{{ JSON.stringify(issue, null, 2) }}</pre></details>
          </details>
        </section>
        <section
          id="fund-accounts"
          class="panel section-panel section-anchor"
          aria-labelledby="fund-accounts-title"
          tabindex="-1"
        >
          <div class="section-heading">
            <div>
              <h2 id="fund-accounts-title">公司资金账户</h2>
              <p class="list-caption">{{ funds.account_count }} 个账户 · 期末 {{ formatFen(funds.total_fen) }}</p>
            </div>
          </div>
          <div v-if="funds.accounts.length" class="account-grid">
            <article
              v-for="account in funds.accounts"
              :key="accountKey(account.type, account.account_id)"
              class="account-card dashboard-record-card"
              :class="{ attention: accountOwnerState(account).tone === 'attention' }"
            >
              <div class="account-card-summary">
                <div class="account-card-topline">
                  <span class="account-classification">
                    {{ accountTypeLabel(account.type) }}
                    <template v-if="accountCodeAddsInformation(account.name, account.code)"> · {{ account.code }}</template>
                  </span>
                </div>
                <div class="account-head">
                  <div class="account-name">
                    <h3>{{ fundAccountDisplayName(account.name, account.code) }}</h3>
                    <span class="account-owner-state" :class="accountOwnerState(account).tone">
                      {{ accountOwnerState(account).label }}
                    </span>
                    <p>{{ accountOwnerState(account).detail }}</p>
                  </div>
                  <div class="account-balance" :class="{ unknown: account.closing_fen === null }">
                    <span>所选月末账面余额</span>
                    <strong :class="{ loss: account.negative_balance }">{{ formatFen(account.closing_fen) }}</strong>
                    <small :class="{ loss: fen(account.net_change_fen) < 0n }">
                      {{ accountChangeLabel(account.net_change_fen) }}
                      <template v-if="fen(account.net_change_fen) !== 0n"> {{ formatSigned(account.net_change_fen) }}</template>
                    </small>
                  </div>
                </div>
                <div class="account-owner-grid">
                  <div>
                    <span>本月账面流入</span>
                    <strong>{{ formatFen(account.inflow_fen) }}</strong>
                  </div>
                  <div>
                    <span>本月账面流出</span>
                    <strong>{{ formatFen(account.outflow_fen) }}</strong>
                  </div>
                  <div>
                    <span>资金活动</span>
                    <strong>{{ account.movement_count }} 笔</strong>
                    <small>{{ accountActivityLabel(account) }}</small>
                  </div>
                </div>
                <p class="account-flow-scope">账户流入、流出包含公司账户之间划转</p>
              </div>
            </article>
          </div>
          <p v-else class="empty">本月暂无已入账的公司资金账户。</p>
          <DashboardPagination compact item-label="个账户" :page="funds.collections.accounts?.page" :loaded="funds.accounts.length" :loading="pageStates.accounts.loading" :error="pageStates.accounts.error" @more="loadMore('accounts')" @retry="loadMore('accounts')" />
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
          <DashboardPagination compact item-label="个产品" :page="funds.collections.investment_products?.page" :loaded="funds.investments.products.length" :loading="pageStates.investment_products.loading" :error="pageStates.investment_products.error" @more="loadMore('investment_products')" @retry="loadMore('investment_products')" />
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
          <DashboardPagination compact item-label="条明细" :page="funds.collections.investment_events?.page" :loaded="funds.investments.events.length" :loading="pageStates.investment.loading" :error="pageStates.investment.error" @more="loadMore('investment')" @retry="loadMore('investment')" />
          </details>
        </section>

        <details v-if="attentionItems.length || funds.attention_account_count" id="funds-attention" class="funds-review section-anchor" tabindex="-1">
          <summary>
            <span class="review-mark" aria-hidden="true">!</span>
            <strong>资金核对</strong>
            <span>{{ funds.attention_account_count ? `${funds.attention_account_count} 个账户需核对` : '银行流水需核对' }}</span>
            <span class="review-action">查看事项 <span aria-hidden="true">⌄</span></span>
          </summary>
          <div class="review-content">
            <p class="muted">账户问题仅包含已加载 {{ funds.accounts.length }} 个账户；流水覆盖与待匹配数量为全公司范围，两类数量不相加。</p>
            <ul class="attention-list"><li v-for="(item, index) in attentionItems" :key="`${index}-${item}`">{{ item }}</li></ul>
          </div>
        </details>

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
              <p class="list-caption">
                <strong>{{ selectedDetailView === "book"
                  ? `${selectedMovementAccountLabel} · ${loading || pageStates.book.loading ? "正在读取…" : `${visibleMovementCount} 笔资金变动`}`
                  : `${selectedBankAccountLabel} · ${loading || pageStates.bank.loading ? "正在读取…" : `${visibleBankRowCount} 笔银行流水`}` }}</strong>
                <template v-if="funds.collections.accounts?.page.has_more"> · 账户选项已加载 {{ funds.accounts.length }} / {{ funds.account_count }}</template>
              </p>
            </div>
            <div class="heading-controls">
              <label v-if="selectedDetailView === 'bank'" class="account-filter">
                <select
                  v-model="selectedBankAccount"
                  class="control"
                  aria-label="筛选银行流水账户"
                >
                  <option value="">全部银行账户</option>
                  <option v-for="option in bankAccountOptions" :key="option.value" :value="option.value">
                    {{ option.label }}
                  </option>
                </select>
              </label>
              <button v-if="selectedDetailView === 'bank' && funds.collections.accounts?.page.has_more" class="control" :disabled="pageStates.accounts.loading" @click="loadMore('accounts')">{{ pageStates.accounts.loading ? '正在加载账户…' : pageStates.accounts.error ? '重试加载账户' : '继续加载账户选项' }}</button>
              <span v-if="selectedDetailView === 'bank' && pageStates.accounts.error" role="alert">{{ pageStates.accounts.error }}</span>
              <div class="view-switch" role="tablist" aria-label="选择资金明细口径">
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
                  按业务
                </button>
                <button
                  id="fund-detail-tab-bank"
                  type="button"
                  role="tab"
                  :aria-selected="selectedDetailView === 'bank'"
                  aria-controls="fund-detail-panel-bank"
                  aria-label="按流水"
                  :tabindex="selectedDetailView === 'bank' ? 0 : -1"
                  @click="selectDetailView('bank')"
                  @keydown="handleDetailTabKey($event, 1)"
                >
                  按流水
                </button>
              </div>
            </div>
          </div>
          <div class="fund-detail-panels">
          <div
            id="fund-detail-panel-book"
            class="fund-detail-panel"
            :class="{ 'is-active': selectedDetailView === 'book' }"
            role="tabpanel"
            aria-labelledby="fund-detail-tab-book"
            :aria-hidden="selectedDetailView === 'book' ? undefined : 'true'"
            :inert="selectedDetailView !== 'book'"
            :tabindex="selectedDetailView === 'book' ? 0 : -1"
          >
            <div class="fund-business-workbench">
              <nav class="fund-account-index" aria-label="资金账户">
                <span class="fund-account-heading">资金账户</span>
                <button
                  type="button"
                  title="全部账户 · 查看全部已缓存资金变动"
                  :aria-current="selectedAccount === '' ? 'true' : undefined"
                  @click="selectMovementAccount('')"
                >
                  <span class="fund-account-copy">
                    <strong>全部账户</strong>
                    <small>{{ funds.account_count }} 个资金账户</small>
                  </span>
                  <b>{{ funds.movement_count }} 笔</b>
                </button>
                <button
                  v-for="account in movementAccountEntries"
                  :key="account.value"
                  type="button"
                  :title="`${account.label} · ${account.meta}`"
                  :aria-current="selectedAccount === account.value ? 'true' : undefined"
                  @click="selectMovementAccount(account.value)"
                >
                  <span class="fund-account-copy">
                    <strong>{{ account.label }}</strong>
                    <small>{{ account.meta }}</small>
                  </span>
                  <b>{{ account.movementCount === null ? `${visibleMovementCount} 笔` : `${account.movementCount} 笔` }}</b>
                </button>
                <div v-if="funds.collections.accounts?.page.has_more || pageStates.accounts.error" class="fund-account-more">
                  <button type="button" :disabled="pageStates.accounts.loading" @click="loadMore('accounts')">
                    {{ pageStates.accounts.loading ? '正在加载账户…' : pageStates.accounts.error ? '重试加载账户' : '继续加载账户选项' }}
                  </button>
                  <span v-if="pageStates.accounts.error" role="alert">{{ pageStates.accounts.error }}</span>
                </div>
              </nav>

              <div class="fund-business-detail" :aria-label="`${selectedMovementAccountLabel}资金明细`" :aria-busy="pageStates.book.loading" aria-live="polite">
            <div v-if="visibleMovements.length" class="book-activity-feed" role="region" aria-label="账面资金明细" tabindex="0">
              <div class="book-list-columns" aria-hidden="true">
                <span>日期</span><span>业务与对象</span><span>方向</span><span class="book-column-number">金额</span><span class="book-column-action">凭证</span>
              </div>
              <ol class="book-activity-list">
                <li v-for="item in visibleMovements" :key="item.id" class="book-activity-row">
                  <time :datetime="item.date || undefined">{{ formatDate(item.date) }}</time>
                  <div class="book-movement-copy">
                    <strong>{{ item.list_summary || item.type }}</strong>
                    <small>{{ item.party || "无需往来对象" }}<template v-if="item.internal_transfer"> · 账户互转</template></small>
                  </div>
                  <span class="direction" :class="item.direction">
                    {{ item.internal_transfer ? (item.direction === "inflow" ? "转入" : "转出") : item.direction === "inflow" ? "流入" : "流出" }}
                  </span>
                  <strong class="book-movement-amount" :class="item.direction">{{ movementAmount(item.direction, item.amount_fen) }}</strong>
                  <span v-if="voucherTarget(item.reference, responsePeriod)" class="book-voucher-link">
                    <RouterLink
                      class="book-voucher-button"
                      :to="voucherTarget(item.reference, responsePeriod)!"
                      :aria-label="`打开凭证 ${item.reference}：${item.list_summary || item.type}`"
                      :aria-describedby="`fund-voucher-preview-${item.id}`"
                    >
                      凭证 {{ item.reference }}
                    </RouterLink>
                    <span :id="`fund-voucher-preview-${item.id}`" class="book-voucher-preview" role="tooltip">
                      <span class="book-preview-heading">
                        <span>
                          <small>凭证 {{ item.reference }} · {{ formatDate(item.date) }}</small>
                          <strong>{{ item.list_summary || item.type }}</strong>
                        </span>
                        <b>{{ movementAmount(item.direction, item.amount_fen) }}</b>
                      </span>
                      <span class="book-preview-lines">
                        <span>
                          <span>{{ fundAccountDisplayName(item.account_name, item.account_code) }}<template v-if="accountCodeAddsInformation(item.account_name, item.account_code)"> · {{ item.account_code }}</template></span>
                          <strong>{{ item.direction === "inflow" ? "增加" : "减少" }}</strong>
                        </span>
                        <span><span>{{ item.party || "无需往来对象" }}</span><strong>{{ item.internal_transfer ? "账户互转" : item.direction === "inflow" ? "收款" : "付款" }}</strong></span>
                      </span>
                      <span class="book-preview-footer">
                        <span class="direction" :class="item.direction">{{ item.direction === "inflow" ? "流入" : "流出" }}</span>
                        <small>点击打开凭证详情</small>
                      </span>
                    </span>
                  </span>
                  <span v-else class="book-voucher-missing">凭证未定位</span>
                </li>
              </ol>
            </div>
            <p v-else class="empty">{{ loading || pageStates.book.loading ? "正在读取资金明细…" : selectedAccount ? "该账户本月没有已入账资金变动。" : "本月没有已入账资金变动。" }}</p>
            <DashboardPagination compact item-label="笔变动" :page="funds.collections.movements?.page" :loaded="funds.movements.length" :loading="pageStates.book.loading" :error="pageStates.book.error" @more="loadMore('book')" @retry="loadMore('book')" />
              </div>
            </div>
          </div>

          <div
            id="fund-detail-panel-bank"
            class="fund-detail-panel"
            :class="{ 'is-active': selectedDetailView === 'bank' }"
            role="tabpanel"
            aria-labelledby="fund-detail-tab-bank"
            :aria-hidden="selectedDetailView === 'bank' ? undefined : 'true'"
            :inert="selectedDetailView !== 'bank'"
            :tabindex="selectedDetailView === 'bank' ? 0 : -1"
          >
            <div v-if="visibleBankRows.length" class="bank-activity-feed" role="region" aria-label="银行流水明细" tabindex="0">
              <ol class="bank-activity-list">
                <li v-for="item in visibleBankRows" :key="item.id" class="bank-activity-item" :class="{ attention: item.state !== 'matched' }">
                  <details class="bank-activity-record">
                    <summary class="bank-activity-summary">
                      <time class="bank-activity-date" :datetime="item.date || undefined">{{ formatDate(item.date) }}</time>
                      <span class="bank-activity-account">
                        <strong>{{ fundAccountDisplayName(item.account_name, item.account_code) }}</strong>
                        <small v-if="accountCodeAddsInformation(item.account_name, item.account_code)">{{ item.account_code }}</small>
                      </span>
                      <span class="bank-activity-main">
                        <strong>{{ item.memo || item.party || "用途未提供" }}</strong>
                        <small>{{ item.memo ? item.party || "对方名称未提供" : "摘要未提供，仅保留对方名称" }}</small>
                      </span>
                      <span class="bank-match-status" :class="item.state">
                        <span class="bank-match-mark" aria-hidden="true">{{ bankMatchMark(item.state) }}</span>
                        {{ bankMatchLabel(item.state) }}
                      </span>
                      <span class="bank-activity-amount" :class="item.direction">
                        <small class="bank-amount-direction">{{ bankDirectionLabel(item.direction) }}</small>
                        <strong>{{ movementAmount(item.direction, item.amount_fen) }}</strong>
                      </span>
                      <span class="bank-record-chevron" aria-hidden="true"></span>
                    </summary>
                    <div class="bank-record-detail">
                      <dl class="bank-record-fields">
                        <div>
                          <dt>交易日期</dt>
                          <dd>{{ formatDate(item.date) }}</dd>
                        </div>
                        <div>
                          <dt>银行账户</dt>
                          <dd>{{ fundAccountDisplayName(item.account_name, item.account_code) }}</dd>
                        </div>
                        <div>
                          <dt>交易用途</dt>
                          <dd>{{ item.memo || "用途未提供" }}</dd>
                        </div>
                        <div>
                          <dt>对方名称</dt>
                          <dd>{{ item.party || "对方名称未提供" }}</dd>
                        </div>
                        <div>
                          <dt>收支金额</dt>
                          <dd class="bank-record-amount" :class="item.direction">{{ movementAmount(item.direction, item.amount_fen) }}</dd>
                        </div>
                        <div>
                          <dt>流水编号</dt>
                          <dd class="bank-record-reference">{{ item.reference || "编号未提供" }}</dd>
                        </div>
                        <div v-if="item.source_check?.message">
                          <dt>核对说明</dt>
                          <dd>{{ item.source_check.message }}</dd>
                        </div>
                      </dl>
                      <section v-if="item.batch_payment" class="bank-batch-detail" aria-label="整批付款逐项明细">
                        <p class="bank-batch-heading">
                          <span class="bank-batch-heading-title">整批付款明细 · {{ item.batch_payment.items.length }} 项</span>
                          <span class="bank-batch-heading-meta">{{ item.batch_payment.bank_row_count }} 笔银行流水 · 批次合计 {{ formatFen(item.batch_payment.total_fen) }}</span>
                        </p>
                        <p v-if="item.batch_payment.bank_row_count > 1" class="bank-batch-scope">
                          以下逐项金额属于整个付款批次。现有银行资料没有逐项对应到本条流水，不能据此把某位收款人归到本条。
                        </p>
                        <ul class="bank-batch-items">
                          <li v-for="(allocation, index) in item.batch_payment.items" :key="`${item.id}-batch-${index}`">
                            <span class="bank-batch-index" aria-hidden="true">{{ index + 1 }}</span>
                            <strong>{{ allocation.party }}</strong>
                            <b>{{ formatFen(allocation.amount_fen) }}</b>
                          </li>
                        </ul>
                      </section>
                    </div>
                  </details>
                </li>
              </ol>
            </div>
            <p v-else class="empty">{{ loading ? "正在读取银行流水…" : selectedBankAccount ? "该账户本月没有已提供的银行流水。" : "本月没有已提供的银行流水。" }}</p>
            <button v-if="!visibleBankRows.length && selectedBankAccount" class="control" @click="selectedBankAccount = ''">清除账户筛选</button>
            <DashboardPagination compact item-label="笔流水" :page="funds.collections.statements?.page" :loaded="funds.bank_statement.rows.length" :loading="pageStates.bank.loading" :error="pageStates.bank.error" @more="loadMore('bank')" @retry="loadMore('bank')" />
          </div>
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
  min-height: 0;
}

.panel,
.state-panel {
  min-width: 0;
  border: 1px solid var(--line);
  background: var(--surface);
}

.panel {
  border-radius: var(--radius-panel);
}

.state-panel {
  display: grid;
  gap: 7px;
  padding: 28px;
  border-radius: var(--radius-panel);
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
  border-radius: var(--radius-control);
  background: var(--accent);
  color: var(--surface);
  cursor: pointer;
}

.error-state {
  border-color: var(--danger);
}

.funds-hero {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px 40px;
  min-height: 198px;
  padding: 25px 28px;
  border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line));
  border-radius: 20px;
  background:
    radial-gradient(circle at 7% 12%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 32%),
    linear-gradient(125deg, var(--surface), color-mix(in srgb, var(--accent-soft) 66%, var(--surface)));
}

.funds-hero > div > span {
  color: var(--muted);
  font-size: 12px;
  font-weight: 750;
}

.funds-total {
  color: var(--accent);
}

.muted {
  color: var(--muted);
}

.funds-hero .muted {
  margin: 8px 0 0;
  font-size: 12px;
}

.loss {
  color: var(--danger);
}

.investment-details {
  margin-top: 14px;
}

summary {
  cursor: pointer;
  color: var(--muted);
}

.investment-details > summary {
  padding: 10px 0;
}

.section-panel {
  padding: 0;

  border: 0;

  background: transparent;

  margin-top: 22px;
}

.section-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 14px;
}

/* 标题下带小字的标题行，与下方列表/卡片保持 16px 间距（三页一致）。 */
.section-heading:has(.list-caption) {
  margin-bottom: 16px;
}

.section-heading h2 {
  margin: 0;
  font-size: 20px;
}

.detail-heading {
  align-items: flex-end;
  margin-bottom: 14px;
}

/* 口径切换按经营简报 .view-switch 的样式；资金明细标题下只保留一行计数小字。 */
.heading-controls {
  display: flex;
  flex: none;
  align-items: center;
  gap: 12px;
}

.view-switch {
  display: grid;
  flex: none;
  grid-template-columns: repeat(2, 1fr);
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--surface-soft);
}

.view-switch button {
  min-height: 34px;
  padding: 0 13px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  font: inherit;
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
}

.view-switch button[aria-selected="true"] {
  background: var(--surface);
  color: var(--text);
}

.view-switch button:focus-visible,
[role="tabpanel"]:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* 标题与小字同属左侧一组，结构对齐简报页“本月发生了什么 / 27 张凭证 · 7 类业务”。 */
.list-caption {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.5;
}

.list-caption strong {
  font-weight: inherit;
}

.account-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.attention-panel {
  border-color: color-mix(in srgb, var(--warning) 52%, var(--line));
}

.attention-list {
  display: grid;
  gap: 8px;
  margin: 0;
  padding-left: 21px;
}

.account-filter {
  display: block;
}

.fund-detail-panels {
  display: grid;
  min-width: 0;
  align-items: stretch;
}

.fund-detail-panel {
  grid-area: 1 / 1;
  min-width: 0;
  visibility: hidden;
  pointer-events: none;
}

.fund-detail-panel.is-active {
  z-index: 1;
  visibility: visible;
  pointer-events: auto;
}

#fund-detail-panel-book {
  display: grid;
}

#fund-detail-panel-book > .fund-business-workbench {
  height: 100%;
}

.fund-business-workbench,
#fund-detail-panel-bank {
  min-height: 480px;
}

.fund-business-workbench {
  display: grid;
  min-width: 0;
  grid-template-columns: 280px minmax(0, 1fr);
  align-items: stretch;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
}

.fund-account-index {
  display: grid;
  min-width: 0;
  align-content: start;
  gap: 3px;
  padding: 12px 10px;
  border-right: 1px solid var(--line);
  background: var(--surface);
}

.fund-account-heading {
  display: flex;
  min-height: 30px;
  align-items: center;
  padding: 0 12px 7px;
  color: var(--muted);
  font-size: 11px;
  letter-spacing: 0.04em;
}

.fund-account-index > button {
  position: relative;
  display: grid;
  width: 100%;
  min-width: 0;
  min-height: 46px;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 5px 10px;
  align-items: center;
  padding: 11px 12px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: transparent;
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 140ms ease, border-color 140ms ease;
}

.fund-account-index > button::before {
  position: absolute;
  top: 12px;
  bottom: 12px;
  left: -1px;
  width: 2px;
  border-radius: 999px;
  background: transparent;
  content: "";
}

.fund-account-index > button:hover {
  background: var(--surface-soft);
}

.fund-account-index > button[aria-current="true"] {
  border-color: color-mix(in srgb, var(--accent) 16%, transparent);
  background: color-mix(in srgb, var(--accent-soft) 54%, var(--surface));
}

.fund-account-index > button[aria-current="true"]::before {
  background: var(--accent);
}

.fund-account-index > button:focus-visible,
.fund-account-more button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.fund-account-copy {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.fund-account-copy strong,
.fund-account-copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fund-account-copy strong {
  font-size: 14px;
  font-weight: 600;
  line-height: 1.5;
}

.fund-account-index > button[aria-current="true"] .fund-account-copy strong {
  font-weight: 750;
}

.fund-account-copy small {
  color: var(--muted);
  font-size: 11px;
}

.fund-account-index > button > b {
  min-width: 31px;
  padding: 1px 5px;
  border-radius: 5px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 600;
  text-align: center;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.fund-account-index > button[aria-current="true"] > b {
  background: var(--surface);
  color: var(--accent);
}

.fund-account-more {
  display: grid;
  gap: 6px;
  padding: 8px 12px 2px;
}

.fund-account-more button {
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--accent);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.fund-account-more button:disabled {
  color: var(--muted);
  cursor: default;
  opacity: 0.6;
}

.fund-account-more span {
  color: var(--danger);
  font-size: 11px;
  line-height: 1.45;
}

.fund-business-detail {
  min-width: 0;
  padding: 12px 20px;
}

.fund-business-detail > .empty {
  margin-top: 4px;
}

.book-activity-feed {
  --book-list-columns: 88px minmax(220px, 1fr) 62px minmax(122px, auto) 86px;
  min-width: 0;
}

.book-activity-feed:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.book-list-columns,
.book-activity-row {
  display: grid;
  grid-template-columns: var(--book-list-columns);
  gap: 12px;
  align-items: center;
}

.book-list-columns {
  min-height: 34px;
  padding: 0 4px 8px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 11px;
}

.book-column-number,
.book-column-action {
  text-align: right;
}

.book-activity-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.book-activity-row {
  position: relative;
  min-height: 76px;
  padding: 14px 4px;
  transition: background-color 140ms ease;
}

.book-activity-row + .book-activity-row {
  border-top: 1px solid var(--line);
}

.book-activity-row:hover,
.book-activity-row:focus-within {
  z-index: 4;
  background: var(--surface-soft);
}

.book-activity-row > time {
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.book-movement-copy {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.book-movement-copy strong,
.book-movement-copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.book-movement-copy small {
  color: var(--muted);
  font-size: 11px;
}

.book-movement-copy > strong {
  font-size: 14px;
}

.book-activity-row > .direction {
  justify-self: start;
}

.book-movement-amount {
  justify-self: end;
  font-size: 14px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.book-movement-amount.inflow {
  color: var(--accent);
}

.book-movement-amount.outflow {
  color: var(--warning);
}

.book-voucher-link {
  position: relative;
  justify-self: end;
}

.book-voucher-button {
  display: inline-flex;
  min-height: 32px;
  align-items: center;
  padding: 0 9px;
  border-radius: 8px;
  color: var(--accent);
  font-size: 11px;
  font-weight: 750;
  text-decoration: none;
  white-space: nowrap;
}

.book-voucher-button:hover,
.book-voucher-button:focus-visible {
  background: var(--accent-soft);
  outline: none;
}

.book-voucher-button:focus-visible {
  box-shadow: 0 0 0 2px var(--accent);
}

.book-voucher-missing {
  justify-self: end;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
}

.book-voucher-preview {
  position: absolute;
  top: 50%;
  right: calc(100% + 10px);
  z-index: 30;
  display: grid;
  width: min(380px, calc(100vw - 48px));
  gap: 10px;
  padding: 13px;
  border: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line));
  border-radius: 12px;
  background: var(--surface);
  box-shadow: var(--shadow-overlay);
  opacity: 0;
  color: var(--text);
  pointer-events: none;
  text-align: left;
  transform: translate(8px, -50%);
  transition: opacity 140ms ease, transform 140ms ease, visibility 140ms ease;
  visibility: hidden;
}

.book-voucher-preview::after {
  position: absolute;
  top: calc(50% - 5px);
  right: -6px;
  width: 10px;
  height: 10px;
  border-top: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line));
  border-right: 1px solid color-mix(in srgb, var(--accent) 20%, var(--line));
  background: var(--surface);
  content: "";
  transform: rotate(45deg);
}

.book-voucher-link:hover .book-voucher-preview,
.book-voucher-link:focus-within .book-voucher-preview {
  opacity: 1;
  transform: translate(0, -50%);
  visibility: visible;
}

.book-preview-heading,
.book-preview-footer,
.book-preview-lines > span {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.book-preview-heading > span {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.book-preview-heading small,
.book-preview-footer small {
  color: var(--muted);
  font-size: 10px;
}

.book-preview-heading strong {
  overflow-wrap: anywhere;
  font-size: 13px;
}

.book-preview-heading b {
  flex: none;
  font-size: 14px;
  white-space: nowrap;
}

.book-preview-lines {
  display: grid;
  gap: 5px;
  padding: 8px 9px;
  border-radius: 8px;
  background: var(--surface-soft);
}

.book-preview-lines > span {
  align-items: flex-start;
  min-width: 0;
  color: var(--muted);
  font-size: 11px;
}

.book-preview-lines > span > span {
  min-width: 0;
  overflow-wrap: anywhere;
}

.book-preview-lines strong {
  flex: none;
  color: var(--text);
  font-size: 11px;
  white-space: nowrap;
}

.book-preview-footer > .direction {
  min-height: 20px;
  padding: 1px 6px;
  font-size: 10px;
}

/* 摘要卡内的银行流水核对结论，位置与资产页的核对区块对应。 */
.reconciliation {
  display: grid;
  align-content: start;
  align-self: stretch;
  gap: 4px;
  min-width: 0;
}

.reconciliation > span {
  color: var(--muted);
  font-size: 11px;
}

.reconciliation > strong {
  color: var(--accent);
  font-size: 20px;
  line-height: 1.25;
}

.reconciliation.attention > strong {
  color: var(--warning);
}

.reconciliation-details {
  min-width: 0;
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.reconciliation-details summary {
  color: var(--accent);
  font-size: 12px;
  font-weight: 750;
  cursor: pointer;
}

.reconciliation-details p {
  margin: 5px 0 0;
  font-variant-numeric: tabular-nums;
}


.bank-activity-feed {
  --bank-list-columns: 92px minmax(126px, 0.85fr) minmax(230px, 1.7fr) 96px minmax(150px, auto);
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
}

.bank-activity-feed:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* 与经营简报“按凭证查看”的 .voucher-row 保持同一行高与内边距。 */
.bank-activity-summary {
  position: relative;
  display: grid;
  grid-template-columns: var(--bank-list-columns);
  gap: 14px;
  align-items: center;
  min-height: 62px;
  padding: 10px 44px 10px 16px;
  list-style: none;
  color: var(--text);
  cursor: pointer;
  transition: background-color 140ms ease;
}

.bank-activity-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.bank-activity-item {
  position: relative;
}

.bank-activity-item + .bank-activity-item {
  border-top: 1px solid var(--line);
}

.bank-activity-item.attention > .bank-activity-record {
  box-shadow: inset 3px 0 var(--warning);
}

.bank-activity-record {
  background: var(--surface);
}

.bank-activity-summary::-webkit-details-marker {
  display: none;
}

.bank-activity-summary:hover,
.bank-activity-summary:focus-visible,
.bank-activity-record[open] > .bank-activity-summary {
  background: color-mix(in srgb, var(--accent-soft) 34%, var(--surface));
}

.bank-activity-item.attention .bank-activity-summary {
  background: color-mix(in srgb, var(--warning-soft) 24%, var(--surface));
}

.bank-activity-item.attention .bank-activity-summary:hover,
.bank-activity-item.attention .bank-activity-summary:focus-visible,
.bank-activity-item.attention .bank-activity-record[open] > .bank-activity-summary {
  background: color-mix(in srgb, var(--warning-soft) 48%, var(--surface));
}

.bank-activity-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.bank-activity-date {
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.bank-activity-main {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.bank-activity-account {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.bank-activity-account strong,
.bank-activity-account small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bank-activity-account strong {
  font-size: 12px;
}

.bank-activity-account small {
  color: var(--muted);
  font-size: 11px;
}

.bank-activity-main > strong {
  overflow: hidden;
  font-size: 13.5px;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bank-activity-main > small {
  overflow: hidden;
  color: var(--muted);
  font-size: 10.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bank-match-status {
  display: inline-flex;
  width: fit-content;
  min-height: 22px;
  align-items: center;
  gap: 5px;
  padding: 2px 9px 2px 4px;
  border-radius: 999px;
  background: var(--surface-soft);
  color: var(--muted);
  font-size: 10px;
  font-weight: 780;
  white-space: nowrap;
}

.bank-match-mark {
  display: inline-grid;
  width: 16px;
  height: 16px;
  flex: none;
  place-items: center;
  border-radius: 50%;
  background: color-mix(in srgb, currentcolor 14%, transparent);
  font-size: 9.5px;
  font-weight: 850;
}

.bank-match-status.matched {
  background: var(--accent-soft);
  color: var(--accent);
}

.bank-match-status.unmatched {
  background: var(--warning-soft);
  color: var(--warning);
}

.bank-match-status.needs_review {
  background: var(--info-soft);
  color: var(--info);
}

.bank-record-chevron {
  position: absolute;
  top: calc(50% - 5px);
  right: 18px;
  width: 7px;
  height: 7px;
  border-right: 1.5px solid var(--muted);
  border-bottom: 1.5px solid var(--muted);
  transform: rotate(45deg);
  transition: transform 140ms ease;
}

.bank-activity-record[open] .bank-record-chevron {
  transform: rotate(225deg) translate(-1px, -1px);
}

.bank-record-detail {
  padding: 12px 16px 14px 34px;
  border-top: 1px solid var(--line);
  background: var(--surface-soft);
}

/* 展开详情按字段列表排列，不再使用卡片分区。 */
.bank-record-fields {
  margin: 0;
}

.bank-record-fields > div {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: 12px;
  align-items: baseline;
  padding: 7px 0;
}

.bank-record-fields > div + div {
  border-top: 1px dashed color-mix(in srgb, var(--line) 72%, transparent);
}

.bank-record-fields dt {
  color: var(--muted);
  font-size: 11px;
}

.bank-record-fields dd {
  margin: 0;
  overflow-wrap: anywhere;
  font-size: 12px;
  line-height: 1.5;
}

.bank-record-amount {
  font-weight: 720;
  font-variant-numeric: tabular-nums;
}

.bank-record-amount.inflow {
  color: var(--accent);
}

.bank-record-amount.outflow {
  color: var(--warning);
}

.bank-record-reference {
  font-variant-numeric: tabular-nums;
  word-break: break-all;
}

.bank-batch-detail {
  margin-top: 12px;
  padding: 10px 0 0;
  border-top: 1px solid var(--line);
}

.bank-batch-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 14px;
  margin: 0;
}

.bank-batch-heading-title {
  font-size: 12px;
  font-weight: 780;
}

.bank-batch-heading-meta {
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.bank-batch-scope {
  margin: 5px 0 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.55;
}

.bank-batch-items {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 7px;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}

.bank-batch-items li {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  min-width: 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--surface-soft);
}

.bank-batch-items li > strong {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 12px;
}

.bank-batch-items li > b {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.bank-batch-index {
  display: inline-grid;
  width: 20px;
  height: 20px;
  place-items: center;
  border-radius: 50%;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 10px;
  font-weight: 780;
}

.bank-activity-amount {
  display: grid;
  min-width: 0;
  justify-items: end;
  gap: 2px;
  text-align: right;
  white-space: nowrap;
}

.bank-activity-amount .bank-amount-direction {
  color: var(--muted);
  font-size: 10px;
  font-weight: 780;
  letter-spacing: 0.08em;
}

.bank-activity-amount strong {
  font-size: 15.5px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}

.bank-activity-amount.inflow .bank-amount-direction {
  color: color-mix(in srgb, var(--accent) 78%, var(--muted));
}

.bank-activity-amount.outflow .bank-amount-direction {
  color: color-mix(in srgb, var(--warning) 82%, var(--muted));
}

.bank-activity-amount.inflow strong {
  color: var(--accent);
}

.bank-activity-amount.outflow strong {
  color: var(--warning);
}

.control {
  min-height: 38px;
  padding: 0 11px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
  background: var(--surface);
  color: var(--text);
}

.table-wrap {
  background: var(--surface);
  min-width: 0;
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.account-selector { display: grid; min-width: 0; gap: 6px; }
.account-selector select { max-width: 100%; }

.investment-table {
  table-layout: auto;
}

.date-column { width: 90px; }
.reference-column { width: 84px; }
.investment-amount-column { width: 140px; }
.investment-summary-table { min-width: 500px; }
.investment-cost-table { min-width: 920px; }
.investment-events-table { min-width: 1040px; }

.table-wrap:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.investment-table td {
  overflow-wrap: anywhere;
}

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
.direction.outflow { color: var(--warning); background: var(--warning-soft); }

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
  border-radius: var(--radius-control);
  background: var(--surface-soft);
  color: var(--muted);
  text-align: center;
}

@media (max-width: 980px) {
  .account-grid { grid-template-columns: 1fr; }
  .funds-hero { grid-template-columns: 1fr; }
}

@media (max-width: 760px) {
  .account-grid { grid-template-columns: 1fr; }
  .account-selector { width: 100%; }
  .page-content {
    width: min(calc(100% - 24px), 1320px);
    padding: 16px 0 24px;
  }

  .funds-hero {
    grid-template-columns: 1fr;
  }

  .funds-hero {
    gap: 13px;
    padding: 19px;
    border-radius: 17px;
  }

  .section-heading,
  .account-head {
    align-items: stretch;
    flex-direction: column;
  }

  .heading-controls {
    flex-wrap: wrap;
    width: 100%;
    gap: 8px;
  }

  .view-switch {
    flex: 1 1 auto;
  }

  .view-switch button {
    min-width: 0;
  }

  .account-balance {
    text-align: left;
  }

  .heading-controls .account-filter,
  .heading-controls .control {
    width: 100%;
    max-width: none;
    min-height: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .funds-dashboard { transition: none; }
}


.funds-hero > .dashboard-hero-eyebrow { grid-column: 1 / -1; margin: 0 0 -12px; }
/* 等宽两列 + 与资产页相同的基准间隙，核对区块横向对齐资产页的“资产明细与账面记录”。 */
.funds-hero { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px 40px; }
.flow-panel { grid-column: 1 / -1; }
.flow-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 20px 28px; margin: 8px 0 0; }
.flow-summary div { display: grid; min-width: 0; align-content: start; gap: 6px; }
.flow-summary dt { color: var(--muted); font-size: 12px; font-weight: 750; }
.flow-summary dd { margin: 4px 0; font-size: clamp(20px, 2vw, 26px); font-weight: 700; line-height: 1.15; letter-spacing: -0.025em; overflow-wrap: anywhere; }
.flow-note { margin: 12px 0 0; font-size: 11px; color: var(--muted); }
.account-grid { align-items: start; gap: 14px; }
.account-card { overflow: hidden; padding: 0; border-radius: var(--radius-panel); background: var(--surface); }
.account-card.attention { border-color: color-mix(in srgb, var(--warning) 55%, var(--line)); }
.account-card-summary { min-height: 244px; padding: 17px 18px 16px; background: linear-gradient(135deg, color-mix(in srgb, var(--accent-soft) 24%, var(--surface)), var(--surface)); }
.account-card.attention > .account-card-summary { background: linear-gradient(135deg, color-mix(in srgb, var(--warning-soft) 45%, var(--surface)), var(--surface)); }
.account-card-topline { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 11px; }
.account-classification { min-width: 0; color: var(--muted); font-size: 11px; font-weight: 720; }
.account-head { display: flex; min-width: 0; align-items: flex-start; justify-content: space-between; gap: 20px; }
.account-name { display: grid; min-width: 0; gap: 7px; }
.account-name h3 { margin: 0; overflow-wrap: anywhere; font-size: 20px; line-height: 1.2; }
.account-name p { max-width: 42em; min-height: 2.9em; margin: 0; color: var(--muted); font-size: 11px; line-height: 1.45; }
.account-owner-state { display: inline-flex; width: fit-content; min-height: 22px; align-items: center; padding: 2px 8px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 11px; font-weight: 780; white-space: nowrap; }
.account-owner-state.attention { background: var(--warning-soft); color: var(--warning); }
.account-owner-state.neutral { background: var(--surface-soft); color: var(--muted); }
.account-balance { display: grid; min-width: 160px; justify-items: end; gap: 2px; text-align: right; white-space: nowrap; }
.account-balance span, .account-balance small { color: var(--muted); font-size: 11px; }
.account-balance strong { display: block; margin: 0; font-size: 22px; line-height: 1.2; }
.account-balance small { color: var(--accent); font-weight: 720; }
.account-balance small.loss { color: var(--danger); }
.account-balance.unknown strong { color: var(--warning); font-size: 17px; }
.account-owner-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: 16px; overflow: hidden; border: 1px solid color-mix(in srgb, var(--line) 82%, transparent); border-radius: 11px; background: var(--surface-soft); }
.account-owner-grid > div { display: grid; min-width: 0; align-content: start; gap: 3px; padding: 11px 12px; }
.account-owner-grid > div + div { border-left: 1px solid var(--line); }
.account-owner-grid span, .account-owner-grid small { color: var(--muted); font-size: 10.5px; line-height: 1.35; }
.account-owner-grid strong { overflow-wrap: anywhere; font-size: 14px; line-height: 1.35; }
.account-flow-scope { margin: 9px 0 0; color: var(--muted); font-size: 10.5px; }
.funds-review { margin-top: 6px; border: 1px solid var(--line); border-radius: var(--radius-panel); background: var(--surface); }
.funds-review > summary { display: flex; align-items: center; gap: 12px; padding: 15px 20px; list-style: none; font-size: 12px; }
.funds-review > summary::-webkit-details-marker { display: none; }
.funds-review strong { color: var(--text); font-size: 14px; }
.review-mark { width: 22px; height: 22px; display: grid; place-items: center; border-radius: 50%; background: var(--warning-soft); color: var(--warning); }
.review-action { margin-left: auto; color: var(--accent); white-space: nowrap; }
.review-action > span { display: inline-block; }
.funds-review[open] .review-action > span { transform: rotate(180deg); }
.review-content { padding: 0 20px 16px; font-size: 12px; }
#bank-details { padding: 0; border: 0; border-radius: 0; background: transparent; }
#bank-details .view-switch { padding: 3px; }
#bank-details .view-switch button { min-height: 34px; padding: 0 12px; }
@media (min-width: 761px) and (max-width: 1199px) {
  .fund-business-workbench { grid-template-columns: 264px minmax(0, 1fr); }
  .book-list-columns { display: none; }
  .book-activity-row {
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 6px 12px;
    padding: 12px 2px;
  }
  .book-activity-row > time { grid-row: 1; grid-column: 1; }
  .book-activity-row > .direction { grid-row: 1; grid-column: 2; justify-self: end; }
  .book-movement-copy { grid-row: 2; grid-column: 1 / -1; }
  .book-movement-amount { grid-row: 3; grid-column: 1; justify-self: start; }
  .book-voucher-link,
  .book-voucher-missing { grid-row: 3; grid-column: 2; justify-self: end; }
}
@media (max-width: 760px) {
  .funds-hero { padding: 22px; gap: 24px; }
  .flow-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); padding-top: 18px; border-top: 1px solid var(--line); }
  .account-card { padding: 0; }
  .account-card-summary { min-height: 0; padding: 16px; }
  .account-name p { min-height: 0; }
  .account-balance { min-width: 0; justify-items: start; text-align: left; white-space: normal; }
  .account-owner-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .account-owner-grid > div + div { border-left: 1px solid var(--line); }
  .account-owner-grid > div:nth-child(3) { grid-column: 1 / -1; border-top: 1px solid var(--line); border-left: 0; }
  .funds-review > summary { flex-wrap: wrap; padding: 14px; gap: 8px; }
  .funds-review > summary > span:nth-child(3) { flex-basis: calc(100% - 32px); order: 2; margin-left: 30px; }
  .fund-business-workbench,
  #fund-detail-panel-bank { min-height: 0; }
  .fund-business-workbench { grid-template-columns: minmax(0, 1fr); }
  .fund-account-index { padding: 10px; border-right: 0; border-bottom: 1px solid var(--line); }
  .fund-account-index > button { min-height: 44px; padding: 9px 12px; }
  .fund-business-detail { padding: 4px 12px; }
  .book-activity-feed { border: 0; background: transparent; }
  .book-list-columns { display: none; }
  .book-activity-list { display: block; }
  .book-activity-row {
    min-height: 78px;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      "date direction"
      "copy copy"
      "amount voucher";
    gap: 5px 10px;
    align-items: start;
    padding: 9px 10px;
    border: 0;
    border-radius: 0;
    background: transparent;
  }
  .book-activity-row + .book-activity-row { border: 0; border-top: 1px solid var(--line); }
  .book-activity-row > time { grid-area: date; }
  .book-movement-copy { grid-area: copy; }
  .book-activity-row > .direction { grid-area: direction; justify-self: end; }
  .book-movement-amount { grid-area: amount; justify-self: start; align-self: center; }
  .book-voucher-link,
  .book-voucher-missing { grid-area: voucher; justify-self: end; }
  .book-voucher-button { min-height: 44px; }
  .book-voucher-preview { display: none; }
  .bank-activity-feed { overflow: visible; border: 0; background: transparent; }
  .bank-activity-list { display: grid; gap: 10px; }
  .bank-activity-item {
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: var(--radius-control);
    background: var(--surface);
  }
  .bank-activity-summary {
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
      "date amount"
      "account status"
      "main main";
    gap: 11px 14px;
    align-items: start;
    padding: 14px 36px 14px 14px;
  }
  .bank-activity-item + .bank-activity-item { border: 1px solid var(--line); }
  .bank-activity-date { grid-area: date; }
  .bank-activity-account { grid-area: account; }
  .bank-activity-main { grid-area: main; }
  .bank-match-status { grid-area: status; justify-self: end; }
  .bank-activity-amount { grid-area: amount; }
  .bank-record-fields > div { grid-template-columns: minmax(84px, 0.32fr) minmax(0, 1fr); }
  .bank-batch-heading { display: grid; gap: 4px; }
  .bank-batch-heading-meta { text-align: left; }
  .bank-batch-items { grid-template-columns: 1fr; }
}

</style>
