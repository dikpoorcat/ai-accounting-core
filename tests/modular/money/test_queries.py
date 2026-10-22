"""Money's private wire conversion preserves typed values and independent output."""

from dataclasses import asdict, is_dataclass
from types import SimpleNamespace

from ai_accounting_modular.contracts.public import canonical_json, content_digest
from ai_accounting_modular.money._queries import version, wire
from ai_accounting_modular.money._read_types import (
    AccountActivity,
    MovementQuery,
    SettlementPart,
    StatementActivity,
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
    source = {"business_id": COMPANY, "slot": "primary"}
    statement = StatementActivity(1, 2, 9007199254740993, 1, "2026-01-31", "complete")
    return {
        "query": MovementQuery(),
        "accounts": [
            AccountActivity(
                COMPANY,
                "bank_account",
                "合成银行账户",
                9007199254740993,
                1,
                2,
                "2026-01-31",
                statement,
                "attention",
                -1,
            )
        ],
        "movements": (
            {
                "allocations": (
                    SettlementPart(source, 9007199254740993, None),
                    SettlementPart(source, 1, COMPANY),
                ),
                "amount_fen": "9007199254740993",
            },
        ),
        "flags": [True, False, None],
    }


def test_actual_money_shapes_keep_wire_bytes_and_scope_version():
    value = sample()
    expected = previous_wire(value)
    assert canonical_json(wire(value)) == canonical_json(expected)
    assert wire(value)["accounts"][0]["inflow_fen"] == 9007199254740993
    assert wire(value)["movements"][0]["amount_fen"] == "9007199254740993"
    scope = {
        "company_id": COMPANY,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "as_posted",
    }
    read = SimpleNamespace(company_id=COMPANY, database_id=DATABASE)
    assert version(scope, read, "account_activity", value) == content_digest(
        {
            "company_id": COMPANY,
            "database_id": DATABASE,
            "scope": dict(scope, as_of="2026-01"),
            "selection": {"view": "as_posted", "as_of": "2026-01"},
            "collection": "account_activity",
            "data": expected,
        }
    )


def test_nested_money_values_are_fresh_in_both_directions_and_do_not_preserve_aliases():
    value = sample()
    expected = previous_wire(value)
    result = wire(value)
    result["movements"][0]["allocations"][0]["source"]["slot"] = "changed"
    result["accounts"][0]["statement"]["row_count"] = 0
    assert previous_wire(value) == expected
    assert result["movements"][0]["allocations"][1]["source"]["slot"] == "primary"

    saved = wire(value)
    value["movements"][0]["allocations"][0].source["slot"] = "later"
    value["accounts"].clear()
    value["flags"].append("later")
    assert saved == expected
    assert wire(value) == previous_wire(value)
