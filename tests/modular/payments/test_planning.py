"""Real public accounting produces one coherent bank plan without inventing payments."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.people.public import create_people
from ai_accounting_modular.files.public import create_files
from ai_accounting_modular.payments.public import create_payment_export_producer, create_payments
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_people import PERSON, SOURCE, declaration, no_contributions
from tests.modular.domains.people.test_people import case as business_case
from tests.modular.domains.people.test_read_remuneration import (
    add,
    bank_object,
    payment,
    wage_with_actuals,
)
from tests.modular.domains.people.test_read_remuneration_tax import tax_target, unpaid_wage

from .test_workbook import template


def owner(case):
    return create_payments(
        storage=case.kernel.storage,
        ledger=case.kernel.ledger,
        records=case.kernel.records,
        identity=case.kernel.identity,
        people=create_people(ledger=case.kernel.ledger),
        businesses=case.app,
    )


def payee(case, identity=PERSON, **values):
    with case.kernel.storage.read_unit(case.company) as read:
        obj = case.kernel.identity.get_batch([identity], read)[0]
    update = {key: deepcopy(obj[key]) for key in ("object_id", "kind", "name", "data")}
    update["expected_seq"] = obj["seq"]
    update["data"].update(
        {"payee_name": "合成收款人", "payee_account": "00012345678901234567890", **values}
    )
    result = case.app.accounting_apply(case.request(objects=[update]))
    assert result["completion"] == "complete", result


def confirm_zero_tax(case, wage):
    """Establish an independent synthetic zero-tax source, never infer zero from missing data."""
    source_id = str(uuid4())
    text = f"软件验收专用最终薪酬扣税明细：业务{wage['business_id']}，已确认扣税金额0分。"
    target = tax_target(wage, "0", month="2026-01")
    target["evidence_ids"] = [source_id]
    target["data"]["components"][0]["source"] = {
        "source_id": source_id,
        "location": "最终扣税明细中的本笔报酬",
    }
    saved = case.app.accounting_apply(
        case.request(
            target,
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
    assert saved["completion"] == "complete", saved


def register_template(case, raw=None):
    with case.kernel.storage.read_unit(case.company) as read:
        company = case.kernel.storage.company_metadata(read)
    relative = f"originals/unassigned/{uuid4()}.xlsx"
    path = company.company_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw or template())
    result = case.app.accounting_sources(
        {
            "company_id": case.company,
            "action": "import",
            "sources": [{"kind": "file", "relative_path": relative, "archive_month": "2026-01"}],
        }
    )
    return result["sources"][0]["source_id"]


def inspect(module, case, **extra):
    with case.kernel.storage.read_unit(case.company) as read:
        return module.inspect({"company_id": case.company, "period": "2026-01", **extra}, read)


def make_plan(module, case, template_id, **extra):
    with case.kernel.storage.read_unit(case.company) as read:
        return module.plan(
            {
                "company_id": case.company,
                "period": "2026-01",
                "template_source_id": template_id,
                **extra,
            },
            read,
        )


def current(module, case, plan):
    with case.kernel.storage.read_unit(case.company) as read:
        return module.is_current(plan, read)


def expense(case, amount="30000", *, party=PERSON, period="2026-01"):
    fact = case.expense(amount, period)
    fact["data"].update(creditor_id=party, creditor_kind="employee")
    return fact


def test_inspect_discovers_month_scope_without_demanding_template_or_premature_accounts(case):
    case.seed(expense=False)
    wage = unpaid_wage()
    add(case, no_contributions(), wage)
    result = inspect(owner(case), case)
    assert result["scope"] == {"period": "2026-01", "selection": "period"}
    assert result["total_fen"] == "100000"
    assert result["candidates"][0]["business_id"] == wage["business_id"]
    assert {item["code"] for item in result["issues"]} == {
        "payment_account_missing",
        "payment_template_missing",
        "payment_payable_composition_unconfirmed",
    }
    assert result["available"] is False and result["rows"] == []
    add(case, expense(case))
    rechecked = inspect(owner(case), case)
    assert (
        len([item for item in rechecked["issues"] if item["code"] == "payment_account_missing"])
        == 1
    )


def test_later_tax_partial_payment_and_reimbursements_use_public_current_payable_once(case):
    case.seed(expense=False)
    wage, bank = unpaid_wage(), bank_object()
    first, second = expense(case, "30000"), expense(case, "17000")
    add(case, no_contributions(), wage, first, second, objects=[bank])
    payee(case)
    add(
        case,
        tax_target(wage, "10000"),
        payment(bank["object_id"], "2026-02", (wage["business_id"], "30000")),
    )
    module = owner(case)
    result = inspect(module, case, template_source_id=register_template(case))
    assert result["available"] and result["balance_period"] == "2026-02"
    assert {row["purpose"]: row["amount_fen"] for row in result["rows"]} == {
        "2026-01 工资": "60000",
        "2026-01 费用报销": "47000",
    }
    assert result["total_fen"] == "107000"
    reimbursement = next(row for row in result["rows"] if row["purpose"].endswith("费用报销"))
    assert {item["business_id"] for item in reimbursement["obligation_refs"]} == {
        first["business_id"],
        second["business_id"],
    }
    with case.kernel.storage.read_unit(case.company) as read:
        wage_item = module.people.remuneration_positions([wage["business_id"]], "2026-02", read)[0]
    assert wage_item["payable_fen"] == "60000"


def test_scope_currentness_tracks_new_oil_and_accounts_but_ignores_unrelated_future_revenue(
    case, monkeypatch
):
    case.seed(expense=False)
    wage = unpaid_wage()
    add(case, no_contributions(), wage)
    confirm_zero_tax(case, wage)
    payee(case)
    module, source = owner(case), register_template(case)
    period_plan = make_plan(module, case, source)
    selected_plan = make_plan(module, case, source, business_ids=[wage["business_id"]])
    assert current(module, case, period_plan) and current(module, case, selected_plan)

    # No原件读取 is permitted in the final writer-lock validity predicate.
    def forbidden(*args, **kwargs):
        pytest.fail(
            "currentness must use registered template metadata, never reread original bytes"
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(module.records, "lease_source", forbidden)
        assert current(module, case, period_plan)
    bank = bank_object()
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
    add(case, revenue, objects=[bank])
    assert current(module, case, period_plan) and current(module, case, selected_plan)
    new_expense = expense(case)
    add(case, new_expense)
    assert not current(module, case, period_plan)
    get_positions = module.ledger.get_obligation_positions
    with monkeypatch.context() as scoped:
        scoped.setattr(
            module.ledger,
            "get_obligation_positions",
            lambda *args, **kwargs: (
                ()
                if any(
                    ref["business_id"] == new_expense["business_id"]
                    for ref in kwargs.get("references", ())
                )
                else get_positions(*args, **kwargs)
            ),
        )
        assert not current(module, case, period_plan)
    assert current(module, case, selected_plan)
    payee(case, payee_account="000999")
    assert not current(module, case, selected_plan)


def test_only_supported_selected_scope_and_no_empty_payment_file(case):
    supplier = case.seed()
    module = owner(case)
    with pytest.raises(ContractError, match="供应商"):
        inspect(module, case, business_ids=[supplier["business_id"]])
    missing = inspect(module, case)
    assert missing["candidates"] == [] and missing["total_fen"] == "0"
    assert not missing["available"]  # No records is not a claim about real-world completeness.
    source = register_template(case)
    with pytest.raises(ContractError, match="空代发表"):
        make_plan(module, case, source)


def test_plan_rejects_changed_merged_amount_even_if_outer_digest_is_recomputed(case):
    case.seed(expense=False)
    wage = unpaid_wage()
    add(case, no_contributions(), wage)
    confirm_zero_tax(case, wage)
    payee(case)
    module = owner(case)
    plan = make_plan(module, case, register_template(case))
    plan["rows"][0]["amount_fen"] = "100001"
    plan["total_fen"] = "100001"
    from ai_accounting_modular.payments import _plans

    plan["scope_head"] = _plans.head(plan)
    with pytest.raises(ContractError, match="完整对应"):
        _plans.validate(plan)


def test_background_file_is_verified_and_downloadable_without_posting_bank_payment(case):
    case.seed(expense=False)
    wage = unpaid_wage()
    add(case, no_contributions(), wage)
    confirm_zero_tax(case, wage)
    payee(case)
    module = owner(case)
    plan = make_plan(module, case, register_template(case, template(shifted=True)))
    before = case.get(wage["business_id"])
    service = create_files(
        case.kernel.storage, {"payment_export": create_payment_export_producer(module)}
    )
    try:
        receipt = service.ensure_current(plan, plan["scope_head"], str(uuid4()))
        assert receipt["state"] == "pending"
        same = service.ensure_current(plan, plan["scope_head"], str(uuid4()))
        assert same["task_id"] == receipt["task_id"]
        service.run(case.company, receipt["task_id"])
        ready = service.get(case.company, receipt["task_id"], check_current=True)
        assert ready["state"] == "succeeded", ready
        with service.verified_download(case.company, ready["task_id"]) as verified:
            book = load_workbook(BytesIO(verified.stream.read()))
            assert book.active["B4"].value == "合成收款人"
            assert book.active["D4"].value == "00012345678901234567890"
            assert book.active["F4"].value == "1000.00"
            book.close()
        assert case.get(wage["business_id"])["businesses"] == before["businesses"]
        add(case, expense(case))
        assert service.get(case.company, ready["task_id"], check_current=True)["state"] == "expired"
    finally:
        service.close()


def test_bonus_labor_personal_reimbursement_and_pass_through_select_only_payable_slots(case):
    supplier = case.seed()
    wage, bonus, labor, bank = (
        unpaid_wage(),
        business_case("B17B"),
        business_case("B17L"),
        bank_object(),
    )
    labor["data"]["person_id"] = PERSON

    def fact(kind, data, **fields):
        return {
            "business_id": str(uuid4()),
            "kind": "operations." + kind,
            "period": "2026-01",
            "evidence_ids": [SOURCE],
            "data": data,
            **fields,
        }

    reimbursement = fact(
        "reimbursement",
        {
            "type": "acceptance",
            "payer_id": PERSON,
            "payer_kind": "employee",
            "sources": [
                {
                    "source": {"business_id": supplier["business_id"], "slot": "primary"},
                    "amount_fen": "20000",
                    "recipient_id": case.party,
                }
            ],
        },
    )
    through = fact(
        "pass_through", {"payer_id": case.party, "beneficiary_id": PERSON, "amount_fen": "100000"}
    )
    received = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-01",
        "effective_on": "2026-01-12",
        "evidence_ids": [SOURCE],
        "data": {
            "type": "settlement",
            "funds_account_id": bank["object_id"],
            "direction": "inflow",
            "amount_fen": "30000",
            "payment_method": "individual",
            "counterparty_id": case.party,
            "allocations": [
                {
                    "source": {"business_id": through["business_id"], "slot": "collection"},
                    "amount_fen": "30000",
                }
            ],
        },
    }
    returned = fact(
        "pass_through_return", {"pass_through_id": through["business_id"], "amount_fen": "20000"}
    )
    add(
        case,
        no_contributions(),
        wage,
        bonus,
        labor,
        reimbursement,
        through,
        received,
        returned,
        objects=[bank],
    )
    confirm_zero_tax(case, wage)
    confirm_zero_tax(case, labor)
    payee(case)
    payee(case, case.party, payee_name="合成付款人", payee_account="000666")
    result = inspect(owner(case), case, template_source_id=register_template(case))
    assert result["available"], result["issues"]
    assert {row["purpose"]: row["amount_fen"] for row in result["rows"]} == {
        "2026-01 工资": "100000",
        "2026-01 奖金": "90000",
        "2026-01 劳务报酬": "100000",
        "2026-01 代垫款归还": "20000",
        "2026-01 代收款转付": "80000",
        "2026-01 代收款退回": "20000",
    }
    assert result["total_fen"] == "410000"
    references = [ref for row in result["rows"] for ref in row["obligation_refs"]]
    assert len(references) == 6
    assert {ref["slot"] for ref in references} == {
        "net_pay",
        "reimbursement",
        "remittance",
        "refund",
    }
    assert not any(ref["business_id"] == supplier["business_id"] for ref in references)


def test_settled_person_does_not_require_a_bank_account_or_produce_an_empty_file(case):
    case.seed(expense=False)
    wage, bank = unpaid_wage(), bank_object()
    add(case, no_contributions(), wage, objects=[bank])
    add(case, payment(bank["object_id"], "2026-02", (wage["business_id"], "100000")))
    result = inspect(owner(case), case, template_source_id=register_template(case))
    assert result["total_fen"] == "0" and result["candidates"][0]["payable_fen"] == "0"
    assert result["issues"] == [] and result["rows"] == [] and not result["available"]


def test_unknown_tax_is_not_zero_for_bank_export_and_does_not_block_selected_reimbursement(case):
    case.seed(expense=False)
    wage, reimbursed = unpaid_wage(), expense(case)
    add(case, no_contributions(), wage, reimbursed)
    payee(case)
    module, template_id = owner(case), register_template(case)
    request = {"template_source_id": template_id}
    waiting = inspect(module, case, **request)
    assert not waiting["available"]
    assert (
        waiting["total_fen"] == "130000"
    )  # Formal debt is still readable, not erased or treated as paid.
    assert waiting["issues"] == [
        {
            "code": "payment_payable_composition_unconfirmed",
            "field": "/candidates/payable_fen",
            "message": waiting["issues"][0]["message"],
            "business_id": wage["business_id"],
        }
    ]
    only_expense = inspect(module, case, **request, business_ids=[reimbursed["business_id"]])
    assert only_expense["available"] and only_expense["total_fen"] == "30000"
    with case.kernel.storage.read_unit(case.company) as read:
        assert (
            module.prepare({"company_id": case.company, "period": "2026-01", **request}, read)[1]
            is None
        )
    confirm_zero_tax(case, wage)
    ready = inspect(module, case, **request)
    assert ready["available"] and ready["total_fen"] == "130000"
    assert ready["issues"] == []
    assert case.get(wage["business_id"])["businesses"][0]["fact"]["data"].get("tax_basis") is None


def test_initial_zero_tax_with_its_own_source_allows_the_known_payment_amount(case):
    case.seed(expense=False)
    wage, source_id = unpaid_wage(), str(uuid4())
    text = "软件验收专用已核对工资表：本笔工资已确认个税扣款0分。"
    saved = case.app.accounting_apply(
        case.request(
            sources=[
                {
                    "source_id": source_id,
                    "kind": "owner_statement",
                    "text": text,
                    "sha256": sha256(text.encode()).hexdigest(),
                    "archive_month": "2026-01",
                }
            ]
        )
    )
    assert saved["completion"] == "complete", saved
    wage["data"]["tax_basis"] = {
        "tax_period": "2026-01",
        "tax_fen": "0",
        "basis_type": "final_statement",
        "source": {"source_id": source_id, "location": "工资表已确认个税扣款"},
    }
    wage["evidence_ids"].append(source_id)
    add(case, no_contributions(), wage)
    payee(case)
    result = inspect(owner(case), case, template_source_id=register_template(case))
    assert result["available"] and result["total_fen"] == "100000" and result["issues"] == []


@pytest.mark.parametrize(
    "declared,expected,held", [("30000", "500000", "0"), ("40000", "490000", "10000")]
)
def test_existing_evidenced_disbursement_composition_including_zero_hold_is_ready(
    case, declared, expected, held
):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    declaration_fact, reference = declaration(wage, declared)
    wage["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    add(case, wage, employment, actual, declaration_fact)
    payee(case)
    result = inspect(owner(case), case, template_source_id=register_template(case))
    assert result["available"] and result["total_fen"] == expected
    with case.kernel.storage.read_unit(case.company) as read:
        position = create_people(ledger=case.kernel.ledger).remuneration_positions(
            [wage["business_id"]], "2026-01", read
        )[0]
    assert position["amounts"]["held_fen"] == held


def test_selected_prior_month_wage_and_expense_keep_original_month_in_bank_purpose(
    case, monkeypatch
):
    case.seed(expense=False)
    wage, reimbursed = unpaid_wage(), expense(case)
    add(case, no_contributions(), wage, reimbursed)
    confirm_zero_tax(case, wage)
    payee(case)
    module, source = owner(case), register_template(case)

    def no_discovery(*args, **kwargs):
        pytest.fail("Explicit source IDs must not search historical month membership.")

    monkeypatch.setattr(module.businesses, "find_businesses", no_discovery)
    plan = make_plan(
        module,
        case,
        source,
        period="2026-02",
        business_ids=[wage["business_id"], reimbursed["business_id"]],
    )
    assert plan["total_fen"] == "130000"
    assert {row["purpose"] for row in plan["rows"]} == {"2026-01 工资", "2026-01 费用报销"}
    assert current(module, case, plan)
