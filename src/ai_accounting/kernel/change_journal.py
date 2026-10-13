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
    "evidence",
    "company_note",
    "display_profile",
    "close",
    "entity_profile",
    "commentary",
)
VERSIONED_SOURCES = frozenset({"company_note", "display_profile", "entity_profile", "commentary"})
_ADDED_SOURCES = SOURCES[8:]
_RETAINED_QUERIES = {
    "evidence": "SELECT lower(hex(digest)),lower(hex(digest)),NULL FROM evidence",
    "company_note": "SELECT 'company',id,revision FROM company_note_revision",
    "display_profile": "SELECT kind||':'||entity_id,id,revision FROM display_profile_revision",
    "entity_profile": "SELECT entity_id,id,revision FROM entity_profile_revision",
    "commentary": "SELECT CAST(period AS TEXT),id,revision FROM period_commentary_revision",
    "close": "SELECT CAST(period AS TEXT),lower(hex(digest)),NULL FROM period_close",
}


def retained_coverage_ddl():
    """An explicit old-format boundary, never fabricated historical events."""
    return """
CREATE TABLE source_change_retained_record(source TEXT NOT NULL
 CHECK(source IN('evidence','company_note','display_profile','entity_profile',
 'commentary','close')),
 target_id TEXT NOT NULL,after_ref TEXT NOT NULL,revision INTEGER,
 CHECK((source IN('company_note','display_profile','entity_profile','commentary')
 AND revision IS NOT NULL AND revision>0)
 OR (source IN('evidence','close') AND revision IS NULL)),
 PRIMARY KEY(source,target_id,after_ref)) STRICT;
CREATE TABLE source_change_retained_boundary(id INTEGER PRIMARY KEY CHECK(id=1),
 source_fingerprint BLOB NOT NULL CHECK(length(source_fingerprint)=32),
 through_sequence INTEGER NOT NULL CHECK(through_sequence>=0),
 retained_history_digest BLOB NOT NULL CHECK(length(retained_history_digest)=32),
 row_count INTEGER NOT NULL CHECK(row_count>=0),
 digest BLOB NOT NULL CHECK(length(digest)=32)) STRICT;
CREATE TRIGGER source_change_retained_record_sealed BEFORE INSERT
 ON source_change_retained_record WHEN EXISTS(SELECT 1 FROM source_change_retained_boundary)
 BEGIN SELECT RAISE(ABORT,'retained journal coverage sealed'); END;
CREATE TRIGGER source_change_retained_boundary_count BEFORE INSERT
 ON source_change_retained_boundary WHEN NEW.row_count<>(SELECT count(*)
 FROM source_change_retained_record)
 BEGIN SELECT RAISE(ABORT,'retained journal coverage count mismatch'); END;
CREATE TRIGGER source_change_retained_record_update BEFORE UPDATE
 ON source_change_retained_record BEGIN SELECT RAISE(ABORT,'immutable journal coverage'); END;
CREATE TRIGGER source_change_retained_record_delete BEFORE DELETE
 ON source_change_retained_record BEGIN SELECT RAISE(ABORT,'retained journal coverage'); END;
CREATE TRIGGER source_change_retained_boundary_update BEFORE UPDATE
 ON source_change_retained_boundary BEGIN SELECT RAISE(ABORT,'immutable journal boundary'); END;
CREATE TRIGGER source_change_retained_boundary_delete BEFORE DELETE
 ON source_change_retained_boundary BEGIN SELECT RAISE(ABORT,'retained journal boundary'); END;
"""


def capture_retained_coverage(connection, *, source_fingerprint, retained_history_digest):
    """Capture only the declared eight-source format, under the migration write lock."""
    from .development_contracts import SOURCE24_FINGERPRINT
    from .types import digest

    if not connection.in_transaction or source_fingerprint != SOURCE24_FINGERPRINT:
        raise KernelError("migration_not_declared", "旧变化记录必须来自明确的锁内结构")
    rows = sorted(
        (source, target, reference, revision)
        for source, query in _RETAINED_QUERIES.items()
        for target, reference, revision in connection.execute(query)
    )
    value = {"source_fingerprint": source_fingerprint, "through_sequence": head(connection),
             "retained_history_digest": retained_history_digest, "rows": rows}
    return {**value, "digest": digest(value).hex()}


def install_retained_coverage(connection, value):
    """Seal the exact capture before the real migration receipt and content verification."""
    from .development_contracts import SOURCE24_FINGERPRINT
    from .types import digest

    expected = {"source_fingerprint", "through_sequence", "retained_history_digest", "rows",
                "digest"}
    if (not connection.in_transaction or set(value) != expected
            or value["source_fingerprint"] != SOURCE24_FINGERPRINT
            or digest({key: item for key, item in value.items() if key != "digest"}).hex()
            != value["digest"]
            or head(connection) != value["through_sequence"]
            or connection.execute("SELECT 1 FROM source_change_retained_boundary").fetchone()
            or connection.execute("SELECT 1 FROM source_change_retained_record").fetchone()):
        raise KernelError("migration_integrity_failed", "旧变化记录承接与原快照不一致")
    connection.executemany(
        "INSERT INTO source_change_retained_record VALUES(?,?,?,?)", value["rows"],
    )
    connection.execute(
        "INSERT INTO source_change_retained_boundary VALUES(1,?,?,?,?,?)",
        (bytes.fromhex(value["source_fingerprint"]), value["through_sequence"],
         bytes.fromhex(value["retained_history_digest"]), len(value["rows"]),
         bytes.fromhex(value["digest"])),
    )


def _retained_coverage(connection):
    """A migration-bound original set; later records still require actual events."""
    from .development_contracts import SOURCE24_FINGERPRINT
    from .types import digest

    boundaries = connection.execute("SELECT * FROM source_change_retained_boundary").fetchall()
    rows = [tuple(row) for row in connection.execute(
        "SELECT source,target_id,after_ref,revision FROM source_change_retained_record "
        "ORDER BY source,target_id,after_ref",
    )]
    if not boundaries:
        if rows:
            _invalid("journal_retained_boundary_missing")
        return 0, ()
    if len(boundaries) != 1:
        _invalid("journal_retained_boundary_invalid")
    boundary = boundaries[0]
    source, through, retained, count, recorded_digest = tuple(boundary)[1:]
    value = {"source_fingerprint": bytes(source).hex(), "through_sequence": through,
             "retained_history_digest": bytes(retained).hex(), "rows": rows}
    if (value["source_fingerprint"] != SOURCE24_FINGERPRINT
            or type(through) is not int or not 0 <= through <= head(connection)
            or count != len(rows) or digest(value) != bytes(recorded_digest)
            or connection.execute(
                "SELECT count(*) FROM schema_draft_history WHERE source_fingerprint=? "
                "AND retained_history_digest=?", (source, retained),
            ).fetchone()[0] != 1):
        _invalid("journal_retained_boundary_mismatch")
    return through, rows


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
    # Keep metadata and freeze events under source-specific names. Their
    # identities and invalidation semantics differ from identity corrections.
    sql.extend(
        (
            "CREATE TRIGGER change_evidence_insert AFTER INSERT ON evidence BEGIN "
            + _append("evidence", "lower(hex(NEW.digest))", "NULL", "lower(hex(NEW.digest))")
            + " END;",
            "CREATE TRIGGER change_company_note_insert AFTER INSERT ON company_note_revision BEGIN "
            + _append(
                "company_note",
                "'company'",
                "(SELECT id FROM company_note_revision WHERE revision=NEW.revision-1)",
                "NEW.id",
            )
            + " END;",
            "CREATE TRIGGER change_display_profile_insert AFTER INSERT "
            "ON display_profile_revision BEGIN "
            + _append(
                "display_profile",
                "NEW.kind||':'||NEW.entity_id",
                "(SELECT id FROM display_profile_revision WHERE kind=NEW.kind "
                "AND entity_id=NEW.entity_id AND revision=NEW.revision-1)",
                "NEW.id",
            )
            + " END;",
            "CREATE TRIGGER change_entity_profile_insert AFTER INSERT "
            "ON entity_profile_revision BEGIN "
            + _append(
                "entity_profile",
                "NEW.entity_id",
                "(SELECT id FROM entity_profile_revision WHERE entity_id=NEW.entity_id "
                "AND revision=NEW.revision-1)",
                "NEW.id",
            )
            + " END;",
            "CREATE TRIGGER change_commentary_insert AFTER INSERT "
            "ON period_commentary_revision BEGIN "
            + _append(
                "commentary",
                "CAST(NEW.period AS TEXT)",
                "(SELECT id FROM period_commentary_revision WHERE period=NEW.period "
                "AND revision=NEW.revision-1)",
                "NEW.id",
            )
            + " END;",
            "CREATE TRIGGER change_close_insert AFTER INSERT ON period_close BEGIN "
            + _append("close", "CAST(NEW.period AS TEXT)", "NULL", "lower(hex(NEW.digest))")
            + " END;",
        )
    )
    return "\n".join(sql) + "\n" + retained_coverage_ddl()


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
                source not in {"fact", "calculation", "pending"} | VERSIONED_SOURCES
                and (before is not None or next_ref is None)
            )
            or (source in VERSIONED_SOURCES and next_ref is None)
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


def verify_journal(connection, *, _source_fingerprint=None) -> int:
    """Independently compare the complete event history with authoritative tables."""
    events = changes_since(connection, 0)
    heads = {"fact": {}, "calculation": {}}
    pending = set()
    recorded = {source: set() for source in SOURCES}
    versioned_heads = {source: {} for source in VERSIONED_SOURCES}
    versioned_before = {source: {} for source in VERSIONED_SOURCES}
    from .development_contracts import (
        REMUNERATION_SOURCE_FINGERPRINT,
        REMUNERATION_TARGET_FINGERPRINT,
    )

    # These two exact package sources already record all fourteen event types,
    # but predate the retained-boundary tables. Current structure never skips it.
    through, retained_rows = (
        (0, ()) if _source_fingerprint in {
            REMUNERATION_SOURCE_FINGERPRINT, REMUNERATION_TARGET_FINGERPRINT,
        } else _retained_coverage(connection)
    )
    grouped = {}
    for source, target, reference, revision in retained_rows:
        if source not in _ADDED_SOURCES or not target or not reference:
            _invalid("journal_retained_record_invalid")
        recorded[source].add((target, reference))
        if source in VERSIONED_SOURCES:
            if type(revision) is not int or revision < 1:
                _invalid("journal_retained_record_invalid")
            grouped.setdefault((source, target), []).append((revision, reference))
        elif revision is not None:
            _invalid("journal_retained_record_invalid")
    for (source, target), versions in grouped.items():
        prior = None
        for expected, (revision, reference) in enumerate(sorted(versions), 1):
            if revision != expected:
                _invalid("journal_retained_revision_gap")
            versioned_before[source][target, reference] = prior
            prior = reference
        versioned_heads[source][target] = prior
    for event in events:
        if event.sequence <= through and event.source in _ADDED_SOURCES:
            _invalid("journal_retained_prefix_invalid")
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
            if event.source in VERSIONED_SOURCES:
                selected = versioned_heads[event.source]
                if selected.get(event.target_id) != event.before_ref:
                    _invalid("journal_revision_chain_mismatch")
                selected[event.target_id] = event.after_ref
                versioned_before[event.source][key] = event.before_ref
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
        ("evidence", "SELECT lower(hex(digest)),lower(hex(digest)) FROM evidence"),
        ("company_note", "SELECT 'company',id,revision FROM company_note_revision "
         "ORDER BY revision"),
        (
            "display_profile",
            "SELECT kind||':'||entity_id,id,revision FROM display_profile_revision "
            "ORDER BY kind,entity_id,revision",
        ),
        ("entity_profile", "SELECT entity_id,id,revision FROM entity_profile_revision "
         "ORDER BY entity_id,revision"),
        ("commentary", "SELECT CAST(period AS TEXT),id,revision FROM period_commentary_revision "
         "ORDER BY period,revision"),
        (
            "close",
            "SELECT CAST(period AS TEXT),lower(hex(digest)) FROM period_close",
        ),
    )
    for source, query in sources:
        if source in VERSIONED_SOURCES:
            actual, previous = set(), {}
            for target, ident, revision in connection.execute(query):
                prior_revision, prior_id = previous.get(target, (0, None))
                key = target, ident
                if (
                    revision != prior_revision + 1
                    or key not in versioned_before[source]
                    or versioned_before[source][key] != prior_id
                ):
                    _invalid("journal_revision_source_mismatch")
                actual.add(key)
                previous[target] = revision, ident
        else:
            actual = {tuple(row) for row in connection.execute(query)}
        # Calculations can be preserved batch members before they become a
        # selected head. Every recorded adoption must still identify its source.
        if (source == "calculation" and not recorded[source] <= actual) or (
            source != "calculation" and recorded[source] != actual
        ):
            _invalid("journal_source_coverage_mismatch")
    return len(events)
