export class LocalApiError extends Error {
  constructor(public readonly status: number, public readonly code: string, message: string) {
    super(message);
    this.name = "LocalApiError";
  }
}

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
  opening_balances?: { account: string; debit: LocalFen; credit: LocalFen }[];
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

export interface LocalJob {
  id: string;
  kind: string;
  status: string;
  attempts: number;
  last_error: string | null;
  result: unknown;
  download_available?: boolean;
  download_file_name?: string | null;
}

export function localJobDownloadAvailable(job: LocalJob): boolean {
  return job.kind === "report_export" && job.status === "succeeded" && job.download_available === true;
}

export function localJobName(kind: string): string {
  return ({ portable_backup: "公司备份", payment_export: "银行代发文件", report_export: "季度报表文件" } as Record<string, string>)[kind] ?? "后台任务";
}

export function localJobStatus(status: string): string {
  return ({ pending: "等待处理", running: "正在生成", succeeded: "已完成", failed: "生成失败" } as Record<string, string>)[status] ?? "状态待核对";
}

export function localJobMessage(job: LocalJob): string {
  if (job.status === "pending") return "任务已排队，等待生成文件。";
  if (job.status === "running") return "正在生成并检查文件。";
  if (job.status === "succeeded") return localJobDownloadAvailable(job)
    ? "报表已生成并通过校验，可以下载。"
    : "文件已生成，可在会计任务中查看交付结果。";
  if (job.status === "failed") return job.attempts >= 3
    ? "文件生成未完成，自动重试次数已用尽。请在会计任务中处理原因后重试。"
    : "本次文件生成失败，尚未完成。请刷新查看重试结果。";
  return "暂时无法确认任务结果，请刷新核对。";
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
      opening_lines?: { account: string; debit: LocalFen; credit: LocalFen; cashflow: null }[];
      opening?: boolean;
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
  expense_recovery: "费用退回确认",
  reimbursement_acceptance: "已付负债报销承接",
  project_cost: "项目阶段成本",
  project_release: "项目成本转费用",
  pass_through: "代收代付",
  advance: "预收预付款",
  asset_advance: "资产预付款",
  funding: "股东投入或借款",
  payment: "实际收付款",
  cash_payment: "现金收付款",
  cash_funding: "现金投入或借款",
  cash_bank_transfer: "现金存取",
  platform_payment: "支付平台收付款",
  platform_funding: "支付平台投入或借款",
  bank_platform_transfer: "银行与支付平台转款",
  platform_movement: "支付平台原始资金记录",
  platform_expense_confirmation: "平台管理资金费用确认",
  managed_reserve_scope: "备用金核算范围确认",
  managed_reserve_bank_expense: "银行转入备用金费用确认",
  managed_reserve_obligation_settlement: "备用金结清原应付款",
  platform_boundary_disposition: "平台原交易范围处置",
  payroll_reserve_payment: "毛额工资支付及返池费用确认",
  settlement: "非现金核销",
  sale_return: "销售退回",
  funds_transfer: "资金调拨",
  bank_income: "其他收入",
  refundable_deposit: "可退保证金",
  reimbursed_deposit: "垫付押金确认",
  overpayment: "超付追收",
  employee_advance: "个人垫付及债务转移",
  pass_through_return: "代收款退回",
  asset: "资产购置",
  reimbursed_asset: "报销形成的资产",
  reimbursed_asset_batch: "整批资产验收",
  money_fund_subscription: "货币基金申购确认",
  money_fund_redemption: "货币基金赎回确认",
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
  payroll_bounded: "工资计提",
  payroll_tax_declaration_actual: "工资个税实际申报记录",
  payroll_withholding_actual: "实际工资扣税确认",
  payroll_disbursement_basis: "按实际申报个税编制代发",
  annual_bonus_policy: "全年一次性奖金规则",
  annual_bonus_opening_usage: "奖金既有计税记录",
  annual_bonus: "全年一次性奖金",
  labor_income_tax_policy: "劳务个税规则",
  labor: "个人劳务计提",
  labor_accrual: "未支付个人劳务计提",
  labor_project_cost: "资产项目劳务成本",
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
  continuation_report_profile: "接续企业报表口径",
  report_carry_forward: "接账前财务报表累计依据",
  opening_bank: "银行存款期初",
  opening_cash: "库存现金期初",
  opening_obligation: "往来明细期初",
  opening_asset: "期初资产卡片",
  opening_money_fund: "期初货币基金成本",
  opening_loan: "借款本金及利息期初",
  opening_tax: "税费明细期初",
  opening_payroll_payable: "薪酬未付明细期初",
  opening_payroll_state: "人员薪酬累计接续",
  opening_equity: "权益明细期初",
  opening_package: "期初接续总清单",
  company_workflow_scope_v2: "公司月度业务范围",
  filing_calendar_policy_v2: "申报期限日历",
  material_source_v2: "原始资料逐项核对来源",
  material_resolution_v2: "原始资料处理结果",
  material_group_resolution: "原始资料联合处理结果",
  material_period_allocation: "原始资料核算月份归属",
  payroll_plan_v2: "工资标准计划",
  payroll_plan_bounded: "工资标准计划",
  payroll_change_notice_v2: "工资变动通知",
  payroll_no_change_v2: "工资无变动确认",
  tax_import_identity_v2: "税务材料企业身份核对",
  tax_import_details_v2: "税务材料明细",
  tax_import_mapping_v2: "税务材料业务匹配",
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

export function verifyMoneyStrings(value: unknown, parentKey = ""): void {
  if (Array.isArray(value)) {
    value.forEach(item => verifyMoneyStrings(item, parentKey));
  } else if (record(value)) {
    for (const [key, field] of Object.entries(value)) {
      if (field !== null && (key.endsWith("_fen") || ["debit", "credit", "amount"].includes(key) || (key === "total" && parentKey !== "checks"))) {
        const amountPattern = key.startsWith("unrounded_") ? /^-?\d+(\.\d+)?$/ : /^-?\d+$/;
        if (typeof field !== "string" || !amountPattern.test(field)) {
          throw new LocalApiError(502, "LOCAL_MONEY_FORMAT", "金额传输格式不正确，无法可靠展示。请更新本地内核后重试。");
        }
      }
      verifyMoneyStrings(field, key);
    }
  }
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

async function localRequest<T>(path: string, parameters: Record<string, string>, signal?: AbortSignal): Promise<T> {
  const query = new URLSearchParams(parameters);
  const payload = await requestLocalJson(`/api/local/${path}?${query}`, { signal });
  verifyMoneyStrings(payload);
  return payload as T;
}

export type SecurityAction = "bootstrap_owner" | "login" | "change_password" | "recover" | "replace_recovery_code";
export interface LocalSecurityState {
  provisioned?: boolean;
  authenticated?: boolean;
  login_name?: string;
  request_id?: string;
  status?: "starting" | "waiting_for_user" | "running" | "succeeded" | "failed" | "cancelled" | "expired";
  login_completed?: boolean;
  browser_authenticated?: boolean;
  operation_committed?: boolean;
  error_code?: string | null;
}

export async function localSecurity(operation: "request" | "status" | "cancel" | "session_status", payload: { kind?: SecurityAction; login_name?: string; request_id?: string } = {}): Promise<LocalSecurityState> {
  const result = await requestLocalJson("/api/security-request", {
    method: "POST", body: JSON.stringify({ operation, payload }),
  });
  if (!record(result)) throw new LocalApiError(502, "LOCAL_SECURITY_RESPONSE", "安全窗口状态无法读取，请重新打开工作台。");
  return result as LocalSecurityState;
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

export function fetchLocalJobs(companyId: string, signal?: AbortSignal): Promise<LocalJob[]> {
  return localRequest("jobs", { company_id: companyId, limit: "20" }, signal);
}

export function fetchLocalJob(companyId: string, jobId: string, signal?: AbortSignal): Promise<LocalJob[]> {
  return localRequest("jobs", { company_id: companyId, job_id: jobId, limit: "1" }, signal);
}

export function localErrorMessage(error: unknown): string {
  if (error instanceof LocalApiError) return error.message;
  if (error instanceof DOMException && error.name === "AbortError") return "";
  return "本地账务数据暂时无法读取，请检查内核服务后重试。";
}
