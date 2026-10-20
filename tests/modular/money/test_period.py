"""Closing checks complete real sources without turning unpaid business into an error."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError

from .test_application import assemble, payment, reconciled_unit, request, seed


def check(system, period="2026-01"):
    _, storage, company, money, _ = system
    with storage.read_unit(company) as read:
        return money.check_period(period, period, read)


def test_unpaid_business_and_unused_accounts_need_no_invented_statement(tmp_path):
    system = assemble(tmp_path)
    seed(system)
    assert check(system).blocking_issues == ()


def test_actual_bank_money_requires_original_and_complete_matching(tmp_path):
    system = assemble(tmp_path)
    app, _, company, _, _ = system
    ids, _ = seed(system)
    transfer, paid, statement, match = reconciled_unit(ids)
    assert app.accounting_apply(request(company, paid, transfer))["completion"] == "complete"
    assert [i["code"] for i in check(system).blocking_issues] == ["statement_missing"]
    assert app.accounting_apply(request(company, statement))["completion"] == "complete"
    assert [i["code"] for i in check(system).blocking_issues] == ["bank_reconciliation_missing"]
    assert app.accounting_apply(request(company, match))["completion"] == "complete"
    assert check(system).blocking_issues == ()


def test_complete_bank_head_ignores_future_and_unrelated_object_names(tmp_path):
    system = assemble(tmp_path)
    app, _, company, _, _ = system
    ids, expense = seed(system)
    assert app.accounting_apply(request(company, *reconciled_unit(ids)))["completion"] == "complete"
    before = check(system)
    future = deepcopy(expense)
    future.update(business_id=str(uuid4()), period="2026-02")
    assert app.accounting_apply(request(company, future))["completion"] == "complete"
    assert check(system) == before
    object_change = {
        "object_id": ids["bank"],
        "kind": "bank_account",
        "name": "仅更正显示名称",
        "expected_seq": 1,
        "data": {
            "bank_name": "合成银行",
            "account_number": "synthetic-only",
            "currency": "CNY",
            "owner": "company",
            "opened_period": "2026-01",
            "zero_at_opening": True,
        },
    }
    assert (
        app.accounting_apply(request(company, objects=[object_change]))["completion"] == "complete"
    )
    assert check(system) == before


def platform_setup(system):
    app, _, company, _, _ = system
    ids, _ = seed(system)
    ids["platform"] = str(uuid4())
    account = {
        "object_id": ids["platform"],
        "kind": "platform_account",
        "name": "合成平台",
        "expected_seq": 0,
        "data": {
            "platform": "合成平台",
            "account_key": "synthetic",
            "currency": "CNY",
            "owner": "company",
        },
    }
    rows = [
        {
            "row_id": str(uuid4()),
            "source": {"source_id": ids["source"], "location": f"第{i}行"},
            "actual_on": "2026-01-20",
            "direction": "outflow",
            "amount_fen": value,
        }
        for i, value in enumerate(["100000", "200"], 1)
    ]
    statement = {
        "business_id": str(uuid4()),
        "kind": "money.platform_activity",
        "period": "2026-01",
        "evidence_ids": [],
        "data": {"platform_account_id": ids["platform"], "rows": rows},
    }
    assert (
        app.accounting_apply(request(company, statement, objects=[account]))["completion"]
        == "complete"
    )
    return ids, rows


def test_platform_rows_require_actual_or_explicit_internal_coverage(tmp_path):
    system = assemble(tmp_path)
    app, _, company, _, _ = system
    ids, rows = platform_setup(system)
    assert [i["code"] for i in check(system).blocking_issues] == ["platform_activity_incomplete"]
    paid = payment(ids, account="platform")
    paid["data"]["platform_row_ids"] = [rows[0]["row_id"]]
    assert app.accounting_apply(request(company, paid))["completion"] == "complete"
    assert [i["code"] for i in check(system).blocking_issues] == ["platform_activity_incomplete"]
    internal = {
        "business_id": str(uuid4()),
        "kind": "money.reserve_internal",
        "period": "2026-01",
        "evidence_ids": [],
        "data": {
            "platform_account_id": ids["platform"],
            "platform_row_ids": [rows[1]["row_id"]],
            "boundary_source": {"source_id": ids["source"], "location": "明确代管内部移动"},
        },
    }
    assert app.accounting_apply(request(company, internal))["completion"] == "complete"
    assert check(system).blocking_issues == ()


def test_period_check_rejects_reversed_or_historical_scope(tmp_path):
    system = assemble(tmp_path)
    _, storage, company, money, _ = system
    with storage.read_unit(company) as read:
        with pytest.raises(ContractError) as error:
            money.check_period("2026-02", "2026-01", read)
        assert error.value.code == "invalid_input"
    with storage.read_unit(company, view="as_posted", as_of="2026-01") as read:
        with pytest.raises(ContractError) as error:
            money.check_period("2026-01", "2026-01", read)
        assert error.value.code == "invalid_input"
