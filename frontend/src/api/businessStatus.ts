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
export interface DuplicateSignal {
  code: string;
  evidence?: string[];
  source_locations?: Array<{ evidence_digest: string; location: string }>;
  distinct_locations_proven?: boolean;
}
export interface DuplicateCandidate {
  subject_id: string;
  fact_id: string | null;
  kind: string;
  period: string;
  signals: DuplicateSignal[];
}
export interface DuplicateCheckDetails {
  status: "clear" | "review_required";
  strong_candidates: DuplicateCandidate[];
  weak_candidates: DuplicateCandidate[];
  unresolved: Array<{ message: string; candidate_subject_id: string; review_period: string; signals: DuplicateSignal[] }>;
  checks: Array<{ check_id: string; action: "clear" | "reuse_existing" | "create_separate"; explanation: string; created_at: string }>;
  check_count: number;
  checks_truncated: boolean;
}
export interface IdentityCorrectionDetails {
  id: string;
  action: string;
  before_fact_id: string;
  after_fact_id: string | null;
  replacement_subject_id: string | null;
  digest: string;
}
export interface EntityReferenceDetails {
  fact_id: string;
  path: string;
  recorded_entity_id: string;
  current_entity_id: string;
  role: string;
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
  duplicate_checks: DuplicateCheckDetails;
  identity_corrections: IdentityCorrectionDetails[];
  entity_references: EntityReferenceDetails[];
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
  return requestJson<{ schema_version: 3; snapshot_version: string; data: BusinessStatusData }>(`/api/dashboard/business-status?${query}`, { signal });
}
