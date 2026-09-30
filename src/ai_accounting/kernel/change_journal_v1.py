"""Released v1 read-only interpretation of the private source-change journal.

The event alphabet, sequence rules and authoritative table mapping are fixed
here so a later journal format cannot reinterpret an already closed v1 month.
"""

from __future__ import annotations

from typing import NamedTuple

from .contracts import KernelError

CONTRACT = "ai-accounting-kernel/2/source-change/1"
SOURCES = (
    "fact",
    "calculation",
    "pending",
    "identity",
    "duplicate",
    "inventory",
    "inventory_item",
    "publication",
)


class SourceChange(NamedTuple):
    sequence: int
    source: str
    target_id: str
    before_ref: str | None
    after_ref: str | None


def _invalid(reason):
    raise KernelError(
        "content_integrity_failed",
        "资料变化记录与权威来源不一致",
        component="source_change",
        record_id="*",
        reason=reason,
    )


def head(connection) -> int:
    row = connection.execute(
        "SELECT h.sequence,(SELECT max(sequence) FROM source_change) "
        "FROM source_change_head h WHERE id=1"
    ).fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0 or row[0] != (row[1] or 0):
        _invalid("journal_head_mismatch")
    return row[0]


def changes_since(connection, after: int, *, through: int | None = None):
    latest = head(connection)
    through = latest if through is None else through
    if type(after) is not int or type(through) is not int or not 0 <= after <= through <= latest:
        _invalid("journal_interval_invalid")
    result = []
    expected = after + 1
    for row in connection.execute(
        "SELECT sequence,source,target_id,before_ref,after_ref FROM source_change "
        "WHERE sequence>? AND sequence<=? ORDER BY sequence",
        (after, through),
    ):
        sequence, source, target, before, next_ref = row
        if (
            sequence != expected
            or source not in SOURCES
            or not isinstance(target, str)
            or not target
            or before == next_ref
            or (before is not None and (not isinstance(before, str) or not before))
            or (next_ref is not None and (not isinstance(next_ref, str) or not next_ref))
            or (
                source not in {"fact", "calculation", "pending"}
                and (before is not None or next_ref is None)
            )
            or (source == "pending" and (before is None) == (next_ref is None))
        ):
            _invalid("journal_entry_invalid")
        result.append(SourceChange(sequence, source, target, before, next_ref))
        expected += 1
    if expected != through + 1:
        _invalid("journal_entry_missing")
    return tuple(result)


def heads_at(connection, through: int) -> dict[str, dict[str, str]]:
    result = {"fact": {}, "calculation": {}}
    for event in changes_since(connection, 0, through=through):
        if event.source not in result:
            continue
        selected = result[event.source]
        if selected.get(event.target_id) != event.before_ref:
            _invalid("journal_head_chain_mismatch")
        if event.after_ref is None:
            selected.pop(event.target_id, None)
        else:
            selected[event.target_id] = event.after_ref
    return result


def verify_journal(connection) -> int:
    """Rebuild v1 selection and inventory coverage from the immutable events."""
    events = changes_since(connection, 0)
    heads = {"fact": {}, "calculation": {}}
    pending = set()
    recorded = {source: set() for source in SOURCES}
    for event in events:
        if event.source in heads:
            selected = heads[event.source]
            if selected.get(event.target_id) != event.before_ref:
                _invalid("journal_head_chain_mismatch")
            if event.after_ref is None:
                selected.pop(event.target_id, None)
            else:
                selected[event.target_id] = event.after_ref
                recorded[event.source].add((event.target_id, event.after_ref))
        elif event.source == "pending":
            key = event.target_id, event.before_ref or event.after_ref
            if (event.before_ref is None) == (key in pending):
                _invalid("journal_pending_chain_mismatch")
            if event.before_ref is None:
                pending.add(key)
            else:
                pending.remove(key)
        else:
            key = event.target_id, event.after_ref
            if key in recorded[event.source]:
                _invalid("journal_record_duplicated")
            recorded[event.source].add(key)
    for source, table, reference in (
        ("fact", "fact_current", "fact_id"),
        ("calculation", "calculation_current", "calculation_id"),
    ):
        actual = dict(connection.execute(f"SELECT subject_id,{reference} FROM {table}"))
        if actual != heads[source]:
            _invalid("journal_current_heads_mismatch")
    if pending != {
        tuple(row) for row in connection.execute("SELECT subject_id,cause_id FROM pending")
    }:
        _invalid("journal_pending_heads_mismatch")
    sources = (
        ("fact", "SELECT subject_id,id FROM fact_revision"),
        ("calculation", "SELECT subject_id,id FROM calculation"),
        ("identity", "SELECT subject_id,id FROM identity_correction_item"),
        ("duplicate", "SELECT proposed_subject_id,id FROM business_duplicate_check"),
        ("inventory", "SELECT period||':'||category,CAST(id AS TEXT) FROM material_revision"),
        (
            "inventory_item",
            "SELECT CAST(inventory_id AS TEXT),lower(hex(evidence_digest)) FROM material_item",
        ),
        ("publication", "SELECT subject_id,id FROM calculation_publication"),
    )
    for source, query in sources:
        actual = {tuple(row) for row in connection.execute(query)}
        if (source == "calculation" and not recorded[source] <= actual) or (
            source != "calculation" and recorded[source] != actual
        ):
            _invalid("journal_source_coverage_mismatch")
    return len(events)
