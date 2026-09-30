"""Finite, rebuildable indexes of immutable source references.

Indexes locate candidates; the source contracts still determine adoption and
business meaning. Local checks cover selected sources only. A deliberately
omitted reverse-index row can only reliably be found by full verification.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import ChainMap
from dataclasses import dataclass

from .content_history_context import month_type
from .content_history_context import source_canonical as canonical
from .contracts import KernelError

JOB_KINDS = ("payment_export", "tax_import", "report_export")
AUDIT_ACTIONS = (
    "confirm_fact",
    "confirm_facts",
    "recording_correction",
    "save_display_profile",
    "payee",
    "register_entity",
    "update_entity_profile",
)
CLOSE_CALCULATIONS = "adopted_results[*].calculation_id"
CLOSE_ADOPTION_FACTS = "adopted_results[*].fact_id"
CLOSE_VOUCHER_BASES = "vouchers[*].adopted_calculation_id"
CLOSE_MATERIAL_FACTS = "material_coverage.fact_ids[*]"
CLOSE_ASSET_ADOPTIONS = "asset_batch_adoptions[*].owner_calculation_id"
CLOSE_ASSET_CARD_ADOPTIONS = "asset_card_adoptions[*].calculation_id"
CLOSE_ASSET_CARD_ACCEPTANCES = "asset_card_adoptions[*].acceptance_calculation_id"
CLOSE_VOUCHERS = "vouchers[*].id"
CLOSE_VOUCHER_CALCULATIONS = "vouchers[*].calculation_id"
CLOSE_REPORT_FACTS = "readiness.financial_reports.facts[*]"
CLOSE_TYPED_FACTS = "management_snapshot.typed_facts[*].id"
CLOSE_PROFILES = "management_snapshot.profiles[*].id"
CLOSE_ENTITY_PROFILES = "management_snapshot.entity_profiles[*].id"
CLOSE_MANAGEMENT = "management_snapshot.management[*].id"
CLOSE_PAYEES = "management_snapshot.payees[*].id"
_CLOSE_PATH_TYPES = {
    CLOSE_CALCULATIONS: "calculation",
    CLOSE_ADOPTION_FACTS: "fact",
    CLOSE_VOUCHER_BASES: "calculation",
    CLOSE_MATERIAL_FACTS: "fact",
    CLOSE_ASSET_ADOPTIONS: "calculation",
    CLOSE_ASSET_CARD_ADOPTIONS: "calculation",
    CLOSE_ASSET_CARD_ACCEPTANCES: "calculation",
    CLOSE_VOUCHERS: "voucher",
    CLOSE_VOUCHER_CALCULATIONS: "calculation",
    CLOSE_REPORT_FACTS: "fact",
    CLOSE_TYPED_FACTS: "fact",
    CLOSE_PROFILES: "display_profile",
    CLOSE_ENTITY_PROFILES: "entity_profile",
    CLOSE_MANAGEMENT: "management",
    CLOSE_PAYEES: "payee",
}

READ_INDEX_DDL = """
CREATE INDEX voucher_line_account ON voucher_line(account,version_id,line_no);
CREATE INDEX voucher_version_reverses ON voucher_version(reverses_id,id);
CREATE INDEX dependency_scope_any_kind ON dependency_scope(source,scope_key,kind,calculation_id);
CREATE INDEX calculation_obligations ON calculation(
 json_array_length(outcome,'$.values.obligations'),subject_id,id);
CREATE TABLE read_index_source(source_kind TEXT NOT NULL
 CHECK(source_kind IN ('close','job','audit')),source_id TEXT NOT NULL,
 source_digest BLOB NOT NULL CHECK(length(source_digest)=32),
 PRIMARY KEY(source_kind,source_id)) STRICT;
CREATE TRIGGER read_index_source_owner BEFORE INSERT ON read_index_source
 WHEN (NEW.source_kind='close' AND NOT EXISTS(
 SELECT 1 FROM period_close WHERE period=CAST(NEW.source_id AS INTEGER)
 AND CAST(period AS TEXT)=NEW.source_id))
 OR (NEW.source_kind='job' AND NOT EXISTS(SELECT 1 FROM jobs WHERE id=NEW.source_id
 AND kind IN ('payment_export','tax_import','report_export')))
 OR (NEW.source_kind='audit' AND NOT EXISTS(SELECT 1 FROM audit
 WHERE id=CAST(NEW.source_id AS INTEGER) AND CAST(id AS TEXT)=NEW.source_id AND action IN
 ('confirm_fact','confirm_facts','recording_correction','save_display_profile','payee',
 'register_entity','update_entity_profile')))
 BEGIN SELECT RAISE(ABORT,'read index source ownership mismatch'); END;
CREATE TABLE close_reference(close_period INTEGER NOT NULL REFERENCES period_close(period),
 path TEXT NOT NULL,position TEXT NOT NULL,reference_type TEXT NOT NULL,
 reference_id TEXT NOT NULL,related_id TEXT,
 source_kind TEXT GENERATED ALWAYS AS ('close') STORED,
 source_id TEXT GENERATED ALWAYS AS (CAST(close_period AS TEXT)) STORED,
 PRIMARY KEY(close_period,path,position,reference_type),
 FOREIGN KEY(source_kind,source_id) REFERENCES read_index_source
 DEFERRABLE INITIALLY DEFERRED) STRICT;
CREATE INDEX close_reference_lookup ON close_reference(reference_type,reference_id,close_period);
CREATE INDEX close_reference_related ON close_reference(related_id,close_period)
 WHERE related_id IS NOT NULL;
CREATE TABLE job_reference(job_id TEXT NOT NULL REFERENCES jobs(id),path TEXT NOT NULL,
 position TEXT NOT NULL,reference_type TEXT NOT NULL,reference_id TEXT NOT NULL,
 related_id TEXT,start_period INTEGER,end_period INTEGER,
 source_kind TEXT GENERATED ALWAYS AS ('job') STORED,
 source_id TEXT GENERATED ALWAYS AS (job_id) STORED,
 PRIMARY KEY(job_id,path,position,reference_type),
 FOREIGN KEY(source_kind,source_id) REFERENCES read_index_source
 DEFERRABLE INITIALLY DEFERRED) STRICT;
CREATE INDEX job_reference_lookup ON job_reference(reference_type,reference_id,job_id);
CREATE INDEX job_reference_period ON job_reference(start_period,end_period,job_id)
 WHERE start_period IS NOT NULL;
CREATE TABLE audit_reference(audit_id INTEGER NOT NULL REFERENCES audit(id),
 position TEXT NOT NULL,source_type TEXT NOT NULL,source_id TEXT NOT NULL,
 index_source_kind TEXT GENERATED ALWAYS AS ('audit') STORED,
 index_source_id TEXT GENERATED ALWAYS AS (CAST(audit_id AS TEXT)) STORED,
 PRIMARY KEY(audit_id,position),
 FOREIGN KEY(index_source_kind,index_source_id) REFERENCES read_index_source
 DEFERRABLE INITIALLY DEFERRED) STRICT;
CREATE INDEX audit_reference_lookup ON audit_reference(source_type,source_id,audit_id);
"""

for _table, _kind, _column in (
    ("close_reference", "close", "close_period"),
    ("job_reference", "job", "job_id"),
    ("audit_reference", "audit", "audit_id"),
):
    READ_INDEX_DDL += f"""
CREATE TRIGGER sealed_{_table}_insert BEFORE INSERT ON {_table}
 WHEN EXISTS(SELECT 1 FROM read_index_source WHERE source_kind='{_kind}'
 AND source_id=CAST(NEW.{_column} AS TEXT))
 BEGIN SELECT RAISE(ABORT,'sealed read index source'); END;
"""
for _table in ("read_index_source", "close_reference", "job_reference", "audit_reference"):
    for _event in ("UPDATE", "DELETE"):
        READ_INDEX_DDL += f"""
CREATE TRIGGER immutable_{_table}_{_event} BEFORE {_event} ON {_table}
 BEGIN SELECT RAISE(ABORT,'immutable read index'); END;
"""


def _object(value):
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _items(value):
    return enumerate(value) if isinstance(value, list) else ()


def _text(value):
    return isinstance(value, str) and bool(value)


def _close_references(row, *, manifest=None):
    from .content_history_context import close_contract

    if manifest is None:
        manifest = close_contract().require_close_contract(_object(row["manifest"]))
    result = []

    def add(path, position, typ, ident, related=None):
        if _text(ident) or (typ == "management" and type(ident) is int):
            result.append(
                (path, str(position), typ, str(ident), related if _text(related) else None)
            )

    for pos, reference in _items(manifest["adopted_results"]):
        add(CLOSE_CALCULATIONS, pos, "calculation", reference["calculation_id"])
        add(CLOSE_ADOPTION_FACTS, pos, "fact", reference["fact_id"])
    for pos, ident in _items(manifest["material_coverage"].get("fact_ids")):
        add(CLOSE_MATERIAL_FACTS, pos, "fact", ident)
    for pos, reference in _items(manifest.get("asset_batch_adoptions")):
        if isinstance(reference, dict):
            add(CLOSE_ASSET_ADOPTIONS, pos, "calculation", reference.get("owner_calculation_id"))
    for pos, reference in _items(manifest.get("asset_card_adoptions")):
        if isinstance(reference, dict):
            add(CLOSE_ASSET_CARD_ADOPTIONS, pos, "calculation", reference.get("calculation_id"))
            add(
                CLOSE_ASSET_CARD_ACCEPTANCES,
                pos,
                "calculation",
                reference.get("acceptance_calculation_id"),
            )
    for pos, reference in _items(manifest.get("vouchers")):
        if isinstance(reference, dict):
            add(
                "vouchers[*].id",
                pos,
                "voucher",
                reference.get("id"),
                reference.get("calculation_id"),
            )
            # A malformed voucher ID does not erase an independently declared root.
            add(CLOSE_VOUCHER_CALCULATIONS, pos, "calculation", reference.get("calculation_id"))
            add(CLOSE_VOUCHER_BASES, pos, "calculation", reference.get("adopted_calculation_id"))
    for name, references in manifest["readiness"].items():
        if isinstance(references, dict):
            for field, typ in (("facts", "fact"), ("calculations", "calculation")):
                for pos, ident in _items(references.get(field)):
                    add(f"readiness.{name}.{field}[*]", pos, typ, ident)
    snapshot = manifest.get("management_snapshot")
    if isinstance(snapshot, dict):
        for field, typ in (
            ("typed_facts", "fact"),
            ("profiles", "display_profile"),
            ("entity_profiles", "entity_profile"),
            ("management", "management"),
            ("payees", "payee"),
        ):
            for pos, reference in _items(snapshot.get(field)):
                if isinstance(reference, dict):
                    add(f"management_snapshot.{field}[*].id", pos, typ, reference.get("id"))
    return sorted(result)


def _job_references(row):
    plan = _object(row["payload"]).get("plan")
    if not isinstance(plan, dict):
        return []
    result = []

    def add(path, position, typ, ident, related=None, start=None, end=None):
        if _text(ident):
            result.append(
                (path, str(position), typ, ident, related if _text(related) else None, start, end)
            )

    def month(path, position, value):
        if not isinstance(value, str):
            return
        try:
            period = month_type()(value)
        except ValueError:
            return
        add(path, position, "period", value, start=period.ordinal, end=period.ordinal)

    kind = row["kind"]
    if kind == "payment_export":
        for pos, item in _items(plan.get("rows")):
            if not isinstance(item, dict):
                continue
            for subpos, source in _items(item.get("sources")):
                if not isinstance(source, dict):
                    continue
                for field, typ in (
                    ("subject_id", "subject"),
                    ("calculation_id", "calculation"),
                    ("obligation", "obligation"),
                ):
                    add(
                        f"plan.rows[*].sources[*].{field}",
                        f"{pos}.{subpos}",
                        typ,
                        source.get(field),
                        source.get("calculation_id"),
                    )
        month("plan.period", "0", plan.get("period"))
    elif kind == "tax_import":
        for pos, ident in _items(plan.get("source_versions")):
            add("plan.source_versions[*]", pos, "version", ident)
        month("plan.period", "0", plan.get("period"))
    elif kind == "report_export":
        for pos, ident in _items(plan.get("report_fact_ids")):
            add("plan.report_fact_ids[*]", pos, "fact", ident)
        for pos, item in _items(plan.get("source_closes")):
            if isinstance(item, dict):
                month("plan.source_closes[*].period", pos, item.get("period"))
        period = plan.get("period")
        if isinstance(period, dict):
            start, end = period.get("quarter_start"), period.get("quarter_end")
            if isinstance(start, str) and isinstance(end, str):
                try:
                    first, last = month_type()(start[:7]), month_type()(end[:7])
                except ValueError:
                    pass
                else:
                    add(
                        "plan.period.quarter_start/quarter_end",
                        "0",
                        "period_range",
                        f"{start}/{end}",
                        start=first.ordinal,
                        end=last.ordinal,
                    )
    return sorted(result)


def _audit_references(row):
    from .provenance import _references, _result

    return [
        (str(pos), *reference)
        for pos, reference in enumerate(
            _references(row["action"], _result(_object(row["payload"])))
        )
        if reference is not None
    ]


_SOURCES = {
    "close": (
        "period_close",
        "period",
        "close_reference",
        "close_period",
        "path,position,reference_type,reference_id,related_id",
        _close_references,
    ),
    "job": (
        "jobs",
        "id",
        "job_reference",
        "job_id",
        "path,position,reference_type,reference_id,related_id,start_period,end_period",
        _job_references,
    ),
    "audit": (
        "audit",
        "id",
        "audit_reference",
        "audit_id",
        "position,source_type,source_id",
        _audit_references,
    ),
}


def _source(connection, kind, ident):
    table, key, *_ = _SOURCES[kind]
    row = connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (ident,)).fetchone()
    if row is None:
        _invalid(kind, ident, "source_missing")
    return row


def _supported(kind, row):
    return (
        kind == "close"
        or kind == "job"
        and row["kind"] in JOB_KINDS
        or kind == "audit"
        and row["action"] in AUDIT_ACTIONS
    )


def _source_digest(kind, row):
    if kind == "close":
        return bytes(row["digest"])
    values = (
        [row["id"], row["kind"], row["payload"]]
        if kind == "job"
        else [row["id"], row["action"], row["payload"], row["created_at"]]
    )
    return hashlib.sha256(canonical(values).encode("utf-8")).digest()


def _invalid(kind, ident, reason):
    raise KernelError(
        "read_index_integrity_failed",
        "精确引用目录与不可变来源不一致，需要显式完整性检查",
        source_kind=kind,
        source_id=str(ident),
        reason=reason,
    )


def _verified_close_manifest(connection, row, marker, identity):
    from .content_history_context import close_reader

    decode_close = close_reader().decode_close

    period = row["period"]
    if marker is None or bytes(marker) != bytes(row["digest"]):
        _invalid("close", period, "source_digest_or_marker_mismatch")
    manifest = decode_close(connection, row)
    if (
        manifest["period"] != str(month_type().from_ordinal(period))
        or manifest["company_id"] != identity["company_id"]
        or manifest["database_id"] != identity["database_id"]
    ):
        _invalid("close", period, "manifest_identity_mismatch")
    return manifest


@dataclass(frozen=True)
class _VerifiedCloses:
    connection: object
    lease: object
    closes: tuple


def _verified_closes_for_indexes(connection, closes):
    """Carry the integrity loop's decoded closes only within its transaction."""
    from .verified_close_archive import VerifiedCloseArchive
    from .verified_source_lease import current_verified_lease

    return _VerifiedCloses(
        connection,
        current_verified_lease(connection),
        closes if isinstance(closes, VerifiedCloseArchive) else tuple(closes),
    )


def _require_verified_closes_for_indexes(connection, token):
    from .verified_source_lease import require_verified_lease

    if type(token) is not _VerifiedCloses or token.connection is not connection:
        raise ValueError("verified closes belong to another connection")
    require_verified_lease(connection, token.lease)
    from .verified_close_archive import VerifiedCloseArchive

    if isinstance(token.closes, VerifiedCloseArchive):
        rows = tuple(connection.execute("SELECT * FROM period_close ORDER BY period"))
        return token.closes.lookup(connection, rows)
    saved = tuple(
        (row["period"], bytes(row["digest"]))
        for row in connection.execute("SELECT period,digest FROM period_close ORDER BY period")
    )
    verified = tuple((row["period"], bytes(row["digest"])) for row, _ in token.closes)
    if saved != verified:
        _invalid("close", "*", "predecoded_close_set_mismatch")
    return {str(row["period"]): (row, manifest) for row, manifest in token.closes}


def verify_source(connection, source_kind, source_id, *, source=None):
    """Check one selected source, including its complete occurrence multiset."""
    row = source if source is not None else _source(connection, source_kind, source_id)
    verify_sources(connection, source_kind, [row])
    return row


def verify_sources(connection, source_kind, sources):
    """Batch local checks: query count does not grow per matching source."""
    _, source_key, table, key, columns, extract = _SOURCES[source_kind]
    sources = {str(row[source_key]): row for row in sources}
    if not sources:
        return {}
    if source_kind == "close":
        return _verify_close_sources(connection, sources)
    identities = json.dumps(list(sources))
    markers = {
        row[0]: bytes(row[1])
        for row in connection.execute(
            "SELECT source_id,source_digest FROM read_index_source WHERE source_kind=? "
            "AND source_id IN (SELECT value FROM json_each(?))",
            (source_kind, identities),
        )
    }
    entries = {}
    for row in connection.execute(
        f"SELECT {key},{columns} FROM {table} WHERE {key} IN (SELECT value FROM json_each(?))",
        (identities,),
    ):
        entries.setdefault(str(row[0]), []).append(tuple(row)[1:])
    for source_id, row in sources.items():
        if not _supported(source_kind, row) or source_id not in markers:
            _invalid(source_kind, source_id, "marker_missing_or_unsupported")
        if markers[source_id] != _source_digest(source_kind, row):
            _invalid(source_kind, source_id, "source_digest_mismatch")
        if sorted(entries.get(source_id, ())) != sorted(extract(row)):
            _invalid(source_kind, source_id, "reference_multiset_mismatch")
    return {}


def _verify_close_sources(connection, sources, *, verified_manifests=None):
    """Compare complete sealed close memberships in primary-key order, one row at a time."""
    identities = json.dumps(list(sources))
    identity = connection.execute(
        "SELECT company_id,database_id FROM identity WHERE id=1"
    ).fetchone()
    if identity is None:
        _invalid("close", "*", "company_identity_missing")
    markers = {
        row[0]: bytes(row[1])
        for row in connection.execute(
            "SELECT source_id,source_digest FROM read_index_source WHERE source_kind='close' "
            "AND source_id IN (SELECT value FROM json_each(?))",
            (identities,),
        )
    }
    entries = iter(
        connection.execute(
            "SELECT close_period,path,position,reference_type,reference_id,related_id "
            "FROM close_reference WHERE close_period IN (SELECT value FROM json_each(?)) "
            "ORDER BY close_period,path,position,reference_type",
            (identities,),
        )
    )
    actual = next(entries, None)
    parsed = {}
    for source_id, row in sorted(sources.items(), key=lambda item: item[1]["period"]):
        if verified_manifests is None:
            manifest = _verified_close_manifest(connection, row, markers.get(source_id), identity)
        else:
            adopted = verified_manifests.get(source_id)
            if (
                adopted is None
                or adopted[0]["period"] != row["period"]
                or bytes(adopted[0]["digest"]) != bytes(row["digest"])
                or markers.get(source_id) != bytes(row["digest"])
            ):
                _invalid("close", source_id, "source_digest_or_marker_mismatch")
            manifest = adopted[1]
            if (
                manifest["period"] != str(month_type().from_ordinal(row["period"]))
                or manifest["company_id"] != identity["company_id"]
                or manifest["database_id"] != identity["database_id"]
            ):
                _invalid("close", source_id, "manifest_identity_mismatch")
        for expected in _close_references(row, manifest=manifest):
            if (
                actual is None
                or actual[0] != row["period"]
                or (
                    actual[1] != expected[0]
                    or actual[2] != expected[1]
                    or actual[3] != expected[2]
                    or actual[4] != expected[3]
                    or actual[5] != expected[4]
                )
            ):
                _invalid("close", source_id, "reference_multiset_mismatch")
            actual = next(entries, None)
        if actual is not None and actual[0] == row["period"]:
            _invalid("close", source_id, "reference_multiset_mismatch")
        parsed[row["period"]] = manifest
    if actual is not None:
        _invalid("close", actual[0], "reference_multiset_mismatch")
    return parsed


def authoritative_close_rows(connection, *, periods, through_period=None):
    """Read exact published periods without using the reverse reference directory.

    This verifies immutable close content and its seal, but does not mark the
    close_reference multiset as checked. Callers must have independently obtained
    these exact periods from authoritative calculation publications.
    """
    periods = sorted(set(periods))
    if not periods:
        return [], {}
    if any(type(period) is not int for period in periods):
        raise ValueError("authoritative close periods must be integer ordinals")
    sql = "SELECT p.* FROM period_close p WHERE p.period IN (SELECT value FROM json_each(?))"
    parameters = [json.dumps(periods)]
    if through_period is not None:
        sql += " AND p.period<=?"
        parameters.append(through_period)
    rows = list(connection.execute(sql + " ORDER BY p.period", parameters))
    if not rows:
        return [], {}
    identity = connection.execute(
        "SELECT company_id,database_id FROM identity WHERE id=1"
    ).fetchone()
    if identity is None:
        _invalid("close", "*", "company_identity_missing")
    markers = {
        row[0]: bytes(row[1])
        for row in connection.execute(
            "SELECT source_id,source_digest FROM read_index_source WHERE source_kind='close' "
            "AND source_id IN (SELECT value FROM json_each(?))",
            (json.dumps([str(row["period"]) for row in rows]),),
        )
    }
    from .content_history_context import close_reader

    verified_header = close_reader().verified_header

    headers = {}
    for row in rows:
        period = row["period"]
        if markers.get(str(period)) != bytes(row["digest"]):
            _invalid("close", period, "source_digest_or_marker_mismatch")
        header = verified_header(connection, row)
        if (
            header.root["company_id"] != identity["company_id"]
            or header.root["database_id"] != identity["database_id"]
        ):
            _invalid("close", period, "manifest_identity_mismatch")
        headers[period] = header
    return rows, headers


def selected_voucher_references(connection, voucher_ids, *, through_period):
    """Select all indexed voucher leaves through a close, with IDs driving lookup.

    This only selects directory rows. Callers retain their own missing-hit rules
    and must verify every returned reference against immutable close content.
    Duplicate input IDs retain their original SQL multiset behavior.
    """
    voucher_ids = list(voucher_ids)
    if not voucher_ids:
        return []
    return connection.execute(
        "SELECT r.* FROM json_each(?) ids "
        "CROSS JOIN close_reference r INDEXED BY close_reference_lookup "
        "WHERE r.reference_type='voucher' AND r.reference_id=ids.value "
        "AND r.close_period<=?",
        (canonical(voucher_ids), through_period),
    ).fetchall()


def verify_close_references(
    connection, references, *, _verified_manifests=None, _verified_headers=None,
    _verified_storage_parts=None,
):
    """Check indexed hits at their fixed JSON leaves, parsing each close once.

    The selected immutable source, its digest, marker, and each leaf are checked.
    An omitted source cannot be discovered from its indexed hits.
    """
    requested = {}
    for reference in references:
        item = {
            field: reference[field]
            for field in (
                "close_period",
                "path",
                "position",
                "reference_type",
                "reference_id",
                "related_id",
            )
        }
        path, position = item["path"], str(item["position"])
        reference_type = _CLOSE_PATH_TYPES.get(path)
        readiness_path = re.fullmatch(
            r"readiness\.[a-z][a-z0-9_]*\.(facts|calculations)\[\*\]", path
        )
        if readiness_path:
            reference_type = "fact" if readiness_path[1] == "facts" else "calculation"
        if reference_type is None or not position.isascii() or not position.isdecimal():
            _invalid("close", item["close_period"], "invalid_reference_path")
        if item["reference_type"] != reference_type:
            _invalid("close", item["close_period"], "invalid_reference_type")
        item["index"] = int(position)
        item["segments"] = path.split(".")
        period_key = json.dumps(item["close_period"])
        requested.setdefault(period_key, []).append(item)
    if not requested:
        return

    def leaf(manifest, segments, index):
        value = manifest
        for segment in segments:
            if segment.endswith("[*]"):
                value = value.get(segment[:-3]) if isinstance(value, dict) else None
                value = value[index] if isinstance(value, list) and index < len(value) else None
            else:
                value = value.get(segment) if isinstance(value, dict) else None
            if value is None:
                break
        return value

    def verify_leaves(manifest, expected):
        period = expected[0]["close_period"]
        for item in expected:
            value = leaf(manifest, item["segments"], item["index"])
            valid_leaf = _text(value) or (
                item["reference_type"] == "management" and type(value) is int
            )
            related = (
                leaf(manifest, ["vouchers[*]", "calculation_id"], item["index"])
                if item["path"] == CLOSE_VOUCHERS
                else None
            )
            related = related if _text(related) else None
            if (
                not valid_leaf
                or str(value) != item["reference_id"]
                or related != item.get("related_id")
            ):
                _invalid("close", period, "reference_leaf_mismatch")

    added_parts = {}
    storage_parts = ChainMap(added_parts, _verified_storage_parts or {})

    def verify_storage_leaves(header, expected):
        from .content_history_context import close_reader

        reference_leaves = close_reader().reference_leaves

        period = expected[0]["close_period"]
        values = reference_leaves(
            connection, header,
            [(item["path"], item["index"], item["reference_id"]) for item in expected],
            parts=storage_parts,
        )
        for item, (value, related) in zip(expected, values, strict=True):
            valid_leaf = _text(value) or (
                item["reference_type"] == "management" and type(value) is int
            )
            if (
                not valid_leaf
                or str(value) != item["reference_id"]
                or related != item.get("related_id")
            ):
                _invalid("close", period, "reference_leaf_mismatch")

    # Only a managed QueryReads snapshot supplies these already checked sources.
    # Leaf identities are still compared, even when another leaf used this source.
    remaining = {}
    for key, expected in requested.items():
        period = expected[0]["close_period"]
        if type(period) is int and period in (_verified_manifests or {}):
            verify_leaves(_verified_manifests[period], expected)
        elif type(period) is int and period in (_verified_headers or {}):
            verify_storage_leaves(_verified_headers[period], expected)
        else:
            remaining[key] = expected
    requested = remaining
    if not requested:
        if _verified_storage_parts is not None:
            _verified_storage_parts.update(added_parts)
        return
    periods = [items[0]["close_period"] for items in requested.values()]
    for row in connection.execute(
        "SELECT q.key,p.period,p.manifest,p.digest,s.source_digest "
        "FROM json_each(?) q LEFT JOIN period_close p ON p.period=q.value "
        "LEFT JOIN read_index_source s "
        "ON s.source_kind='close' AND s.source_id=CAST(p.period AS TEXT)",
        (json.dumps(periods),),
    ):
        expected = requested[json.dumps(periods[int(row[0])])]
        period = expected[0]["close_period"]
        if row["digest"] is None or row["source_digest"] != row["digest"]:
            _invalid("close", period, "source_digest_or_marker_mismatch")
        from .content_history_context import close_reader

        verified_header = close_reader().verified_header

        verify_storage_leaves(verified_header(connection, row), expected)
    if _verified_storage_parts is not None:
        _verified_storage_parts.update(added_parts)


def _sync(connection, kind, ident):
    if not connection.in_transaction:
        raise ValueError("read indexes must be synchronized in the source transaction")
    row = _source(connection, kind, ident)
    if not _supported(kind, row):
        return
    if connection.execute(
        "SELECT 1 FROM read_index_source WHERE source_kind=? AND source_id=?",
        (kind, str(ident)),
    ).fetchone():
        verify_source(connection, kind, ident, source=row)
        return
    _, _, table, key, columns, extract = _SOURCES[kind]
    if kind == "close":
        from .content_history_context import close_reader

        decode_close = close_reader().decode_close

        entries = _close_references(
            row, manifest=decode_close(connection, row, require_marker=False)
        )
    else:
        entries = extract(row)
    placeholders = ",".join("?" for _ in range(len(columns.split(",")) + 1))
    connection.executemany(
        f"INSERT INTO {table}({key},{columns}) VALUES({placeholders})",
        [(ident, *entry) for entry in entries],
    )
    connection.execute(
        "INSERT INTO read_index_source VALUES(?,?,?)", (kind, str(ident), _source_digest(kind, row))
    )
    verify_source(connection, kind, ident, source=row)


def sync_close(connection, period):
    _sync(connection, "close", period)


def sync_job(connection, job_id):
    _sync(connection, "job", job_id)


def sync_audit(connection, audit_id):
    _sync(connection, "audit", audit_id)


def _all_sources(connection):
    for row in connection.execute("SELECT period FROM period_close ORDER BY period"):
        yield "close", row[0]
    for row in connection.execute(
        "SELECT id FROM jobs WHERE kind IN (SELECT value FROM json_each(?)) ORDER BY id",
        (json.dumps(JOB_KINDS),),
    ):
        yield "job", row[0]
    for row in connection.execute(
        "SELECT id FROM audit WHERE action IN (SELECT value FROM json_each(?)) ORDER BY id",
        (json.dumps(AUDIT_ACTIONS),),
    ):
        yield "audit", row[0]


def backfill_read_indexes(connection):
    for kind, ident in _all_sources(connection):
        _sync(connection, kind, ident)
    verify_read_indexes(connection)


def verify_read_indexes(connection, *, _verified_closes=None):
    """Explicit full verification; never called by ordinary connection validation."""
    verified_closes = (
        None
        if _verified_closes is None
        else _require_verified_closes_for_indexes(connection, _verified_closes)
    )
    expected = set()
    for kind, ident in _all_sources(connection):
        expected.add((kind, str(ident)))
        if kind == "close" and verified_closes is not None:
            _verify_close_sources(
                connection,
                {str(ident): _source(connection, kind, ident)},
                verified_manifests=verified_closes,
            )
        else:
            verify_source(connection, kind, ident)
    actual = {
        tuple(row)
        for row in connection.execute("SELECT source_kind,source_id FROM read_index_source")
    }
    if actual != expected:
        _invalid("directory", "*", "source_set_mismatch")
    # Catch an orphan occurrence even if foreign-key enforcement was bypassed.
    for kind, (_, _, table, key, _, _) in _SOURCES.items():
        if connection.execute(
            f"SELECT 1 FROM {table} r LEFT JOIN read_index_source s "
            f"ON s.source_kind=? AND s.source_id=CAST(r.{key} AS TEXT) "
            "WHERE s.source_id IS NULL LIMIT 1",
            (kind,),
        ).fetchone():
            _invalid(kind, "*", "orphan_reference")
    return {"sources": len(expected)}


def repair_read_indexes(connection, *, bundle, fault=None):
    """Restore the four directories in a caller-owned, source-verified transaction.

    Only the eight released immutable directory triggers are suspended. Their
    exact schema-verified SQL is restored before validation. No other table or
    historical dependency is writable through this operation.
    """
    from .versions import verify_schema

    if not connection.in_transaction:
        raise ValueError("read-index repair requires the caller's write transaction")
    verify_schema(connection, bundle=bundle)
    try:
        verified = verify_read_indexes(connection)
        return {"changed": False, **verified}
    except KernelError as exc:
        if exc.code != "read_index_integrity_failed":
            raise
    # Read and validate every frozen source independently of the broken index.
    sources = []
    for kind, ident in _all_sources(connection):
        row = _source(connection, kind, ident)
        if kind == "close":
            from .content_history_context import close_reader

            decode_close = close_reader().decode_close

            decode_close(connection, row, require_marker=False)
        sources.append((kind, ident))
    tables = ("close_reference", "job_reference", "audit_reference", "read_index_source")
    names = tuple(
        f"immutable_{table}_{event}" for table in tables for event in ("UPDATE", "DELETE")
    )
    triggers = {}
    for name in names:
        row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
        if row is None:
            _invalid("directory", name, "repair_trigger_missing")
        triggers[name] = row[0]
    connection.execute("SAVEPOINT read_index_repair")
    try:
        for name in names:
            connection.execute(f'DROP TRIGGER "{name}"')
        if fault:
            fault("after_unseal", connection)
        for table in tables:
            connection.execute(f"DELETE FROM {table}")
        for kind, ident in sources:
            _sync(connection, kind, ident)
        if fault:
            fault("after_rebuild", connection)
            fault("before_restore", connection)
        for name in names:
            connection.execute(triggers[name])
        verify_schema(connection, bundle=bundle)
        result = verify_read_indexes(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            _invalid("directory", "*", "foreign_key_mismatch")
        if fault:
            fault("after_verify", connection)
        connection.execute("RELEASE read_index_repair")
    except BaseException:
        connection.execute("ROLLBACK TO read_index_repair")
        connection.execute("RELEASE read_index_repair")
        raise
    return {"changed": True, **result}


def close_rows(
    connection,
    *,
    periods=None,
    calculation_ids=None,
    subject_ids=None,
    fact_ids=None,
    through_period=None,
    verified_periods=(),
    parsed_manifests=None,
):
    """Find closes and check hits not already checked in this caller's transaction."""
    predicates, parameters = [], []
    periods = tuple(periods) if periods is not None else None
    if periods is not None:
        predicates.append("p.period IN (SELECT value FROM json_each(?))")
        parameters.append(json.dumps(list(periods)))
    if through_period is not None:
        predicates.append("p.period<=?")
        parameters.append(through_period)
    for ids, typ in ((calculation_ids, "calculation"), (fact_ids, "fact")):
        if ids is not None:
            predicates.append(
                "p.period IN (SELECT close_period FROM close_reference "
                "WHERE reference_type=? AND reference_id IN "
                "(SELECT value FROM json_each(?)))"
            )
            parameters.extend((typ, json.dumps(list(ids))))
    if subject_ids is not None:
        # SQLite otherwise chooses the low-cardinality reference_type prefix
        # first and scans every historical calculation reference. Keep the
        # explicit requested identities as the driver of this reverse lookup.
        scoped = (
            "SELECT r.close_period FROM json_each(?) ids "
            "CROSS JOIN calculation c INDEXED BY calculation_subject "
            "ON c.subject_id=ids.value "
            "CROSS JOIN close_reference r INDEXED BY close_reference_lookup "
            "ON r.reference_type='calculation' AND r.reference_id=c.id WHERE 1=1"
        )
        parameters.append(json.dumps(list(subject_ids)))
        if periods is not None:
            scoped += " AND r.close_period IN (SELECT value FROM json_each(?))"
            parameters.append(json.dumps(periods))
        if through_period is not None:
            scoped += " AND r.close_period<=?"
            parameters.append(through_period)
        predicates.append("p.period IN (" + scoped + ")")
    rows = list(
        connection.execute(
            "SELECT p.* FROM period_close p"
            + (" WHERE " + " AND ".join(predicates) if predicates else "")
            + " ORDER BY p.period",
            parameters,
        )
    )
    checked = set(verified_periods)
    verified = verify_sources(
        connection, "close", [row for row in rows if row["period"] not in checked]
    )
    if parsed_manifests is not None:
        parsed_manifests.update(verified)
    return rows


def job_rows(connection, *, subject_id=None, period):
    month = month_type()(period).ordinal
    parameters = [month, month]
    period_match = (
        "SELECT job_id FROM job_reference WHERE start_period IS NOT NULL "
        "AND start_period<=? AND end_period>=?"
    )
    if subject_id is None:
        candidates = period_match
    else:
        # Report period associations also apply to a single business under T3.
        candidates = (
            "SELECT r.job_id FROM job_reference r JOIN jobs j ON j.id=r.job_id "
            "WHERE j.kind='report_export' AND r.start_period IS NOT NULL "
            "AND r.start_period<=? AND r.end_period>=? UNION "
            "SELECT job_id FROM job_reference WHERE reference_type='subject' "
            "AND reference_id IN (SELECT value FROM json_each(?)) "
            "UNION SELECT r.job_id FROM calculation c "
            "JOIN job_reference r ON r.reference_type IN ('calculation','version') "
            "AND r.reference_id=c.id WHERE c.subject_id IN (SELECT value FROM json_each(?)) UNION "
            "SELECT r.job_id FROM fact_revision f JOIN job_reference r "
            "ON r.reference_type IN ('fact','version') AND r.reference_id=f.id "
            "WHERE f.subject_id IN (SELECT value FROM json_each(?))"
        )
        subjects = json.dumps([subject_id] if isinstance(subject_id, str) else sorted(subject_id))
        parameters.extend((subjects, subjects, subjects))
    rows = list(
        connection.execute(
            "SELECT j.* FROM jobs j WHERE j.id IN (" + candidates + ") ORDER BY j.rowid", parameters
        )
    )
    verify_sources(connection, "job", rows)
    return rows


def audit_rows(connection, references):
    requested = list(references)
    if not requested:
        return []
    rows = list(
        connection.execute(
            "SELECT a.* FROM audit a WHERE a.id IN (SELECT r.audit_id FROM json_each(?) q "
            "JOIN audit_reference r ON r.source_type=json_extract(q.value,'$[0]') "
            "AND r.source_id=json_extract(q.value,'$[1]')) ORDER BY a.id",
            (json.dumps(requested),),
        )
    )
    verify_sources(connection, "audit", rows)
    return rows
