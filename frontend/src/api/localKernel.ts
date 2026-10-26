import type { BrowserSecurityStatusContract, BrowserSecurityStatusResponse } from "./generated/browserSecurityStatus";
import { validateBrowserSecurityStatusResponse } from "./generated/browserSecurityStatus.js";

export class LocalApiError extends Error {
  constructor(public readonly status: number, public readonly code: string, message: string) {
    super(message);
    this.name = "LocalApiError";
  }
}

// Fallback labels for the current registered facts; backend display profiles take precedence.
export const localBusinessNames: Record<string, string> = {
  "operations.service_sale": "服务收入",
  "operations.expense": "费用",
  "operations.other_income": "其他收入",
  "operations.advance": "预收预付款",
  "operations.advance_fulfillment": "预收款履约确认",
  "operations.sale_return": "销售退回",
  "operations.advance_refund": "预收预付款退回",
  "operations.expense_recovery": "费用退回确认",
  "operations.pass_through": "代收代付",
  "operations.pass_through_return": "代收款退回",
  "operations.deposit": "可退保证金",
  "operations.reimbursement": "报销确认",
  "operations.overpayment": "超额收付款处理",
  "people.employment": "用工登记",
  "people.remuneration": "工资奖金及劳务报酬",
  "people.contribution_policy": "社保公积金规则",
  "people.contribution_actual": "实际社保公积金",
  "people.relationship": "用工关系",
  "people.remuneration_tax": "薪酬个人所得税",
  "assets.project_cost": "项目阶段成本",
  "assets.project_release": "项目成本转费用",
  "assets.acquisition": "资产购置",
  "assets.activation": "资产启用",
  "assets.disposal": "资产处置",
  "financing.owner_funding": "股东投入或借款",
  "financing.loan_contract": "借款合同",
  "financing.loan_drawdown": "借款到账",
  "financing.interest_period": "借款利息计提",
  "investment.money_fund_purchase": "货币基金申购确认",
  "investment.money_fund_redemption": "货币基金赎回确认",
  "investment.cooperation_contract": "合作投资合同",
  "investment.cooperation_funding": "合作投资出资",
  "investment.cooperation_collection": "合作项目代收款",
  "investment.cooperation_payment": "合作项目代付款",
  "investment.cooperation_operator_transfer": "合作经营者转款",
  "investment.cooperation_distribution": "合作投资收益分配",
  "investment.cooperation_exit": "合作投资退出",
  "money.movement": "实际收付款",
  "money.transfer": "资金划转",
  "money.bank_statement": "银行流水",
  "money.reconciliation": "银行对账",
  "money.reserve_internal": "备用金内部资金记录",
  "money.platform_activity": "支付平台原始资金记录",
  "taxes.vat_policy": "增值税规则",
  "taxes.surtax_policy": "附加税费规则",
  "taxes.used_asset_vat_policy": "资产出售税务规则",
  "taxes.income_tax_policy": "企业所得税政策",
  "taxes.prepayment_basis": "企业所得税季度预缴依据",
  "taxes.assessment": "税费确认",
  "taxes.tax_credit": "税额抵减与退税确认",
  "taxes.actual_declaration": "实际税费申报",
  "taxes.tax_point": "增值税税期确认",
  "opening.package": "期初接续总清单",
  "reports.continuation": "财务报表接续资料",
};

export function localBusinessName(kind?: string): string {
  return kind ? (localBusinessNames[kind] ?? "其他业务") : "会计业务";
}

export async function consumeLocalTicket(development = false): Promise<void> {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  let supplied = fragment.get("ticket");
  // Remove sensitive launch fragments before any asynchronous request. Owner
  // credentials never enter JavaScript storage or subsequent query parameters.
  if (fragment.has("ticket") || fragment.has("token")) {
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  if (!supplied && development) {
    const result = await requestLocalJson("/api/browser-ticket", {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (!record(result) || typeof result.url !== "string") {
      throw new LocalApiError(502, "LOCAL_TICKET_RESPONSE", "本地开发会话无法建立，请重新启动本地服务。");
    }
    supplied = new URLSearchParams(new URL(result.url).hash.slice(1)).get("ticket");
  }
  if (supplied) {
    await requestLocalJson("/api/browser-session", { method: "POST", body: JSON.stringify({ ticket: supplied }) });
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function requestLocalJson(path: string, options: RequestInit = {}): Promise<unknown> {
  const response = await fetch(path, {
    ...options,
    headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) },
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",
  });
  let payload: unknown;
  try { payload = await response.json(); }
  catch { throw new LocalApiError(response.status, "LOCAL_INVALID_RESPONSE", "本地服务响应无法读取，请检查服务运行状态。"); }
  if (!response.ok || (record(payload) && payload.status === "rejected")) {
    if (response.status === 401 && typeof window !== "undefined" && window.dispatchEvent) {
      window.dispatchEvent(new Event("finance-session-expired"));
    }
    const code = record(payload) && typeof payload.code === "string" ? payload.code : "LOCAL_REQUEST_FAILED";
    const message = ["launcher_required", "same_origin_required", "session_ticket_expired"].includes(code)
      ? "请从本机记账启动器重新打开工作台，再使用本机安全窗口登录。"
      : response.status === 401 || response.status === 403 || /SESSION|UNAUTH|PROVISIONED/i.test(code)
      ? "请通过本机安全窗口完成负责人登录；本页不接收密码。"
      : record(payload) && typeof payload.message === "string"
        ? payload.message : "本地账务数据暂时无法读取，请稍后重试。";
    throw new LocalApiError(response.status, code, message);
  }
  return payload;
}

export type SecurityAction = Exclude<BrowserSecurityStatusContract.BrowserSecurityRequestState["kind"], "approve_period_close">;
export type LocalSecurityState = BrowserSecurityStatusResponse;
export type LocalSecurityRequestState = BrowserSecurityStatusContract.BrowserSecurityRequestState;
export type LocalSecuritySessionStatus = BrowserSecurityStatusContract.BrowserSecuritySessionStatus;

export function isSecurityRequestState(value: LocalSecurityState): value is LocalSecurityRequestState { return "status" in value; }
export function isSecuritySessionStatus(value: LocalSecurityState): value is LocalSecuritySessionStatus { return "authenticated" in value; }

export async function localSecurity(operation: "request" | "status" | "cancel" | "session_status", payload: { kind?: SecurityAction; login_name?: string; request_id?: string } = {}): Promise<LocalSecurityState> {
  const result = await requestLocalJson("/api/security-request", {
    method: "POST", body: JSON.stringify({ operation, payload }),
  });
  if (!validateBrowserSecurityStatusResponse(result)
    || (payload.request_id && (!isSecurityRequestState(result) || result.request_id !== payload.request_id))
    || (operation === "session_status" && !isSecuritySessionStatus(result))
    || (operation !== "session_status" && !isSecurityRequestState(result))) {
    throw new LocalApiError(502, "LOCAL_SECURITY_RESPONSE", "安全窗口状态无法读取，请重新打开工作台。");
  }
  return result;
}

export function localErrorMessage(error: unknown): string {
  if (error instanceof LocalApiError) return error.message;
  if (error instanceof DOMException && error.name === "AbortError") return "";
  return "本地账务数据暂时无法读取，请检查内核服务后重试。";
}
