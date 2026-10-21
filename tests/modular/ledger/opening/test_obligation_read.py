"""Opening explains declared ordinary/tax metadata without inventing other resource keys."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger.opening.public import (
    OpeningLoanReference,
    OpeningOrdinaryReference,
    OpeningTaxReference,
    opening_obligation_references,
    opening_payroll_references,
)
from ai_accounting_modular.ledger.public import ObligationPositionQuery

from ._support import BANK, PARTY, PERSON, assemble, loan_opening, member, opening, request
from .test_payroll_read import adopted

NATURES = (
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
)


def create_package(tmp_path):
    app, company = assemble(tmp_path)
    ordinary = [
        member(
            {
                "type": "obligation",
                "counterparty_id": PERSON if "reimbursement" in nature else PARTY,
                "nature": nature,
                "amount_fen": "100",
                "original_business_reference": "合成原业务 · " + nature,
            }
        )
        for nature in NATURES
    ]
    taxes = [
        member(
            {
                "type": "tax",
                "authority_id": PARTY,
                "tax_kind": kind,
                "balance_kind": "payable",
                "amount_fen": amount,
                "tax_period": "2026-06",
            }
        )
        for kind, amount in (
            ("vat", "200"),
            ("surtax", "300"),
            ("individual_income_tax", "400"),
            ("enterprise_income_tax", "500"),
        )
    ]
    taxes.append(
        member(
            {
                "type": "tax",
                "authority_id": PARTY,
                "tax_kind": "vat",
                "balance_kind": "refundable",
                "amount_fen": "600",
                "tax_period": "2026-05",
            }
        )
    )
    package = opening(
        *ordinary,
        *taxes,
        member({"type": "bank", "funds_account_id": BANK, "balance_fen": "1200"}),
    )
    saved = app.accounting_apply(request(company, package))
    assert saved.get("completion") == "complete", saved
    return app, company, package


def test_declared_natures_original_references_and_tax_periods_match_actual_resources(tmp_path):
    app, company, package = create_package(tmp_path)
    content = adopted(app, company, package["business_id"])
    before = deepcopy(content)
    values = opening_obligation_references(content)
    ordinary = [value for value in values if isinstance(value, OpeningOrdinaryReference)]
    taxes = [value for value in values if isinstance(value, OpeningTaxReference)]
    assert len(values) == len({value.source["slot"] for value in values}) == 15
    assert [(value.nature, value.original_business_reference) for value in ordinary] == [
        (nature, "合成原业务 · " + nature) for nature in NATURES
    ]
    assert [(value.tax_kind, value.tax_period, value.balance_kind) for value in taxes] == [
        ("vat", "2026-06", "payable"),
        ("surtax", "2026-06", "payable"),
        ("individual_income_tax", "2026-06", "payable"),
        ("enterprise_income_tax", "2026-06", "payable"),
        ("vat", "2026-05", "refundable"),
    ]
    with app.storage.read_unit(company) as read:
        definitions = app.ledger.resource_definitions(
            [value.source for value in values], "obligation", read
        )
    assert [definition.reference for definition in definitions] == [
        value.source for value in values
    ]
    assert [definition.value["amount_fen"] for definition in definitions] == [
        *("100" for _ in NATURES),
        "200",
        "300",
        "400",
        "500",
        "600",
    ]
    assert definitions[-1].value["direction"] == "receivable"
    assert opening_payroll_references(content) == ()
    assert content == before


@pytest.mark.parametrize(
    "principal,interest,bank_balance,categories",
    (
        ("0", "100", "100", ["interest"]),
        ("10000", "0", "10000", ["loan_principal"]),
        ("10000", "2000", "12000", ["interest", "loan_principal"]),
    ),
)
def test_loan_members_have_no_invented_unsuffixed_metadata_or_zero_resource(
    tmp_path, principal, interest, bank_balance, categories
):
    app, company = assemble(tmp_path)
    contract, _, loan = loan_opening()
    loan["data"].update(principal_fen=principal, accrued_interest_fen=interest)
    if principal == "0":
        loan["data"].pop("interest_start_on")
    package = opening(
        loan,
        member(
            {
                "type": "bank",
                "funds_account_id": BANK,
                "balance_fen": bank_balance,
            }
        ),
    )
    saved = app.accounting_apply(request(company, contract, package))
    assert saved.get("completion") == "complete", saved
    content = adopted(app, company, package["business_id"])
    references = opening_obligation_references(content)
    assert all(isinstance(item, OpeningLoanReference) for item in references)
    assert {item.contract_id for item in references} == {contract["business_id"]}
    assert [item.component for item in references] == [
        component
        for component, amount in (("principal", principal), ("interest", interest))
        if amount != "0"
    ]
    with app.storage.read_unit(company) as read:
        definitions = app.ledger.resource_definitions(
            [item.source for item in references], "obligation", read
        )
        actual = app.ledger.query_obligation_positions(
            ObligationPositionQuery("2026-07", business_ids=(package["business_id"],)),
            PageQuery(),
            read,
        ).page.items
    assert [item.definition.value["category"] for item in actual] == categories
    assert [item.reference for item in definitions] == [item.source for item in references]


@pytest.mark.parametrize("damage", ("source", "period", "count", "duplicate", "slot"))
def test_new_helper_reuses_exact_opening_identity_and_directory_checks(tmp_path, damage):
    app, company, package = create_package(tmp_path)
    content = adopted(app, company, package["business_id"])
    ref, source, target, basis = map(
        deepcopy, (content.reference, content.source, content.target, content.basis)
    )
    if damage == "source":
        source["business_id"] = str(uuid4())
    elif damage == "period":
        ref["business_period"] = "2026-06"
    elif damage == "count":
        target["values"]["member_count"] += 1
    elif damage == "duplicate":
        basis["input"]["members"].append(deepcopy(basis["input"]["members"][0]))
        target["values"]["member_count"] += 1
    else:
        target["result_slot"] = "other"
    with pytest.raises(ContractError) as error:
        opening_obligation_references(
            replace(content, reference=ref, source=source, target=target, basis=basis)
        )
    assert error.value.code == ("invalid_input" if damage == "source" else "storage_corrupt")


def test_nonopening_formal_content_is_rejected_before_member_interpretation(tmp_path):
    app, company = assemble(tmp_path)
    contract, _, _ = loan_opening()
    saved = app.accounting_apply(request(company, contract))
    assert saved.get("completion") == "complete", saved
    with pytest.raises(ContractError) as error:
        opening_obligation_references(adopted(app, company, contract["business_id"]))
    assert error.value.code == "unsupported_business"
