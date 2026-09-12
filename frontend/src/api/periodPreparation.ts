import { DashboardApiError, requestJson } from "./client";
import type { BriefData, BriefValidationItem, BriefValidation } from "./brief";
import type { DashboardReadContext, PeriodPreparation } from "./dashboardContracts";

export interface PeriodPreparationResult {
  schema_version: 1;
  projection: "dashboard_period_preparation_result";
  read_context: DashboardReadContext;
  period: string;
  data: {
    period_preparation: PeriodPreparation;
    brief_checks: {
      material_completeness: BriefData["material_completeness"];
      issues: NonNullable<BriefValidation["issues"]>;
      attention_count: number;
      items: BriefValidationItem[];
    };
  };
}

export async function fetchPeriodPreparation(context: DashboardReadContext, period: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ company_id: context.company_id, period, expected_read_version: context.read_version, as_of: context.as_of });
  const result = await requestJson<PeriodPreparationResult>(`/api/dashboard/period-preparation?${query}`, { signal });
  if (result.read_context.database_id !== context.database_id) {
    throw new DashboardApiError(409, "dashboard_snapshot_changed", "资料已变化，请刷新主页面后重新核对。");
  }
  return result;
}
