"""Activity groups follow published business facts and exact settlement obligations."""

from collections import Counter

import pytest
from test_banking import book as book
from test_banking import (
    close_month,
    entry,
    funding,
    inventories,
    opening,
    reconciliation,
    statement,
)
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard

PERIOD = "2026-09"


def _expense(save, publish, subject, party, amount, creditor_kind):
    save(
        "expense",
        subject,
        {
            "period": PERIOD,
            "counterparty_id": party,
            "amount_fen": amount,
            "expense_class": "administration",
            "creditor_kind": creditor_kind,
        },
    )
    publish(subject)


def _allocation(kind, subject, amount, obligation="primary", recipient=None):
    result = {
        "source_kind": kind,
        "source_id": subject,
        "obligation": obligation,
        "amount_fen": amount,
    }
    if recipient is not None:
        result["recipient_id"] = recipient
    return result


def _payment(book, subject, party, allocations, *, channel="bank", direction="outflow"):
    _, save, publish, proof = book
    data = {
        "period": PERIOD,
        "actual_date": "2026-09-28",
        "direction": direction,
        "counterparty_id": party,
        "amount_fen": sum(item["amount_fen"] for item in allocations),
        "allocations": allocations,
    }
    if channel == "bank":
        kind, account_field = "payment", "bank_account_id"
    elif channel == "cash":
        kind, account_field = "cash_payment", "cash_account_id"
    else:
        kind, account_field = "platform_payment", "platform_account_id"
        movement = subject + "-movement"
        save(
            "platform_movement",
            movement,
            {
                "period": PERIOD,
                "actual_date": data["actual_date"],
                "direction": direction,
                "platform_account_id": "platform",
                "transaction_reference": movement,
                "amount_fen": data["amount_fen"],
                "source_evidence_digest": proof,
                "source_location": "row:" + movement,
            },
        )
        publish(movement)
        data["movement_ids"] = [movement]
    data[account_field] = channel
    save(kind, subject, data)
    publish(subject)


def _reimbursement_sources(book):
    _, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 7000, "supplier")
    _expense(save, publish, "alice-cost", "alice", 10000, "employee")
    _expense(save, publish, "bob-cost", "bob", 12000, "employee")
    save(
        "reimbursed_asset",
        "computer",
        {
            "period": PERIOD,
            "asset_id": "computer",
            "asset_type": "fixed",
            "cost_fen": 50000,
            "company_acceptance_confirmed": True,
            "creditors": [
                {"employee_id": "alice", "amount_fen": 20000},
                {"employee_id": "bob", "amount_fen": 30000},
            ],
        },
    )
    save(
        "reimbursed_deposit",
        "deposit",
        {
            "period": PERIOD,
            "counterparty_id": "landlord",
            "employee_id": "alice",
            "amount_fen": 30000,
            "company_acceptance_confirmed": True,
            "refund_right_confirmed": True,
        },
    )
    publish("computer", "deposit")
    return {
        "supplier-cost": "expense_supplier",
        "alice-cost": "employee_reimbursement",
        "bob-cost": "employee_reimbursement",
        "computer": "assets",
        "deposit": "fund_movement",
    }


def _reimbursement_payments(book, channel="bank"):
    expected = _reimbursement_sources(book)
    _payment(
        book,
        "alice-reimbursement",
        "alice",
        [
            _allocation("expense", "alice-cost", 10000),
            _allocation("reimbursed_asset", "computer", 20000, "alice"),
            _allocation("reimbursed_deposit", "deposit", 30000, "reimbursement"),
        ],
        channel=channel,
    )
    _payment(
        book,
        "bob-reimbursement",
        "bob",
        [
            _allocation("expense", "bob-cost", 12000),
            _allocation("reimbursed_asset", "computer", 30000, "bob"),
        ],
        channel=channel,
    )
    _payment(
        book,
        "deposit-return",
        "landlord",
        [_allocation("reimbursed_deposit", "deposit", 30000, "refund")],
        channel=channel,
        direction="inflow",
    )
    expected.update(
        {
            "alice-reimbursement": "employee_reimbursement",
            "bob-reimbursement": "employee_reimbursement",
            "deposit-return": "fund_movement",
        }
    )
    return expected


def _assert_groups(data, expected, *, complete=True):
    activities = data["collections"]["activity"]["items"]
    components = {item["subject_id"]: item for item in activities}
    if complete:
        assert set(components) == set(expected)
    assert {ident: item["group"] for ident, item in components.items()} == {
        ident: expected[ident] for ident in components
    }
    assert {item["key"]: item["event_count"] for item in data["activity_groups"]} == dict(
        Counter(expected.values())
    )
    for group in data["activity_groups"]:
        assert sum(item["count"] for item in group["type_counts"]) == group["event_count"]
    assert len(activities) == len(components)


def test_expense_group_follows_typed_creditor_kind(book):
    engine, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 7000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 10000, "employee")
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(
        data,
        {"supplier-cost": "expense_supplier", "employee-cost": "employee_reimbursement"},
    )


@pytest.mark.parametrize("channel", ["bank", "cash", "platform"])
def test_reimbursement_payments_and_deposit_return_follow_their_exact_obligations(book, channel):
    engine, _, _, _ = book
    expected = _reimbursement_payments(book, channel)
    before = engine.ledger(PERIOD)
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(data, expected)
    assert engine.ledger(PERIOD) == before


def test_whole_month_groups_stay_complete_when_only_one_voucher_is_loaded(book):
    engine, _, _, _ = book
    expected = _reimbursement_payments(book)
    dashboard = Dashboard(engine)
    response = dashboard.brief(PERIOD, preparation="deferred", limit=1)
    loaded = set()
    while True:
        data = response["data"]
        _assert_groups(data, expected, complete=False)
        items = data["collections"]["activity"]["items"]
        assert len(items) == 1
        subject = items[0]["subject_id"]
        assert subject not in loaded
        loaded.add(subject)
        page = data["collections"]["activity"]["page"]
        assert page["total_count"] == len(expected)
        if not page["has_more"]:
            break
        response = dashboard.brief(
            PERIOD,
            preparation="deferred",
            section="activity",
            cursor=page["next_cursor"],
            expected_version=response["snapshot_version"],
            limit=1,
        )
    assert loaded == set(expected)


def test_payment_with_supplier_and_employee_sources_has_one_other_business_event(book):
    engine, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 7000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 10000, "employee")
    save(
        "payment",
        "mixed-batch",
        {
            "period": PERIOD,
            "actual_date": "2026-09-28",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": None,
            "payment_method": "bank_batch",
            "amount_fen": 17000,
            "allocations": [
                _allocation("expense", "supplier-cost", 7000, recipient="supplier"),
                _allocation("expense", "employee-cost", 10000, recipient="alice"),
            ],
        },
    )
    publish("mixed-batch")
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(
        data,
        {
            "supplier-cost": "expense_supplier",
            "employee-cost": "employee_reimbursement",
            "mixed-batch": "other",
        },
    )
    assert data["activity_count"] == 3


def test_closed_month_expense_and_payment_keep_their_published_source_after_a_later_draft(book):
    engine, save, publish, proof = book
    opening(save, publish, bank="bank")
    funding(save, publish, subject="capital", amount=10000, bank="bank")
    _expense(save, publish, "employee-cost", "alice", 10000, "employee")
    _payment(book, "employee-paid", "alice", [_allocation("expense", "employee-cost", 10000)])
    statement(
        save,
        publish,
        [entry("capital", amount=10000), entry("employee-paid", "2026-09-28", -10000)],
        bank="bank",
    )
    reconciliation(
        save,
        publish,
        [
            {"reference": "capital", "source_kind": "funding", "source_id": "capital"},
            {"reference": "employee-paid", "source_kind": "payment", "source_id": "employee-paid"},
        ],
        bank="bank",
    )
    assert (
        close_month(
            inventories(engine, proof, PERIOD, {"transactions", "bank", "financing"}), proof, PERIOD
        )["status"]
        == "closed"
    )
    original = engine.ledger(PERIOD)
    save(
        "expense",
        "employee-cost",
        {
            "period": PERIOD,
            "counterparty_id": "alice",
            "amount_fen": 10000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        revision=1,
    )
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(
        data,
        {
            "capital": "financing_owner",
            "employee-cost": "employee_reimbursement",
            "employee-paid": "employee_reimbursement",
        },
    )
    assert engine.ledger(PERIOD) == original


@pytest.mark.parametrize("source_scope", ["off_page_expense", "prior_month_payment_source"])
@pytest.mark.parametrize("corruption", ["duplicate_key", "digest"])
def test_bounded_activity_summary_rejects_damaged_off_page_classification_sources(
    book, source_scope, corruption
):
    engine, save, publish, _ = book
    funding(save, publish, subject="first-voucher", amount=10000, bank="bank")
    if source_scope == "off_page_expense":
        _expense(save, publish, "employee-cost", "alice", 10000, "employee")
        expected = {
            "first-voucher": "financing_owner",
            "employee-cost": "employee_reimbursement",
        }
    else:
        save(
            "expense",
            "employee-cost",
            {
                "period": "2026-08",
                "counterparty_id": "alice",
                "amount_fen": 10000,
                "expense_class": "administration",
                "creditor_kind": "employee",
            },
        )
        publish("employee-cost")
        _payment(book, "employee-paid", "alice", [_allocation("expense", "employee-cost", 10000)])
        expected = {
            "first-voucher": "financing_owner",
            "employee-paid": "employee_reimbursement",
        }
    before = Dashboard(engine).brief(PERIOD, preparation="deferred", limit=1)["data"]
    _assert_groups(before, expected, complete=False)
    assert before["collections"]["activity"]["items"][0]["subject_id"] == "first-voucher"
    with engine.store.connection(read_only=True) as connection:
        source = connection.execute(
            "SELECT c.id,c.outcome FROM calculation c "
            "JOIN calculation_current selected ON selected.calculation_id=c.id "
            "WHERE selected.subject_id='employee-cost'"
        ).fetchone()
    if corruption == "duplicate_key":
        # The final values object is unchanged; a permissive decoder would
        # accept this duplicate key and reproduce the original content digest.
        duplicate = '{"values":{},' + source["outcome"][1:]
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=? WHERE id=?",
            (duplicate, source["id"]),
        )
    else:
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET digest=zeroblob(32) WHERE id=?",
            (source["id"],),
        )
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).brief(PERIOD, preparation="deferred", limit=1)
    assert failure.value.code == "content_integrity_failed"


def test_retained_bank_verification_payment_is_income(book):
    engine, save, publish, _ = book
    save(
        "bank_income",
        "verification-income",
        {
            "period": PERIOD,
            "actual_date": "2026-09-21",
            "bank_account_id": "bank",
            "counterparty_id": "verification-provider",
            "amount_fen": 1,
            "income_kind": "retained_verification_payment",
            "entitlement_confirmed": True,
        },
    )
    publish("verification-income")
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(data, {"verification-income": "income_customer"})
    assert data["collections"]["activity"]["items"][0]["amount_fen"] == 1


def test_overpayment_confirmation_and_its_actual_refund_keep_fund_movement_group(book):
    engine, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 1000, "supplier")
    save(
        "payment",
        "supplier-overpaid",
        {
            "period": PERIOD,
            "actual_date": "2026-09-28",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "supplier",
            "amount_fen": 1100,
            "allocations": [_allocation("expense", "supplier-cost", 1100)],
        },
    )
    save(
        "overpayment",
        "recovery",
        {
            "period": PERIOD,
            "source_kind": "expense",
            "source_id": "supplier-cost",
            "obligation_name": "primary",
            "counterparty_id": "supplier",
            "amount_fen": 100,
            "recovery_right_confirmed": True,
        },
    )
    publish("supplier-overpaid", "recovery")
    _payment(
        book,
        "overpayment-returned",
        "supplier",
        [_allocation("overpayment", "recovery", 100)],
        direction="inflow",
    )
    data = Dashboard(engine).brief(PERIOD, preparation="deferred")["data"]
    _assert_groups(
        data,
        {
            "supplier-cost": "expense_supplier",
            "supplier-overpaid": "expense_supplier",
            "recovery": "fund_movement",
            "overpayment-returned": "fund_movement",
        },
    )
