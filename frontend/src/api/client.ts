import { LocalApiError, requestLocalJson, verifyMoneyStrings } from "./localKernel";
import { validDashboardContract } from "./dashboardContracts";
import type {
  DashboardContextResponse,
  DashboardFundsContract,
  DashboardFundsResponse,
} from "./generated/dashboardResponses";
import {
  validateDashboardContextResponse,
  validateDashboardFundsResponse,
} from "./generated/dashboardValidators.js";

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

// Remaining endpoints migrate to generated requests in their assigned stages.
export async function requestJson<T>(
  path: string,
  options: { signal?: AbortSignal } = {},
): Promise<T> {
  const target = withCurrentCompany(path);
  const payload = await requestLocalJson(target, { signal: options.signal });
  if (!validDashboardContract(target, payload)) {
    throw new DashboardApiError(502, "DASHBOARD_SCHEMA_MISMATCH", "看板数据版本或分页信息不匹配，请刷新页面后重试。");
  }
  verifyMoneyStrings(payload);
  return payload as T;
}

async function requestGeneratedJson<T>(
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

function validPage(page: DashboardFundsContract.CollectionPage, returnedCount: number): boolean {
  return [page.total_count, page.filtered_count, page.returned_count].every(
    count => Number.isSafeInteger(count) && count >= 0,
  ) && page.total_count >= page.filtered_count
    && page.filtered_count >= page.returned_count
    && page.returned_count === returnedCount
    && (page.has_more ? typeof page.next_cursor === "string" && page.next_cursor.length > 0 : page.next_cursor === null);
}

function aliasPageMatchesCollection(
  alias: DashboardFundsContract.CollectionPage | undefined,
  collection: DashboardFundsContract.CollectionPage,
): boolean {
  return alias !== undefined && (
    alias.total_count === collection.filtered_count
    && alias.filtered_count === collection.filtered_count
    && alias.returned_count === collection.returned_count
    && alias.has_more === collection.has_more
    && alias.next_cursor === collection.next_cursor
  );
}

function sameItems(left: readonly unknown[], right: readonly unknown[]): boolean {
  return left.length === right.length && left.every((item, index) => JSON.stringify(item) === JSON.stringify(right[index]));
}

function fundsMatchesRequest(url: URL, response: DashboardFundsResponse): boolean {
  const period = url.searchParams.get("period");
  if (period !== null && response.selected_period?.key !== period) return false;
  const expectedVersion = url.searchParams.get("expected_version");
  if (expectedVersion !== null && response.snapshot_version !== expectedVersion) return false;
  if (response.data === null) return url.searchParams.get("section") === null;

  const data = response.data;
  const deferred = url.searchParams.get("preparation") === "deferred";
  if (deferred ? data.period_preparation !== null : data.period_preparation === null) return false;
  const requestedSection = url.searchParams.get("section") ?? "movements";
  if (!(requestedSection in data.collections)) return false;

  const { accounts, movements, statements, investment_products: products, investment_events: events } = data.collections;
  for (const collection of [accounts, movements, statements, products, events]) {
    if (collection && !validPage(collection.page, collection.items.length)) return false;
  }
  if (accounts && !sameItems(accounts.items, data.accounts)) return false;
  if (movements && (!sameItems(movements.items, data.movements) || !aliasPageMatchesCollection(data.movement_page, movements.page))) return false;
  if (statements && (!sameItems(statements.items, data.bank_statement.rows) || !aliasPageMatchesCollection(data.bank_statement.page, statements.page))) return false;
  if (products && !sameItems(products.items, data.investments.products)) return false;
  if (events && (!sameItems(events.items, data.investments.events) || !aliasPageMatchesCollection(data.investments.page, events.page))) return false;

  const movementType = url.searchParams.get("movement_account_type");
  const movementAccount = url.searchParams.get("movement_account_id");
  const statementAccount = url.searchParams.get("statement_account_id");
  return (!movementType || data.movements.every((item: DashboardFundsContract.FundMovement) => item.account_type === movementType))
    && (!movementAccount || data.movements.every((item: DashboardFundsContract.FundMovement) => item.account_id === movementAccount))
    && (!statementAccount || data.bank_statement.rows.every((item: DashboardFundsContract.BankStatementRow) => item.account_id === statementAccount));
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
