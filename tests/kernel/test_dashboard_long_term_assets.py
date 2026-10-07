"""Brief asset totals reuse ledger proof and count only adopted asset identities."""

import json

import pytest
from stage9_metrics import measure_work
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import activate, activation_members
from test_asset_batches import asset_engine as asset_engine_fixture
from test_asset_owner_frozen_scope import amend_activation
from test_banking import consume_assets
from test_integrity_content import damage
from test_labor_assets import cost
from test_opening_continuation import book as opening_book_fixture
from test_payroll_corrections import Company
from test_reimbursement_assets import consume_assets as revise_consumption

from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _active_asset_counts, _long_term_assets
from ai_accounting.kernel.domains.assets import AssetDisposal
from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
from ai_accounting.kernel.domains.transactions import Expense
from ai_accounting.kernel.runtime import _PrivateConnection

asset_engine = asset_engine_fixture
opening_book = opening_book_fixture


def summary(dashboard, period):
    with dashboard._snapshot(period) as snap:
        return _long_term_assets(snap)


def assert_counts_match_asset_page(dashboard, period, fixed, intangible):
    result = summary(dashboard, period)
    assets = dashboard.assets(period, preparation="deferred")["data"]
    assert result == {
        "net_fen": assets["ledger_net_fen"],
        "fixed_active_count": fixed,
        "intangible_active_count": intangible,
    }
    assert result["fixed_active_count"] == assets["fixed"]["active_count"]
    assert result["intangible_active_count"] == assets["intangible"]["active_count"]
    return result


def test_opening_counts_cross_month_and_net_includes_all_seven_accounts(opening_book):
    engine, save, publish, package, _ = opening_book
    package([
        ("opening_asset", "fixed-opening", {
            "asset_id": "fixed-opening", "asset_type": "fixed", "cost_fen": 120000,
            "accumulated_fen": 20000, "in_use_date": "2025-10-12",
            "useful_life_months": 12, "completed_months": 2, "residual_fen": 0,
            "benefit_area": "administration", "rounding_policy": "floor_final_remainder",
        }),
        ("opening_asset", "intangible-opening", {
            "asset_id": "intangible-opening", "asset_type": "intangible", "cost_fen": 60000,
            "accumulated_fen": 10000, "in_use_date": "2025-11-12",
            "useful_life_months": 12, "completed_months": 2, "residual_fen": 0,
            "benefit_area": "administration", "rounding_policy": "floor_final_remainder",
        }),
        ("opening_equity", "equity", {
            "equity_kind": "retained_earnings", "balance_fen": 150000,
            "holder_or_basis_id": "prior-statements",
        }),
    ])
    for asset_id, asset_type, amount in (
        ("pending-fixed", "fixed", 30000), ("pending-intangible", "intangible", 20000),
    ):
        save("asset", asset_id, {
            "period": "2026-01", "asset_id": asset_id, "asset_type": asset_type,
            "cost_fen": amount, "acquisition_date": "2026-01-15",
            "supplier_id": "supplier", "acquisition_basis": "direct_purchase",
        })
    save("project_cost", "project", {
        "period": "2026-01", "project_id": "internal-project", "supplier_id": "supplier",
        "amount_fen": 1600000, "project_nature": "internal_development",
        "capitalization_conditions_confirmed": True,
    })
    publish("pending-fixed", "pending-intangible", "project")
    save("expense", "february-source", {
        "period": "2026-02", "counterparty_id": "supplier", "amount_fen": 1,
        "expense_class": "administration", "creditor_kind": "supplier",
    })
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-01") as snap:
        assert {account: snap.accounts[account] for account in (
            "1601", "1701", "1604", "189901", "4301", "1602", "1702",
        )} == {
            "1601": 120000, "1701": 60000, "1604": 30000, "189901": 20000,
            "4301": 1600000, "1602": -20000, "1702": -10000,
        }
    for period in ("2026-01", "2026-02"):
        assert assert_counts_match_asset_page(dashboard, period, 1, 1)["net_fen"] == 1800000
    assert dashboard.brief("2026-01")["data"]["long_term_assets"] == summary(
        dashboard, "2026-01",
    )


def test_batch_activation_and_open_member_withdrawal_keep_only_current_members(asset_engine):
    engine, evidence = asset_engine
    dashboard = Dashboard(engine)
    assert assert_counts_match_asset_page(dashboard, "2026-01", 0, 0)["net_fen"] == 30004
    activate(engine, evidence)
    assert_counts_match_asset_page(dashboard, "2026-01", 1, 1)
    options = {
        "subject_id": "activation-batch", "period": "2026-01",
        "members": activation_members(1)[:1], "evidence": (evidence,), "expected_revision": 1,
    }
    batches = AssetBatches(engine)
    preview = batches.prepare_activation_batch(**options)
    batches.confirm_activation_batch(
        **options, preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id="brief-withdraw-intangible",
    )
    assert assert_counts_match_asset_page(dashboard, "2026-01", 1, 0)["net_fen"] == 30004


def test_batch_acceptance_and_cards_are_deduplicated_and_disposal_excludes_asset(tmp_path):
    company = Company(tmp_path / "brief-disposal.sqlite")
    prepare_batch_assets(company)
    company.close("2026-02")
    dashboard = Dashboard(company.engine)
    assert_counts_match_asset_page(dashboard, "2026-02", 2, 0)
    consume_assets(company.engine, company.owner_confirmation, "2026-03")
    company.save(AssetDisposal(
        period="2026-03", asset_id="chair", disposal_date="2026-03-31",
        disposal_kind="scrap", gross_proceeds_fen=0,
    ), "scrap-chair")
    revise_consumption(
        company.engine, "2026-03", (company.owner_confirmation,), expected_revision=1,
        request_id="brief-post-disposal",
    )
    assert_counts_match_asset_page(dashboard, "2026-02", 2, 0)
    consume_assets(company.engine, company.owner_confirmation, "2026-04")
    for period in ("2026-03", "2026-04"):
        assert_counts_match_asset_page(dashboard, period, 1, 0)


def test_closed_activation_correction_preserves_frozen_and_current_counts(tmp_path):
    company = Company(tmp_path / "brief-correction.sqlite")
    prepare_batch_assets(company)
    company.close("2026-02")
    dashboard = Dashboard(company.engine)
    frozen = assert_counts_match_asset_page(dashboard, "2026-02", 2, 0)
    amend_activation(
        company, company.owner_confirmation, revision=1, posting_period="2026-03",
        benefit_area="service",
    )
    assert summary(dashboard, "2026-02") == frozen
    assert_counts_match_asset_page(dashboard, "2026-03", 2, 0)
    consume_assets(company.engine, company.owner_confirmation, "2026-03")
    company.close("2026-03")
    assert_counts_match_asset_page(dashboard, "2026-03", 2, 0)


def test_count_reads_do_not_expand_project_unrelated_or_consumption_bodies(tmp_path, monkeypatch):
    company = Company(tmp_path / "brief-growth.sqlite")
    prepare_batch_assets(company)
    company.save(Expense(
        period="2026-05", counterparty_id="supplier", amount_fen=1,
        expense_class="administration", creditor_kind="supplier",
    ), "may-source")
    dashboard = Dashboard(company.engine)
    count_queries = []
    original_execute = _PrivateConnection.execute

    def observed(connection, sql, *args, **kwargs):
        if "acquisition_rows AS" in sql and "GROUP BY asset_type" in sql:
            count_queries.append((sql, args[0]))
        return original_execute(connection, sql, *args, **kwargs)

    monkeypatch.setattr(_PrivateConnection, "execute", observed)

    def counts():
        with dashboard._snapshot("2026-05") as snap:
            return _active_asset_counts(snap)

    initial_work, initial = measure_work(company.engine, counts)
    for period in ("2026-03", "2026-04", "2026-05"):
        consume_assets(company.engine, company.owner_confirmation, period)
    for index in range(6):
        company.save(Expense(
            period="2026-03", counterparty_id="supplier", amount_fen=index + 1,
            expense_class="administration", creditor_kind="supplier",
        ), f"unrelated-{index}")
        company.save(LaborProjectCost.model_validate(cost(
            period="2026-03", project_id=f"project-{index}",
        )), f"project-{index}")
    company.publish(*[
        subject for index in range(6) for subject in (f"unrelated-{index}", f"project-{index}")
    ])

    def unexpected_history(*_args, **_kwargs):
        raise AssertionError("asset counts must not build card, project, or consumption details")

    monkeypatch.setattr(BusinessQueries, "_selected_asset_member_heads", unexpected_history)
    monkeypatch.setattr("ai_accounting.kernel.dashboard._asset_card_sources", unexpected_history)
    monkeypatch.setattr("ai_accounting.kernel.dashboard._project_cost_balances", unexpected_history)
    grown_work, grown = measure_work(company.engine, counts)
    assert grown == initial == {"fixed": 2}
    for name in (
        "calculation_result_rows_loaded", "calculation_result_bytes_loaded",
        "calculation_result_json_decodes", "typed_fact_json_decodes", "raw_fact_json_decodes",
    ):
        assert grown_work["counters"][name] == initial_work["counters"][name]
    assert len(count_queries) == 2
    with company.engine.store.connection(read_only=True) as connection:
        query, parameters = count_queries[-1]
        query_plan = [row[3] for row in connection.execute(
            "EXPLAIN QUERY PLAN " + query, parameters,
        )]
    assert not any(detail.startswith("SCAN f") for detail in query_plan)
    print(json.dumps({
        "initial": initial_work["counters"], "grown": grown_work["counters"],
        "count_query_plan": query_plan,
    }))


@pytest.mark.parametrize("broken", ["acceptance_detail", "activation_member", "frozen_adoption"])
def test_count_rejects_damaged_required_identity_instead_of_reporting_lower_count(tmp_path, broken):
    company = Company(tmp_path / "brief-damage.sqlite")
    prepare_batch_assets(company)
    company.close("2026-02")
    dashboard = Dashboard(company.engine)
    assert summary(dashboard, "2026-02")["fixed_active_count"] == 2
    if broken == "acceptance_detail":
        damage(company.engine, "fact_reimbursed_asset_batch_assets",
               "DELETE FROM fact_reimbursed_asset_batch_assets WHERE asset_id='chair'")
    elif broken == "activation_member":
        damage(company.engine, "asset_batch_member",
               "DELETE FROM asset_batch_member WHERE asset_id='chair'")
    else:
        damage(company.engine, "close_reference",
               "UPDATE close_reference SET related_id='forged' WHERE reference_type='voucher'")
    with dashboard._snapshot("2026-02") as snap:
        with pytest.raises(KernelError):
            _active_asset_counts(snap)
