"""Opening packages contain several real payment obligations, never a synthetic gross wage."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.opening.public import (
    OpeningOrdinaryReference,
    opening_obligation_references,
    opening_payroll_references,
)
from ai_accounting_modular.payments import _plans
from tests.modular.application._working_case import case as case
from tests.modular.ledger.opening._support import (
    BANK,
    PARTY,
    PERSON,
    SOURCE,
    member,
    opening,
    request,
)

from .test_planning import current, inspect, make_plan, owner, payee, register_template


def opening_case(case):
    second = str(uuid4())
    members = [
        member(
            {
                "type": "payroll_payable",
                "person_id": PERSON,
                "recipient_id": PARTY,
                "payroll_period": "2025-12",
                "component": "net",
                "amount_fen": "10000",
            }
        ),
        member(
            {
                "type": "payroll_payable",
                "person_id": second,
                "recipient_id": PERSON,
                "payroll_period": "2025-11",
                "component": "net",
                "amount_fen": "20000",
            }
        ),
        member(
            {
                "type": "obligation",
                "counterparty_id": PERSON,
                "nature": "employee_reimbursement",
                "amount_fen": "3000",
                "original_business_reference": "建账前已核对员工报销",
            }
        ),
        member(
            {
                "type": "obligation",
                "counterparty_id": PERSON,
                "nature": "owner_reimbursement",
                "amount_fen": "2000",
                "original_business_reference": "建账前已核对负责人垫款",
            }
        ),
        member(
            {
                "type": "obligation",
                "counterparty_id": PARTY,
                "nature": "supplier_service_payable",
                "amount_fen": "4000",
                "original_business_reference": "不纳入代发的供应商款",
            }
        ),
        member(
            {
                "type": "obligation",
                "counterparty_id": PARTY,
                "nature": "customer_receivable",
                "amount_fen": "5000",
                "original_business_reference": "不纳入代发的客户欠款",
            }
        ),
        member(
            {
                "type": "tax",
                "authority_id": PARTY,
                "tax_kind": "vat",
                "balance_kind": "payable",
                "amount_fen": "600",
                "tax_period": "2025-12",
            }
        ),
        member(
            {
                "type": "payroll_payable",
                "person_id": PERSON,
                "recipient_id": PARTY,
                "payroll_period": "2025-12",
                "component": "employee_social",
                "amount_fen": "700",
            }
        ),
        member({"type": "bank", "funds_account_id": BANK, "balance_fen": "35300"}),
    ]
    package = opening(*members, period="2026-01")
    wire = request(case.company, package)
    wire["objects"].append(
        {
            "object_id": second,
            "kind": "person",
            "name": "合成第二员工",
            "expected_seq": 0,
            "data": {},
        }
    )
    result = case.app.accounting_apply(wire)
    assert result["completion"] == "complete", result
    payee(case, PARTY, payee_name="合成工资实际收款方", payee_account="000111")
    payee(case, PERSON, payee_name="合成期初个人收款方", payee_account="000222")
    with case.kernel.storage.read_unit(case.company) as read:
        refs = case.kernel.ledger.get_result_refs([package["business_id"]], read)
        content = case.kernel.ledger.get_adopted_contents(refs, read)[0]
    return package, content, second


def test_one_opening_package_exports_multiple_net_and_reimbursement_obligations(case, monkeypatch):
    package, content, _ = opening_case(case)
    module, source = owner(case), register_template(case)
    allowed = {
        (item.source["business_id"], item.source["slot"])
        for item in opening_payroll_references(content)
        if item.component == "net"
    } | {
        (item.source["business_id"], item.source["slot"])
        for item in opening_obligation_references(content)
        if isinstance(item, OpeningOrdinaryReference)
        and item.nature in {"employee_reimbursement", "owner_reimbursement"}
    }
    original = module.ledger.get_obligation_positions

    def only_known_obligations(*args, **kwargs):
        assert not kwargs.get("business_ids")
        assert {(ref["business_id"], ref["slot"]) for ref in kwargs["references"]} <= allowed
        return original(*args, **kwargs)

    monkeypatch.setattr(module.ledger, "get_obligation_positions", only_known_obligations)
    plan = make_plan(module, case, source)
    assert len(plan["candidates"]) == 4
    assert {candidate["business_id"] for candidate in plan["candidates"]} == {
        package["business_id"]
    }
    assert plan["total_fen"] == "35000"
    assert {(row["party_id"], row["purpose"]): row["amount_fen"] for row in plan["rows"]} == {
        (PARTY, "2025-12 工资"): "10000",
        (PERSON, "2025-11 工资"): "20000",
        (PERSON, "期初费用报销"): "5000",
    }
    assert {
        (ref["business_id"], ref["slot"]) for row in plan["rows"] for ref in row["obligation_refs"]
    } == allowed
    assert current(module, case, plan)
    assert not any(
        "gross_fen" in candidate or "tax_fen" in candidate for candidate in plan["candidates"]
    )
    tampered = deepcopy(plan)
    tampered["candidates"].append(deepcopy(tampered["candidates"][0]))
    tampered["scope_head"] = _plans.head(tampered)
    with pytest.raises(ContractError, match="同一付款义务"):
        _plans.validate(tampered)


def test_opening_net_partial_payment_and_later_work_month_preserve_real_payroll_month(case):
    package, content, second = opening_case(case)
    module, source = owner(case), register_template(case)
    plan = make_plan(module, case, source)
    source_ref = next(
        item.source for item in opening_payroll_references(content) if item.person_id == second
    )
    paid = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-02",
        "effective_on": "2026-02-03",
        "evidence_ids": [SOURCE],
        "data": {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": "outflow",
            "amount_fen": "5000",
            "payment_method": "individual",
            "counterparty_id": PERSON,
            "allocations": [{"source": source_ref, "amount_fen": "5000"}],
        },
    }
    saved = case.app.accounting_apply(case.request(paid))
    assert saved["completion"] == "complete", saved
    assert not current(module, case, plan)
    selected = make_plan(
        module, case, source, period="2026-02", business_ids=[package["business_id"]]
    )
    assert selected["total_fen"] == "30000"
    assert selected["scope"] == {
        "period": "2026-02",
        "selection": "selected",
        "business_ids": [package["business_id"]],
    }
    assert (
        next(row["amount_fen"] for row in selected["rows"] if row["purpose"] == "2025-11 工资")
        == "15000"
    )
    assert current(module, case, selected)
    month_only = inspect(module, case, period="2026-02", template_source_id=source)
    assert month_only["candidates"] == []


def test_missing_formal_opening_main_is_not_reported_as_no_payment_work(case, monkeypatch):
    opening_case(case)
    module = owner(case)
    query = module.ledger.query_adopted_targets

    def missing_main(*args, **kwargs):
        return replace(
            query(*args, **kwargs), items=(), total_count=0, filtered_count=0, next_cursor=None
        )

    monkeypatch.setattr(module.ledger, "query_adopted_targets", missing_main)
    with pytest.raises(ContractError, match="正式主结果"):
        inspect(module, case)
