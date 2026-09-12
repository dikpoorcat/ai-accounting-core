export interface DashboardPage {
  total_count: number;
  filtered_count: number;
  returned_count: number;
  has_more: boolean;
  next_cursor: string | null;
  collection_version?: string;
}

export interface DashboardCollection<T = unknown> {
  items: T[];
  page: DashboardPage;
}

export type DashboardCollections = Record<string, DashboardCollection>;

export interface BusinessIssue {
  code?: string;
  message?: string;
  subject_id?: string;
  field?: string;
  [key: string]: unknown;
}
export interface UnestablishedSelection {
  candidates: Array<{ calculation_id: string; [key: string]: unknown }>;
  reason?: string;
  [key: string]: unknown;
}
export interface PeriodPreparation {
  company_id: string;
  database_id: string;
  period: string;
  as_of: string;
  as_of_semantics: "current_knowledge";
  projection: "dashboard_period_preparation";
  closure: { state: "exact_close" | "sealed_by_later_close" | "open"; sealing_boundary?: string; digest?: string; sealing_digest?: string };
  frozen_readiness: null | { status: string; source?: string; reason?: string; readiness?: { status: "recorded" | "not_recorded" }; inventories?: { status: "recorded" | "not_recorded" }; material_coverage?: { status: "recorded" | "not_recorded" }; previous_close_digest?: { status: "recorded" | "not_recorded" } };
  readiness: null | { period?: string; order_failure?: unknown; issues?: BusinessIssue[] };
  current_followups: {
    knowledge: "current_knowledge";
    affects_frozen_readiness: false;
    materials: { status: string; issues: BusinessIssue[]; inventory_count: number; coverage_digest: string };
    accounting: { status: string; issues: BusinessIssue[]; pending_subject_id: unknown; unpublished_count: number };
    close_requirements: { status: string; issues: BusinessIssue[] };
    settlements: { status: string; cutoff_period?: string; current_cutoff_period?: string; complete?: boolean; unestablished_state_selection_count?: number; issues?: BusinessIssue[]; obligation_count: number; movement_count: number; source_amount_fen: string | null; paid_fen: string | null; other_settled_fen: string | null; remaining_fen: string | null };
    external: { status: string; obligation_count: number; completion_status_counts: Record<string, number>; fact_issues?: BusinessIssue[] };
    file_jobs: { total_count: number; status_counts: Record<string, number>; issue_count: number };
  };
  read_semantics: Record<string, string>;
}

export interface DashboardReadContext {
  read_version: string;
  company_id: string;
  database_id: string;
  as_of: string;
}

export function businessStateLabel(status: string | null | undefined) {
  const labels: Record<string, string> = {
    ready: "准备就绪", completed: "已完成", not_applicable: "不适用", unestablished: "尚不能确认", not_established: "尚不能确认",
    established: "关系已确认", partially_established: "部分关系待核对", followup_required: "有事项待跟进", unknown: "未知",
    current: "依据有效", review_required: "需要复核", unpublished: "尚未发布", published: "已发布", deleted: "已撤去", not_required: "无需复核",
    needs_information: "需要补充资料", needs_review: "需要复核", blocked: "尚有条件待处理", pending: "待处理", incomplete: "尚未完整", complete: "已完整",
    succeeded: "文件生成成功", failed: "文件处理失败", running: "处理中", recorded: "已记录", not_recorded: "未记录", unavailable: "无法提供",
    settled: "已结清", over_settled: "超额结算", partial: "部分结算", open: "尚未结算", resolved: "关系已确认", unresolved: "关系尚待确认",
  };
  return status ? labels[status] ?? "状态待核对" : "未知";
}

export interface DashboardPageQuery {
  section?: string;
  cursor?: string;
  limit?: number;
  expected_version?: string | null;
}

const versions: Record<string, number> = {
  context: 2, brief: 2, funds: 2, employees: 2, assets: 2,
  "quarterly-report": 1, "business-status": 1, "period-preparation": 1,
};
const primaryCollections: Record<string, string> = {
  brief: "vouchers", funds: "movements", employees: "employees", assets: "assets",
};
const allowedCollections: Record<string, string[]> = {
  brief: ["vouchers", "businesses", "open_items", "settlement_events", "external_followups", "file_jobs"],
  funds: ["accounts", "movements", "statements", "investment_products", "investment_events"],
  employees: ["employees", "payroll_sources", "labor_sources", "settlement_events"],
  assets: ["assets", "projects", "source_history", "settlement_events"],
  "business-status": ["events", "settlement_events", "source_history", "file_jobs"],
};

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validPreparation(value: unknown): boolean {
  if (!record(value) || value.projection !== "dashboard_period_preparation" || typeof value.period !== "string" || !record(value.closure)) return false;
  if (!["exact_close", "sealed_by_later_close", "open"].includes(String(value.closure.state))) return false;
  const current = value.current_followups;
  return record(current) && current.affects_frozen_readiness === false
    && ["materials", "accounting", "close_requirements", "settlements", "external", "file_jobs"].every(key => record(current[key]))
    && ["materials", "accounting", "close_requirements"].every(key => Array.isArray((current[key] as Record<string, unknown>).issues))
    && (value.frozen_readiness === null || record(value.frozen_readiness));
}

function validReadContext(value: unknown): value is DashboardReadContext & Record<string, unknown> {
  return record(value) && ["read_version", "company_id", "database_id", "as_of"].every(key => typeof value[key] === "string" && value[key].length > 0);
}

function validCheckItems(value: unknown, keys: string[], pending = false): boolean {
  return Array.isArray(value) && value.length === keys.length && keys.every(key => value.filter(item => record(item) && item.key === key).length === 1)
    && value.every(item => record(item) && typeof item.label === "string" && typeof item.text === "string"
      && (pending && item.key !== "balance" ? item.state === "pending" : ["pass", "pending", "error", "neutral"].includes(String(item.state))));
}

function validBriefChecks(value: unknown): boolean {
  if (!record(value) || !record(value.material_completeness)) return false;
  const material = value.material_completeness;
  return typeof material.closed === "boolean" && typeof material.satisfied === "boolean" && Array.isArray(material.issues)
    && material.issues.every(issue => record(issue) && typeof issue.code === "string" && typeof issue.message === "string")
    && Array.isArray(value.issues) && value.issues.every(issue => record(issue) && typeof issue.message === "string")
    && Number.isSafeInteger(value.attention_count) && Number(value.attention_count) >= 0
    && validCheckItems(value.items, ["materials", "accounting", "close_requirements"]);
}

function validCollection(value: unknown): boolean {
  if (!record(value) || !Array.isArray(value.items) || !record(value.page)) return false;
  const page = value.page;
  return [page.total_count, page.filtered_count, page.returned_count].every(
    count => typeof count === "number" && Number.isSafeInteger(count) && count >= 0,
  ) && page.returned_count === value.items.length
    && typeof page.has_more === "boolean"
    && (page.has_more ? typeof page.next_cursor === "string" && page.next_cursor.length > 0 : page.next_cursor === null)
    && (page.collection_version === undefined || typeof page.collection_version === "string");
}

function validSettlementPage(value: unknown): boolean {
  return record(value) && typeof value.subject_id === "string" && value.settlement_view === "historical"
    && value.movements_scope === "business_related_settlement_events"
    && validCollection({ items: value.movements, page: value.movements_page });
}

export function validDashboardContract(path: string, payload: unknown): boolean {
  const url = new URL(path, "http://dashboard.invalid");
  const endpoint = url.pathname.split("/").at(-1) ?? "";
  const version = versions[endpoint];
  if (version === undefined) return true;
  if (!record(payload) || payload.schema_version !== version) return false;
  if (endpoint === "period-preparation") {
    if (payload.projection !== "dashboard_period_preparation_result" || !validReadContext(payload.read_context)
      || payload.period !== url.searchParams.get("period") || !record(payload.data) || !validPreparation(payload.data.period_preparation)
      || !validBriefChecks(payload.data.brief_checks)) return false;
    const context = payload.read_context, preparation = payload.data.period_preparation as Record<string, unknown>;
    return context.company_id === url.searchParams.get("company_id") && context.read_version === url.searchParams.get("expected_read_version")
      && context.as_of === url.searchParams.get("as_of") && preparation.period === payload.period
      && ["company_id", "database_id", "as_of"].every(key => preparation[key] === context[key]);
  }
  const deferred = url.searchParams.get("preparation") === "deferred";
  if (endpoint === "quarterly-report") return deferred
    ? payload.projection === "dashboard_quarterly_report_deferred" && payload.period_preparations === null
      && validReadContext(payload.read_context) && payload.read_context.company_id === url.searchParams.get("company_id")
    : payload.projection === undefined && Array.isArray(payload.period_preparations) && payload.period_preparations.every(validPreparation);
  if (endpoint === "brief" && deferred) {
    if (payload.projection !== "dashboard_brief_deferred") return false;
    if (payload.data === null) return payload.read_context === null;
    if (!validReadContext(payload.read_context) || payload.read_context.company_id !== url.searchParams.get("company_id")
      || !record(payload.data) || payload.data.period_preparation !== null || payload.data.material_completeness !== null
      || !record(payload.data.validation) || !["pending", "attention", "error"].includes(String(payload.data.validation.state))
      || !Number.isSafeInteger(payload.data.validation.attention_count) || Number(payload.data.validation.attention_count) < 0
      || !(typeof payload.data.validation.integrity_valid === "boolean" || payload.data.validation.integrity_valid === null)
      || !validCheckItems(payload.data.validation.items, ["balance", "materials", "accounting", "close_requirements"], true)) return false;
  } else if (endpoint === "brief" && payload.projection !== undefined) return false;
  if (!(endpoint in primaryCollections) && endpoint !== "business-status") return true;
  if (payload.data === null) return true;
  if (!record(payload.data) || !record(payload.data.collections)) return false;
  if (endpoint in primaryCollections && !(endpoint === "brief" && deferred) && !validPreparation(payload.data.period_preparation)) return false;
  const collections = payload.data.collections;
  if (!Object.keys(collections).every(key => allowedCollections[endpoint]?.includes(key))) return false;
  const required = url.searchParams.get("section") ?? primaryCollections[endpoint];
  if (required && !record(collections[required])) return false;
  if (endpoint === "employees" && record(payload.data.employees) && Array.isArray(payload.data.employees.items)
    && !payload.data.employees.items.every(item => record(item) && (item.selection_status === "unestablished"
      ? typeof item.employee_id === "string" && Array.isArray(item.candidate_selections)
      : validCollection({ items: item.payroll_sources, page: item.payroll_source_page }) && (item.payroll_sources as unknown[]).every(validSettlementPage)))) return false;
  if (endpoint === "employees" && record(payload.data.workforce_cost) && record(payload.data.workforce_cost.personal_labor)
    && Array.isArray(payload.data.workforce_cost.personal_labor.items) && !payload.data.workforce_cost.personal_labor.items.every(validSettlementPage)) return false;
  if (endpoint === "assets") {
    const assets = record(collections.assets) && Array.isArray(collections.assets.items) ? collections.assets.items : [];
    if (!assets.every(item => record(item) && (item.selection_status === "unestablished" || (Array.isArray(item.settlements)
      && item.settlements.every(validSettlementPage) && [item.disposal, item.retirement].every(exit => !exit || record(exit) && validSettlementPage(exit.settlement)))))) return false;
    if (Array.isArray(payload.data.projects) && !payload.data.projects.every(item => record(item) && validSettlementPage(item.settlement))) return false;
  }
  return Object.entries(collections).every(([key, value]) => validCollection(value)
    && (key !== "file_jobs" || (record(value) && record(value.page) && typeof value.page.collection_version === "string" && value.page.collection_version.length > 0)));
}

export function pageQuery(query: URLSearchParams, options: DashboardPageQuery) {
  if (options.section) query.set("section", options.section);
  if (options.cursor) query.set("cursor", options.cursor);
  query.set("limit", String(options.limit ?? 100));
  if (options.expected_version) query.set("expected_version", options.expected_version);
}
