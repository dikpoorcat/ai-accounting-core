"""Exact source changes used to locate work after an authenticated close.

The journal records database changes, not calls, conversations or accounting
decisions. Triggers append it in the same transaction as its authoritative
sources. It cannot be rebuilt or used instead of checking a selected source.
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


def _append(source, target, before, after):
    return (
        "INSERT INTO source_change(sequence,source,target_id,before_ref,after_ref) "
        f"SELECT sequence+1,'{source}',{target},{before},{after} "
        "FROM source_change_head WHERE id=1;"
    )


def journal_ddl():
    """Fixed internal triggers; no caller-provided names or SQL."""
    sql = [
        "CREATE TABLE source_change_head(id INTEGER PRIMARY KEY CHECK(id=1),"
        "sequence INTEGER NOT NULL CHECK(sequence>=0)) STRICT;",
        "INSERT INTO source_change_head VALUES(1,0);",
        "CREATE TABLE source_change(sequence INTEGER PRIMARY KEY CHECK(sequence>0),"
        "source TEXT NOT NULL CHECK(source IN("
        + ",".join(f"'{name}'" for name in SOURCES)
        + ")),target_id TEXT NOT NULL,before_ref TEXT,after_ref TEXT,"
        "CHECK(before_ref IS NOT after_ref)) STRICT;",
        "CREATE INDEX source_change_reference ON source_change(source,after_ref,sequence);",
        "CREATE TRIGGER source_change_order BEFORE INSERT ON source_change WHEN "
        "NEW.sequence IS NOT (SELECT sequence+1 FROM source_change_head WHERE id=1) "
        "BEGIN SELECT RAISE(ABORT,'source change sequence mismatch'); END;",
        "CREATE TRIGGER source_change_advance AFTER INSERT ON source_change BEGIN "
        "UPDATE source_change_head SET sequence=NEW.sequence WHERE id=1; END;",
        "CREATE TRIGGER source_change_head_order BEFORE UPDATE ON source_change_head WHEN "
        "NEW.id IS NOT OLD.id OR NEW.sequence<>OLD.sequence+1 OR NOT EXISTS("
        "SELECT 1 FROM source_change WHERE sequence=NEW.sequence) "
        "BEGIN SELECT RAISE(ABORT,'source change head mismatch'); END;",
        "CREATE TRIGGER source_change_head_retained BEFORE DELETE ON source_change_head "
        "BEGIN SELECT RAISE(ABORT,'retained source change head'); END;",
        "CREATE TRIGGER source_change_immutable BEFORE UPDATE ON source_change "
        "BEGIN SELECT RAISE(ABORT,'immutable source change'); END;",
        "CREATE TRIGGER source_change_retained BEFORE DELETE ON source_change "
        "BEGIN SELECT RAISE(ABORT,'retained source change'); END;",
    ]
    for source, table, reference in (
        ("fact", "fact_current", "fact_id"),
        ("calculation", "calculation_current", "calculation_id"),
    ):
        sql.extend(
            (
                f"CREATE TRIGGER change_{source}_key BEFORE UPDATE OF subject_id ON {table} "
                "WHEN NEW.subject_id IS NOT OLD.subject_id "
                "BEGIN SELECT RAISE(ABORT,'stable source identity'); END;",
                f"CREATE TRIGGER change_{source}_insert AFTER INSERT ON {table} BEGIN "
                + _append(source, "NEW.subject_id", "NULL", f"NEW.{reference}")
                + " END;",
                f"CREATE TRIGGER change_{source}_update AFTER UPDATE ON {table} "
                f"WHEN NEW.{reference} IS NOT OLD.{reference} BEGIN "
                + _append(source, "NEW.subject_id", f"OLD.{reference}", f"NEW.{reference}")
                + " END;",
                f"CREATE TRIGGER change_{source}_delete AFTER DELETE ON {table} BEGIN "
                + _append(source, "OLD.subject_id", f"OLD.{reference}", "NULL")
                + " END;",
            )
        )
    sql.extend(
        (
            "CREATE TRIGGER change_pending_update BEFORE UPDATE ON pending BEGIN "
            "SELECT RAISE(ABORT,'replace pending causes by insert or delete'); END;",
            "CREATE TRIGGER change_pending_insert AFTER INSERT ON pending BEGIN "
            + _append("pending", "NEW.subject_id", "NULL", "NEW.cause_id")
            + " END;",
            "CREATE TRIGGER change_pending_delete AFTER DELETE ON pending BEGIN "
            + _append("pending", "OLD.subject_id", "OLD.cause_id", "NULL")
            + " END;",
        )
    )
    for source, table, target, reference in (
        ("identity", "identity_correction_item", "NEW.subject_id", "NEW.id"),
        ("duplicate", "business_duplicate_check", "NEW.proposed_subject_id", "NEW.id"),
        ("inventory", "material_revision", "NEW.period||':'||NEW.category", "CAST(NEW.id AS TEXT)"),
        (
            "inventory_item",
            "material_item",
            "CAST(NEW.inventory_id AS TEXT)",
            "lower(hex(NEW.evidence_digest))",
        ),
        ("publication", "calculation_publication", "NEW.subject_id", "NEW.id"),
    ):
        sql.append(
            f"CREATE TRIGGER change_{source}_insert AFTER INSERT ON {table} BEGIN "
            + _append(source, target, "NULL", reference)
            + " END;"
        )
    return "\n".join(sql) + "\n"


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


def changes_since(
    connection, after: int, *, through: int | None = None
) -> tuple[SourceChange, ...]:
    """Read a continuous interval in the caller's snapshot; never infer a gap."""
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
    """Reconstruct exact historic heads for independent freeze verification."""
    result = {"fact": {}, "calculation": {}}
    for event in changes_since(connection, 0, through=through):
        if event.source not in result:
            continue
        heads = result[event.source]
        if heads.get(event.target_id) != event.before_ref:
            _invalid("journal_head_chain_mismatch")
        if event.after_ref is None:
            heads.pop(event.target_id, None)
        else:
            heads[event.target_id] = event.after_ref
    return result


def first_created_subjects(connection, events, *, _fact_rows=None) -> frozenset[str]:
    """Prove which subjects first appeared in this retained journal interval.

    A later restoration also has before_ref=NULL, so only revision one whose
    earliest fact-head insertion is this event qualifies. This is useful for
    ruling out references from a complete earlier freeze, never for deciding
    whether a new fact duplicates an earlier business.
    """
    from .types import canonical

    insertions = {}
    for event in events:
        if event.source == "fact" and event.before_ref is None and event.after_ref is not None:
            insertions.setdefault(event.after_ref, (event.target_id, event.sequence))
    if not insertions:
        return frozenset()
    # The material watch already reads these exact revision headers to verify
    # changed owners and kinds. Its same-call rows also carry the earliest
    # insertion, avoiding a second query for all newly registered facts.
    rows = (
        connection.execute(
            "SELECT f.id,f.subject_id,f.revision,(SELECT sequence FROM source_change "
            "WHERE source='fact' AND after_ref=f.id ORDER BY sequence LIMIT 1) "
            "FROM json_each(?) ids CROSS JOIN fact_revision f ON f.id=ids.value",
            (canonical(sorted(insertions)),),
        )
        if _fact_rows is None
        else (row for row in _fact_rows if row[0] in insertions)
    )
    result, found = set(), set()
    for ident, subject_id, revision, first_sequence in rows:
        found.add(ident)
        if subject_id != insertions[ident][0]:
            _invalid("inserted_fact_owner_mismatch")
        if revision == 1 and first_sequence == insertions[ident][1]:
            result.add(subject_id)
    if found != set(insertions):
        _invalid("inserted_fact_missing")
    return frozenset(result)


def verify_journal(connection) -> int:
    """Independently compare the complete event history with authoritative tables."""
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
        # Calculations can be preserved batch members before they become a
        # selected head. Every recorded adoption must still identify its source.
        if (source == "calculation" and not recorded[source] <= actual) or (
            source != "calculation" and recorded[source] != actual
        ):
            _invalid("journal_source_coverage_mismatch")
    return len(events)
