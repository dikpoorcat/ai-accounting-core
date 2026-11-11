"""Unit-action evidence freezes existing members without a second inventory event."""

from copy import deepcopy

import pytest

from ._investment_support import acquire, source, terms
from ._support import PRODUCT, SOURCE, assemble, fact, result
from .test_investment_lifecycle import apply_facts


def _changes(holding, purchases):
    return [
        fact(
            "investment.unit_change",
            {
                "terms_id": holding["business_id"],
                "source": source(purchased),
                "quantity_after": units,
                "transaction_sequence": sequence,
            },
            actual="2026-01-04",
        )
        for sequence, (purchased, units) in enumerate(zip(purchases, ("6", "10"), strict=True), 1)
    ]


def _basis(changes):
    return fact(
        "investment.unit_change_basis",
        {
            "product_id": PRODUCT,
            "action_reference": "合成同一产品份额拆分公告001",
            "effective_on": "2026-01-04",
            "from_units": "1",
            "to_units": "2",
            "unit_change_sources": [
                {"business_id": item["business_id"], "slot": "main"} for item in changes
            ],
            "source": {"source_id": SOURCE, "location": "合成真实公告原始单位1份换2份"},
        },
    )


def test_sourced_action_adopts_precise_existing_results_without_inventory_or_income(tmp_path):
    app, company = assemble(tmp_path)
    holding = terms()
    purchased = [acquire(holding, "300", "3"), acquire(holding, "500", "5", "2026-01-03")]
    changed = _changes(holding, purchased)
    apply_facts(app, company, holding, *purchased, *changed, initial=True)
    before = [deepcopy(result(app, company, item)) for item in changed]
    basis = _basis(changed)
    apply_facts(app, company, basis)
    body = result(app, company, basis)
    assert body["vouchers"] == []
    assert "economic_event" not in body["target"]["values"]
    assert body["target"]["values"]["members"] == body["basis"]["source_results"]
    assert {row["business_id"] for row in body["target"]["values"]["members"]} == {
        item["business_id"] for item in changed
    }
    assert [result(app, company, item) for item in changed] == before


@pytest.mark.parametrize("invalid", ["ratio", "known_day", "duplicate_member", "wrong_kind"])
def test_action_evidence_cannot_relabel_or_duplicate_its_formal_members(tmp_path, invalid):
    app, company = assemble(tmp_path)
    holding = terms()
    purchased = [acquire(holding, "300", "3"), acquire(holding, "500", "5", "2026-01-03")]
    changed = _changes(holding, purchased)
    apply_facts(app, company, holding, *purchased, *changed, initial=True)
    basis = _basis(changed)
    if invalid == "ratio":
        basis["data"]["to_units"] = "3"
    elif invalid == "known_day":
        basis["data"]["effective_on"] = "2026-01-05"
    elif invalid == "duplicate_member":
        apply_facts(app, company, basis)
        basis = _basis(changed)
        basis["data"]["action_reference"] = "另一个公告不能再次占用同一正式变化"
    else:
        basis["data"]["unit_change_sources"][0]["business_id"] = purchased[0]["business_id"]
    from ._support import request

    payload = request(company, basis)
    payload.pop("objects")
    payload.pop("sources")
    rejected = app.accounting_apply(payload)
    assert rejected["error"] == "invalid_input", rejected
