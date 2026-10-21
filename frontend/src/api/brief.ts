import { pageQuery, validDashboardCollections, type DashboardPageQuery } from "./dashboardContracts";
import { requestGeneratedDashboard, requestGeneratedJson, type DashboardDeliveryAttempt } from "./client";
import type { DashboardBriefContract, DashboardBriefResponse } from "./generated/dashboardBrief";
import type { DashboardBriefDeliveryResponse } from "./generated/dashboardBriefDelivery";
import type { DashboardBriefGroupContract } from "./generated/dashboardBriefGroup";
import { validateDashboardBriefResponse } from "./generated/dashboardBrief.js";
import { validateDashboardBriefDeliveryResponse } from "./generated/dashboardBriefDelivery.js";

export type BriefResponse = DashboardBriefResponse;
export type BriefOverviewResponse = Extract<BriefResponse, { scope: "overview" }>;
export type BriefData = DashboardBriefContract.BriefData;
export type BriefActivityGroup = DashboardBriefContract.BriefActivityGroup;
export type BriefActivityDisplayGroup = DashboardBriefContract.BriefActivityDisplayGroup;
export type BriefOpenDisplayGroup = DashboardBriefContract.BriefOpenDisplayGroup;
export type BriefActivityRow = DashboardBriefContract.BriefActivityRow;
export type BriefOpenItems = DashboardBriefContract.BriefOpenSummary;
export type BriefOpenCategory = DashboardBriefContract.BriefOpenCategory;
export type BriefOpenItem = DashboardBriefGroupContract.BriefOpenItem;
export type BriefVoucher = DashboardBriefContract.OwnerBriefVoucher;

export interface BriefQuery extends DashboardPageQuery {
  section?: "activity" | "open_items" | "vouchers";
  voucher_version_id?: string;
  voucher_number?: number;
}

function matchesRequest(url: URL, response: DashboardBriefResponse) {
  const companyId = url.searchParams.get("company_id");
  const period = url.searchParams.get("period");
  const expectedVersion = url.searchParams.get("expected_version");
  const section = url.searchParams.get("section");
  const voucherVersionId = url.searchParams.get("voucher_version_id");
  const voucherNumber = url.searchParams.get("voucher_number");
  const overview = response.scope === "overview";
  return response.read_context.company_id === companyId
    && response.scope === (section ?? "overview")
    && !(section !== null && (voucherVersionId !== null || voucherNumber !== null))
    && validDashboardCollections(response.data)
    && (!overview || !response.data?.focused_activity || response.data.focused_activity.detail_scope_key === null || response.data.focused_activity.detail_scope_key === response.data.focused_activity.key)
    && (period === null || response.selected_period?.key === period)
    && (expectedVersion === null || response.snapshot_version === expectedVersion)
    && (section === null || response.data === null || section in response.data.collections)
    && (voucherVersionId === null || (overview && (response.data === null || response.data.focused_voucher?.voucher_version_id === voucherVersionId)))
    && (voucherNumber === null || (overview && (response.data === null || response.data.focused_voucher?.number === voucherNumber)));
}

function inspectDelivery(delivery: DashboardBriefDeliveryResponse): DashboardDeliveryAttempt<DashboardBriefResponse> {
  if (delivery.state === "ready") {
    return { state: "ready", response: delivery.response, modelVersion: delivery.model_version };
  }
  if (delivery.state === "preparing") return { state: "preparing", retryAfterMs: delivery.retry_after_ms };
  return { state: "failed", reason: delivery.reason };
}

function matchesDeliveryRequest(url: URL, delivery: DashboardBriefDeliveryResponse): boolean {
  const companyId = url.searchParams.get("company_id");
  const period = url.searchParams.get("period");
  if (companyId !== delivery.company_id || (period !== null && period !== delivery.period)) return false;
  return delivery.state !== "ready"
    || (delivery.model_version === delivery.response.snapshot_version
      && delivery.response.read_context.company_id === delivery.company_id
      && (delivery.period === null || delivery.response.selected_period?.key === delivery.period));
}

export function fetchDeferredBrief(companyId: string, period: string | null, signal?: AbortSignal, expectedVersion?: string | null, options: BriefQuery = {}, onPreparing?: () => void, retry = false) {
  const query = new URLSearchParams({ company_id: companyId, ...(period ? { period } : {}), ...(expectedVersion ? { expected_version: expectedVersion } : {}) });
  pageQuery(query, { ...options, limit: options.limit ?? 20 });
  if (options.voucher_version_id) query.set("voucher_version_id", options.voucher_version_id);
  if (options.voucher_number !== undefined) query.set("voucher_number", String(options.voucher_number));
  const path = `/api/dashboard/brief?${query}`;
  if (options.section || expectedVersion) {
    return requestGeneratedJson(path, "/api/dashboard/brief", validateDashboardBriefResponse, matchesRequest, { signal });
  }
  return requestGeneratedDashboard(
    path, "/api/dashboard/brief", validateDashboardBriefDeliveryResponse, inspectDelivery, matchesDeliveryRequest,
    validateDashboardBriefResponse, matchesRequest, { signal, onPreparing, retry },
  );
}
