import { requestGeneratedJson } from "./client";
import type { DashboardCloseReviewResponse } from "./generated/dashboardCloseReview";
import { validateDashboardCloseReviewResponse } from "./generated/dashboardCloseReview.js";

export interface CloseReviewQuery { previewDigest?: string; }

function matchesRequest(url: URL, response: DashboardCloseReviewResponse) {
  const digest = url.searchParams.get("preview_digest");
  const noActiveReview = (response.state === "unprepared" || response.state === "covered") && response.owner_review === null;
  return response.company_id === url.searchParams.get("company_id")
    && response.period === url.searchParams.get("period")
    && (response.owner_review === null || response.owner_review.period === response.period)
    && (digest === null || response.preview_digest === digest || response.state === "stale" || noActiveReview);
}

export function fetchCloseReview(companyId: string, period: string, signal?: AbortSignal, options: CloseReviewQuery = {}) {
  const query = new URLSearchParams({ company_id: companyId, period });
  if (options.previewDigest) query.set("preview_digest", options.previewDigest);
  return requestGeneratedJson(`/api/dashboard/close-review?${query}`, "/api/dashboard/close-review", validateDashboardCloseReviewResponse, matchesRequest, { signal });
}
