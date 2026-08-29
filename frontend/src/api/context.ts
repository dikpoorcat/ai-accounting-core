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
  schema_version: 2;
  company: string;
  companies: DashboardCompany[];
  current_company: DashboardCompany;
  generated_at: string;
  default_period: string | null;
  periods: DashboardPeriod[];
  default_quarter: string | null;
  quarters: DashboardQuarter[];
  disclaimer: string;
}

export interface DashboardCompany {
  org_id: string;
  name: string;
  status: "active" | "archived";
}

export function fetchDashboardContext(signal?: AbortSignal) {
  return requestJson<DashboardContext>("/api/dashboard/context", { signal });
}
