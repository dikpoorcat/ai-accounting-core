"""Financing feedback separates references, dependencies and real missing facts."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, EvaluatedSlot, validate_contract
from ai_accounting_modular.domains.financing._cooperation_common import terms
from ai_accounting_modular.domains.financing._cooperation_events import Event, require_book_start
from ai_accounting_modular.domains.financing._cooperation_math import Totals
from ai_accounting_modular.domains.financing._cooperation_sources import source_trade
from ai_accounting_modular.domains.financing._interest import principal_basis
from ai_accounting_modular.domains.financing._loans import contract_result

from ._cooperation_support import agency_payment, contract, expense, funding, submitted
from ._support import SOURCE, assemble, get, request, result, six


def assert_problem(error, fact, field, code):
    (problem,) = error.fact_issues
    validate_contract("core.schema.json", "FactIssue", problem)
    assert problem["business_id"] == fact["business_id"]
    assert problem["field"] == field
    assert problem["code"] == code
    assert problem["reusable_sources"] == [{"source_id": SOURCE}]


def test_unregistered_object_id_is_an_input_error_and_does_not_save_the_contract(tmp_path):
    app, company = assemble(tmp_path)
    _, agreement, *_ = six()
    agreement["data"]["lender_id"] = str(uuid4())
    actual = app.accounting_apply(request(company, agreement))
    assert actual["error"] == "invalid_input", actual
    assert actual["completion"] == "not_saved"
    assert actual["fact_issues"][0]["code"] == "reference_not_found"
    assert actual["fact_issues"][0]["field"] == "/data/lender_id"
    assert get(app, company, agreement["business_id"])["businesses"] == []


@pytest.mark.parametrize("cooperation", [False, True])
def test_referenced_contract_without_result_is_a_dependency_not_a_question(cooperation):
    agreement = contract()
    fact, resolve = (funding(agreement), terms) if cooperation else (six()[2], contract_result)
    context = SimpleNamespace(get_result=lambda *_: None)
    with pytest.raises(ContractError) as failed:
        resolve(fact, context)
    assert failed.value.code == "dependency_conflict"
    assert_problem(failed.value, fact, "/data/contract_id", "result_not_ready")


@pytest.mark.parametrize("cooperation", [False, True])
def test_wrong_contract_kind_is_an_input_error_with_its_actual_reference(tmp_path, cooperation):
    app, company = assemble(tmp_path)
    owner, loan_contract, drawdown, *_ = six()
    if cooperation:
        fact = funding(contract())
        fact["data"]["contract_id"] = loan_contract["business_id"]
        attempt = submitted(company, loan_contract, fact)
    else:
        fact = drawdown
        fact["data"]["contract_id"] = owner["business_id"]
        attempt = request(company, owner, fact)
    actual = app.accounting_apply(attempt)
    assert actual["error"] == "invalid_input", actual
    assert actual["completion"] == "not_saved"
    assert actual["fact_issues"][0]["code"] == "incompatible_business_kind"
    assert actual["fact_issues"][0]["field"] == "/data/contract_id"
    assert get(app, company, fact["business_id"])["businesses"] == []


def test_known_loan_contract_month_conflict_does_not_claim_a_missing_fact(tmp_path):
    app, company = assemble(tmp_path)
    _, agreement, drawdown, *_ = six()
    agreement["period"] = "2026-02"
    actual = app.accounting_apply(request(company, agreement, drawdown))
    assert actual["error"] == "dependency_conflict", actual
    assert actual["fact_issues"][0]["code"] == "inconsistent_fact"
    assert actual["fact_issues"][0]["business_id"] == drawdown["business_id"]
    assert actual["fact_issues"][0]["field"] == "/data/contract_id"
    assert get(app, company, agreement["business_id"], drawdown["business_id"])["businesses"] == []


@pytest.fixture
def principal_inputs(tmp_path):
    app, company = assemble(tmp_path)
    _, agreement, loan, interest, *_ = six()
    saved = app.accounting_apply(request(company, agreement, loan))
    assert saved["completion"] == "complete", saved
    current = result(app, company, loan)
    with app.storage.read_unit(company) as read:
        (definition,) = app.ledger.resource_definitions(
            [interest["data"]["principal_source"]], "obligation", read
        )
        source = app.ledger.get_result_source(loan["business_id"], "main", read)
    return interest, definition, EvaluatedSlot(source, current["target"], current["basis"])


@pytest.mark.parametrize("problem", ["unready", "principal_quantity", "terms", "contradiction"])
def test_principal_result_problem_stays_with_the_upstream_result(principal_inputs, problem):
    fact, definition, original = principal_inputs
    basis = deepcopy(original.basis)
    if problem in {"principal_quantity", "terms"}:
        removed = "loan_principal" if problem == "principal_quantity" else "loan_terms"
        basis["adopted_quantities"] = [
            quantity for quantity in basis["adopted_quantities"] if quantity["type"] != removed
        ]
    elif problem == "contradiction":
        principal = next(
            quantity
            for quantity in basis["adopted_quantities"]
            if quantity["type"] == "loan_principal"
        )
        principal["outstanding_fen"] = "999999"
    source = (
        None if problem == "unready" else EvaluatedSlot(original.source, original.target, basis)
    )
    context = SimpleNamespace(
        resource_definitions=lambda *_: (definition,), get_result=lambda *_: source
    )
    with pytest.raises(ContractError) as failed:
        principal_basis(fact, context)
    assert failed.value.code == "dependency_conflict"
    code = (
        "result_not_ready"
        if problem == "unready"
        else "inconsistent_fact"
        if problem == "contradiction"
        else "incompatible_source_result"
    )
    assert_problem(failed.value, fact, "/data/principal_source", code)


def test_referenced_trade_without_result_is_an_upstream_dependency(principal_inputs):
    _, definition, _ = principal_inputs
    fact = agency_payment(contract(), {"business_id": definition.reference["business_id"]})
    fact["data"]["source"] = deepcopy(definition.reference)
    context = SimpleNamespace(
        resource_definitions=lambda *_: (definition,), get_result=lambda *_: None
    )
    with pytest.raises(ContractError) as failed:
        source_trade(fact, context)
    assert failed.value.code == "dependency_conflict"
    assert_problem(failed.value, fact, "/data/source", "result_not_ready")


def test_ordinary_expense_is_not_a_cooperation_source_that_needs_more_boss_information(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement)
    charged["data"].pop("cooperation_id")
    charged["data"].pop("project_total_fen")
    paid = agency_payment(agreement, charged)
    actual = app.accounting_apply(submitted(company, agreement, funding(agreement), charged, paid))
    assert actual["error"] == "invalid_input", actual
    assert actual["fact_issues"][0]["code"] == "incompatible_source_result"
    assert actual["fact_issues"][0]["business_id"] == paid["business_id"]
    assert actual["fact_issues"][0]["field"] == "/data/source"
    assert get(app, company, agreement["business_id"], charged["business_id"])["businesses"] == []


@pytest.mark.parametrize("amount", [0, 1])
def test_historical_cooperation_position_is_a_capability_boundary_only_when_nonzero(amount):
    agreement = contract()
    agreement["period"] = "2025-12"
    events = [Event(str(uuid4()), "2025-12", "2025-12-10", Totals(company_funded_fen=amount))]
    if not amount:
        require_book_start(agreement, events, "2026-01")
        return
    with pytest.raises(ContractError) as failed:
        require_book_start(agreement, events, "2026-01")
    assert failed.value.code == "unsupported_business"
    assert_problem(failed.value, agreement, "/period", "unsupported_calculation")
