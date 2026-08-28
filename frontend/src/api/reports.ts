import { DashboardApiError, requestJson } from "./client";

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
  passed: boolean;
}

export interface QuarterlyReport {
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
  export: {
    available: boolean;
    file_name: string;
    calculation_hash: string | null;
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
    classification_count: number;
    income_tax_confirmation_count: number;
    requirement_codes: string[];
    errors: string[];
  };
}

export async function fetchQuarterlyReport(
  year: number,
  quarter: number,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({
    year: String(year),
    quarter: String(quarter),
  });
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

export async function fetchQuarterlyWorkbook(
  report: QuarterlyReport,
  signal?: AbortSignal,
) {
  const calculationHash = report.export.calculation_hash;
  if (!report.export.available || !calculationHash) {
    throw new DashboardApiError(
      409,
      "REPORT_EXPORT_UNAVAILABLE",
      "季度报表尚未准备完成，当前不能导出。",
    );
  }
  const query = new URLSearchParams({
    year: String(report.period.year),
    quarter: String(report.period.quarter),
    calculation_hash: calculationHash,
  });
  const response = await fetch(`/financial-reports/quarterly.xlsx?${query}`, {
    headers: {
      Accept: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
    signal,
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as
      | { errors?: unknown; message?: unknown }
      | null;
    const errors = Array.isArray(payload?.errors) ? payload.errors : [];
    const code = typeof errors[0] === "string" ? errors[0] : "REPORT_EXPORT_FAILED";
    const message =
      typeof payload?.message === "string"
        ? payload.message
        : "季度报表导出失败，请稍后重试。";
    throw new DashboardApiError(response.status, code, message);
  }
  return response.blob();
}
