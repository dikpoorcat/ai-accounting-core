import { requestGeneratedJson } from "./client";
import type { DashboardCloseReviewResponse } from "./generated/dashboardResponses";
import { validateDashboardCloseReviewResponse } from "./generated/dashboardValidators.js";

export type CloseReviewSection = "vouchers" | "adopted_bases" | "policies" | "payroll_confirmations" | "evidence";
export type CloseReviewCollection = NonNullable<DashboardCloseReviewResponse["collection"]>;
export type CloseReviewCollections = Partial<Record<CloseReviewSection, CloseReviewCollection>>;

export interface CloseReviewQuery {
  previewDigest?: string;
  section?: CloseReviewSection;
  cursor?: string;
  limit?: number;
}

export function closeReviewBinding(response: DashboardCloseReviewResponse | null): string | null {
  if (response?.state === "closed") return response.close_digest;
  if (response?.state === "prepared") return response.preview_digest;
  return null;
}

export function mergeCloseReviewSection(
  current: DashboardCloseReviewResponse | null,
  collections: CloseReviewCollections,
  result: DashboardCloseReviewResponse,
  section: CloseReviewSection,
  more: boolean,
) {
  const bindingChanged = current?.state !== result.state || closeReviewBinding(current) !== closeReviewBinding(result);
  if (!result.collection) return { bindingChanged, collections: bindingChanged ? {} : collections };
  const existing = bindingChanged ? undefined : collections[section];
  return {
    bindingChanged,
    collections: {
      ...(bindingChanged ? {} : collections),
      [section]: {
        ...result.collection,
        items: [...(more ? existing?.items ?? [] : []), ...result.collection.items],
      },
    },
  };
}

function matchesRequest(url: URL, response: DashboardCloseReviewResponse) {
  const companyId = url.searchParams.get("company_id");
  const period = url.searchParams.get("period");
  const digest = url.searchParams.get("preview_digest");
  const section = url.searchParams.get("section");
  const noActiveReview = (response.state === "unprepared" || response.state === "covered")
    && response.owner_review === null && response.collection === null;
  return response.company_id === companyId
    && response.period === period
    && (digest === null || response.preview_digest === digest || response.state === "stale" || noActiveReview)
    && (noActiveReview || (section === null ? response.collection === null : response.collection === null || response.collection.section === section));
}

export function fetchCloseReview(companyId: string, period: string, signal?: AbortSignal, options: CloseReviewQuery = {}) {
  const query = new URLSearchParams({ company_id: companyId, period, limit: String(options.limit ?? 50) });
  if (options.previewDigest) query.set("preview_digest", options.previewDigest);
  if (options.section) query.set("section", options.section);
  if (options.cursor) query.set("cursor", options.cursor);
  return requestGeneratedJson(
    `/api/dashboard/close-review?${query}`,
    "/api/dashboard/close-review",
    validateDashboardCloseReviewResponse,
    matchesRequest,
    { signal },
  );
}
