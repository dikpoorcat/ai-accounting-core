import { requestJson } from "./client";
import { pageQuery, type BusinessIssue, type DashboardCollections, type DashboardPageQuery, type PublicationAdoption } from "./dashboardContracts";
export interface BusinessSettlements {
  status: string;
  cutoff_period: string;
  current_cutoff_period?: string;
  complete?: boolean;
  unestablished_state_selections?: Array<{ candidates: Array<{ calculation_id: string; [key: string]: unknown }>; reason?: string; [key: string]: unknown }>;
  obligations: Array<Record<string, unknown>>;
  issues: BusinessIssue[];
}
export interface AsPostedAccounting {
  cutoff_period: string;
  status: string;
  voucher_events: Array<{ calculation_id?: string; [key: string]: unknown }>;
  state_results: Array<{ calculation_id: string; [key: string]: unknown }>;
  unestablished_state_selections: Array<{ candidates: Array<{ calculation_id: string; [key: string]: unknown }>; reason?: string; [key: string]: unknown }>;
}
export interface BusinessStatusData {
  identity: { subject_id: string; kind: string; company_id: string; database_id: string };
  period: string;
  review: { status: string };
  closure: { state: "exact_close" | "covered_by_later_close" | "open"; close_period?: string; digest?: string };
  as_posted: AsPostedAccounting;
  current_business_result: Record<string, unknown> | null;
  frozen_adoption: PublicationAdoption | null;
  settlements: BusinessSettlements;
  current_followups?: { settlements: BusinessSettlements; [key: string]: unknown };
  external: Record<string, unknown>;
  collections: DashboardCollections;
  [key: string]: unknown;
}
export interface BusinessStatusQuery extends DashboardPageQuery {
  settlement_view?: "historical" | "current";
}
export function fetchBusinessStatus(period: string, subjectId: string, signal?: AbortSignal, options: BusinessStatusQuery = {}) {
  const query = new URLSearchParams({ period, subject_id: subjectId });
  pageQuery(query, options);
  if (options.settlement_view) query.set("settlement_view", options.settlement_view);
  return requestJson<{ schema_version: 2; snapshot_version: string; data: BusinessStatusData }>(`/api/dashboard/business-status?${query}`, { signal });
}
