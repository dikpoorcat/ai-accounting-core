import { requestDashboardFunds } from "./client";
import type { DashboardFundsContract, DashboardFundsResponse } from "./generated/dashboardResponses";

export type FundsDashboardResponse = DashboardFundsResponse;
export type FundsData = DashboardFundsContract.FundsData;
export type FundAccount = DashboardFundsContract.FundAccount;
export type BankStatementState = DashboardFundsContract.BankStatementRow["state"];

export interface FundsQuery {
  section?: "accounts" | "movements" | "statements" | "investment_products" | "investment_events";
  cursor?: string;
  after_movement?: string;
  after_statement?: string;
  after_investment?: string;
  expected_version?: string;
  movement_account_type?: FundAccount["type"];
  movement_account_id?: string;
  statement_account_id?: string;
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
