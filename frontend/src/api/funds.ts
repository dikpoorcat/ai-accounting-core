import { requestJson } from "./client";
import type { DashboardPeriod } from "./context";

export type FenValue = string;
export type FundDirection = "inflow" | "outflow";
export type BankStatementState =
  | "matched"
  | "unmatched"
  | "invalid_match"
  | "pending_late"
  | "handled_late";

export interface FundReconciliation {
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
  book_closing_fen?: FenValue;
  difference_fen?: FenValue;
  unmatched_count?: number;
  pending_late_count?: number;
  warning_count?: number;
  coverage_start_date?: string;
  coverage_end_date?: string;
  confirmed_at?: string;
}

export interface FundAccountStatement {
  account_code: string;
  account_name: string;
  inflow_fen: FenValue;
  outflow_fen: FenValue;
  transaction_count: number;
  ordinary_count: number;
  matched_count: number;
  unmatched_count: number;
  late_count: number;
  pending_late_count: number;
  last_activity_date: string | null;
}

export interface FundAccount {
  code: string;
  name: string;
  type: "bank" | "cash";
  active: boolean;
  opening_fen: FenValue;
  inflow_fen: FenValue;
  outflow_fen: FenValue;
  net_change_fen: FenValue;
  closing_fen: FenValue;
  movement_count: number;
  last_activity_date: string | null;
  negative_balance: boolean;
  statement: FundAccountStatement;
  reconciliation: FundReconciliation;
}

export interface FundMovement {
  date: string;
  account_code: string;
  account_name: string;
  account_type: "bank" | "cash";
  direction: FundDirection;
  amount_fen: FenValue;
  signed_amount_fen: FenValue;
  reference: string;
  type: string;
  summary: string;
  party: string;
  internal_transfer: boolean;
}

export interface BankStatementRow {
  date: string;
  account_code: string;
  account_name: string;
  direction: FundDirection;
  amount_fen: FenValue;
  signed_amount_fen: FenValue;
  party: string;
  memo: string;
  state: BankStatementState;
  is_late: boolean;
}

export interface FundBankStatement {
  transaction_count: number;
  inflow_fen: FenValue;
  outflow_fen: FenValue;
  matched_count: number;
  ordinary_count: number;
  unmatched_count: number;
  late_count: number;
  pending_late_count: number;
  rows: BankStatementRow[];
}

export interface FundsData {
  total_fen: FenValue;
  bank_fen: FenValue;
  cash_fen: FenValue;
  opening_fen: FenValue;
  inflow_fen: FenValue;
  outflow_fen: FenValue;
  net_change_fen: FenValue;
  internal_transfer_fen: FenValue;
  account_count: number;
  bank_account_count: number;
  cash_account_count: number;
  attention_account_count: number;
  accounts: FundAccount[];
  movements: FundMovement[];
  movement_count: number;
  bank_statement: FundBankStatement;
}

export interface FundsDashboardResponse {
  schema_version: 1;
  selected_period: DashboardPeriod | null;
  data: FundsData | null;
}

export function fetchFundsDashboard(periodKey?: string, signal?: AbortSignal) {
  const query = periodKey ? `?period=${encodeURIComponent(periodKey)}` : "";
  return requestJson<FundsDashboardResponse>(`/api/dashboard/funds${query}`, {
    signal,
  });
}
