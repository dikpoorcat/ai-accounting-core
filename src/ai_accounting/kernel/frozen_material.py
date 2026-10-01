"""Verify whether closed material rows can be reused in a current read.

This module never decides completeness.  It only identifies closed sources whose
frozen, complete row proof still has exactly the same authoritative inputs.
Callers run the normal checker for every other source and construct the current
result (including its digest) in the normal format.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field

from .integrity import _invalid
from .types import YearMonth, canonical, digest

# This identifies the coverage semantics, not the application build. Increment
# it when parsing, allocation, disposition or competing-use rules change. Version
# 2 excludes proven empty XLSX formula templates from invented amount positions.
# 3 additionally validates exact whole-group copies without a second capacity use.
# Earlier complete proofs were checked under different disposition semantics.
# Their current reads must run the full checker, without rewriting old anchors.
# An unknown rule falls back to the full checker; the identity never substitutes
# for the manifest, current-source and evidence-content checks below.
MATERIAL_COVERAGE_RULE_DIGEST = digest(
    {"contract": "ai-accounting-kernel/2/material-coverage", "version": 3}
)


_KINDS = {
    "source": ("material_source_v2", "fact_material_source_v2"),
    "allocation": ("material_period_allocation", "fact_material_period_allocation"),
    "resolution": ("material_resolution_v2", "fact_material_resolution_v2"),
    "group": ("material_group_resolution", "fact_material_group_resolution"),
}
_PROOF_FIELDS = {
    "source": "source_versions",
    "allocation": "allocation_versions",
    "resolution": "resolution_versions",
    "group": "group_versions",
}


@dataclass(frozen=True)
class FrozenMaterialReuse:
    """Validated source-level slices of one complete closed-month proof."""

    close_period: int
    source_ids: frozenset[str]
    coverage_by_source: dict[str, tuple[dict, ...]]
    file_summaries_by_source: dict[str, dict]
    version_ids_by_kind: dict[str, frozenset[str]]
    # Only the separate current-read summary consumes these commitments. They
    # never stand in for coverage rows in a close or a completeness proof.
    summary_commitments: dict[str, dict] = field(default_factory=dict)


def _current_version_summaries(connection, kind, table):
    source_column = "c.subject_id" if kind == "material_source_v2" else "t.source_id"
    result = {}
    for source_id, count, ids, invalid in connection.execute(
        f"SELECT {source_column},count(*),json_group_array(c.fact_id ORDER BY c.fact_id),"
        "max(t.revision_id IS NULL OR z.fact_id IS NULL OR f.subject_id IS NOT c.subject_id) "
        "FROM subject s JOIN fact_current c ON c.subject_id=s.id "
        f"LEFT JOIN {table} t ON t.revision_id=c.fact_id "
        "LEFT JOIN fact_revision f ON f.id=c.fact_id "
        "LEFT JOIN fact_seal z ON z.fact_id=c.fact_id "
        f"WHERE s.kind=? GROUP BY {source_column}",
        (kind,),
    ):
        if invalid:
            _invalid("fact", "*", "current_material_fact_seal_missing")
        result[source_id] = {
            "count": count,
            "digest": hashlib.sha256(ids.encode("utf-8")).hexdigest(),
        }
    return result


def verified_frozen_material_summary(
    connection,
    review_month,
    closed_through,
    registry,
    *,
    _query_reads=None,
    _inspection_cache=None,
):
    """Verify unchanged closed sources without reading their complete row bodies.

    The committed summary only establishes a closed proof. Current dispositions,
    linked result versions, competing sources and evidence are independently
    checked before reuse. Changed or new sources go through the complete checker.
    """
    if closed_through is None:
        return None
    if registry is not None and any(kind not in registry.models for kind, _ in _KINDS.values()):
        return None
    rule = connection.execute(
        "SELECT rule_digest FROM material_close_rule WHERE period=?",
        (closed_through,),
    ).fetchone()
    if rule is None or rule[0] != MATERIAL_COVERAGE_RULE_DIGEST:
        return None
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_watch_root'"
    ).fetchone():
        return _watched_material_summary(
            connection, closed_through, _query_reads, _inspection_cache
        )
    current_sources = _current_versions(connection, *_KINDS["source"])
    if _query_reads is not None:
        rows = _query_reads.authoritative_close_rows(periods=[closed_through])
        if not rows:
            return None
        summaries = _query_reads.close_material_source_summaries(
            rows[0],
            source_ids=current_sources,
        )
    else:
        from .close_storage import material_source_summaries, verified_header

        row = connection.execute(
            "SELECT period,manifest,digest FROM period_close WHERE period=?",
            (closed_through,),
        ).fetchone()
        if row is None:
            return None
        summaries = material_source_summaries(
            connection,
            verified_header(connection, row),
            current_sources,
        )
    current = {
        label: _current_version_summaries(connection, kind, table)
        for label, (kind, table) in _KINDS.items()
    }
    empty = {"count": 0, "digest": digest([]).hex()}
    eligible, commitments, file_summaries = set(), {}, {}
    for source_id, summary in summaries.items():
        file_summary = summary.file_summary
        if (
            not summary.eligible_closed_rows
            or file_summary.get("status") != "complete"
            or file_summary.get("issue_count") != 0
            or file_summary.get("unprocessed_count") != 0
            or file_summary.get("unknown_period_count") != 0
            or file_summary.get("item_count") != summary.coverage_count
            or current_sources.get(source_id) != {file_summary["source_fact_id"]}
            or any(
                current[label].get(source_id, empty) != summary.version_sets[label]
                for label in _KINDS
            )
        ):
            continue
        eligible.add(source_id)
        file_summaries[source_id] = file_summary
        commitments[source_id] = {
            "coverage_count": summary.coverage_count,
            "coverage_digest": summary.coverage_digest,
            "versions": summary.version_sets,
        }
    eligible.intersection_update(
        _sealed_source_evidence(
            connection, {key: file_summaries[key]["source_fact_id"] for key in eligible}
        )
    )
    edges, stale = _source_dependencies(connection, {"source": current_sources})
    queue = list(((set(current_sources) | set(edges)) - eligible) | stale)
    invalid = set(queue)
    while queue:
        for neighbour in edges.get(queue.pop(), ()):
            if neighbour not in invalid:
                invalid.add(neighbour)
                queue.append(neighbour)
    eligible.difference_update(invalid)
    return FrozenMaterialReuse(
        close_period=closed_through,
        source_ids=frozenset(eligible),
        coverage_by_source={},
        file_summaries_by_source={key: file_summaries[key] for key in eligible},
        version_ids_by_kind={label: frozenset() for label in _KINDS},
        summary_commitments={key: commitments[key] for key in eligible},
    )


def _watched_material_summary(connection, closed_through, reads, inspection_cache):
    """Use authenticated post-close changes to locate sources needing a full check."""
    from .material_watch import changed_material_sources

    frozen_sources, affected = changed_material_sources(
        connection, closed_through, _inspection_cache=inspection_cache
    )
    if reads is not None:
        rows = reads.authoritative_close_rows(periods=[closed_through])
        if len(rows) != 1:
            _invalid("close", "*", "frozen_material_close_missing")
        summaries = reads.close_material_source_summaries(rows[0], source_ids=frozen_sources)
    else:
        from .close_storage import material_source_summaries, verified_header

        row = connection.execute(
            "SELECT period,manifest,digest FROM period_close WHERE period=?", (closed_through,)
        ).fetchone()
        if row is None:
            _invalid("close", "*", "frozen_material_close_missing")
        summaries = material_source_summaries(
            connection, verified_header(connection, row), frozen_sources
        )
    eligible, commitments, files = set(), {}, {}
    for source_id, summary in summaries.items():
        file_summary = summary.file_summary
        if (
            source_id in affected
            or not summary.eligible_closed_rows
            or file_summary.get("status") != "complete"
            or file_summary.get("issue_count") != 0
            or file_summary.get("unprocessed_count") != 0
            or file_summary.get("unknown_period_count") != 0
            or file_summary.get("item_count") != summary.coverage_count
        ):
            continue
        eligible.add(source_id)
        files[source_id] = file_summary
        commitments[source_id] = {
            "coverage_count": summary.coverage_count,
            "coverage_digest": summary.coverage_digest,
            "versions": summary.version_sets,
        }
    if set(summaries) != frozen_sources:
        _invalid("close", "*", "frozen_material_source_directory_mismatch")
    return FrozenMaterialReuse(
        close_period=closed_through,
        source_ids=frozenset(eligible),
        coverage_by_source={},
        file_summaries_by_source=files,
        version_ids_by_kind={label: frozenset() for label in _KINDS},
        summary_commitments=commitments,
    )


def _current_versions(connection, kind, table):
    source_column = "c.subject_id" if kind == "material_source_v2" else "t.source_id"
    rows = connection.execute(
        f"SELECT {source_column},c.fact_id FROM {table} t "
        "JOIN fact_current c ON c.fact_id=t.revision_id "
        "JOIN fact_revision f ON f.id=c.fact_id "
        "JOIN fact_seal seal ON seal.fact_id=f.id "
        "JOIN subject s ON s.id=c.subject_id WHERE s.kind=?",
        (kind,),
    ).fetchall()
    count = connection.execute(
        "SELECT count(*) FROM fact_current c JOIN subject s ON s.id=c.subject_id WHERE s.kind=?",
        (kind,),
    ).fetchone()[0]
    if len(rows) != count:
        _invalid("fact", "*", "current_material_fact_seal_missing")
    result = defaultdict(set)
    for source_id, fact_id in rows:
        result[source_id].add(fact_id)
    return result


def _frozen_versions(connection, kind, table, identifiers):
    if not isinstance(identifiers, list) or any(not isinstance(v, str) for v in identifiers):
        _invalid("close", "*", "frozen_material_version_list_invalid")
    if len(identifiers) != len(set(identifiers)):
        _invalid("close", "*", "frozen_material_version_list_duplicate")
    if not identifiers:
        return defaultdict(set)
    if kind == "material_source_v2":
        rows = connection.execute(
            "SELECT f.subject_id,ids.value FROM json_each(?) ids "
            "CROSS JOIN fact_revision f ON f.id=ids.value "
            "CROSS JOIN fact_seal seal ON seal.fact_id=f.id "
            "CROSS JOIN subject s ON s.id=f.subject_id AND s.kind=? "
            f"CROSS JOIN {table} t ON t.revision_id=f.id",
            (canonical(identifiers), kind),
        ).fetchall()
    else:
        rows = connection.execute(
            f"SELECT t.source_id,ids.value FROM json_each(?) ids CROSS JOIN {table} t "
            "ON t.revision_id=ids.value CROSS JOIN fact_revision f ON f.id=t.revision_id "
            "CROSS JOIN fact_seal seal ON seal.fact_id=f.id "
            "CROSS JOIN subject s ON s.id=f.subject_id AND s.kind=?",
            (canonical(identifiers), kind),
        ).fetchall()
    if len(rows) != len(identifiers):
        _invalid("close", "*", "frozen_material_fact_seal_missing")
    result = defaultdict(set)
    for source_id, fact_id in rows:
        result[source_id].add(fact_id)
    return result


def _changed_sources(connection, kind, table, identifiers):
    """Compare complete current and frozen fact sets inside SQLite.

    A full set difference is needed: checking only frozen IDs would miss new
    current dispositions or group competitors.  The query returns only source
    identities whose exact fact set changed.
    """
    source_current = "c.subject_id" if kind == "material_source_v2" else "t.source_id"
    source_frozen = "f.subject_id" if kind == "material_source_v2" else "t.source_id"
    frozen_ids = canonical(identifiers)
    sealed_count = connection.execute(
        "SELECT count(*) FROM json_each(?) ids "
        "CROSS JOIN fact_revision f ON f.id=ids.value "
        "CROSS JOIN fact_seal seal ON seal.fact_id=f.id "
        "CROSS JOIN subject s ON s.id=f.subject_id AND s.kind=? "
        f"CROSS JOIN {table} t ON t.revision_id=f.id",
        (frozen_ids, kind),
    ).fetchone()[0]
    if sealed_count != len(identifiers):
        _invalid("close", "*", "frozen_material_fact_seal_missing")
    missing = connection.execute(
        "SELECT 1 FROM fact_current c JOIN subject s ON s.id=c.subject_id "
        f"LEFT JOIN {table} t ON t.revision_id=c.fact_id "
        "LEFT JOIN fact_seal seal ON seal.fact_id=c.fact_id "
        "WHERE s.kind=? AND (t.revision_id IS NULL OR seal.fact_id IS NULL) LIMIT 1",
        (kind,),
    ).fetchone()
    if missing:
        _invalid("fact", "*", "current_material_fact_seal_missing")
    rows = connection.execute(
        "WITH frozen(id) AS MATERIALIZED (SELECT value FROM json_each(?)),"
        "added AS ("
        f"SELECT {source_current} source_id FROM fact_current c "
        "JOIN subject s ON s.id=c.subject_id AND s.kind=? "
        f"JOIN {table} t ON t.revision_id=c.fact_id "
        "WHERE c.fact_id NOT IN (SELECT id FROM frozen)),"
        "removed AS ("
        f"SELECT {source_frozen} source_id FROM frozen ids "
        "JOIN fact_revision f ON f.id=ids.id "
        f"JOIN {table} t ON t.revision_id=f.id "
        "WHERE NOT EXISTS(SELECT 1 FROM fact_current c WHERE c.fact_id=ids.id)) "
        "SELECT source_id FROM added UNION SELECT source_id FROM removed",
        (frozen_ids, kind),
    ).fetchall()
    return {row[0] for row in rows}


def _eligible_frozen_ids(connection, kind, table, identifiers, eligible):
    """Use a flat frozen directory, asking SQLite only for excluded IDs."""
    if not identifiers or not eligible:
        return frozenset()
    source_column = "f.subject_id" if kind == "material_source_v2" else "t.source_id"
    excluded = {
        row[0]
        for row in connection.execute(
            "SELECT ids.value FROM json_each(?) ids "
            "JOIN fact_revision f ON f.id=ids.value "
            f"JOIN {table} t ON t.revision_id=f.id "
            f"WHERE {source_column} NOT IN (SELECT value FROM json_each(?))",
            (canonical(identifiers), canonical(sorted(eligible))),
        )
    }
    return frozenset(identifiers) - excluded


def _sealed_source_evidence(connection, source_facts):
    """Check each original's bytes once, retaining every exact source binding."""
    if not source_facts:
        return set()
    bindings = {}
    for row in connection.execute(
        "SELECT requested.key,s.evidence_digest,e.digest FROM json_each(?) requested "
        "JOIN fact_material_source_v2 s ON s.revision_id=requested.value "
        "JOIN fact_revision f ON f.id=s.revision_id AND f.subject_id=requested.key "
        "JOIN fact_seal seal ON seal.fact_id=f.id "
        "JOIN fact_evidence fe ON fe.fact_id=f.id AND fe.evidence_digest=unhex(s.evidence_digest) "
        "JOIN evidence e ON e.digest=fe.evidence_digest",
        (canonical(source_facts),),
    ):
        source_id, expected, evidence_digest = row
        if isinstance(expected, str) and evidence_digest.hex() == expected:
            bindings[source_id] = expected
    checked = set()
    for expected, content in connection.execute(
        "SELECT ids.value,e.content FROM json_each(?) ids JOIN evidence e "
        "ON e.digest=unhex(ids.value)",
        (canonical(sorted(set(bindings.values()))),),
    ):
        if hashlib.sha256(content).hexdigest() != expected:
            _invalid("evidence", expected, "evidence_digest_mismatch")
        checked.add(expected)
    return {source_id for source_id, ident in bindings.items() if ident in checked}


def _source_dependencies(connection, current):
    """Find current cross-source links and locally stale linked results."""
    edges = defaultdict(set)
    stale = set()
    branches = []
    kinds = []
    for label in ("resolution", "group"):
        kind, table = _KINDS[label]
        branches.append(
            "SELECT t.source_id,l.subject_id,l.fact_kind,l.fact_id,"
            f"l.calculation_id FROM {table} t "
            "JOIN fact_current c ON c.fact_id=t.revision_id "
            "JOIN subject own ON own.id=c.subject_id AND own.kind=? "
            f"JOIN {table}_links l ON l.revision_id=t.revision_id"
        )
        kinds.append(kind)
    # Keep every authoritative link in the checks, but return only affected
    # sources and groups of cross-source links. Thousands of rows from one file
    # do not need to become thousands of Python objects. IS NOT deliberately
    # treats a missing current fact/result as a mismatch as well.
    for source_id, target, is_stale in connection.execute(
        "WITH links AS MATERIALIZED (" + " UNION ALL ".join(branches) + ") "
        "SELECT l.source_id,NULL,1 FROM links l "
        "LEFT JOIN fact_current fc ON fc.subject_id=l.subject_id "
        "LEFT JOIN calculation_current cc ON cc.subject_id=l.subject_id "
        "LEFT JOIN calculation calc ON calc.id=cc.calculation_id "
        "LEFT JOIN calculation_seal seal ON seal.calculation_id=calc.id "
        "LEFT JOIN subject s ON s.id=l.subject_id "
        "LEFT JOIN pending p ON p.subject_id=l.subject_id "
        "WHERE l.fact_id IS NOT fc.fact_id "
        "OR l.calculation_id IS NOT cc.calculation_id "
        "OR l.fact_kind IS NOT s.kind OR calc.fact_id IS NOT l.fact_id "
        "OR seal.calculation_id IS NULL OR p.subject_id IS NOT NULL "
        "GROUP BY l.source_id UNION ALL "
        "SELECT NULL,json_group_array(DISTINCT source_id),0 FROM links "
        "GROUP BY subject_id HAVING count(DISTINCT source_id)>1",
        tuple(kinds),
    ):
        if is_stale:
            stale.add(source_id)
        else:
            linked = set(json.loads(target))
            for source_id in linked:
                edges[source_id].update(linked - {source_id})
    for source_id, target in connection.execute(
        "SELECT r.source_id,r.duplicate_source_id FROM fact_material_resolution_v2 r "
        "JOIN fact_current c ON c.fact_id=r.revision_id "
        "JOIN subject s ON s.id=c.subject_id AND s.kind=? "
        "WHERE r.duplicate_source_id IS NOT NULL",
        (_KINDS["resolution"][0],),
    ):
        edges[source_id].add(target)
        edges[target].add(source_id)
    by_evidence = defaultdict(set)
    evidence_by_fact = dict(
        connection.execute(
            "SELECT t.revision_id,t.evidence_digest FROM fact_material_source_v2 t "
            "JOIN fact_current c ON c.fact_id=t.revision_id "
            "JOIN subject s ON s.id=c.subject_id AND s.kind=?",
            (_KINDS["source"][0],),
        )
    )
    for source_id, source_fact_id in current["source"].items():
        if len(source_fact_id) != 1:
            stale.add(source_id)
            continue
        evidence = evidence_by_fact.get(next(iter(source_fact_id)))
        if evidence is None:
            stale.add(source_id)
        else:
            by_evidence[evidence].add(source_id)
    for linked in by_evidence.values():
        for source_id in linked:
            edges[source_id].update(linked - {source_id})
    return edges, stale


def verified_frozen_materials(
    connection,
    review_month: int,
    closed_through: int | None,
    registry=None,
    *,
    _query_reads=None,
) -> FrozenMaterialReuse | None:
    """Return proven reusable sources, or ``None`` if no usable proof exists.

    The coverage-rule anchor was written in the close transaction. A schema
    version or material epoch alone is not a sufficient rule identity.
    """
    if closed_through is None or review_month <= closed_through:
        return None
    if registry is not None and any(kind not in registry.models for kind, _ in _KINDS.values()):
        return None
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_close_rule'"
        ).fetchone()
        is None
    ):
        return None
    anchored = connection.execute(
        "SELECT rule_digest FROM material_close_rule WHERE period=?", (closed_through,)
    ).fetchone()
    if anchored is None or anchored[0] != MATERIAL_COVERAGE_RULE_DIGEST:
        return None
    if _query_reads is not None and _query_reads._snapshot_active:
        if _query_reads.connection is not connection:
            raise ValueError("material sources belong to another snapshot connection")
        rows = _query_reads.authoritative_close_rows(periods=[closed_through])
        if not rows:
            return None
        manifest = _query_reads.close_manifest(rows[0])
    else:
        row = connection.execute(
            "SELECT period,manifest,digest FROM period_close WHERE period=?", (closed_through,)
        ).fetchone()
        if row is None:
            return None
        from .close_storage import decode_close

        manifest = decode_close(connection, row)
    proof = manifest["material_coverage"]
    if (
        manifest["period"] != str(YearMonth.from_ordinal(closed_through))
        or proof.get("status") != "complete"
        or proof.get("period") != manifest["period"]
        or not isinstance(proof.get("coverage"), list)
        or not isinstance(proof.get("file_summaries"), list)
        or proof.get("coverage_digest")
        != digest(
            {key: value for key, value in proof.items() if key not in {"status", "coverage_digest"}}
            | {"issues": []}
        ).hex()
    ):
        _invalid("close", closed_through, "material_coverage_mismatch")
    coverage = defaultdict(list)
    for item in proof["coverage"]:
        if not isinstance(item, dict) or not isinstance(item.get("source_id"), str):
            return None
        coverage[item["source_id"]].append(item)
    summaries = {}
    for item in proof["file_summaries"]:
        if not isinstance(item, dict) or not isinstance(item.get("source_id"), str):
            return None
        if item["source_id"] in summaries:
            return None
        summaries[item["source_id"]] = item
    current = {}
    changed_sources = set()
    for label, (kind, table) in _KINDS.items():
        current[label] = _current_versions(connection, kind, table) if label == "source" else None
        identifiers = proof.get(_PROOF_FIELDS[label])
        if not isinstance(identifiers, list) or any(not isinstance(v, str) for v in identifiers):
            _invalid("close", "*", "frozen_material_version_list_invalid")
        if len(identifiers) != len(set(identifiers)):
            _invalid("close", "*", "frozen_material_version_list_duplicate")
        if label == "source":
            frozen_sources = _frozen_versions(connection, kind, table, identifiers)
        else:
            changed_sources.update(_changed_sources(connection, kind, table, identifiers))
    eligible = set()
    for source_id, summary in summaries.items():
        rows = coverage.get(source_id, ())
        if (
            summary.get("status") != "complete"
            or summary.get("issue_count") != 0
            or not rows
            or summary.get("item_count") != len(rows)
            or summary.get("unprocessed_count") != 0
            or summary.get("unknown_period_count") != 0
            or summary.get("source_fact_id") not in current["source"].get(source_id, ())
            or current["source"].get(source_id, set()) != frozen_sources.get(source_id, set())
            or source_id in changed_sources
        ):
            continue
        if not all(
            item.get("source_fact_id") == summary["source_fact_id"]
            and item.get("complete") is True
            and item.get("review_period") == manifest["period"]
            and isinstance(item.get("origin_periods"), list)
            and item["origin_periods"]
            and all(YearMonth(value).ordinal <= closed_through for value in item["origin_periods"])
            for item in rows
        ):
            continue
        eligible.add(source_id)
    eligible.intersection_update(
        _sealed_source_evidence(
            connection, {key: summaries[key]["source_fact_id"] for key in eligible}
        )
    )
    edges, stale = _source_dependencies(connection, current)
    queue = list(((set(current["source"]) | set(edges)) - eligible) | stale)
    invalid = set(queue)
    while queue:
        for neighbour in edges.get(queue.pop(), ()):
            if neighbour not in invalid:
                invalid.add(neighbour)
                queue.append(neighbour)
    eligible.difference_update(invalid)
    return FrozenMaterialReuse(
        close_period=closed_through,
        source_ids=frozenset(eligible),
        coverage_by_source={source_id: tuple(coverage[source_id]) for source_id in eligible},
        file_summaries_by_source={source_id: summaries[source_id] for source_id in eligible},
        version_ids_by_kind={
            label: _eligible_frozen_ids(
                connection, kind, table, proof[_PROOF_FIELDS[label]], eligible
            )
            for label, (kind, table) in _KINDS.items()
        },
    )
