"""Original platform rows have one typed disposition, regardless of payment medium."""

import json
import sqlite3

import pytest
from pydantic import ValidationError
from test_deletion_boundaries import book as book

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import Context, KernelError, NeedsInformation, Read
from ai_accounting.kernel.domains import platforms
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.types import YearMonth


def movement(
    proof,
    *,
    amount=1870000,
    direction="outflow",
    day="2026-01-02",
    location="row8",
    reference="txn8",
):
    return {
        "period": day[:7],
        "actual_date": day,
        "platform_account_id": "platform",
        "direction": direction,
        "amount_fen": amount,
        "source_evidence_digest": proof,
        "source_location": location,
        "transaction_reference": reference,
    }


def cost(**changes):
    return {
        "period": "2026-01",
        "platform_account_id": "platform",
        "expense_class": "administration",
        "outgoing_movement_ids": ["out"],
        "returned_movement_ids": ["returned"],
        "confirmed_amount_fen": 1855000,
        "treatment_confirmed": True,
        **changes,
    }


def group(book):
    _, save, _, _, _, proof = book
    save("platform_movement", "out", movement(proof))
    save(
        "platform_movement",
        "returned",
        movement(
            proof,
            amount=15000,
            direction="inflow",
            day="2026-01-03",
            location="row9",
            reference="txn9",
        ),
    )
    return save("platform_expense_confirmation", "cost", cost())


def calc(engine, kind, subject):
    with engine.store.connection(read_only=True) as conn:
        return engine.store.select(conn, Read("calculation", kind, "@" + subject))[0]


def readiness(engine, period="2026-01"):
    with engine.store.connection(read_only=True) as conn:
        month = YearMonth(period)
        context = Context(
            {read: engine.store.select(conn, read) for read in platforms.required_reads(month)}
        )
        return platforms.required_work(month, context)


def recovery(**changes):
    return {
        "period": "2026-02",
        "source_expense_id": "cost",
        "counterparty_id": "confirmed-return-party",
        "amount_fen": 10000,
        "recovery_right_confirmed": True,
        **changes,
    }


def test_net_group_has_no_payable_and_retains_both_actual_dates(book):
    engine, _, publish, *_ = book
    group(book)
    publish("out", "returned", "cost")
    result = calc(engine, "platform_expense_confirmation", "cost")
    assert result.values["gross_outflow_fen"] == 1870000
    assert result.values["returned_fen"] == 15000
    assert result.values["expense_fen"] == 1855000
    assert result.values["obligations"] == ()
    assert calc(engine, "platform_movement", "out").values["actual_date"] == "2026-01-02"
    assert calc(engine, "platform_movement", "returned").values["actual_date"] == "2026-01-03"
    assert engine.overview("2026-01")["cashflow"] == [
        {"category": "operating_payments", "amount": -1855000}
    ]
    assert not readiness(engine)
    report = Reports(engine).report(2026, 1)
    assert report["statements"]["cash_flow_statement"]["6"]["current_fen"] == 1855000
    with engine.store.connection(read_only=True) as conn:
        assert [
            tuple(r) for r in conn.execute("SELECT balance_key,amount,category FROM balance")
        ] == [("platform", -1855000, "platform")]


@pytest.mark.parametrize("duplicate", ["expense", "payment", "transfer"])
def test_same_outgoing_row_cannot_fund_two_business_dispositions(book, duplicate):
    engine, save, publish, *_ = book
    group(book)
    publish("out", "returned", "cost")
    if duplicate == "expense":
        save(
            "platform_expense_confirmation",
            "second",
            cost(returned_movement_ids=[], confirmed_amount_fen=1870000),
        )
    elif duplicate == "transfer":
        save(
            "bank_platform_transfer",
            "second",
            {
                "period": "2026-01",
                "actual_date": "2026-01-02",
                "direction": "platform_to_bank",
                "bank_account_id": "bank",
                "platform_account_id": "platform",
                "amount_fen": 1870000,
                "movement_ids": ["out"],
            },
        )
    else:
        save(
            "refundable_deposit",
            "subscription",
            {
                "period": "2026-01",
                "counterparty_id": "fund",
                "amount_fen": 1870000,
                "refund_right_confirmed": True,
            },
        )
        publish("subscription")
        save(
            "platform_payment",
            "second",
            {
                "period": "2026-01",
                "actual_date": "2026-01-02",
                "direction": "outflow",
                "platform_account_id": "platform",
                "counterparty_id": "fund",
                "amount_fen": 1870000,
                "movement_ids": ["out"],
                "allocations": [
                    {
                        "source_kind": "refundable_deposit",
                        "source_id": "subscription",
                        "obligation": "deposit",
                        "amount_fen": 1870000,
                    }
                ],
            },
        )
    with pytest.raises(KernelError) as error:
        publish("second")
    assert error.value.code == "platform_movement_consumed"
    assert calc(engine, "platform_expense_confirmation", "cost").values["expense_fen"] == 1855000


@pytest.mark.parametrize("identity", ["location", "reference"])
def test_duplicate_source_identity_is_rejected_with_current_empty_scope_tracking(book, identity):
    engine, save, publish, _, _, proof = book
    save("platform_movement", "original", movement(proof))
    publish("original")
    duplicate = (
        movement(proof, reference="other")
        if identity == "location"
        else movement(proof, location="other")
    )
    save("platform_movement", "duplicate", duplicate)
    with pytest.raises(KernelError) as error:
        publish("duplicate")
    assert error.value.code == "duplicate_platform_movement"
    assert calc(engine, "platform_movement", "original").values["amount_fen"] == 1870000


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"confirmed_amount_fen": 1870000}, "platform_expense_net"),
        ({"period": "2026-02"}, "platform_movement_period"),
        ({"platform_account_id": "other"}, "platform_movement_mismatch"),
        (
            {"outgoing_movement_ids": ["returned"], "returned_movement_ids": []},
            "platform_movement_mismatch",
        ),
        ({"treatment_confirmed": None}, "needs_information"),
        ({"outgoing_movement_ids": ["missing"]}, "needs_information"),
    ],
)
def test_expense_group_rejects_missing_wrong_or_unsupported_sources(book, changes, code):
    _, save, publish, *_ = book
    group(book)
    save("platform_expense_confirmation", "cost", cost(**changes), revision=1)
    with pytest.raises(KernelError) as error:
        publish("out", "returned", "cost")
    assert error.value.code == code


def test_source_only_or_unpublished_disposition_is_not_month_complete(book):
    engine, save, publish, *_ = book
    group(book)
    assert len(readiness(engine)) == 2
    publish("out", "returned")
    assert len(readiness(engine)) == 2
    publish("cost")
    assert readiness(engine) == []
    save("platform_expense_confirmation", "second", cost())
    assert len(readiness(engine)) == 2


def test_public_schema_requires_raw_movement_and_rejects_duplicate_group(book):
    engine, _, _, _, _, proof = book
    wire = command_models(engine.store.registry)
    data = {
        "period": "2026-01",
        "actual_date": "2026-01-02",
        "funding_kind": "capital",
        "owner_id": "owner",
        "amount_fen": 100,
        "platform_account_id": "platform",
    }

    def check(kind, data):
        return validate_command(
            wire,
            "save_fact",
            {
                "company_id": "company",
                "kind": kind,
                "subject_id": "new",
                "data": data,
                "evidence": [proof],
                "expected_revision": 0,
                "request_id": "wire",
            },
        )

    with pytest.raises((ValidationError, KernelError)):
        check("platform_funding", data)
    with pytest.raises((ValidationError, KernelError)):
        check("platform_expense_confirmation", cost(outgoing_movement_ids=["out", "out"]))
    with pytest.raises((ValidationError, KernelError)):
        check("platform_movement", movement(proof, day="2026-01"))
    check("platform_movement", movement(proof))


def test_correction_preserves_original_money_and_does_not_silently_change_net_cost(book):
    engine, save, publish, _, _, proof = book
    group(book)
    publish("out", "returned", "cost")
    original = calc(engine, "platform_movement", "out")
    with pytest.raises(KernelError) as error:
        save("platform_movement", "out", movement(proof, amount=1870001), revision=1)
    assert error.value.code == "immutable_fact"
    save("platform_movement", "out", movement(proof, amount=1870001), revision=1, amend=True)
    with pytest.raises(KernelError) as error:
        publish("out")
    assert error.value.code == "platform_expense_net"
    assert calc(engine, "platform_movement", "out").id == original.id
    save("platform_expense_confirmation", "cost", cost(confirmed_amount_fen=1855001), revision=1)
    result = publish("out")
    assert {item["subject_id"] for item in result["results"]} == {"out", "cost"}
    assert calc(engine, "platform_expense_confirmation", "cost").values["expense_fen"] == 1855001
    with engine.store.connection(read_only=True) as conn:
        assert engine.store.fact(conn, original.fact_id).fact.amount_fen == 1870000
    with engine.store.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable fact_platform_movement"):
            conn.execute(
                "UPDATE fact_platform_movement SET amount_fen=1 WHERE revision_id=?",
                (original.fact_id,),
            )


def test_original_evidence_change_propagates_but_never_rewrites_actual_date(book):
    engine, save, publish, _, _, proof = book
    group(book)
    publish("out", "returned", "cost")
    replacement = engine.register_evidence(
        b"corrected synthetic platform original",
        "text/plain",
        "corrected",
        request_id="replacement",
    )["digest"]
    engine.amend_fact(
        "platform_movement",
        "out",
        movement(replacement),
        evidence=[replacement],
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="amend-original",
    )
    plan = publish("out")
    assert {r["subject_id"] for r in plan["results"]} == {"out", "cost"}
    assert calc(engine, "platform_movement", "out").values["actual_date"] == "2026-01-02"
    assert calc(engine, "platform_expense_confirmation", "cost").values["expense_fen"] == 1855000
    with pytest.raises(NeedsInformation):
        save(
            "platform_movement",
            "unbacked",
            movement(replacement, location="missing", reference="missing"),
        )
        publish("unbacked")


def test_closed_expense_and_actual_rows_stay_frozen_while_later_receipt_reduces_only_cost(book):
    engine, save, publish, close, _, proof = book
    group(book)
    publish("out", "returned", "cost")
    close("2026-01")
    frozen = Periods(engine).closed_report("2026-01")
    save("expense_recovery", "return", recovery())
    publish("return")
    save(
        "payment",
        "receipt",
        {
            "period": "2026-02",
            "actual_date": "2026-02-05",
            "direction": "inflow",
            "bank_account_id": "bank",
            "counterparty_id": "confirmed-return-party",
            "amount_fen": 10000,
            "allocations": [
                {
                    "source_kind": "expense_recovery",
                    "source_id": "return",
                    "obligation": "primary",
                    "amount_fen": 10000,
                }
            ],
        },
    )
    publish("receipt")
    assert Periods(engine).closed_report("2026-01") == frozen
    assert engine.overview("2026-02")["cashflow"] == [
        {"category": "other_operating_receipts", "amount": 10000}
    ]
    save("platform_movement", "out", movement(proof, amount=1870001), revision=1, amend=True)
    save("platform_expense_confirmation", "cost", cost(confirmed_amount_fen=1855001), revision=1)
    with pytest.raises(KernelError):
        publish("out")
    publish("out", correction_period="2026-02")
    assert Periods(engine).closed_report("2026-01") == frozen
    assert calc(engine, "payment", "receipt").values["amount_fen"] == 10000
    with engine.store.connection(read_only=True) as conn:
        assert json.loads(conn.execute("SELECT manifest FROM period_close").fetchone()[0])


def test_return_capacity_has_fixed_original_cost_and_rejects_earlier_period(book):
    engine, save, publish, *_ = book
    group(book)
    publish("out", "returned", "cost")
    save("expense_recovery", "one", recovery(amount_fen=1855000))
    publish("one")
    save("expense_recovery", "excess", recovery(period="2026-03", amount_fen=1))
    with pytest.raises(KernelError) as error:
        publish("excess")
    assert error.value.code == "excess_expense_recovery"
    save("expense_recovery", "before", recovery(period="2025-12", amount_fen=1))
    with pytest.raises(KernelError) as error:
        publish("before")
    assert error.value.code == "expense_recovery_period"
    assert calc(engine, "expense_recovery", "one").values["amount_fen"] == 1855000


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"actual_date": "2026-01-03"}, "platform_movement_date"),
        ({"amount_fen": 999}, "platform_movement_amount"),
        ({"platform_account_id": "other"}, "platform_movement_mismatch"),
    ],
)
def test_funding_requires_matching_actual_raw_money(book, changes, code):
    _, save, publish, _, _, proof = book
    save("platform_movement", "raw", movement(proof, amount=1000, direction="inflow"))
    publish("raw")
    save(
        "platform_funding",
        "capital",
        {
            "period": "2026-01",
            "actual_date": "2026-01-02",
            "funding_kind": "capital",
            "owner_id": "owner",
            "platform_account_id": "platform",
            "amount_fen": 1000,
            "movement_ids": ["raw"],
            **changes,
        },
    )
    with pytest.raises(KernelError) as error:
        publish("capital")
    assert error.value.code == code
