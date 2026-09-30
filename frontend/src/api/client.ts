import { LocalApiError, requestLocalJson } from "./localKernel";
import type { DashboardContextResponse } from "./generated/dashboardContext";
import { validateDashboardContextResponse } from "./generated/dashboardContext.js";

export class DashboardApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "DashboardApiError";
    this.status = status;
    this.code = code;
  }
}

export async function requestGeneratedJson<T>(
  path: string,
  endpoint: string,
  validator: (value: unknown) => value is T,
  matchesRequest: (url: URL, value: T) => boolean,
  options: { signal?: AbortSignal } = {},
): Promise<T> {
  const target = withCurrentCompany(path);
  const payload = await requestLocalJson(target, { signal: options.signal });
  const url = new URL(target, window.location.origin);
  if (url.pathname !== endpoint || !validator(payload) || !matchesRequest(url, payload)) {
    throw new DashboardApiError(502, "DASHBOARD_SCHEMA_MISMATCH", "看板数据格式不匹配，请更新本地内核后重试。");
  }
  return payload;
}

function contextMatchesRequest(url: URL, response: DashboardContextResponse): boolean {
  const companyId = url.searchParams.get("company_id");
  return companyId === null || response.current_company?.company_id === companyId;
}

export function requestDashboardContext(options: { signal?: AbortSignal } = {}): Promise<DashboardContextResponse> {
  return requestGeneratedJson(
    "/api/dashboard/context", "/api/dashboard/context", validateDashboardContextResponse, contextMatchesRequest, options,
  );
}

export function withCurrentCompany(path: string): string {
  const target = new URL(path, window.location.origin);
  const current = new URLSearchParams(window.location.search).get("company_id");
  if (current && !target.searchParams.has("company_id")) {
    target.searchParams.set("company_id", current);
  }
  return `${target.pathname}${target.search}${target.hash}`;
}

export function dashboardErrorMessage(error: unknown): string {
  if (error instanceof DashboardApiError || error instanceof LocalApiError) return error.message;
  if (error instanceof DOMException && error.name === "AbortError") return "";
  return "财务工作台数据加载失败，请稍后重试。";
}

export function isDashboardSnapshotChanged(error: unknown): boolean {
  return (error instanceof DashboardApiError || error instanceof LocalApiError)
    && error.code === "dashboard_snapshot_changed";
}
