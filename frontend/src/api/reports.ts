import { DashboardApiError, requestGeneratedDashboard, type DashboardDeliveryAttempt } from "./client";
import { requestLocalJson, LocalApiError } from "./localKernel";
import type { DashboardQuarterlyReportContract, DashboardQuarterlyReportResponse } from "./generated/dashboardQuarterlyReport";
import type { DashboardQuarterlyReportDeliveryResponse } from "./generated/dashboardQuarterlyReportDelivery";
import type { ReportExportStatusResponse } from "./generated/reportExportStatus";
import { validateDashboardQuarterlyReportResponse } from "./generated/dashboardQuarterlyReport.js";
import { validateDashboardQuarterlyReportDeliveryResponse } from "./generated/dashboardQuarterlyReportDelivery.js";

export type QuarterlyReport = DashboardQuarterlyReportResponse;
export type DeferredQuarterlyReport = DashboardQuarterlyReportResponse;
export type ReportStatement = DashboardQuarterlyReportContract.ReportStatement;
export type ReportStatementRow = DashboardQuarterlyReportContract.ReportStatementRow;

function matchesRequest(url: URL, response: DashboardQuarterlyReportResponse) {
  const companyId = url.searchParams.get("company_id");
  const year = Number(url.searchParams.get("year"));
  const quarter = Number(url.searchParams.get("quarter"));
  return response.read_context.company_id === companyId
    && response.period.year === year
    && response.period.quarter === quarter;
}

function inspectDelivery(delivery: DashboardQuarterlyReportDeliveryResponse): DashboardDeliveryAttempt<DashboardQuarterlyReportResponse> {
  if (delivery.state === "ready") {
    return { state: "ready", response: delivery.response, modelVersion: delivery.model_version };
  }
  if (delivery.state === "preparing") return { state: "preparing", retryAfterMs: delivery.retry_after_ms };
  return { state: "failed", reason: delivery.reason };
}

function matchesDeliveryRequest(url: URL, delivery: DashboardQuarterlyReportDeliveryResponse): boolean {
  const companyId = url.searchParams.get("company_id");
  const year = Number(url.searchParams.get("year"));
  const quarter = Number(url.searchParams.get("quarter"));
  const period = `${year}-Q${quarter}`;
  if (companyId !== delivery.company_id || delivery.period !== period) return false;
  return delivery.state !== "ready"
    || (delivery.model_version === delivery.response.read_context.read_version
      && delivery.response.read_context.company_id === delivery.company_id
      && `${delivery.response.period.year}-Q${delivery.response.period.quarter}` === delivery.period);
}

export function fetchDeferredQuarterlyReport(companyId: string, year: number, quarter: number, signal?: AbortSignal, onPreparing?: () => void) {
  const query = new URLSearchParams({ company_id: companyId, year: String(year), quarter: String(quarter), preparation: "deferred" });
  return requestGeneratedDashboard(
    `/api/dashboard/quarterly-report?${query}`, "/api/dashboard/quarterly-report", validateDashboardQuarterlyReportDeliveryResponse,
    inspectDelivery, matchesDeliveryRequest, validateDashboardQuarterlyReportResponse, matchesRequest, { signal, onPreparing },
  );
}

export async function requestQuarterlyExport(companyId: string, report: QuarterlyReport, requestId: string, signal?: AbortSignal) {
  if (!report.export.available || !report.export.preview_digest || !report.export.epochs) {
    throw new DashboardApiError(409, "REPORT_EXPORT_UNAVAILABLE", "季度报表尚未准备完成，当前不能导出。");
  }
  const previewDigest = report.export.preview_digest;
  const body = JSON.stringify({
    company_id: companyId, year: report.period.year, quarter: report.period.quarter,
    preview_digest: report.export.preview_digest, epochs: report.export.epochs, request_id: requestId,
  });
  const { validateReportExportReceiptResponse } = await import("./generated/reportExportReceipt.js");
  signal?.throwIfAborted();
  const result = await requestLocalJson("/api/local/report-export", { method: "POST", signal, body });
  if (!validateReportExportReceiptResponse(result) || result.preview_digest !== previewDigest) {
    throw new DashboardApiError(502, "REPORT_JOB_RESPONSE", "报表任务响应无法读取，请重新生成报表。");
  }
  return result;
}

export async function fetchReportExportStatus(companyId: string, jobId: string, signal?: AbortSignal): Promise<ReportExportStatusResponse> {
  const query = new URLSearchParams({ company_id: companyId });
  const path = `/api/local/report-export/${encodeURIComponent(jobId)}/status?${query}`;
  const { validateReportExportStatusResponse } = await import("./generated/reportExportStatus.js");
  signal?.throwIfAborted();
  const result = await requestLocalJson(path, { signal });
  if (!validateReportExportStatusResponse(result) || result.company_id !== companyId || result.job_id !== jobId) {
    throw new LocalApiError(502, "REPORT_STATUS_RESPONSE", "报表生成状态无法读取，请重试。");
  }
  return result;
}

export async function fetchQuarterlyWorkbook(companyId: string, jobId: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ company_id: companyId });
  const response = await fetch(`/api/local/report-export/${encodeURIComponent(jobId)}/download?${query}`, {
    headers: { Accept: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
    signal, cache: "no-store", credentials: "same-origin", redirect: "error",
  });
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("finance-session-expired"));
    const payload: unknown = await response.json().catch(() => null);
    const code = payload && typeof payload === "object" && "code" in payload && typeof payload.code === "string" ? payload.code : "REPORT_EXPORT_FAILED";
    const message = payload && typeof payload === "object" && "message" in payload && typeof payload.message === "string" ? payload.message : "报表文件尚不可下载，请刷新任务状态后重试。";
    throw new LocalApiError(response.status, code, message);
  }
  return response.blob();
}
