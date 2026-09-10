import { DashboardApiError } from "./client";

export type LocalFen = string;

export interface LocalCompany {
  id: string;
  name: string;
  taxpayer_id: string;
  database_id?: string;
}

export interface LocalOverview {
  period: string;
  epochs: { accounting: number; material: number; management: number };
  accounts: { account: string; debit: LocalFen; credit: LocalFen }[];
  cashflow: { category: string; amount: LocalFen }[];
  pending: { subject_id: string; causes: number; kind?: string }[];
  pending_count?: number;
  closed?: boolean;
}

export interface LocalVoucher {
  number: number;
  id: string;
  calculation_id: string;
  period: number;
  total: LocalFen;
  kind?: string;
  reverses_id: string | null;
}

export interface LocalTrace {
  calculation: {
    id: string;
    subject_id: string;
    kind: string;
    period: number;
    digest: string;
    program_version: string;
    outcome: {
      lines: { account: string; debit: LocalFen; credit: LocalFen; cashflow: string | null }[];
      values: Record<string, unknown>;
      explanation: Record<string, unknown>[];
      balances: { key: string; amount: LocalFen; category: string }[];
    };
  };
  facts: {
    id: string;
    subject_id: string;
    revision: number;
    kind: string;
    data: Record<string, unknown>;
    evidence: string[];
  }[];
  upstream: string[];
}

// These labels reuse the existing business presentation vocabulary. The new
// kernel's fact kinds are separately enumerated; unknown types stay Chinese.
export const localBusinessNames: Record<string, string> = {
  service_sale: "服务收入",
  expense: "费用",
  project_cost: "项目阶段成本",
  project_release: "项目成本转费用",
  pass_through: "代收代付",
  advance: "预收预付款",
  funding: "股东投入或借款",
  payment: "实际收付款",
  cash_payment: "现金收付款",
  cash_funding: "现金投入或借款",
  cash_bank_transfer: "现金存取",
  settlement: "非现金核销",
  sale_return: "销售退回",
  funds_transfer: "资金调拨",
  bank_income: "其他收入",
  refundable_deposit: "可退保证金",
  overpayment: "超付追收",
  employee_advance: "个人垫付及债务转移",
  pass_through_return: "代收款退回",
  asset: "资产购置",
  asset_activation: "资产启用",
  asset_consumption: "折旧与摊销",
  asset_disposal: "资产处置",
  loan_agreement: "借款合同",
  loan_drawdown: "借款到账",
  loan_interest: "借款利息计提",
  vat_policy: "增值税规则",
  surtax_policy: "附加税费规则",
  used_asset_vat_policy: "资产出售税务规则",
  tax_assessment: "增值税及附加税费确认",
  income_tax_assessment: "企业所得税确认",
  tax_credit_confirmation: "税额抵减与退税确认",
  payroll_contribution_policy: "社保公积金规则",
  payroll_income_tax_policy: "工资个税规则",
  payroll_profile: "员工核算资料",
  payroll_contribution_actual: "实际社保公积金",
  payroll_opening_state: "工资期初累计资料",
  payroll_first_wage_treatment: "首次工资计税依据",
  payroll: "工资计提",
  annual_bonus_policy: "全年一次性奖金规则",
  annual_bonus_opening_usage: "奖金既有计税记录",
  annual_bonus: "全年一次性奖金",
  labor_income_tax_policy: "劳务个税规则",
  labor: "个人劳务计提",
  bank_statement: "银行流水",
  bank_opening: "银行账面起点",
  bank_reconciliation: "银行对账",
  service_tax_point: "服务收入增值税确认",
  advance_fulfillment: "预收款履约确认",
  advance_refund: "预收预付款退回",
  report_profile: "财务报表编制资料",
  report_classification: "财务报表分类",
  report_income_tax_confirmation: "报表所得税确认",
  external_obligation: "外部申报及办理事项",
  external_completion: "外部事项完成确认",
};

export function localBusinessName(kind?: string): string {
  return kind ? (localBusinessNames[kind] ?? "其他业务") : "会计业务";
}

let localToken = "";

export function consumeLocalToken(): void {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const supplied = fragment.get("token");
  if (supplied) {
    localToken = supplied;
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function verifyMoneyStrings(value: unknown): void {
  if (Array.isArray(value)) {
    value.forEach(verifyMoneyStrings);
  } else if (record(value)) {
    for (const [key, field] of Object.entries(value)) {
      if (field !== null && (key.endsWith("_fen") || ["debit", "credit", "total", "amount"].includes(key))) {
        const amountPattern = key.startsWith("unrounded_") ? /^-?\d+(\.\d+)?$/ : /^-?\d+$/;
        if (typeof field !== "string" || !amountPattern.test(field)) {
          throw new DashboardApiError(502, "LOCAL_MONEY_FORMAT", "金额传输格式不正确，无法可靠展示。请更新本地内核后重试。");
        }
      }
      verifyMoneyStrings(field);
    }
  }
}

async function localRequest<T>(path: string, parameters: Record<string, string>, signal?: AbortSignal): Promise<T> {
  if (!localToken) {
    throw new DashboardApiError(401, "LOCAL_SESSION_REQUIRED", "请从本地内核重新打开工作台，使用新生成的访问链接。");
  }
  const query = new URLSearchParams(parameters);
  const response = await fetch(`/api/local/${path}?${query}`, {
    headers: { Accept: "application/json", Authorization: `Bearer ${localToken}` },
    signal,
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  const payload: unknown = await response.json();
  if (!response.ok) {
    const code = record(payload) && typeof payload.code === "string" ? payload.code : "LOCAL_REQUEST_FAILED";
    const message = response.status === 401 || response.status === 403
      ? "访问会话已失效，请从本地内核重新打开工作台。"
      : record(payload) && typeof payload.message === "string"
        ? payload.message : "本地账务数据暂时无法读取，请稍后重试。";
    throw new DashboardApiError(response.status, code, message);
  }
  verifyMoneyStrings(payload);
  return payload as T;
}

export function fetchLocalCompanies(signal?: AbortSignal): Promise<LocalCompany[]> {
  return localRequest("companies", {}, signal);
}

export function fetchLocalOverview(companyId: string, period: string, signal?: AbortSignal): Promise<LocalOverview> {
  return localRequest("overview", { company_id: companyId, period }, signal);
}

export function fetchLocalLedger(companyId: string, period: string, afterNumber = 0, signal?: AbortSignal): Promise<LocalVoucher[]> {
  return localRequest("ledger", { company_id: companyId, period, after_number: String(afterNumber), limit: "50" }, signal);
}

export function fetchLocalTrace(companyId: string, calculationId: string, signal?: AbortSignal): Promise<LocalTrace> {
  return localRequest("trace", { company_id: companyId, calculation_id: calculationId }, signal);
}

export function localErrorMessage(error: unknown): string {
  if (error instanceof DashboardApiError) return error.message;
  if (error instanceof DOMException && error.name === "AbortError") return "";
  return "本地账务数据暂时无法读取，请检查内核服务后重试。";
}
