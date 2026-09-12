import { requestJson } from "./client";
import type { DashboardPeriod } from "./context";
import { pageQuery, type DashboardCollections, type DashboardPageQuery, type PeriodPreparation } from "./dashboardContracts";
import type { DashboardCollection, UnestablishedSelection } from "./dashboardContracts";
import type { SettlementView } from "./employees";

export type AssetStatus = "active" | "pending_activation" | "disposed" | "retired";

interface AssetItemBase {
  selection_status?: "established";
  asset_id: string;
  asset_type: "fixed" | "intangible";
  code: string;
  name: string;
  category: string;
  category_label: string;
  status: AssetStatus;
  status_label: string;
  acquisition_date: string | null;
  posting_period: string;
  recognition_label: string;
  source_label: string;
  source_party_label: string;
  source_parties: string | null;
  settlement_scope: string;
  settlements: Array<SettlementView & { source_id: string; label: string }>;
  cost_fen: string | null;
  accumulated_charge_fen: string | null;
  month_charge_fen: string | null;
  book_value_fen: string | null;
  latest_charge_period: string | null;
  benefit_area_label: string | null;
  useful_life_months: number | null;
  acquisition_reference: string;
}

export interface FixedAssetDisposal {
  settlement: SettlementView;
  kind: "sale" | "retirement";
  date: string;
  gross_proceeds_fen: string | null;
  book_value_fen: string | null;
  gain_fen: string | null;
  loss_fen: string | null;
  party: string;
  reference: string;
}

export interface FixedAssetItem extends AssetItemBase {
  asset_type: "fixed";
  in_service_date: string | null;
  residual_value_fen: string | null;
  depreciation_method_label: string | null;
  rounding_policy_label: string | null;
  disposal: FixedAssetDisposal | null;
}

export interface IntangibleAssetRetirement {
  settlement: SettlementView;
  date: string;
  book_value_fen: string | null;
  reference: string;
}

export interface IntangibleAssetItem extends AssetItemBase {
  asset_type: "intangible";
  available_for_use_date: string | null;
  life_basis_label: string;
  life_basis_explanation: string;
  rights_description: string;
  retirement: IntangibleAssetRetirement | null;
}

export type EstablishedAssetItem = FixedAssetItem | IntangibleAssetItem;
export interface UnestablishedAssetItem {
  asset_id: string;
  asset_type: "fixed" | "intangible" | null;
  name: string | null;
  selection_status: "unestablished";
  candidate_selections: UnestablishedSelection[];
  established_card?: EstablishedAssetItem;
  trace_targets: Array<{ calculation_id: string; voucher_version_id?: string | null }>;
}
export type AssetItem = EstablishedAssetItem | UnestablishedAssetItem;

export interface FixedAssetSummary {
  unestablished_count?: number;
  registered_count: number;
  active_count: number;
  pending_count: number;
  disposed_count: number;
  active_cost_fen: string | null;
  active_accumulated_fen: string | null;
  active_net_fen: string | null;
  pending_cost_fen: string | null;
  month_depreciation_fen: string | null;
  month_acquired_count: number;
  month_acquired_fen: string | null;
  month_cost_adjustment_fen?: string | null;
  month_activated_count: number;
  month_disposed_count: number;
  items: Array<FixedAssetItem | UnestablishedAssetItem>;
}

export interface IntangibleAssetSummary {
  unestablished_count?: number;
  pending_count: number;
  pending_cost_fen: string | null;
  registered_count: number;
  active_count: number;
  retired_count: number;
  active_cost_fen: string | null;
  active_accumulated_fen: string | null;
  active_net_fen: string | null;
  month_amortization_fen: string | null;
  month_acquired_count: number;
  month_acquired_fen: string | null;
  month_cost_adjustment_fen?: string | null;
  month_retired_count: number;
  items: Array<IntangibleAssetItem | UnestablishedAssetItem>;
}

export interface AssetsDashboardData {
  unestablished_count?: number;
  period_preparation: PeriodPreparation;
  collections: DashboardCollections & { assets: DashboardCollection<AssetItem> };
  active_ledger_net_fen: string | null;
  pending_intangible_count: number;
  pending_intangible_cost_fen: string | null;
  project_cost_fen: string | null;
  reconciliation_scope: string;
  projects: Array<{ source_id: string; project_id: string; period: string; kind: string; label: string; party: string; cost_fen: string | null; remaining_fen: string | null; settlement: SettlementView }>;
  fixed_asset_cost_fen: string | null;
  accumulated_depreciation_fen: string | null;
  fixed_asset_net_fen: string | null;
  intangible_asset_cost_fen: string | null;
  accumulated_amortization_fen: string | null;
  intangible_asset_net_fen: string | null;
  active_count: number;
  registered_count: number;
  ledger_cost_fen: string | null;
  ledger_accumulated_fen: string | null;
  ledger_net_fen: string | null;
  card_cost_fen: string | null;
  card_accumulated_fen: string | null;
  card_net_fen: string | null;
  pending_fixed_count: number;
  pending_fixed_cost_fen: string | null;
  month_charge_fen: string | null;
  month_acquired_count: number;
  month_acquired_fen: string | null;
  month_cost_adjustment_fen?: string | null;
  month_activated_count: number;
  month_exited_count: number;
  reconciled: boolean | null;
  reconciliation_label: string;
  differences: {
    cost_fen: string | null;
    accumulated_fen: string | null;
    net_fen: string | null;
  };
  fixed: FixedAssetSummary;
  intangible: IntangibleAssetSummary;
}

export interface AssetsDashboardResponse {
  schema_version: 2;
  snapshot_version: string;
  selected_period: DashboardPeriod | null;
  data: AssetsDashboardData | null;
}

export interface AssetsQuery extends DashboardPageQuery {
  section?: "assets" | "projects" | "source_history" | "settlement_events";
  asset_filter?: "all" | "active" | "fixed" | "intangible" | "pending" | "exited";
  asset_id?: string;
  project_id?: string;
}

export function fetchAssetsDashboard(period: string, signal?: AbortSignal, options: AssetsQuery = {}) {
  const query = new URLSearchParams({ period });
  pageQuery(query, options);
  if (options.asset_filter) query.set("asset_filter", options.asset_filter);
  if (options.asset_id) query.set("asset_id", options.asset_id);
  if (options.project_id) query.set("project_id", options.project_id);
  return requestJson<AssetsDashboardResponse>(`/api/dashboard/assets?${query}`, {
    signal,
  });
}
