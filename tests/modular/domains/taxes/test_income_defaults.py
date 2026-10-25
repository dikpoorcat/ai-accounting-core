"""Project profit defaults preserve sourced accounting and immutable history."""

from copy import deepcopy
from hashlib import sha256
from itertools import product

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.taxes.public import create_taxes

from ._fixtures import (
    LOCATION,
    Context,
    actual_vat,
    assessment,
    income_actual,
    income_inputs,
    policy,
    sale,
    uid,
)
from .test_application import assembled as assembled
from .test_application import get, main, replace, request


def default_inputs():
    current, basis, params = income_inputs()
    current["data"].pop("method")
    basis["data"].pop("collection_method")
    basis["data"].pop("basis_source")
    return current, basis, params


def evaluate(current, *others, **values):
    return create_taxes().evaluate_slot(
        current["business_id"], "main", Context(current, *others, **values)
    )


@pytest.mark.parametrize(
    "omit_method,omit_collection,omit_source", tuple(product((False, True), repeat=3))
)
def test_defaults_are_independent_and_only_normalize_adopted_input(
    omit_method, omit_collection, omit_source
):
    current, basis, params = income_inputs()
    explicit = evaluate(current, basis, params)
    if omit_method:
        current["data"].pop("method")
    if omit_collection:
        basis["data"].pop("collection_method")
    if omit_source:
        basis["data"].pop("basis_source")
    originals = deepcopy((current, basis, params))
    adopted = evaluate(current, basis, params)
    adopted_basis = evaluate(basis)
    assert adopted["target"] == explicit["target"]
    assert adopted["lines"] == explicit["lines"]
    assert adopted["target"]["values"]["cumulative_tax_fen"] == "100000"
    assert adopted["basis"]["input"]["method"] == "computed_provision"
    assert adopted_basis["basis"]["input"]["collection_method"] == "actual_profit"
    assert adopted["calculation_revision"] == (2 if omit_method else 1)
    assert adopted_basis["calculation_revision"] == (2 if omit_collection or omit_source else 1)
    if omit_source:
        assert adopted_basis["basis"]["input"]["collection_method_basis"] == "project_default"
    else:
        assert "collection_method_basis" not in adopted_basis["basis"]["input"]
        assert LOCATION in adopted_basis["basis"]["source_locations"]
    assert (current, basis, params) == originals


@pytest.mark.parametrize(
    "owner,field",
    [
        ("current", "policy_id"),
        ("current", "prepayment_basis_id"),
        ("basis", "treatment"),
        ("basis", "eligibility_source"),
        ("basis", "loss_deduction_fen"),
        ("basis", "special_treatments"),
    ],
)
def test_project_defaults_do_not_fill_other_required_accounting_facts(owner, field):
    current, basis, params = default_inputs()
    target = current if owner == "current" else basis
    target["data"].pop(field)
    before = deepcopy(target)
    with pytest.raises(ContractError):
        if owner == "current":
            evaluate(current, basis, params)
        else:
            evaluate(basis)
    assert target == before


def test_missing_method_does_not_turn_confirmed_amount_into_computed_profit():
    current, _, _ = default_inputs()
    current["data"] = {
        "type": "income_tax",
        "tax_year": 2026,
        "tax_period_end": "2026-03",
        "cumulative_tax_fen": "12345",
        "source": LOCATION,
    }
    with pytest.raises(ContractError):
        evaluate(current)


def test_adopted_default_marker_cannot_be_submitted_as_a_business_fact():
    _, basis, _ = default_inputs()
    basis["data"]["collection_method_basis"] = "project_default"
    with pytest.raises(ContractError):
        evaluate(basis)


@pytest.mark.parametrize("valid", (False, True))
def test_explicit_collection_source_is_adopted_or_atomically_rejected(assembled, valid):
    app, company, _, _ = assembled
    current, basis, params = default_inputs()
    basis["data"]["basis_source"] = {"source_id": uid("collection-proof")}
    command = request(company, current, basis, params)
    if valid:
        text = "合成税务依据：按实际利润预缴，与真实公司无关。"
        command["sources"].append(
            {
                "source_id": uid("collection-proof"),
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": "2026-01",
            }
        )
    result = app.accounting_apply(command)
    if valid:
        assert result["completion"] == "complete", result
        adopted = main(app, company, basis)["basis"]
        assert basis["data"]["basis_source"] in adopted["source_locations"]
        assert "collection_method_basis" not in adopted["input"]
    else:
        assert result["completion"] == "not_saved", result
        queried = app.accounting_get(
            {
                "company_id": company,
                "business_ids": [row["business_id"] for row in (current, basis, params)],
                "include": ["facts", "results"],
            }
        )
        assert queried["businesses"] == []
        assert set(queried["missing_business_ids"]) == {
            row["business_id"] for row in (current, basis, params)
        }


def test_default_zero_is_formal_and_upstream_profit_recalculates_without_rewriting_fact(assembled):
    app, company, _, _ = assembled
    current, basis, params = default_inputs()
    result = app.accounting_apply(request(company, current, basis, params, policy()))
    assert result["completion"] == "complete", result
    zero = main(app, company, current)
    assert zero["target"]["values"]["cumulative_tax_fen"] == "0"
    assert zero["vouchers"] == []
    earned = sale("default-profit", "2060000")
    result = app.accounting_apply(request(company, earned))
    assert result["completion"] == "complete", result
    assert main(app, company, current)["target"]["values"]["cumulative_tax_fen"] == "100000"
    earned["data"]["gross_fen"] = "1030000"
    result = app.accounting_apply(request(company, mutations=[replace(earned)]))
    assert result["completion"] == "complete", result
    values = main(app, company, current)["target"]["values"]
    assert values["profit_before_tax_fen"] == "1000000"
    assert values["cumulative_tax_fen"] == "50000"
    assert get(app, company, current["business_id"])["fact"] == current
    assert get(app, company, basis["business_id"])["fact"] == basis


def test_default_quarters_allow_reverse_registration_and_keep_one_cumulative_node(assembled):
    app, company, _, _ = assembled
    first, basis, params = default_inputs()
    second, second_basis = deepcopy(first), deepcopy(basis)
    for row in (second, second_basis):
        row["business_id"] = uid("second-" + row["kind"])
        row["period"] = row["data"]["quarter_end"] = "2026-06"
    second["data"]["prepayment_basis_id"] = second_basis["business_id"]
    result = app.accounting_apply(
        request(company, second, second_basis, params, policy(), sale(gross="2060000"))
    )
    assert result["completion"] == "complete", result
    assert main(app, company, second)["target"]["values"]["change_fen"] == "100000"
    result = app.accounting_apply(request(company, first, basis))
    assert result["completion"] == "complete", result
    assert main(app, company, first)["target"]["values"]["change_fen"] == "100000"
    assert main(app, company, second)["target"]["values"]["change_fen"] == "0"
    before = main(app, company, first)
    duplicate = deepcopy(first)
    duplicate["business_id"] = uid("duplicate-default-quarter")
    duplicate["data"]["method"] = "computed_provision"
    result = app.accounting_apply(request(company, duplicate))
    assert result["completion"] == "not_saved", result
    assert main(app, company, first) == before


def test_actual_declaration_overrides_default_without_loading_reference_inputs(assembled):
    app, company, _, _ = assembled
    current, _, _ = default_inputs()
    actual = income_actual(current, "90000", "60000", "30000", period="2026-04")
    result = app.accounting_apply(request(company, current, actual))
    assert result["completion"] == "complete", result
    values = main(app, company, current)["target"]["values"]
    assert values["adopted_basis"] == "actual"
    assert values["cumulative_tax_fen"] == values["change_fen"] == "90000"
    assert "profit_before_tax_fen" not in values
    assert get(app, company, current["business_id"])["fact"] == current


@pytest.mark.parametrize("frozen", (False, True))
def test_explicit_sourced_result_survives_real_rescheduling_without_new_adoption(
    assembled, monkeypatch, frozen
):
    app, company, storage, _ = assembled
    current, basis, params = income_inputs()
    result = app.accounting_apply(request(company, current, basis, params))
    assert result["completion"] == "complete", result
    if frozen:
        with storage.write_unit(company) as write:
            write.repository("ledger").freeze_heads("2026-03")
    before = get(app, company, current["business_id"])["results"]
    basis_before = get(app, company, basis["business_id"])["results"]
    with storage.read_unit(company) as read:
        for row in (current, basis):
            assert (
                read.repository("ledger").current_result(row["business_id"], "main")[
                    "calculation_revision"
                ]
                == 1
            )
    owner = app.registry.owner("taxes.assessment")
    original = owner.evaluate_slot
    evaluated = []

    def observe(business_id, slot, context):
        evaluated.append((business_id, slot))
        return original(business_id, slot, context)

    monkeypatch.setattr(owner, "evaluate_slot", observe)
    no_profit = assessment("zero-vat-publisher", "2026-02", policies=False)
    result = app.accounting_apply(request(company, no_profit, actual_vat(no_profit)))
    assert result["completion"] == "complete", result
    assert (current["business_id"], "main") in evaluated
    assert get(app, company, current["business_id"])["results"] == before
    assert get(app, company, basis["business_id"])["results"] == basis_before
