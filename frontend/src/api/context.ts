import { requestJson } from "./client";

export interface DashboardPeriod {
  key: string;
  year: number;
  month: number;
  label: string;
  short_label: string;
  status: "open" | "closed" | string;
  start_date: string;
  end_date: string;
  closed_at: string | null;
}

export interface DashboardQuarter {
  key: string;
  year: number;
  quarter: number;
  label: string;
  complete: boolean;
}

export interface DashboardContext {
  schema_version: 1;
  company: string;
  generated_at: string;
  default_period: string | null;
  periods: DashboardPeriod[];
  default_quarter: string | null;
  quarters: DashboardQuarter[];
  disclaimer: string;
}

export function fetchDashboardContext(signal?: AbortSignal) {
  return requestJson<DashboardContext>("/api/dashboard/context", { signal });
}
