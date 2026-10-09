"""Payment display parts preserve exact slots, original batches and voucher money."""

import json

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
from test_dashboard_activity_classification import (
    PERIOD,
    _allocation,
    _category,
    _expense,
    _loaded_members,
)
from test_dashboard_provenance import profile
from test_dashboard_settlement_parties import batch_company as batch_company
from test_integrity_content import damage
from test_payroll_reserve_payment import batch
from test_payroll_reserve_payment import company as company

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_brief_groups import activity_group_page
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.response_contracts import validate_response


def _bank_batch(book, subject, allocations, *, revision=0, date="2026-09-28"):
    _, save, publish, _ = book
    data = {
        "period": PERIOD, "actual_date": date, "direction": "outflow",
        "bank_account_id": "bank", "counterparty_id": None, "payment_method": "bank_batch",
        "amount_fen": sum(item["amount_fen"] for item in allocations), "allocations": allocations,
    }
    save("payment", subject, data, revision=revision)
    if not revision:
        publish(subject)
    return data


def _parts_for(dashboard, subject, period=PERIOD):
    with dashboard._snapshot(period) as snap:
        rows = snap.month_journal.verified_rows()
        if rows is None:
            query, parameters = snap.month_journal.sql()
            rows = list(snap.connection.execute(query, parameters))
        bases = {row[0] for row in snap.connection.execute(
            "SELECT id FROM calculation WHERE subject_id=?", (subject,),
        )}
        ids = {row["id"] for row in rows if row["basis_calculation_id"] in bases}
        return [part for ident, parts in snap.activity_components.items()
                if ident in ids for part in parts]


def test_mixed_batch_objects_belong_only_to_their_own_exact_category(book):
    engine, save, publish, _ = book
    profile(engine, "counterparty", "supplier", display_name="合成供应商")
    profile(engine, "counterparty", "alice", display_name="合成员工")
    _expense(save, publish, "supplier-cost", "supplier", 7000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 10000, "employee")
    _bank_batch(book, "mixed", [
        _allocation("expense", "supplier-cost", 7000, recipient="supplier"),
        _allocation("expense", "employee-cost", 10000, recipient="alice"),
    ])
    dashboard = Dashboard(engine)
    before = engine.ledger(PERIOD)
    parts = _parts_for(dashboard, "mixed")
    assert {item["source_category"]: set(item["identities"]) for item in parts} == {
        "expense_supplier": {"supplier"}, "employee_reimbursement": {"alice"},
    }
    assert all(item["is_batch"] for item in parts)
    response = dashboard.brief(PERIOD, voucher_number=3)
    validate_response("dashboard_brief", response)
    assert response["schema_version"] == 18
    data = response["data"]
    assert data["focused_activity"] is None
    assert data["focused_activity_group"] is None
    assert data["focused_voucher"]["subject_id"] == "mixed"
    assert data["focused_voucher"]["business_amount_fen"] == 17000
    members = [item for item in _loaded_members(engine, data) if item["subject_id"] == "mixed"]
    assert {_category(item): item["party"] for item in members} == {
        "expense_supplier": "合成供应商", "employee_reimbursement": "合成员工",
    }
    assert len({item["key"] for item in members}) == 2
    assert len({item["voucher_version_id"] for item in members}) == 1
    assert engine.ledger(PERIOD) == before


def test_same_people_in_different_bank_batches_keep_independent_display_groups(book):
    engine, save, publish, _ = book
    profile(engine, "counterparty", "alice", display_name="张三")
    profile(engine, "counterparty", "bob", display_name="李四")
    for person, amount in (("alice", 1000), ("bob", 2000)):
        _expense(save, publish, "cost-" + person, person, amount, "employee")
    allocations = [
        _allocation("expense", "cost-alice", 500, recipient="alice"),
        _allocation("expense", "cost-bob", 1000, recipient="bob"),
    ]
    _bank_batch(book, "first-batch", allocations)
    _bank_batch(book, "second-batch", allocations, date="2026-09-29")
    dashboard = Dashboard(engine)
    data = dashboard.brief(PERIOD)["data"]
    payment_groups = [item for item in data["collections"]["activity"]["items"]
                      if item["kind"] == "payment"]
    assert len(payment_groups) == 2
    assert len({item["group_key"] for item in payment_groups}) == 2
    assert all(item["member_count"] == item["voucher_count"] == 1 for item in payment_groups)
    assert [item["amount_fen"] for item in payment_groups] == [1500, 1500]
    assert [item["party"] for item in payment_groups] == ["李四、张三", "李四、张三"]
    for subject in ("first-batch", "second-batch"):
        parts = _parts_for(dashboard, subject)
        assert len(parts) == 1
        assert parts[0]["source_category"] == "employee_reimbursement"
        assert parts[0]["identities"] == ("alice", "bob")
        assert parts[0]["party"] == "李四、张三"
        assert set(parts[0]["slots"]) == {0, 1}


def test_same_category_formal_wage_batch_keeps_four_people_in_one_part(batch_company):
    company = batch_company
    dashboard = Dashboard(company.engine)
    before = company.engine.ledger("2026-02")
    parts = _parts_for(dashboard, "batch", "2026-02")
    assert len(parts) == 1
    assert parts[0]["source_category"] == "payroll"
    assert parts[0]["is_batch"]
    assert set(parts[0]["slots"]) == {0, 1, 2, 3}
    assert set(parts[0]["identities"]) == {"one", "two", "three", "four"}
    assert parts[0]["amount_fen"] == sum(
        company.current("wage-" + person).values["net_fen"]
        for person in ("one", "two", "three", "four")
    )
    data = dashboard.brief("2026-02")["data"]
    assert data["activity_count"] == data["voucher_count"] == data["group_count"] == 1
    assert data["collections"]["activity"]["items"][0]["member_count"] == 1
    assert company.engine.ledger("2026-02") == before


def test_explicit_reserve_expense_is_a_separate_component_without_employee_allocation(company):
    fact = batch(company)
    company.save(fact, "gross-batch")
    company.publish("gross-batch")
    dashboard = Dashboard(company.engine)
    before = company.engine.ledger("2026-02")
    parts = {item["source_category"]: item
             for item in _parts_for(dashboard, "gross-batch", "2026-02")}
    assert set(parts) == {"payroll", "expense_supplier"}
    assert parts["payroll"]["amount_fen"] == sum(item.amount_fen for item in fact.allocations)
    assert set(parts["payroll"]["slots"]) == {0, 1}
    expense = parts["expense_supplier"]
    assert expense["amount_fen"] == fact.reserve_expense_fen
    assert not expense["slots"] and not expense["obligation_keys"] and not expense["identities"]
    assert sum(item["amount_fen"] for item in parts.values()) == fact.amount_fen
    data = dashboard.brief("2026-02")["data"]
    assert data["activity_count"] == 2 and data["voucher_count"] == 1
    scopes = {
        item["group"]
        for group in data["collections"]["activity"]["items"]
        for item in dashboard.brief_group(
            "2026-02", section="activity", group_key=group["group_key"],
        )["data"]["collections"]["members"]["items"]
    }
    assert scopes == {
        "payroll", "expense_supplier",
    }
    assert company.engine.ledger("2026-02") == before


def test_display_component_key_is_stable_when_only_confirmed_names_change(book):
    engine, save, publish, _ = book
    _expense(save, publish, "cost", "alice", 1000, "employee")
    _bank_batch(book, "batch", [_allocation("expense", "cost", 1000, recipient="alice")])
    dashboard = Dashboard(engine)
    before = _parts_for(dashboard, "batch")[0]
    profile(engine, "counterparty", "alice", display_name="合成名称更正")
    after = _parts_for(dashboard, "batch")[0]
    assert after["key"] == before["key"]
    assert after["identities"] == before["identities"]
    assert after["amount_fen"] == before["amount_fen"]


def test_mixed_group_summary_uses_narrow_inputs_without_fact_bodies(book, monkeypatch):
    engine, save, publish, _ = book
    _expense(save, publish, "supplier-cost", "supplier", 7000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 10000, "employee")
    _bank_batch(book, "mixed", [
        _allocation("expense", "supplier-cost", 7000, recipient="supplier"),
        _allocation("expense", "employee-cost", 10000, recipient="alice"),
    ])

    def unexpected(reads, ids):
        if set(ids):
            pytest.fail("activity components must classify from narrow adopted fields")
        return {}

    monkeypatch.setattr(QueryReads, "facts", unexpected)
    with Dashboard(engine)._snapshot(PERIOD) as snap:
        groups = activity_group_page(snap)
    assert groups["page"]["total_count"] == 4
    assert len([item for item in groups["items"] if item["kind"] == "payment"]) == 2


@pytest.mark.parametrize("field,value", [
    ("amount_fen", 1100), ("amount_fen", None), ("source_calculation", "missing"),
    ("obligation", "unrelated"),
])
def test_damaged_payment_slot_is_rejected_instead_of_becoming_a_residual(book, field, value):
    engine, save, publish, _ = book
    _expense(save, publish, "cost", "alice", 1000, "employee")
    _bank_batch(book, "batch", [_allocation("expense", "cost", 1000, recipient="alice")])
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT c.id,c.outcome FROM calculation c JOIN calculation_current selected "
            "ON selected.calculation_id=c.id WHERE selected.subject_id='batch'",
        ).fetchone()
    outcome = json.loads(row["outcome"])
    outcome["values"]["settlements"][0][field] = value
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?", (
        json.dumps(outcome, separators=(",", ":")), row["id"],
    ))
    with pytest.raises(KernelError) as caught:
        Dashboard(engine).brief(PERIOD, limit=1)
    assert caught.value.code == "content_integrity_failed"


def test_closed_mixed_payment_reversal_keeps_original_categories_and_exact_negative_amounts(book):
    engine, save, publish, proof = book
    opening(save, publish, bank="bank")
    funding(save, publish, subject="capital", amount=200, bank="bank")
    _expense(save, publish, "supplier-cost", "alice", 1000, "supplier")
    _expense(save, publish, "employee-cost", "alice", 1000, "employee")
    original = [
        _allocation("expense", "supplier-cost", 100, recipient="alice"),
        _allocation("expense", "employee-cost", 100, recipient="alice"),
    ]
    _bank_batch(book, "mixed", original)
    statement(save, publish, [
        entry("capital", amount=200), entry("mixed", "2026-09-28", -200),
    ], bank="bank")
    reconciliation(save, publish, [
        {"reference": "capital", "source_kind": "funding", "source_id": "capital"},
        {"reference": "mixed", "source_kind": "payment", "source_id": "mixed"},
    ], bank="bank")
    assert close_month(
        inventories(engine, proof, PERIOD, {"transactions", "bank", "financing"}), proof, PERIOD,
    )["status"] == "closed"
    frozen_ledger = engine.ledger(PERIOD)
    amended = [dict(original[0], amount_fen=150), dict(original[1], amount_fen=50)]
    _bank_batch(book, "mixed", amended, revision=1)
    preview = engine.preview(["mixed"], posting_period="2026-10")
    engine.confirm(["mixed"], preview_digest=preview["digest"], epochs=preview["epochs"],
                   posting_period="2026-10", request_id="publish-reallocation")
    dashboard = Dashboard(engine)
    data = dashboard.brief("2026-10")["data"]
    corrections = [
        item for group in data["collections"]["activity"]["items"]
        if group["group"] == "correction"
        for item in dashboard.brief_group(
            "2026-10", section="activity", group_key=group["group_key"],
        )["data"]["collections"]["members"]["items"]
    ]
    original_categories = {part["key"]: part["source_category"]
                           for part in _parts_for(dashboard, "mixed", "2026-10")}
    assert {original_categories[item["key"]]: item["amount_fen"] for item in corrections} == {
        "expense_supplier": -100, "employee_reimbursement": -100,
    }
    assert data["activity_count"] == 4 and data["voucher_count"] == 2
    assert len({item["key"] for item in corrections}) == 2
    assert engine.ledger(PERIOD) == frozen_ledger
