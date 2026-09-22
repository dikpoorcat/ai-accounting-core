import { pageQuery, validDashboardCollections, type DashboardPageQuery } from "./dashboardContracts";
import { requestGeneratedJson } from "./client";
import type { DashboardBriefContract, DashboardBriefResponse } from "./generated/dashboardResponses";
import { validateDashboardBriefResponse } from "./generated/dashboardValidators.js";

export type BriefResponse = DashboardBriefResponse;
export type DeferredBriefResponse = DashboardBriefResponse;
export type BriefData = DashboardBriefContract.BriefData;
export type BriefVoucher = DashboardBriefContract.BriefVoucher;
export type BriefVoucherLine = DashboardBriefContract.VoucherLine;
export type BriefComponent = DashboardBriefContract.VoucherComponent;
export type BriefFundMovement = DashboardBriefContract.VoucherFundMovement;
export type BriefSettlement = DashboardBriefContract.VoucherSettlement;
export type BriefAssetReference = DashboardBriefContract.AssetReference;
export type BriefActivityGroup = DashboardBriefContract.BriefActivityGroup;
export type BriefActivityRow = DashboardBriefContract.BriefActivityRow;
export type BriefOpenItems = DashboardBriefContract.BriefOpenItems;
export type BriefOpenCategory = DashboardBriefContract.OpenCategory;
export type BriefOpenItem = DashboardBriefContract.OpenItem;
export type BriefPosition = DashboardBriefContract.BriefPosition;
export type BriefWorkforceCost = DashboardBriefContract.WorkforceCost;
export type BriefValidation = DashboardBriefContract.BriefValidation;
export type BriefValidationItem = DashboardBriefContract.ValidationItem;

export interface BriefQuery extends DashboardPageQuery {
  section?: "vouchers" | "businesses" | "open_items" | "settlement_events" | "external_followups" | "file_jobs";
  voucher_version_id?: string;
  voucher_number?: number;
}

function matchesRequest(url: URL, response: DashboardBriefResponse) {
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

export function fetchDeferredBrief(companyId: string, period: string | null, signal?: AbortSignal, expectedVersion?: string | null, options: BriefQuery = {}) {
  const query = new URLSearchParams({ company_id: companyId, preparation: "deferred", ...(period ? { period } : {}), ...(expectedVersion ? { expected_version: expectedVersion } : {}) });
  pageQuery(query, options);
  if (options.voucher_version_id) query.set("voucher_version_id", options.voucher_version_id);
  if (options.voucher_number !== undefined) query.set("voucher_number", String(options.voucher_number));
  return requestGeneratedJson(`/api/dashboard/brief?${query}`, "/api/dashboard/brief", validateDashboardBriefResponse, matchesRequest, { signal });
}
