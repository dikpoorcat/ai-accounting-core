import { requestGeneratedJson } from "./client";
import { validDashboardCollections } from "./dashboardContracts";
import type { DashboardFundsContract, DashboardFundsResponse } from "./generated/dashboardFunds";
import { validateDashboardFundsResponse } from "./generated/dashboardFunds.js";

export type FundsDashboardResponse = DashboardFundsResponse;
export type FundsData = DashboardFundsContract.FundsData;
export type FundAccount = DashboardFundsContract.FundAccount;
export type BankStatementState = DashboardFundsContract.BankStatementRow["state"];

export interface FundsQuery {
  section?: "accounts" | "movements" | "statements" | "investment_products" | "investment_events";
  cursor?: string;
  expected_version?: string;
  movement_account_type?: FundAccount["type"];
  movement_account_id?: string;
  statement_account_id?: string;
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

export function requestDashboardFunds(path: string, options: { signal?: AbortSignal } = {}): Promise<DashboardFundsResponse> {
  return requestGeneratedJson(path, "/api/dashboard/funds", validateDashboardFundsResponse, fundsMatchesRequest, options);
}

export function fetchFundsDashboard(periodKey?: string, signal?: AbortSignal, options: FundsQuery = {}) {
  const query = new URLSearchParams({ limit: "100", ...(periodKey ? { period: periodKey } : {}), ...options });
  query.set("preparation", "deferred");
  return requestDashboardFunds(`/api/dashboard/funds?${query}`, {
    signal,
  });
}

// Keep selector labels across source navigation without adding cached rows to a page.
let accountLabelScope = "";
const accountLabels = new Map<string, string>();

export function fundAccountDisplayName(name: string, code: string): string {
  const normalizedName = name.trim();
  const normalizedCode = code.trim();
  if (!normalizedName || !normalizedCode) return normalizedName;
  const trailingDigits = normalizedCode.match(/\d{3,}$/)?.[0];
  const suffixes = [...new Set([normalizedCode, trailingDigits ? `尾号${trailingDigits}` : ""].filter(Boolean))]
    .flatMap(suffix => [`（${suffix}）`, `(${suffix})`, suffix]);
  for (const suffix of suffixes) {
    if (!normalizedName.endsWith(suffix)) continue;
    const base = normalizedName.slice(0, -suffix.length).replace(/[（(·\-—\s]+$/u, "").trim();
    if (base) return base;
  }
  return normalizedName;
}

export function fundAccountDisplayLabel(name: string, code: string): string {
  const displayName = fundAccountDisplayName(name, code);
  const normalizedCode = code.trim();
  if (!normalizedCode) return displayName;
  const trailingDigits = normalizedCode.match(/\d{3,}$/)?.[0];
  if (displayName.includes(normalizedCode) || (trailingDigits && displayName.includes(trailingDigits))) return displayName;
  return `${displayName}（${normalizedCode}）`;
}

export function fundAccountLabel(company: string, period: string, type: string, id: string): string | undefined {
  const scope = JSON.stringify([company, period]);
  if (scope !== accountLabelScope) { accountLabels.clear(); accountLabelScope = scope; }
  return accountLabels.get(JSON.stringify([company, type, id]));
}
export function rememberFundAccounts(company: string, period: string, accounts: FundAccount[]) {
  fundAccountLabel(company, period, "", "");
  for (const account of accounts) accountLabels.set(JSON.stringify([company, account.type, account.account_id]), fundAccountDisplayLabel(account.name, account.code));
}
