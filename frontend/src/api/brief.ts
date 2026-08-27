import type { DashboardPeriod } from "./context";
import { requestJson } from "./client";

export interface BriefVoucherLine {
  line_number: number;
  code: string;
  account: string;
  debit_fen: string;
  credit_fen: string;
  party: string;
}

export interface BriefVoucher {
  number: string;
  date: string;
  type: string;
  state: string;
  summary: string;
  list_summary: string;
  amount_fen: string;
  evidence: string[];
  lines: BriefVoucherLine[];
}

export interface BriefActivityRow {
  date: string;
  reference: string;
  title: string;
  subject: string;
  description: string;
  amount_fen: string;
  state: string;
  party: string;
  evidence: string[];
}

export interface BriefActivityGroup {
  key: string;
  label: string;
  event_count: number;
  type_counts: Array<{ label: string; count: number }>;
  rows: BriefActivityRow[];
}

export interface BriefBankRow {
  date: string;
  party: string;
  memo: string;
  direction: "inflow" | "outflow";
  amount_fen: string;
  state: string;
}

export interface BriefCash {
  transaction_count: number;
  ordinary_count: number;
  late_count: number;
  matched_count: number;
  unmatched_count: number;
  pending_late_count: number;
  inflow_fen: string;
  outflow_fen: string;
  net_fen: string;
}

export interface BriefPosition {
  assets_fen: string;
  liabilities_fen: string;
  capital_fen: string;
  bank_fen: string;
  fixed_asset_cost_fen: string;
  accumulated_depreciation_fen: string;
  fixed_asset_net_fen: string;
  intangible_asset_cost_fen: string;
  accumulated_amortization_fen: string;
  intangible_asset_net_fen: string;
  other_assets_fen: string;
  month_revenue_fen: string;
  month_expense_fen: string;
  month_result_fen: string;
  cumulative_result_fen: string;
  equation_valid: boolean;
}

export interface BriefOpenItem {
  voucher: string;
  party: string;
  description: string;
  status: "open" | "partial" | string;
  outstanding_fen: string;
}

export interface BriefOpenCategory {
  key: string;
  label: string;
  direction: "receivable" | "payable";
  unit: string;
  count: number;
  outstanding_fen: string;
  groups: Array<{
    party: string;
    count: number;
    outstanding_fen: string;
    open_count: number;
    partial_count: number;
  }>;
  items: BriefOpenItem[];
}

export interface BriefOpenItems {
  receivable_count: number;
  receivable_fen: string;
  payable_count: number;
  payable_fen: string;
  total_count: number;
  current_outstanding?: {
    receivable_count: number;
    receivable_fen: string;
    payable_count: number;
    payable_fen: string;
    total_count: number;
  };
  categories: BriefOpenCategory[];
}

export interface WorkforcePeriod {
  payroll_period?: string;
  remuneration_period?: string;
  total_fen: string;
  has_reversal: boolean;
}

export interface BriefEmployeeCost {
  has_activity: boolean;
  breakdown_available: boolean;
  reason: string | null;
  total_fen: string;
  controlled_total_fen: string;
  gross_salary_fen: string | null;
  employer_social_insurance_fen: string | null;
  employer_housing_fund_fen: string | null;
  employee_social_insurance_fen: string | null;
  employee_housing_fund_fen: string | null;
  settlement_adjustment_fen: string;
  prior_period_settlement_adjustment_fen: string;
  periods: WorkforcePeriod[];
}

export interface BriefLaborCost {
  has_activity: boolean;
  breakdown_available: boolean;
  reason: string | null;
  total_fen: string;
  gross_remuneration_fen: string | null;
  actual_withholding_tax_fen: string | null;
  unwithheld_tax_fen: string | null;
  withholding_status: string;
  periods: WorkforcePeriod[];
}

export interface BriefWorkforceCost {
  has_activity: boolean;
  total_fen: string;
  employee: BriefEmployeeCost;
  personal_labor: BriefLaborCost;
}

export interface BriefValidationItem {
  key: string;
  label: string;
  state: "pass" | "pending" | "error" | "neutral";
  text: string;
}

export interface BriefValidation {
  state: "complete" | "attention" | "error";
  title: string;
  summary: string;
  integrity_valid: boolean;
  attention_count: number;
  items: BriefValidationItem[];
}

export interface BriefData {
  generated_at: string;
  management_commentary: string;
  voucher_count: number;
  line_count: number;
  total_debit_fen: string;
  total_credit_fen: string;
  vouchers: BriefVoucher[];
  activity_groups: BriefActivityGroup[];
  position: BriefPosition;
  cash: BriefCash;
  unmatched_bank_activity: {
    count: number;
    ordinary_count: number;
    pending_late_count: number;
    inflow_fen: string;
    outflow_fen: string;
    rows: BriefBankRow[];
  };
  open_items: BriefOpenItems;
  workforce_cost: BriefWorkforceCost;
  long_term_assets: {
    net_fen: string;
    fixed_net_fen: string;
    intangible_net_fen: string;
    fixed_active_count: number;
    intangible_active_count: number;
  };
  validation: BriefValidation;
}

export interface BriefResponse {
  schema_version: 1;
  selected_period: DashboardPeriod | null;
  data: BriefData | null;
}

export function fetchBrief(period: string | null, signal?: AbortSignal) {
  const query = period ? `?period=${encodeURIComponent(period)}` : "";
  return requestJson<BriefResponse>(`/api/dashboard/brief${query}`, { signal });
}
