import { pageQuery, validDashboardCollections, type DashboardPageQuery } from "./dashboardContracts";
import { requestGeneratedJson } from "./client";
import type { DashboardEmployeesContract, DashboardEmployeesResponse } from "./generated/dashboardResponses";
import { validateDashboardEmployeesResponse } from "./generated/dashboardValidators.js";

export type EmployeesDashboardResponse = DashboardEmployeesResponse;
export type EmployeesDashboardData = DashboardEmployeesContract.EmployeesData;
export type EmployeesSummary = DashboardEmployeesContract.EmployeeSummary;
export type EmployeeDashboardItem = DashboardEmployeesContract.EmployeeItem | DashboardEmployeesContract.UnestablishedEmployee;
export type EstablishedEmployeeItem = DashboardEmployeesContract.EmployeeItem;
export type UnestablishedEmployeeItem = DashboardEmployeesContract.UnestablishedEmployee;
export type PayrollSource = DashboardEmployeesContract.PayrollSource;
export type PersonalLaborItem = DashboardEmployeesContract.LaborSource;
export type WorkforceCost = DashboardEmployeesContract.WorkforceCost;

export interface EmployeesQuery extends DashboardPageQuery {
  section?: "employees" | "payroll_sources" | "labor_sources" | "settlement_events";
  employee_filter?: "all" | "in_period" | "payroll" | "no_payroll" | "unknown" | "ended";
  employee_id?: string;
}

function matchesRequest(url: URL, response: DashboardEmployeesResponse) {
  const companyId = url.searchParams.get("company_id");
  const period = url.searchParams.get("period");
  const expectedVersion = url.searchParams.get("expected_version");
  const section = url.searchParams.get("section");
  return response.read_context.company_id === companyId
    && validDashboardCollections(response.data)
    && (period === null || response.selected_period?.key === period)
    && (expectedVersion === null || response.snapshot_version === expectedVersion)
    && (section === null || (response.data !== null && section in response.data.collections));
}

export function fetchEmployeesDashboard(periodKey: string | null, signal?: AbortSignal, options: EmployeesQuery = {}) {
  const query = new URLSearchParams(periodKey ? { period: periodKey } : {});
  query.set("preparation", "deferred");
  pageQuery(query, options);
  if (options.employee_filter) query.set("employee_filter", options.employee_filter);
  if (options.employee_id) query.set("employee_id", options.employee_id);
  return requestGeneratedJson(`/api/dashboard/employees?${query}`, "/api/dashboard/employees", validateDashboardEmployeesResponse, matchesRequest, { signal });
}
