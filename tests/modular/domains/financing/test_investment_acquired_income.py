"""The acquired pre-existing right keeps its known interest or distribution identity."""

import pytest

from ._investment_support import acquire, terms
from ._support import PRODUCT, SOURCE, assemble, request, result
from .test_investment_lifecycle import apply_facts


def contract():
    holding = terms(product_type="bank_wealth", measurement="other_financial_cost")
    holding["data"].update(
        legal_form="financial_contract",
        tax_facts={
            "instrument_type": "bank_wealth",
            "principal_protected": False,
            "source": {"source_id": SOURCE, "location": "合成金融合同分配权利"},
        },
    )
    return holding


def acquired_right(holding):
    purchased = acquire(holding, "11000", "100")
    purchased["data"]["separate_declared_income_fen"] = "1000"
    return purchased


def right_accounts(body):
    return {
        line["account_code"]
        for voucher in body["vouchers"]
        for line in voucher["lines"]
        if line["account_code"] in {"1131", "1132"} and line["debit_fen"] == "1000"
    }


@pytest.mark.parametrize(
    ("character", "account"), [("interest", "1132"), ("market_distribution", "1131")]
)
def test_financial_contract_existing_right_uses_sourced_character(tmp_path, character, account):
    app, company = assemble(tmp_path)
    holding = contract()
    purchased = acquired_right(holding)
    purchased["data"]["separate_income_character"] = character
    apply_facts(app, company, holding, purchased, initial=True)
    body = result(app, company, purchased)
    assert body["target"]["values"]["cost_fen"] == "10000"
    assert right_accounts(body) == {account}
    assert body["target"]["values"]["tax_event"]["separate_income_character"] == character
    assert body["basis"]["input"]["separate_income_character"] == character


def test_financial_contract_unknown_existing_right_needs_source_but_zero_does_not(tmp_path):
    app, company = assemble(tmp_path)
    holding = contract()
    purchased = acquired_right(holding)
    payload = request(company, holding, purchased)
    for obj in payload["objects"]:
        if obj["object_id"] == PRODUCT:
            obj["data"]["category"] = "bank_wealth"
    rejected = app.accounting_apply(payload)
    assert rejected["error"] == "needs_information", rejected
    assert "/data/separate_income_character" in str(rejected)
    purchased["data"]["separate_declared_income_fen"] = "0"
    apply_facts(app, company, holding, purchased, initial=True)
    assert result(app, company, purchased)["target"]["values"]["cost_fen"] == "11000"


def test_partnership_known_noninterest_distribution_requires_no_new_label(tmp_path):
    app, company = assemble(tmp_path)
    holding = terms(product_type="equity", measurement="long_equity_cost")
    holding["data"].update(
        legal_form="partnership_interest",
        tax_facts={
            "instrument_type": "partnership",
            "source": {"source_id": SOURCE, "location": "合成合伙分配权利"},
        },
    )
    purchased = acquired_right(holding)
    apply_facts(app, company, holding, purchased, initial=True)
    assert right_accounts(result(app, company, purchased)) == {"1131"}
