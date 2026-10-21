import { DashboardApiError } from "./client";
import { LocalApiError, requestLocalJson } from "./localKernel";
import type { ReviewMaterialsResponse, ReviewMaterialsContract } from "./generated/reviewMaterials";
import type { ReviewMaterialsQuery } from "./generated/reviewMaterialsQuery";
import { validateReviewMaterialsResponse } from "./generated/reviewMaterials.js";
import { validateReviewMaterialsQuery } from "./generated/reviewMaterialsQuery.js";
import { validateSourceContentQuery } from "./generated/sourceContentQuery.js";
import { validateSourceTextContent } from "./generated/sourceTextContent.js";
import { validateSourceFileReceipt } from "./generated/sourceFileReceipt.js";

export type { ReviewMaterialsResponse, ReviewMaterialsQuery };
export type MaterialSource = ReviewMaterialsContract.SourceItem;

function invalidResponse(): never {
  throw new DashboardApiError(502, "REVIEW_RESPONSE_MISMATCH", "资料响应与所选业务不一致，请重新读取。");
}

export function parseMaterialsQuery(parameters: URLSearchParams): ReviewMaterialsQuery {
  const query: Record<string, unknown> = {};
  for (const [key, value] of parameters) {
    if (key in query) throw new LocalApiError(400, "invalid_command", "查阅链接含有重复参数。");
    if ((key === "generation" || key === "limit") && (!/^[1-9][0-9]*$/.test(value) || !Number.isSafeInteger(Number(value)))) {
      throw new LocalApiError(400, "invalid_command", "查阅链接中的编号或分页数值无法准确读取。");
    }
    query[key] = key === "generation" || key === "limit" ? Number(value) : value;
  }
  if (!validateReviewMaterialsQuery(query)) {
    throw new LocalApiError(400, "invalid_command", "查阅链接不完整或无效，请向会计 AI 获取正确链接。");
  }
  return query;
}

function encode(query: object): string {
  return new URLSearchParams(Object.entries(query).filter(([, value]) => value !== undefined)
    .map(([key, value]) => [key, String(value)])).toString();
}

function sameSelection(left: ReviewMaterialsResponse["selection"], right: ReviewMaterialsResponse["selection"]): boolean {
  if (left.mode === "current") return right.mode === "current" && left.fact_seq === right.fact_seq;
  return right.mode === left.mode && right.result_id === left.result_id
    && right.generation === left.generation && right.posting_period === left.posting_period;
}

async function verifyContent(raw: ArrayBuffer, row: MaterialSource, signal: AbortSignal) {
  signal.throwIfAborted();
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", raw));
  signal.throwIfAborted();
  if (raw.byteLength !== row.byte_size || Array.from(digest, byte => byte.toString(16).padStart(2, "0")).join("") !== row.sha256) invalidResponse();
}

export async function fetchMaterials(query: ReviewMaterialsQuery, signal: AbortSignal, previous?: ReviewMaterialsResponse) {
  if (!validateReviewMaterialsQuery(query)) throw new LocalApiError(400, "invalid_command", "资料查阅范围无效。");
  const value = await requestLocalJson(`/api/review/materials?${encode(query)}`, { signal });
  signal.throwIfAborted();
  if (!validateReviewMaterialsResponse(value)
    || value.company_id !== query.company_id || value.business_id !== query.business_id
    || (query.result_id ? value.selection.mode === "current" || value.selection.result_id !== query.result_id || value.selection.generation !== query.generation : value.selection.mode !== "current")
    || (query.expected_version !== undefined && value.snapshot_version !== query.expected_version)
    || value.page.collection_version !== value.snapshot_version
    || value.page.returned_count !== value.sources.length || value.sources.length > (query.limit ?? 20)
    || value.page.returned_count > value.page.filtered_count || value.page.filtered_count !== value.page.total_count
    || (value.page.has_more && value.sources.length === 0)
    || value.page.has_more !== (value.page.next_cursor !== null)
    || value.sources.some((row, index) => index > 0 && row.source_id <= value.sources[index - 1]!.source_id)
    || (previous && (value.database_id !== previous.database_id || value.snapshot_version !== previous.snapshot_version
      || !sameSelection(value.selection, previous.selection)
      || value.page.total_count !== previous.page.total_count || value.page.filtered_count !== previous.page.filtered_count))) invalidResponse();
  return value;
}

function contentQuery(query: ReviewMaterialsQuery, response: ReviewMaterialsResponse, row: MaterialSource) {
  const selected = {
    company_id: response.company_id, business_id: response.business_id, source_id: row.source_id,
    expected_version: response.snapshot_version,
    ...(query.result_id ? { result_id: query.result_id, generation: query.generation } : {}),
  };
  if (!validateSourceContentQuery(selected) || query.company_id !== selected.company_id || query.business_id !== selected.business_id) invalidResponse();
  const { source_id, ...parameters } = selected;
  return `/api/review/sources/${encodeURIComponent(source_id)}/content?${encode(parameters)}`;
}

export async function fetchSourceText(query: ReviewMaterialsQuery, response: ReviewMaterialsResponse, row: MaterialSource, signal: AbortSignal) {
  const value = await requestLocalJson(contentQuery(query, response, row), { signal });
  signal.throwIfAborted();
  if (!validateSourceTextContent(value) || row.kind !== "owner_statement"
    || value.company_id !== response.company_id || value.database_id !== response.database_id
    || value.source_id !== row.source_id || value.sha256 !== row.sha256) invalidResponse();
  await verifyContent(new TextEncoder().encode(value.text).buffer, row, signal);
  return value.text;
}

export async function fetchSourceFile(query: ReviewMaterialsQuery, response: ReviewMaterialsResponse, row: MaterialSource, signal: AbortSignal) {
  const result = await fetch(contentQuery(query, response, row), {
    headers: { Accept: "application/octet-stream" }, credentials: "same-origin", cache: "no-store", redirect: "error", signal,
  });
  if (!result.ok) {
    if (result.status === 401) window.dispatchEvent(new Event("finance-session-expired"));
    const error: unknown = await result.json().catch(() => null);
    throw new LocalApiError(result.status,
      error && typeof error === "object" && "code" in error && typeof error.code === "string" ? error.code : "source_unavailable",
      error && typeof error === "object" && "message" in error && typeof error.message === "string" ? error.message : "原件暂时无法下载，请重新读取或联系会计 AI。");
  }
  const disposition = /^attachment;\s*filename\*=UTF-8''([^;]*)$/i.exec(result.headers.get("Content-Disposition") ?? "");
  let filename = "";
  try { filename = decodeURIComponent(disposition?.[1] ?? ""); } catch { invalidResponse(); }
  const length = result.headers.get("Content-Length") ?? "";
  const receipt = {
    company_id: result.headers.get("X-Accounting-Company-Id"), database_id: result.headers.get("X-Accounting-Database-Id"),
    source_id: result.headers.get("X-Accounting-Source-Id"), sha256: result.headers.get("X-Content-SHA256"),
    byte_size: /^(0|[1-9][0-9]*)$/.test(length) ? Number(length) : NaN,
    file_name: filename, content_type: result.headers.get("Content-Type"), disposition: disposition ? "attachment" : "",
  };
  if (!validateSourceFileReceipt(receipt) || row.kind !== "file"
    || receipt.company_id !== response.company_id || receipt.database_id !== response.database_id
    || receipt.source_id !== row.source_id || receipt.sha256 !== row.sha256 || receipt.byte_size !== row.byte_size
    || result.headers.get("X-Content-Type-Options") !== "nosniff") invalidResponse();
  const blob = await result.blob();
  signal.throwIfAborted();
  await verifyContent(await blob.arrayBuffer(), row, signal);
  return { blob, receipt };
}
