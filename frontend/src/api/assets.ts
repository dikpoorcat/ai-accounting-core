import { pageQuery, validDashboardCollections, type DashboardPageQuery } from "./dashboardContracts";
import { requestGeneratedJson } from "./client";
import type { DashboardAssetsContract, DashboardAssetsResponse } from "./generated/dashboardResponses";
import { validateDashboardAssetsResponse } from "./generated/dashboardValidators.js";

export type AssetsDashboardResponse = DashboardAssetsResponse;
export type AssetsDashboardData = DashboardAssetsContract.AssetsData;
export type AssetItem = DashboardAssetsContract.FixedAssetItem | DashboardAssetsContract.IntangibleAssetItem | DashboardAssetsContract.UnestablishedAsset;
export type EstablishedAssetItem = DashboardAssetsContract.FixedAssetItem | DashboardAssetsContract.IntangibleAssetItem;
export type FixedAssetItem = DashboardAssetsContract.FixedAssetItem;
export type IntangibleAssetItem = DashboardAssetsContract.IntangibleAssetItem;
export type UnestablishedAssetItem = DashboardAssetsContract.UnestablishedAsset;

export interface AssetsQuery extends DashboardPageQuery {
  section?: "assets" | "projects" | "source_history" | "settlement_events";
  asset_filter?: "all" | "active" | "fixed" | "intangible" | "pending" | "exited";
  asset_id?: string;
  project_id?: string;
}

function matchesRequest(url: URL, response: DashboardAssetsResponse) {
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

export function fetchAssetsDashboard(period: string, signal?: AbortSignal, options: AssetsQuery = {}) {
  const query = new URLSearchParams({ period, preparation: "deferred" });
  pageQuery(query, options);
  if (options.asset_filter) query.set("asset_filter", options.asset_filter);
  if (options.asset_id) query.set("asset_id", options.asset_id);
  if (options.project_id) query.set("project_id", options.project_id);
  return requestGeneratedJson(`/api/dashboard/assets?${query}`, "/api/dashboard/assets", validateDashboardAssetsResponse, matchesRequest, { signal });
}
