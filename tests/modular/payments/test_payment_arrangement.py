"""A sourced payment arrangement is independent of actual withholding and filing."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from ai_accounting_modular.files.public import create_files
from ai_accounting_modular.payments.public import create_payment_export_producer
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_people import PERSON, declaration, no_contributions
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_read_remuneration import (
    add,
    bank_object,
    correct,
    payment,
    wage_with_actuals,
)
from tests.modular.domains.people.test_read_remuneration_tax import tax_target, unpaid_wage

from .test_planning import current, inspect, make_plan, owner, payee, register_template


def confirmation(case, fact):
    source_id = str(uuid4())
    text = (
        f"软件验收专用付款安排：业务{fact['business_id']}本次按内核当前应付余额发放，"
        "除此没有未明确的暂扣；实际扣税、申报仍按后续真实业务分别办理。"
    )
    result = case.app.accounting_apply(
        case.request(
            sources=[
                {
                    "source_id": source_id,
                    "kind": "owner_statement",
                    "text": text,
                    "sha256": sha256(text.encode()).hexdigest(),
                    "archive_month": fact["period"],
                }
            ]
        )
    )
    assert result["completion"] == "complete", result
    return {
        "type": "current_payable",
        "source": {"source_id": source_id, "location": "本次付款安排"},
    }


def remuneration(kind):
    fact = unpaid_wage() if kind == "wage" else business_case("B17B" if kind == "bonus" else "B17L")
    fact["data"].pop("tax_basis", None)
    fact["data"].update(person_id=PERSON, gross_fen="327750")
    return fact


def position(module, case, fact, period="2026-01"):
    with case.kernel.storage.read_unit(case.company) as read:
        return module.people.remuneration_positions([fact["business_id"]], period, read)[0]


@pytest.mark.parametrize("kind", ["wage", "bonus", "labor"])
def test_sourced_current_payable_can_plan_without_tax_period_or_actual_withholding(case, kind):
    case.seed(expense=False)
    fact = remuneration(kind)
    fact["data"]["disbursement_basis"] = confirmation(case, fact)
    add(case, *([no_contributions()] if kind == "wage" else []), fact)
    payee(case)
    module = owner(case)
    actual = position(module, case, fact)
    assert actual["payment_composition_state"] == "confirmed"
    assert actual["tax_state"] == "not_withheld_not_filed"
    assert actual["tax_targets"] == []
    assert actual["payable_fen"] == "327750"
    plan = make_plan(module, case, register_template(case))
    assert plan["total_fen"] == "327750"
    exact = case.get(fact["business_id"])["businesses"][0]
    assert "tax_period" not in exact["results"][0]["target"]["values"]
    assert "tax_basis" not in exact["fact"]["data"]
    assert "effective_on" not in exact["fact"]
    assert exact["fact"]["data"]["gross_fen"] == "327750"


def test_source_text_alone_does_not_confirm_and_removing_typed_arrangement_expires_plan(case):
    case.seed(expense=False)
    fact = remuneration("labor")
    basis = confirmation(case, fact)
    fact["evidence_ids"].append(basis["source"]["source_id"])
    add(case, fact)
    payee(case)
    module, source = owner(case), register_template(case)
    waiting = inspect(module, case, template_source_id=source)
    assert not waiting["available"]
    assert [issue["code"] for issue in waiting["issues"]] == [
        "payment_payable_composition_unconfirmed"
    ]
    assert position(module, case, fact)["payment_composition_state"] == "unconfirmed"
    assert "付款" in waiting["issues"][0]["message"]
    assert "税期" not in waiting["issues"][0]["message"]
    changed = deepcopy(fact)
    changed["data"]["disbursement_basis"] = basis
    correct(case, changed)
    plan = make_plan(module, case, source)
    assert plan["total_fen"] == "327750"
    assert current(module, case, plan)
    correct(case, fact, seq=2)
    assert not current(module, case, plan)
    assert position(module, case, fact)["payment_composition_state"] == "unconfirmed"


@pytest.mark.parametrize(
    "basis_type,expected,held",
    [("current_payable", "395000", "0"), ("declared_tax", "390000", "5000")],
)
def test_confirmed_arrangement_uses_current_tax_social_hold_and_partial_pay_once(
    case, basis_type, expected, held
):
    case.seed(expense=False)
    fact, employment, actual = wage_with_actuals()
    extra = []
    if basis_type == "current_payable":
        fact["data"]["disbursement_basis"] = confirmation(case, fact)
    else:
        declared, reference = declaration(fact, "40000")
        fact["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
        extra.append(declared)
    bank = bank_object()
    add(case, fact, employment, actual, *extra, objects=[bank])
    payee(case)
    add(case, tax_target(fact, "35000", tax_period="2026-01"))
    add(case, payment(bank["object_id"], "2026-02", (fact["business_id"], "100000")))
    module = owner(case)
    result = position(module, case, fact, "2026-02")
    assert result["payment_composition_state"] == "confirmed"
    assert result["amounts"]["net_fen"] == "495000"
    assert result["amounts"]["held_fen"] == held
    assert result["remaining_fen"] == "395000" and result["payable_fen"] == expected
    plan = make_plan(module, case, register_template(case))
    assert plan["total_fen"] == expected


def test_generated_arranged_labor_file_stays_unpaid_and_only_relevant_changes_expire_it(case):
    case.seed(expense=False)
    fact, bank = remuneration("labor"), bank_object()
    fact["data"]["disbursement_basis"] = confirmation(case, fact)
    add(case, fact, objects=[bank])
    payee(case)
    module = owner(case)
    plan = make_plan(module, case, register_template(case))
    original = case.get(fact["business_id"])["businesses"]
    service = create_files(
        case.kernel.storage, {"payment_export": create_payment_export_producer(module)}
    )
    try:
        task = service.ensure_current(plan, plan["scope_head"], str(uuid4()))
        service.run(case.company, task["task_id"])
        ready = service.get(case.company, task["task_id"], check_current=True)
        assert ready["state"] == "succeeded", ready
        with service.verified_download(case.company, task["task_id"]) as download:
            workbook = load_workbook(BytesIO(download.stream.read()))
            assert workbook.active["C2"].value == "3277.50"
            workbook.close()
        assert case.get(fact["business_id"])["businesses"] == original
        assert position(module, case, fact)["remaining_fen"] == "327750"
        revenue = {
            "business_id": str(uuid4()),
            "kind": "operations.other_income",
            "period": "2026-03",
            "effective_on": "2026-03-01",
            "evidence_ids": [case.source_id],
            "data": {
                "income_kind": "bank_interest",
                "amount_fen": "100",
                "counterparty_id": case.party,
                "funds_account_id": bank["object_id"],
            },
        }
        add(case, revenue)
        assert (
            service.get(case.company, task["task_id"], check_current=True)["state"] == "succeeded"
        )
        add(case, tax_target(fact, "10000"))
        assert service.get(case.company, task["task_id"], check_current=True)["state"] == "expired"
        updated = make_plan(module, case, plan["template"]["source_id"])
        assert updated["total_fen"] == "317750"
    finally:
        service.close()
