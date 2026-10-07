"""Owner money amounts retain proofs without consuming bank matching presentation."""

import json
from copy import deepcopy

import pytest
import test_banking as banking
import test_platforms as platforms
from stage9_metrics import measure_work
from test_funds_historical_account_identities import source
from test_bank_identity_witness_scope import history
from test_integrity_content import damage
from test_opening_continuation import book as _opening_book

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FUND_TYPES, FundsRead, _sum, funds
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.identity_corrections import IdentityCorrections
from ai_accounting.kernel.query_reads import QueryReads

bank_book = banking.book
platform_book = platforms.book
opening_book = _opening_book
MONTH = "2026-09"
AMOUNTS = {
    "total_fen", "bank_fen", "cash_fen", "payment_platform_fen", "inflow_fen",
    "outflow_fen", "net_change_fen", "internal_transfer_fen",
}


def summary(engine, period=MONTH):
    with Dashboard(engine)._snapshot(period) as snap:
        # Match the brief's preceding complete month-amount proof.
        snap.month_journal.account_amounts()
        return funds(snap, summary_only=True)


def legacy_summary(engine):
    """Execute the actual retired bank work on the same reader, without mocks."""
    with Dashboard(engine)._snapshot(MONTH) as snap:
        snap.month_journal.account_amounts()
        read = FundsRead(snap)
        movements = read.account_summary()
        bank = read.bank_summary()
        assert bank["transaction_count"] > 0
        accounts = [*read.account_rows.values(), *read.omitted_account_rows.values()]
        return {
            "total_fen": _sum(accounts, "closing_fen"),
            "net_change_fen": _sum(accounts, "net_change_fen"),
            **{
                FUND_TYPES[category] + "_fen": _sum(
                    [item for item in accounts if item["type"] == FUND_TYPES[category]],
                    "closing_fen",
                )
                for category in FUND_TYPES
            },
            **{key: movements[key] for key in (
                "inflow_fen", "outflow_fen", "internal_transfer_fen",
            )},
        }


@pytest.mark.parametrize("unrelated_pairs", [4, 104])
def test_amount_equivalence_and_real_retired_bank_work(
    bank_book, unrelated_pairs, record_property,
):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    banking.opening(save, publish, bank="zero-bank")
    banking.funding(save, publish)
    entries = [banking.entry()]
    for index in range(unrelated_pairs):
        entries.extend([
            banking.entry(f"unrelated-in-{index}", amount=1),
            banking.entry(f"unrelated-out-{index}", amount=-1),
        ])
    banking.statement(save, publish, entries)
    narrow_work, narrow = measure_work(engine, lambda: summary(engine))
    old_work, old = measure_work(engine, lambda: legacy_summary(engine))
    assert narrow == {
        "bank_calculation": {"opening_fen": 0, "inflow_fen": 1000, "outflow_fen": 0},
        "total_fen": 1000, "bank_fen": 1000, "cash_fen": 0,
        "payment_platform_fen": 0, "inflow_fen": 1000, "outflow_fen": 0,
        "net_change_fen": 1000, "internal_transfer_fen": 0,
    }
    assert old == {key: narrow[key] for key in AMOUNTS}
    detailed = Dashboard(engine).funds(MONTH)["data"]
    assert {key: narrow[key] for key in AMOUNTS} == {key: detailed[key] for key in AMOUNTS}
    assert detailed["bank_account_count"] == 2  # Independent zero-account source remains.
    assert detailed["bank_statement"]["transaction_count"] == len(entries)
    assert len(detailed["collections"]["statements"]["items"]) == min(20, len(entries))
    for counter in ("sqlite_vm_steps", "returned_rows", "returned_value_bytes"):
        assert narrow_work["counters"][counter] < old_work["counters"][counter]
    for counter in ("typed_fact_json_decodes", "calculation_result_json_decodes"):
        assert narrow_work["counters"][counter] <= old_work["counters"][counter]
    narrow_children = sum(row["calls"] for row in narrow_work["sql"]
                          if "fact_bank_statement_entries" in row["statement"])
    old_children = sum(row["calls"] for row in old_work["sql"]
                       if "fact_bank_statement_entries" in row["statement"])
    assert narrow_children < old_children
    record_property("owner_money_scope_work", json.dumps({
        "original_rows": len(entries), "narrow": narrow_work["counters"],
        "legacy": old_work["counters"],
        "bank_child_sql_calls": {"narrow": narrow_children, "legacy": old_children},
    }, sort_keys=True))


def test_all_money_channels_and_internal_transfer_keep_business_amounts(platform_book):
    engine, save, publish, _ = platform_book
    banking.opening(save, publish)
    banking.funding(save, publish)
    save("cash_funding", "cash-capital", {
        "period": MONTH, "actual_date": MONTH + "-01", "cash_account_id": "cash",
        "owner_id": "owner", "funding_kind": "capital", "amount_fen": 200,
    })
    save("platform_funding", "platform-capital", platforms.funding_data(amount=500))
    save("bank_platform_transfer", "transfer",
         platforms.transfer_data(direction="bank_to_platform", amount=100))
    publish("cash-capital", "platform-capital", "transfer")
    actual = summary(engine)
    assert actual == {
        "bank_calculation": {"opening_fen": 0, "inflow_fen": 1000, "outflow_fen": 100},
        "total_fen": 1700, "bank_fen": 900, "cash_fen": 200,
        "payment_platform_fen": 600, "inflow_fen": 1700, "outflow_fen": 0,
        "net_change_fen": 1700, "internal_transfer_fen": 100,
    }
    detailed = Dashboard(engine).funds(MONTH)["data"]
    assert {key: actual[key] for key in AMOUNTS} == {key: detailed[key] for key in AMOUNTS}
    assert detailed["collections"]["movements"]["page"]["total_count"] == 5


@pytest.mark.parametrize("corruption", ["outcome", "fact_seal", "publication"])
def test_cold_and_previously_warm_summaries_still_reject_money_source_damage(
    bank_book, corruption,
):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    banking.funding(save, publish)
    assert summary(engine)["total_fen"] == 1000
    row = source(engine, "funding")
    if corruption == "outcome":
        damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
               ('{"values":{},' + row["outcome"][1:], row["id"]))
    elif corruption == "fact_seal":
        damage(engine, "fact_seal", "DELETE FROM fact_seal WHERE fact_id=?",
               (row["fact_id"],), foreign_keys=False)
    else:
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE calculation_id=?",
               (row["id"],), foreign_keys=False)
    for _ in range(2):
        with pytest.raises(KernelError) as failure:
            summary(engine)
        assert failure.value.code == "content_integrity_failed"


def test_amount_only_does_not_claim_zero_identity_proof_or_disable_full_checks(bank_book):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish, bank="zero-bank")
    banking.funding(save, publish)
    assert summary(engine)["total_fen"] == 1000
    row = source(engine, "opening-zero-bank")
    damage(engine, "fact_seal", "DELETE FROM fact_seal WHERE fact_id=?",
           (row["fact_id"],), foreign_keys=False)
    # This zero identity has no lines or balances and is outside brief amounts.
    # The full account directory and independent checker still consume it.
    assert summary(engine)["total_fen"] == 1000
    with pytest.raises(KernelError):
        Dashboard(engine).funds(MONTH)
    with engine.store.connection(read_only=True) as connection, pytest.raises(KernelError):
        verify_integrity(engine, connection)


def test_amount_only_avoids_historical_bank_identity_work_and_keeps_full_accounts(
    bank_book, record_property,
):
    engine, _, _ = history(bank_book)
    period = "2026-11"
    with engine.store.connection(read_only=True) as connection:
        bank_ids = {row[0] for row in connection.execute(
            "SELECT id FROM calculation WHERE kind IN "
            "('bank_opening','bank_statement','bank_reconciliation')"
        )}

    def account_amounts(*, amounts_only):
        with Dashboard(engine)._snapshot(period) as snap:
            snap.month_journal.account_amounts()
            read = FundsRead(snap, amounts_only=amounts_only)
            assert read.amounts_only is amounts_only
            movement = read.account_summary()
            if amounts_only:
                assert read.states == {}
                assert not any("statement" in row or "fallback_code" in row
                               for row in read.account_rows.values())
                assert not bank_ids & snap.reads._verified_sql_outcomes
            else:
                assert len(read.states) == 4
                assert len(read.account_rows) == 2
            accounts = [*read.account_rows.values(), *read.omitted_account_rows.values()]
            return {
                "total_fen": _sum(accounts, "closing_fen"),
                "net_change_fen": _sum(accounts, "net_change_fen"),
                **{FUND_TYPES[category] + "_fen": _sum(
                    [row for row in accounts if row["type"] == FUND_TYPES[category]], "closing_fen"
                ) for category in FUND_TYPES},
                **{key: movement[key] for key in (
                    "inflow_fen", "outflow_fen", "internal_transfer_fen"
                )},
            }

    narrow_work, narrow = measure_work(engine, lambda: account_amounts(amounts_only=True))
    complete_work, complete = measure_work(engine, lambda: account_amounts(amounts_only=False))
    assert narrow == complete == {
        "total_fen": 1000, "bank_fen": 1000, "cash_fen": 0, "payment_platform_fen": 0,
        "inflow_fen": 0, "outflow_fen": 0, "net_change_fen": 0, "internal_transfer_fen": 0,
    }
    assert summary(engine, period) == {
        **narrow,
        "bank_calculation": {"opening_fen": 1000, "inflow_fen": 0, "outflow_fen": 0},
    }
    full = Dashboard(engine).funds(period)["data"]
    assert full["bank_account_count"] == 2
    assert full["bank_statement"]["missing_account_count"] == 2
    assert narrow == {key: full[key] for key in AMOUNTS}
    record_property("brief_amount_state_scope_work", json.dumps({
        "narrow": narrow_work["counters"], "complete": complete_work["counters"],
    }, sort_keys=True))
    for counter in ("returned_rows", "returned_value_bytes", "sqlite_vm_steps",
                    "calculation_result_rows_loaded", "calculation_result_json_decodes"):
        assert narrow_work["counters"].get(counter, 0) < complete_work["counters"].get(counter, 0)
    assert not any("close_storage_subroot" in row["statement"] for row in narrow_work["sql"])
    assert any("close_storage_subroot" in row["statement"] for row in complete_work["sql"])


@pytest.mark.parametrize("boundary", ["v1", "unowned", "disabled"])
def test_amount_scope_keeps_original_reader_when_snapshot_guards_are_incomplete(
    bank_book, boundary,
):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    banking.funding(save, publish)
    with historical_content(1 if boundary == "v1" else None), Dashboard(engine)._snapshot(MONTH) as snap:
        if boundary == "unowned":
            snap.reads = QueryReads(engine, snap.connection)
            snap.queries = BusinessQueries(engine, reads=snap.reads)
        elif boundary == "disabled":
            snap.reads._snapshot_active = False
        read = FundsRead(snap, amounts_only=True)
        assert not read.amounts_only
        assert {item["kind"] for item in read.states.values()} == {"bank_opening"}
        read.account_summary()
        assert read.account_rows["bank", "bank-a"]["closing_fen"] == 1000


def test_identity_correction_keeps_full_scope_and_exact_amounts(bank_book):
    engine, save, publish, proof = bank_book
    banking.opening(save, publish)
    banking.opening(save, publish, bank="zero-bank")
    banking.funding(save, publish)
    with engine.store.connection(read_only=True) as connection:
        data = engine.store.current_fact(connection, "funding").fact.model_dump(mode="json")
    command = IdentityCorrections(engine)
    options = dict(changes=[dict(
        subject_id="funding", expected_revision=1, action="reassign",
        data={**data, "bank_account_id": "zero-bank"},
    )], evidence=[proof], reason="explicit synthetic bank identity correction")
    preview = command.preview_identity_correction(**options)
    command.confirm_identity_correction(**options, preview_digest=preview["digest"],
                                        epochs=preview["epochs"], request_id="correct-bank")
    with Dashboard(engine)._snapshot(MONTH) as snap:
        read = FundsRead(snap, amounts_only=True)
        assert not read.amounts_only
        assert len(read.states) == 2
    actual = summary(engine)
    assert actual["bank_fen"] == actual["total_fen"] == actual["inflow_fen"] == 1000
    assert actual["bank_calculation"] == {
        "opening_fen": 0, "inflow_fen": 1000, "outflow_fen": 0,
    }
    detailed = Dashboard(engine).funds(MONTH)["data"]
    assert {key: actual[key] for key in AMOUNTS} == {key: detailed[key] for key in AMOUNTS}


def test_amount_scope_retains_open_replacement_review_and_withdrawal(bank_book, monkeypatch):
    from ai_accounting.kernel import engine as engine_module

    engine, save, publish, proof = bank_book
    banking.opening(save, publish, bank="zero-bank")
    data = dict(period=MONTH, actual_date=MONTH + "-01", cash_account_id="cash",
                owner_id="owner", funding_kind="capital", amount_fen=100)
    save("cash_funding", "cash-money", data)
    publish("cash-money")
    assert summary(engine)["cash_fen"] == 100
    engine.amend_fact("cash_funding", "cash-money", data | {"amount_fen": 70},
                      evidence=(proof,), expected_revision=1, recording_error_confirmed=True,
                      request_id="replace-cash")
    publish("cash-money")
    with monkeypatch.context() as changed_program:
        changed_program.setattr(engine_module, "PROGRAM_VERSION", "synthetic-amount-review")
        assert publish("cash-money")["results"][0]["impact"] == "review_no_impact"
    actual = summary(engine)
    assert actual["cash_fen"] == actual["inflow_fen"] == 70
    assert actual["bank_calculation"] == {
        "opening_fen": 0, "inflow_fen": 0, "outflow_fen": 0,
    }
    detailed = Dashboard(engine).funds(MONTH)["data"]
    assert {key: actual[key] for key in AMOUNTS} == {key: detailed[key] for key in AMOUNTS}
    preview = engine.preview_delete("cash-money", recording_error_evidence=proof)
    engine.delete("cash-money", preview_digest=preview["digest"], epochs=preview["epochs"],
                  recording_error_evidence=proof, request_id="withdraw-cash")
    actual = summary(engine)
    assert actual == {
        **{key: 0 for key in AMOUNTS},
        "bank_calculation": {"opening_fen": 0, "inflow_fen": 0, "outflow_fen": 0},
    }
    detailed = Dashboard(engine).funds(MONTH)["data"]
    assert {key: actual[key] for key in AMOUNTS} == {key: detailed[key] for key in AMOUNTS}


def test_uncertain_opening_retains_none_after_actual_source_verification(
    opening_book, monkeypatch,
):
    engine, _, _, package, _ = opening_book
    package([
        ("opening_cash", "cash", {"cash_account_id": "cash", "balance_fen": 100}),
        ("opening_equity", "equity", {
            "equity_kind": "paid_in_capital", "balance_fen": 100,
            "holder_or_basis_id": "owner",
        }),
    ])
    original = BusinessQueries._selected_accounting

    def uncertain(*args, **kwargs):
        # Exercise a legal downstream uncertainty using a genuinely proven source;
        # this is not the SQL/work comparison and does not invent an opening body.
        selected = deepcopy(original(*args, **kwargs))
        through = selected["through_period"]
        candidate = next(item for item in through["state_results"]
                         if item["kind"] == "opening_package")
        through["state_results"].remove(candidate)
        through["unestablished_state_selections"].append({
            "reason": "state_selection_unavailable", "candidates": [candidate],
        })
        return selected

    monkeypatch.setattr(BusinessQueries, "_selected_accounting", uncertain)
    actual = summary(engine, "2026-01")
    assert set(actual) == AMOUNTS | {"bank_calculation"}
    assert actual["total_fen"] is actual["cash_fen"] is None
    assert actual["bank_fen"] == actual["payment_platform_fen"] == 0
    assert actual["bank_calculation"] == {
        "opening_fen": 0, "inflow_fen": 0, "outflow_fen": 0,
    }
    with Dashboard(engine)._snapshot("2026-01") as snap:
        full = funds(snap, sections=set())
    assert {key: actual[key] for key in AMOUNTS} == {key: full[key] for key in AMOUNTS}
    with monkeypatch.context() as patch:
        patch.setattr(FundsRead, "_verified_money_summary", lambda self: None)
        with Dashboard(engine)._snapshot("2026-01") as snap:
            assert funds(snap, sections=set()) == full


def test_retired_matching_display_does_not_disable_core_match_checks(bank_book):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    banking.funding(save, publish)
    banking.statement(save, publish, [banking.entry(amount=999)])
    assert summary(engine)["inflow_fen"] == 1000
    detailed = Dashboard(engine).funds(MONTH)["data"]
    assert detailed["bank_statement"]["inflow_fen"] == 999
    assert detailed["bank_statement"]["review_state"] == "pending"
    with pytest.raises(KernelError) as failure:
        banking.reconciliation(save, publish, [banking.match()])
    assert failure.value.code == "bank_match_difference"
