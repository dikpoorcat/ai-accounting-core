"""Frozen v1 read-only duplicate close directory verification.

Rebuilds every closed root from the immutable v1 journal and historical
fact decoder. No write, repair, or current duplicate rules are used.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass

from .change_journal_v1 import changes_since
from .close_storage_v1 import derived_root, verified_header
from .contracts import KernelError
from .duplicate_checks_v1 import SourceLocation, _require_check_record
from .duplicate_checks_v1 import verify_duplicate_checks as _verify_duplicate_checks
from .duplicate_rules_v1 import (
    ACTUAL_MONEY_KINDS,
    ELIGIBLE_KINDS,
    ORIGIN_KINDS,
    _actual_money_legs,
    _signature,
)
from .history_encoding_v1 import canonical, digest
from .key_membership_filter_v1 import (
    DecodedKeysFilter,
    build_keys_filter,
    decode_keys_filter,
    may_contain,
)

DERIVED_ROOT_NAME = "duplicate"


FORMAT = "ai-accounting-kernel/2/duplicate-freeze/2"


BUCKET_COUNT = 65536


def verify_duplicate_checks(connection) -> None:
    _verify_duplicate_checks(connection)


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
    fact = version.fact
    kind = fact.kind
    period = fact.period.ordinal
    keys = set()
    if kind in ORIGIN_KINDS:
        signature = _signature(fact)
        keys.update(
            canonical(["origin", kind, period, signature, evidence])
            for evidence in version.evidence
        )
    role = "actual_money" if kind in ACTUAL_MONEY_KINDS else kind
    keys.update(
        canonical(["location", role, item["evidence_digest"], item["location"]])
        for item in locations
        if item.get("evidence_digest") is not None and item.get("location") is not None
    )
    if kind in ACTUAL_MONEY_KINDS:
        for leg in _actual_money_legs(fact):
            coordinates = [
                leg[field]
                for field in ("category", "account_id", "actual_date", "direction", "amount_fen")
            ]
            if leg["object_id"] is not None:
                keys.add(canonical(["money_object", kind, *coordinates, leg["object_id"]]))
            keys.update(
                canonical(["money_movement", *coordinates, movement])
                for movement in leg["movement_ids"]
            )
    dependencies = {
        item["source_id"] for item in locations if isinstance(item.get("source_id"), str)
    }
    dependencies.update(
        item["resolution_subject_id"]
        for item in locations
        if isinstance(item.get("resolution_subject_id"), str)
    )
    keys.update(canonical(["dependency", source_id]) for source_id in dependencies)
    return keys


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
            self.versions((fact_id,))
        return self._versions[fact_id]

    def versions(self, fact_ids):
        fact_ids = tuple(fact_ids)
        missing = set(fact_ids) - self._versions.keys()
        if missing:
            from .content_v1 import load_v1_fact_versions

            self._versions.update(
                load_v1_fact_versions(self.connection, self.store.registry, missing)
            )
        return {fact_id: self._versions[fact_id] for fact_id in fact_ids}

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
    authority.versions(authority.facts.values())
    selected = {}
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


def verified_duplicate_root(connection, header) -> VerifiedDuplicateRoot | None:
    """Return None only for an absent optional accelerator; reject a malformed one."""
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
    wanted_directories = {_directory(_bucket(key)) for key in possible_keys}
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
                entries = json.loads(content)
                if not isinstance(entries, dict) or any(
                    not isinstance(leaf, str)
                    or not leaf.isdecimal()
                    or int(leaf) >= BUCKET_COUNT
                    or _directory(leaf) != number
                    or not isinstance(block, str)
                    or len(block) != 64
                    or bytes.fromhex(block).hex() != block
                    for leaf, block in entries.items()
                ):
                    _invalid(root.period, "directory_shape_invalid")
            except (TypeError, ValueError):
                _invalid(root.period, "directory_shape_invalid")
            cache["directory", number] = entries
    wanted_leaves = {}
    for key in possible_keys:
        number = _bucket(key)
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
        key: tuple(tuple(value) for value in cache.get(("bucket", _bucket(key)), {}).get(key, ()))
        if key in possible_keys else ()
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
    if repair:
        raise ValueError("historical duplicate verification cannot repair a database")
    events = iter(changes_since(connection, 0))
    pending = next(events, None)
    authority = _Authority(connection, engine.store)
    last_highwater = 0
    referenced_directories = {}
    referenced_buckets = {}
    for close in connection.execute("SELECT * FROM period_close ORDER BY period"):
        period = close["period"]
        header = verified_header(connection, close)
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
            _invalid(period, "stored_root_mismatch")
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
            _invalid(-1, "stored_block_set_mismatch")
    return False
