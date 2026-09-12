import { requestJson } from "./client";
import type { DashboardPeriod } from "./context";
import { pageQuery, type DashboardCollections, type DashboardPage, type DashboardPageQuery, type PeriodPreparation } from "./dashboardContracts";
import type { BusinessIssue, UnestablishedSelection } from "./dashboardContracts";

export type Fen = string | null;
export type OpeningPayrollComponent = "net" | "withheld_tax" | "employee_social" | "employee_housing" | "employer_social" | "employer_housing";

export interface SettlementView {
  status?: string;
  cutoff_period?: string;
  issues?: BusinessIssue[];
  current_followups?: { status: string; issues: BusinessIssue[]; current_cutoff_period?: string; cutoff_semantics?: string; obligations: Array<Record<string, unknown>> };
  subject_id: string;
  settlement_view: "historical";
  movements_scope: "business_related_settlement_events";
  movements_page: DashboardPage;
  obligations: Array<{ key: string; name: string; amount_fen: Fen; remaining_fen: Fen; paid_fen: Fen; other_settled_fen: Fen }>;
  movements: Array<{ id: string; calculation_id: string; source_id: string; label: string; mode: string; period: string; date: string | null; amount_fen: Fen; obligation: string; party: string; reversal: boolean; relation_state: "resolved" | "unresolved"; source_business: { kind: string; subject_id: string } | null; source_calculation_id: string | null }>;
}

export interface PayrollSource extends SettlementView {
  source_id: string;
  calculation_id: string;
  kind: string;
  period: string;
  opening_period: string | null;
  component: OpeningPayrollComponent | null;
  label: string;
  declarations: Array<{ fact_id: string; revision: number; source: string; tax_period: string; recording_period: string; date: string | null; declared_tax_fen: string; recorded_later: boolean }>;
  disbursements: Array<{ calculation_id: string; recording_period: string; needs_review: boolean; matches_displayed_wage: boolean; original_net_fen: string; target_net_fen: string; held_fen: string; declared_tax_fen: string }>;
}

export interface EstablishedEmployeeItem {
  selection_status?: "established";
  employee_id: string;
  direct_net_payments_fen: Fen;
  other_net_settlements_fen: Fen;
  payroll_sources: PayrollSource[];
  payroll_source_page: DashboardPage;
  declared_tax_fen?: Fen;
  recorded_net_payments_fen?: Fen;
  tax_details?: Array<{
    calculation_id: string;
    period: string;
    kind: string;
    reversal: boolean;
    booked_tax_fen: Fen;
    calculated_tax_fen: Fen;
    actual_withholding_tax_fen: Fen;
    actual_withholding_fact_id: string | null;
  }>;
  code: string;
  name: string;
  record_status: "active" | "inactive" | "unknown";
  period_state: "in_period" | "ended" | "not_started" | "unknown";
  period_state_label: string;
  in_period: boolean | null;
  employment_start_date: string | null;
  employment_end_date: string | null;
  tax_withholding_start_date: string | null;
  profile_available: boolean;
  expense_areas: string[];
  social_insurance_participating: boolean | null;
  housing_fund_participating: boolean | null;
  social_insurance_base_fen: Fen | null;
  housing_fund_base_fen: Fen | null;
  has_payroll_activity: boolean;
  batch_count: number;
  payroll_periods: string[];
  has_annual_bonus: boolean;
  gross_salary_fen: Fen;
  annual_bonus_fen: Fen;
  employer_social_insurance_fen: Fen;
  employer_housing_fund_fen: Fen;
  employee_social_insurance_fen: Fen;
  employee_housing_fund_fen: Fen;
  individual_income_tax_fen: Fen;
  personal_deduction_fen: Fen;
  net_salary_fen: Fen;
  tax_reported_salary_fen: Fen;
  company_cost_fen: Fen;
  wage_tax_scope: "none" | "wage_income" | "contributions_only" | "not_applicable" | "mixed";
  wage_tax_scope_label: string;
}

export interface UnestablishedEmployeeItem {
  employee_id: string;
  name: string | null;
  selection_status: "unestablished";
  candidate_selections: UnestablishedSelection[];
  established_card?: EstablishedEmployeeItem;
  trace_targets: Array<{ calculation_id: string; voucher_version_id?: string | null }>;
}
export type EmployeeDashboardItem = EstablishedEmployeeItem | UnestablishedEmployeeItem;

export interface EmployeesSummary {
  unestablished_count?: number;
  registered_count: number;
  unknown_period_count?: number;
  in_period_count: number | null;
  payroll_count: number;
  without_payroll_count: number | null;
  profile_missing_count: number | null;
  contributions_only_count: number;
  gross_salary_fen: Fen;
  annual_bonus_fen: Fen;
  employer_social_insurance_fen: Fen;
  employer_housing_fund_fen: Fen;
  personal_deduction_fen: Fen;
  individual_income_tax_fen: Fen;
  net_salary_fen: Fen;
  controlled_cost_fen: Fen;
  settlement_adjustment_fen: Fen;
  ledger_cost_fen: Fen;
  detail_reconciled: boolean | null;
  breakdown_available: boolean;
  breakdown_reason: string | null;
  items: EmployeeDashboardItem[];
  identity_note: string;
}

export interface WorkforcePeriod {
  total_fen: Fen;
  has_reversal: boolean;
  has_amendment: boolean;
  correction_ids: string[];
}

export interface EmployeeWorkforceCost {
  annual_bonus_fen: Fen;
  has_activity: boolean;
  breakdown_available: boolean;
  reason: string | null;
  total_fen: Fen;
  controlled_total_fen: Fen;
  settlement_adjustment_fen: Fen;
  prior_period_settlement_adjustment_fen: Fen;
  gross_salary_fen: Fen | null;
  employer_social_insurance_fen: Fen | null;
  employer_housing_fund_fen: Fen | null;
  employee_social_insurance_fen: Fen | null;
  employee_housing_fund_fen: Fen | null;
  personal_withholding_fen: Fen | null;
  batch_count: number;
  periods: Array<
    WorkforcePeriod & {
      payroll_period: string;
      gross_salary_fen: Fen;
      employer_social_insurance_fen: Fen;
      employer_housing_fund_fen: Fen;
      employee_social_insurance_fen: Fen;
      employee_housing_fund_fen: Fen;
    }
  >;
}

export interface PersonalLaborWorkforceCost {
  has_activity: boolean;
  breakdown_available: boolean;
  reason: string | null;
  total_fen: Fen;
  gross_remuneration_fen: Fen | null;
  theoretical_withholding_tax_fen: Fen | null;
  booked_withholding_tax_fen: Fen | null;
  unwithheld_tax_fen: Fen | null;
  withholding_status: string;
  withholding_note: string;
  items: Array<SettlementView & {
    source_id: string; calculation_id: string; period: string; person_id: string; name: string;
    capitalized: boolean; project_id: string | null; gross_fen: Fen; net_fen: Fen;
    booked_tax_fen: Fen; theoretical_tax_fen: Fen; withholding_method: string; withholding_label: string;
  }>;
  settlement_modes: string[];
  batch_count: number;
  periods: Array<
    WorkforcePeriod & {
      remuneration_period: string;
      gross_remuneration_fen: Fen;
      theoretical_withholding_tax_fen: Fen;
    }
  >;
}

export interface WorkforceCost {
  capitalized_labor_fen: Fen;
  has_activity: boolean;
  total_fen: Fen;
  employee: EmployeeWorkforceCost;
  personal_labor: PersonalLaborWorkforceCost;
}

export interface EmployeesDashboardData {
  period_preparation: PeriodPreparation;
  collections: DashboardCollections;
  employees: EmployeesSummary;
  workforce_cost: WorkforceCost;
}

export interface EmployeesDashboardResponse {
  schema_version: 2;
  snapshot_version: string;
  selected_period: DashboardPeriod | null;
  data: EmployeesDashboardData | null;
}

export interface EmployeesQuery extends DashboardPageQuery {
  section?: "employees" | "payroll_sources" | "labor_sources" | "settlement_events";
  employee_filter?: "all" | "in_period" | "payroll" | "no_payroll" | "unknown" | "ended";
  employee_id?: string;
}

export function fetchEmployeesDashboard(periodKey: string | null, signal?: AbortSignal, options: EmployeesQuery = {}) {
  const query = new URLSearchParams(periodKey ? { period: periodKey } : {});
  pageQuery(query, options);
  if (options.employee_filter) query.set("employee_filter", options.employee_filter);
  if (options.employee_id) query.set("employee_id", options.employee_id);
  return requestJson<EmployeesDashboardResponse>(`/api/dashboard/employees?${query}`, {
    signal,
  });
}
