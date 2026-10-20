"""Public period checks, real horizon work, and atomic range closing."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract

from ._working_case import case as case
from .conftest import source


def asset_case(case):
    case.seed(expense=False)
    evidence = source(
        "合成核实：公司一月向供应商购入1200元固定设备并启用，尚未付款；可用12个月，残值0，行政管理使用。"
    )
    asset = str(uuid4())
    acquisition = {
        "business_id": str(uuid4()),
        "kind": "assets.acquisition",
        "period": "2026-01",
        "evidence_ids": [evidence["source_id"]],
        "data": {
            "type": "direct",
            "supplier_id": case.party,
            "asset": {"asset_id": asset, "asset_type": "fixed", "cost_fen": "120000"},
        },
    }
    activation = {
        "business_id": str(uuid4()),
        "kind": "assets.activation",
        "period": "2026-01",
        "evidence_ids": [evidence["source_id"]],
        "data": {
            "items": [
                {
                    "asset_id": asset,
                    "useful_life_months": 12,
                    "residual_fen": "0",
                    "benefit_area": "administration",
                    "rounding": "round_half_up_card",
                }
            ]
        },
    }
    response = case.app.accounting_apply(
        case.request(
            acquisition,
            activation,
            sources=[evidence],
            objects=[
                {
                    "object_id": asset,
                    "kind": "asset",
                    "name": "合成设备",
                    "expected_seq": 0,
                    "data": {"asset_type": "fixed", "category": "合成设备"},
                }
            ],
        )
    )
    assert response["completion"] == "complete", response
    return acquisition, activation


def test_horizon_check_is_persistently_read_only_then_one_close_completes_every_needed_month(case):
    acquired, enabled = asset_case(case)
    before = case.get(enabled["business_id"])
    documents = case.app.accounting_documents({"company_id": case.company, "action": "status"})
    with case.kernel.storage.read_unit(case.company) as read:
        head = read.head
    first = case.check("2026-03")
    second = case.check("2026-03")
    assert first["ready"] and second["head"] == first["head"], first
    assert first["report_head"] is None
    assert case.get(enabled["business_id"]) == before
    assert (
        case.app.accounting_documents({"company_id": case.company, "action": "status"}) == documents
    )
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == head
        assert case.kernel.ledger.closed_through(read) is None
    closed = case.close(first)
    assert closed["state"] == "closed" and closed["document_state"] == "ready"
    current = case.get(enabled["business_id"])["businesses"][0]
    assert current["state"]["seq"] == 1
    assert {view["reference"]["result_slot"] for view in current["results"]} == {
        "main",
        "depreciation:2026-02",
        "depreciation:2026-03",
    }
    with case.kernel.storage.read_unit(case.company) as read:
        report = case.kernel.ledger.financial_snapshot(
            {
                "company_id": case.company,
                "period_from": "2026-03",
                "period_to": "2026-03",
                "view": "current",
            },
            read,
        )
        assert report["pre_tax"]["profit_before_tax"] == {"state": "known", "amount_fen": "-20000"}
        assert case.kernel.ledger.closed_through(read) == "2026-03"
        closed_head = read.head
    assert case.close(first)["state"] == "closed"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == closed_head
    context = case.context(note_scopes=[], issue_scopes=[])
    assert context["closed_periods"] == ["2026-01", "2026-02", "2026-03"]
    assert context["open_periods"] == ["2026-04"]
    assert context["company_identity"]["working_period_to"] == "2026-03"


def test_close_failure_after_horizon_adoption_rolls_everything_back(case, monkeypatch):
    _, enabled = asset_case(case)
    checked = case.check("2026-03")
    before = case.get(enabled["business_id"])
    documents = case.app.accounting_documents({"company_id": case.company, "action": "status"})
    original = case.kernel.ledger.close_period

    def fail_after_freeze(*args, **kwargs):
        original(*args, **kwargs)
        raise ContractError("temporary_failure", "合成冻结后的提交失败。")

    monkeypatch.setattr(case.kernel.ledger, "close_period", fail_after_freeze)
    with pytest.raises(ContractError) as error:
        case.close(checked)
    assert error.value.code == "temporary_failure"
    assert case.get(enabled["business_id"]) == before
    assert (
        case.app.accounting_documents({"company_id": case.company, "action": "status"}) == documents
    )
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.kernel.ledger.closed_through(read) is None


def test_preview_rolls_back_note_invalidation_and_close_clears_the_old_explanation(case):
    asset_case(case)
    note = {
        "scope": "period:2026-02",
        "expected_seq": 0,
        "text": "原来尚未生成二月折旧时的说明。",
        "evidence_ids": [case.source_id],
        "expected_basis_head": case.note_head("2026-02"),
    }
    assert case.app.accounting_apply(case.request(notes=[note]))["completion"] == "complete"
    query = {"note_scopes": [note["scope"]], "issue_scopes": []}
    before = case.context(**query)["notes"]
    checked = case.check("2026-03")
    assert case.context(**query)["notes"] == before
    assert case.close(checked)["state"] == "closed"
    after = case.context(**query)["notes"][0]
    assert after["state"] == "needs_refresh" and after["seq"] == 2
    assert after["text"] is None and after["evidence_ids"] == []


def test_related_change_conflicts_but_future_business_and_company_background_do_not(case):
    fact = case.seed()
    checked = case.check()
    note = {
        "scope": "company",
        "expected_seq": 0,
        "text": "公司现实背景。",
        "evidence_ids": [case.source_id],
    }
    assert (
        case.app.accounting_apply(case.request(case.expense("30000", "2026-04"), notes=[note]))[
            "completion"
        ]
        == "complete"
    )
    assert case.check()["head"] == checked["head"]
    changed = deepcopy(fact)
    changed["data"]["amount_fen"] = "90000"
    request = case.request(changed)
    request["mutations"][0].update(op="replace", expected_seq=1)
    assert case.app.accounting_apply(request)["completion"] == "complete"
    with pytest.raises(ContractError) as error:
        case.close(checked)
    assert error.value.code == "conflict"
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.kernel.ledger.closed_through(read) is None


def test_two_concurrent_closes_reuse_one_committed_close(case):
    case.seed()
    checked = case.check()
    with case.kernel.storage.read_unit(case.company) as read:
        head = read.head
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: case.close(checked), range(2)))
    assert all(item["state"] == "closed" for item in outcomes)
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == head + 1


def test_custom_scope_missing_fact_blocks_but_external_filing_and_unpaid_debt_do_not(case):
    case.seed()
    external = {
        "issue_id": str(uuid4()),
        "scope": "custom:work",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "missing_fields": [],
        "question": "老板稍后实际申报。",
        "assignee": "owner",
        "action": "external_filing",
    }
    assert (
        case.app.accounting_apply(
            case.request(
                unresolved=[external], unresolved_scope=external["scope"], expected_scope_head=0
            )
        )["completion"]
        == "complete"
    )
    ready = case.check()
    assert ready["ready"] and ready["unresolved"] == [external], ready
    necessary = {
        **external,
        "issue_id": str(uuid4()),
        "missing_fields": ["/data/amount_fen"],
        "question": "本月一张原件金额不清楚。",
        "assignee": "ai",
        "action": "provide_material",
    }
    assert (
        case.app.accounting_apply(
            case.request(
                unresolved=[external, necessary],
                unresolved_scope=external["scope"],
                expected_scope_head=1,
            )
        )["completion"]
        == "complete"
    )
    blocked = case.check()
    assert not blocked["ready"]
    index = next(
        index
        for index, item in enumerate(blocked["unresolved"])
        if item["issue_id"] == necessary["issue_id"]
    )
    assert any(
        item["field"] == f"/unresolved/{index}/missing_fields/0"
        for item in blocked["blocking_issues"]
    )
    with pytest.raises(ContractError) as error:
        case.close(blocked)
    assert error.value.code == "needs_information"


@pytest.mark.parametrize("necessary", [False, True], ids=["external", "necessary"])
def test_legal_multi_scope_work_keeps_all_issues_without_a_500_output_cap(case, necessary):
    case.seed()
    expected = set()
    for scope, count in (("external:a", 300), ("external:b", 201)):
        issues = [
            {
                "issue_id": str(uuid4()),
                "scope": scope,
                "period": "2026-01",
                "evidence_ids": [case.source_id],
                "missing_fields": ["/data/amount_fen"] if necessary else [],
                "question": "请核实资料的实际金额。" if necessary else "老板按实际情况办理申报。",
                "assignee": "ai" if necessary else "owner",
                "action": "provide_material" if necessary else "external_filing",
            }
            for _ in range(count)
        ]
        expected.update(item["issue_id"] for item in issues)
        result = case.app.accounting_apply(
            case.request(unresolved=issues, unresolved_scope=scope, expected_scope_head=0)
        )
        assert result["completion"] == "complete", result
    checked = case.check()
    assert checked["ready"] is (not necessary), checked
    assert {item["issue_id"] for item in checked["unresolved"]} == expected
    assert len(case.context(issue_scopes=["external:a", "external:b"])["issues"]) == 501
    if necessary:
        assert len({item["field"] for item in checked["blocking_issues"]}) == 501
        with pytest.raises(ContractError) as error:
            case.close(checked)
        assert len(error.value.fact_issues) == 501
        validate_contract("core.schema.json", "ErrorResponse", error.value.to_response())


def test_future_item_in_the_same_issue_scope_does_not_invalidate_this_month_check(case):
    case.seed()
    current = {
        "issue_id": str(uuid4()),
        "scope": "shared:tasks",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "missing_fields": [],
        "question": "一月外部申报待办。",
        "assignee": "owner",
        "action": "external_filing",
    }
    future = {
        **current,
        "issue_id": str(uuid4()),
        "period": "2026-04",
        "question": "四月外部申报待办。",
    }
    assert (
        case.app.accounting_apply(
            case.request(
                unresolved=[current, future],
                unresolved_scope=current["scope"],
                expected_scope_head=0,
            )
        )["completion"]
        == "complete"
    )
    checked = case.check()
    future["question"] = "仅修改四月的外部待办。"
    assert (
        case.app.accounting_apply(
            case.request(
                unresolved=[current, future],
                unresolved_scope=current["scope"],
                expected_scope_head=1,
            )
        )["completion"]
        == "complete"
    )
    assert case.check()["head"] == checked["head"]
    assert case.close(checked)["state"] == "closed"


def test_correction_and_frozen_withdrawal_enter_explicit_discovery_month_horizon(case):
    acquired, enabled = asset_case(case)
    case.close(case.check())
    request = case.request(
        correction_period="2026-03", explanation="已核实整项采购及启用均误记，不存在对应现实业务。"
    )
    request["mutations"] = [
        {"op": "withdraw", "business_id": fact["business_id"], "expected_seq": 1}
        for fact in (acquired, enabled)
    ]
    result = case.app.accounting_apply(request)
    assert result["completion"] == "complete", result
    assert (
        case.context(note_scopes=[], issue_scopes=[])["company_identity"]["working_period_to"]
        == "2026-03"
    )


def test_bank_actual_money_requires_complete_original_statement_and_reconciliation(case):
    expense = case.seed()
    evidence = source("合成核验：建账时银行余额1000元和留存收益1000元，一月20日实际支付费用400元。")
    bank = str(uuid4())
    opening_id = case.app.accounting_find(
        {
            "company_id": case.company,
            "family": "opening",
            "period_from": "2026-01",
            "period_to": "2026-01",
            "limit": 1,
        }
    )["items"][0]["business_id"]
    opening = case.get(opening_id)["businesses"][0]["fact"]
    opening["evidence_ids"] = [evidence["source_id"]]
    opening["data"] = {
        "basis": "existing_ledger",
        "members": [
            {"member_id": str(uuid4()), "evidence_ids": [evidence["source_id"]], "data": data}
            for data in (
                {"type": "bank", "funds_account_id": bank, "balance_fen": "100000"},
                {
                    "type": "equity",
                    "equity_kind": "retained_earnings",
                    "holder_or_basis_id": case.party,
                    "balance_fen": "100000",
                },
            )
        ],
    }
    movement = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-01",
        "effective_on": "2026-01-20",
        "evidence_ids": [evidence["source_id"]],
        "data": {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "amount_fen": "40000",
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": expense["business_id"], "slot": "primary"},
                    "amount_fen": "40000",
                }
            ],
        },
    }
    request = case.request(
        opening,
        movement,
        sources=[evidence],
        objects=[
            {
                "object_id": bank,
                "kind": "bank_account",
                "name": "合成银行",
                "expected_seq": 0,
                "data": {
                    "bank_name": "合成银行",
                    "account_number": "synthetic-only",
                    "currency": "CNY",
                    "owner": "company",
                },
            }
        ],
    )
    request["mutations"][0].update(op="replace", expected_seq=1)
    result = case.app.accounting_apply(request)
    assert result["completion"] == "complete", result
    blocked = case.check()
    assert not blocked["ready"] and any(
        item["code"] == "statement_missing" for item in blocked["blocking_issues"]
    )
    with case.kernel.storage.read_unit(case.company) as read:
        movement_id = case.kernel.ledger.fund_movements(bank, "2026-01", "2026-01", read)[0].value[
            "movement_id"
        ]
    row = str(uuid4())
    statement = {
        "business_id": str(uuid4()),
        "kind": "money.bank_statement",
        "period": "2026-01",
        "evidence_ids": [evidence["source_id"]],
        "data": {
            "funds_account_id": bank,
            "opening_fen": "100000",
            "closing_fen": "60000",
            "rows": [
                {
                    "row_id": row,
                    "source": {"source_id": evidence["source_id"]},
                    "actual_on": "2026-01-20",
                    "direction": "outflow",
                    "amount_fen": "40000",
                }
            ],
        },
    }
    reconciliation = {
        "business_id": str(uuid4()),
        "kind": "money.reconciliation",
        "period": "2026-01",
        "evidence_ids": [evidence["source_id"]],
        "data": {
            "statement_id": statement["business_id"],
            "matches": [{"statement_row_ids": [row], "fund_movement_ids": [movement_id]}],
        },
    }
    result = case.app.accounting_apply(case.request(statement, reconciliation))
    assert result["completion"] == "complete", result
    ready = case.check()
    assert ready["ready"], ready
    assert case.close(ready)["state"] == "closed"
