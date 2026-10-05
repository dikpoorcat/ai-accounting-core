"""Exact child coordinates keep their complete proof without row transfer."""

import sqlite3
from types import SimpleNamespace

import pytest
from test_cash import book as book  # noqa: F401

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_fact_child_order
from ai_accounting.kernel.schema import sequence_model, table_name
from ai_accounting.kernel.schema_bundle import production_bundle

MODELS = production_bundle().registry.models
CHILDREN = [(kind, name) for kind, model in MODELS.items()
            for name, field in model.model_fields.items()
            if sequence_model(field.annotation) is not None]


@pytest.fixture
def coordinates():
    # The minimal SQLite fixture reproduces the real coordinate PK/type
    # contract. Business fact values and hashing are covered by integration.
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    for kind, name in CHILDREN:
        connection.execute(
            f"CREATE TABLE {table_name(kind)}_{name}(revision_id TEXT NOT NULL,"
            "item_no INTEGER NOT NULL CHECK(item_no>=0),"
            "PRIMARY KEY(revision_id,item_no)) STRICT"
        )
    yield connection, SimpleNamespace(
        store=SimpleNamespace(registry=SimpleNamespace(models=MODELS))
    )
    connection.close()


def test_all_current_child_declarations_have_the_unique_integer_coordinate_contract(book):
    with book[0].store.connection(read_only=True) as connection:
        assert len(CHILDREN) == 43
        for kind, name in CHILDREN:
            table = f"{table_name(kind)}_{name}"
            info = {row["name"]: row for row in connection.execute(f"PRAGMA table_info({table})")}
            assert info["revision_id"]["pk"] == 1
            assert (info["item_no"]["type"], info["item_no"]["notnull"], info["item_no"]["pk"]) == (
                "INTEGER", 1, 2,
            )
            sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name=?", (table,)
            ).fetchone()[0]
            assert "CHECK(item_no>=0)" in sql and sql.endswith("STRICT")


@pytest.mark.parametrize("kind,name", CHILDREN)
def test_every_child_family_keeps_empty_exact_scope_and_detects_coordinate_holes(
    coordinates, kind, name
):
    connection, engine = coordinates
    table = f"{table_name(kind)}_{name}"
    connection.executemany(f"INSERT INTO {table} VALUES(?,?)", [
        ("selected", 0), ("selected", 1), ("selected", 2), ("unrelated", 7),
    ])
    verify_fact_child_order(engine, connection, {kind: set()})
    verify_fact_child_order(engine, connection, {kind: {"selected", "empty"}})
    connection.execute(f"UPDATE {table} SET item_no=3 WHERE revision_id='selected' AND item_no=1")
    with pytest.raises(KernelError) as failure:
        verify_fact_child_order(engine, connection, {kind: {"selected"}})
    assert failure.value.details == {
        "component": "fact", "record_id": "selected", "reason": "fact_child_order_mismatch",
    }


@pytest.mark.parametrize("position", [1.0, 1.5, "1", "bad", b"1", None, -1, 2**63 - 1])
def test_untrusted_coordinate_types_and_bounds_are_explicitly_rejected(position):
    # A non-STRICT fixture exercises the aggregate's explicit type guard;
    # real company DDL rejects these storage types before the checker runs.
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE fact_child_items(revision_id TEXT,item_no,PRIMARY KEY(revision_id,item_no))"
    )
    model = SimpleNamespace(
        model_fields={"items": MODELS["bank_statement"].model_fields["entries"]}
    )
    engine = SimpleNamespace(
        store=SimpleNamespace(registry=SimpleNamespace(models={"child": model}))
    )
    try:
        connection.executemany(
            "INSERT INTO fact_child_items VALUES('selected',?)", [(0,), (position,)]
        )
        with pytest.raises(KernelError) as failure:
            verify_fact_child_order(engine, connection, {"child": {"selected"}})
        assert failure.value.details["reason"] == "fact_child_order_mismatch"
    finally:
        connection.close()


def test_bounded_order_proof_returns_no_child_rows_and_keeps_v1_row_rule(coordinates):
    connection, engine = coordinates
    table = "fact_bank_statement_entries"
    connection.executemany(f"INSERT INTO {table} VALUES(?,?)", [
        ("selected", index) for index in range(6000)
    ] + [("unrelated", index) for index in range(6000)])
    queries, returned = [], []

    class Observed:
        def execute(self, sql, parameters):
            queries.append(sql)
            rows = list(connection.execute(sql, parameters))
            returned.extend(rows)
            return rows

    verify_fact_child_order(engine, Observed(), {"bank_statement": {"selected"}})
    assert returned == []
    assert len(queries) == 1
    # The old rule still transfers/checks every selected coordinate. It is a
    # separate fixed-version model carrier, never a current success marker.
    original = MODELS["bank_statement"].model_fields["entries"]
    model = SimpleNamespace(model_fields={"entries": original},
                            _v1_fields={"entries": {"kind": "sequence"}})
    historical = SimpleNamespace(
        store=SimpleNamespace(registry=SimpleNamespace(models={"bank_statement": model}))
    )
    queries.clear()
    verify_fact_child_order(historical, Observed(), {"bank_statement": {"selected"}})
    assert len(returned) == 6000
    assert all(row["revision_id"] == "selected" for row in returned)
    assert "GROUP BY" not in queries[0]
    connection.execute(f"DELETE FROM {table} WHERE revision_id='selected' AND item_no=20")
    for selected_engine in (engine, historical):
        with pytest.raises(KernelError):
            verify_fact_child_order(selected_engine, connection, {"bank_statement": {"selected"}})
