import { requestJson } from "./client";
import type { DashboardPeriod } from "./context";

export type Fen = string;

export interface EmployeeDashboardItem {
  code: string;
  name: string;
  record_status: string;
  period_state: "in_period" | "ended" | "not_started";
  period_state_label: string;
  in_period: boolean;
  employment_start_date: string;
  employment_end_date: string | null;
  tax_withholding_start_date: string | null;
  profile_available: boolean;
  expense_areas: string[];
  social_insurance_participating: boolean | null;
  housing_fund_participating: boolean | null;
  social_insurance_base_fen: Fen | null;
  housing_fund_base_fen: Fen | null;
  resident_employee: boolean | null;
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
  declaration_state: "none" | "declared" | "not_declared" | "not_applicable" | "mixed";
  declaration_label: string;
}

export interface EmployeesSummary {
  registered_count: number;
  in_period_count: number;
  payroll_count: number;
  without_payroll_count: number;
  profile_missing_count: number;
  declaration_attention_count: number;
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
  detail_reconciled: boolean;
  breakdown_available: boolean;
  breakdown_reason: string | null;
  items: EmployeeDashboardItem[];
  identity_note: string;
}

export interface WorkforcePeriod {
  total_fen: Fen;
  has_reversal: boolean;
}

export interface EmployeeWorkforceCost {
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
  actual_withholding_tax_fen: Fen | null;
  unwithheld_tax_fen: Fen | null;
  pending_theoretical_tax_fen: Fen | null;
  settled_gross_fen: Fen | null;
  unsettled_gross_fen: Fen | null;
  withholding_status: string;
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
  has_activity: boolean;
  total_fen: Fen;
  employee: EmployeeWorkforceCost;
  personal_labor: PersonalLaborWorkforceCost;
}

export interface EmployeesDashboardData {
  employees: EmployeesSummary;
  workforce_cost: WorkforceCost;
}

export interface EmployeesDashboardResponse {
  schema_version: 1;
  selected_period: DashboardPeriod | null;
  data: EmployeesDashboardData | null;
}

export function fetchEmployeesDashboard(periodKey: string | null, signal?: AbortSignal) {
  const query = periodKey ? `?period=${encodeURIComponent(periodKey)}` : "";
  return requestJson<EmployeesDashboardResponse>(`/api/dashboard/employees${query}`, {
    signal,
  });
}
