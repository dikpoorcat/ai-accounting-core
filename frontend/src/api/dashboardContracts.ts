import type { DashboardFundsContract, DashboardPeriodPreparationContract } from "./generated/dashboardResponses";

export type BusinessIssue = DashboardPeriodPreparationContract.ReadinessIssue;
export type PeriodPreparation = DashboardPeriodPreparationContract.PeriodPreparation;
export type DashboardReadContext = DashboardPeriodPreparationContract.DashboardReadContext;

export type DashboardPage = DashboardFundsContract.CollectionPage;
export type DashboardCollection<T = unknown> = Omit<DashboardFundsContract.Collection, "items"> & { items: T[] };

export interface DashboardPageQuery {
  section?: string;
  cursor?: string;
  limit?: number;
  expected_version?: string | null;
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

export function pageQuery(query: URLSearchParams, options: DashboardPageQuery) {
  if (options.section) query.set("section", options.section);
  if (options.cursor) query.set("cursor", options.cursor);
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.expected_version) query.set("expected_version", options.expected_version);
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function validDashboardCollections(data: unknown): boolean {
  if (data === null) return true;
  if (!object(data) || !object(data.collections)) return false;
  return Object.values(data.collections).every(collection => {
    if (!object(collection) || !Array.isArray(collection.items) || !object(collection.page)) return false;
    const page = collection.page;
    const total = page.total_count, filtered = page.filtered_count, returned = page.returned_count;
    return [total, filtered, returned].every(value => Number.isSafeInteger(value) && (value as number) >= 0)
      && (total as number) >= (filtered as number)
      && (filtered as number) >= (returned as number)
      && returned === collection.items.length
      && (page.has_more === true
        ? typeof page.next_cursor === "string" && page.next_cursor.length > 0
        : page.has_more === false && page.next_cursor === null);
  });
}
