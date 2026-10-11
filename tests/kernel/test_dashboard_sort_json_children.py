"""Binary child locators preserve JSON scalar shapes and exact stored proof."""

import json
import sqlite3

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.stored_json import verify_sql_outcomes
from ai_accounting.kernel.types import canonical, digest


@pytest.mark.parametrize("values", [
    {}, {"obligations": None}, {"obligations": []},
    {"obligations": [{"key": "准确来源", "name": "primary", "counterparty_id": "职员"}]},
    {"obligations": [{"key": None, "name": None, "counterparty_id": None}]},
    {"obligations": [{"key": 1.25, "name": 100.0, "counterparty_id": False}]},
    {"obligations": [{"key": ["原值", 1.25, None], "name": {"名称": "甲"},
                      "counterparty_id": ["甲", "乙"]}]},
])
def test_binary_ancestor_fields_equal_text_fields_for_exact_noncanonical_json(values):
    outcome = {"values": values, "balances": [{"key": "准确来源"}, {"key": None}]}
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE calculation(id TEXT PRIMARY KEY,outcome TEXT,digest BLOB)")
        # Reformatting and Unicode escapes require the existing strict decoder's
        # semantic digest fallback. JSONB must not replace this source proof.
        connection.execute("INSERT INTO calculation VALUES(?,?,?)", (
            "source", json.dumps(outcome, ensure_ascii=True, indent=2), digest(outcome),
        ))
        verify_sql_outcomes(connection, {"source"})
        for path in ("$.balances", "$.values.obligations"):
            outputs = []
            for iterator in ("json_each", "jsonb_each"):
                outputs.append([tuple(row) for row in connection.execute(
                    "SELECT json_type(child.value),json_extract(child.value,'$.key'),"
                    "json_extract(child.value,'$.name'),"
                    "json_extract(child.value,'$.counterparty_id') FROM calculation c,"
                    f"{iterator}(c.outcome,?) child", (path,),
                )])
            assert outputs[0] == outputs[1]
            assert all(not isinstance(value, bytes) for row in outputs[1] for value in row)


@pytest.mark.parametrize("corruption", ["duplicate", "escaped_duplicate", "digest"])
def test_binary_child_locators_do_not_replace_strict_original_bytes_proof(corruption):
    outcome = {"values": {"obligations": [{
        "key": "key", "name": "primary", "counterparty_id": "party",
    }]}}
    raw = canonical(outcome)
    if corruption == "duplicate":
        raw = raw.replace(
            '"counterparty_id":"party"', '"counterparty_id":"party","counterparty_id":"other"',
        )
    elif corruption == "escaped_duplicate":
        raw = raw.replace('"counterparty_id":"party"',
                          '"counterparty_id":"party","counterparty_\\u0069d":"other"')
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE calculation(id TEXT PRIMARY KEY,outcome TEXT,digest BLOB)")
        connection.execute("INSERT INTO calculation VALUES(?,?,?)", (
            "source", raw, bytes(32) if corruption == "digest" else digest(outcome),
        ))
        # SQL itself can extract a plausible locator from damaged saved JSON.
        # The unchanged independent proof must still reject it before use.
        assert connection.execute(
            "SELECT json_extract(child.value,'$.counterparty_id') FROM calculation c,"
            "jsonb_each(c.outcome,'$.values.obligations') child",
        ).fetchone()[0] == "party"
        with pytest.raises(KernelError) as error:
            verify_sql_outcomes(connection, {"source"})
        assert error.value.code == "content_integrity_failed"
