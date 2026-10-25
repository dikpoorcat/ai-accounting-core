"""Feedback distinguishes missing facts, bad references and unsupported calculation."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError, PretaxView, validate_contract
from ai_accounting_modular.domains.taxes.public import create_taxes

from ._fixtures import (
    LOCATION,
    Context,
    actual_vat,
    assessment,
    confirmed_income,
    fact,
    income_actual,
    income_inputs,
    policy,
    sale,
    surtax,
    tax_sale,
    uid,
)
from .test_application import assembled as assembled
from .test_application import main, request


def test_unsupported_special_treatment_keeps_explicit_cause_and_rolls_back(assembled):
    app, company, storage, _ = assembled
    current, basis, policy = income_inputs()
    basis["data"]["special_treatments"] = ["研发费用加计扣除"]
    rejected = app.accounting_apply(request(company, current, basis, policy))
    assert rejected["error"] == "unsupported_business", rejected
    assert rejected["changes"] == "none" and rejected["completion"] == "not_saved"
    problem = rejected["fact_issues"][0]
    assert problem["code"] == "unsupported_calculation"
    assert problem["business_id"] == basis["business_id"]
    assert problem["field"] == "/data/special_treatments"
    assert problem["meaning"] == "季度所得税暂计算法的适用范围"
    assert problem["reusable_sources"] == [{"source_id": LOCATION["source_id"]}]
    for stored in (current, basis, policy):
        assert (
            app.accounting_get(
                {
                    "company_id": company,
                    "business_ids": [stored["business_id"]],
                    "include": ["facts"],
                }
            )["businesses"]
            == []
        )
    with storage.read_unit(company) as read:
        assert read.head == 0


@pytest.mark.parametrize("route", ["confirmed_provision", "actual"])
def test_supported_sourced_route_does_not_require_unsupported_reference_calculation(
    assembled, route
):
    app, company, _, _ = assembled
    current, basis, policy = income_inputs()
    basis["data"]["special_treatments"] = ["研发费用加计扣除"]
    if route == "confirmed_provision":
        current = confirmed_income("income", "2026-03", "80000")
        facts = [current, basis, policy]
    else:
        facts = [current, basis, policy, income_actual(current, "80000", "0", "80000")]
    result = app.accounting_apply(request(company, *facts))
    assert result["completion"] == "complete", result
    adopted = main(app, company, current)
    assert adopted["target"]["values"]["cumulative_tax_fen"] == "80000"
    assert adopted["target"]["values"]["adopted_basis"] == route
    declared = app.accounting_find(
        {
            "company_id": company,
            "family": "taxes",
            "kind": "taxes.actual_declaration",
            "period_from": "2026-01",
            "period_to": "2026-03",
            "limit": 10,
        }
    )
    assert declared["page"]["filtered_count"] == (0 if route == "confirmed_provision" else 1)


@pytest.mark.parametrize(
    "change,profit,field",
    [
        (
            {"loss_deduction_fen": "2000001", "loss_source": LOCATION},
            "2000000",
            "loss_deduction_fen",
        ),
        ({}, "300000001", "treatment"),
    ],
)
def test_conflicting_known_amounts_are_not_new_missing_facts(change, profit, field):
    current, basis, policy = income_inputs()
    basis["data"].update(change)
    context = Context(current, basis, policy, profit=profit)
    with pytest.raises(ContractError) as failed:
        create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert failed.value.code == "dependency_conflict"
    problem = failed.value.fact_issues[0]
    assert problem["code"] == "inconsistent_fact"
    assert problem["business_id"] == basis["business_id"]
    assert problem["field"] == "/data/" + field
    assert basis["data"] == context.facts[basis["business_id"]]["data"]


@pytest.mark.parametrize("located", [True, False])
def test_unknown_pretax_reports_actual_upstream_issue_without_a_profit_input(located):
    current, basis, policy = income_inputs()
    context = Context(current, basis, policy)
    fields = deepcopy(context.pre_tax("2026-01", "2026-03").fields)
    fields["profit_before_tax"] = {"state": "unestablished", "issue_indexes": [0]}
    upstream = {
        "code": "missing_prior_values",
        "business_id": uid("actual-report-continuation"),
        "field": "/data/prior_profit",
        "meaning": "接账前累计损益",
        "message": "接账前累计损益尚未明确。",
        "allowed_precision": ["integer_fen"],
        "reusable_sources": [LOCATION],
        "blocking_scope": "none",
    }
    context.pre_tax = lambda *_: PretaxView(fields, issues=(upstream,) if located else ())
    with pytest.raises(ContractError) as failed:
        create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert failed.value.code == "dependency_conflict"
    problem = failed.value.fact_issues[0]
    validate_contract("core.schema.json", "FactIssue", problem)
    if located:
        assert problem == {**upstream, "blocking_scope": "business"}
    else:
        assert problem["code"] == "result_not_ready"
        assert problem["field"] == "/" and problem["allowed_precision"] == []
    assert "/data/profit_before_tax" not in str(failed.value.to_response())


def test_missing_loss_source_remains_a_real_fact_requirement():
    current, basis, policy = income_inputs()
    basis["data"]["loss_deduction_fen"] = "100"
    with pytest.raises(ContractError) as failed:
        create_taxes().evaluate_slot(
            current["business_id"], "main", Context(current, basis, policy)
        )
    assert failed.value.code == "needs_information"
    assert failed.value.fact_issues[0]["business_id"] == basis["business_id"]
    assert failed.value.fact_issues[0]["field"] == "/data/loss_source"


@pytest.mark.parametrize("missing", ["fact", "result"])
def test_provided_reference_never_becomes_an_owner_fact_question(missing):
    current, basis, policy = income_inputs()
    context = Context(current, policy) if missing == "fact" else Context(current, basis, policy)
    if missing == "result":
        original = context.get_result
        context.get_result = lambda identity, slot: (
            None if identity == basis["business_id"] else original(identity, slot)
        )
    with pytest.raises(ContractError) as failed:
        create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert failed.value.code == ("invalid_input" if missing == "fact" else "dependency_conflict")
    assert failed.value.fact_issues[0]["field"] == "/data/prepayment_basis_id"
    assert failed.value.fact_issues[0]["code"] == (
        "reference_not_found" if missing == "fact" else "result_not_ready"
    )


@pytest.mark.parametrize("mixed", [False, True])
def test_unsupported_vat_reference_keeps_its_cause_when_actual_amount_is_adopted(mixed):
    current, params, surcharge = assessment(), policy(), surtax(zero=True)
    sales = (
        [tax_sale("positive", "10000", "300"), tax_sale("negative", "-1000", "-10", rate="0.01")]
        if mixed
        else [tax_sale("returned", "-10000", "-300")]
    )
    with pytest.raises(ContractError) as failed:
        create_taxes().evaluate_slot(
            current["business_id"], "main", Context(current, params, surcharge, sales=sales)
        )
    assert failed.value.code == "unsupported_business"
    assert failed.value.fact_issues[0]["code"] == "unsupported_calculation"
    declared = actual_vat(current, "290" if mixed else "0")
    result = create_taxes().evaluate_slot(
        current["business_id"],
        "main",
        Context(current, params, surcharge, declared, sales=sales),
    )
    values = result["target"]["values"]
    assert values["adopted_basis"] == "actual"
    assert values["payable_vat_fen"] == ("290" if mixed else "0")
    assert {
        **failed.value.fact_issues[0],
        "blocking_scope": "none",
    } in values["filing_issues"]


def test_given_policy_with_incompatible_period_is_an_input_reference_problem():
    current, params, surcharge = assessment(), policy(), surtax()
    params["data"]["policy"]["effective_from"] = "2026-01-15"
    with pytest.raises(ContractError) as failed:
        create_taxes().evaluate_slot(
            current["business_id"], "main", Context(current, params, surcharge)
        )
    assert failed.value.code == "invalid_input"
    assert failed.value.fact_issues[0]["code"] == "incompatible_policy_period"
    assert failed.value.fact_issues[0]["field"] == "/data/vat_policy_id"


def test_tax_point_with_missing_target_is_a_bad_reference():
    point = fact(
        "missing-target",
        "taxes.tax_point",
        {
            "target_business_id": uid("not-registered"),
            "transaction_mode": "ordinary_service",
            "service_state": "not_started",
            "contractual_entitlement": "not_set",
            "events": [],
        },
    )
    with pytest.raises(ContractError) as caught:
        create_taxes().evaluate_slot(point["business_id"], "main", Context(point))
    assert caught.value.code == "invalid_input"
    assert caught.value.fact_issues[0]["code"] == "reference_not_found"
    assert caught.value.fact_issues[0]["field"] == "/data/target_business_id"


@pytest.mark.parametrize("actual", [False, True])
def test_credit_requires_adopted_actual_result_and_valid_return_reference(actual):
    original = assessment()
    business = sale()
    credit = fact(
        "credit",
        "taxes.tax_credit",
        {
            "original_assessment_id": original["business_id"],
            "start_month": "2026-01",
            "end_month": "2026-01",
            "returns": [
                {
                    "return_id": uid("missing-return"),
                    "original_business_id": business["business_id"],
                    "gross_fen": "10300",
                    "vat_fen": "300",
                }
            ],
            "original_filing_source": LOCATION,
            "authority_source": LOCATION,
            "disposition": "refund",
            "vat_credit_fen": "300",
            "surtax_credit_fen": "0",
            "exemption_reversal_fen": "0",
        },
        "2026-02",
    )
    sources = [actual_vat(original, "300")] if actual else []
    context = Context(
        credit, original, policy(), surtax(), *sources, sales=[tax_sale("sale", "10000", "300")]
    )
    with pytest.raises(ContractError) as caught:
        create_taxes().evaluate_slot(credit["business_id"], "main", context)
    assert caught.value.code == ("invalid_input" if actual else "dependency_conflict")
    problem = caught.value.fact_issues[0]
    assert problem["code"] == ("reference_not_found" if actual else "result_not_ready")
    assert problem["field"] == (
        "/data/returns/0/return_id" if actual else "/data/original_assessment_id"
    )
