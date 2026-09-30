"""Private, bounded storage for the unchanged logical period-close v4 contract.

The logical close digest remains the identity used by approval, the preceding
close chain, reports, and read-index markers. Storage SHA commits a compact root
whose named subroots commit fixed buckets of source records. A partial read is
explicitly typed; only decode_close returns a complete logical v4 manifest.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .close_contract_v1 import require_close_contract
from .content_v1 import _V1YearMonth as YearMonth
from .contracts import KernelError
from .key_membership_filter_v1 import build_keys_filter, decode_keys_filter, may_contain

STORAGE_FORMAT = "ai-accounting-kernel/2/close-storage/3"
SOURCE_CHANGE_CONTRACT = "ai-accounting-kernel/2/source-change/1"
BLOCK_SIZE = 128
FAMILIES = ("accounting", "material", "management", "review")
ACCOUNTING_FIELDS = ("adopted_results", "vouchers")
MATERIAL_FIELDS = (
    "material_coverage.coverage",
    "material_coverage.file_summaries",
    "material_coverage.fact_ids",
    "material_coverage.resolution_versions",
    "material_coverage.inventory_versions",
    "material_coverage.allocation_versions",
    "material_coverage.source_versions",
    "material_coverage.group_versions",
)
ALL_FIELDS = ACCOUNTING_FIELDS + MATERIAL_FIELDS
MATERIAL_SUMMARIES_FIELD = "material_source_summaries"
_MATERIAL_KINDS = {
    "source": ("material_source_v2", "fact_material_source_v2"),
    "allocation": ("material_period_allocation", "fact_material_period_allocation"),
    "resolution": ("material_resolution_v2", "fact_material_resolution_v2"),
    "group": ("material_group_resolution", "fact_material_group_resolution"),
}
_MATERIAL_PROOF_FIELDS = {
    "source": "source_versions",
    "allocation": "allocation_versions",
    "resolution": "resolution_versions",
    "group": "group_versions",
}


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )

@dataclass(frozen=True)
class CloseHeader:
    period: int
    logical_digest: bytes
    storage_digest: bytes
    root: dict[str, Any]


@dataclass(frozen=True)
class CloseAccountingSlice:
    period: int
    logical_digest: bytes
    subjects: frozenset[str]
    adopted_results: tuple[dict, ...]
    vouchers: tuple[dict, ...]


@dataclass(frozen=True)
class CloseMaterialSlice:
    period: int
    logical_digest: bytes
    source_ids: frozenset[str]
    coverage: tuple[dict, ...]
    file_summaries: tuple[dict, ...]


@dataclass(frozen=True)
class CloseMaterialSourceSummary:
    source_id: str
    file_summary: dict | None
    coverage_count: int
    coverage_digest: str
    eligible_closed_rows: bool
    version_sets: dict[str, dict[str, int | str]]


def _invalid(period, reason):
    raise KernelError(
        "content_integrity_failed",
        "关账私有存储与已冻结的逻辑关账内容不一致",
        component="close",
        record_id=str(period),
        reason=reason,
    )


def _sha(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def _accounting_subject_filter(subroot, period):
    """Only a checked accounting subroot may prove a subject absent."""
    if not isinstance(subroot, dict) or set(subroot) != {"directories", "subject_filter"}:
        _invalid(period, "storage_accounting_filter_invalid")
    try:
        return decode_keys_filter(subroot["subject_filter"])
    except ValueError:
        _invalid(period, "storage_accounting_filter_invalid")


def _preview_digest(manifest: dict) -> str:
    # Retain the released receipt rule even if current close-review presentation
    # changes in a future content version.
    approval = manifest.get("approval")
    if isinstance(approval, dict) and isinstance(approval.get("preview_digest"), str):
        return approval["preview_digest"]
    preview = {**manifest, "approval": None}
    versions = manifest["read_version"]
    return _sha(canonical(
        [preview, versions["accounting"], versions["material"], versions["management"]]
    )).hex()


def _bucket(key: str) -> int:
    return hashlib.sha256(key.encode("utf-8")).digest()[0]


def _path(root: dict, path: str):
    value = root
    for segment in path.split("."):
        value = value[segment]
    return value


def _set(root: dict, path: str, value):
    parts = path.split(".")
    target = root
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def _key(field: str, value, adopted_subjects):
    if field == "adopted_results":
        return value["subject_id"]
    if field == "vouchers":
        return adopted_subjects[value["adopted_calculation_id"]]
    if field in ("material_coverage.coverage", "material_coverage.file_summaries"):
        return value["source_id"]
    if field == "material_coverage.inventory_versions":
        return str(value["inventory_id"])
    if field == MATERIAL_SUMMARIES_FIELD:
        return value["source_id"]
    return value


def _v1_frozen_versions(connection, kind, table, identifiers):
    if not isinstance(identifiers, list) or any(
        not isinstance(value, str) for value in identifiers
    ) or len(identifiers) != len(set(identifiers)):
        _invalid("*", "frozen_material_version_list_invalid")
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
        _invalid("*", "frozen_material_fact_seal_missing")
    grouped = defaultdict(set)
    for source_id, fact_id in rows:
        grouped[source_id].add(fact_id)
    return grouped


def _material_summaries(connection, logical_manifest):
    """Derive compact per-source facts from the complete logical material proof."""
    proof = logical_manifest["material_coverage"]
    coverage = defaultdict(list)
    for item in proof["coverage"]:
        coverage[item["source_id"]].append(item)
    files = {item["source_id"]: item for item in proof["file_summaries"]}
    versions = {
        label: _v1_frozen_versions(
            connection, kind, table, proof[_MATERIAL_PROOF_FIELDS[label]]
        )
        for label, (kind, table) in _MATERIAL_KINDS.items()
    }
    all_sources = set(coverage) | set(files)
    for grouped in versions.values():
        all_sources.update(grouped)
    summaries = []
    for source_id in sorted(all_sources):
        rows = coverage[source_id]
        file_summary = files.get(source_id)
        eligible_closed_rows = (
            isinstance(file_summary, dict)
            and file_summary.get("status") == "complete"
            and file_summary.get("issue_count") == 0
            and bool(rows)
            and file_summary.get("item_count") == len(rows)
            and file_summary.get("unprocessed_count") == 0
            and file_summary.get("unknown_period_count") == 0
            and all(
                item.get("source_fact_id") == file_summary.get("source_fact_id")
                and item.get("complete") is True
                and item.get("review_period") == logical_manifest["period"]
                and isinstance(item.get("origin_periods"), list)
                and bool(item["origin_periods"])
                and all(
                    YearMonth(origin).ordinal <= YearMonth(logical_manifest["period"]).ordinal
                    for origin in item["origin_periods"]
                )
                for item in rows
            )
        )
        version_sets = {}
        for label, grouped in versions.items():
            identifiers = sorted(grouped.get(source_id, ()))
            version_sets[label] = {
                "count": len(identifiers),
                "digest": _sha(canonical(identifiers)).hex(),
            }
        summaries.append(
            {
                "source_id": source_id,
                "file_summary": file_summary,
                "coverage_count": len(coverage[source_id]),
                "coverage_digest": _sha(canonical(coverage[source_id])).hex(),
                "eligible_closed_rows": eligible_closed_rows,
                "version_sets": version_sets,
            }
        )
    return summaries


def verified_header(connection, row, *, require_marker=True) -> CloseHeader:
    """Verify root SHA, logical digest, marker, and actual company identity."""
    period = row["period"]
    storage = connection.execute(
        "SELECT storage_digest FROM close_storage_root WHERE period=?", (period,)
    ).fetchone()
    if storage is None or _sha(row["manifest"]) != bytes(storage[0]):
        _invalid(period, "storage_root_digest_mismatch")
    if require_marker:
        marker = connection.execute(
            "SELECT source_digest FROM read_index_source "
            "WHERE source_kind='close' AND source_id=?",
            (str(period),),
        ).fetchone()
        if marker is None or bytes(marker[0]) != bytes(row["digest"]):
            _invalid(period, "source_digest_or_marker_mismatch")
    try:
        root = json.loads(row["manifest"])
        identity = connection.execute(
            "SELECT company_id,database_id FROM identity WHERE id=1"
        ).fetchone()
        if (
            not isinstance(root, dict)
            or set(root) != {
                "encoding", "period", "company_id", "database_id", "logical_digest",
                "preview_digest", "small", "subroots", "derived_roots",
                "source_changes",
            }
            or root["encoding"] != STORAGE_FORMAT
            or not isinstance(root["source_changes"], dict)
            or set(root["source_changes"]) != {"contract", "highwater"}
            or root["source_changes"]["contract"] != SOURCE_CHANGE_CONTRACT
            or type(root["source_changes"]["highwater"]) is not int
            or root["source_changes"]["highwater"] < 0
            or root["period"] != period
            or root["logical_digest"] != bytes(row["digest"]).hex()
            or not isinstance(root["preview_digest"], str)
            or len(root["preview_digest"]) != 64
            or any(char not in "0123456789abcdef" for char in root["preview_digest"])
            or (
                root["small"]["approval"] is not None
                and root["preview_digest"] != root["small"]["approval"]["preview_digest"]
            )
            or identity is None
            or root["company_id"] != identity["company_id"]
            or root["database_id"] != identity["database_id"]
            or set(root["subroots"]) != set(FAMILIES)
            or not isinstance(root["derived_roots"], dict)
            or any(
                not isinstance(name, str)
                or not name
                or not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
                for name, value in root["derived_roots"].items()
            )
            or root["small"]["period"] != str(YearMonth.from_ordinal(period))
            or root["small"]["company_id"] != root["company_id"]
            or root["small"]["database_id"] != root["database_id"]
        ):
            _invalid(period, "storage_root_identity_mismatch")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _invalid(period, "storage_root_contract_invalid")
    return CloseHeader(period, bytes(row["digest"]), bytes(storage[0]), root)


def derived_root(header: CloseHeader, name: str) -> bytes | None:
    """Return a named, committed private projection root from a verified header."""
    value = header.root["derived_roots"].get(name)
    return bytes.fromhex(value) if value is not None else None


def _family(connection, header: CloseHeader, family: str):
    row = connection.execute(
        "SELECT content,digest FROM close_storage_subroot WHERE period=? AND family=?",
        (header.period, family),
    ).fetchone()
    expected = header.root["subroots"].get(family)
    if row is None or _sha(row["content"]) != bytes(row["digest"]) or (
        bytes(row["digest"]).hex() != expected
    ):
        _invalid(header.period, "storage_subroot_digest_mismatch")
    try:
        return json.loads(row["content"])
    except json.JSONDecodeError:
        _invalid(header.period, "storage_subroot_invalid")


def _family_once(connection, header, family, subroots):
    if subroots is None:
        return _family(connection, header, family)
    if family not in subroots:
        subroots[family] = _family(connection, header, family)
    return subroots[family]


def _bucket_rows(connection, header, subroot, field, bucket):
    descriptors = subroot["directories"][field]
    expected = next((entry for entry in descriptors if entry[0] == bucket), None)
    if expected is None:
        return []
    directory = connection.execute(
        "SELECT content,digest FROM close_storage_directory "
        "WHERE period=? AND field=? AND bucket=?",
        (header.period, field, bucket),
    ).fetchone()
    if directory is None or _sha(directory["content"]) != bytes(directory["digest"]) or (
        bytes(directory["digest"]).hex() != expected[1]
    ):
        _invalid(header.period, "storage_directory_digest_mismatch")
    try:
        parts = json.loads(directory["content"])
        if (
            not isinstance(parts, list)
            or [item[0] for item in parts] != list(range(len(parts)))
            or sum(item[2] for item in parts) != expected[2]
        ):
            _invalid(header.period, "storage_directory_contract_invalid")
    except (TypeError, ValueError, IndexError, KeyError, json.JSONDecodeError):
        _invalid(header.period, "storage_directory_contract_invalid")
    actual_parts = connection.execute(
        "SELECT count(*) FROM close_storage_block WHERE period=? AND field=? AND bucket=?",
        (header.period, field, bucket),
    ).fetchone()[0]
    if actual_parts != len(parts):
        _invalid(header.period, "storage_block_multiset_mismatch")
    entries = []
    for part, hashed, count in parts:
        row = connection.execute(
            "SELECT content,digest FROM close_storage_block "
            "WHERE period=? AND field=? AND bucket=? AND part=?",
            (header.period, field, bucket, part),
        ).fetchone()
        if row is None or _sha(row["content"]) != bytes(row["digest"]) or (
            bytes(row["digest"]).hex() != hashed
        ):
            _invalid(header.period, "storage_block_digest_mismatch")
        try:
            decoded = json.loads(row["content"])
            if not isinstance(decoded, list) or len(decoded) != count:
                _invalid(header.period, "storage_block_contract_invalid")
        except json.JSONDecodeError:
            _invalid(header.period, "storage_block_contract_invalid")
        entries.extend(decoded if not field.startswith("owner_review_keys:") else [decoded])
    return entries


def _field_values(connection, header, subroot, field):
    values = []
    for bucket, _, _ in subroot["directories"][field]:
        for index, value in _bucket_rows(connection, header, subroot, field, bucket):
            if field != "vouchers" and _bucket(_key(field, value, {})) != bucket:
                _invalid(header.period, "storage_bucket_identity_mismatch")
            values.append((index, value))
    if len(values) != len({index for index, _ in values}):
        _invalid(header.period, "storage_position_duplicate")
    return [value for _, value in sorted(values)]


def _management_subroot(connection, header, *, _subroots=None):
    subroot = _family_once(connection, header, "management", _subroots)
    if not isinstance(subroot, dict) or set(subroot) != {"directories"}:
        _invalid(header.period, "storage_management_contract_invalid")
    directories = subroot["directories"]
    if (
        not isinstance(directories, dict)
        or "management_snapshot" not in directories
        or any(
            type(name) is not str
            or (name != "management_snapshot" and not name.startswith("readiness:"))
            or name == "readiness:"
            or type(descriptors) is not list
            or len(descriptors) != 1
            or type(descriptors[0]) is not list
            or len(descriptors[0]) != 3
            or descriptors[0][0] != 0
            or type(descriptors[0][2]) is not int
            or descriptors[0][2] != 1
            for name, descriptors in directories.items()
        )
    ):
        _invalid(header.period, "storage_management_contract_invalid")
    return subroot


def _management_value(connection, header, subroot, name):
    if name not in subroot["directories"]:
        _invalid(header.period, "storage_management_section_missing")
    entries = _bucket_rows(connection, header, subroot, name, 0)
    if len(entries) != 1 or entries[0][0] != 0:
        _invalid(header.period, "storage_management_section_invalid")
    return entries[0][1]


def read_readiness_check(connection, header: CloseHeader, name: str):
    """Read one historical root-committed readiness checker."""
    if type(name) is not str or not name:
        raise ValueError("readiness checker name must be nonempty")
    subroot = _management_subroot(connection, header)
    section = "readiness:" + name
    if section not in subroot["directories"]:
        return {}
    return _management_value(connection, header, subroot, section)


def read_accounting(
    connection, header: CloseHeader, subjects, *, _verified_parts=None
) -> CloseAccountingSlice:
    """Read only committed subject buckets; publication rows decide existence."""
    subjects = frozenset(subjects)
    if not subjects:
        return CloseAccountingSlice(header.period, header.logical_digest, subjects, (), ())
    subroot = _family(connection, header, "accounting")
    subject_filter = _accounting_subject_filter(subroot, header.period)
    adopted, vouchers = [], []
    try:
        subject_buckets = {
            _bucket(subject) for subject in subjects if may_contain(subject_filter, subject)
        }
    except ValueError:
        _invalid(header.period, "storage_accounting_filter_invalid")
    for bucket in sorted(subject_buckets):
        adopted.extend(_bucket_rows(connection, header, subroot, "adopted_results", bucket))
        vouchers.extend(_bucket_rows(connection, header, subroot, "vouchers", bucket))
    selected = [
        (position, item) for position, item in adopted
        if item["subject_id"] in subjects
    ]
    adopted_by_id = {item["calculation_id"]: item for _, item in selected}
    chosen_vouchers = [
        (position, item) for position, item in vouchers
        if item["adopted_calculation_id"] in adopted_by_id
    ]
    for _, item in selected:
        if _bucket(item["subject_id"]) not in subject_buckets:
            _invalid(header.period, "storage_bucket_identity_mismatch")
        if item["posting_period"] != str(YearMonth.from_ordinal(header.period)):
            _invalid(header.period, "storage_adoption_period_mismatch")
    # A missing reverse-directory row cannot conceal an adopted publication.
    expected = {
        row["subject_id"]: (row["id"], row["calculation_id"])
        for row in connection.execute(
            "SELECT p.subject_id,p.id,p.calculation_id FROM json_each(?) requested "
            "JOIN calculation_publication p ON p.subject_id=requested.value "
            "WHERE p.posting_period=? AND p.sequence<=? AND p.calculation_id IS NOT NULL "
            "AND NOT EXISTS(SELECT 1 FROM calculation_publication later "
            "WHERE later.previous_publication_id=p.id AND later.sequence<=?)",
            (
                canonical(sorted(subjects)), header.period,
                header.root["small"]["publication_sequence"],
                header.root["small"]["publication_sequence"],
            ),
        )
    }
    actual = {
        item["subject_id"]: (item["publication_id"], item["calculation_id"])
        for _, item in selected
    }
    if actual != expected:
        _invalid(header.period, "storage_adoption_publication_mismatch")
    expected_vouchers = {
        row["id"]
        for row in connection.execute(
            "SELECT v.id FROM voucher_current h "
            "JOIN voucher_version v ON v.id=h.version_id "
            "JOIN calculation c ON c.id=v.calculation_id "
            "WHERE v.period=? AND c.subject_id IN (SELECT value FROM json_each(?))",
            (header.period, canonical(sorted(subjects))),
        )
    }
    if {item["id"] for _, item in chosen_vouchers} != expected_vouchers:
        _invalid(header.period, "storage_voucher_source_mismatch")
    return CloseAccountingSlice(
        header.period,
        header.logical_digest,
        subjects,
        tuple(item for _, item in sorted(selected)),
        tuple(item for _, item in sorted(chosen_vouchers)),
    )


def read_material_sources(connection, header: CloseHeader, source_ids) -> CloseMaterialSlice:
    """Read bounded material coverage and summary buckets for named sources."""
    source_ids = frozenset(source_ids)
    if not source_ids:
        return CloseMaterialSlice(header.period, header.logical_digest, source_ids, (), ())
    subroot = _family(connection, header, "material")
    result = {}
    for field in ("material_coverage.coverage", "material_coverage.file_summaries"):
        entries = []
        for bucket in sorted({_bucket(source_id) for source_id in source_ids}):
            for position, item in _bucket_rows(connection, header, subroot, field, bucket):
                if _bucket(item["source_id"]) != bucket:
                    _invalid(header.period, "storage_bucket_identity_mismatch")
                if item["source_id"] in source_ids:
                    entries.append((position, item))
        result[field] = tuple(item for _, item in sorted(entries))
    return CloseMaterialSlice(
        header.period, header.logical_digest, source_ids,
        result["material_coverage.coverage"],
        result["material_coverage.file_summaries"],
    )


def material_source_summaries(
    connection, header: CloseHeader, source_ids
) -> dict[str, CloseMaterialSourceSummary]:
    """Read bounded per-source freeze summaries, never full material coverage."""
    source_ids = frozenset(source_ids)
    if not source_ids:
        return {}
    subroot = _family(connection, header, "material")
    found = {}
    for bucket in sorted({_bucket(source_id) for source_id in source_ids}):
        for _, item in _bucket_rows(
            connection, header, subroot, MATERIAL_SUMMARIES_FIELD, bucket
        ):
            source_id = item["source_id"]
            if _bucket(source_id) != bucket:
                _invalid(header.period, "storage_bucket_identity_mismatch")
            if source_id in source_ids:
                if source_id in found:
                    _invalid(header.period, "storage_material_summary_duplicate")
                found[source_id] = CloseMaterialSourceSummary(
                    source_id=source_id,
                    file_summary=item["file_summary"],
                    coverage_count=item["coverage_count"],
                    coverage_digest=item["coverage_digest"],
                    eligible_closed_rows=item["eligible_closed_rows"],
                    version_sets=item["version_sets"],
                )
    return found


def reference_leaves(connection, header: CloseHeader, references, *, parts):
    """Batch source locations and reuse checked blocks within a caller's snapshot.

    The caller publishes ``parts`` to its snapshot cache only after every leaf
    comparison succeeds. No result or failure survives a new transaction.
    """
    source_ids = defaultdict(set)
    for path, _, ident in references:
        if path.startswith("adopted_results[*]."):
            table = "calculation" if path.endswith("calculation_id") else "fact_revision"
            source_ids[table].add(ident)
        elif path.startswith("vouchers[*]."):
            source_ids["voucher_version" if path.endswith(".id") else "calculation"].add(ident)
    for table, identifiers in source_ids.items():
        missing = [ident for ident in identifiers if ("subject", table, ident) not in parts]
        if not missing:
            continue
        sql = (
            "SELECT v.id,c.subject_id FROM json_each(?) ids "
            "JOIN voucher_version v ON v.id=ids.value "
            "JOIN calculation c ON c.id=v.calculation_id"
            if table == "voucher_version"
            else f"SELECT f.id,f.subject_id FROM json_each(?) ids JOIN {table} f ON f.id=ids.value"
        )
        for row in connection.execute(sql, (canonical(sorted(missing)),)):
            parts["subject", table, row["id"]] = row["subject_id"]
        if any(("subject", table, ident) not in parts for ident in missing):
            _invalid(header.period, "storage_reference_source_missing")
    return [
        reference_leaf(connection, header, path, index, ident, _parts=parts)
        for path, index, ident in references
    ]


def reference_leaf(
    connection, header: CloseHeader, path: str, index: int, reference_id: str, *, _parts=None
):
    """Read one fixed logical leaf from its committed bucket or complete section."""
    if _parts is None:
        return reference_leaves(connection, header, [(path, index, reference_id)], parts={})[0]
    prefix = (header.period, header.storage_digest)

    def family(name):
        key = (*prefix, "family", name)
        if key not in _parts:
            _parts[key] = _family(connection, header, name)
        return _parts[key]

    if path.startswith("adopted_results[*]."):
        table = "calculation" if path.endswith("calculation_id") else "fact_revision"
        field, bucket = "adopted_results", _bucket(_parts["subject", table, reference_id])
    elif path.startswith("vouchers[*]."):
        table = "voucher_version" if path.endswith(".id") else "calculation"
        field, bucket = "vouchers", _bucket(_parts["subject", table, reference_id])
    elif path == "material_coverage.fact_ids[*]":
        field, bucket = "material_coverage.fact_ids", _bucket(reference_id)
    else:
        field = None
    if field is not None:
        subroot = family("material" if field.startswith("material_") else "accounting")
        key = (*prefix, "bucket", field, bucket)
        if key not in _parts:
            values = _bucket_rows(connection, header, subroot, field, bucket)
            by_position = dict(values)
            if len(by_position) != len(values):
                _invalid(header.period, "storage_reference_position_missing")
            _parts[key] = by_position
        if index not in _parts[key]:
            _invalid(header.period, "storage_reference_position_missing")
        item = _parts[key][index]
        if field == "material_coverage.fact_ids":
            if _bucket(item) != bucket:
                _invalid(header.period, "storage_bucket_identity_mismatch")
            return item, None
        key = path.rsplit(".", 1)[-1]
        if field == "adopted_results" and _bucket(item["subject_id"]) != bucket:
            _invalid(header.period, "storage_bucket_identity_mismatch")
        related = item["calculation_id"] if path == "vouchers[*].id" else None
        return item.get(key), related
    segments = path.split(".")
    section = segments[0]
    if section == "readiness" and len(segments) > 1:
        checker = segments[1]
        key = (*prefix, "readiness_check", checker)
        if key not in _parts:
            _parts[key] = read_readiness_check(connection, header, checker)
        value = _parts[key]
        segments = segments[2:]
    else:
        key = (*prefix, "section", section)
        if key not in _parts:
            _parts[key] = read_section(connection, header, section)
        value = _parts[key]
        segments = segments[1:]
    for segment in segments:
        if segment.endswith("[*]"):
            value = value.get(segment[:-3]) if isinstance(value, dict) else None
            value = value[index] if isinstance(value, list) and index < len(value) else None
        else:
            value = value.get(segment) if isinstance(value, dict) else None
        if value is None:
            break
    return value, None


def read_section(connection, header: CloseHeader, name: str):
    """Return one authenticated logical field without a caller-supplied cache."""
    return _read_section(connection, header, name)


def _read_section(
    connection, header: CloseHeader, name: str, *, _subroots=None, _adopted_results=None
):
    """Return one *complete* logical v4 field, never a partial manifest dict."""
    if name in ACCOUNTING_FIELDS:
        subroot = _family_once(connection, header, "accounting", _subroots)
        if name == "vouchers":
            adopted = (
                _field_values(connection, header, subroot, "adopted_results")
                if _adopted_results is None
                else _adopted_results
            )
            subjects = {item["calculation_id"]: item["subject_id"] for item in adopted}
            values = []
            for bucket, _, _ in subroot["directories"]["vouchers"]:
                for index, item in _bucket_rows(
                    connection, header, subroot, "vouchers", bucket
                ):
                    if _bucket(subjects[item["adopted_calculation_id"]]) != bucket:
                        _invalid(header.period, "storage_bucket_identity_mismatch")
                    values.append((index, item))
            return [item for _, item in sorted(values)]
        return _field_values(connection, header, subroot, name)
    if name == "material_coverage":
        subroot = _family_once(connection, header, "material", _subroots)
        result = copy.deepcopy(subroot["small"])
        for field in MATERIAL_FIELDS:
            _set(
                {"material_coverage": result},
                field,
                _field_values(connection, header, subroot, field),
            )
        return result
    if name in ("management_snapshot", "readiness"):
        subroot = _management_subroot(connection, header, _subroots=_subroots)
        if name == "management_snapshot":
            return _management_value(connection, header, subroot, name)
        return {
            section[len("readiness:") :]: _management_value(
                connection, header, subroot, section
            )
            for section in sorted(subroot["directories"])
            if section.startswith("readiness:")
        }
    if name == "owner_review":
        subroot = _family_once(connection, header, "review", _subroots)
        result = copy.deepcopy(subroot["owner_review"])
        for section in result["collections"]:
            field = "owner_review_keys:" + section["section"]
            for block in section["blocks"]:
                values = _bucket_rows(connection, header, subroot, field, block["index"])
                if len(values) != 1:
                    _invalid(header.period, "storage_review_keys_missing")
                block["keys"] = values[0]
        return result
    if name in header.root["small"]:
        return copy.deepcopy(header.root["small"][name])
    raise ValueError(f"unsupported close section: {name}")


def decode_close(connection, row, *, require_marker=True) -> dict:
    """Fully reconstruct and verify the logical v4 object and entire directory."""
    header = verified_header(connection, row, require_marker=require_marker)
    manifest = copy.deepcopy(header.root["small"])
    subroots = {}
    for name in (
        "adopted_results", "vouchers", "material_coverage", "management_snapshot",
        "readiness", "owner_review",
    ):
        manifest[name] = _read_section(
            connection,
            header,
            name,
            _subroots=subroots,
            _adopted_results=manifest.get("adopted_results"),
        )
    accounting_subroot = _family_once(connection, header, "accounting", subroots)
    _accounting_subject_filter(accounting_subroot, header.period)
    expected_filter = build_keys_filter(
        item["subject_id"] for item in manifest["adopted_results"]
    )
    if accounting_subroot["subject_filter"] != expected_filter:
        _invalid(header.period, "storage_accounting_filter_mismatch")
    expected_directories = set()
    expected_blocks = set()
    directory_rows = {
        (item["field"], item["bucket"]): item
        for item in connection.execute(
            "SELECT field,bucket,content FROM close_storage_directory WHERE period=?",
            (header.period,),
        )
    }
    for family in FAMILIES:
        subroot = _family_once(connection, header, family, subroots)
        for field, descriptors in subroot.get("directories", {}).items():
            for bucket, _, _ in descriptors:
                expected_directories.add((field, bucket))
                row_directory = directory_rows.get((field, bucket))
                if row_directory is None:
                    _invalid(header.period, "storage_directory_missing")
                expected_blocks.update(
                    (field, bucket, part)
                    for part, _, _ in json.loads(row_directory["content"])
                )
    actual_directories = set(directory_rows)
    actual_blocks = {
        (item[0], item[1], item[2]) for item in connection.execute(
            "SELECT field,bucket,part FROM close_storage_block WHERE period=?",
            (header.period,),
        )
    }
    if actual_directories != expected_directories or actual_blocks != expected_blocks:
        _invalid(header.period, "storage_directory_multiset_mismatch")
    if _sha(canonical(manifest)) != header.logical_digest:
        _invalid(header.period, "logical_manifest_digest_mismatch")
    if _preview_digest(manifest) != header.root["preview_digest"]:
        _invalid(header.period, "storage_preview_digest_mismatch")
    material_subroot = _family_once(connection, header, "material", subroots)
    stored_summaries = _field_values(
        connection, header, material_subroot, MATERIAL_SUMMARIES_FIELD
    )
    if canonical(stored_summaries) != canonical(_material_summaries(connection, manifest)):
        _invalid(header.period, "storage_material_summary_mismatch")
    return require_close_contract(manifest)
