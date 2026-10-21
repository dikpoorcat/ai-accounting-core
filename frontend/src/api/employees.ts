import { pageQuery, validDashboardCollections, type DashboardPageQuery } from "./dashboardContracts";
import { requestGeneratedDashboard, requestGeneratedJson, type DashboardDeliveryAttempt } from "./client";
import type { DashboardEmployeesContract, DashboardEmployeesResponse } from "./generated/dashboardEmployees";
import type { DashboardEmployeesDeliveryResponse } from "./generated/dashboardEmployeesDelivery";
import { validateDashboardEmployeesResponse } from "./generated/dashboardEmployees.js";
import { validateDashboardEmployeesDeliveryResponse } from "./generated/dashboardEmployeesDelivery.js";

export type EmployeesDashboardResponse = DashboardEmployeesResponse;
export type EmployeesDashboardData = DashboardEmployeesContract.EmployeesData;
export type EmployeesSummary = DashboardEmployeesContract.EmployeeSummary;
export type EmployeeDashboardItem = DashboardEmployeesContract.EmployeeItem | DashboardEmployeesContract.UnestablishedEmployee;
export type EstablishedEmployeeItem = DashboardEmployeesContract.EmployeeItem;
export type UnestablishedEmployeeItem = DashboardEmployeesContract.UnestablishedEmployee;
export type PersonalLaborItem = DashboardEmployeesContract.LaborSource;
export type LaborWorkerItem = DashboardEmployeesContract.LaborWorkerItem;
export type WorkforceRelationshipSegment = DashboardEmployeesContract.WorkforceRelationshipSegment;
export type WorkforceCost = DashboardEmployeesContract.OwnerWorkforceCost;
export type EmployeeFilter = DashboardEmployeesContract.EmployeesData["employee_filter"];

export interface EmployeesQuery extends DashboardPageQuery {
  section?: "employees" | "labor_workers" | "labor_sources";
  employee_filter?: EmployeeFilter;
  employee_id?: string;
}

function matchesRequest(url: URL, response: DashboardEmployeesResponse) {
  const companyId = url.searchParams.get("company_id");
  const period = url.searchParams.get("period");
  const expectedVersion = url.searchParams.get("expected_version");
  const section = url.searchParams.get("section");
  return response.read_context.company_id === companyId
    && validDashboardCollections(response.data)
    && (response.data === null || (
      response.data.employee_id === url.searchParams.get("employee_id")
      && response.data.employee_filter === (url.searchParams.get("employee_filter") ?? "employment_active")
    ))
    && (period === null || response.selected_period?.key === period)
    && (expectedVersion === null || response.snapshot_version === expectedVersion)
    && (section === null || (response.data !== null && section in response.data.collections));
}

function inspectDelivery(delivery: DashboardEmployeesDeliveryResponse): DashboardDeliveryAttempt<DashboardEmployeesResponse> {
  if (delivery.state === "ready") {
    return { state: "ready", response: delivery.response, modelVersion: delivery.model_version };
  }
  if (delivery.state === "preparing") return { state: "preparing", retryAfterMs: delivery.retry_after_ms };
  return { state: "failed", reason: delivery.reason };
}

function matchesDeliveryRequest(url: URL, delivery: DashboardEmployeesDeliveryResponse): boolean {
  const companyId = url.searchParams.get("company_id");
  const period = url.searchParams.get("period");
  if (companyId !== delivery.company_id || (period !== null && period !== delivery.period)) return false;
  return delivery.state !== "ready"
    || (delivery.model_version === delivery.response.snapshot_version
      && delivery.response.read_context.company_id === delivery.company_id
      && (delivery.period === null || delivery.response.selected_period?.key === delivery.period));
}

export function fetchEmployeesDashboard(periodKey: string | null, signal?: AbortSignal, options: EmployeesQuery = {}, onPreparing?: () => void, retry = false) {
  const query = new URLSearchParams(periodKey ? { period: periodKey } : {});
  query.set("preparation", "deferred");
  pageQuery(query, options);
  if (options.employee_filter) query.set("employee_filter", options.employee_filter);
  if (options.employee_id) query.set("employee_id", options.employee_id);
  const path = `/api/dashboard/employees?${query}`;
  if (options.section || options.expected_version) {
    return requestGeneratedJson(path, "/api/dashboard/employees", validateDashboardEmployeesResponse, matchesRequest, { signal });
  }
  return requestGeneratedDashboard(
    path, "/api/dashboard/employees", validateDashboardEmployeesDeliveryResponse, inspectDelivery, matchesDeliveryRequest,
    validateDashboardEmployeesResponse, matchesRequest, { signal, onPreparing, retry },
  );
}
