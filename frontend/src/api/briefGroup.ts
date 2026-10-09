import { pageQuery, validDashboardCollections, type DashboardPageQuery } from "./dashboardContracts";
import { requestGeneratedJson } from "./client";
import type { DashboardBriefGroupResponse } from "./generated/dashboardBriefGroup";

export type BriefGroupResponse = DashboardBriefGroupResponse;
export type BriefGroupSection = "activity" | "open_items";

function matchesRequest(url: URL, response: BriefGroupResponse) {
  const data = response.data;
  const members = data.collections.members.items;
  const ids = [...new Set(members.flatMap(item => item.voucher_version_id ? [item.voucher_version_id] : []))];
  return response.read_context.company_id === url.searchParams.get("company_id")
    && response.selected_period?.key === url.searchParams.get("period")
    && response.snapshot_version === url.searchParams.get("expected_version")
    && data.section === url.searchParams.get("section")
    && data.group_key === url.searchParams.get("group_key")
    && validDashboardCollections(data)
    && members.every(item => item.group_key === data.group_key)
    && ids.length === data.collections.vouchers.items.length
    && ids.every((id, index) => data.collections.vouchers.items[index]?.voucher_version_id === id);
}

export async function fetchBriefGroup(companyId: string, period: string, section: BriefGroupSection, groupKey: string, expectedVersion: string, signal?: AbortSignal, options: DashboardPageQuery = {}) {
  const query = new URLSearchParams({ company_id: companyId, period, section, group_key: groupKey, expected_version: expectedVersion });
  pageQuery(query, { ...options, limit: options.limit ?? 20 });
  const { validateDashboardBriefGroupResponse } = await import("./generated/dashboardBriefGroup.js");
  signal?.throwIfAborted();
  return requestGeneratedJson(`/api/dashboard/brief-group?${query}`, "/api/dashboard/brief-group", validateDashboardBriefGroupResponse, matchesRequest, { signal });
}
