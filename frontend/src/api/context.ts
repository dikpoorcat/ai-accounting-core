import { requestDashboardContext } from "./client";
import type { DashboardContextContract, DashboardContextResponse } from "./generated/dashboardResponses";

export type DashboardContext = DashboardContextResponse;
export type DashboardPeriod = DashboardContextContract.DashboardPeriod;
export type DashboardQuarter = DashboardContextContract.DashboardQuarter;
export type DashboardCompany = DashboardContextContract.DashboardCompany;

export function fetchDashboardContext(signal?: AbortSignal) {
  return requestDashboardContext({ signal });
}
