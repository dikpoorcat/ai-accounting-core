"""Asset readiness traces only inputs used by its rule; close proof stays independent."""

import pytest
from entity_fixture import seed_entities
from monthly_close_fixture import ready
from test_asset_batches import activate, month
from test_asset_batches import asset_engine as _asset_engine_fixture
from test_identity_corrections import confirm, opening_package
from test_identity_corrections import identity_engine as _identity_engine_fixture
from test_integrity_content import damage
from test_opening_continuation import _close_without_current_business
from test_reimbursement_assets import (
    accepted_batch,
    activate_assets,
    activation,
    batch_card,
    consume_assets,
    fact_evidence,
)
from test_reimbursement_assets import book as _reimbursed_book_fixture

from ai_accounting.kernel.contracts import KernelError, Read
from ai_accounting.kernel.domains import assets
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth


@pytest.fixture
def asset_engine_fixture(tmp_path):
    return _asset_engine_fixture.__wrapped__(tmp_path)


@pytest.fixture
def identity_engine_fixture(tmp_path):
    return _identity_engine_fixture.__wrapped__(tmp_path)


@pytest.fixture
def reimbursed_book_fixture(tmp_path):
    return _reimbursed_book_fixture.__wrapped__(tmp_path)


def _former_reads(period):
    before = YearMonth.from_ordinal(period.ordinal + 1)
    kinds = (
        "asset", "reimbursed_asset", "reimbursed_asset_batch", "opening_asset",
        "opening_identity_binding", "asset_activation", "asset_consumption",
        "asset_disposal", "loan_agreement", "loan_drawdown", "opening_loan",
        "loan_interest", *assets.ACTUAL_PAYMENT_KINDS, "employee_advance",
    )
    return tuple(
        Read(
            source,
            kind,
            "loan-principal"
            if kind in (*assets.ACTUAL_PAYMENT_KINDS, "employee_advance") else "*",
            before,
        )
        for source in ("fact", "calculation")
        for kind in kinds
        if source == "fact" or kind != "loan_agreement"
    )


def _former_work(period, context):
    rows = {(read.source, read.kind): context.select(read) for read in _former_reads(period)}
    issues = assets._required_asset_work(period, rows)
    issues.extend(assets._required_loan_work(period, rows))
    return issues


def _collect(engine, period, *, former=False):
    registry = engine.store.registry.readiness
    original = registry["assets_and_financing"]
    if former:
        registry["assets_and_financing"] = (_former_reads, _former_work)
    try:
        with QueryReads.snapshot(engine) as reads:
            return Periods(engine).collect_current_readiness(
                reads.connection, period, _query_reads=reads
            )
    finally:
        registry["assets_and_financing"] = original


def _asset_trace(result):
    return result["close_requirements"]["readiness"]["assets_and_financing"]


def test_real_batch_consumption_keeps_issues_and_needed_calculations(reimbursed_book_fixture):
    engine, save, publish = reimbursed_book_fixture
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    save("reimbursed_asset", "chair", batch_card(30000, asset_id="chair"))
    publish("batch", "computer", "chair")
    evidence = fact_evidence(engine, "batch")
    activate_assets(
        engine,
        "activation-batch",
        "2026-02",
        [
            ("activate-computer", activation()),
            ("activate-chair", activation(asset_id="chair", useful_life_months=6)),
        ],
        evidence,
    )
    consume_assets(engine, "2026-03", evidence)

    former = _collect(engine, "2026-03", former=True)
    current = _collect(engine, "2026-03")
    assert current["issues"] == former["issues"]
    assert current["close_requirements"]["issues"] == former["close_requirements"]["issues"]
    with engine.store.connection(read_only=True) as connection:
        consumption_facts = {
            row[0] for row in connection.execute(
                "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
                "WHERE s.kind='asset_consumption'"
            )
        }
        consumption_calculations = {
            row[0] for row in connection.execute(
                "SELECT id FROM calculation WHERE kind='asset_consumption'"
            )
        }
        batch_calculations = {
            row[0] for row in connection.execute(
                "SELECT id FROM calculation WHERE kind='reimbursed_asset_batch'"
            )
        }
    assert consumption_facts and consumption_calculations and batch_calculations
    assert consumption_facts <= set(_asset_trace(former)["facts"])
    assert consumption_facts.isdisjoint(_asset_trace(current)["facts"])
    assert consumption_calculations <= set(_asset_trace(current)["calculations"])
    assert batch_calculations <= set(_asset_trace(former)["calculations"])
    assert batch_calculations.isdisjoint(_asset_trace(current)["calculations"])


def test_real_selection_does_not_decode_growing_unused_consumption_facts(
    asset_engine_fixture, monkeypatch
):
    engine, evidence = asset_engine_fixture
    activate(engine, evidence)
    decoded = []
    original = Store.facts

    def counted(store, connection, identifiers):
        if store is engine.store:
            decoded.extend(identifiers)
        return original(store, connection, identifiers)

    observed_counts = []
    for period_text, request_id in (
        ("2026-01", "consume-january"),
        ("2026-02", "consume-february"),
        ("2026-03", "consume-march"),
    ):
        month(engine, evidence, period_text, request_id)
        period = YearMonth(period_text)
        former = _collect(engine, period_text, former=True)
        current = _collect(engine, period_text)
        assert current["issues"] == former["issues"]
        assert current["close_requirements"]["issues"] == former["close_requirements"]["issues"]
        with engine.store.connection(read_only=True) as connection:
            consumption_ids = {
                row[0] for row in connection.execute(
                    "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
                    "WHERE s.kind='asset_consumption'"
                )
            }
            result_calculations = {
                row[0] for row in connection.execute(
                    "SELECT id FROM calculation WHERE kind='asset_consumption'"
                )
            }
            acquisition_calculations = {
                row[0] for row in connection.execute(
                    "SELECT id FROM calculation WHERE kind='asset'"
                )
            }
        assert consumption_ids and result_calculations and acquisition_calculations
        observed_counts.append(len(consumption_ids))
        assert acquisition_calculations <= set(_asset_trace(former)["calculations"])
        assert acquisition_calculations.isdisjoint(_asset_trace(current)["calculations"])
        assert result_calculations <= set(_asset_trace(current)["calculations"])

        monkeypatch.setattr(Store, "facts", counted)
        decoded.clear()
        with QueryReads.snapshot(engine) as reads:
            reads.prime_select(assets.required_reads(period))
        assert consumption_ids.isdisjoint(decoded)
        decoded.clear()
        with QueryReads.snapshot(engine) as reads:
            reads.prime_select(_former_reads(period))
        assert consumption_ids <= set(decoded)
        monkeypatch.setattr(Store, "facts", original)
    assert observed_counts == sorted(observed_counts)
    assert len(set(observed_counts)) == 3


def test_opening_loan_identity_correction_keeps_exact_rule_issues(identity_engine_fixture):
    engine, evidence, first, second = identity_engine_fixture
    cash = Entities(engine).register_entity(
        "fund_account", {}, account_type="cash", source="synthetic", request_id="cash"
    )["entity_id"]
    agreement = {
        "period": "2026-01", "lender_id": first, "lender_is_licensed": True,
        "currency": "CNY", "annual_rate_percent": "12", "day_count_basis": "actual_360",
        "maturity_date": "2027-01-01", "loan_term": "short_term",
    }
    engine.save_fact(
        "loan_agreement", "agreement", agreement, evidence=(evidence,),
        expected_revision=0, request_id="agreement",
    )
    loan = {
        "agreement_id": "agreement", "lender_id": first, "loan_term": "short_term",
        "principal_fen": 120000, "accrued_interest_fen": 0,
        "interest_start": "2026-01-01",
    }
    opening_package(
        engine,
        evidence,
        [
            ("opening_loan", "principal", loan),
            ("opening_cash", "cash-opening", {"cash_account_id": cash, "balance_fen": 120000}),
        ],
    )
    confirm(
        engine,
        {
            "changes": [
                {"subject_id": "agreement", "expected_revision": 1, "action": "reassign",
                 "data": agreement | {"lender_id": second}},
                {"subject_id": "principal", "expected_revision": 1, "action": "reassign",
                 "data": {"period": "2026-01", "package_id": "opening", **loan}
                 | {"lender_id": second}},
            ],
            "evidence": [evidence],
            "reason": "synthetic lender identity correction",
        },
    )
    former = _collect(engine, "2026-01", former=True)
    current = _collect(engine, "2026-01")
    assert current["issues"] == former["issues"]


def test_frozen_opening_binding_keeps_exact_rule_issues(identity_engine_fixture):
    engine, evidence, first, second = identity_engine_fixture
    opening = {
        "counterparty_id": first,
        "nature": "customer_receivable",
        "outstanding_fen": 20000,
        "business_reference": "confirmed-invoice",
    }
    opening_package(
        engine,
        evidence,
        [
            ("opening_obligation", "receivable", opening),
            (
                "opening_equity",
                "capital",
                {
                    "equity_kind": "retained_earnings",
                    "balance_fen": 20000,
                    "holder_or_basis_id": first,
                },
            ),
        ],
    )
    _close_without_current_business(engine, "2026-01", evidence)
    confirm(
        engine,
        {
            "changes": [
                {
                    "subject_id": "receivable",
                    "expected_revision": 1,
                    "action": "reassign",
                    "data": {"period": "2026-01", "package_id": "opening", **opening}
                    | {"counterparty_id": second},
                }
            ],
            "evidence": [evidence],
            "reason": "synthetic customer identity correction",
            "posting_period": "2026-02",
        },
    )
    former = _collect(engine, "2026-02", former=True)
    current = _collect(engine, "2026-02")
    assert current["issues"] == former["issues"]
    with engine.store.connection(read_only=True) as connection:
        binding_facts = {row[0] for row in connection.execute(
            "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind='opening_identity_binding'"
        )}
        binding_calculations = {row[0] for row in connection.execute(
            "SELECT id FROM calculation WHERE kind='opening_identity_binding'"
        )}
    assert binding_facts and binding_calculations
    assert binding_facts <= set(_asset_trace(current)["facts"])
    assert binding_calculations <= set(_asset_trace(former)["calculations"])
    assert binding_calculations.isdisjoint(_asset_trace(current)["calculations"])


def test_unpublished_asset_and_corrupt_source_still_block_formal_close(asset_engine_fixture):
    engine, evidence = asset_engine_fixture
    ready(engine, evidence, first="2026-01", last="2026-01")
    seed_entities(engine, [("fixed-c", "asset", None)])
    engine.save_fact(
        "asset", "fixed-c",
        {
            "period": "2026-01", "asset_id": "fixed-c", "asset_type": "fixed",
            "acquisition_date": "2026-01-04", "supplier_id": "supplier",
            "cost_fen": 30000, "acquisition_basis": "direct_purchase",
        },
        evidence=(evidence,), expected_revision=0, request_id="unpublished-asset",
    )
    with pytest.raises(KernelError) as unpublished:
        Periods(engine).preview_close("2026-01", owner_confirmation=evidence)
    assert unpublished.value.code == "period_not_ready"
    assert any(
        issue.get("field") == "fixed-c"
        for issue in unpublished.value.details["fact_issues"]
    )

    damage(
        engine,
        "fact_asset",
        "UPDATE fact_asset SET cost_fen=cost_fen+1 WHERE revision_id=("
        "SELECT fact_id FROM fact_current WHERE subject_id='fixed-a')",
    )
    with pytest.raises(KernelError) as corrupt:
        Periods(engine).preview_close("2026-01", owner_confirmation=evidence)
    assert corrupt.value.code == "content_integrity_failed"
