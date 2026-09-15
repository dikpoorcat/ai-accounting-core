import { requestJson } from "./client";
import type { DashboardPeriod } from "./context";
import type { DashboardCollections, PeriodPreparation, UnestablishedSelection } from "./dashboardContracts";

export interface FundPage { has_more: boolean; next_cursor: string | null; total_count: number }

export type FenValue = string;
export type FundDirection = "inflow" | "outflow";
export type BankStatementState =
  | "matched"
  | "unmatched"
  | "needs_review";
export type BankCoverageState = "missing" | "partial" | "complete" | "not_applicable";

export interface BankSourceCheck {
  state: "confirmed" | "unestablished" | "needs_review" | "conflict";
  message: string;
  statement_confirmed?: boolean;
  reconciliation_valid?: boolean;
  selected_statement_calculation_ids?: string[];
  statement_calculation_id: string | null;
  reconciliation_calculation_id: string | null;
  statement_fact_id: string;
  reconciliation_fact_id: string | null;
  selection_source: string | null;
  selection_proof: Record<string, unknown> | null;
  proof_method: string | null;
}

export interface FundReconciliation {
  source_check?: BankSourceCheck;
  state:
    | "not_applicable"
    | "not_configured"
    | "out_of_scope"
    | "pending"
    | "attention"
    | "complete";
  label: string;
  version?: number;
  statement_closing_fen?: FenValue;
  book_closing_fen?: FenValue | null;
  difference_fen?: FenValue | null;
  unmatched_count?: number;
  needs_review_count?: number;
  warning_count?: number;
  coverage_start_date?: string;
  coverage_end_date?: string;
  confirmed_at?: string;
}

export interface FundAccountStatement {
  account_code: string;
  account_name: string;
  inflow_fen: FenValue | null;
  outflow_fen: FenValue | null;
  transaction_count: number;
  matched_count: number;
  unmatched_count: number;
  needs_review_count: number;
  coverage_state: BankCoverageState;
  last_activity_date: string | null;
}

export interface FundAccount {
  account_id: string;
  code: string;
  name: string;
  type: "bank" | "cash" | "payment_platform";
  active: boolean | null;
  opening_fen: FenValue | null;
  inflow_fen: FenValue;
  outflow_fen: FenValue;
  net_change_fen: FenValue;
  closing_fen: FenValue | null;
  movement_count: number;
  last_activity_date: string | null;
  negative_balance: boolean;
  statement: FundAccountStatement;
  reconciliation: FundReconciliation;
}

export interface FundMovement {
  id: string;
  date: string | null;
  account_id: string;
  account_code: string;
  account_name: string;
  account_type: "bank" | "cash" | "payment_platform";
  direction: FundDirection;
  amount_fen: FenValue;
  signed_amount_fen: FenValue;
  reference: string;
  type: string;
  summary: string;
  display_summary: string;
  list_summary: string;
  party: string;
  internal_transfer: boolean;
  component_kinds: string[];
}

export interface BankBatchPaymentItem {
  party: string;
  amount_fen: FenValue;
}

export interface BankBatchPayment {
  bank_row_count: number;
  total_fen: FenValue;
  items: BankBatchPaymentItem[];
}

export interface BankStatementRow {
  id: string;
  date: string | null;
  reference: string;
  account_id: string;
  account_code: string;
  account_name: string;
  direction: FundDirection;
  amount_fen: FenValue;
  signed_amount_fen: FenValue;
  party: string;
  memo: string;
  state: BankStatementState;
  source_check?: BankSourceCheck;
  batch_payment?: BankBatchPayment;
}

export interface FundBankStatement {
  transaction_count: number;
  inflow_fen: FenValue | null;
  outflow_fen: FenValue | null;
  matched_count: number;
  unmatched_count: number;
  needs_review_count: number;
  coverage_state: BankCoverageState;
  statement_count: number;
  expected_account_count: number;
  provided_account_count: number;
  missing_account_count: number;
  rows: BankStatementRow[];
  page: FundPage;
}

export interface InvestmentProduct {
  fund_id: string;
  name: string;
  opening_cost_fen: FenValue;
  subscription_cost_fen: FenValue;
  redemption_cost_fen: FenValue;
  closing_cost_fen: FenValue;
  investment_income_fen: FenValue;
}

export interface InvestmentEvent {
  id: string;
  date: string | null;
  period: string;
  fund_id: string;
  name: string;
  type: string;
  reference: string;
  cost_fen: FenValue | null;
  net_proceeds_fen: FenValue | null;
  investment_income_fen: FenValue | null;
  settlement_fen: FenValue | null;
}

export interface FundInvestments {
  opening_cost_fen: FenValue;
  subscription_cost_fen: FenValue;
  redemption_cost_fen: FenValue;
  closing_cost_fen: FenValue;
  investment_income_fen: FenValue;
  actual_payments_fen: FenValue;
  actual_receipts_fen: FenValue;
  products: InvestmentProduct[];
  events: InvestmentEvent[];
  event_count: number;
  page: FundPage;
}

export interface FundsData {
  fact_issues: UnestablishedSelection[];
  period_preparation: PeriodPreparation;
  collections: DashboardCollections;
  total_fen: FenValue | null;
  bank_fen: FenValue | null;
  bank_opening_fen: FenValue | null;
  bank_inflow_fen: FenValue | null;
  bank_outflow_fen: FenValue | null;
  cash_fen: FenValue | null;
  payment_platform_fen: FenValue | null;
  opening_fen: FenValue | null;
  inflow_fen: FenValue;
  outflow_fen: FenValue;
  net_change_fen: FenValue;
  internal_transfer_fen: FenValue;
  account_count: number;
  bank_account_count: number;
  cash_account_count: number;
  payment_platform_account_count: number;
  attention_account_count: number;
  accounts: FundAccount[];
  movements: FundMovement[];
  movement_page: FundPage;
  movement_count: number;
  bank_statement: FundBankStatement;
  investments: FundInvestments;
}

export interface FundsDashboardResponse {
  schema_version: 2;
  snapshot_version: string;
  selected_period: DashboardPeriod | null;
  data: FundsData | null;
}

export interface FundsQuery {
  section?: "accounts" | "movements" | "statements" | "investment_products" | "investment_events";
  cursor?: string;
  after_movement?: string;
  after_statement?: string;
  after_investment?: string;
  expected_version?: string;
  movement_account_type?: FundAccount["type"];
  movement_account_id?: string;
  statement_account_id?: string;
}

export function fetchFundsDashboard(periodKey?: string, signal?: AbortSignal, options: FundsQuery = {}) {
  const query = new URLSearchParams({ limit: "100", ...(periodKey ? { period: periodKey } : {}), ...options });
  return requestJson<FundsDashboardResponse>(`/api/dashboard/funds?${query}`, {
    signal,
  });
}

// Keep selector labels across source navigation without adding cached rows to a page.
let accountLabelScope = "";
const accountLabels = new Map<string, string>();

export function fundAccountDisplayName(name: string, code: string): string {
  const normalizedName = name.trim();
  const normalizedCode = code.trim();
  if (!normalizedName || !normalizedCode) return normalizedName;
  const trailingDigits = normalizedCode.match(/\d{3,}$/)?.[0];
  const suffixes = [...new Set([normalizedCode, trailingDigits ? `尾号${trailingDigits}` : ""].filter(Boolean))]
    .flatMap(suffix => [`（${suffix}）`, `(${suffix})`, suffix]);
  for (const suffix of suffixes) {
    if (!normalizedName.endsWith(suffix)) continue;
    const base = normalizedName.slice(0, -suffix.length).replace(/[（(·\-—\s]+$/u, "").trim();
    if (base) return base;
  }
  return normalizedName;
}

export function fundAccountDisplayLabel(name: string, code: string): string {
  const displayName = fundAccountDisplayName(name, code);
  const normalizedCode = code.trim();
  if (!normalizedCode) return displayName;
  const trailingDigits = normalizedCode.match(/\d{3,}$/)?.[0];
  if (displayName.includes(normalizedCode) || (trailingDigits && displayName.includes(trailingDigits))) return displayName;
  return `${displayName}（${normalizedCode}）`;
}

export function fundAccountLabel(company: string, period: string, type: string, id: string): string | undefined {
  const scope = JSON.stringify([company, period]);
  if (scope !== accountLabelScope) { accountLabels.clear(); accountLabelScope = scope; }
  return accountLabels.get(JSON.stringify([company, type, id]));
}
export function rememberFundAccounts(company: string, period: string, accounts: FundAccount[]) {
  fundAccountLabel(company, period, "", "");
  for (const account of accounts) accountLabels.set(JSON.stringify([company, account.type, account.account_id]), fundAccountDisplayLabel(account.name, account.code));
}
