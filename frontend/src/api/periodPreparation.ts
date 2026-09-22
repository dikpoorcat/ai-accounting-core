import { DashboardApiError, requestGeneratedJson } from "./client";
import type { DashboardReadContext } from "./dashboardContracts";
import type { DashboardPeriodPreparationResponse } from "./generated/dashboardResponses";
import { validateDashboardPeriodPreparationResponse } from "./generated/dashboardValidators.js";

export type PeriodPreparationResult = DashboardPeriodPreparationResponse;

function matchesRequest(url: URL, response: DashboardPeriodPreparationResponse) {
  return response.read_context.company_id === url.searchParams.get("company_id")
    && response.period === url.searchParams.get("period")
    && response.read_context.read_version === url.searchParams.get("expected_read_version")
    && response.read_context.as_of === url.searchParams.get("as_of");
}

export async function fetchPeriodPreparation(context: DashboardReadContext, period: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ company_id: context.company_id, period, expected_read_version: context.read_version, as_of: context.as_of });
  const result = await requestGeneratedJson(`/api/dashboard/period-preparation?${query}`, "/api/dashboard/period-preparation", validateDashboardPeriodPreparationResponse, matchesRequest, { signal });
  if (result.read_context.database_id !== context.database_id) {
    throw new DashboardApiError(409, "dashboard_snapshot_changed", "资料已变化，请刷新主页面后重新核对。");
  }
  return result;
}
