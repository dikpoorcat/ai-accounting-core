import { LocalApiError, requestLocalJson, verifyMoneyStrings } from "./localKernel";
import { validDashboardContract } from "./dashboardContracts";

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

export async function requestJson<T>(
  path: string,
  options: { signal?: AbortSignal } = {},
): Promise<T> {
  const payload = await requestLocalJson(withCurrentCompany(path), { signal: options.signal });
  if (!validDashboardContract(path, payload)) {
    throw new DashboardApiError(502, "DASHBOARD_SCHEMA_MISMATCH", "看板数据版本或分页信息不匹配，请刷新页面后重试。");
  }
  verifyMoneyStrings(payload);
  return payload as T;
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
