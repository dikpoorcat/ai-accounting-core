"""Opening payroll locations are owned metadata, not reconstructed slot names."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.opening.public import opening_payroll_references

from ._support import BANK, PARTY, PERSON, assemble, loan_opening, member, opening, request


def adopted(app, company, business_id):
    with app.storage.read_unit(company) as read:
        refs = app.ledger.get_result_refs([business_id], read)
        return app.ledger.get_adopted_contents(refs, read)[0]


def create_package(tmp_path):
    app, company = assemble(tmp_path)
    payroll = member(
        {
            "type": "payroll_payable",
            "person_id": PERSON,
            "recipient_id": PARTY,
            "payroll_period": "2026-06",
            "component": "net",
            "amount_fen": "10000",
        }
    )
    package = opening(
        member({"type": "bank", "funds_account_id": BANK, "balance_fen": "10000"}), payroll
    )
    result = app.accounting_apply(request(company, package))
    assert result.get("completion") == "complete", result
    return app, company, package, payroll


def test_formal_opening_member_keeps_the_person_separate_from_the_recipient(tmp_path):
    app, company, package, payroll = create_package(tmp_path)
    content = adopted(app, company, package["business_id"])
    result = opening_payroll_references(content)
    assert len(result) == 1
    ref = result[0]
    assert (ref.person_id, ref.recipient_id, ref.payroll_period, ref.component, ref.tax_period) == (
        PERSON,
        PARTY,
        "2026-06",
        "net",
        None,
    )
    with app.storage.read_unit(company) as read:
        definition = app.ledger.resource_definitions([ref.source], "obligation", read)[0]
    assert definition.value["amount_fen"] == "10000"
    assert definition.value["party_id"] == PARTY
    assert ref.source["business_id"] == package["business_id"]
    assert payroll["member_id"] in ref.source["slot"]


@pytest.mark.parametrize("change", ["source", "period", "member_count", "duplicate", "slot"])
def test_crossed_identity_or_inconsistent_formal_member_directory_is_rejected(tmp_path, change):
    app, company, package, _ = create_package(tmp_path)
    content = adopted(app, company, package["business_id"])
    ref, source, target, basis = map(
        deepcopy,
        (
            content.reference,
            content.source,
            content.target,
            content.basis,
        ),
    )
    if change == "source":
        source["business_id"] = str(uuid4())
    elif change == "period":
        ref["business_period"] = "2026-06"
    elif change == "member_count":
        target["values"]["member_count"] += 1
    elif change == "duplicate":
        basis["input"]["members"].append(deepcopy(basis["input"]["members"][0]))
        target["values"]["member_count"] += 1
    else:
        target["result_slot"] = "other"
    with pytest.raises(ContractError) as error:
        opening_payroll_references(
            replace(content, reference=ref, source=source, target=target, basis=basis)
        )
    assert error.value.code == ("invalid_input" if change == "source" else "storage_corrupt")


def test_another_valid_formal_kind_is_not_misread_as_an_opening(tmp_path):
    app, company = assemble(tmp_path)
    contract, _, _ = loan_opening()
    saved = app.accounting_apply(request(company, contract))
    assert saved.get("completion") == "complete", saved
    content = adopted(app, company, contract["business_id"])
    with pytest.raises(ContractError) as error:
        opening_payroll_references(content)
    assert error.value.code == "unsupported_business"
