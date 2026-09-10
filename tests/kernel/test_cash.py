"""Real cash and bank facts share allocation capacity without sharing accounts."""

import itertools

import pytest
from test_payroll import labor, labor_policy

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


@pytest.fixture
def book(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite",
            default_registry(),
            "company",
            "911100000000000001",
            "database",
        )
    )
    proof = engine.register_evidence(
        b"Synthetic actual cash evidence", "text/plain", "proof", request_id="evidence"
    )["digest"]
    requests = itertools.count()

    def save(kind, subject, data, revision=0):
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=revision,
            request_id="save-" + str(next(requests)),
        )

    def publish(*subjects):
        preview = engine.preview(list(subjects))
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="publish-" + str(next(requests)),
        )

    return engine, save, publish, proof


def expense(save, amount=300):
    save(
        "expense",
        "expense",
        {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": amount,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )


def test_personal_advance_is_actual_money_and_recording_correction_is_explicit(book):
    engine, save, publish, proof = book
    expense(save)
    publish("expense")
    data = {
        "period": "2026-09",
        "payer_id": "owner",
        "payer_kind": "owner",
        "payment_on_behalf_confirmed": True,
        "actual_creditor_payment_date": "2026-09-10",
        "sources": [
            {
                "source_kind": "expense",
                "source_id": "expense",
                "obligation": "primary",
                "amount_fen": 200,
            }
        ],
    }
    original = save("employee_advance", "personal-payment", data)
    first = publish("personal-payment")["results"][0]
    for replacement in (
        {**data, "actual_creditor_payment_date": "2026-09-11"},
        {**data, "sources": [{**data["sources"][0], "amount_fen": 250}]},
    ):
        with pytest.raises(KernelError) as error:
            save("employee_advance", "personal-payment", replacement, revision=1)
        assert error.value.code == "immutable_fact"
    with pytest.raises(KernelError) as error:
        engine.preview_delete("personal-payment")
    assert error.value.code == "immutable_fact"
    corrected = {**data, "actual_creditor_payment_date": "2026-09-11"}
    engine.amend_fact(
        "employee_advance",
        "personal-payment",
        corrected,
        evidence=(proof,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="fix-recording",
    )
    second = publish("personal-payment")["results"][0]
    assert first["voucher_number"] == second["voucher_number"]
    with engine.store.connection(read_only=True) as connection:
        before = engine.store.fact(connection, original["fact_id"])
        after = engine.store.current_fact(connection, "personal-payment")
        assert str(before.fact.actual_creditor_payment_date) == "2026-09-10"
        assert str(after.fact.actual_creditor_payment_date) == "2026-09-11"
        assert before.id != after.id


def payment(
    save,
    kind="cash_payment",
    amount=300,
    *,
    source_kind="expense",
    source_id="expense",
    subject="cash-payment",
    party="supplier",
    obligation="primary",
):
    account = (
        {"cash_account_id": "cashbox"} if kind == "cash_payment" else {"bank_account_id": "bank"}
    )
    save(
        kind,
        subject,
        {
            "period": "2026-09",
            "actual_date": "2026-09-10",
            "direction": "outflow",
            **account,
            "counterparty_id": party,
            "amount_fen": amount,
            "allocations": [
                {
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "obligation": obligation,
                    "amount_fen": amount,
                }
            ],
        },
    )


def fund(save, kind="cash_funding", amount=1000):
    account = (
        {"cash_account_id": "cashbox"} if kind == "cash_funding" else {"bank_account_id": "bank"}
    )
    save(
        kind,
        "funding",
        {
            "period": "2026-09",
            "actual_date": "2026-09-01",
            "funding_kind": "capital",
            "owner_id": "owner",
            "amount_fen": amount,
            **account,
        },
    )


def test_cash_payments_use_inventory_cash_and_need_no_bank_statement(book):
    engine, save, publish, proof = book
    fund(save)
    expense(save)
    publish("funding", "expense")
    payment(save)
    publish("cash-payment")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM voucher_line WHERE account='1002'").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE category='cash' AND balance_key='cashbox'"
            ).fetchone()[0]
            == 700
        )
        assert (
            connection.execute(
                "SELECT coalesce(sum(amount),0) FROM balance "
                "WHERE balance_key='expense:expense:primary'"
            ).fetchone()[0]
            == 0
        )
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        active = category == "transactions"
        periods.inventory(
            "2026-09",
            category,
            evidence=[proof] if active else [],
            expected=int(active),
            no_business=not active,
            confirmation_evidence=proof,
            request_id="inventory-" + category,
        )
    preview = periods.preview_close("2026-09", owner_confirmation=proof)
    assert (
        periods.close(
            "2026-09",
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="close",
        )["status"]
        == "closed"
    )


def test_bank_and_cash_share_capacity_and_require_explicit_overpayment_recovery(book):
    engine, save, publish, _ = book
    expense(save, 1000)
    publish("expense")
    payment(save, "payment", 600, subject="bank-payment")
    publish("bank-payment")
    payment(save, amount=500)
    with pytest.raises(NeedsInformation) as failure:
        publish("cash-payment")
    assert failure.value.issues[0]["field"] == "overpayment"
    save(
        "overpayment",
        "recovery",
        {
            "period": "2026-09",
            "source_kind": "expense",
            "source_id": "expense",
            "obligation_name": "primary",
            "counterparty_id": "supplier",
            "amount_fen": 100,
            "recovery_right_confirmed": True,
        },
    )
    publish("bank-payment", "cash-payment", "recovery")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT coalesce(sum(amount),0) FROM balance "
                "WHERE balance_key='expense:expense:primary'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE balance_key='overpayment:recovery:primary'"
            ).fetchone()[0]
            == 100
        )


def test_deposit_advanced_by_employee_remains_receivable_when_cash_reimburses_employee(book):
    engine, save, publish, _ = book
    fund(save)
    save(
        "refundable_deposit",
        "deposit",
        {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": 1000,
            "refund_right_confirmed": True,
        },
    )
    publish("funding", "deposit")
    save(
        "employee_advance",
        "advance",
        {
            "period": "2026-09",
            "payer_id": "alice",
            "payer_kind": "employee",
            "payment_on_behalf_confirmed": True,
            "actual_creditor_payment_date": "2026-09-03",
            "sources": [
                {
                    "source_kind": "refundable_deposit",
                    "source_id": "deposit",
                    "obligation": "payment",
                    "amount_fen": 1000,
                }
            ],
        },
    )
    publish("advance")
    payment(save, amount=1000, source_kind="employee_advance", source_id="advance", party="alice")
    publish("cash-payment")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE balance_key='refundable_deposit:deposit:refund'"
            ).fetchone()[0]
            == 1000
        )
        assert (
            connection.execute(
                "SELECT coalesce(sum(amount),0) FROM balance "
                "WHERE balance_key='employee_advance:advance:primary'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM voucher_line WHERE account LIKE '5%'"
            ).fetchone()[0]
            == 0
        )
    # A second direct payment cannot spend the same supplier obligation again.
    payment(
        save,
        amount=1,
        source_kind="refundable_deposit",
        source_id="deposit",
        subject="duplicate",
        obligation="payment",
    )
    with pytest.raises(KernelError) as failure:
        publish("duplicate")
    assert failure.value.code == "overallocated_obligation"


def test_bank_reconciliation_matches_only_bank_sides_of_cash_withdrawals_and_deposits(book):
    engine, save, publish, _ = book
    fund(save, "funding")
    expense(save, 50)
    save(
        "bank_opening",
        "opening",
        {"period": "2026-09", "bank_account_id": "bank", "opening_fen": 0, "basis": "new_account"},
    )
    publish("funding", "expense", "opening")
    for subject, direction, amount, day in (
        ("withdrawal", "withdrawal", 100, "2026-09-02"),
        ("deposit", "deposit", 20, "2026-09-11"),
    ):
        save(
            "cash_bank_transfer",
            subject,
            {
                "period": "2026-09",
                "actual_date": day,
                "direction": direction,
                "bank_account_id": "bank",
                "cash_account_id": "cashbox",
                "amount_fen": amount,
            },
        )
    payment(save, amount=50)
    publish("withdrawal", "deposit", "cash-payment")
    save(
        "bank_statement",
        "statement",
        {
            "period": "2026-09",
            "bank_account_id": "bank",
            "opening_fen": 0,
            "closing_fen": 920,
            "entries": [
                {"reference": "funding", "actual_date": "2026-09-01", "signed_fen": 1000},
                {"reference": "withdrawal", "actual_date": "2026-09-02", "signed_fen": -100},
                {"reference": "deposit", "actual_date": "2026-09-11", "signed_fen": 20},
            ],
        },
    )
    publish("statement")
    save(
        "bank_reconciliation",
        "reconciliation",
        {
            "period": "2026-09",
            "bank_account_id": "bank",
            "statement_id": "statement",
            "matches": [
                {"reference": subject, "source_kind": kind, "source_id": subject}
                for subject, kind in (
                    ("funding", "funding"),
                    ("withdrawal", "cash_bank_transfer"),
                    ("deposit", "cash_bank_transfer"),
                )
            ],
        },
    )
    publish("reconciliation")
    with engine.store.connection(read_only=True) as connection:
        assert dict(
            connection.execute(
                "SELECT category,amount FROM balance WHERE category IN ('bank','cash')"
            )
        ) == {"bank": 920, "cash": 30}


def test_historical_gross_labor_cash_payment_and_accrual_publish_together(book):
    engine, save, publish, _ = book
    fund(save, amount=1_000_000)
    save("labor_income_tax_policy", "labor-policy", labor_policy().model_dump(mode="json"))
    save(
        "labor",
        "labor",
        labor(
            period="2026-09",
            income_date="2026-09-10",
            withholding_method="gross_paid_without_withholding",
            gross_payment_kind="cash_payment",
            gross_payment_id="cash-payment",
        ).model_dump(mode="json"),
    )
    payment(
        save,
        amount=1_000_000,
        source_kind="labor",
        source_id="labor",
        party="contractor",
        obligation="net",
    )
    assert publish("funding", "labor", "cash-payment")["status"] == "published"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM voucher_line WHERE account='222103'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT coalesce(sum(amount),0) FROM balance WHERE category='cash'"
            ).fetchone()[0]
            == 0
        )


def test_owner_may_advance_existing_payroll_tax_without_becoming_an_employee(book):
    engine, save, publish, _ = book
    save("labor_income_tax_policy", "labor-policy", labor_policy().model_dump(mode="json"))
    save(
        "labor", "labor", labor(period="2026-09", income_date="2026-09-10").model_dump(mode="json")
    )
    publish("labor")
    save(
        "employee_advance",
        "owner-tax",
        {
            "period": "2026-09",
            "payer_id": "owner",
            "payer_kind": "owner",
            "payment_on_behalf_confirmed": True,
            "actual_creditor_payment_date": "2026-09-20",
            "sources": [
                {
                    "source_kind": "labor",
                    "source_id": "labor",
                    "obligation": "tax",
                    "amount_fen": 160000,
                }
            ],
        },
    )
    publish("owner-tax")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT coalesce(sum(amount),0) FROM balance WHERE balance_key='labor:labor:tax'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE balance_key='employee_advance:owner-tax:primary'"
            ).fetchone()[0]
            == 160000
        )


def close_transactions(engine, proof, period):
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        active = category == "transactions"
        periods.inventory(
            period,
            category,
            evidence=[proof] if active else [],
            expected=int(active),
            no_business=not active,
            confirmation_evidence=proof,
            request_id=f"close-inventory-{period}-{category}",
        )
    preview = periods.preview_close(period, owner_confirmation=proof)
    periods.close(
        period,
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="close-" + period,
    )
    return periods.closed_report(period)


def test_future_settlement_does_not_invalidate_a_closed_personal_advance(book):
    engine, save, publish, proof = book
    expense(save, 1000)
    publish("expense")
    save(
        "employee_advance",
        "advance",
        {
            "period": "2026-09",
            "payer_id": "owner",
            "payer_kind": "owner",
            "payment_on_behalf_confirmed": True,
            "actual_creditor_payment_date": "2026-09-10",
            "sources": [
                {
                    "source_kind": "expense",
                    "source_id": "expense",
                    "obligation": "primary",
                    "amount_fen": 600,
                }
            ],
        },
    )
    publish("advance")
    frozen = close_transactions(engine, proof, "2026-09")
    save(
        "cash_payment",
        "later-payment",
        {
            "period": "2026-10",
            "actual_date": "2026-10-01",
            "direction": "outflow",
            "cash_account_id": "cashbox",
            "counterparty_id": "supplier",
            "amount_fen": 400,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "expense",
                    "obligation": "primary",
                    "amount_fen": 400,
                }
            ],
        },
    )
    publish("later-payment")
    assert engine.overview("2026-09")["pending"] == []
    assert Periods(engine).closed_report("2026-09") == frozen


def test_future_partial_pass_through_return_does_not_reopen_closed_return(book):
    engine, save, publish, proof = book
    save(
        "pass_through",
        "agency",
        {
            "period": "2026-09",
            "payer_id": "payer",
            "beneficiary_id": "beneficiary",
            "amount_fen": 1000,
            "rights_and_obligation_confirmed": True,
        },
    )
    publish("agency")
    save(
        "cash_payment",
        "collection",
        {
            "period": "2026-09",
            "actual_date": "2026-09-01",
            "direction": "inflow",
            "cash_account_id": "cashbox",
            "counterparty_id": "payer",
            "amount_fen": 1000,
            "allocations": [
                {
                    "source_kind": "pass_through",
                    "source_id": "agency",
                    "obligation": "collection",
                    "amount_fen": 1000,
                }
            ],
        },
    )
    publish("collection")
    save(
        "pass_through_return",
        "first-return",
        {
            "period": "2026-09",
            "source_id": "agency",
            "amount_fen": 200,
            "refund_right_confirmed": True,
        },
    )
    publish("first-return")
    frozen = close_transactions(engine, proof, "2026-09")
    save(
        "pass_through_return",
        "later-return",
        {
            "period": "2026-10",
            "source_id": "agency",
            "amount_fen": 300,
            "refund_right_confirmed": True,
        },
    )
    publish("later-return")
    assert engine.overview("2026-09")["pending"] == []
    assert Periods(engine).closed_report("2026-09") == frozen
