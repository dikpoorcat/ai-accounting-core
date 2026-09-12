"""Damaged frozen references stay visible without becoming established payments.

The mismatch is injected only into temporary tables of an in-memory copy of a
synthetic book. It is not a state constructible through normal business writes.
The real selector, slot SQL, relation resolver and shared reducer are exercised.
"""

import json
import sqlite3
from contextlib import closing

import pytest
from test_deletion_boundaries import book as domain_book_fixture
from test_deletion_boundaries import expense, payment, prepare_payment

from ai_accounting.kernel.business_queries import BusinessQueries

domain_book = domain_book_fixture


@pytest.fixture
def mismatched_frozen_payment(domain_book):
    engine, save, publish, *_ = domain_book
    prepare_payment(domain_book)
    save("expense", "expense-b", expense())
    publish("expense-b")
    forty = payment() | {
        "amount_fen": 40,
        "allocations": [payment()["allocations"][0] | {"amount_fen": 40}],
    }
    save("cash_payment", "payment", forty, revision=1, amend=True)
    publish("payment")
    twenty = payment() | {
        "amount_fen": 20,
        "allocations": [payment()["allocations"][0] | {"amount_fen": 20}],
    }
    save("cash_payment", "later-payment", twenty)
    publish("later-payment")
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        with engine.store.connection(read_only=True) as source:
            source.backup(connection)
        # Shadow only synthetic calculation/edge data, leaving immutable source
        # facts (declared expense A), source IDs and all real query paths intact.
        connection.execute("CREATE TEMP TABLE calculation AS SELECT * FROM main.calculation")
        for name, columns in (
            ("calculation_subject", "subject_id,id"),
            ("calculation_kind_period", "kind,period,id"),
            (
                "calculation_obligations",
                "json_array_length(outcome,'$.values.obligations'),subject_id,id",
            ),
        ):
            connection.execute(f"CREATE INDEX temp.{name} ON calculation({columns})")
        connection.execute(
            "CREATE TEMP TABLE dependency_calculation AS SELECT * FROM main.dependency_calculation"
        )
        connection.execute(
            "CREATE INDEX temp.dependency_upstream "
            "ON dependency_calculation(upstream_id,calculation_id)"
        )
        identifiers = {
            row["subject_id"]: row["calculation_id"]
            for row in connection.execute("SELECT * FROM calculation_current")
        }
        damaged_id, source_a, source_b = (
            identifiers["payment"],
            identifiers["expense"],
            identifiers["expense-b"],
        )
        outcome = json.loads(
            connection.execute(
                "SELECT outcome FROM calculation WHERE id=?", (damaged_id,)
            ).fetchone()[0]
        )
        frozen = outcome["values"]["settlements"][0]
        assert frozen["source_calculation"] == source_a
        original_key = frozen["obligation"]
        frozen["source_calculation"] = source_b
        connection.execute(
            "UPDATE calculation SET outcome=? WHERE id=?", (json.dumps(outcome), damaged_id)
        )
        connection.execute(
            "UPDATE dependency_calculation SET upstream_id=? "
            "WHERE calculation_id=? AND upstream_id=?",
            (source_b, damaged_id, source_a),
        )
        connection.commit()
        connection.execute("BEGIN")
        yield BusinessQueries(engine), connection, identifiers, original_key


def test_declared_and_frozen_sources_share_unresolved_slot_pagination(mismatched_frozen_payment):
    queries, connection, identifiers, original_key = mismatched_frozen_payment
    for current in (False, True):
        first = queries.business_collection(
            connection,
            "expense",
            "2026-01",
            section="settlement_events",
            limit=1,
            current=current,
        )
        assert first["page"]["total_count"] == first["page"]["filtered_count"] == 2
        assert first["page"]["returned_count"] == 1 and first["page"]["has_more"]
        damaged = first["items"][0]
        assert damaged["state"] == "unresolved"
        assert damaged["source_business"]["subject_id"] == "expense-b"
        assert damaged["source_calculation_id"] == identifiers["expense-b"]
        assert (
            damaged["source_fact_id"]
            == connection.execute(
                "SELECT fact_id FROM calculation WHERE id=?", (identifiers["expense-b"],)
            ).fetchone()[0]
        )
        assert damaged["obligation_key"] == original_key
        assert damaged["amount_fen"] == 40 and damaged["issues"]
        second = queries.business_collection(
            connection,
            "expense",
            "2026-01",
            section="settlement_events",
            limit=1,
            after=first["page"]["next_cursor"],
            current=current,
        )
        assert second["page"]["total_count"] == 2
        assert second["page"]["returned_count"] == 1 and not second["page"]["has_more"]
        assert second["items"][0]["id"] != damaged["id"]
        assert second["items"][0]["state"] == "resolved"
        assert second["items"][0]["amount_fen"] == 20
        source_b = queries.business_collection(
            connection,
            "expense-b",
            "2026-01",
            section="settlement_events",
            current=current,
        )
        assert source_b["page"]["total_count"] == 1
        assert source_b["items"] == [damaged]
        union = queries.business_collection(
            connection,
            {"expense", "expense-b"},
            "2026-01",
            section="settlement_events",
            current=current,
        )
        assert union["page"]["total_count"] == 2
        assert len({item["id"] for item in union["items"]}) == 2


def test_unresolved_source_mismatch_propagates_unknown_without_reassigning_payment(
    mismatched_frozen_payment,
):
    queries, connection, identifiers, original_key = mismatched_frozen_payment
    for current in (False, True):
        full = queries.settlements(connection, "2026-01", subject_ids={"expense"}, current=current)
        summary = queries.settlement_summary(
            connection,
            "2026-01",
            subject_ids={"expense"},
            current=current,
        )
        assert full["status"] == summary["status"] == "partially_established"
        assert summary["movement_count"] == len(full["movements"]) == 2
        assert summary["complete"] is False and summary["issues"]
        obligation = summary["obligations"][0]
        assert obligation["key"] == original_key
        assert obligation["source_amount_fen"] == 100
        assert obligation["paid_fen"] is None and obligation["period_paid_fen"] is None
        assert obligation["remaining_fen"] is None
        assert obligation["settlement_status"] == "unestablished"
        assert obligation["other_settled_fen"] == 0
        damaged = next(item for item in full["movements"] if item["state"] == "unresolved")
        assert damaged["source_calculation_id"] == identifiers["expense-b"]
        assert damaged["amount_fen"] == 40
        source_b = queries.settlement_summary(
            connection,
            "2026-01",
            subject_ids={"expense-b"},
            current=current,
        )
        assert source_b["status"] == "partially_established" and source_b["complete"] is False
        assert source_b["movement_count"] == 1
        assert source_b["obligations"][0]["source_amount_fen"] == 100
        assert source_b["obligations"][0]["paid_fen"] == 0
        assert source_b["obligations"][0]["remaining_fen"] == 100
        assert source_b["obligations"][0]["key"] != original_key
        assert source_b["obligations"][0]["settlement_status"] == "open"
    selected = queries._selected_accounting(
        connection,
        {"expense", "expense-b", "payment", "later-payment"},
        "2026-01",
        include_lines=False,
    )
    unrelated = queries._settlements(
        connection,
        {"unrelated-c"},
        selected,
        relation_selected=selected,
        summary=True,
    )
    assert unrelated["status"] == "not_established" and unrelated["issues"] == []
    assert unrelated["business_count"] == unrelated["movement_count"] == 0
    assert unrelated["line_relation_count"] == 0
