import type { DashboardPeriod } from "./context";
import { pageQuery, type DashboardCollections, type DashboardPageQuery, type PeriodPreparation, type BusinessIssue, type DashboardReadContext } from "./dashboardContracts";
import { requestJson } from "./client";
import type { EvidenceDetails } from "./localKernel";

export interface BriefVoucherLine {
  line_number: number;
  code: string;
  account: string;
  debit_fen: string;
  credit_fen: string;
  party: string;
  source_label: string;
  party_state: "known" | "multiple" | "name_missing" | "not_applicable" | "unresolved";
  parties: { id: string; name: string; amount_fen: string }[];
  component_id: string | null;
}

export interface BriefComponent {
  id: string;
  key: string;
  kind: string;
  group: string;
  label: string;
  description: string;
  amount_fen: string | null;
  amount_label: string;
  parties: string[];
  management: {
    version: number;
    display_names?: { counterparty?: string; beneficiary?: string; handler?: string };
    metadata: {
      purpose?: string;
      description?: string;
      counterparty?: { name?: string };
      beneficiary?: { name?: string };
    };
    history: Array<{ version: number; created_at: string; metadata: Record<string, unknown> }>;
  };
  facts: Record<string, unknown>;
  recognition?: { precision: "month" | "day"; period: string | null; date: string | null; label: string };
  derived: Record<string, unknown>;
  source_references: Array<{ type: string; value: string }>;
  party_sources?: Array<{ party_id: string; name: string; source: string | null; id?: string }>;
}

export interface BriefFundMovement {
  id: string; account_id: string; category: string; name: string;
  direction: "inflow" | "outflow"; amount_fen: string;
}
export interface BriefSettlement {
  id: string; label: string; account: string; amount_fen: string; change_fen: string;
  party: string; source_period: string; source_calculation_id: string;
  source_label?: string;
}

export interface BriefVoucher {
  calculation_id: string;
  voucher_version_id: string;
  reverses_version_id: string | null;
  business_amount_fen: string | null;
  business_amount_label: string;
  fund_inflow_fen: string;
  fund_outflow_fen: string;
  recognition?: { precision: "month" | "day"; period: string; date: string | null; label: string };
  number: string;
  date: string | null;
  type: string;
  state: string;
  summary: string;
  display_summary: string;
  list_summary: string;
  amount_fen: string;
  evidence: string[];
  evidence_details: EvidenceDetails[];
  components: BriefComponent[];
  funds: BriefFundMovement[];
  settlements: BriefSettlement[];
  lines: BriefVoucherLine[];
}

export interface BriefActivityRow {
  amount_label: string;
  calculation_id: string;
  voucher_version_id: string;
  date: string | null;
  recognition?: { precision: "month" | "day"; period: string; date: string | null; label: string };
  reference: string;
  title: string;
  subject: string;
  description: string;
  display_description: string;
  amount_fen: string | null;
  journal_total_fen: string;
  state: string;
  party: string;
  evidence: string[];
  evidence_details: EvidenceDetails[];
  components: BriefComponent[];
  funds: BriefFundMovement[];
  settlements: BriefSettlement[];
}

export interface BriefActivityGroup {
  key: string;
  label: string;
  event_count: number;
  loaded_count?: number;
  type_counts: Array<{ label: string; count: number }>;
  rows: BriefActivityRow[];
}

export interface BriefBankRow {
  id: string;
  date: string | null;
  party: string;
  memo: string;
  direction: "inflow" | "outflow";
  amount_fen: string;
  state: string;
}

export interface BriefCash {
  transaction_count: number;
  matched_count: number;
  unmatched_count: number;
  needs_review_count: number;
  missing_account_count?: number;
  coverage_state: "missing" | "partial" | "complete" | "not_applicable";
  inflow_fen: string | null;
  outflow_fen: string | null;
  net_fen: string | null;
}

export interface BriefPosition {
  assets_fen: string | null;
  liabilities_fen: string | null;
  capital_fen: string | null;
  bank_fen: string;
  fixed_asset_cost_fen: string;
  accumulated_depreciation_fen: string;
  fixed_asset_net_fen: string | null;
  intangible_asset_cost_fen: string;
  accumulated_amortization_fen: string;
  intangible_asset_net_fen: string | null;
  other_assets_fen: string | null;
  month_revenue_fen: string | null;
  month_expense_fen: string | null;
  month_result_fen: string | null;
  cumulative_result_fen: string | null;
  equation_valid: boolean | null;
  complete: boolean;
  issues: Array<{ field: string; message: string }>;
}

export interface BriefOpenItem {
  source_business?: { subject_id: string; kind: string };
  id: string;
  voucher: string;
  party_key: string;
  party: string;
  description: string;
  status: "open" | "partial" | string;
  outstanding_fen: string | null;
}

export interface BriefOpenCategory {
  key: string;
  label: string;
  direction: "receivable" | "payable";
  unit: string;
  count: number;
  outstanding_fen: string | null;
  groups: Array<{
    key: string;
    party: string;
    count: number;
    outstanding_fen: string | null;
    open_count: number;
    partial_count: number;
  }>;
  items: BriefOpenItem[];
}

export interface BriefOpenItems {
  complete: boolean;
  unestablished_count: number;
  issues: BusinessIssue[];
  cutoff_period: string;
  current_cutoff_period: string;
  receivable_count: number;
  receivable_fen: string | null;
  payable_count: number;
  payable_fen: string | null;
  total_count: number;
  current_outstanding?: {
    receivable_count: number;
    receivable_fen: string | null;
    payable_count: number;
    payable_fen: string | null;
    total_count: number;
  };
  categories: BriefOpenCategory[];
}

export interface WorkforcePeriod {
  payroll_period?: string;
  remuneration_period?: string;
  total_fen: string;
  has_reversal: boolean;
  has_amendment: boolean;
  correction_ids: string[];
}

export interface BriefEmployeeCost {
  annual_bonus_fen: string | null;
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
  booked_withholding_tax_fen: string | null;
  unwithheld_tax_fen: string | null;
  withholding_status: string;
  withholding_note: string;
  periods: WorkforcePeriod[];
}

export interface BriefWorkforceCost {
  capitalized_labor_fen: string | null;
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
  integrity_valid: boolean | null;
  attention_count: number;
  items: BriefValidationItem[];
  issues?: Array<{ field?: string; message: string; subject_id?: string; period?: string }>;
}

export interface BriefData {
  period_preparation: PeriodPreparation;
  collections: DashboardCollections;
  focused_voucher: BriefVoucher | null;
  funds_overview: {
    total_fen: string | null; bank_fen: string | null; cash_fen: string | null; payment_platform_fen: string | null;
    inflow_fen: string; outflow_fen: string; net_change_fen: string; internal_transfer_fen: string;
  };
  management_commentary_details?: {
    status: "frozen" | "current" | "stale" | "not_provided";
    current: { text: string; revision: number } | null;
    frozen: { text: string; revision: number } | null;
    latest: { text: string; revision: number } | null;
    supplements: { id: string; text: string; revision: number; supplementary: boolean }[];
  };
  material_completeness: {
    closed: boolean;
    satisfied: boolean;
    revision?: number;
    company_notes?: { path: string; sha256: string; exists: boolean };
    issues: Array<{
      code: string;
      message: string;
      location?: string;
      source_name?: string;
      excerpt?: string;
      difference_fen?: string;
    }>;
  };
  generated_at: string;
  management_commentary: string;
  voucher_count: number;
  line_count: number;
  total_debit_fen: string;
  total_credit_fen: string;
  vouchers: BriefVoucher[];
  voucher_page: { has_more: boolean; next_after_number: number | null; total_count: number };
  activity_groups: BriefActivityGroup[];
  position: BriefPosition;
  cash: BriefCash;
  unmatched_bank_activity: {
    count: number;
    inflow_fen: string;
    outflow_fen: string;
    rows: BriefBankRow[];
    rows_truncated: boolean;
  };
  open_items: BriefOpenItems;
  workforce_cost: BriefWorkforceCost;
  long_term_assets: {
    net_fen: string | null;
    fixed_net_fen: string | null;
    intangible_net_fen: string | null;
    fixed_active_count: number;
    intangible_active_count: number;
    pending_count: number;
    project_cost_fen: string | null;
  };
  validation: BriefValidation;
}

export interface BriefResponse {
  schema_version: 2;
  snapshot_version: string | null;
  selected_period: DashboardPeriod | null;
  data: BriefData | null;
}

export type DeferredBriefData = Omit<BriefData, "period_preparation" | "material_completeness" | "validation"> & {
  period_preparation: null;
  material_completeness: null;
  validation: Omit<BriefValidation, "state"> & { state: "pending" | "attention" | "error" };
};
export type DeferredBriefResponse = Omit<BriefResponse, "data"> & {
  projection: "dashboard_brief_deferred";
} & ({ data: DeferredBriefData; read_context: DashboardReadContext } | { data: null; read_context: null });

export interface BriefQuery extends DashboardPageQuery {
  section?: "vouchers" | "businesses" | "open_items" | "settlement_events" | "external_followups" | "file_jobs";
  voucher_version_id?: string;
  voucher_number?: number;
}

export function fetchBrief(period: string | null, signal?: AbortSignal, afterNumber = 0, expectedVersion?: string | null, options: BriefQuery = {}) {
  const query = new URLSearchParams({ after_number: String(afterNumber), limit: "100", ...(period ? { period } : {}), ...(expectedVersion ? { expected_version: expectedVersion } : {}) });
  pageQuery(query, options);
  if (options.voucher_version_id) query.set("voucher_version_id", options.voucher_version_id);
  if (options.voucher_number !== undefined) query.set("voucher_number", String(options.voucher_number));
  return requestJson<BriefResponse>(`/api/dashboard/brief?${query}`, { signal });
}

export function fetchDeferredBrief(companyId: string, period: string | null, signal?: AbortSignal, expectedVersion?: string | null, options: BriefQuery = {}) {
  const query = new URLSearchParams({ company_id: companyId, preparation: "deferred", after_number: "0", ...(period ? { period } : {}), ...(expectedVersion ? { expected_version: expectedVersion } : {}) });
  pageQuery(query, options);
  if (options.voucher_version_id) query.set("voucher_version_id", options.voucher_version_id);
  if (options.voucher_number !== undefined) query.set("voucher_number", String(options.voucher_number));
  return requestJson<DeferredBriefResponse>(`/api/dashboard/brief?${query}`, { signal });
}
