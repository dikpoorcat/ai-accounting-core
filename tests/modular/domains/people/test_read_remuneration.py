"""Exact public payroll activity, independent of roster filters or bank batch size."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.domains.people.public import create_people
from ai_accounting_modular.ledger.opening.public import opening_payroll_references
from ai_accounting_modular.ledger.public import ObligationPositionQuery
from tests.modular.application._working_case import case as case

from .test_application import replacement, request
from .test_people import PERSON, SOURCE, declaration, no_contributions
from .test_people import case as business_case
from .test_relationships import relationship


def add(book, *facts, objects=()):
    wire = request(book.company, *facts)
    wire["objects"].extend(objects)
    with book.kernel.storage.read_unit(book.company) as read:
        existing = {
            obj["object_id"]
            for obj in book.kernel.identity.get_batch(
                [item["object_id"] for item in wire["objects"]], read
            )
        }
    wire["objects"] = [item for item in wire["objects"] if item["object_id"] not in existing]
    saved = book.app.accounting_apply(wire)
    assert saved.get("completion") == "complete", saved
    return saved


def activity(book, month="2026-01", *, view="as_posted", **extra):
    scope = {
        "company_id": book.company,
        "period_from": month,
        "period_to": month,
        "view": view,
        **extra,
    }
    with book.kernel.storage.read_unit(book.company) as read:
        return create_people(ledger=book.kernel.ledger).remuneration_activity(scope, read)


def correct(book, fact, *, period=None, seq=1):
    wire = replacement(book.company, fact, seq=seq)
    if period is not None:
        wire["correction_period"] = period
    saved = book.app.accounting_apply(wire)
    assert saved.get("completion") == "complete", saved
    return saved


def wage_with_actuals():
    wage, employment, actual = (business_case(name) for name in ("B17A", "B14", "B16"))
    wage["data"].update(gross_fen="600000")
    wage["data"]["tax_basis"]["tax_fen"] = "30000"
    actual["data"]["items"] = [
        {
            "code": "social",
            "base_kind": "social_insurance",
            "state": "declared",
            "employee_fen": "50000",
            "employer_fen": "60000",
        },
        {
            "code": "housing",
            "base_kind": "housing_fund",
            "state": "declared",
            "employee_fen": "20000",
            "employer_fen": "30000",
        },
    ]
    return wage, employment, actual


def test_complete_costs_and_same_person_labor_are_formal_not_relationship_filters(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    bonus, labor, capital = (business_case(name) for name in ("B17B", "B17L", "B17P"))
    labor["data"].update(person_id=PERSON, gross_fen="200000")
    capital["data"].update(person_id=PERSON, gross_fen="300000")
    capital["data"]["tax_basis"]["tax_fen"] = "30000"
    relation = relationship(("labor", "2026-01-01"), ("regular", "2026-01-20"))
    add(case, wage, employment, actual, bonus, labor, capital, relation)
    result = activity(case)
    values = result.summary.amounts
    assert (values.gross_salary_fen, values.annual_bonus_fen) == (600000, 100000)
    assert (values.employee_social_fen, values.employee_housing_fen) == (50000, 20000)
    assert (values.employer_social_fen, values.employer_housing_fen) == (60000, 30000)
    assert (values.employee_cost_fen, values.personal_deduction_fen) == (790000, 110000)
    assert (values.net_salary_fen, values.labor_net_fen) == (590000, 470000)
    assert (values.expensed_labor_fen, values.capitalized_labor_fen, values.total_expense_fen) == (
        200000,
        300000,
        990000,
    )
    assert result.summary.outstanding_remuneration_fen == 1060000
    assert result.summary.outstanding_capitalized_labor_net_fen == 270000
    assert len(result.persons) == 1
    person = result.persons[0]
    assert person.has_payroll_activity and person.has_labor_activity
    with case.kernel.storage.read_unit(case.company) as read:
        member = create_people().relationships([PERSON], "2026-01", read)[0]
    assert member.employee_member is member.labor_member is True
    relation["data"]["segments"] = [{"state": "departed", "effective_from": "2026-01"}]
    correct(case, relation)
    assert activity(case) == result


def test_contribution_parameters_alone_are_not_another_payroll_cost(case):
    case.seed(expense=False)
    add(case, business_case("B14"), business_case("B16"))
    result = activity(case)
    assert result.summary.amounts.total_expense_fen == 0
    assert result.entries == result.positions == result.persons == ()


def test_employer_borne_shortfall_uses_the_adopted_split(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    wage["data"]["gross_fen"] = "10000"
    wage["data"]["tax_basis"]["tax_fen"] = "1000"
    employment["data"]["contribution_shortfall"] = "employer_borne"
    actual["data"]["items"][0].update(employee_fen="12000", employer_fen="3000")
    actual["data"]["items"][1].update(employee_fen="0", employer_fen="0")
    add(case, wage, employment, actual)
    values = activity(case).summary.amounts
    assert (values.employee_social_fen, values.employer_social_fen) == (9000, 6000)
    assert (values.gross_salary_fen, values.net_salary_fen, values.employee_cost_fen) == (
        10000,
        0,
        16000,
    )


def test_closed_type_correction_uses_old_and_new_formal_bases(case):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add(case, no_contributions(), wage)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    before = activity(case)
    labor = business_case("B17L")
    labor["business_id"] = wage["business_id"]
    labor["data"].update(person_id=PERSON, gross_fen="80000")
    correct(case, labor, period="2026-02")
    result = activity(case, "2026-02")
    assert [(item.remuneration_type, item.amounts.cost_fen) for item in result.entries] == [
        ("wage", -100000),
        ("labor", 80000),
    ]
    assert result.summary.amounts.total_expense_fen == -20000
    assert len(set(item.entry_id for item in result.entries)) == 2
    assert all(item.business_period == "2026-01" for item in result.entries)
    assert result.entries[0].effective["state"] == "frozen"
    assert {(source.remuneration_type, source.amounts.cost_fen) for source in result.sources} == {
        ("wage", 100000),
        ("labor", 80000),
    }
    assert activity(case) == before
    current = activity(case, view="current")
    assert (
        current.summary.amounts.gross_salary_fen,
        current.summary.amounts.expensed_labor_fen,
    ) == (
        0,
        80000,
    )


def test_closed_capitalized_labor_reclassification_does_not_make_a_second_payment(case):
    case.seed(expense=False)
    capital = business_case("B17P")
    add(case, capital)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    changed = deepcopy(capital)
    changed["data"]["destination"] = {"type": "expense", "area": "administration"}
    correct(case, changed, period="2026-02")
    result = activity(case, "2026-02")
    values = result.summary.amounts
    assert (values.capitalized_labor_fen, values.expensed_labor_fen) == (-100000, 100000)
    assert values.labor_net_fen == 0
    assert result.summary.net_settlements.money_outflow_fen == 0
    assert result.entries[0].project_id is not None and result.entries[1].project_id is None


def test_missing_reader_dependency_and_stale_scope_are_explicit(case):
    case.seed(expense=False)
    scope = {
        "company_id": case.company,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "as_posted",
    }
    with case.kernel.storage.read_unit(case.company) as read:
        with pytest.raises(ContractError) as error:
            create_people().remuneration_activity(scope, read)
        assert error.value.code == "unsupported_business"
    first = activity(case)
    add(case, business_case("B17L"))
    with pytest.raises(ContractError) as error:
        activity(case, expected_head=first.version)
    assert error.value.code == "snapshot_changed"


def bank_object():
    return {
        "object_id": str(uuid4()),
        "kind": "bank_account",
        "name": "合成工资银行",
        "expected_seq": 0,
        "data": {
            "bank_name": "合成银行",
            "account_number": "synthetic-payroll",
            "currency": "CNY",
            "owner": "company",
        },
    }


def payment(account, month, *allocations, amount=None):
    return {
        "business_id": str(uuid4()),
        "kind": "money.movement",
        "period": month,
        "effective_on": month + "-20",
        "evidence_ids": [SOURCE],
        "data": {
            "type": "settlement",
            "funds_account_id": account,
            "direction": "outflow",
            "payment_method": "individual",
            "amount_fen": amount or str(sum(int(value) for _, value in allocations)),
            "allocations": [
                {"source": {"business_id": source, "slot": "net_pay"}, "amount_fen": value}
                for source, value in allocations
            ],
        },
    }


def test_prior_wage_and_capitalized_labor_unpaid_survive_a_month_with_only_real_payments(case):
    case.seed(expense=False)
    wage, capital = business_case("B17Z"), business_case("B17P")
    capital["data"].update(person_id=PERSON, gross_fen="80000")
    capital["data"]["tax_basis"]["tax_fen"] = "0"
    bank = bank_object()
    paid = payment(
        bank["object_id"],
        "2026-02",
        (wage["business_id"], "60000"),
        (capital["business_id"], "30000"),
    )
    add(case, no_contributions(), wage, capital, paid, objects=[bank])
    result = activity(case, "2026-02")
    assert result.entries == () and result.summary.amounts.total_expense_fen == 0
    assert result.summary.net_settlements.money_outflow_fen == 90000
    assert (
        result.summary.outstanding_employee_net_fen,
        result.summary.outstanding_labor_net_fen,
        result.summary.outstanding_capitalized_labor_net_fen,
        result.summary.outstanding_remuneration_fen,
    ) == (40000, 50000, 50000, 90000)
    assert all(item.payroll_period == "2026-01" for item in result.settlements)
    assert len(result.persons) == 1 and not result.persons[0].has_payroll_activity
    assert not result.persons[0].has_labor_activity


def test_gross_payroll_bank_occurrence_is_not_the_net_pay_use_allocation(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    bank = bank_object()
    paid = payment(bank["object_id"], "2026-01", (wage["business_id"], "500000"), amount="600000")
    paid["data"] = {
        "type": "payroll_reserve",
        "funds_account_id": bank["object_id"],
        "amount_fen": "600000",
        "payroll_period": "2026-01",
        "allocations": paid["data"]["allocations"],
        "reserve_expense_fen": "100000",
        "return_period": "2026-01",
        "return_source": {"source_id": SOURCE},
    }
    add(case, wage, employment, actual, paid, objects=[bank])
    result = activity(case)
    assert result.summary.amounts.employee_cost_fen == 690000
    assert result.summary.net_settlements.money_outflow_fen == 500000
    cash = [item for item in result.settlements if item.event.movement is not None]
    assert len(cash) == 1
    assert cash[0].event.use.value["amount_fen"] == "500000"
    assert cash[0].event.movement.value["amount_fen"] == "600000"


def test_establishing_labor_tax_splits_the_same_cost_and_keeps_the_tax_state(case):
    case.seed(expense=False)
    labor = business_case("B17L")
    add(case, labor)
    first = activity(case)
    assert first.entries[0].tax_state == "not_withheld_not_filed"
    assert first.entries[0].amounts.tax_fen == 0
    changed = deepcopy(labor)
    changed["data"].update(
        withholding_state="deduction_established",
        tax_basis=business_case("B17B")["data"]["tax_basis"],
    )
    correct(case, changed)
    result = activity(case)
    assert result.summary.amounts.expensed_labor_fen == 100000
    assert (result.summary.amounts.labor_tax_fen, result.summary.amounts.labor_net_fen) == (
        10000,
        90000,
    )
    assert result.entries[0].tax_state == "final_statement"


def test_more_than_500_remunerations_are_all_in_the_complete_month(case):
    case.seed(expense=False)
    facts = []
    for _ in range(501):
        labor = business_case("B17L")
        labor["business_id"] = str(uuid4())
        labor["data"].update(person_id=str(uuid4()), gross_fen="1")
        facts.append(labor)
    for offset in range(0, len(facts), 250):
        add(case, *facts[offset : offset + 250])
    result = activity(case)
    assert len(result.entries) == len(result.positions) == len(result.persons) == 501
    assert len(result.sources) == len(result.roster) == 501
    assert result.summary.amounts.expensed_labor_fen == 501
    assert result.summary.outstanding_remuneration_fen == 501


def test_opening_net_pay_is_attributed_to_the_person_not_the_payment_recipient(case):
    bank, recipient = bank_object(), str(uuid4())
    package = {
        "business_id": str(uuid4()),
        "kind": "opening.package",
        "period": "2026-01",
        "evidence_ids": [SOURCE],
        "data": {
            "basis": "existing_ledger",
            "members": [
                {
                    "member_id": str(uuid4()),
                    "evidence_ids": [SOURCE],
                    "data": {
                        "type": "bank",
                        "funds_account_id": bank["object_id"],
                        "balance_fen": "10000",
                    },
                },
                {
                    "member_id": str(uuid4()),
                    "evidence_ids": [SOURCE],
                    "data": {
                        "type": "payroll_payable",
                        "person_id": PERSON,
                        "recipient_id": recipient,
                        "payroll_period": "2025-12",
                        "component": "net",
                        "amount_fen": "10000",
                    },
                },
            ],
        },
    }
    objects = [
        bank,
        *[
            {"object_id": person_id, "kind": "person", "name": label, "expected_seq": 0, "data": {}}
            for person_id, label in ((PERSON, "合成员工"), (recipient, "有据代收人"))
        ],
    ]
    add(case, package, objects=objects)
    with case.kernel.storage.read_unit(case.company) as read:
        refs = case.kernel.ledger.get_result_refs([package["business_id"]], read)
        content = case.kernel.ledger.get_adopted_contents(refs, read)[0]
    source = opening_payroll_references(content)[0]
    paid = payment(bank["object_id"], "2026-02", (package["business_id"], "4000"))
    paid["data"]["allocations"][0]["source"] = source.source
    add(case, paid)
    result = activity(case, "2026-02")
    assert result.entries == () and result.summary.amounts.total_expense_fen == 0
    assert result.summary.net_settlements.money_outflow_fen == 4000
    assert result.summary.outstanding_employee_net_fen == 6000
    assert [person.person_id for person in result.persons] == [PERSON]
    assert result.positions[0].payroll_period == "2025-12"
    assert result.positions[0].position.definition.value["party_id"] == recipient
    assert result.sources == ()
    assert [(person.person_id, person.roles) for person in result.roster] == [
        (PERSON, ("employee",))
    ]


def paid_wage_recovery(case):
    case.seed(expense=False)
    wage, bank = business_case("B17Z"), bank_object()
    paid = payment(bank["object_id"], "2026-01", (wage["business_id"], "100000"))
    add(case, no_contributions(), wage, paid, objects=[bank])
    wage["data"]["gross_fen"] = "80000"
    original = {"business_id": wage["business_id"], "slot": "net_pay"}
    claim = {
        "business_id": str(uuid4()),
        "kind": "operations.overpayment",
        "period": "2026-02",
        "effective_on": "2026-02-10",
        "evidence_ids": [SOURCE],
        "data": {"source": original, "amount_fen": "20000"},
    }
    wire = replacement(case.company, wage)
    wire["mutations"].append({"op": "create", "expected_seq": 0, "fact": claim})
    result = case.app.accounting_apply(wire)
    assert result.get("completion") == "complete", result
    return bank, original, claim


def test_actual_overpaid_wage_recovery_keeps_direction_and_formal_parent_without_new_cost(case):
    bank, original, claim = paid_wage_recovery(case)

    def inspect(month, remaining, received=0):
        result = activity(case, month)
        assert result.summary.amounts.total_expense_fen == 0
        assert result.summary.outstanding_remuneration_fen == 0
        assert [person.person_id for person in result.persons] == [PERSON]
        assert (
            result.summary.net_settlements.money_outflow_fen,
            result.summary.net_settlements.money_inflow_fen,
        ) == (0, received)
        recovery = next(item for item in result.positions if item.recovery)
        assert recovery.parent_source == original
        assert recovery.position.definition.reference == {
            "business_id": claim["business_id"],
            "slot": "recovery",
        }
        assert recovery.position.definition.value["direction"] == "receivable"
        assert recovery.position.amounts.remaining_fen == remaining
        net = next(
            item for item in result.positions if not item.recovery and item.component == "net_pay"
        )
        assert (
            net.position.amounts.amount_fen,
            net.position.amounts.money_settled_fen,
            net.position.amounts.excess_reclassified_fen,
            net.position.amounts.remaining_fen,
        ) == (80000, 100000, 20000, 0)
        if received:
            returned = next(item for item in result.settlements if item.recovery)
            assert returned.parent_source == original
            assert returned.event.use.value["amount_fen"] == str(received)
            assert returned.event.movement.value["direction"] == "inflow"
        return result

    established = inspect("2026-02", 20000)
    quiet = inspect("2026-03", 20000)
    assert quiet.entries == quiet.settlements == ()
    for month, received, remaining in (("2026-04", 5000, 15000), ("2026-05", 15000, 0)):
        refund = payment(bank["object_id"], month, (claim["business_id"], str(received)))
        refund["data"]["direction"] = "inflow"
        refund["data"]["allocations"][0]["source"]["slot"] = "recovery"
        add(case, refund)
        inspect(month, remaining, received)
    assert activity(case, "2026-02") == established
    assert activity(case, "2026-03") == quiet
    settled = activity(case, "2026-06")
    assert settled.positions == settled.settlements == ()
    assert [person.person_id for person in settled.persons] == [PERSON]
    assert not settled.persons[0].has_payroll_activity
    assert settled.summary.outstanding_remuneration_fen == 0


def test_nonpayroll_overpayment_is_not_classified_as_a_payroll_recovery(case):
    cost = case.seed()
    bank = bank_object()
    paid = payment(bank["object_id"], "2026-01", (cost["business_id"], "100000"))
    paid["data"]["allocations"][0]["source"]["slot"] = "primary"
    add(case, paid, objects=[bank])
    cost["data"]["amount_fen"] = "80000"
    claim = {
        "business_id": str(uuid4()),
        "kind": "operations.overpayment",
        "period": "2026-02",
        "evidence_ids": [SOURCE],
        "data": {
            "source": {"business_id": cost["business_id"], "slot": "primary"},
            "amount_fen": "20000",
        },
    }
    wire = replacement(case.company, cost)
    wire["mutations"].append({"op": "create", "expected_seq": 0, "fact": claim})
    saved = case.app.accounting_apply(wire)
    assert saved.get("completion") == "complete", saved
    for month in ("2026-02", "2026-03"):
        result = activity(case, month)
        assert result.positions == result.settlements == result.persons == ()


def test_claim_of_a_claim_is_not_recursively_classified_as_payroll(case):
    bank, original, claim = paid_wage_recovery(case)
    refund = payment(bank["object_id"], "2026-03", (claim["business_id"], "25000"))
    refund["data"]["direction"] = "inflow"
    refund["data"]["allocations"][0]["source"]["slot"] = "recovery"
    followup = {
        "business_id": str(uuid4()),
        "kind": "operations.overpayment",
        "period": "2026-03",
        "effective_on": "2026-03-21",
        "evidence_ids": [SOURCE],
        "data": {
            "source": {"business_id": claim["business_id"], "slot": "recovery"},
            "amount_fen": "5000",
        },
    }
    add(case, refund, followup)
    result = activity(case, "2026-03")
    recovered = [item for item in result.positions if item.recovery]
    assert [item.parent_source for item in recovered] == [original]
    assert recovered[0].position.definition.reference["business_id"] == claim["business_id"]
    assert recovered[0].position.amounts.remaining_fen == 0
    assert result.summary.net_settlements.money_inflow_fen == 25000
    assert result.summary.outstanding_remuneration_fen == 0
    with case.kernel.storage.read_unit(case.company) as read:
        payable = case.kernel.ledger.query_obligation_positions(
            ObligationPositionQuery("2026-04", business_ids=(followup["business_id"],)),
            PageQuery(),
            read,
        ).page.items[0]
    assert payable.definition.value["direction"] == "payable"
    assert payable.amounts.remaining_fen == 5000
    unrelated = activity(case, "2026-04")
    assert unrelated.positions == unrelated.settlements == ()
    assert [person.person_id for person in unrelated.persons] == [PERSON]
    assert not unrelated.persons[0].has_payroll_activity
    assert unrelated.summary.outstanding_remuneration_fen == 0


def test_disbursement_hold_remains_part_of_the_formal_net_obligation(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    declared, reference = declaration(wage, "40000")
    wage["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    add(case, wage, employment, actual, declared)
    result = activity(case)
    assert result.summary.amounts.employee_cost_fen == 690000
    assert result.summary.outstanding_employee_net_fen == 500000
    assert result.entries[0].amounts.net_fen == 500000
    assert result.entries[0].amounts.held_fen == 10000
    assert result.entries[0].amounts.disbursement_target_fen == 490000


def test_frozen_source_location_review_keeps_the_formal_head_without_new_month_activity(case):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add(case, no_contributions(), wage)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    before = case.get(wage["business_id"])["businesses"][0]["results"][0]["reference"]
    wage["data"]["tax_basis"]["source"] = {
        "source_id": SOURCE,
        "location": "复核后明确的原件位置",
    }
    saved = correct(case, wage, period="2026-02")
    assert saved["accounting"] == "unchanged"
    after = case.get(wage["business_id"])["businesses"][0]["results"][0]["reference"]
    assert after == before
    result = activity(case, "2026-02")
    assert result.entries == () and not result.persons[0].has_payroll_activity
    assert result.summary.amounts.total_expense_fen == 0


def test_equivalent_actual_declaration_evidence_does_not_repeat_frozen_wage_activity(case):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add(case, no_contributions(), wage)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    before = case.get(wage["business_id"])["businesses"][0]["results"][0]
    declared, reference = declaration(wage, "0")
    wage["data"]["tax_basis"]["pooled_source"] = reference
    wire = replacement(case.company, wage)
    wire["correction_period"] = "2026-02"
    wire["mutations"].append({"op": "create", "expected_seq": 0, "fact": declared})
    saved = case.app.accounting_apply(wire)
    assert saved.get("completion") == "complete", saved
    after = case.get(wage["business_id"])["businesses"][0]["results"][0]
    assert before["target"] == after["target"] and after["vouchers"] == []
    assert before["reference"]["result_id"] != after["reference"]["result_id"]
    result = activity(case, "2026-02")
    assert result.entries == () and not result.persons[0].has_payroll_activity
    assert result.summary.amounts.total_expense_fen == 0
    assert result.summary.outstanding_employee_net_fen == 100000
    assert len(result.sources) == 1
    assert result.sources[0].reference == after["reference"]
    assert result.sources[0].amounts.gross_fen == 100000


def test_equal_target_expense_reclassification_retains_both_formal_legs(case):
    case.seed(expense=False)
    wage = business_case("B17Z")
    add(case, no_contributions(), wage)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    before = case.get(wage["business_id"])["businesses"][0]["results"][0]
    wage["data"]["expense_area"] = "sales"
    correct(case, wage, period="2026-02")
    after = case.get(wage["business_id"])["businesses"][0]["results"][0]
    assert before["target"] == after["target"]
    result = activity(case, "2026-02")
    assert [(entry.expense_area, entry.amounts.cost_fen) for entry in result.entries] == [
        ("administration", -100000),
        ("sales", 100000),
    ]
    assert result.persons[0].has_payroll_activity


def test_quiet_month_roster_keeps_settled_roles_but_sources_load_only_referenced_labor(
    case, monkeypatch
):
    case.seed(expense=False)
    wage, capital = business_case("B17Z"), business_case("B17P")
    capital["data"].update(person_id=PERSON, gross_fen="80000")
    capital["data"]["tax_basis"]["tax_fen"] = "0"
    bank = bank_object()
    paid = payment(bank["object_id"], "2026-01", (wage["business_id"], "100000"))
    add(case, no_contributions(), wage, capital, paid, objects=[bank])
    original = case.kernel.ledger.get_adopted_contents

    def scoped(refs, read):
        assert all(ref["business_id"] != wage["business_id"] for ref in refs)
        return original(refs, read)

    monkeypatch.setattr(case.kernel.ledger, "get_adopted_contents", scoped)
    result = activity(case, "2026-02")
    assert result.entries == result.settlements == ()
    assert [(person.person_id, person.roles) for person in result.roster] == [
        (PERSON, ("employee", "labor"))
    ]
    assert len(result.sources) == 1
    source = result.sources[0]
    assert source.reference["business_id"] == capital["business_id"]
    assert (source.business_period, source.remuneration_type, source.tax_state) == (
        "2026-01",
        "labor",
        "final_statement",
    )
    assert (source.amounts.gross_fen, source.amounts.tax_fen, source.amounts.net_fen) == (
        80000,
        0,
        80000,
    )
    assert source.project_id == capital["data"]["destination"]["project_id"]
    assert result.summary.outstanding_capitalized_labor_net_fen == 80000


def test_missing_opening_keeps_known_monthly_pay_and_cash_but_cumulative_unpaid_unknown(case):
    wage, labor = business_case("B17Z"), business_case("B17L")
    labor["data"]["person_id"] = PERSON
    bank = bank_object()
    paid = payment(bank["object_id"], "2026-01", (wage["business_id"], "40000"))
    add(case, no_contributions(), wage, labor, paid, objects=[bank])
    result = activity(case)
    assert result.summary.amounts.gross_salary_fen == 100000
    assert result.summary.amounts.labor_gross_fen == 100000
    assert result.summary.net_settlements.money_outflow_fen == 40000
    for summary in (result.summary, result.persons[0].summary):
        assert summary.outstanding_employee_net_fen is None
        assert summary.outstanding_labor_net_fen is None
        assert summary.outstanding_capitalized_labor_net_fen is None
        assert summary.outstanding_remuneration_fen is None
    assert {issue["field"] for issue in result.issues} == {
        "/accounts/221101/all/ending",
        "/accounts/224104/all/ending",
    }
    wage_position = next(
        item
        for item in result.positions
        if item.position.definition.reference["business_id"] == wage["business_id"]
        and item.component == "net_pay"
    )
    assert wage_position.position.amounts.remaining_fen == 60000


@pytest.mark.parametrize("has_opening", (True, False))
def test_settled_wage_in_a_quiet_month_keeps_the_person_and_known_or_unknown_unpaid(
    case, monkeypatch, has_opening
):
    if has_opening:
        case.seed(expense=False)
    wage, bank = business_case("B17Z"), bank_object()
    paid = payment(bank["object_id"], "2026-01", (wage["business_id"], "100000"))
    add(case, no_contributions(), wage, paid, objects=[bank])
    original = case.kernel.ledger.get_adopted_contents

    def scoped(refs, read):
        assert all(ref["business_id"] != wage["business_id"] for ref in refs)
        return original(refs, read)

    monkeypatch.setattr(case.kernel.ledger, "get_adopted_contents", scoped)
    result = activity(case, "2026-02")
    assert result.entries == result.positions == result.settlements == result.sources == ()
    assert len(result.roster) == len(result.persons) == 1
    person = result.persons[0]
    assert person.person_id == PERSON and not person.has_payroll_activity
    assert person.summary.amounts.total_expense_fen == 0
    assert person.summary.net_settlements.money_outflow_fen == 0
    expected = 0 if has_opening else None
    assert person.summary.outstanding_employee_net_fen == expected
    assert person.summary.outstanding_labor_net_fen == expected
    assert person.summary.outstanding_remuneration_fen == expected
