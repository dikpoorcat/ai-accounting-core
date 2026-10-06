import { pageQuery, validDashboardCollections, type DashboardPageQuery } from "./dashboardContracts";
import { requestGeneratedJson } from "./client";
import type { DashboardBriefContract, DashboardBriefResponse } from "./generated/dashboardBrief";
import { validateDashboardBriefResponse } from "./generated/dashboardBrief.js";

export type BriefResponse = DashboardBriefResponse;
export type BriefData = DashboardBriefContract.BriefData;
export type BriefActivityGroup = DashboardBriefContract.BriefActivityGroup;
export type BriefActivityRow = DashboardBriefContract.BriefActivityRow;
export type BriefOpenItems = DashboardBriefContract.BriefOpenSummary;
export type BriefOpenCategory = DashboardBriefContract.BriefOpenCategory;
export type BriefOpenItem = DashboardBriefContract.BriefOpenItem;
export type BriefVoucher = DashboardBriefContract.OwnerBriefVoucher;

export interface BriefQuery extends DashboardPageQuery {
  section?: "activity" | "open_items" | "vouchers";
  voucher_version_id?: string;
  voucher_number?: number;
}

function pairedActivityVouchers(data: BriefData | null) {
  if (data === null) return true;
  const activity = data.collections.activity, vouchers = data.collections.vouchers;
  if (!activity || !vouchers) return false;
  const ids = [...new Set(activity.items.flatMap(item => item.voucher_version_id ? [item.voucher_version_id] : []))];
  return ids.length === vouchers.items.length
    && ids.every((id, index) => vouchers.items[index]?.voucher_version_id === id);
}

function matchesRequest(url: URL, response: DashboardBriefResponse) {
  const companyId = url.searchParams.get("company_id");
  const period = url.searchParams.get("period");
  const expectedVersion = url.searchParams.get("expected_version");
  const section = url.searchParams.get("section");
  const voucherVersionId = url.searchParams.get("voucher_version_id");
  const voucherNumber = url.searchParams.get("voucher_number");
  return response.read_context.company_id === companyId
    && validDashboardCollections(response.data)
    && (section !== null || response.data === null || (response.data.financial_position !== undefined && response.data.workforce_cost !== undefined))
    && (period === null || response.selected_period?.key === period)
    && (expectedVersion === null || response.snapshot_version === expectedVersion)
    && (section === null || (response.data !== null && section in response.data.collections))
    && ((section !== null && section !== "activity") || pairedActivityVouchers(response.data))
    && (response.data === null || voucherVersionId === null || response.data.focused_voucher?.voucher_version_id === voucherVersionId)
    && (response.data === null || voucherNumber === null || response.data.focused_voucher?.number === voucherNumber);
}

export function fetchDeferredBrief(companyId: string, period: string | null, signal?: AbortSignal, expectedVersion?: string | null, options: BriefQuery = {}) {
  const query = new URLSearchParams({ company_id: companyId, ...(period ? { period } : {}), ...(expectedVersion ? { expected_version: expectedVersion } : {}) });
  pageQuery(query, { ...options, limit: options.limit ?? 20 });
  if (options.voucher_version_id) query.set("voucher_version_id", options.voucher_version_id);
  if (options.voucher_number !== undefined) query.set("voucher_number", String(options.voucher_number));
  return requestGeneratedJson(`/api/dashboard/brief?${query}`, "/api/dashboard/brief", validateDashboardBriefResponse, matchesRequest, { signal });
}
