"""The design's named opening examples execute unchanged through the public app."""

import json
from pathlib import Path

import pytest

from ._support import assemble, request, result, source

_CASES = json.loads(
    (Path(__file__).parents[4] / "docs/redesign/examples/business-facts-cases.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case_id", ["B44", "B44Z"])
def test_fixed_opening_examples_have_exact_totals_and_no_historical_journal(tmp_path, case_id):
    case = next(item for item in _CASES["valid_cases"] if item["case_id"] == case_id)
    item = case["when"]
    app, company = assemble(tmp_path, start_period=item["period"])
    submitted = request(company, item)
    submitted["objects"] = [
        {
            "object_id": _CASES["object_ids"]["bank"],
            "kind": "bank_account",
            "name": "固定样例银行",
            "expected_seq": 0,
            "data": {
                "bank_name": "合成银行",
                "account_number": "fixed-opening-001",
                "currency": "CNY",
                "owner": "company",
            },
        },
        {
            "object_id": _CASES["object_ids"]["owner"],
            "kind": "person",
            "name": "固定样例老板",
            "expected_seq": 0,
            "data": {},
        },
    ]
    submitted["sources"] = [source(item["evidence_ids"][0])]
    actual = app.accounting_apply(submitted)
    assert actual.get("completion") == "complete", actual
    view = result(app, company, item)
    assert view["vouchers"] == []
    assert view["target"]["values"]["debit_fen"] == case["then"]["debit_fen"]
    assert view["target"]["values"]["credit_fen"] == case["then"]["credit_fen"]
