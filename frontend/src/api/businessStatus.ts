import { pageQuery, validDashboardCollections, type DashboardPageQuery } from "./dashboardContracts";
import { requestGeneratedJson } from "./client";
import type { DashboardBusinessStatusContract, DashboardBusinessStatusResponse } from "./generated/dashboardResponses";
import { validateDashboardBusinessStatusResponse } from "./generated/dashboardValidators.js";

export type BusinessStatusData = DashboardBusinessStatusContract.BusinessStatusData;
export type BusinessStatusResponse = DashboardBusinessStatusResponse;

export interface BusinessStatusQuery extends DashboardPageQuery {
  settlement_view?: "historical" | "current";
}

function matchesRequest(url: URL, response: DashboardBusinessStatusResponse) {
  const companyId = url.searchParams.get("company_id");
  const period = url.searchParams.get("period");
  const subjectId = url.searchParams.get("subject_id");
  const expectedVersion = url.searchParams.get("expected_version");
  const section = url.searchParams.get("section");
  return response.read_context.company_id === companyId
    && validDashboardCollections(response.data)
    && response.data.identity.subject_id === subjectId
    && (period === null || response.selected_period.key === period)
    && (expectedVersion === null || response.snapshot_version === expectedVersion)
    && (section === null || section in response.data.collections);
}

export function fetchBusinessStatus(period: string, subjectId: string, signal?: AbortSignal, options: BusinessStatusQuery = {}) {
  const query = new URLSearchParams({ period, subject_id: subjectId });
  pageQuery(query, options);
  if (options.settlement_view) query.set("settlement_view", options.settlement_view);
  return requestGeneratedJson(`/api/dashboard/business-status?${query}`, "/api/dashboard/business-status", validateDashboardBusinessStatusResponse, matchesRequest, { signal });
}
