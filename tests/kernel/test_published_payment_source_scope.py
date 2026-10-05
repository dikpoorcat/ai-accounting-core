"""Real payment source families remain valid through the narrow own-anchor reader."""

import pytest
import test_asset_batches as assets
import test_cash as cash
import test_investments as investments
import test_opening_continuation as opening
import test_payment_party_scope as party
import test_reimbursement_assets as reimbursement
from entity_fixture import seed_registration_entities
from test_brief_activity_reuse import classify

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_reads import Journal
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_open_contribution import verify_published_source_bindings
from ai_accounting.kernel.types import YearMonth, canonical

cash_book = cash.book
opening_book = opening.book
investment_book = investments.book
reimbursement_book = reimbursement.book
party_book = party.book
asset_engine = assets.asset_engine


def _actual_settlement_sources(engine):
    with QueryReads.snapshot(engine) as reads:
        roots = {
            row[0] for row in reads.connection.execute(
                "SELECT calculation_id FROM calculation_publication "
                "WHERE calculation_id IS NOT NULL"
            )
        }
        assert verify_published_source_bindings(reads, roots) == frozenset()
        reads.verify_sql_outcomes(roots)
        sources = {
            row[0] for row in reads.connection.execute(
                "SELECT json_extract(item.value,'$.source_calculation') "
                "FROM json_each(?) ids CROSS JOIN calculation c ON c.id=ids.value "
                "JOIN json_each(c.outcome,'$.values.settlements') item",
                (canonical(sorted(roots)),),
            )
        }
        assert sources
        assert None not in sources
        assert verify_published_source_bindings(reads, sources) == frozenset()
        reads.verify_sql_outcomes(sources)
        kinds = {
            row[0] for row in reads.connection.execute(
                "SELECT c.kind FROM json_each(?) ids JOIN calculation c ON c.id=ids.value",
                (canonical(sorted(sources)),),
            )
        }
        periods = {
            str(YearMonth.from_ordinal(row[0])) for row in reads.connection.execute(
                "SELECT DISTINCT period FROM voucher_version"
            )
        }
    for period in periods:
        actual = classify(engine, period)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Journal, "verified_rows", lambda self: None)
            assert classify(engine, period) == actual
    return kinds


@pytest.mark.parametrize("scenario,expected", [
    (cash.test_cash_payments_use_inventory_cash_and_need_no_bank_statement, {"expense"}),
    (cash.test_deposit_advanced_by_employee_remains_receivable_when_cash_reimburses_employee,
     {"employee_advance"}),
    (cash.test_historical_gross_labor_cash_payment_and_accrual_publish_together, {"labor"}),
    (cash.test_future_partial_pass_through_return_does_not_reopen_closed_return, {"pass_through"}),
])
def test_real_cash_and_bank_settlements_keep_existing_business_results(
    cash_book, scenario, expected
):
    scenario(cash_book)
    assert expected <= _actual_settlement_sources(cash_book[0])


@pytest.mark.parametrize("scenario,expected", [
    (opening.test_detail_cannot_be_consumed_before_package_and_later_real_receipt_settles_it,
     {"opening_obligation"}),
    (opening.test_asset_depreciation_and_split_loan_interest_continue_without_fake_drawdown,
     {"opening_loan"}),
])
def test_real_opening_settlements_keep_exact_independent_sources(opening_book, scenario, expected):
    scenario(opening_book)
    assert expected <= _actual_settlement_sources(opening_book[0])


@pytest.mark.parametrize("scenario,expected", [
    (reimbursement.test_deposit_acceptance_separates_landlord_right_employee_debt_and_real_funds,
     {"reimbursed_deposit"}),
    (reimbursement.test_month_precision_reuses_acceptance_without_inventing_personal_payment_day,
     {"reimbursed_asset"}),
])
def test_real_accepted_cost_settlements_keep_all_obligation_variants(
    reimbursement_book, scenario, expected
):
    scenario(reimbursement_book)
    assert expected <= _actual_settlement_sources(reimbursement_book[0])


def test_real_investment_payments_keep_direct_subscription_and_redemption_sources(investment_book):
    investments.test_purchase_partial_redemption_actual_cash_reports_and_rebuild(investment_book)
    assert {"money_fund_subscription", "money_fund_redemption"} <= _actual_settlement_sources(
        investment_book[0]
    )


def test_real_unnamed_rightsholder_payment_keeps_published_source(party_book):
    party.test_explicit_unnamed_rightsholder_keeps_liability_without_fake_entity(party_book)
    assert "pass_through" in _actual_settlement_sources(party_book[0])


@pytest.mark.parametrize("member_kind", ["asset_activation", "asset_consumption"])
def test_unpublished_asset_member_cannot_be_a_legal_payment_obligation(asset_engine, member_kind):
    engine, evidence = asset_engine
    assets.activate(engine, evidence)
    if member_kind == "asset_consumption":
        assets.month(engine, evidence, "2026-01", "consume")
    with QueryReads.snapshot(engine) as reads:
        member = reads.connection.execute(
            "SELECT c.id,c.subject_id FROM calculation c WHERE c.kind=? LIMIT 1", (member_kind,)
        ).fetchone()
        assert reads.verify_saved_input_identity({member["id"]})[member["id"]][
            "values"
        ].get("obligations", []) == []
        with pytest.raises(KernelError) as failure:
            verify_published_source_bindings(reads, {member["id"]})
        assert failure.value.details["reason"] == "own_publication_missing"
    data = {
        "period": "2026-01", "actual_date": "2026-01-05", "direction": "outflow",
        "cash_account_id": "cash", "counterparty_id": "supplier", "amount_fen": 1,
        "allocations": [{
            "source_kind": member_kind, "source_id": member["subject_id"],
            "obligation": "primary", "amount_fen": 1,
        }],
    }
    seed_registration_entities(engine, "cash_payment", data)
    engine.save_fact("cash_payment", "invalid-member-payment", data, evidence=(evidence,),
                     expected_revision=0, request_id="member-payment")
    with pytest.raises(KernelError) as failure:
        engine.preview(["invalid-member-payment"])
    assert failure.value.code == "unknown_obligation"


def test_direct_asset_acquisition_retains_its_own_published_payment_source(asset_engine):
    engine, evidence = asset_engine
    with QueryReads.snapshot(engine) as reads:
        source = reads.connection.execute(
            "SELECT c.id,c.subject_id FROM calculation c WHERE c.kind='asset' LIMIT 1"
        ).fetchone()
        outcome = reads.verify_saved_input_identity({source["id"]})[source["id"]]
        item = outcome["values"]["obligations"][0]
        assert verify_published_source_bindings(reads, {source["id"]}) == frozenset()
    data = {
        "period": "2026-01", "actual_date": "2026-01-05", "direction": "outflow",
        "cash_account_id": "cash", "counterparty_id": "supplier", "amount_fen": item["amount_fen"],
        "allocations": [{
            "source_kind": "asset", "source_id": source["subject_id"],
            "obligation": item["name"], "amount_fen": item["amount_fen"],
        }],
    }
    seed_registration_entities(engine, "cash_payment", data)
    engine.save_fact("cash_payment", "acquisition-payment", data, evidence=(evidence,),
                     expected_revision=0, request_id="acquisition-payment")
    preview = engine.preview(["acquisition-payment"])
    result = engine.confirm(["acquisition-payment"], preview_digest=preview["digest"],
                            epochs=preview["epochs"], request_id="publish-acquisition-payment")
    assert result["status"] == "published"
    assert _actual_settlement_sources(engine) == {"asset"}
