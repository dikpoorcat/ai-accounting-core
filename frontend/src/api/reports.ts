import { DashboardApiError, requestJson } from "./client";
import { requestLocalJson, LocalApiError } from "./localKernel";
import type { DashboardReadContext, PeriodPreparation } from "./dashboardContracts";

export type ReportStatus =
  | "ready"
  | "blocked"
  | "in_progress"
  | "not_applicable"
  | "error";

export interface ReportReadinessDetail {
  primary: string;
  secondary: string;
  amount_fen?: string | null;
  location?: { field: string; period?: string; voucher_number?: number; voucher_version_id?: string; line_no?: number; subject_id?: string; account?: string };
}

export interface ReportReadinessItem {
  key: string;
  label: string;
  state: "pass" | "pending" | "attention";
  summary: string;
  details: ReportReadinessDetail[];
}

export interface ReportSummary {
  assets_total_fen: string | null;
  liabilities_equity_total_fen: string | null;
  current_net_profit_fen: string | null;
  year_to_date_net_profit_fen: string | null;
  current_cash_change_fen: string | null;
  ending_cash_fen: string | null;
}

export interface ReportStatementColumn {
  key: string;
  label: string;
}

export interface ReportStatementRow {
  line: number;
  name: string;
  values: Record<string, string | null>;
  is_total: boolean;
  has_amount: boolean;
}

export interface ReportStatement {
  key: string;
  label: string;
  columns: ReportStatementColumn[];
  rows: ReportStatementRow[];
}

export interface ReportCheck {
  code: string;
  label: string;
  passed: boolean | null;
}

export interface QuarterlyReport {
  period_preparations: PeriodPreparation[];
  schema_version: 1;
  status: ReportStatus;
  status_label: string;
  headline: string;
  message: string;
  checked_at: string;
  organization?: {
    name: string | null;
    taxpayer_identification_number: string | null;
  };
  period: {
    year: number;
    quarter: number;
    label: string;
    quarter_start?: string;
    quarter_end: string;
  };
  readiness: ReportReadinessItem[];
  summary: ReportSummary;
  statements: ReportStatement[];
  checks: {
    passed: number;
    total: number;
    items: ReportCheck[];
  };
  draft: boolean;
  close_state: "open" | "closed";
  readiness_state: "ready" | "blocked";
  carry_forward: {
    selected_fact_id: string | null;
    options: { fact_id: string; subject_id: string; revision: number; period: string; label: string; evidence_count: number; used: boolean }[];
  };
  export: {
    available: boolean;
    file_name: string;
    calculation_hash: string | null;
    preview_digest: string | null;
    epochs: { accounting: number; material: number; management: number } | null;
  };
  technical: {
    calculation_hash: string | null;
    template: {
      file_name?: string;
      profile?: string;
      sha256?: string;
    };
    rule: {
      version?: string;
    };
    source_close_hashes: string[];
    classification_count: number | null;
    income_tax_confirmation_count: number | null;
    requirement_codes: string[];
    errors: string[];
  };
}

export async function fetchQuarterlyReport(
  year: number,
  quarter: number,
  signal?: AbortSignal,
  carryForwardFactId?: string,
) {
  const query = new URLSearchParams({
    year: String(year),
    quarter: String(quarter),
  });
  if (carryForwardFactId) query.set("carry_forward_fact_id", carryForwardFactId);
  const report = await requestJson<QuarterlyReport>(
    `/api/dashboard/quarterly-report?${query}`,
    { signal },
  );
  if (report.schema_version !== 1) {
    throw new DashboardApiError(
      502,
      "REPORT_SCHEMA_MISMATCH",
      "季度报表响应无法识别，请重启本地看板服务后重试。",
    );
  }
  return report;
}

export type DeferredQuarterlyReport = Omit<QuarterlyReport, "period_preparations"> & {
  projection: "dashboard_quarterly_report_deferred";
  read_context: DashboardReadContext;
  period_preparations: null;
};

export function fetchDeferredQuarterlyReport(companyId: string, year: number, quarter: number, signal?: AbortSignal, carryForwardFactId?: string) {
  const query = new URLSearchParams({ company_id: companyId, year: String(year), quarter: String(quarter), preparation: "deferred" });
  if (carryForwardFactId) query.set("carry_forward_fact_id", carryForwardFactId);
  return requestJson<DeferredQuarterlyReport>(`/api/dashboard/quarterly-report?${query}`, { signal });
}

export async function requestQuarterlyExport(companyId: string, report: QuarterlyReport | DeferredQuarterlyReport, requestId: string, signal?: AbortSignal): Promise<{ job_id: string }> {
  if (!report.export.available || !report.export.preview_digest || !report.export.epochs) {
    throw new DashboardApiError(409, "REPORT_EXPORT_UNAVAILABLE", "季度报表尚未准备完成，当前不能导出。");
  }
  const result = await requestLocalJson("/api/local/report-export", { method: "POST", signal, body: JSON.stringify({
    company_id: companyId, year: report.period.year, quarter: report.period.quarter,
    preview_digest: report.export.preview_digest, epochs: report.export.epochs, request_id: requestId,
    ...(report.carry_forward.selected_fact_id ? { carry_forward_fact_id: report.carry_forward.selected_fact_id } : {}),
  }) });
  if (!result || typeof result !== "object" || !("job_id" in result) || typeof result.job_id !== "string") {
    throw new DashboardApiError(502, "REPORT_JOB_RESPONSE", "报表任务响应无法读取，请刷新后台任务核对。");
  }
  return { job_id: result.job_id };
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
