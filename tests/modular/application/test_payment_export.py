"""The public command delivers current bank files without posting a payment."""

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from ai_accounting_modular.agent.public import create_agent
from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.storage.public import CompanySpec
from tests.modular.application._working_case import Case
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_people import no_contributions
from tests.modular.domains.people.test_read_remuneration import add, bank_object, payment
from tests.modular.domains.people.test_read_remuneration_tax import tax_target, unpaid_wage
from tests.modular.payments.test_planning import expense, payee, register_template
from tests.modular.payments.test_workbook import template


def prepare(case, *, shifted=False):
    case.seed(expense=False)
    wage = unpaid_wage()
    add(case, no_contributions(), wage)
    tax = tax_target(wage, "0", month="2026-01")
    source_id = str(uuid4())
    text = "合成最终工资结算资料：本次应付工资1000元，确认该笔报酬本税期代扣税额为0元。"
    tax["evidence_ids"] = [source_id]
    tax["data"]["components"][0]["source"] = {"source_id": source_id}
    result = case.app.accounting_apply(
        case.request(
            tax,
            sources=[
                {
                    "source_id": source_id,
                    "kind": "owner_statement",
                    "text": text,
                    "sha256": sha256(text.encode()).hexdigest(),
                    "archive_month": "2026-01",
                }
            ],
        )
    )
    assert result["completion"] == "complete", result
    payee(case)
    return wage, register_template(case, template(shifted=shifted))


def command(case, action, **fields):
    return create_agent(case.app).dispatch(
        "accounting_payment_export",
        {"company_id": case.company, "period": "2026-01", "action": action, **fields},
    )


@pytest.mark.parametrize("shifted", [False, True])
def test_inspect_generate_status_and_repeated_generation_match_real_file(case, shifted):
    wage, source = prepare(case, shifted=shifted)
    before = case.get(wage["business_id"])
    assert command(case, "status")["state"] == "not_generated"
    missing = command(case, "inspect")
    assert missing["state"] == "inspected"
    assert missing["inspection"]["total_fen"] == "100000"
    assert command(case, "generate")["state"] == "needs_information"
    pending = command(case, "generate", template_source_id=source)
    assert pending["state"] == "pending"
    assert command(case, "status")["state"] == "pending"
    assert command(case, "generate", template_source_id=source)["task"] == pending["task"]
    case.kernel.files.run_pending(case.company)
    ready = command(case, "status")
    assert ready["state"] == "succeeded" and ready["inspection"]["available"]
    path = Path(ready["task"]["path"])
    book = load_workbook(path)
    assert book.active["D4" if shifted else "B2"].value == "00012345678901234567890"
    assert book.active["F4" if shifted else "C2"].value == "1000.00"
    book.close()
    assert command(case, "generate", template_source_id=source)["task"] == ready["task"]
    assert case.get(wage["business_id"])["businesses"] == before["businesses"]


def test_new_reimbursement_invalidates_completed_scope_then_is_included(case):
    wage, source = prepare(case)
    first = command(case, "generate", template_source_id=source)
    case.kernel.files.run_pending(case.company)
    assert command(case, "status")["state"] == "succeeded"
    add(case, expense(case))
    assert command(case, "status")["state"] == "expired"
    second = command(case, "generate", template_source_id=source)
    assert second["task"]["task_id"] != first["task"]["task_id"]
    assert second["inspection"]["total_fen"] == "130000"
    case.kernel.files.run_pending(case.company)
    assert command(case, "status")["state"] == "succeeded"


def test_actual_output_corruption_cannot_remain_succeeded_and_can_regenerate(case):
    _, source = prepare(case)
    command(case, "generate", template_source_id=source)
    case.kernel.files.run_pending(case.company)
    ready = command(case, "status")
    Path(ready["task"]["path"]).write_bytes(b"not-the-verified-workbook")
    repaired = command(case, "generate", template_source_id=source)
    assert repaired["state"] == "pending"
    case.kernel.files.run_pending(case.company)
    assert command(case, "status")["state"] == "succeeded"


def test_current_work_month_can_deliver_selected_earlier_unpaid_wage(case):
    wage, source = prepare(case)
    selected = {"period": "2026-02", "business_ids": [wage["business_id"]]}
    pending = command(case, "generate", template_source_id=source, **selected)
    assert pending["state"] == "pending"
    assert pending["inspection"]["rows"][0]["purpose"] == "2026-01 工资"
    case.kernel.files.run_pending(case.company)
    ready = command(case, "status", **selected)
    assert ready["state"] == "succeeded"
    assert ready["inspection"]["total_fen"] == "100000"
    assert command(case, "status")["state"] == "not_generated"


@pytest.mark.parametrize("template_fields", [{}, {"template_source_id": str(uuid4())}])
def test_known_fully_paid_scope_does_not_require_a_usable_bank_template(case, template_fields):
    wage, _ = prepare(case)
    bank = bank_object()
    add(
        case,
        payment(bank["object_id"], "2026-02", (wage["business_id"], "100000")),
        objects=[bank],
    )
    result = command(case, "generate", **template_fields)
    assert result["state"] == "no_payments"
    assert result["inspection"]["total_fen"] == "0"
    assert result["inspection"]["issues"]
    assert "task" not in result


def test_pending_template_is_protected_from_explicit_discard(case):
    _, source = prepare(case)
    command(case, "generate", template_source_id=source)
    with pytest.raises(ContractError):
        case.app.accounting_sources(
            {"company_id": case.company, "action": "discard", "source_ids": [source]}
        )
    assert command(case, "status")["state"] == "pending"
    case.kernel.files.run_pending(case.company)
    assert command(case, "status")["state"] == "succeeded"


def test_command_rejects_external_amounts_and_foreign_templates(case):
    prepare(case)
    with pytest.raises(ContractError):
        command(case, "generate", amount_fen="1")
    result = command(case, "generate", template_source_id=str(uuid4()))
    assert result["state"] == "needs_information"
    assert {item["code"] for item in result["inspection"]["issues"]} == {"payment_template_invalid"}


def test_explicit_current_template_choice_cannot_reuse_a_different_old_file(case):
    _, first = prepare(case)
    command(case, "generate", template_source_id=first)
    case.kernel.files.run_pending(case.company)
    assert command(case, "status")["state"] == "succeeded"
    changed = register_template(case, template(shifted=True))
    status = command(case, "status", template_source_id=changed)
    assert status["state"] == "expired"
    assert status["inspection"]["template"]["source_id"] == changed
    assert "task" not in status  # Never offer the old template's successful path as current.
    command(case, "generate", template_source_id=changed)
    case.kernel.files.run_pending(case.company)
    assert command(case, "status")["state"] == "succeeded"


def test_two_companies_keep_their_own_template_and_recipient_data(case, tmp_path):
    _, first_template = prepare(case)
    second_id = str(uuid4())
    case.kernel.storage.create_company(
        CompanySpec(
            second_id,
            str(uuid4()),
            "913100000000000002",
            "另一家合成公司",
            "2026-01",
            tmp_path / "second-company",
        )
    )
    second = Case(case.kernel, second_id, str(uuid4()), str(uuid4()))
    _, second_template = prepare(second, shifted=True)
    invalid = command(second, "generate", template_source_id=first_template)
    assert invalid["state"] == "needs_information"
    assert invalid["inspection"]["issues"][0]["code"] == "payment_template_invalid"
    for selected, source in ((case, first_template), (second, second_template)):
        command(selected, "generate", template_source_id=source)
        selected.kernel.files.run_pending(selected.company)
    first = command(case, "status")
    other = command(second, "status")
    assert first["state"] == other["state"] == "succeeded"
    assert first["inspection"]["template"]["source_id"] == first_template
    assert other["inspection"]["template"]["source_id"] == second_template
    assert first["task"]["path"] != other["task"]["path"]
