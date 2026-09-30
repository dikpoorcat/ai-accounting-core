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
from collections import ChainMap, defaultdict
from dataclasses import dataclass
from typing import Any

from .close_contract import require_close_contract
from .contracts import KernelError
from .key_membership_filter import (
    DecodedKeysFilter,
    _positions,
    build_keys_filter,
    decode_keys_filter,
    may_contain,
)
from .types import YearMonth, canonical

STORAGE_FORMAT = "ai-accounting-kernel/2/close-storage/3"
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

# Root integration imports this constant into the one company DDL bundle. The
# period_close table retains its three fields; manifest contains private root
# JSON while digest remains the SHA of the decoded logical v4 manifest.
CLOSE_STORAGE_DDL = """
CREATE TABLE close_storage_root(period INTEGER PRIMARY KEY REFERENCES period_close(period)
 DEFERRABLE INITIALLY DEFERRED,storage_digest BLOB NOT NULL
 CHECK(length(storage_digest)=32)) STRICT;
CREATE TABLE close_storage_subroot(period INTEGER NOT NULL REFERENCES period_close(period)
 DEFERRABLE INITIALLY DEFERRED,family TEXT NOT NULL
 CHECK(family IN('accounting','material','management','review')),
 content TEXT NOT NULL CHECK(json_valid(content)),digest BLOB NOT NULL CHECK(length(digest)=32),
 PRIMARY KEY(period,family)) STRICT;
CREATE TABLE close_storage_directory(period INTEGER NOT NULL REFERENCES period_close(period)
 DEFERRABLE INITIALLY DEFERRED,field TEXT NOT NULL,bucket INTEGER NOT NULL CHECK(bucket>=0),
 content TEXT NOT NULL CHECK(json_valid(content)),digest BLOB NOT NULL CHECK(length(digest)=32),
 PRIMARY KEY(period,field,bucket)) STRICT;
CREATE TABLE close_storage_block(period INTEGER NOT NULL REFERENCES period_close(period)
 DEFERRABLE INITIALLY DEFERRED,field TEXT NOT NULL,bucket INTEGER NOT NULL CHECK(bucket>=0),
 part INTEGER NOT NULL CHECK(part>=0),content TEXT NOT NULL CHECK(json_valid(content)),
 digest BLOB NOT NULL CHECK(length(digest)=32),PRIMARY KEY(period,field,bucket,part)) STRICT;
"""


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


_ACCOUNTING_BATCH_KEY = object()


@dataclass(frozen=True)
class _AccountingAuthorityBatch:
    key: object
    connection: object
    subjects: frozenset[str]
    roots: dict[int, tuple[bytes, int]]
    publications: dict[int, dict[str, tuple[str, str]]]
    vouchers: dict[int, set[str]]

    def for_close(self, connection, header, subjects):
        if (
            self.key is not _ACCOUNTING_BATCH_KEY
            or self.connection is not connection
            or self.subjects != subjects
            or self.roots.get(header.period)
            != (header.storage_digest, header.root["small"]["publication_sequence"])
        ):
            _invalid(header.period, "storage_accounting_authority_scope_mismatch")
        return self.publications[header.period], self.vouchers[header.period]


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


def _preview_digest(manifest: dict) -> str:
    from .close_review import reviewed_preview_digest

    return reviewed_preview_digest(manifest)


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


def _rows_by_bucket(manifest, field):
    adopted_subjects = {
        item["calculation_id"]: item["subject_id"] for item in manifest["adopted_results"]
    }
    groups = defaultdict(list)
    for index, value in enumerate(_path(manifest, field)):
        groups[_bucket(_key(field, value, adopted_subjects))].append([index, value])
    return groups


def _accounting_subject_filter(subroot, period):
    """Use a committed negative filter only after its accounting root is checked."""
    if not isinstance(subroot, dict) or set(subroot) != {"directories", "subject_filter"}:
        _invalid(period, "storage_accounting_filter_invalid")
    try:
        return decode_keys_filter(subroot["subject_filter"])
    except ValueError:
        _invalid(period, "storage_accounting_filter_invalid")


def _write_field(connection, period, field, groups):
    descriptors = []
    for bucket, entries in sorted(groups.items()):
        parts = []
        for part, offset in enumerate(range(0, len(entries), BLOCK_SIZE)):
            rows = entries[offset : offset + BLOCK_SIZE]
            content = canonical(rows)
            hashed = _sha(content)
            connection.execute(
                "INSERT INTO close_storage_block VALUES(?,?,?,?,?,?)",
                (period, field, bucket, part, content, hashed),
            )
            parts.append([part, hashed.hex(), len(rows)])
        directory = canonical(parts)
        hashed = _sha(directory)
        connection.execute(
            "INSERT INTO close_storage_directory VALUES(?,?,?,?,?)",
            (period, field, bucket, directory, hashed),
        )
        descriptors.append([bucket, hashed.hex(), len(entries)])
    return descriptors


def _material_summaries(connection, logical_manifest):
    """Derive compact per-source facts from the complete logical material proof."""
    from .frozen_material import _KINDS, _PROOF_FIELDS, _frozen_versions

    proof = logical_manifest["material_coverage"]
    coverage = defaultdict(list)
    for item in proof["coverage"]:
        coverage[item["source_id"]].append(item)
    files = {item["source_id"]: item for item in proof["file_summaries"]}
    versions = {
        label: _frozen_versions(connection, kind, table, proof[_PROOF_FIELDS[label]])
        for label, (kind, table) in _KINDS.items()
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


def write_close(
    connection,
    period: int,
    logical_manifest: dict,
    *,
    projection_roots: dict[str, bytes] | None = None,
) -> bytes:
    """Write one close and all private blocks inside the caller's close transaction."""
    require_close_contract(logical_manifest)
    if logical_manifest["period"] != str(YearMonth.from_ordinal(period)):
        _invalid(period, "storage_period_mismatch")
    logical_digest = _sha(canonical(logical_manifest))
    projection_roots = projection_roots or {}
    if any(
        not isinstance(name, str) or not name or not isinstance(value, bytes) or len(value) != 32
        for name, value in projection_roots.items()
    ):
        raise ValueError("derived close roots must be named 32-byte digests")
    small = copy.deepcopy(logical_manifest)
    directories = {}
    for field in ALL_FIELDS:
        groups = _rows_by_bucket(logical_manifest, field)
        _set(small, field, [])
        directories[field] = _write_field(connection, period, field, groups)
    source_summaries = _material_summaries(connection, logical_manifest)
    summary_groups = defaultdict(list)
    for index, value in enumerate(source_summaries):
        summary_groups[_bucket(value["source_id"])].append([index, value])
    directories[MATERIAL_SUMMARIES_FIELD] = _write_field(
        connection, period, MATERIAL_SUMMARIES_FIELD, summary_groups
    )
    for section in small["owner_review"]["collections"]:
        field = "owner_review_keys:" + section["section"]
        directories[field] = []
        for block in section["blocks"]:
            index = block["index"]
            keys = block.pop("keys")
            content = canonical(keys)
            hashed = _sha(content)
            connection.execute(
                "INSERT INTO close_storage_block VALUES(?,?,?,?,?,?)",
                (period, field, index, 0, content, hashed),
            )
            directory = canonical([[0, hashed.hex(), len(keys)]])
            directory_digest = _sha(directory)
            connection.execute(
                "INSERT INTO close_storage_directory VALUES(?,?,?,?,?)",
                (period, field, index, directory, directory_digest),
            )
            directories[field].append([index, directory_digest.hex(), len(keys)])

    readiness = small.pop("readiness")
    if any(type(name) is not str or not name for name in readiness):
        _invalid(period, "storage_management_contract_invalid")
    management_sections = {"management_snapshot": small.pop("management_snapshot")}
    management_sections.update(
        {"readiness:" + name: value for name, value in readiness.items()}
    )
    management_directories = {
        name: _write_field(connection, period, name, {0: [[0, value]]})
        for name, value in sorted(management_sections.items())
    }

    families = {
        "accounting": {
            "directories": {key: directories[key] for key in ACCOUNTING_FIELDS},
            "subject_filter": build_keys_filter(
                item["subject_id"] for item in logical_manifest["adopted_results"]
            ),
        },
        "material": {
            "small": small.pop("material_coverage"),
            "directories": {
                key: directories[key] for key in (*MATERIAL_FIELDS, MATERIAL_SUMMARIES_FIELD)
            },
        },
        "management": {"directories": management_directories},
        "review": {
            "owner_review": small.pop("owner_review"),
            "directories": {
                key: value
                for key, value in directories.items()
                if key.startswith("owner_review_keys:")
            },
        },
    }
    family_digests = {}
    for family, value in families.items():
        content = canonical(value)
        hashed = _sha(content)
        connection.execute(
            "INSERT INTO close_storage_subroot VALUES(?,?,?,?)",
            (period, family, content, hashed),
        )
        family_digests[family] = hashed.hex()
    root = {
        "encoding": STORAGE_FORMAT,
        "period": period,
        "company_id": logical_manifest["company_id"],
        "database_id": logical_manifest["database_id"],
        "logical_digest": logical_digest.hex(),
        "preview_digest": _preview_digest(logical_manifest),
        "small": small,
        "subroots": family_digests,
        "derived_roots": {name: value.hex() for name, value in sorted(projection_roots.items())},
    }
    from .change_journal import CONTRACT as SOURCE_CHANGE_CONTRACT
    from .change_journal import head as source_change_head

    root["source_changes"] = {
        "contract": SOURCE_CHANGE_CONTRACT,
        "highwater": source_change_head(connection),
    }
    root_text = canonical(root)
    connection.execute(
        "INSERT INTO period_close(period,manifest,digest) VALUES(?,?,?)",
        (period, root_text, logical_digest),
    )
    connection.execute(
        "INSERT INTO close_storage_root(period,storage_digest) VALUES(?,?)",
        (period, _sha(root_text)),
    )
    return logical_digest


def verified_header(connection, row, *, require_marker=True) -> CloseHeader:
    """Verify root SHA, logical digest, marker, and actual company identity."""
    from .change_journal import CONTRACT as SOURCE_CHANGE_CONTRACT

    period = row["period"]
    storage = connection.execute(
        "SELECT storage_digest FROM close_storage_root WHERE period=?", (period,)
    ).fetchone()
    if storage is None or _sha(row["manifest"]) != bytes(storage[0]):
        _invalid(period, "storage_root_digest_mismatch")
    if require_marker:
        marker = connection.execute(
            "SELECT source_digest FROM read_index_source WHERE source_kind='close' AND source_id=?",
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
            or set(root)
            != {
                "encoding",
                "period",
                "company_id",
                "database_id",
                "logical_digest",
                "preview_digest",
                "small",
                "subroots",
                "derived_roots",
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
    if (
        row is None
        or _sha(row["content"]) != bytes(row["digest"])
        or (bytes(row["digest"]).hex() != expected)
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


def _bucket_rows(connection, header, subroot, field, bucket, *, _stored=None):
    descriptors = subroot["directories"][field]
    expected = next((entry for entry in descriptors if entry[0] == bucket), None)
    if expected is None:
        return []
    directory = (
        connection.execute(
            "SELECT content,digest FROM close_storage_directory "
            "WHERE period=? AND field=? AND bucket=?",
            (header.period, field, bucket),
        ).fetchone()
        if _stored is None
        else _stored[0]
    )
    if (
        directory is None
        or _sha(directory["content"]) != bytes(directory["digest"])
        or (bytes(directory["digest"]).hex() != expected[1])
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
    actual_parts = (
        connection.execute(
            "SELECT count(*) FROM close_storage_block WHERE period=? AND field=? AND bucket=?",
            (header.period, field, bucket),
        ).fetchone()[0]
        if _stored is None
        else len(_stored[1])
    )
    if actual_parts != len(parts):
        _invalid(header.period, "storage_block_multiset_mismatch")
    entries = []
    for part, hashed, count in parts:
        row = (
            connection.execute(
                "SELECT content,digest FROM close_storage_block "
                "WHERE period=? AND field=? AND bucket=? AND part=?",
                (header.period, field, bucket, part),
            ).fetchone()
            if _stored is None
            else _stored[1].get(part)
        )
        if (
            row is None
            or _sha(row["content"]) != bytes(row["digest"])
            or (bytes(row["digest"]).hex() != hashed)
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


def _buckets_rows(connection, header, subroot, field, buckets):
    """Read the requested physical blocks in two queries, with the same checks."""
    buckets = sorted(set(buckets))
    if not buckets:
        return {}
    present = {entry[0] for entry in subroot["directories"][field]}
    wanted = [bucket for bucket in buckets if bucket in present]
    if not wanted:
        return {bucket: [] for bucket in buckets}
    parameters = (canonical(wanted), header.period, field)
    directories = {
        row["bucket"]: row
        for row in connection.execute(
            "SELECT d.bucket,d.content,d.digest FROM json_each(?) requested "
            "CROSS JOIN close_storage_directory d ON d.bucket=requested.value "
            "AND d.period=? AND d.field=?",
            parameters,
        )
    }
    blocks = defaultdict(dict)
    for row in connection.execute(
        "SELECT b.bucket,b.part,b.content,b.digest FROM json_each(?) requested "
        "CROSS JOIN close_storage_block b ON b.bucket=requested.value "
        "AND b.period=? AND b.field=?",
        parameters,
    ):
        blocks[row["bucket"]][row["part"]] = row
    return {
        bucket: _bucket_rows(
            connection,
            header,
            subroot,
            field,
            bucket,
            _stored=(directories.get(bucket), blocks.get(bucket, {})),
        )
        for bucket in buckets
    }


def _prime_buckets(connection, header, subroot, field, buckets, parts):
    prefix = (header.period, header.storage_digest, "bucket", field)
    missing = [bucket for bucket in buckets if (*prefix, bucket) not in parts]
    for bucket, values in _buckets_rows(connection, header, subroot, field, missing).items():
        by_position = dict(values)
        if len(by_position) != len(values):
            _invalid(header.period, "storage_reference_position_missing")
        parts[*prefix, bucket] = by_position


def _field_values(connection, header, subroot, field):
    values = []
    buckets = [item[0] for item in subroot["directories"][field]]
    for bucket, entries in _buckets_rows(connection, header, subroot, field, buckets).items():
        for index, value in entries:
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
    """Read one root-committed readiness checker without decoding its siblings."""
    if type(name) is not str or not name:
        raise ValueError("readiness checker name must be nonempty")
    subroot = _management_subroot(connection, header)
    section = "readiness:" + name
    if section not in subroot["directories"]:
        return {}
    return _management_value(connection, header, subroot, section)


def _may_contain_with_positions(value, key, positions_cache):
    """Reuse only pure key positions; each authenticated filter still checks its bits."""
    if not isinstance(value, DecodedKeysFilter):
        raise ValueError("decoded filter required")
    cache_key = key, value.bit_count
    positions = positions_cache.get(cache_key)
    if positions is None:
        positions = positions_cache[cache_key] = tuple(_positions(key, value.bit_count))
    return all(
        value.bits[position >> 3] & (1 << (position & 7)) for position in positions
    )


def read_accounting(
    connection, header: CloseHeader, subjects, *, _verified_parts=None, _positions_cache=None,
    _authority: _AccountingAuthorityBatch | None = None,
) -> CloseAccountingSlice:
    """Read only committed subject buckets; publication rows decide existence."""
    subjects = frozenset(subjects)
    if not subjects:
        return CloseAccountingSlice(header.period, header.logical_digest, subjects, (), ())
    # Keep newly verified blocks private until the entire slice succeeds. A
    # full copy of the snapshot cache here grows quadratically across closes.
    fresh = {}
    parts = ChainMap(fresh, _verified_parts) if _verified_parts is not None else fresh
    fresh_positions = {}
    positions = (
        ChainMap(fresh_positions, _positions_cache)
        if _positions_cache is not None else None
    )
    prefix = (header.period, header.storage_digest)
    family_key = (*prefix, "family", "accounting")
    if family_key not in parts:
        parts[family_key] = _family(connection, header, "accounting")
    subroot = parts[family_key]
    filter_key = (*prefix, "accounting_subject_filter")
    if filter_key not in parts:
        parts[filter_key] = _accounting_subject_filter(subroot, header.period)
    subject_filter = parts[filter_key]
    adopted, vouchers = [], []
    try:
        subject_buckets = {
            _bucket(subject)
            for subject in subjects
            if (
                may_contain(subject_filter, subject)
                if positions is None
                else _may_contain_with_positions(subject_filter, subject, positions)
            )
        }
    except ValueError:
        _invalid(header.period, "storage_accounting_filter_invalid")
    for field in ("adopted_results", "vouchers"):
        _prime_buckets(connection, header, subroot, field, subject_buckets, parts)
    for bucket in sorted(subject_buckets):
        for field, target in (("adopted_results", adopted), ("vouchers", vouchers)):
            key = (*prefix, "bucket", field, bucket)
            target.extend(parts[key].items())
    selected = [(position, item) for position, item in adopted if item["subject_id"] in subjects]
    adopted_by_id = {item["calculation_id"]: item for _, item in selected}
    chosen_vouchers = [
        (position, item)
        for position, item in vouchers
        if item["adopted_calculation_id"] in adopted_by_id
    ]
    for _, item in selected:
        if _bucket(item["subject_id"]) not in subject_buckets:
            _invalid(header.period, "storage_bucket_identity_mismatch")
        if item["posting_period"] != str(YearMonth.from_ordinal(header.period)):
            _invalid(header.period, "storage_adoption_period_mismatch")
    # A missing reverse-directory row cannot conceal an adopted publication.
    if _authority is None:
        expected = {
            row["subject_id"]: (row["id"], row["calculation_id"])
            for row in connection.execute(
                "SELECT p.subject_id,p.id,p.calculation_id FROM json_each(?) requested "
                "CROSS JOIN calculation_publication p INDEXED BY publication_subject "
                "ON p.subject_id=requested.value "
                "WHERE p.posting_period=? AND p.sequence<=? AND p.calculation_id IS NOT NULL "
                "AND NOT EXISTS(SELECT 1 FROM calculation_publication later "
                "WHERE later.previous_publication_id=p.id AND later.sequence<=?)",
                (
                    canonical(sorted(subjects)),
                    header.period,
                    header.root["small"]["publication_sequence"],
                    header.root["small"]["publication_sequence"],
                ),
            )
        }
    else:
        expected, _ = _authority.for_close(connection, header, subjects)
    actual = {
        item["subject_id"]: (item["publication_id"], item["calculation_id"]) for _, item in selected
    }
    if actual != expected:
        _invalid(header.period, "storage_adoption_publication_mismatch")
    if _authority is None:
        expected_vouchers = {
            row["id"]
            for row in connection.execute(
                "SELECT v.id FROM json_each(?) requested "
                "CROSS JOIN calculation c INDEXED BY calculation_subject "
                "ON c.subject_id=requested.value "
                "CROSS JOIN voucher_version v INDEXED BY voucher_calculation "
                "ON v.calculation_id=c.id "
                "CROSS JOIN voucher_current h ON h.version_id=v.id "
                "WHERE v.period=?",
                (canonical(sorted(subjects)), header.period),
            )
        }
    else:
        _, expected_vouchers = _authority.for_close(connection, header, subjects)
    if {item["id"] for _, item in chosen_vouchers} != expected_vouchers:
        _invalid(header.period, "storage_voucher_source_mismatch")
    if _verified_parts is not None:
        _verified_parts.update(fresh)
    if _positions_cache is not None:
        _positions_cache.update(fresh_positions)
    return CloseAccountingSlice(
        header.period,
        header.logical_digest,
        subjects,
        tuple(item for _, item in sorted(selected)),
        tuple(item for _, item in sorted(chosen_vouchers)),
    )


def _accounting_authority(connection, headers, subjects) -> _AccountingAuthorityBatch:
    """Select the exact publication and voucher authority for one close group."""
    if len({header.period for header in headers}) != len(headers):
        _invalid(headers[0].period, "storage_accounting_authority_period_duplicate")
    roots = {}
    limits = []
    for header in headers:
        highwater = header.root["small"]["publication_sequence"]
        if type(header.period) is not int or type(highwater) is not int:
            _invalid(header.period, "storage_accounting_authority_limit_invalid")
        roots[header.period] = (header.storage_digest, highwater)
        limits.append([header.period, highwater])
    encoded_subjects = canonical(sorted(subjects))
    publications = {header.period: {} for header in headers}
    for row in connection.execute(
        "WITH limits(period,highwater) AS MATERIALIZED ("
        "SELECT CAST(json_extract(value,'$[0]') AS INTEGER),"
        "CAST(json_extract(value,'$[1]') AS INTEGER) FROM json_each(?)) "
        "SELECT p.posting_period,p.subject_id,p.id,p.calculation_id "
        "FROM json_each(?) requested "
        "CROSS JOIN calculation_publication p INDEXED BY publication_subject "
        "ON p.subject_id=requested.value JOIN limits l ON l.period=p.posting_period "
        "WHERE p.sequence<=l.highwater AND p.calculation_id IS NOT NULL "
        "AND NOT EXISTS(SELECT 1 FROM calculation_publication later "
        "WHERE later.previous_publication_id=p.id AND later.sequence<=l.highwater)",
        (canonical(limits), encoded_subjects),
    ):
        expected = publications[row["posting_period"]]
        if row["subject_id"] in expected:
            _invalid(row["posting_period"], "storage_adoption_publication_mismatch")
        expected[row["subject_id"]] = (row["id"], row["calculation_id"])
    vouchers = {header.period: set() for header in headers}
    for row in connection.execute(
        "WITH wanted(period) AS MATERIALIZED ("
        "SELECT CAST(value AS INTEGER) FROM json_each(?)) "
        "SELECT v.period,v.id FROM json_each(?) requested "
        "CROSS JOIN calculation c INDEXED BY calculation_subject "
        "ON c.subject_id=requested.value "
        "CROSS JOIN voucher_version v INDEXED BY voucher_calculation "
        "ON v.calculation_id=c.id "
        "CROSS JOIN voucher_current h ON h.version_id=v.id "
        "JOIN wanted w ON w.period=v.period",
        (canonical([header.period for header in headers]), encoded_subjects),
    ):
        vouchers[row["period"]].add(row["id"])
    return _AccountingAuthorityBatch(
        _ACCOUNTING_BATCH_KEY, connection, subjects, roots, publications, vouchers
    )


def read_accounting_many(
    connection, headers, subjects, *, _verified_parts=None, _positions_cache=None
) -> tuple[CloseAccountingSlice, ...]:
    """Verify one scoped group while sharing only its two authority selections."""
    headers = tuple(headers)
    subjects = frozenset(subjects)
    if not headers:
        return ()
    if not subjects:
        return tuple(
            CloseAccountingSlice(header.period, header.logical_digest, subjects, (), ())
            for header in headers
        )
    authority = _accounting_authority(connection, headers, subjects)
    # ChainMap writes into these new maps, while successfully read prior blocks
    # remain available. Publish nothing until every month has passed its normal
    # read_accounting comparisons.
    new_parts = {}
    new_positions = {}
    parts = ChainMap(new_parts, _verified_parts) if _verified_parts is not None else new_parts
    positions = (
        ChainMap(new_positions, _positions_cache)
        if _positions_cache is not None else new_positions
    )
    result = tuple(
        read_accounting(
            connection, header, subjects, _verified_parts=parts,
            _positions_cache=positions, _authority=authority,
        )
        for header in headers
    )
    if _verified_parts is not None:
        _verified_parts.update(new_parts)
    if _positions_cache is not None:
        _positions_cache.update(new_positions)
    return result


def read_material_sources(connection, header: CloseHeader, source_ids) -> CloseMaterialSlice:
    """Read bounded material coverage and summary buckets for named sources."""
    source_ids = frozenset(source_ids)
    if not source_ids:
        return CloseMaterialSlice(header.period, header.logical_digest, source_ids, (), ())
    subroot = _family(connection, header, "material")
    result = {}
    for field in ("material_coverage.coverage", "material_coverage.file_summaries"):
        entries = []
        buckets = {_bucket(source_id) for source_id in source_ids}
        for bucket, values in _buckets_rows(connection, header, subroot, field, buckets).items():
            for position, item in values:
                if _bucket(item["source_id"]) != bucket:
                    _invalid(header.period, "storage_bucket_identity_mismatch")
                if item["source_id"] in source_ids:
                    entries.append((position, item))
        result[field] = tuple(item for _, item in sorted(entries))
    return CloseMaterialSlice(
        header.period,
        header.logical_digest,
        source_ids,
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
    buckets = {_bucket(source_id) for source_id in source_ids}
    groups = _buckets_rows(connection, header, subroot, MATERIAL_SUMMARIES_FIELD, buckets)
    for bucket, values in groups.items():
        for _, item in values:
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
    requested = defaultdict(set)
    for path, _, ident in references:
        if path.startswith("adopted_results[*]."):
            table = "calculation" if path.endswith("calculation_id") else "fact_revision"
            requested["adopted_results"].add(_bucket(parts["subject", table, ident]))
        elif path.startswith("vouchers[*]."):
            table = "voucher_version" if path.endswith(".id") else "calculation"
            requested["vouchers"].add(_bucket(parts["subject", table, ident]))
        elif path == "material_coverage.fact_ids[*]":
            requested["material_coverage.fact_ids"].add(_bucket(ident))
    prefix = (header.period, header.storage_digest)
    for field, buckets in requested.items():
        family = "material" if field.startswith("material_") else "accounting"
        family_key = (*prefix, "family", family)
        if family_key not in parts:
            parts[family_key] = _family(connection, header, family)
        _prime_buckets(connection, header, parts[family_key], field, buckets, parts)
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
            buckets = [item[0] for item in subroot["directories"]["vouchers"]]
            groups = _buckets_rows(connection, header, subroot, "vouchers", buckets)
            for bucket, entries in groups.items():
                for index, item in entries:
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
            groups = _buckets_rows(
                connection, header, subroot, field, [block["index"] for block in section["blocks"]]
            )
            for block in section["blocks"]:
                values = groups[block["index"]]
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
        "adopted_results",
        "vouchers",
        "material_coverage",
        "management_snapshot",
        "readiness",
        "owner_review",
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
                    (field, bucket, part) for part, _, _ in json.loads(row_directory["content"])
                )
    actual_directories = set(directory_rows)
    actual_blocks = {
        (item[0], item[1], item[2])
        for item in connection.execute(
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
    stored_summaries = _field_values(connection, header, material_subroot, MATERIAL_SUMMARIES_FIELD)
    if canonical(stored_summaries) != canonical(_material_summaries(connection, manifest)):
        _invalid(header.period, "storage_material_summary_mismatch")
    return require_close_contract(manifest)
