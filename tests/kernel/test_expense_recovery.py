"""Expense returns retain their source and never manufacture or rewrite cash."""

import json

import pytest
from test_deletion_boundaries import book as book
from test_deletion_boundaries import expense

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.types import YearMonth


def recovery(**changes):
    return {
        "period": "2026-02",
        "source_expense_id": "cost",
        "counterparty_id": "supplier",
        "amount_fen": 40,
        "recovery_right_confirmed": True,
        **changes,
    }


def balance(engine, key):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT amount FROM balance WHERE balance_key=?", (key,)
        ).fetchone()
        return row[0] if row else 0


def test_closed_cost_return_is_open_period_reduction_and_separate_actual_receipt(book):
    engine, save, publish, close, *_ = book
    save("expense", "cost", expense())
    publish("cost")
    frozen = close("2026-01")
    save("expense_recovery", "refund", recovery())
    publish("refund")
    assert balance(engine, "expense_recovery:refund:primary") == 40
    assert engine.overview("2026-02")["cashflow"] == []
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            "SELECT manifest FROM period_close WHERE period=?", (YearMonth("2026-01").ordinal,)
        ).fetchone()[0]
    receipt = {
        "period": "2026-02",
        "actual_date": "2026-02-07",
        "direction": "inflow",
        "bank_account_id": "bank",
        "counterparty_id": "supplier",
        "amount_fen": 30,
        "allocations": [
            {
                "source_kind": "expense_recovery",
                "source_id": "refund",
                "obligation": "primary",
                "amount_fen": 30,
            }
        ],
    }
    save("payment", "receipt", receipt)
    publish("receipt")
    assert balance(engine, "expense_recovery:refund:primary") == 10
    assert balance(engine, "bank") == 30
    assert engine.overview("2026-02")["cashflow"] == [
        {"category": "other_operating_receipts", "amount": 30}
    ]
    save("expense_recovery", "refund", recovery(amount_fen=50), revision=1)
    publish("refund")
    assert balance(engine, "expense_recovery:refund:primary") == 20
    assert balance(engine, "bank") == 30
    with pytest.raises(KernelError) as error:
        save("payment", "receipt", {**receipt, "actual_date": "2026-02-08"}, revision=1)
    assert error.value.code == "immutable_fact"
    engine.rebuild_projections(request_id="rebuild")
    assert balance(engine, "expense_recovery:refund:primary") == 20
    assert balance(engine, "expense:cost:primary") == 100
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT manifest FROM period_close WHERE period=?", (YearMonth("2026-01").ordinal,)
            ).fetchone()[0]
            == original
        )
        refund_outcome = json.loads(
            connection.execute(
                "SELECT c.outcome FROM calculation c JOIN calculation_current a "
                "ON a.calculation_id=c.id WHERE a.subject_id='refund'"
            ).fetchone()[0]
        )
    assert refund_outcome["lines"] == [
        {"account": "1221", "debit": 50, "credit": 0, "cashflow": None},
        {"account": "5602", "debit": 0, "credit": 50, "cashflow": None},
    ]
    assert frozen["status"] == "closed"


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"recovery_right_confirmed": None}, "needs_information"),
        ({"recovery_right_confirmed": False}, "needs_information"),
        ({"period": "2025-12"}, "expense_recovery_period"),
        ({"amount_fen": 101}, "excess_expense_recovery"),
    ],
)
def test_unsupported_return_rejects_without_publishing(book, changes, code):
    engine, save, publish, *_ = book
    save("expense", "cost", expense())
    publish("cost")
    save("expense_recovery", "refund", recovery(**changes))
    with pytest.raises(KernelError) as error:
        publish("refund")
    assert error.value.code == code
    assert balance(engine, "expense_recovery:refund:primary") == 0


def test_cumulative_returns_cannot_exceed_current_source_and_failed_correction_is_atomic(book):
    engine, save, publish, *_ = book
    save("expense", "cost", expense())
    publish("cost")
    save("expense_recovery", "first", recovery(amount_fen=60))
    publish("first")
    save("expense_recovery", "second", recovery(period="2026-03", amount_fen=41))
    with pytest.raises(KernelError) as error:
        publish("second")
    assert error.value.code == "excess_expense_recovery"
    save("expense_recovery", "second", recovery(period="2026-03"), revision=1)
    publish("second")
    save("expense", "cost", expense(amount=99), revision=1)
    with pytest.raises(KernelError) as error:
        publish("cost")
    assert error.value.code == "excess_expense_recovery"
    assert balance(engine, "expense:cost:primary") == 100
    assert balance(engine, "expense_recovery:first:primary") == 60
    assert balance(engine, "expense_recovery:second:primary") == 40


def test_source_must_be_published_and_return_must_have_retained_evidence(book):
    engine, save, publish, *_ = book
    save("expense", "cost", expense())
    save("expense_recovery", "refund", recovery())
    with pytest.raises(NeedsInformation):
        publish("refund")
    publish("cost", "refund")
    with pytest.raises(NeedsInformation):
        engine.save_fact(
            "expense_recovery",
            "no-proof",
            recovery(amount_fen=1),
            evidence=(),
            expected_revision=0,
            request_id="no-proof",
        )


def test_nonemployee_reimbursement_retains_individual_identity_and_is_not_payroll(book):
    engine, save, publish, *_ = book
    save("expense", "training", {**expense(), "creditor_kind": "individual"})
    preview = engine.preview(["training"])
    assert list(preview["results"][0]["lines"]) == [
        {"account": "5602", "debit": 100, "credit": 0, "cashflow": None},
        {"account": "2241", "debit": 0, "credit": 100, "cashflow": None},
    ]
    publish("training")
    save(
        "payment",
        "reimburse",
        {
            "period": "2026-01",
            "actual_date": "2026-01-10",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "supplier",
            "amount_fen": 100,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "training",
                    "obligation": "primary",
                    "amount_fen": 100,
                }
            ],
        },
    )
    publish("reimburse")
    assert balance(engine, "expense:training:primary") == 0
    assert balance(engine, "bank") == -100


@pytest.mark.parametrize(
    "nature,account",
    [
        ("tax_late_fee", "571103"),
        ("social_contribution_late_fee", "571104"),
    ],
)
def test_surcharges_retain_distinct_nature_and_reverse_original_classification(
    book, nature, account
):
    engine, save, publish, *_ = book
    save("expense", "cost", {**expense(), "expense_class": nature})
    save("expense_recovery", "refund", recovery())
    preview = engine.preview(["cost", "refund"])
    published = {r["subject_id"]: r for r in preview["results"]}
    assert published["cost"]["values"]["expense_class"] == nature
    assert published["cost"]["lines"][0]["account"] == account
    assert published["refund"]["lines"][1]["account"] == account
    publish("cost", "refund")
    assert balance(engine, "expense_recovery:refund:primary") == 40
