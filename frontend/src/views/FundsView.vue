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
  type FundAccount,
  type FundMovement,
  type FundsData,
  type FundsQuery,
} from "../api/funds";
import DashboardModuleHeader from "../components/DashboardModuleHeader.vue";
import DashboardPagination from "../components/DashboardPagination.vue";
import BusinessStatusDetails from "../components/BusinessStatusDetails.vue";
import DashboardSectionNav from "../components/DashboardSectionNav.vue";
import { useDashboardContext } from "../composables/useDashboardContext";
import { useDashboardSections } from "../composables/useDashboardSections";
import { cashFlowClass, fen, formatFen, formatPositiveFen } from "../utils/money";
import { appendDashboardCollection } from "../utils/dashboardCollections";
import type { DashboardFundsContract } from "../api/generated/dashboardFunds";

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
const expandedMovementId = ref("");
type BankStatementRow = DashboardFundsContract.BankStatementRow;
const expandedBatchId = ref(""), previewBatchId = ref("");
const batchPreviewPanel = ref<HTMLElement | null>(null);
const batchPreviewList = ref<HTMLElement | null>(null);
const batchPreviewPosition = ref({ left: 0, top: 0, side: "left", arrow: 0, ready: false });
let batchPreviewAnchor: HTMLElement | null = null;
let expandedBatchAnchor: HTMLElement | null = null;
let batchPreviewGeneration = 0;
let batchCloseTimer: ReturnType<typeof setTimeout> | undefined;
let batchTriggerHovered = false, batchPanelHovered = false, restoringBatchFocus = false;
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
const accounts = computed(() => funds.value?.collections.accounts?.items ?? []);
const movements = computed(() => funds.value?.collections.movements?.items ?? []);
const bankRows = computed(() => funds.value?.collections.statements?.items ?? []);
const investmentProducts = computed(() => funds.value?.collections.investment_products?.items ?? []);
const investmentEvents = computed(() => funds.value?.collections.investment_events?.items ?? []);
const visibleMovements = movements;
const bankAccounts = computed(
  () => accounts.value.filter((account) => account.type === "bank"),
);
const accountOptions = computed(() => {
  const options = accounts.value.map(account => ({ value: accountKey(account.type, account.account_id), label: fundAccountDisplayLabel(account.name, account.code) }));
  if (selectedAccount.value && !options.some(option => option.value === selectedAccount.value)) {
    const separator = selectedAccount.value.indexOf(":");
    options.push({ value: selectedAccount.value, label: fundAccountLabel(queryText("company_id"), selectedPeriod.value, selectedAccount.value.slice(0, separator), selectedAccount.value.slice(separator + 1)) ?? "所选账户（名称尚未加载）" });
  }
  return options;
});
const movementAccountEntries = computed<MovementAccountEntry[]>(() => {
  const entries: MovementAccountEntry[] = accounts.value.map((account) => ({
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
const visibleMovementCount = computed(() => funds.value?.collections.movements?.page.filtered_count ?? 0);
const selectedMovementAccountLabel = computed(
  () => movementAccountEntries.value.find((entry) => entry.value === selectedAccount.value)?.label ?? "资金账户",
);
const bankAccountOptions = computed(() => {
  const options = bankAccounts.value.map(account => ({ value: account.account_id, label: fundAccountDisplayLabel(account.name, account.code) }));
  if (selectedBankAccount.value && !options.some(option => option.value === selectedBankAccount.value)) options.push({ value: selectedBankAccount.value, label: fundAccountLabel(queryText("company_id"), selectedPeriod.value, "bank", selectedBankAccount.value) ?? "所选银行账户（名称尚未加载）" });
  return options;
});
const visibleBankRows = bankRows;
const previewBatchRow = computed(() => visibleBankRows.value.find(item => item.id === previewBatchId.value));
const selectedBankAccountLabel = computed(
  () => bankAccountOptions.value.find((option) => option.value === selectedBankAccount.value)?.label ?? "全部银行账户",
);
const visibleBankRowCount = computed(() => funds.value?.collections.statements?.page.filtered_count ?? 0);
const attentionItems = computed(() => {
  if (!funds.value) return [];
  const items: string[] = [];
  for (const account of accounts.value) {
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
  if (statement.review_state === "pending") items.push("AI 会计核对中，如需您补充资料会另列待办。");
  if (statement.missing_account_count) {
    items.push(`${statement.missing_account_count} 个银行账户尚未提供本月流水，不能据此判断没有收支。`);
  }
  return items;
});
const bankNeedsAttention = computed(() => {
  const statement = funds.value?.bank_statement;
  return Boolean(statement && (statement.review_state === "pending" || statement.missing_account_count || ["missing", "partial"].includes(statement.coverage_state)));
});
const sectionLinks = computed(() => {
  if (!funds.value) return [];
  const links = [
    { id: "funds-overview", label: "概览" },
    { id: "fund-accounts", label: "账户" },
  ];
  if (investmentProducts.value.length) links.push({ id: "fund-investments", label: "货币基金" });
  if (attentionItems.value.length || funds.value?.attention_account_count) links.push({ id: "funds-attention", label: "关注" });
  links.push({ id: "bank-details", label: "资金明细" });
  return links;
});
const { activeSection, focusSection, focusSelectedPanel, positionSection, lockSectionSync } =
  useDashboardSections(sectionLinks, "funds-overview");

function accountKey(type: string, id: string) {
  return `${type}:${id}`;
}

function queryText(key: string) { return typeof route.query[key] === "string" ? route.query[key] as string : ""; }
function routeAccount() {
  const type = queryText("movement_account_type"), id = queryText("movement_account_id");
  return ["bank", "cash", "payment_platform"].includes(type) && id ? accountKey(type, id) : "";
}
function cancelPages() {
  for (const request of pageRequests.values()) request.abort();
  pageRequests.clear();
  for (const state of Object.values(pageStates.value)) { state.loading = false; state.error = ""; }
}

function queryFilters(): FundsQuery {
  const separator = selectedAccount.value.indexOf(":");
  const type = selectedAccount.value.slice(0, separator);
  const filters: FundsQuery = {};
  if (separator > 0 && (type === "bank" || type === "cash" || type === "payment_platform")) {
    filters.movement_account_type = type;
    filters.movement_account_id = selectedAccount.value.slice(separator + 1);
  } else {
    filters.movement_account_selection = "first";
  }
  if (selectedBankAccount.value) filters.statement_account_id = selectedBankAccount.value;
  return filters;
}
let requestedFilters = "";

function selectionKey() {
  return JSON.stringify([route.query.company_id, route.query.period, selectedPeriod.value, selectedAccount.value, selectedBankAccount.value]);
}
function pageSelectionKey(kind: PageKind) {
  return JSON.stringify([route.query.company_id, route.query.period, selectedPeriod.value,
    kind === "book" ? selectedAccount.value : kind === "bank" ? selectedBankAccount.value : null]);
}
function isPageCurrent(kind: PageKind, generation: number, selection: string) {
  return mounted && generation === requestGeneration && pageSelectionKey(kind) === selection;
}
function isCurrent(generation: number, selection: string) { return mounted && generation === requestGeneration && selectionKey() === selection; }
function invalidateRequests(keepContent = false) {
  resetBatchDetails();
  requestGeneration += 1;
  activeRequest?.abort(); cancelPages();
  activeRequest = null;
  if (!keepContent) { snapshotVersion.value = ""; funds.value = null; responsePeriod.value = ""; }
  loading.value = keepContent;
}

function selectMovementAccount(value: string) {
  if (selectedAccount.value === value) return;
  selectedAccount.value = value;
}

function toggleMovement(item: FundMovement, event?: Event) {
  if (!item.subject_id || (event?.target instanceof Element && event.target.closest("button,a,details,.business-status-details"))) return;
  expandedMovementId.value = expandedMovementId.value === item.id ? "" : item.id;
}

function handleMovementKey(item: FundMovement, event: KeyboardEvent) {
  if (event.target !== event.currentTarget || !item.subject_id || !["Enter", " "].includes(event.key)) return;
  event.preventDefault();
  toggleMovement(item);
}

function cancelBatchClose() {
  if (batchCloseTimer !== undefined) clearTimeout(batchCloseTimer);
  batchCloseTimer = undefined;
}

function batchDetailsTitle(item: BankStatementRow) {
  return item.direction === "inflow" ? "整批收款明细" : "整批付款明细";
}

function batchScopeNote(item: BankStatementRow) {
  return item.direction === "inflow"
    ? "以下逐项金额属于整个收款批次。现有银行资料没有逐项对应到本条流水，不能据此把某笔收款归到本条。"
    : "以下逐项金额属于整个付款批次。现有银行资料没有逐项对应到本条流水，不能据此把某位收款人归到本条。";
}

function clearBatchPreview(restoreFocus = false) {
  const anchor = batchPreviewAnchor;
  cancelBatchClose();
  batchPreviewGeneration += 1;
  previewBatchId.value = "";
  batchPreviewPosition.value = { left: 0, top: 0, side: "left", arrow: 0, ready: false };
  batchPreviewAnchor = null;
  batchTriggerHovered = false; batchPanelHovered = false;
  if (restoreFocus && anchor?.isConnected) {
    restoringBatchFocus = true;
    try { anchor.focus({ preventScroll: true }); }
    finally { restoringBatchFocus = false; }
  }
}

function resetBatchDetails() {
  clearBatchPreview();
  expandedBatchId.value = "";
  expandedBatchAnchor = null;
}

async function showBatchPreview(item: BankStatementRow, target: EventTarget | null, source: "pointer" | "focus" = "focus") {
  if (restoringBatchFocus || loading.value || !item.batch_payment?.items.length || expandedBatchId.value === item.id
    || typeof window === "undefined" || !window.matchMedia("(min-width: 761px) and (hover: hover) and (pointer: fine)").matches
    || !(target instanceof HTMLElement)) return;
  cancelBatchClose();
  if (batchPreviewAnchor !== target) { batchTriggerHovered = false; batchPanelHovered = false; }
  if (source === "pointer") batchTriggerHovered = true;
  batchPreviewAnchor = target;
  const generation = ++batchPreviewGeneration;
  previewBatchId.value = item.id;
  batchPreviewPosition.value.ready = false;
  await nextTick();
  const panel = batchPreviewPanel.value;
  if (generation !== batchPreviewGeneration || previewBatchId.value !== item.id || !panel || !target.isConnected) return;
  const anchor = target.getBoundingClientRect(), bounds = panel.getBoundingClientRect();
  const margin = 12, gap = 10;
  const clamp = (value: number, maximum: number) => Math.max(margin, Math.min(value, maximum));
  let side = "left", left = anchor.left - gap - bounds.width;
  let top = clamp(anchor.top + anchor.height / 2 - bounds.height / 2, window.innerHeight - margin - bounds.height);
  if (left < margin) {
    side = "right"; left = anchor.right + gap;
    if (left + bounds.width > window.innerWidth - margin) {
      side = "below";
      left = clamp(anchor.right - bounds.width, window.innerWidth - margin - bounds.width);
      top = anchor.bottom + gap;
      if (top + bounds.height > window.innerHeight - margin && anchor.top - gap - bounds.height >= margin) {
        side = "above"; top = anchor.top - gap - bounds.height;
      }
      top = clamp(top, window.innerHeight - margin - bounds.height);
    }
  }
  const vertical = side === "left" || side === "right";
  const arrow = Math.max(14, Math.min(vertical ? anchor.top + anchor.height / 2 - top : anchor.left + anchor.width / 2 - left,
    (vertical ? bounds.height : bounds.width) - 14));
  batchPreviewPosition.value = { left, top, side, arrow, ready: true };
}

function keepBatchPreview() {
  batchPanelHovered = true;
  cancelBatchClose();
}

function scheduleBatchClose() {
  if (!previewBatchId.value) return;
  cancelBatchClose();
  const generation = batchPreviewGeneration;
  batchCloseTimer = setTimeout(() => {
    batchCloseTimer = undefined;
    if (generation !== batchPreviewGeneration || batchTriggerHovered || batchPanelHovered) return;
    const focus = document.activeElement;
    if (focus === batchPreviewAnchor || (focus && batchPreviewPanel.value?.contains(focus))) return;
    clearBatchPreview();
  }, 150);
}
function leaveBatchTrigger() { batchTriggerHovered = false; scheduleBatchClose(); }
function leaveBatchPreview() { batchPanelHovered = false; scheduleBatchClose(); }

function toggleBatchDetails(item: BankStatementRow, target?: EventTarget | null) {
  if (loading.value || !item.batch_payment?.items.length) return;
  const anchor = target instanceof HTMLElement ? target : batchPreviewAnchor;
  const restoreFocus = Boolean(batchPreviewPanel.value?.contains(document.activeElement));
  expandedBatchId.value = expandedBatchId.value === item.id ? "" : item.id;
  expandedBatchAnchor = expandedBatchId.value ? anchor : null;
  clearBatchPreview(restoreFocus);
}

function handleBatchTriggerKey(event: KeyboardEvent) {
  if (event.key === "Tab" && !event.shiftKey && previewBatchId.value && batchPreviewList.value) {
    event.preventDefault(); batchPreviewList.value.focus();
  }
}
function handleBatchPreviewKey(event: KeyboardEvent) {
  if (event.key !== "Tab" || !batchPreviewAnchor) return;
  if (event.shiftKey && event.target === batchPreviewList.value) {
    event.preventDefault(); batchPreviewAnchor.focus({ preventScroll: true });
  } else if (!event.shiftKey && event.target instanceof HTMLButtonElement) {
    // Teleport places the panel after the page; continue from the original trigger.
    const elements = [...document.querySelectorAll<HTMLElement>("button:not(:disabled),a[href],input,select,textarea,[tabindex='0']")]
      .filter(element => element.getClientRects().length && !element.closest("[inert]") && !batchPreviewPanel.value?.contains(element));
    const next = elements[elements.indexOf(batchPreviewAnchor) + 1];
    if (next) { event.preventDefault(); clearBatchPreview(); next.focus({ preventScroll: true }); }
  }
}
function handleBatchEscape(event: KeyboardEvent) {
  if (event.key !== "Escape") return;
  if (previewBatchId.value) { event.preventDefault(); clearBatchPreview(true); }
  else if (expandedBatchId.value && event.target instanceof HTMLElement && event.target.closest(".bank-activity-item")) {
    event.preventDefault();
    const anchor = expandedBatchAnchor;
    resetBatchDetails();
    restoringBatchFocus = true;
    try { anchor?.focus({ preventScroll: true }); }
    finally { restoringBatchFocus = false; }
  }
}
function handleBatchViewportChange(event: Event) {
  if (!previewBatchId.value) return;
  if (event.type === "scroll" && event.target instanceof Node && batchPreviewPanel.value?.contains(event.target)) return;
  clearBatchPreview();
}

function bankOwnerNote() {
  const statement = funds.value?.bank_statement;
  if (!statement) return "正在读取本月银行收支。";
  if (statement.coverage_state === "not_applicable") return "本月暂无银行流水。";
  if (statement.missing_account_count) return `还有 ${statement.missing_account_count} 个银行账户未提供本月流水，金额仅包含已提供的资料。`;
  if (statement.review_state === "pending") return "AI 会计核对中，如需您补充资料会另列待办。";
  return statement.transaction_count ? `本月共 ${statement.transaction_count} 笔银行流水。` : "本月银行流水完整，未发生银行收支。";
}

function bankStatementSummary() {
  const statement = funds.value?.bank_statement;
  if (!statement) return "正在读取银行资料";
  if (statement.coverage_state === "not_applicable") return "暂无银行流水";
  if (["missing", "partial"].includes(statement.coverage_state)) return "流水资料尚未齐全";
  return statement.review_state === "pending" ? "AI 会计核对中" : "流水已核对";
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
  section?.querySelector<HTMLElement>("[data-section-focus]")?.focus({ preventScroll: true });
}

async function loadFunds(periodKey: string, contextGate?: Promise<void>) {
  resetBatchDetails();
  const generation = ++requestGeneration;
  cancelPages();
  activeRequest?.abort();
  const controller = new AbortController();
  let selection = selectionKey();
  const filters = queryFilters();
  requestedFilters = JSON.stringify(filters);
  lockSectionSync();
  const sectionToRestore = activeSection.value;
  const shouldRestoreSection = funds.value !== null;
  if (!contextGate) { snapshotVersion.value = ""; funds.value = null; }
  activeRequest = controller;
  loading.value = true;
  requestError.value = "";
  try {
    const request = fetchFundsDashboard(periodKey, controller.signal, filters);
    const response = contextGate ? (await Promise.all([request, contextGate]))[0] : await request;
    if (!isCurrent(generation, selection) || activeRequest !== controller) return;
    const selected = response.data?.selected_movement_account;
    if (!selectedAccount.value && selected) {
      selectedAccount.value = accountKey(selected.type, selected.account_id);
      selection = selectionKey();
      requestedFilters = JSON.stringify(queryFilters());
    }
    const data = response.data;
    funds.value = data;
    if (data) rememberFundAccounts(queryText("company_id"), periodKey, data.collections.accounts?.items ?? []);
    snapshotVersion.value = response.snapshot_version;
    responsePeriod.value = response.selected_period?.key ?? "";
    selectedPeriodLabel.value = response.selected_period?.label ?? "";
    loading.value = false; initializing.value = false;
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
    funds.value = null; snapshotVersion.value = "";
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

async function loadDetail(kind: "book" | "bank") {
  if (!funds.value || loading.value || !snapshotVersion.value) return;
  const section = kind === "book" ? "movements" : "statements";
  const generation = requestGeneration, selection = pageSelectionKey(kind), version = snapshotVersion.value;
  pageRequests.get(kind)?.abort();
  const request = new AbortController(); pageRequests.set(kind, request);
  const state = pageStates.value[kind]; state.loading = true; state.error = "";
  // Clear only the changed account's rows; summaries and other collections stay visible.
  const current = funds.value;
  const collection = current.collections[section];
  if (collection) funds.value = { ...current, collections: { ...current.collections, [section]: { ...collection, items: [] } } };
  const valid = () => isPageCurrent(kind, generation, selection) && pageRequests.get(kind) === request && snapshotVersion.value === version;
  try {
    const next = await fetchFundsDashboard(selectedPeriod.value, request.signal,
      { ...queryFilters(), expected_version: version, section });
    if (!valid() || !next.data || !funds.value || !next.data.collections[section]) return;
    if (next.snapshot_version !== version) { await refreshChanged(); return; }
    const latest = funds.value;
    funds.value = { ...latest,
      ...(kind === "book" ? { selected_movement_account: next.data.selected_movement_account } : {}),
      collections: { ...latest.collections, [section]: next.data.collections[section] } };
  } catch (caught) {
    if (valid()) {
      if (isDashboardSnapshotChanged(caught)) await refreshChanged();
      else state.error = dashboardErrorMessage(caught);
    }
  } finally { if (valid()) { pageRequests.delete(kind); state.loading = false; } }
}

async function loadMore(kind: PageKind) {
  if ((kind === "book" || kind === "bank") && pageStates.value[kind].error
    && !funds.value?.collections[kind === "book" ? "movements" : "statements"]?.items.length) {
    return loadDetail(kind);
  }
  const current = funds.value;
  const section = kind === "book" ? "movements" : kind === "bank" ? "statements" : kind === "investment" ? "investment_events" : kind;
  const page = current?.collections[section]?.page;
  const state = pageStates.value[kind];
  if (!current || !page?.has_more || !page.next_cursor || state.loading || loading.value) return;
  const selection = pageSelectionKey(kind);
  const generation = requestGeneration;
  const version = snapshotVersion.value;
  const request = new AbortController(); pageRequests.set(kind, request); state.loading = true; state.error = "";
  try {
    const next = await fetchFundsDashboard(selectedPeriod.value, request.signal,
      { ...queryFilters(), expected_version: snapshotVersion.value, section, cursor: page.next_cursor });
    if (!isPageCurrent(kind, generation, selection) || pageRequests.get(kind) !== request || !next.data || !funds.value || snapshotVersion.value !== version) return;
    const latest = funds.value;
    const collection = next.data.collections[section];
    const previousCollection = latest.collections[section];
    if (!collection || !previousCollection) return;
    const appended = appendDashboardCollection(previousCollection, collection);
    funds.value = { ...latest, collections: { ...latest.collections, [section]: appended } };
    if (kind === "accounts") rememberFundAccounts(queryText("company_id"), selectedPeriod.value, collection.items);
  } catch (caught) {
    if (isPageCurrent(kind, generation, selection) && pageRequests.get(kind) === request) {
      if (isDashboardSnapshotChanged(caught)) { await refreshChanged(); }
      else state.error = dashboardErrorMessage(caught);
    }
  }
  finally { if (isPageCurrent(kind, generation, selection) && pageRequests.get(kind) === request) { pageRequests.delete(kind); state.loading = false; } }
}

function paginationScope(kind: PageKind) { return JSON.stringify([pageSelectionKey(kind), snapshotVersion.value, requestGeneration]); }
function pausePages(kind: PageKind, scope: string) {
  if (scope !== paginationScope(kind)) return;
  pageRequests.get(kind)?.abort(); pageRequests.delete(kind); pageStates.value[kind].loading = false;
}
function changePeriod(value: string) {
  void router.push({ query: { company_id: route.query.company_id, period: value || undefined } });
}

function refresh() { return refreshCurrent(true); }
function refreshChanged() {
  updateNotice.value = "资料已更新，正在重新读取。";
  return refreshCurrent(false);
}
async function refreshCurrent(keepContent: boolean) {
  invalidateRequests(keepContent);
  loading.value = true;
  const generation = requestGeneration;
  const selection = selectionKey();
  const period = selectedPeriod.value;
  try {
    const company = route.query.company_id, requested = routePeriod();
    if (typeof company === "string" && requested && requested === period
      && context.value?.current_company?.company_id === company && context.value.periods.some((item) => item.key === requested)) {
      const contextGate = refreshContext().then((fresh) => {
        if (fresh.current_company?.company_id !== company || !fresh.periods.some((item) => item.key === requested))
          throw new Error("当前公司或期间已变化，请重新选择。");
      });
      await loadFunds(requested, contextGate);
    } else {
      await refreshContext();
      if (isCurrent(generation, selection) && period && !activeRequest) await loadFunds(period);
    }
  } catch (caught) {
    if (isCurrent(generation, selection)) {
      funds.value = null; snapshotVersion.value = ""; loading.value = false;
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
        : "AI 会计核对中",
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
        ? "流水已核对。"
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
  () => [route.query.company_id, route.query.period, selectedPeriod.value, selectedAccount.value, snapshotVersion.value, selectedDetailView.value],
  () => { expandedMovementId.value = ""; },
  { flush: "sync" },
);
watch(
  () => [route.query.company_id, route.query.period, selectedPeriod.value, selectedAccount.value, selectedBankAccount.value,
    snapshotVersion.value, selectedDetailView.value, activeSection.value],
  resetBatchDetails,
  { flush: "sync" },
);
watch(visibleBankRows, rows => {
  if (previewBatchId.value && !rows.some(item => item.id === previewBatchId.value)) clearBatchPreview();
  if (expandedBatchId.value && !rows.some(item => item.id === expandedBatchId.value)) {
    expandedBatchId.value = ""; expandedBatchAnchor = null;
  }
}, { flush: "sync" });

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
    if (companyChanged || selectedPeriod.value !== target || (!funds.value && !activeRequest)) {
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
  () => [selectedAccount.value, selectedBankAccount.value],
  (value, previous) => {
    if (!selectedPeriod.value || JSON.stringify(queryFilters()) === requestedFilters) return;
    if (!funds.value || !snapshotVersion.value || activeRequest) {
      invalidateRequests();
      void loadFunds(selectedPeriod.value);
      return;
    }
    requestedFilters = JSON.stringify(queryFilters());
    if (value[0] !== previous[0]) void loadDetail("book");
    if (value[1] !== previous[1]) void loadDetail("bank");
  },
);
watch(
  () => [route.query.movement_account_type, route.query.movement_account_id, route.query.statement_account_id],
  (value, previous) => {
    if (value.every((item, index) => item === previous[index])) return;
    selectedAccount.value = routeAccount();
    selectedBankAccount.value = queryText("statement_account_id");
  },
);

onMounted(async () => {
  window.addEventListener("scroll", handleBatchViewportChange, true);
  window.addEventListener("resize", handleBatchViewportChange);
  document.addEventListener("keydown", handleBatchEscape);
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
  resetBatchDetails();
  window.removeEventListener("scroll", handleBatchViewportChange, true);
  window.removeEventListener("resize", handleBatchViewportChange);
  document.removeEventListener("keydown", handleBatchEscape);
  mounted = false;
  invalidateRequests();
});
</script>

<template>
  <div class="funds-page" @click="focusSelectedPanel">
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
          <DashboardSectionNav v-if="funds" v-show="!loading && !initializing"
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
        v-if="initializing || loading"
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

      <div v-if="funds" v-show="!loading && !initializing && !pageError" class="funds-result">


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
            <p class="bank-owner-note">{{ bankOwnerNote() }}</p>
          </div>
          <div class="flow-panel">
          <dl class="flow-summary">
            <div><dt>本月实际收款</dt><dd :class="cashFlowClass(funds.inflow_fen, 'inflow')">{{ formatFen(funds.inflow_fen) }}</dd></div>
            <div><dt>本月实际付款</dt><dd :class="cashFlowClass(funds.outflow_fen, 'outflow')">{{ formatFen(funds.outflow_fen) }}</dd></div>
            <div><dt>月初账面资金</dt><dd>{{ formatFen(funds.opening_fen) }}</dd></div>
            <div><dt>本月资金增减</dt><dd :class="{ gain: fen(funds.net_change_fen) > 0n, loss: fen(funds.net_change_fen) < 0n }">{{ formatSigned(funds.net_change_fen) }}</dd></div>
          </dl>
          <p class="flow-note">实际收付款不含公司账户间互转</p>
          </div>
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
          <div v-if="accounts.length" class="account-grid">
            <article
              v-for="account in accounts"
              :key="accountKey(account.type, account.account_id)"
              class="account-card dashboard-record-card" data-section-focus tabindex="-1"
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
                    <small :class="{ gain: fen(account.net_change_fen) > 0n, loss: fen(account.net_change_fen) < 0n }">
                      {{ accountChangeLabel(account.net_change_fen) }}
                      <template v-if="fen(account.net_change_fen) !== 0n"> {{ formatSigned(account.net_change_fen) }}</template>
                    </small>
                    <small v-if="account.attribution_adjustment_fen !== null && fen(account.attribution_adjustment_fen) !== 0n">
                      身份归属调整 {{ formatSigned(account.attribution_adjustment_fen) }}
                    </small>
                  </div>
                </div>
                <div class="account-owner-grid">
                  <div>
                    <span>本月账面流入</span>
                    <strong :class="cashFlowClass(account.inflow_fen, 'inflow')">{{ formatFen(account.inflow_fen) }}</strong>
                  </div>
                  <div>
                    <span>本月账面流出</span>
                    <strong :class="cashFlowClass(account.outflow_fen, 'outflow')">{{ formatFen(account.outflow_fen) }}</strong>
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
          <DashboardPagination automatic :active="!loading && ['fund-accounts', 'funds-attention', 'bank-details'].includes(activeSection)" :scope="paginationScope('accounts')" @pause="pausePages('accounts', $event)" compact item-label="个账户" :page="funds.collections.accounts?.page" :loaded="accounts.length" :loading="pageStates.accounts.loading" :error="pageStates.accounts.error" @more="loadMore('accounts')" @retry="loadMore('accounts')" />
        </section>

        <section v-if="investmentProducts.length" id="fund-investments"
          class="panel section-panel section-anchor" tabindex="-1" aria-labelledby="fund-investments-title">
          <div class="section-heading">
            <div><h2 id="fund-investments-title">货币基金</h2></div>
            <strong>期末账面成本 {{ formatFen(funds.investments.closing_cost_fen) }}</strong>
          </div>
          <p class="muted">按账面成本列示，不代表当前市值；未计入上方账户资金。</p>
          <p class="muted">本月确认收益 {{ formatFen(funds.investments.investment_income_fen) }} ·
            实际申购付款 <span :class="cashFlowClass(funds.investments.actual_payments_fen, 'outflow')">{{ formatFen(funds.investments.actual_payments_fen) }}</span> ·
            实际赎回到账 <span :class="cashFlowClass(funds.investments.actual_receipts_fen, 'inflow')">{{ formatFen(funds.investments.actual_receipts_fen) }}</span></p>
          <div class="table-wrap" data-section-focus role="region" aria-label="基金产品汇总" tabindex="0">
            <table class="investment-table investment-summary-table">
              <colgroup><col><col class="investment-amount-column"><col class="investment-amount-column"></colgroup>
              <thead><tr><th scope="col">产品</th><th scope="col" class="number">月末账面成本</th><th scope="col" class="number">本月确认收益</th></tr></thead>
              <tbody><tr v-for="item in investmentProducts" :key="item.fund_id">
                <td>{{ item.name }}</td><td class="number">{{ formatFen(item.closing_cost_fen) }}</td>
                <td class="number">{{ formatFen(item.investment_income_fen) }}</td>
              </tr></tbody></table>
          </div>
          <DashboardPagination automatic :active="!loading && activeSection === 'fund-investments'" :scope="paginationScope('investment_products')" @pause="pausePages('investment_products', $event)" compact item-label="个产品" :page="funds.collections.investment_products?.page" :loaded="investmentProducts.length" :loading="pageStates.investment_products.loading" :error="pageStates.investment_products.error" @more="loadMore('investment_products')" @retry="loadMore('investment_products')" />
          <details class="investment-details">
          <summary>查看申购、赎回与收付款明细</summary>
          <p class="muted">确认金额与实际收付款分别列示，确认收益不等于已经到账。</p>
          <div class="table-wrap" data-section-focus role="region" aria-label="基金成本变动" tabindex="0">
            <table class="investment-table investment-cost-table">
              <colgroup><col><col v-for="column in 5" :key="column" class="investment-amount-column"></colgroup>
              <thead><tr><th scope="col">产品</th><th scope="col" class="number">期初成本</th><th scope="col" class="number">申购成本变动</th>
              <th scope="col" class="number">赎回成本变动</th><th scope="col" class="number">期末成本</th><th scope="col" class="number">本月确认收益</th></tr></thead>
              <tbody><tr v-for="item in investmentProducts" :key="item.fund_id">
                <td>{{ item.name }}</td>
                <td class="number">{{ formatFen(item.opening_cost_fen) }}</td>
                <td class="number">{{ formatFen(item.subscription_cost_fen) }}</td>
                <td class="number">{{ formatFen(item.redemption_cost_fen) }}</td>
                <td class="number">{{ formatFen(item.closing_cost_fen) }}</td>
                <td class="number">{{ formatFen(item.investment_income_fen) }}</td>
              </tr></tbody></table>
          </div>
          <h3>本月确认及收付款</h3>
          <div v-if="investmentEvents.length" class="table-wrap" data-section-focus role="region" aria-label="基金确认及收付款明细" tabindex="0">
            <table class="investment-table investment-events-table">
              <colgroup><col class="date-column"><col><col v-for="column in 4" :key="column" class="investment-amount-column"></colgroup>
              <thead><tr><th scope="col">日期／所属月</th><th scope="col">产品及事项</th><th scope="col" class="number">确认成本</th>
              <th scope="col" class="number">确认赎回净款</th><th scope="col" class="number">确认收益</th><th scope="col" class="number">实际收付款</th></tr></thead>
              <tbody><tr v-for="item in investmentEvents" :key="item.id">
                <td>{{ formatDate(item.date || item.period) }}</td><td>{{ item.name }} · {{ item.type }}<BusinessStatusDetails :subject-id="item.subject_id" :period="selectedPeriod" :snapshot-version="snapshotVersion" settlement-view="historical" summary-label="查看业务事项" @changed="refreshChanged" /></td>
                <td class="number">{{ item.cost_fen === null ? "—" : formatFen(item.cost_fen) }}</td>
                <td class="number">{{ item.net_proceeds_fen === null ? "—" : formatFen(item.net_proceeds_fen) }}</td>
                <td class="number">{{ item.investment_income_fen === null ? "—" : formatFen(item.investment_income_fen) }}</td>
                <td class="number">{{ item.settlement_fen === null ? "—" : formatFen(item.settlement_fen) }}</td>
              </tr></tbody></table>
          </div>
          <p v-else class="empty">{{ loading ? "正在读取基金明细…" : "本月没有已确认的申赎或实际收付款。" }}</p>
          <DashboardPagination automatic :active="!loading && activeSection === 'fund-investments'" :scope="paginationScope('investment')" @pause="pausePages('investment', $event)" compact item-label="条明细" :page="funds.collections.investment_events?.page" :loaded="investmentEvents.length" :loading="pageStates.investment.loading" :error="pageStates.investment.error" @more="loadMore('investment')" @retry="loadMore('investment')" />
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
            <p class="muted">账户情况包含已加载 {{ accounts.length }} 个账户；银行资料完整性为全公司范围。</p>
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
                <template v-if="funds.collections.accounts?.page.has_more"> · 账户选项已加载 {{ accounts.length }} / {{ funds.account_count }}</template>
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
              <button v-if="selectedDetailView === 'bank' && pageStates.accounts.error" class="control" @click="loadMore('accounts')">重试加载账户</button>
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
            <div class="fund-business-workbench" :data-section-focus="selectedDetailView === 'book' ? '' : undefined" tabindex="-1">
              <nav class="fund-account-index" aria-label="资金账户">
                <span class="fund-account-heading">资金账户</span>
                <button
                  v-for="account in movementAccountEntries"
                  :key="account.value"
                  type="button"
                  :aria-current="selectedAccount === account.value ? 'true' : undefined"
                  @click="selectMovementAccount(account.value)"
                >
                  <span class="fund-account-copy">
                    <strong>{{ account.label }}</strong>
                    <small>{{ account.meta }}</small>
                  </span>
                  <b>{{ account.movementCount === null ? `${visibleMovementCount} 笔` : `${account.movementCount} 笔` }}</b>
                </button>
                <div v-if="pageStates.accounts.error" class="fund-account-more">
                  <button type="button" :disabled="pageStates.accounts.loading" @click="loadMore('accounts')">重试加载账户</button>
                  <span role="alert">{{ pageStates.accounts.error }}</span>
                </div>
              </nav>

              <div class="fund-business-detail" :aria-label="`${selectedMovementAccountLabel}资金明细`" :aria-busy="pageStates.book.loading" aria-live="polite">
            <div v-if="visibleMovements.length" class="book-activity-feed" role="region" aria-label="账面资金明细" tabindex="0">
              <div class="book-list-columns business-list-columns" aria-hidden="true">
                <span>日期</span><span>对象</span><span>事项</span><span>方向</span><span class="book-column-number">金额</span><span></span>
              </div>
              <ol class="book-activity-list">
                <li v-for="item in visibleMovements" :key="item.id" class="book-activity-row business-list-row"
                  :class="{ expandable: item.subject_id, expanded: expandedMovementId === item.id }"
                  :role="item.subject_id ? 'button' : undefined" :tabindex="item.subject_id ? 0 : undefined"
                  :aria-expanded="item.subject_id ? expandedMovementId === item.id : undefined"
                  @click="toggleMovement(item, $event)" @keydown="handleMovementKey(item, $event)">
                  <time class="business-list-date" :datetime="item.date || undefined">{{ formatDate(item.date) }}</time>
                  <div class="book-movement-copy business-list-object"><strong>{{ item.party || "无需往来对象" }}</strong></div>
                  <span class="book-movement-matter business-list-matter">{{ item.list_summary || item.type }}<small v-if="item.internal_transfer && item.list_summary !== '账户互转'">账户互转</small></span>
                  <span class="direction business-list-state" :class="item.correction ? 'correction' : item.direction">
                    {{ item.correction ? "更正原业务" : item.internal_transfer ? (item.direction === "inflow" ? "转入" : "转出") : item.direction === "inflow" ? "流入" : "流出" }}
                  </span>
                  <div class="book-movement-end business-list-money"><strong class="book-movement-amount" :class="item.correction ? 'correction' : item.direction">{{ item.correction ? formatFen(item.signed_amount_fen) : movementAmount(item.direction, item.amount_fen) }}</strong></div>
                  <svg v-if="item.subject_id" class="book-expand-arrow business-list-arrow" :class="{ expanded: expandedMovementId === item.id }" viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg><span v-else class="business-list-arrow-space" aria-hidden="true"></span>
                  <BusinessStatusDetails v-if="item.subject_id" :subject-id="item.subject_id" :period="selectedPeriod" :snapshot-version="snapshotVersion" settlement-view="historical"
                    presentation="funds" :funds-context="item" :expanded="expandedMovementId === item.id" hide-summary
                    @click.stop @keydown.stop @changed="refreshChanged" />
                </li>
              </ol>
            </div>
            <p v-else class="empty">{{ loading || pageStates.book.loading ? "正在读取资金明细…" : selectedAccount ? "该账户本月没有已入账资金变动。" : "本月没有已入账资金变动。" }}</p>
            <DashboardPagination automatic :active="!loading && activeSection === 'bank-details' && selectedDetailView === 'book'" :scope="paginationScope('book')" @pause="pausePages('book', $event)" compact item-label="笔变动" :page="!movements.length && (pageStates.book.loading || pageStates.book.error) ? undefined : funds.collections.movements?.page" :loaded="movements.length" :loading="pageStates.book.loading" :error="pageStates.book.error" @more="loadMore('book')" @retry="loadMore('book')" />
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
            <div v-if="visibleBankRows.length" class="bank-activity-feed" :data-section-focus="selectedDetailView === 'bank' ? '' : undefined" role="region" aria-label="银行流水明细" tabindex="0">
              <div class="bank-activity-columns" aria-hidden="true"><span>日期</span><span>银行账户</span><span>用途／对方</span><span>明细</span><span>收支金额</span></div>
              <ol class="bank-activity-list">
                <li v-for="item in visibleBankRows" :key="item.id" class="bank-activity-item">
                  <div class="bank-activity-record">
                    <div class="bank-activity-summary">
                      <time class="bank-activity-date" :datetime="item.date || undefined">{{ formatDate(item.date) }}</time>
                      <span class="bank-activity-account">
                        <strong>{{ fundAccountDisplayName(item.account_name, item.account_code) }}</strong>
                        <small v-if="accountCodeAddsInformation(item.account_name, item.account_code)">{{ item.account_code }}</small>
                      </span>
                      <span class="bank-activity-main">
                        <strong>{{ item.memo || item.party || "用途未提供" }}</strong>
                        <small>{{ item.memo ? item.party || "对方名称未提供" : "摘要未提供，仅保留对方名称" }}</small>
                      </span>
                      <span class="bank-activity-details">
                        <button v-if="item.batch_payment?.items.length" type="button" class="bank-batch-trigger"
                            :disabled="loading || pageStates.bank.loading"
                            :aria-label="`${batchDetailsTitle(item)}，${item.batch_payment.items.length}项`"
                            :aria-expanded="expandedBatchId === item.id || previewBatchId === item.id"
                            :aria-controls="expandedBatchId === item.id ? `bank-batch-inline-${item.id}` : previewBatchId === item.id ? 'bank-batch-preview' : undefined"
                            @mouseenter="showBatchPreview(item, $event.currentTarget, 'pointer')" @mouseleave="leaveBatchTrigger"
                            @focus="showBatchPreview(item, $event.currentTarget)" @blur="scheduleBatchClose"
                            @keydown="handleBatchTriggerKey" @click.stop="toggleBatchDetails(item, $event.currentTarget)">
                            {{ item.batch_payment.items.length }}项
                        </button>
                        <span v-else class="bank-details-empty" aria-label="无明细">—</span>
                      </span>
                      <span class="bank-activity-amount" :class="item.direction">
                        <small class="bank-amount-direction">{{ bankDirectionLabel(item.direction) }}</small>
                        <strong>{{ movementAmount(item.direction, item.amount_fen) }}</strong>
                      </span>
                    </div>
                    <section v-if="expandedBatchId === item.id && item.batch_payment" :id="`bank-batch-inline-${item.id}`" class="bank-batch-detail" :aria-label="batchDetailsTitle(item)">
                      <header class="bank-batch-heading">
                        <span class="bank-batch-heading-title">{{ batchDetailsTitle(item) }} · {{ item.batch_payment.items.length }} 项</span>
                        <span class="bank-batch-heading-meta">{{ item.batch_payment.bank_row_count }} 笔银行流水 · 批次合计 {{ formatFen(item.batch_payment.total_fen) }}</span>
                      </header>
                      <p v-if="item.batch_payment.bank_row_count > 1" class="bank-batch-scope">
                        {{ batchScopeNote(item) }}
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
                </li>
              </ol>
            </div>
            <p v-else class="empty">{{ loading || pageStates.bank.loading ? "正在读取银行流水…" : selectedBankAccount ? "该账户本月没有已提供的银行流水。" : "本月没有已提供的银行流水。" }}</p>
            <button v-if="!visibleBankRows.length && selectedBankAccount" class="control" @click="selectedBankAccount = ''">清除账户筛选</button>
            <DashboardPagination automatic :active="!loading && activeSection === 'bank-details' && selectedDetailView === 'bank'" :scope="paginationScope('bank')" @pause="pausePages('bank', $event)" compact item-label="笔流水" :page="!bankRows.length && (pageStates.bank.loading || pageStates.bank.error) ? undefined : funds.collections.statements?.page" :loaded="bankRows.length" :loading="pageStates.bank.loading" :error="pageStates.bank.error" @more="loadMore('bank')" @retry="loadMore('bank')" />
          </div>
          </div>
        </section>
        </div>
        </div>
      </div>
    </div>
  </div>
  <Teleport to="body">
    <section v-if="previewBatchRow?.batch_payment" id="bank-batch-preview" ref="batchPreviewPanel" class="bank-batch-preview dashboard-hover-preview"
      :data-side="batchPreviewPosition.side" :style="{ left: `${batchPreviewPosition.left}px`, top: `${batchPreviewPosition.top}px`, '--preview-arrow': `${batchPreviewPosition.arrow}px`, visibility: batchPreviewPosition.ready ? 'visible' : 'hidden' }"
      role="region" :aria-label="`${batchDetailsTitle(previewBatchRow)}预览`" @mouseenter="keepBatchPreview" @mouseleave="leaveBatchPreview"
      @focusin="cancelBatchClose" @focusout="scheduleBatchClose" @keydown="handleBatchPreviewKey">
      <header class="bank-batch-preview-heading">
        <span><small>{{ previewBatchRow.batch_payment.bank_row_count }} 笔银行流水</small><strong>{{ batchDetailsTitle(previewBatchRow) }} · {{ previewBatchRow.batch_payment.items.length }} 项</strong></span>
        <span class="bank-batch-preview-total"><small>批次合计</small><b>{{ formatFen(previewBatchRow.batch_payment.total_fen) }}</b></span>
      </header>
      <div ref="batchPreviewList" class="bank-batch-preview-items" tabindex="0" role="region" aria-label="整批收付款逐项清单">
        <p v-if="previewBatchRow.batch_payment.bank_row_count > 1" class="bank-batch-preview-scope">
          {{ batchScopeNote(previewBatchRow) }}
        </p>
        <ol class="bank-batch-preview-list">
          <li v-for="(allocation, index) in previewBatchRow.batch_payment.items" :key="index"><span>{{ allocation.party }}</span><strong>{{ formatFen(allocation.amount_fen) }}</strong></li>
        </ol>
      </div>
      <footer class="bank-batch-preview-footer"><button type="button" @click="toggleBatchDetails(previewBatchRow)">查看完整明细</button></footer>
    </section>
  </Teleport>
</template>

<style scoped>
.funds-result { display: contents; }
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
  overflow-wrap: anywhere;
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

.gain, .funds-page .cash-inflow { color: var(--accent); }
.funds-page .cash-outflow { color: var(--warning); }

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

.view-switch button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* 标题与小字同属左侧一组，结构对齐简报页“本月发生 / 本月共 27 项 · 7 张凭证”。 */
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
  padding: 12px var(--dashboard-list-gutter);
}

.fund-business-detail > .empty {
  margin-top: 4px;
}

.book-activity-feed {
  --business-list-columns: 88px minmax(0, 1fr) minmax(0, 1fr) 68px 142px 12px;
  min-width: 0;
}

.book-activity-feed:focus-visible {
  outline: none;
}

.book-column-number {
  text-align: right;
}

.book-activity-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.book-movement-amount.correction { color: var(--muted); }
.direction.correction { color: var(--muted); background: var(--surface-soft); }

.book-movement-amount.inflow {
  color: var(--accent);
}

.book-movement-amount.outflow {
  color: var(--warning);
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
  --bank-list-columns: 76px minmax(0, 0.85fr) minmax(0, 1.7fr) 92px minmax(128px, 0.45fr);
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
}

.bank-activity-feed:focus-visible {
  outline: none;
}

.bank-activity-columns,
.bank-activity-summary {
  display: grid;
  grid-template-columns: var(--bank-list-columns);
  gap: 14px;
  align-items: center;
  padding: 10px 4px;
}

.bank-activity-columns {
  margin-inline: var(--dashboard-list-gutter);
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 11px;
}

.bank-activity-columns > :nth-child(4) { padding-right: 8px; text-align: right; }
.bank-activity-columns > :last-child { text-align: right; }

.bank-activity-summary {
  min-height: 62px;
  color: var(--text);
}

.bank-activity-list {
  margin: 0 var(--dashboard-list-gutter);
  padding: 0;
  list-style: none;
}

.bank-activity-item {
  position: relative;
}

.bank-activity-item + .bank-activity-item {
  border-top: 1px solid var(--line);
}

.bank-activity-record {
  background: var(--surface);
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
  overflow-wrap: anywhere;
  line-height: 1.5;
}

.bank-activity-account strong {
  font-size: 12px;
}

.bank-activity-account small {
  color: var(--muted);
  font-size: 11px;
}

.bank-activity-main > strong {
  font-size: 13.5px;
  font-weight: 700;
  overflow-wrap: anywhere;
  line-height: 1.5;
}

.bank-activity-main > small {
  color: var(--muted);
  font-size: 10.5px;
  overflow-wrap: anywhere;
  line-height: 1.5;
}

.bank-batch-detail {
  margin-inline: 4px;
  border-top: 1px solid var(--line);
}

.bank-batch-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
  padding: 10px 0;
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
  margin: 12px 0 14px;
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
  max-width: 100%;
  overflow-wrap: anywhere;
  white-space: normal;
  font-size: 15.5px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}

.bank-activity-details { display: flex; align-items: center; justify-content: flex-end; min-width: 0; }
.bank-details-empty { padding-right: 8px; color: var(--muted); font-size: 12px; }
.bank-batch-trigger { flex: none; min-height: 32px; padding: 0 8px; border: 0; border-radius: 8px; background: transparent; color: var(--accent); font: inherit; font-size: 11px; font-weight: 750; cursor: pointer; }
.bank-batch-trigger:hover, .bank-batch-trigger:focus-visible, .bank-batch-trigger[aria-expanded="true"] { background: var(--accent-soft); }
.bank-batch-trigger:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.bank-batch-trigger:disabled { color: var(--muted); cursor: default; }
.bank-batch-preview { position: fixed; z-index: 100; display: flex; flex-direction: column; width: min(380px, calc(100vw - 24px)); max-height: min(420px, calc(100dvh - 24px)); gap: 10px; }
.bank-batch-preview-heading { display: flex; flex: none; align-items: flex-start; justify-content: space-between; gap: 12px; }
.bank-batch-preview-heading > span { display: grid; min-width: 0; gap: 3px; }
.bank-batch-preview-heading small { color: var(--muted); font-size: 10px; }
.bank-batch-preview-heading strong { font-size: 13px; overflow-wrap: anywhere; }
.bank-batch-preview-total { text-align: right; }
.bank-batch-preview-total b { font-size: 14px; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.bank-batch-preview-items { min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 8px 9px; border-radius: 8px; background: var(--surface-soft); }
.bank-batch-preview-items:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.bank-batch-preview-scope { margin: 0 0 9px; color: var(--muted); font-size: 11px; line-height: 1.55; }
.bank-batch-preview-list { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }
.bank-batch-preview-list li { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; min-width: 0; font-size: 11px; }
.bank-batch-preview-list li > span { min-width: 0; color: var(--muted); overflow-wrap: anywhere; }
.bank-batch-preview-list li > strong { flex: none; white-space: nowrap; font-size: 11px; font-variant-numeric: tabular-nums; }
.bank-batch-preview-footer { display: flex; flex: none; justify-content: flex-end; }
.bank-batch-preview-footer button { padding: 3px 0; border: 0; background: transparent; color: var(--muted); font: inherit; font-size: 10px; cursor: pointer; }
.bank-batch-preview-footer button:hover { color: var(--accent); }
.bank-batch-preview-footer button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }

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
  padding-inline: var(--dashboard-list-gutter);
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
  outline: none;
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
  padding: 10px 4px;
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
.account-balance small { color: var(--muted); font-weight: 720; }
.account-balance small.gain { color: var(--accent); }
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
#bank-details { padding: 0; border: 0; border-radius: 0; background: transparent; outline: none; }
#bank-details .view-switch { padding: 3px; }
#bank-details .view-switch button { min-height: 34px; padding: 0 12px; }
@media (min-width: 761px) and (max-width: 1199px) {
  .fund-business-workbench { grid-template-columns: 264px minmax(0, 1fr); }
}
@media (max-width: 760px) {
  .funds-hero { grid-template-columns: minmax(0, 1fr); padding: 22px; gap: 24px; }
  .funds-hero > div { min-width: 0; }
  .flow-summary { grid-template-columns: minmax(0, 1fr); padding-top: 18px; border-top: 1px solid var(--line); }
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
  .fund-detail-panels { display: block; }
  #fund-detail-panel-book,
  #fund-detail-panel-bank { display: none; }
  #fund-detail-panel-book.is-active,
  #fund-detail-panel-bank.is-active { display: block; }
  #fund-detail-panel-book > .fund-business-workbench { height: auto; }
  .fund-business-workbench { grid-template-columns: minmax(0, 1fr); align-items: start; align-content: start; }
  .fund-account-index { padding: 10px; border-right: 0; border-bottom: 1px solid var(--line); }
  .fund-account-index > button { min-height: 44px; padding: 9px 12px; }
  .fund-business-detail { padding: 4px var(--dashboard-list-gutter); }
  .book-activity-feed { border: 0; background: transparent; }
  .book-activity-list { display: block; }



  .bank-activity-feed { overflow: visible; background: transparent; }
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
      "account account"
      "main main"
      "details details";
    gap: 11px 14px;
    align-items: start;
    padding: 14px 4px;
  }
  .bank-activity-item + .bank-activity-item { border: 1px solid var(--line); }
  .bank-activity-date { grid-area: date; }
  .bank-activity-account { grid-area: account; }
  .bank-activity-main { grid-area: main; }

  .bank-activity-amount { grid-area: amount; }
  .bank-activity-columns { display: none; }
  .bank-activity-details { grid-area: details; justify-self: end; width: 92px; }
  .bank-batch-trigger { min-height: 44px; }
  .bank-batch-heading { display: grid; gap: 4px; }
  .bank-batch-heading-meta { text-align: left; }
  .bank-batch-items { grid-template-columns: 1fr; }
}


.bank-owner-note { margin: 7px 0 0; color: var(--muted); font-size: 12px; }
@media (max-width: 760px) {
  .investment-table, .investment-table tbody, .investment-table tr, .investment-table td { display: block; min-width: 0; width: auto; }
  .investment-table thead, .investment-table colgroup { display: none; }
  .investment-table tr { padding: 12px 4px; border-bottom: 1px solid var(--line); }
  .investment-table td { display: grid; grid-template-columns: minmax(90px, .6fr) minmax(0, 1fr); gap: 10px; padding: 6px 0; border: 0; overflow-wrap: anywhere; }
  .investment-table td::before { color: var(--muted); text-align: left; }
  .investment-cost-table td:nth-child(1)::before { content: "产品"; }
  .investment-cost-table td:nth-child(2)::before { content: "期初成本"; }
  .investment-cost-table td:nth-child(3)::before { content: "申购成本变动"; }
  .investment-cost-table td:nth-child(4)::before { content: "赎回成本变动"; }
  .investment-cost-table td:nth-child(5)::before { content: "期末成本"; }
  .investment-cost-table td:nth-child(6)::before { content: "本月确认收益"; }
  .investment-events-table td:nth-child(1)::before { content: "日期／所属月"; }
  .investment-events-table td:nth-child(2)::before { content: "产品及事项"; }
  .investment-events-table td:nth-child(3)::before { content: "确认成本"; }
  .investment-events-table td:nth-child(4)::before { content: "确认赎回净款"; }
  .investment-events-table td:nth-child(5)::before { content: "确认收益"; }
  .investment-events-table td:nth-child(6)::before { content: "实际收付款"; }
  .table-wrap { overflow-x: visible; }
}
</style>
