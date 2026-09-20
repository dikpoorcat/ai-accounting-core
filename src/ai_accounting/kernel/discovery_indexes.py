"""Narrow rebuildable indexes for ordered fact discovery."""

from __future__ import annotations

from .contracts import KernelError
from .types import canonical

DISCOVERY_INDEX_DDL = """
CREATE TABLE discovery_fact_history(
 fact_id TEXT PRIMARY KEY REFERENCES fact_revision(id),
 subject_id TEXT NOT NULL REFERENCES subject(id),revision INTEGER NOT NULL CHECK(revision>0),
 kind TEXT NOT NULL,period INTEGER NOT NULL CHECK(period BETWEEN 0 AND 119987)) STRICT;
CREATE INDEX discovery_history_order
 ON discovery_fact_history(period DESC,fact_id DESC);
CREATE INDEX discovery_history_kind_order
 ON discovery_fact_history(kind,period DESC,fact_id DESC);
CREATE TABLE discovery_fact_current(
 subject_id TEXT PRIMARY KEY REFERENCES subject(id),
 fact_id TEXT NOT NULL UNIQUE REFERENCES discovery_fact_history(fact_id),kind TEXT NOT NULL,
 period INTEGER NOT NULL CHECK(period BETWEEN 0 AND 119987)) STRICT;
CREATE INDEX discovery_current_order
 ON discovery_fact_current(period DESC,fact_id DESC);
CREATE INDEX discovery_current_kind_order
 ON discovery_fact_current(kind,period DESC,fact_id DESC);
"""


def _failed(record_id, reason):
    raise KernelError(
        "content_integrity_failed",
        "事实发现索引不完整或不一致",
        component="fact_discovery",
        record_id=record_id,
        reason=reason,
    )


def sync_discovery_subjects(connection, subject_ids):
    """Synchronize affected current pointers; immutable history is inserted once."""
    subjects = sorted(set(subject_ids))
    if not subjects:
        return
    encoded = canonical(subjects)
    connection.execute(
        "DELETE FROM discovery_fact_current WHERE subject_id IN (SELECT value FROM json_each(?))",
        (encoded,),
    )
    connection.execute(
        "INSERT INTO discovery_fact_current(subject_id,fact_id,kind,period) "
        "SELECT h.subject_id,h.fact_id,h.kind,h.period FROM json_each(?) ids "
        "JOIN fact_current c ON c.subject_id=ids.value "
        "JOIN discovery_fact_history h ON h.fact_id=c.fact_id",
        (encoded,),
    )


def sync_discovery_fact(connection, fact_id):
    row = connection.execute(
        "SELECT f.id,f.subject_id,f.revision,s.kind,f.period FROM fact_revision f "
        "JOIN subject s ON s.id=f.subject_id WHERE f.id=?",
        (fact_id,),
    ).fetchone()
    if row is None:
        _failed(fact_id, "missing_source_fact")
    connection.execute("INSERT INTO discovery_fact_history VALUES(?,?,?,?,?)", tuple(row))
    sync_discovery_subjects(connection, (row["subject_id"],))


def verify_discovery_indexes(connection):
    mismatch = connection.execute(
        "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
        "LEFT JOIN discovery_fact_history h ON h.fact_id=f.id AND h.subject_id=f.subject_id "
        "AND h.revision=f.revision AND h.kind=s.kind AND h.period=f.period "
        "WHERE h.fact_id IS NULL UNION ALL "
        "SELECT h.fact_id FROM discovery_fact_history h LEFT JOIN fact_revision f "
        "ON f.id=h.fact_id WHERE f.id IS NULL LIMIT 1"
    ).fetchone()
    if mismatch is not None:
        _failed(mismatch[0], "history_mismatch")
    mismatch = connection.execute(
        "SELECT c.subject_id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
        "JOIN subject s ON s.id=f.subject_id LEFT JOIN discovery_fact_current d "
        "ON d.subject_id=c.subject_id AND d.fact_id=c.fact_id AND d.kind=s.kind "
        "AND d.period=f.period WHERE d.subject_id IS NULL UNION ALL "
        "SELECT d.subject_id FROM discovery_fact_current d LEFT JOIN fact_current c "
        "ON c.subject_id=d.subject_id AND c.fact_id=d.fact_id "
        "WHERE c.subject_id IS NULL LIMIT 1"
    ).fetchone()
    if mismatch is not None:
        _failed(mismatch[0], "current_mismatch")


def rebuild_discovery_indexes(connection):
    expected_history = {
        tuple(row)
        for row in connection.execute(
            "SELECT f.id,f.subject_id,f.revision,s.kind,f.period FROM fact_revision f "
            "JOIN subject s ON s.id=f.subject_id"
        )
    }
    actual_history = {
        tuple(row)
        for row in connection.execute(
            "SELECT fact_id,subject_id,revision,kind,period FROM discovery_fact_history"
        )
    }
    expected_current = {
        tuple(row)
        for row in connection.execute(
            "SELECT f.subject_id,f.id,s.kind,f.period FROM fact_current c "
            "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id"
        )
    }
    actual_current = {
        tuple(row)
        for row in connection.execute(
            "SELECT subject_id,fact_id,kind,period FROM discovery_fact_current"
        )
    }
    if actual_history == expected_history and actual_current == expected_current:
        return False
    connection.execute("DELETE FROM discovery_fact_current")
    connection.execute("DELETE FROM discovery_fact_history")
    connection.executemany(
        "INSERT INTO discovery_fact_history VALUES(?,?,?,?,?)", sorted(expected_history)
    )
    connection.executemany(
        "INSERT INTO discovery_fact_current VALUES(?,?,?,?)", sorted(expected_current)
    )
    verify_discovery_indexes(connection)
    return True
