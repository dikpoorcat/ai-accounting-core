import { requestJson } from "./client";
import type { DashboardPeriod } from "./context";

export type AssetStatus = "active" | "pending_activation" | "disposed" | "retired";

interface AssetItemBase {
  asset_type: "fixed" | "intangible";
  code: string;
  name: string;
  category: string;
  category_label: string;
  status: AssetStatus;
  status_label: string;
  acquisition_date: string;
  posting_date: string;
  supplier: string;
  settlement_method: string;
  settlement_label: string;
  payment_date: string | null;
  due_date: string | null;
  purchase_price_fen: string;
  noncreditable_tax_fen: string;
  other_direct_cost_fen: string;
  cost_fen: string;
  accumulated_charge_fen: string;
  month_charge_fen: string;
  book_value_fen: string;
  latest_charge_period: string | null;
  benefit_area_label: string | null;
  useful_life_months: number | null;
  acquisition_reference: string;
}

export interface FixedAssetDisposal {
  kind: "sale" | "retirement";
  date: string;
  gross_proceeds_fen: string;
  book_value_fen: string;
  gain_fen: string;
  loss_fen: string;
  party: string;
  reference: string;
}

export interface FixedAssetItem extends AssetItemBase {
  asset_type: "fixed";
  in_service_date: string | null;
  reimbursing_employee: string;
  residual_value_fen: string | null;
  depreciation_method_label: string | null;
  depreciation_group_code: string | null;
  disposal: FixedAssetDisposal | null;
}

export interface IntangibleAssetRetirement {
  date: string;
  book_value_fen: string;
  reference: string;
}

export interface IntangibleAssetItem extends AssetItemBase {
  asset_type: "intangible";
  available_for_use_date: string;
  life_basis_label: string;
  life_basis_explanation: string;
  rights_description: string;
  retirement: IntangibleAssetRetirement | null;
}

export type AssetItem = FixedAssetItem | IntangibleAssetItem;

export interface FixedAssetSummary {
  registered_count: number;
  active_count: number;
  pending_count: number;
  disposed_count: number;
  active_cost_fen: string;
  active_accumulated_fen: string;
  active_net_fen: string;
  pending_cost_fen: string;
  month_depreciation_fen: string;
  month_acquired_count: number;
  month_acquired_fen: string;
  month_activated_count: number;
  month_disposed_count: number;
  items: FixedAssetItem[];
}

export interface IntangibleAssetSummary {
  registered_count: number;
  active_count: number;
  retired_count: number;
  active_cost_fen: string;
  active_accumulated_fen: string;
  active_net_fen: string;
  month_amortization_fen: string;
  month_acquired_count: number;
  month_acquired_fen: string;
  month_retired_count: number;
  items: IntangibleAssetItem[];
}

export interface AssetsDashboardData {
  fixed_asset_cost_fen: string;
  accumulated_depreciation_fen: string;
  fixed_asset_net_fen: string;
  intangible_asset_cost_fen: string;
  accumulated_amortization_fen: string;
  intangible_asset_net_fen: string;
  active_count: number;
  registered_count: number;
  ledger_cost_fen: string;
  ledger_accumulated_fen: string;
  ledger_net_fen: string;
  card_cost_fen: string;
  card_accumulated_fen: string;
  card_net_fen: string;
  pending_fixed_count: number;
  pending_fixed_cost_fen: string;
  month_charge_fen: string;
  month_acquired_count: number;
  month_acquired_fen: string;
  month_activated_count: number;
  month_exited_count: number;
  reconciled: boolean;
  reconciliation_label: string;
  differences: {
    cost_fen: string;
    accumulated_fen: string;
    net_fen: string;
  };
  fixed: FixedAssetSummary;
  intangible: IntangibleAssetSummary;
}

export interface AssetsDashboardResponse {
  schema_version: 1;
  selected_period: DashboardPeriod | null;
  data: AssetsDashboardData | null;
}

export function fetchAssetsDashboard(period: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ period });
  return requestJson<AssetsDashboardResponse>(`/api/dashboard/assets?${query}`, {
    signal,
  });
}
