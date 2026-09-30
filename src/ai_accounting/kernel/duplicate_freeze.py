"""Private, close-bound directory of possible duplicate signal keys.

The directory is a read accelerator. Its root commits every occupied hash
bucket, including the absence of a key in a verified bucket. Exact duplicate
decisions continue to use the current facts, locations, and review records.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, StrictStr, TypeAdapter

from .contracts import FactVersion, KernelError
from .key_membership_filter import (
    DecodedKeysFilter,
    build_keys_filter,
    decode_keys_filter,
    may_contain,
)
from .storage import _validation_json
from .types import canonical, digest

DERIVED_ROOT_NAME = "duplicate"
FORMAT = "ai-accounting-kernel/2/duplicate-freeze/2"
BUCKET_COUNT = 65536
_DIRECTORY_ENTRIES = TypeAdapter(
    dict[
        StrictStr,
        Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)],
    ]
)


def verify_duplicate_checks(connection) -> None:
    from .duplicates import verify_duplicate_checks as verify_current_checks

    verify_current_checks(connection)


DUPLICATE_FREEZE_DDL = """
CREATE TABLE duplicate_freeze_root(
 period INTEGER PRIMARY KEY REFERENCES period_close(period) DEFERRABLE INITIALLY DEFERRED,
 close_digest BLOB NOT NULL CHECK(length(close_digest)=32),
 journal_highwater INTEGER NOT NULL CHECK(journal_highwater>=0),
 payload TEXT NOT NULL CHECK(json_valid(payload))
  CHECK(json_extract(payload,'$.format') IS 'ai-accounting-kernel/2/duplicate-freeze/2')
  CHECK(json_type(payload,'$.key_filter') IS 'object'),
 digest BLOB NOT NULL CHECK(length(digest)=32)
) STRICT;
CREATE TABLE duplicate_freeze_bucket(
 digest BLOB PRIMARY KEY CHECK(length(digest)=32),
 content TEXT NOT NULL CHECK(json_valid(content))
) STRICT;
CREATE TABLE duplicate_freeze_directory(
 digest BLOB PRIMARY KEY CHECK(length(digest)=32),
 content TEXT NOT NULL CHECK(json_valid(content))
) STRICT;
"""


@dataclass(frozen=True)
class PreparedDuplicateFreeze:
    period: int
    close_digest: bytes
    journal_highwater: int
    payload: str
    root_digest: bytes
    directories: tuple[tuple[bytes, str], ...]
    buckets: tuple[tuple[bytes, str], ...]


@dataclass(frozen=True)
class VerifiedDuplicateRoot:
    period: int
    close_digest: bytes
    journal_highwater: int
    directories: dict[str, str]
    key_filter: DecodedKeysFilter


def _invalid(period: int, reason: str) -> None:
    raise KernelError(
        "content_integrity_failed",
        "疑似重复冻结目录与关账来源不一致",
        component="duplicate_freeze",
        record_id=str(period),
        reason=reason,
    )


def _bucket(key: str) -> str:
    raw = hashlib.sha256(key.encode("utf-8")).digest()
    return str((raw[0] << 8) | raw[1])


def _directory(number: str) -> str:
    return str(int(number) >> 8)


def _entry(subject_id: str, fact_id: str, period: int) -> tuple[str, str, int]:
    return subject_id, fact_id, period


def _signals(version, locations):
    """Necessary keys only; the ordinary reducer still decides exact strength."""
    return {canonical(parts) for parts in _signal_parts(version, locations)}


def _signal_parts(version, locations):
    """Keep key coordinates available until the caller selects its lookup range."""
    from .duplicates import ACTUAL_MONEY_KINDS, ORIGIN_KINDS, _actual_money_legs, _signature

    fact = version.fact
    kind = fact.kind
    period = fact.period.ordinal
    if kind in ORIGIN_KINDS:
        signature = _signature(fact)
        for evidence in version.evidence:
            yield ["origin", kind, period, signature, evidence]
    role = "actual_money" if kind in ACTUAL_MONEY_KINDS else kind
    for item in locations:
        if item.get("evidence_digest") is not None and item.get("location") is not None:
            yield ["location", role, item["evidence_digest"], item["location"]]
    if kind in ACTUAL_MONEY_KINDS:
        for leg in _actual_money_legs(fact):
            coordinates = [
                leg[field]
                for field in ("category", "account_id", "actual_date", "direction", "amount_fen")
            ]
            if leg["object_id"] is not None:
                yield ["money_object", kind, *coordinates, leg["object_id"]]
            for movement in leg["movement_ids"]:
                yield ["money_movement", *coordinates, movement]
    dependencies = {
        item["source_id"] for item in locations if isinstance(item.get("source_id"), str)
    }
    dependencies.update(
        item["resolution_subject_id"]
        for item in locations
        if isinstance(item.get("resolution_subject_id"), str)
    )
    for source_id in dependencies:
        yield ["dependency", source_id]


class _Authority:
    """One historical selection reconstructed from immutable journal events."""

    def __init__(self, connection, store):
        self.connection = connection
        self.store = store
        self._has_sources = "material_source_v2" in store.registry.models
        self._has_resolutions = "material_resolution_v2" in store.registry.models
        self._resolution_subjects = (
            {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM subject WHERE kind='material_resolution_v2'"
                )
            }
            if self._has_resolutions
            else set()
        )
        self.facts: dict[str, str] = {}
        self.check_ids: set[str] = set()
        self.identity_ids: set[str] = set()
        self._versions = {}
        self._source_evidence = {}
        self._checks = {}
        self._identities = {}
        self._resolutions = {}

    def version(self, fact_id):
        if fact_id not in self._versions:
            self._versions[fact_id] = self.store.fact(self.connection, fact_id)
        return self._versions[fact_id]

    def prime_versions(self, fact_ids):
        missing = set(fact_ids) - self._versions.keys()
        if missing:
            self._versions.update(self.store.facts(self.connection, missing))

    def source_evidence(self, fact_id):
        if fact_id not in self._source_evidence:
            row = (
                self.connection.execute(
                    "SELECT evidence_digest FROM fact_material_source_v2 WHERE revision_id=?",
                    (fact_id,),
                ).fetchone()
                if self._has_sources
                else None
            )
            self._source_evidence[fact_id] = row[0] if row is not None else None
        return self._source_evidence[fact_id]

    def check(self, check_id):
        if check_id not in self._checks:
            from .duplicates import _require_check_record

            row = self.connection.execute(
                "SELECT * FROM business_duplicate_check WHERE id=?", (check_id,)
            ).fetchone()
            if row is None:
                raise ValueError("journal duplicate check is missing")
            manifest, _ = _require_check_record(row)
            self._checks[check_id] = row, manifest
        return self._checks[check_id]

    def identity(self, item_id):
        if item_id not in self._identities:
            row = self.connection.execute(
                "SELECT * FROM identity_correction_item WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise ValueError("journal identity item is missing")
            self._identities[item_id] = row
        return self._identities[item_id]

    def resolutions(self, revision_ids):
        missing = set(revision_ids) - self._resolutions.keys()
        if missing and self._has_resolutions:
            payload = canonical(sorted(missing))
            rows = {
                row["revision_id"]: row
                for row in self.connection.execute(
                    "SELECT r.* FROM json_each(?) ids JOIN fact_material_resolution_v2 r "
                    "ON r.revision_id=ids.value",
                    (payload,),
                )
            }
            linked = defaultdict(list)
            for row in self.connection.execute(
                "SELECT l.revision_id,l.fact_id FROM json_each(?) ids "
                "JOIN fact_material_resolution_v2_links l ON l.revision_id=ids.value",
                (payload,),
            ):
                linked[row[0]].append(row[1])
            self._resolutions.update(
                (revision_id, (rows.get(revision_id), tuple(linked[revision_id])))
                for revision_id in missing
            )
        return {
            revision_id: self._resolutions.get(revision_id, (None, ()))
            for revision_id in revision_ids
        }

    def apply(self, change):
        if change.source == "fact":
            if self.facts.get(change.target_id) != change.before_ref:
                _invalid(-1, "journal_fact_chain_mismatch")
            if change.after_ref is None:
                self.facts.pop(change.target_id, None)
            else:
                self.facts[change.target_id] = change.after_ref
        elif change.source == "duplicate":
            self.check_ids.add(change.after_ref)
        elif change.source == "identity":
            self.identity_ids.add(change.after_ref)
        elif change.source not in {
            "calculation",
            "pending",
            "inventory",
            "inventory_item",
            "publication",
        }:
            raise ValueError("unknown authority change source")

    def locations(self, selected):
        from .duplicates import SourceLocation

        locations = {fact_id: [] for fact_id in selected}
        if not locations:
            return locations
        checks = {}
        for ident in self.check_ids:
            row, manifest = self.check(ident)
            if row["result_fact_id"] is not None:
                checks[row["result_fact_id"]] = manifest
        ancestors = {}
        for ident in self.identity_ids:
            item = self.identity(ident)
            if item["action"] == "reassign" and item["after_fact_id"] is not None:
                ancestors[item["after_fact_id"]] = item["before_fact_id"]
        for fact_id in selected:
            ancestor = fact_id
            visited = set()
            while ancestor not in checks and ancestor in ancestors:
                if ancestor in visited:
                    raise ValueError("identity correction cycle")
                visited.add(ancestor)
                ancestor = ancestors[ancestor]
            if ancestor not in checks:
                raise ValueError("current business lacks its registered duplicate check")
            for raw in checks[ancestor]["source_locations"]:
                item = SourceLocation.model_validate(raw).model_dump(mode="json")
                current_source = self.facts.get(item["source_id"])
                current_evidence = (
                    self.source_evidence(current_source) if current_source is not None else None
                )
                if current_evidence is not None:
                    item["source_fact_id"] = current_source
                    item["evidence_digest"] = current_evidence
                locations[fact_id].append(item)
        resolution_ids = {
            revision_id
            for subject_id, revision_id in self.facts.items()
            if subject_id in self._resolution_subjects
        }
        for resolution_id, (row, links) in self.resolutions(resolution_ids).items():
            if row is None or row["treatment"] != "recognize":
                continue
            evidence = self.source_evidence(row["source_fact_id"])
            if evidence is None:
                raise ValueError("recognized material source is missing")
            resolution_subject = self.version(resolution_id).subject_id
            for fact_id in links:
                if fact_id in locations:
                    locations[fact_id].append(
                        {
                            "resolution_fact_id": resolution_id,
                            "resolution_subject_id": resolution_subject,
                            "source_id": row["source_id"],
                            "source_fact_id": row["source_fact_id"],
                            "evidence_digest": evidence,
                            "location": row["location"],
                        }
                    )
        return locations


def _prepare_from_authority(authority, period, close_digest, journal_highwater):
    from .duplicates import ELIGIBLE_KINDS

    selected = {}
    # Non-eligible heads must still be loaded and validated before filtering.
    authority.prime_versions(authority.facts.values())
    for fact_id in authority.facts.values():
        version = authority.version(fact_id)
        if version.fact.kind in ELIGIBLE_KINDS and version.fact.period.ordinal <= period:
            selected[fact_id] = version
    locations = authority.locations(selected)
    grouped = defaultdict(lambda: defaultdict(set))
    for fact_id, version in selected.items():
        value = _entry(version.subject_id, fact_id, version.fact.period.ordinal)
        for key in _signals(version, locations[fact_id]):
            grouped[_bucket(key)][key].add(value)
    buckets = []
    directory_entries = defaultdict(dict)
    for number, entries in sorted(grouped.items(), key=lambda row: int(row[0])):
        content = canonical(
            [
                [key, [list(value) for value in sorted(values)]]
                for key, values in sorted(entries.items())
            ]
        )
        block_digest = hashlib.sha256(content.encode("utf-8")).digest()
        buckets.append((block_digest, content))
        directory_entries[_directory(number)][number] = block_digest.hex()
    directories = []
    root_directories = {}
    for number, entries in sorted(directory_entries.items(), key=lambda row: int(row[0])):
        content = canonical(entries)
        block_digest = hashlib.sha256(content.encode("utf-8")).digest()
        directories.append((block_digest, content))
        root_directories[number] = block_digest.hex()
    root = {
        "format": FORMAT,
        "period": period,
        "close_digest": close_digest.hex(),
        "journal_highwater": journal_highwater,
        "bucket_count": BUCKET_COUNT,
        "directories": root_directories,
        "key_filter": build_keys_filter(
            key for entries in grouped.values() for key in entries
        ),
    }
    payload = canonical(root)
    return PreparedDuplicateFreeze(
        period,
        close_digest,
        journal_highwater,
        payload,
        digest(root),
        tuple(directories),
        tuple(buckets),
    )


def prepare_duplicate_freeze(engine, connection, period, logical_close_digest):
    """Build a close candidate from this transaction's current authoritative facts."""
    from .change_journal import head

    authority = _Authority(connection, engine.store)
    authority.facts = {
        row[0]: row[1] for row in connection.execute("SELECT subject_id,fact_id FROM fact_current")
    }
    authority.check_ids = {
        row[0] for row in connection.execute("SELECT id FROM business_duplicate_check")
    }
    authority.identity_ids = {
        row[0] for row in connection.execute("SELECT id FROM identity_correction_item")
    }
    return _prepare_from_authority(authority, period, logical_close_digest, head(connection))


def persist_duplicate_freeze(connection, prepared: PreparedDuplicateFreeze):
    """Write repairable blocks only after the close commits their root."""
    from .close_storage import derived_root, verified_header

    try:
        declared = json.loads(prepared.payload)
        leaves = defaultdict(dict)
        all_keys = set()
        for block_digest, content in prepared.buckets:
            if hashlib.sha256(content.encode("utf-8")).digest() != block_digest:
                _invalid(prepared.period, "prepared_leaf_digest_mismatch")
            entries = json.loads(content)
            if not isinstance(entries, list) or not entries:
                _invalid(prepared.period, "prepared_leaf_shape_invalid")
            numbers = {_bucket(entry[0]) for entry in entries}
            if len(numbers) != 1:
                _invalid(prepared.period, "prepared_leaf_bucket_mismatch")
            for key, _values in entries:
                if key in all_keys:
                    _invalid(prepared.period, "prepared_leaf_duplicate_key")
                all_keys.add(key)
            number = numbers.pop()
            leaves[_directory(number)][number] = block_digest.hex()
        declared_directories = {}
        for block_digest, content in prepared.directories:
            if hashlib.sha256(content.encode("utf-8")).digest() != block_digest:
                _invalid(prepared.period, "prepared_directory_digest_mismatch")
            entries = json.loads(content)
            if not isinstance(entries, dict) or not entries:
                _invalid(prepared.period, "prepared_directory_shape_invalid")
            numbers = {_directory(number) for number in entries}
            if len(numbers) != 1:
                _invalid(prepared.period, "prepared_directory_bucket_mismatch")
            number = numbers.pop()
            if entries != leaves[number]:
                _invalid(prepared.period, "prepared_directory_leaf_mismatch")
            declared_directories[number] = block_digest.hex()
        if declared["directories"] != declared_directories:
            _invalid(prepared.period, "prepared_root_directory_mismatch")
        decode_keys_filter(declared["key_filter"])
        if declared["key_filter"] != build_keys_filter(all_keys):
            _invalid(prepared.period, "prepared_filter_mismatch")
    except (TypeError, ValueError, KeyError, IndexError):
        _invalid(prepared.period, "prepared_blocks_invalid")
    close = connection.execute(
        "SELECT * FROM period_close WHERE period=?", (prepared.period,)
    ).fetchone()
    if close is None:
        _invalid(prepared.period, "missing_close")
    header = verified_header(connection, close)
    if (
        header.logical_digest != prepared.close_digest
        or header.root["source_changes"]["highwater"] != prepared.journal_highwater
        or derived_root(header, DERIVED_ROOT_NAME) != prepared.root_digest
        or hashlib.sha256(prepared.payload.encode("utf-8")).digest() != prepared.root_digest
    ):
        _invalid(prepared.period, "prepared_root_mismatch")
    for table, blocks in (
        ("duplicate_freeze_directory", prepared.directories),
        ("duplicate_freeze_bucket", prepared.buckets),
    ):
        for block_digest, content in blocks:
            connection.execute(
                f"INSERT INTO {table}(digest,content) VALUES(?,?) ON CONFLICT(digest) DO NOTHING",
                (block_digest, content),
            )
            stored = connection.execute(
                f"SELECT content FROM {table} WHERE digest=?", (block_digest,)
            ).fetchone()
            if stored is None or stored[0] != content:
                _invalid(prepared.period, "block_digest_collision_or_damage")
    connection.execute(
        "INSERT INTO duplicate_freeze_root VALUES(?,?,?,?,?)",
        (
            prepared.period,
            prepared.close_digest,
            prepared.journal_highwater,
            prepared.payload,
            prepared.root_digest,
        ),
    )


def verified_duplicate_root(connection, header) -> VerifiedDuplicateRoot | None:
    """Return None only for an absent optional accelerator; reject a malformed one."""
    from .close_storage import derived_root

    expected = derived_root(header, DERIVED_ROOT_NAME)
    row = connection.execute(
        "SELECT * FROM duplicate_freeze_root WHERE period=?", (header.period,)
    ).fetchone()
    if expected is None:
        if row is not None:
            _invalid(header.period, "unbound_root_row")
        return None
    if row is None:
        _invalid(header.period, "bound_root_missing")
    if (
        bytes(row["close_digest"]) != header.logical_digest
        or row["journal_highwater"] != header.root["source_changes"]["highwater"]
        or bytes(row["digest"]) != expected
        or hashlib.sha256(row["payload"].encode("utf-8")).digest() != expected
    ):
        _invalid(header.period, "root_digest_mismatch")
    try:
        value = json.loads(row["payload"])
        directories = value["directories"]
        key_filter = decode_keys_filter(value["key_filter"])
        if (
            set(value)
            != {
                "format",
                "period",
                "close_digest",
                "journal_highwater",
                "bucket_count",
                "directories",
                "key_filter",
            }
            or value["format"] != FORMAT
            or value["period"] != header.period
            or value["close_digest"] != header.logical_digest.hex()
            or value["journal_highwater"] != row["journal_highwater"]
            or value["bucket_count"] != BUCKET_COUNT
            or not isinstance(directories, dict)
            or any(
                not isinstance(number, str)
                or not number.isdecimal()
                or int(number) >= 256
                or not isinstance(digest_hex, str)
                or len(digest_hex) != 64
                or bytes.fromhex(digest_hex).hex() != digest_hex
                for number, digest_hex in directories.items()
            )
        ):
            _invalid(header.period, "root_shape_invalid")
    except (TypeError, ValueError, KeyError, OverflowError):
        _invalid(header.period, "root_shape_invalid")
    return VerifiedDuplicateRoot(
        header.period, header.logical_digest, row["journal_highwater"], directories, key_filter
    )


def lookup_duplicate_keys(connection, root: VerifiedDuplicateRoot, keys, *, cache=None):
    """Batch-load and SHA-check only directories and leaves hit by these keys."""
    cache = {} if cache is None else cache
    keys = set(keys)
    possible_keys = {key for key in keys if may_contain(root.key_filter, key)}
    buckets = {key: _bucket(key) for key in possible_keys}
    wanted_directories = {_directory(number) for number in buckets.values()}
    missing = {
        number: root.directories[number]
        for number in wanted_directories
        if number in root.directories and ("directory", number) not in cache
    }
    if missing:
        rows = {
            bytes(row[0]).hex(): row[1]
            for row in connection.execute(
                "SELECT digest,content FROM duplicate_freeze_directory "
                "WHERE digest IN (SELECT unhex(value) FROM json_each(?))",
                (canonical(sorted(set(missing.values()))),),
            )
        }
        for number, expected in missing.items():
            content = rows.get(expected)
            if content is None or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected:
                _invalid(root.period, "directory_digest_mismatch")
            try:
                entries = _DIRECTORY_ENTRIES.validate_json(content, strict=True)
                lower = int(number) << 8
                upper = lower + 256
                if any(
                    not leaf.isdecimal()
                    or not lower <= int(leaf) < upper
                    for leaf in entries
                ):
                    _invalid(root.period, "directory_shape_invalid")
            except (TypeError, ValueError):
                _invalid(root.period, "directory_shape_invalid")
            cache["directory", number] = entries
    wanted_leaves = {}
    for number in buckets.values():
        directory = cache.get(("directory", _directory(number)), {})
        if number in directory and ("bucket", number) not in cache:
            wanted_leaves[number] = directory[number]
    if wanted_leaves:
        rows = {
            bytes(row[0]).hex(): row[1]
            for row in connection.execute(
                "SELECT digest,content FROM duplicate_freeze_bucket "
                "WHERE digest IN (SELECT unhex(value) FROM json_each(?))",
                (canonical(sorted(set(wanted_leaves.values()))),),
            )
        }
        for number, expected in wanted_leaves.items():
            content = rows.get(expected)
            if content is None or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected:
                _invalid(root.period, "bucket_digest_mismatch")
            try:
                entries = json.loads(content)
                if (
                    not isinstance(entries, list)
                    or any(
                        not isinstance(item, list)
                        or len(item) != 2
                        or not isinstance(item[0], str)
                        or _bucket(item[0]) != number
                        or not isinstance(item[1], list)
                        or any(
                            not isinstance(value, list)
                            or len(value) != 3
                            or not isinstance(value[0], str)
                            or not isinstance(value[1], str)
                            or type(value[2]) is not int
                            for value in item[1]
                        )
                        for item in entries
                    )
                    or [item[0] for item in entries] != sorted({item[0] for item in entries})
                ):
                    _invalid(root.period, "bucket_shape_invalid")
            except (TypeError, ValueError):
                _invalid(root.period, "bucket_shape_invalid")
            cache["bucket", number] = dict(entries)
    return {
        key: tuple(tuple(value) for value in cache.get(("bucket", buckets[key]), {}).get(key, ()))
        if key in buckets else ()
        for key in keys
    }


def lookup_duplicate_key(connection, root: VerifiedDuplicateRoot, key: str, *, cache=None):
    return lookup_duplicate_keys(connection, root, (key,), cache=cache)[key]


def compare_duplicate_freeze(connection, engine, *, repair=False):
    """Replay authoritative source changes and rebuild every closed directory.

    Reuse within this call is limited to immutable decoded fact/check rows and
    the journal's forward selection. Stored directory rows never supply an
    expected value. The close's private root decides the as-of journal cut.
    """
    from .change_journal import changes_since
    from .close_storage import derived_root, verified_header

    events = iter(changes_since(connection, 0))
    pending = next(events, None)
    authority = _Authority(connection, engine.store)
    last_highwater = 0
    referenced_directories = {}
    referenced_buckets = {}
    changed = False
    for close in connection.execute("SELECT * FROM period_close ORDER BY period"):
        period = close["period"]
        header = verified_header(connection, close, require_marker=not repair)
        highwater = header.root["source_changes"]["highwater"]
        if type(highwater) is not int or highwater < last_highwater:
            _invalid(period, "journal_highwater_invalid")
        while pending is not None and pending.sequence <= highwater:
            authority.apply(pending)
            last_highwater = pending.sequence
            pending = next(events, None)
        if last_highwater != highwater:
            _invalid(period, "journal_highwater_missing")
        prepared = _prepare_from_authority(authority, period, header.logical_digest, highwater)
        if derived_root(header, DERIVED_ROOT_NAME) != prepared.root_digest:
            _invalid(period, "authoritative_root_mismatch")
        row = connection.execute(
            "SELECT close_digest,journal_highwater,payload,digest "
            "FROM duplicate_freeze_root WHERE period=?",
            (period,),
        ).fetchone()
        expected = (
            prepared.close_digest,
            prepared.journal_highwater,
            prepared.payload,
            prepared.root_digest,
        )
        if row is None or tuple(row) != expected:
            changed = True
            if not repair:
                _invalid(period, "stored_root_mismatch")
            connection.execute(
                "INSERT INTO duplicate_freeze_root VALUES(?,?,?,?,?) "
                "ON CONFLICT(period) DO UPDATE SET "
                "close_digest=excluded.close_digest,"
                "journal_highwater=excluded.journal_highwater,"
                "payload=excluded.payload,digest=excluded.digest",
                (period, *expected),
            )
        for block_digest, content in prepared.buckets:
            referenced_buckets[block_digest] = content
        for block_digest, content in prepared.directories:
            referenced_directories[block_digest] = content
    for table, expected_rows in (
        ("duplicate_freeze_directory", referenced_directories),
        ("duplicate_freeze_bucket", referenced_buckets),
    ):
        stored_rows = {
            bytes(row[0]): row[1]
            for row in connection.execute(f"SELECT digest,content FROM {table}")
        }
        if stored_rows != expected_rows:
            changed = True
            if not repair:
                _invalid(-1, "stored_block_set_mismatch")
            connection.execute(f"DELETE FROM {table}")
            connection.executemany(
                f"INSERT INTO {table} VALUES(?,?)",
                sorted(expected_rows.items()),
            )
    return changed


def _current_candidate_facts(connection, root_period, through_period, changed_subjects):
    """Select open facts by period and later changed heads by exact subject."""
    from .duplicates import ELIGIBLE_KINDS

    current = {}
    for fact_id, subject_id, kind, current_subject in connection.execute(
        "SELECT f.id,f.subject_id,s.kind,c.subject_id FROM fact_revision f "
        "INDEXED BY fact_period CROSS JOIN fact_current c ON c.fact_id=f.id "
        "CROSS JOIN subject s ON s.id=f.subject_id "
        "WHERE f.period>? AND f.period<=?",
        (root_period, through_period),
    ):
        if current_subject != subject_id:
            _invalid(root_period, "current_fact_owner_mismatch")
        if kind in ELIGIBLE_KINDS:
            current[subject_id] = fact_id
    remaining = changed_subjects - current.keys()
    if remaining:
        for subject_id, kind, fact_id, fact_subject, period in connection.execute(
            "SELECT ids.value,s.kind,c.fact_id,f.subject_id,f.period "
            "FROM json_each(?) ids LEFT JOIN subject s ON s.id=ids.value "
            "LEFT JOIN fact_current c ON c.subject_id=ids.value "
            "LEFT JOIN fact_revision f ON f.id=c.fact_id",
            (canonical(sorted(remaining)),),
        ):
            if kind is None:
                _invalid(root_period, "changed_subject_missing")
            if fact_id is not None and fact_subject != subject_id:
                _invalid(root_period, "current_fact_owner_mismatch")
            if kind in ELIGIBLE_KINDS and fact_id is not None and period <= through_period:
                current[subject_id] = fact_id
    return current


def narrowed_duplicate_candidates(
    engine, connection, through_period: int, *, inspection_cache=None, query_reads=None
):
    """Return proven candidate and changed IDs, or None for an unknown accelerator.

    A close proves its old/old state. We only recompute subjects touched since
    that close and facts whose period was still open then. Every positive
    directory hit is merely a candidate for the existing exact reducer.
    """
    from .change_journal import changes_since, first_created_subjects
    from .close_storage import verified_header
    from .duplicates import (
        SourceLocation,
        _material_locations,
        _validate_source_locations,
    )
    from .integrity import verify_sources

    if query_reads is not None and (
        query_reads.store is not engine.store or query_reads.connection is not connection
    ):
        raise ValueError("duplicate fact reads belong to another snapshot")
    close = connection.execute(
        "SELECT * FROM period_close WHERE period<=? ORDER BY period DESC LIMIT 1",
        (through_period,),
    ).fetchone()
    if close is None:
        return None
    header = verified_header(connection, close)
    root = verified_duplicate_root(connection, header)
    if root is None:
        return None
    changes = (
        inspection_cache.source_changes_since(connection, root.journal_highwater)
        if inspection_cache is not None and hasattr(inspection_cache, "source_changes_since")
        else changes_since(connection, root.journal_highwater)
    )
    cache = {}
    changed_subjects = set()
    fact_changes = [change for change in changes if change.source == "fact"]
    # A frozen location can depend only on a material source or a material
    # resolution subject. A subject first registered after this close cannot
    # appear in its complete frozen directory either.
    changed_fact_subjects = {change.target_id for change in fact_changes}
    dependency_subjects = {
        row[0]
        for row in connection.execute(
            "SELECT ids.value FROM json_each(?) ids LEFT JOIN subject s ON s.id=ids.value "
            "WHERE s.kind IS NULL OR s.kind IN ('material_source_v2','material_resolution_v2')",
            (canonical(sorted(changed_fact_subjects)),),
        )
    }
    dependency_changes = (
        change for change in fact_changes if change.target_id in dependency_subjects
    )
    first_created = (
        first_created_subjects(connection, dependency_changes)
        if inspection_cache is None or not hasattr(inspection_cache, "first_created_subjects")
        else inspection_cache.first_created_subjects(connection, dependency_changes)
    )
    dependency_subjects -= first_created
    dependency_keys = {canonical(["dependency", subject_id]) for subject_id in dependency_subjects}
    dependency_hits = lookup_duplicate_keys(connection, root, dependency_keys, cache=cache)
    after_facts = {change.after_ref for change in fact_changes if change.after_ref is not None}
    if after_facts:
        changed_subjects.update(
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT f.subject_id FROM json_each(?) ids "
                "JOIN fact_material_resolution_v2_links l ON l.revision_id=ids.value "
                "JOIN fact_revision f ON f.id=l.fact_id",
                (canonical(sorted(after_facts)),),
            )
        )
    check_ids = {change.after_ref for change in changes if change.source == "duplicate"}
    if check_ids:
        check_rows = list(
            connection.execute(
                "SELECT d.id,d.selected_fact_id,f.subject_id FROM json_each(?) ids "
                "LEFT JOIN business_duplicate_check d ON d.id=ids.value "
                "LEFT JOIN fact_revision f ON f.id=d.selected_fact_id",
                (canonical(sorted(check_ids)),),
            )
        )
        if len(check_rows) != len(check_ids) or any(row["id"] is None for row in check_rows):
            _invalid(root.period, "changed_check_missing")
        for row in check_rows:
            if row["selected_fact_id"] is not None:
                if row["subject_id"] is None:
                    _invalid(root.period, "selected_fact_missing")
                changed_subjects.add(row["subject_id"])
    for change in changes:
        if change.source in {"fact", "identity", "duplicate"}:
            changed_subjects.add(change.target_id)
        if change.source == "fact" and change.target_id in dependency_subjects:
            for subject_id, _, _ in dependency_hits[canonical(["dependency", change.target_id])]:
                changed_subjects.add(subject_id)
    current = _current_candidate_facts(connection, root.period, through_period, changed_subjects)
    if not current:
        return set(), set(), {}
    changed_ids = set(current.values())
    locations = _material_locations(connection, changed_ids)
    source_ids = {
        item["source_fact_id"]
        for items in locations.values()
        for item in items
        if isinstance(item.get("source_fact_id"), str)
    }
    resolution_ids = {
        item["resolution_fact_id"]
        for items in locations.values()
        for item in items
        if isinstance(item.get("resolution_fact_id"), str)
    }
    verified_facts = verify_sources(
        engine,
        connection,
        fact_ids=changed_ids | source_ids | resolution_ids,
        _return_facts=True,
    )
    exact_locations = [
        SourceLocation.model_validate({field: item[field] for field in SourceLocation.model_fields})
        for items in locations.values()
        for item in items
    ]
    if exact_locations:
        _validate_source_locations(
            connection, tuple(exact_locations), _inspection_cache=inspection_cache
        )
    by_key = defaultdict(set)
    # Material checks may already have decoded these exact revisions in the
    # caller-owned read-only transaction. Source digests and original bytes
    # above are still checked independently before using any typed object.
    versions = (
        {ident: query_reads._fact_versions[ident]
         for ident in changed_ids if ident in query_reads._fact_versions}
        if query_reads is not None and query_reads._snapshot_active
        else {}
    )
    versions.update({
        fact_id: FactVersion(
            fact_id,
            verified_facts[fact_id]["subject_id"],
            verified_facts[fact_id]["revision"],
            engine.store.registry.models[verified_facts[fact_id]["kind"]].model_validate_json(
                _validation_json(verified_facts[fact_id]["data"])
            ),
            tuple(sorted(verified_facts[fact_id]["evidence"])),
        )
        for fact_id in changed_ids - versions.keys()
    })
    frozen_keys = set()
    for fact_id in changed_ids:
        version = versions[fact_id]
        for parts in _signal_parts(version, locations[fact_id]):
            if parts[0] == "dependency":
                continue
            key = canonical(parts)
            by_key[key].add(fact_id)
            if not (parts[0] == "origin" and parts[2] > root.period):
                frozen_keys.add(key)
    paired = set()
    frozen_hits = lookup_duplicate_keys(connection, root, frozen_keys, cache=cache)
    all_hits = {
        (subject_id, fact_id)
        for rows in frozen_hits.values()
        for subject_id, fact_id, frozen_period in rows
        if subject_id not in changed_subjects and frozen_period <= through_period
    }
    selected_hits = dict(all_hits)
    if len(selected_hits) != len(all_hits):
        _invalid(root.period, "frozen_subject_has_multiple_heads")
    if selected_hits:
        actual_heads = dict(
            connection.execute(
                "SELECT c.subject_id,c.fact_id FROM json_each(?) ids "
                "JOIN fact_current c ON c.subject_id=ids.value",
                (canonical(sorted(selected_hits)),),
            )
        )
        if actual_heads != selected_hits:
            _invalid(root.period, "unchanged_frozen_fact_changed_without_journal")
    for key, new_ids in by_key.items():
        if len(new_ids) > 1:
            paired.update(new_ids)
        for subject_id, _frozen_fact_id, frozen_period in frozen_hits.get(key, ()):
            if subject_id in changed_subjects or frozen_period > through_period:
                continue
            paired.update(new_ids)
            paired.add(selected_hits[subject_id])
    return paired, changed_ids, locations
