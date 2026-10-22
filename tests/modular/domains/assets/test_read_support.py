"""Asset wire values keep their established bytes and own all nested containers."""

from dataclasses import asdict, is_dataclass
from types import SimpleNamespace

from ai_accounting_modular.contracts.public import canonical_json, content_digest
from ai_accounting_modular.domains.assets._read_support import identity, wire
from ai_accounting_modular.domains.assets._read_types import (
    AssetLedgerAmounts,
    AssetPaymentSummary,
    ProjectCostTotals,
)

COMPANY = "00000000-0000-4000-8000-000000000001"
DATABASE = "00000000-0000-4000-8000-000000000002"


def previous_wire(value):
    """Independent pre-change conversion, including its dataclass deep copy."""
    if is_dataclass(value):
        return previous_wire(asdict(value))
    if isinstance(value, dict):
        return {key: previous_wire(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [previous_wire(item) for item in value]
    return value


def sample():
    known = {"state": "known", "amount_fen": "9007199254740993"}
    unknown = {"state": "unestablished", "issue_indexes": [0]}
    issue = {
        "code": "formal_asset_consumption_missing",
        "field": "/fixed_accumulated",
        "meaning": "accounting",
        "message": "缺少正式消耗结果",
        "allowed_precision": ["month"],
        "reusable_sources": [{"source_id": COMPANY, "location": "合成卡片第1行"}],
        "blocking_scope": "business",
    }
    return {
        "period": "2026-01",
        "actual_on": "2026-01-31",
        "payment": AssetPaymentSummary(
            "individual",
            ({"business_id": COMPANY, "slot": "primary"},),
            1,
            9007199254740993,
            0,
            -1,
            9007199254740994,
        ),
        "totals": (ProjectCostTotals(1, 10001, 1, 10000),),
        "ledger": AssetLedgerAmounts(
            known, unknown, unknown, known, known, known, known, known, known, (issue,)
        ),
        "other": [None, True, False, {"date": None}],
    }


def test_actual_asset_value_shapes_keep_wire_bytes_and_identity():
    value = sample()
    expected = previous_wire(value)
    assert canonical_json(wire(value)) == canonical_json(expected)
    assert wire(value)["payment"]["amount_fen"] == 9007199254740993
    assert wire(value)["ledger"]["fixed_cost"]["amount_fen"] == "9007199254740993"
    read = SimpleNamespace(company_id=COMPANY, database_id=DATABASE)
    assert identity(read, "asset_activity", value) == content_digest(
        {
            "company_id": COMPANY,
            "database_id": DATABASE,
            "collection": "asset_activity",
            "value": expected,
        }
    )


def test_nested_asset_values_are_fresh_in_both_directions_and_do_not_preserve_aliases():
    value = sample()
    expected = previous_wire(value)
    result = wire(value)
    result["payment"]["sources"][0]["slot"] = "changed"
    result["ledger"]["fixed_accumulated"]["issue_indexes"].append(1)
    result["ledger"]["issues"][0]["reusable_sources"][0]["location"] = "changed"
    assert previous_wire(value) == expected
    assert result["ledger"]["fixed_net"]["issue_indexes"] == [0]

    saved = wire(value)
    value["payment"].sources[0]["slot"] = "later"
    value["ledger"].issues[0]["allowed_precision"].append("day")
    value["other"].append("later")
    assert saved == expected
    assert wire(value) == previous_wire(value)
