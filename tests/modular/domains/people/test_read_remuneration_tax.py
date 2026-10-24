"""Later tax targets retain original payroll, exact sources, and real payment meanings."""

from copy import deepcopy
from uuid import uuid4

import pytest

from tests.modular.application._working_case import case as case

from .test_application import request
from .test_people import SOURCE, declaration, no_contributions
from .test_people import case as business_case
from .test_read_remuneration import activity, add, bank_object, correct, payment, wage_with_actuals


def tax_target(wage, amount, *, month="2026-02", tax_period=None, basis_type="final_statement"):
    return {
        "business_id": str(uuid4()),
        "kind": "people.remuneration_tax",
        "period": month,
        "evidence_ids": [SOURCE],
        "data": {
            "remuneration_id": wage["business_id"],
            "tax_period": tax_period or month,
            "basis_type": basis_type,
            "components": [
                {
                    "tax_fen": amount,
                    "source": {"source_id": SOURCE, "location": "合成后续薪酬税款"},
                }
            ],
        },
    }


def unpaid_wage():
    wage = business_case("B17Z")
    wage["data"].pop("tax_basis")
    return wage


def test_later_tax_is_its_own_month_and_not_another_wage_cost_or_payment(case):
    case.seed(expense=False)
    wage = unpaid_wage()
    add(case, no_contributions(), wage)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    january = activity(case)
    tax = tax_target(wage, "10000")
    add(case, tax)
    assert activity(case) == january
    assert activity(case, view="current").compositions[0].tax_targets == ()
    february = activity(case, "2026-02")
    assert len(february.entries) == 1
    entry = february.entries[0]
    assert entry.parent_remuneration_source == january.sources[0].source
    assert entry.amounts.cost_fen == entry.amounts.gross_fen == 0
    assert (entry.amounts.tax_fen, entry.amounts.net_fen) == (10000, -10000)
    assert february.persons[0].payroll_business_ids == (wage["business_id"],)
    assert february.persons[0].payroll_periods == ("2026-01",)
    assert february.summary.amounts.total_expense_fen == 0
    assert february.summary.outstanding_employee_net_fen == 90000
    assert february.summary.net_settlements.money_outflow_fen == 0
    assert february.summary.net_settlements.transfer_fen == 0
    assert len(february.sources) == 1
    assert february.sources[0] == january.sources[0]
    composition = february.compositions[0]
    assert composition.remuneration == january.sources[0].source
    assert [item["business_id"] for item in composition.tax_targets] == [tax["business_id"]]
    assert (composition.amounts.tax_fen, composition.amounts.net_fen) == (10000, 90000)
    assert composition.amounts.disbursement_target_fen == 90000

    bank = bank_object()
    paid = payment(bank["object_id"], "2026-02", (wage["business_id"], "90000"))
    add(case, paid, objects=[bank])
    settled = activity(case, "2026-02")
    assert settled.summary.outstanding_employee_net_fen == 0
    assert settled.summary.net_settlements.money_outflow_fen == 90000
    assert settled.summary.net_settlements.transfer_fen == 0
    assert settled.summary.amounts.total_expense_fen == 0


@pytest.mark.parametrize("initial, target", [(None, "0"), ("10000", "10000")])
def test_zero_increment_keeps_evidenced_tax_confirmation_without_duplicate_deduction(
    case, initial, target
):
    case.seed(expense=False)
    wage = unpaid_wage()
    if initial is not None:
        wage["data"]["tax_basis"] = {
            **business_case("B17Z")["data"]["tax_basis"],
            "tax_fen": initial,
            "tax_period": "2026-02",
        }
    add(case, no_contributions(), wage)
    tax = tax_target(wage, target, basis_type="actual_withholding")
    add(case, tax)
    result = activity(case, "2026-02")
    assert result.summary.amounts.total_expense_fen == 0
    assert result.summary.net_settlements.transfer_fen == 0
    assert result.sources[0].amounts.tax_fen == int(initial or "0")
    composition = result.compositions[0]
    assert composition.tax_state == "actual_withholding"
    assert len(composition.tax_targets) == 1
    assert composition.tax_targets[0]["business_id"] == tax["business_id"]
    assert composition.amounts.tax_fen == int(target)
    assert composition.amounts.net_fen == 100000 - int(target)
    assert not any(
        item.position.definition.reference["business_id"] == tax["business_id"]
        for item in result.positions
    )


@pytest.mark.parametrize(
    "target, net, held, ceiling",
    [("35000", 495000, 5000, 490000), ("40000", 490000, 0, 490000), ("45000", 485000, 0, 485000)],
)
def test_formal_tax_absorbs_only_the_same_period_hold_once(case, target, net, held, ceiling):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    declared, reference = declaration(wage, "40000")
    wage["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    add(case, wage, employment, actual, declared)
    tax = tax_target(wage, target, tax_period="2026-01")
    add(case, tax)
    result = activity(case, "2026-02")
    assert result.sources[0].amounts.held_fen == 10000
    assert result.sources[0].amounts.disbursement_target_fen == 490000
    composition = result.compositions[0].amounts
    assert (composition.net_fen, composition.held_fen, composition.disbursement_target_fen) == (
        net,
        held,
        ceiling,
    )


def test_other_tax_period_does_not_consume_the_original_disbursement_hold(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    declared, reference = declaration(wage, "40000")
    wage["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    add(case, wage, employment, actual, declared)
    add(case, tax_target(wage, "5000", tax_period="2026-02"))
    composed = activity(case, "2026-02").compositions[0].amounts
    assert (composed.net_fen, composed.held_fen, composed.disbursement_target_fen) == (
        495000,
        10000,
        485000,
    )


def test_tax_discovery_uses_only_involved_payroll_refs_and_reuses_exact_reads(case, monkeypatch):
    case.seed(expense=False)
    wage = unpaid_wage()
    add(case, no_contributions(), wage, tax_target(wage, "10000"))
    ledger = case.kernel.ledger
    targets, bodies = ledger.query_adopted_targets, ledger.get_adopted_contents
    discovered, loaded = [], []

    def scoped(scope, kinds, page, read, **kwargs):
        if kinds == ("people.remuneration_tax",):
            assert kwargs["source_refs"] == ({"business_id": wage["business_id"], "slot": "main"},)
            assert scope["period_to"] == "2026-02"
            discovered.append(kwargs["source_refs"])
        return targets(scope, kinds, page, read, **kwargs)

    def exact(refs, read):
        refs = tuple(refs)
        loaded.extend((item["result_id"], item["generation"]) for item in refs)
        return bodies(refs, read)

    monkeypatch.setattr(ledger, "query_adopted_targets", scoped)
    monkeypatch.setattr(ledger, "get_adopted_contents", exact)
    result = activity(case, "2026-02")
    assert len(discovered) == 1
    assert len(loaded) == len(set(loaded))
    assert result.compositions[0].amounts.tax_fen == 10000


def test_closed_wage_correction_composes_only_the_selected_wage_not_its_old_negative_leg(case):
    case.seed(expense=False)
    wage = business_case("B17Z")
    wage["data"]["tax_basis"]["tax_fen"] = "10000"
    tax = tax_target(wage, "20000", tax_period="2026-01")
    add(case, no_contributions(), wage, tax)
    checked = case.check("2026-02")
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    february = activity(case, "2026-02")
    changed = deepcopy(wage)
    changed["data"]["tax_basis"]["tax_fen"] = "8000"
    correct(case, changed, period="2026-03")
    result = activity(case, "2026-03")
    assert len(result.sources) == 2
    assert {item.amounts.tax_fen for item in result.sources} == {8000, 10000}
    assert len(result.compositions) == 1
    composition = result.compositions[0]
    selected = next(item for item in result.sources if item.source == composition.remuneration)
    assert selected.amounts.tax_fen == 8000
    assert composition.amounts.tax_fen == 20000
    assert composition.amounts.net_fen == composition.amounts.disbursement_target_fen == 80000
    assert len(composition.tax_targets) == 1
    assert result.summary.amounts.total_expense_fen == 0
    assert result.summary.amounts.individual_income_tax_fen == 0
    assert result.summary.amounts.net_salary_fen == 0
    assert activity(case, "2026-02") == february


def test_tax_transfer_does_not_weaken_the_special_whole_gross_payroll_reserve_model(case):
    case.seed(expense=False)
    wage, bank = unpaid_wage(), bank_object()
    add(case, no_contributions(), wage, tax_target(wage, "10000"), objects=[bank])
    combination = {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": "2026-02",
        "effective_on": "2026-02-20",
        "evidence_ids": [SOURCE],
        "data": {
            "type": "payroll_reserve",
            "funds_account_id": bank["object_id"],
            "payroll_period": "2026-01",
            "return_period": "2026-02",
            "return_source": {"source_id": SOURCE, "location": "合成备用金返入"},
            "amount_fen": "100000",
            "reserve_expense_fen": "10000",
            "allocations": [
                {
                    "source": {"business_id": wage["business_id"], "slot": "net_pay"},
                    "amount_fen": "90000",
                }
            ],
        },
    }
    wire = request(case.company, combination)
    wire["objects"] = wire["sources"] = []
    rejected = case.app.accounting_apply(wire)
    assert rejected.get("completion") == "not_saved", rejected
    assert rejected["error"] in {"dependency_conflict", "unsupported_business"}
    result = activity(case, "2026-02")
    assert result.summary.net_settlements.money_outflow_fen == 0
    assert result.summary.outstanding_employee_net_fen == 90000
