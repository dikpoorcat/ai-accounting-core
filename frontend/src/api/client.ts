import { LocalApiError, requestLocalJson } from "./localKernel";
import type {
  DashboardContextResponse,
  DashboardFundsContract,
  DashboardFundsResponse,
} from "./generated/dashboardResponses";
import {
  validateDashboardContextResponse,
  validateDashboardFundsResponse,
} from "./generated/dashboardValidators.js";
import { validDashboardCollections } from "./dashboardContracts";

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

function fundsMatchesRequest(url: URL, response: DashboardFundsResponse): boolean {
  const companyId = url.searchParams.get("company_id");
  const period = url.searchParams.get("period");
  if (companyId !== null && response.read_context.company_id !== companyId) return false;
  if (period !== null && response.selected_period?.key !== period) return false;
  const expectedVersion = url.searchParams.get("expected_version");
  if (expectedVersion !== null && response.snapshot_version !== expectedVersion) return false;
  if (response.data === null) return url.searchParams.get("section") === null;

  const data = response.data;
  if (!validDashboardCollections(data)) return false;
  const deferred = url.searchParams.get("preparation") === "deferred";
  if (deferred ? data.period_preparation !== null : data.period_preparation === null) return false;
  const requestedSection = url.searchParams.get("section") ?? "movements";
  if (!(requestedSection in data.collections)) return false;

  const { movements, statements } = data.collections;
  const movementType = url.searchParams.get("movement_account_type");
  const movementAccount = url.searchParams.get("movement_account_id");
  const statementAccount = url.searchParams.get("statement_account_id");
  return (!movementType || !movements || movements.items.every((item: DashboardFundsContract.FundMovement) => item.account_type === movementType))
    && (!movementAccount || !movements || movements.items.every((item: DashboardFundsContract.FundMovement) => item.account_id === movementAccount))
    && (!statementAccount || !statements || statements.items.every((item: DashboardFundsContract.BankStatementRow) => item.account_id === statementAccount));
}

export function requestDashboardContext(options: { signal?: AbortSignal } = {}): Promise<DashboardContextResponse> {
  return requestGeneratedJson(
    "/api/dashboard/context", "/api/dashboard/context", validateDashboardContextResponse, contextMatchesRequest, options,
  );
}

export function requestDashboardFunds(path: string, options: { signal?: AbortSignal } = {}): Promise<DashboardFundsResponse> {
  return requestGeneratedJson(path, "/api/dashboard/funds", validateDashboardFundsResponse, fundsMatchesRequest, options);
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
