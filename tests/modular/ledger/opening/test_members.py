"""Strict components and necessary boundaries, verified through the real application."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.ledger.opening.public import create_opening

from ._support import (
    BANK,
    FUND,
    PARTY,
    PERSON,
    PLATFORM,
    assemble,
    bank_and_equity,
    fact,
    get,
    loan_opening,
    member,
    opening,
    replace,
    request,
    result,
    source,
)


def test_all_obligation_tax_payroll_and_equity_variants_have_public_current_resources(tmp_path):
    app, company = assemble(tmp_path)
    members = [member({"type": "bank", "funds_account_id": BANK, "balance_fen": "1000"})]
    for nature in (
        "customer_receivable",
        "supplier_service_payable",
        "supplier_administration_payable",
        "supplier_sales_payable",
        "employee_reimbursement",
        "owner_reimbursement",
        "deposit_receivable",
        "deposit_payable",
        "other_receivable",
        "other_payable",
    ):
        members.append(
            member(
                {
                    "type": "obligation",
                    "counterparty_id": PERSON if "reimbursement" in nature else PARTY,
                    "nature": nature,
                    "amount_fen": "100",
                    "original_business_reference": nature,
                }
            )
        )
    for tax in ("vat", "surtax", "individual_income_tax", "enterprise_income_tax"):
        for kind in ("payable", "refundable"):
            members.append(
                member(
                    {
                        "type": "tax",
                        "authority_id": PARTY,
                        "tax_kind": tax,
                        "balance_kind": kind,
                        "amount_fen": "100",
                        "tax_period": "2026-06",
                    }
                )
            )
    for component in (
        "net",
        "tax",
        "employee_social",
        "employee_housing",
        "employer_social",
        "employer_housing",
    ):
        members.append(
            member(
                {
                    "type": "payroll_payable",
                    "person_id": PERSON,
                    "recipient_id": PERSON if component == "net" else PARTY,
                    "payroll_period": "2026-05" if component == "tax" else "2026-06",
                    "component": component,
                    "amount_fen": "100",
                    **({"tax_period": "2026-06"} if component == "tax" else {}),
                }
            )
        )
    members.append(
        member(
            {
                "type": "money_fund",
                "fund_id": FUND,
                "counterparty_id": PARTY,
                "original_lot_reference": "prior-lot",
                "cost_fen": "500",
                "classification": "readily_redeemable_held_not_over_one_year",
                "cost_basis": "documented_cost",
            }
        )
    )
    for kind, value in (
        ("paid_in_capital", "100"),
        ("capital_reserve", "100"),
        ("surplus_reserve", "100"),
        ("retained_earnings", "200"),
    ):
        members.append(
            member(
                {
                    "type": "equity",
                    "equity_kind": kind,
                    "holder_or_basis_id": PARTY,
                    "balance_fen": value,
                }
            )
        )
    item = opening(*members)
    actual = app.accounting_apply(request(company, item))
    assert actual.get("completion") == "complete", actual.get("message", actual)
    outcome = result(app, company, item)
    assert outcome["vouchers"] == []
    assert outcome["target"]["values"] == {
        "bookkeeping_start": "2026-07",
        "debit_fen": "2200",
        "credit_fen": "2200",
        "member_count": 30,
    }
    quantities = outcome["basis"]["adopted_quantities"]
    refs = [q["source"] for q in quantities if q["type"] == "obligation"]
    with app.storage.read_unit(company) as read:
        definitions = app.ledger.resource_definitions(refs, "obligation", read)
        assert len(definitions) == 24
        taxes = [d.value for d in definitions if d.value["category"] == "tax"]
        assert {d["business_period"] for d in taxes} == {"2026-06"}
        assert {d["account_code"] for d in taxes if d["direction"] == "receivable"} == {"1221"}
        assert all(d.value["amount_fen"] == "100" for d in definitions)
        withholding = next(d for d in definitions if d.value["category"] == "withholding")
        assert withholding.value["business_period"] == "2026-06"
        ambiguous = {
            "member:" + entry["member_id"]
            for entry in members
            if (entry["data"]["type"] == "payroll_payable" and entry["data"]["component"] != "tax")
            or (
                entry["data"]["type"] == "obligation"
                and entry["data"]["nature"]
                in {
                    "employee_reimbursement",
                    "owner_reimbursement",
                    "other_receivable",
                    "other_payable",
                }
            )
        }
        assert all(
            "cashflow_weights" not in definition.value
            for definition in definitions
            if definition.reference["slot"] in ambiguous
        )
        assert all(
            "cashflow_weights" in definition.value
            for definition in definitions
            if definition.reference["slot"] not in ambiguous
        )
        assert create_opening().query_current(
            {
                "company_id": company,
                "family": "opening",
                "period_from": "2026-07",
                "period_to": "2026-07",
                "limit": 20,
                "object_id": FUND,
            },
            read,
        ) == (item,)


@pytest.mark.parametrize(
    "fault",
    [
        "same_member",
        "same_bank",
        "fake_kind",
        "current_tax",
        "current_payroll",
        "negative_capital",
        "missing_object",
        "overflow",
    ],
)
def test_invalid_composition_rolls_back_complete_submission(tmp_path, fault):
    app, company = assemble(tmp_path)
    item = bank_and_equity()
    members = item["data"]["members"]
    if fault == "same_member":
        members.append(deepcopy(members[0]))
    elif fault == "same_bank":
        extra = deepcopy(members[0])
        extra["member_id"] = str(uuid4())
        members.append(extra)
    elif fault == "fake_kind":
        members[0]["data"]["type"] = "cash"
    elif fault == "current_tax":
        members.append(
            member(
                {
                    "type": "tax",
                    "authority_id": PARTY,
                    "tax_kind": "vat",
                    "balance_kind": "payable",
                    "amount_fen": "100",
                    "tax_period": "2026-07",
                }
            )
        )
    elif fault == "current_payroll":
        members.append(
            member(
                {
                    "type": "payroll_payable",
                    "person_id": PERSON,
                    "recipient_id": PERSON,
                    "payroll_period": "2026-07",
                    "component": "net",
                    "amount_fen": "100",
                }
            )
        )
    elif fault == "negative_capital":
        members[1]["data"].update(equity_kind="paid_in_capital", balance_fen="-1")
    elif fault == "missing_object":
        members[0]["data"]["funds_account_id"] = str(uuid4())
    else:
        members[0]["data"]["balance_fen"] = str(2**63 - 1)
        members[1]["data"]["balance_fen"] = str(2**63 - 1)
        members.extend(
            [
                member({"type": "bank", "funds_account_id": PLATFORM, "balance_fen": "1"}),
                member(
                    {
                        "type": "equity",
                        "equity_kind": "paid_in_capital",
                        "holder_or_basis_id": PARTY,
                        "balance_fen": "1",
                    }
                ),
            ]
        )
    actual = app.accounting_apply(request(company, item))
    assert actual.get("completion") == "not_saved", actual
    if fault == "overflow":
        assert actual["error"] == "invalid_input"
        assert "64" in actual["message"]
    assert get(app, company, item["business_id"])["businesses"] == []
    with app.storage.read_unit(company) as read:
        assert read.head == 0


def test_501_members_have_untruncated_distinct_sources_and_normalized_round_trip(tmp_path):
    app, company = assemble(tmp_path)
    sources = [source(str(uuid4())) for _ in range(501)]
    members = [
        member(
            {
                "type": "obligation",
                "counterparty_id": PARTY,
                "nature": "customer_receivable",
                "amount_fen": "1",
                "original_business_reference": f"prior-{n}",
            },
            evidence=[s["source_id"]],
        )
        for n, s in enumerate(sources)
    ]
    members.append(
        member(
            {
                "type": "equity",
                "equity_kind": "retained_earnings",
                "holder_or_basis_id": PARTY,
                "balance_fen": "501",
            }
        )
    )
    item = opening(*members)
    submitted = request(company, item)
    prepare = request(company)
    prepare["sources"].extend(sources[:499])
    assert app.accounting_apply(prepare).get("completion") == "complete"
    submitted.pop("objects")
    submitted["sources"] = sources[499:]
    actual = app.accounting_apply(submitted)
    assert actual.get("completion") == "complete", actual.get("message", actual)
    outcome = result(app, company, item)
    assert outcome["target"]["values"]["member_count"] == 502
    assert len(outcome["basis"]["source_locations"]) == 502
    assert (
        len([q for q in outcome["basis"]["adopted_quantities"] if q["type"] == "obligation"]) == 501
    )
    with app.storage.read_unit(company) as read:
        assert create_opening().export_current([item["business_id"]], read) == (item,)
    assert app.accounting_apply(submitted)["changes"] == "none"
    item["data"]["members"] = list(reversed(item["data"]["members"]))
    changed = app.accounting_apply(replace(company, item))
    assert changed.get("completion") == "complete", changed
    with app.storage.read_unit(company) as read:
        assert create_opening().export_current([item["business_id"]], read) == (item,)


def test_true_overdue_loan_can_continue_as_unpaid_opening_principal(tmp_path):
    app, company = assemble(tmp_path)
    contract, item, loan = loan_opening(maturity="2026-06-30")
    actual = app.accounting_apply(request(company, item, contract))
    assert actual.get("completion") == "complete", actual
    qs = result(app, company, item)["basis"]["adopted_quantities"]
    principal = next(q for q in qs if q["type"] == "loan_principal")
    assert principal["start_on"] == "2026-07-01"
    assert principal["end_before"] == "2026-06-30"
    with app.storage.read_unit(company) as read:
        (definition,) = app.ledger.resource_definitions(
            [
                {
                    "business_id": item["business_id"],
                    "slot": "member:" + loan["member_id"] + ":principal",
                }
            ],
            "obligation",
            read,
        )
        assert definition.value["due_on"] == "2026-06-30"


def test_repaid_principal_keeps_only_real_interest_and_needs_no_new_interest_start(tmp_path):
    app, company = assemble(tmp_path)
    contract, item, _ = loan_opening(maturity="2026-06-30")
    loan = item["data"]["members"][0]
    loan["data"].update(principal_fen="0", accrued_interest_fen="100")
    loan["data"].pop("interest_start_on")
    item["data"]["members"][1]["data"]["balance_fen"] = "100"
    paid = fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": "outflow",
            "amount_fen": "100",
            "payment_method": "individual",
            "counterparty_id": PARTY,
            "allocations": [
                {
                    "source": {
                        "business_id": item["business_id"],
                        "slot": "member:" + loan["member_id"] + ":interest",
                    },
                    "amount_fen": "100",
                }
            ],
        },
        actual="2026-07-02",
    )
    actual = app.accounting_apply(request(company, item, paid, contract))
    assert actual.get("completion") == "complete", actual
    outcome = result(app, company, item)
    assert not [q for q in outcome["basis"]["adopted_quantities"] if q["type"] == "loan_principal"]
    assert {
        q["source"]["slot"]
        for q in outcome["basis"]["adopted_quantities"]
        if q["type"] == "obligation"
    } == {"member:" + loan["member_id"] + ":interest"}
    assert outcome["vouchers"] == []
    with app.storage.read_unit(company) as read:
        (movement,) = app.ledger.fund_movements(BANK, "2026-07", "2026-07", read)
        assert movement.value["cashflow_parts"] == [
            {"category": "interest_payments", "amount_fen": "100"}
        ]
    loan["data"]["interest_start_on"] = "2026-01-02"
    changed = app.accounting_apply(replace(company, item))
    assert changed.get("completion") == "complete", changed
    assert changed["accounting"] == "unchanged"
    after = result(app, company, item)
    assert after["basis"] == outcome["basis"]
    assert after["reference"]["result_id"] == outcome["reference"]["result_id"]


def test_no_principal_or_interest_does_not_create_a_meaningless_opening_loan(tmp_path):
    app, company = assemble(tmp_path)
    contract, item, _ = loan_opening()
    item["data"]["members"][0]["data"].update(principal_fen="0", accrued_interest_fen="0")
    item["data"]["members"][1]["data"]["balance_fen"] = "0"
    actual = app.accounting_apply(request(company, item, contract))
    assert actual.get("status") == "rejected", actual
    assert actual["code"] == "invalid_input"


def test_unclear_original_reimbursement_purpose_preserves_real_payment_without_fake_cashflow(
    tmp_path,
):
    app, company = assemble(tmp_path)
    liability = member(
        {
            "type": "obligation",
            "counterparty_id": PERSON,
            "nature": "owner_reimbursement",
            "amount_fen": "100",
            "original_business_reference": "有据未清的老板代付款，原业务用途暂缺",
        }
    )
    item = opening(
        liability, member({"type": "bank", "funds_account_id": BANK, "balance_fen": "100"})
    )
    paid = fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": "outflow",
            "amount_fen": "100",
            "payment_method": "individual",
            "counterparty_id": PERSON,
            "allocations": [
                {
                    "source": {
                        "business_id": item["business_id"],
                        "slot": "member:" + liability["member_id"],
                    },
                    "amount_fen": "100",
                }
            ],
        },
        actual="2026-07-02",
    )
    actual = app.accounting_apply(request(company, paid, item))
    assert actual.get("completion") == "complete", actual
    with app.storage.read_unit(company) as read:
        (movement,) = app.ledger.fund_movements(BANK, "2026-07", "2026-07", read)
        assert movement.value["amount_fen"] == "100"
        assert "cashflow_parts" not in movement.value


@pytest.mark.parametrize("fault", ["missing_tax_period", "tax_period_on_net", "current_tax_period"])
def test_payroll_opening_tax_period_is_independent_and_cannot_be_invented(tmp_path, fault):
    app, company = assemble(tmp_path)
    data = {
        "type": "payroll_payable",
        "person_id": PERSON,
        "recipient_id": PARTY,
        "payroll_period": "2026-05",
        "component": "tax",
        "tax_period": "2026-06",
        "amount_fen": "100",
    }
    if fault == "missing_tax_period":
        data.pop("tax_period")
    elif fault == "tax_period_on_net":
        data.update(component="net", recipient_id=PERSON)
    else:
        data["tax_period"] = "2026-07"
    item = opening(
        member(data), member({"type": "bank", "funds_account_id": BANK, "balance_fen": "100"})
    )
    actual = app.accounting_apply(request(company, item))
    if fault == "current_tax_period":
        assert actual.get("completion") == "not_saved", actual
    else:
        assert actual.get("status") == "rejected", actual
        assert actual["code"] == "invalid_input"
    assert get(app, company, item["business_id"])["businesses"] == []


@pytest.mark.parametrize(
    "fault", ["future_month", "known_future_day", "missing_contract", "wrong_lender"]
)
def test_opening_loan_requires_real_corresponding_contract_at_boundary(tmp_path, fault):
    app, company = assemble(tmp_path)
    contract, item, _ = loan_opening()
    if fault == "future_month":
        contract["period"] = "2026-08"
    elif fault == "known_future_day":
        contract.update(period="2026-07", effective_on="2026-07-02")
    elif fault == "wrong_lender":
        item["data"]["members"][0]["data"]["lender_id"] = str(uuid4())
    submitted = request(company, item, *([] if fault == "missing_contract" else [contract]))
    if fault == "wrong_lender":
        submitted["objects"].append(
            {
                "object_id": item["data"]["members"][0]["data"]["lender_id"],
                "kind": "organization",
                "name": "另一合成机构",
                "expected_seq": 0,
                "data": {},
            }
        )
    actual = app.accounting_apply(submitted)
    assert actual.get("completion") == "not_saved", actual
    if fault == "missing_contract":
        assert actual["error"] == "invalid_input"
        assert actual["fact_issues"][0]["code"] == "reference_not_found"
        assert actual["fact_issues"][0]["field"] == "/data/members/0/data/contract_id"
    assert get(app, company, item["business_id"])["businesses"] == []
