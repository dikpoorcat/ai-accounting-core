"""Root-bound reverse directory for locating changed closed material sources.

The directory is a repairable locator, not a completeness decision.  Its root
is committed by the close and ordinary reads authenticate only buckets hit by
the post-close source journal.  Full integrity rebuilds every bucket from the
original close proof and immutable source rows.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass

from .change_journal import CONTRACT as JOURNAL_CONTRACT
from .change_journal import changes_since, first_created_subjects, head, heads_at
from .close_storage import decode_close, derived_root, verified_header
from .contracts import KernelError
from .types import YearMonth, canonical, digest

MATERIAL_WATCH_DDL = """
CREATE TABLE material_watch_root(period INTEGER PRIMARY KEY REFERENCES period_close(period),
 content TEXT NOT NULL CHECK(json_valid(content)),
 root_digest BLOB NOT NULL CHECK(length(root_digest)=32)) STRICT;
CREATE TABLE material_watch_directory(period INTEGER NOT NULL REFERENCES period_close(period),
 directory INTEGER NOT NULL CHECK(directory BETWEEN 0 AND 255),
 content TEXT NOT NULL CHECK(json_valid(content)),
 content_digest BLOB NOT NULL CHECK(length(content_digest)=32),
 PRIMARY KEY(period,directory)) STRICT;
CREATE TABLE material_watch_bucket(period INTEGER NOT NULL REFERENCES period_close(period),
 bucket INTEGER NOT NULL CHECK(bucket BETWEEN 0 AND 65535),
 content TEXT NOT NULL CHECK(json_valid(content)),
 content_digest BLOB NOT NULL CHECK(length(content_digest)=32),
 PRIMARY KEY(period,bucket)) STRICT;
"""

CONTRACT = "ai-accounting-kernel/2/material-watch/1"


@dataclass(frozen=True, slots=True)
class PreparedMaterialWatch:
    period: int
    close_digest: bytes
    highwater: int
    content: str
    root_digest: bytes
    directories: tuple[tuple[int, str, bytes], ...]
    buckets: tuple[tuple[int, str, bytes], ...]


def _invalid(period: int, reason: str) -> None:
    raise KernelError(
        "content_integrity_failed",
        "关账资料变化目录与权威来源不一致",
        component="material_watch",
        record_id=str(period),
        reason=reason,
    )


def _bucket(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:2])


def _text_digest(content: str) -> bytes:
    return hashlib.sha256(content.encode("utf-8")).digest()


def _version_rows(connection, identifiers, table):
    if not isinstance(identifiers, list) or len(identifiers) != len(set(identifiers)):
        _invalid(-1, "version_list_invalid")
    if not identifiers:
        return ()
    source_column = "f.subject_id" if table == "fact_material_source_v2" else "t.source_id"
    return tuple(
        connection.execute(
            f"SELECT f.id,f.subject_id,{source_column} FROM json_each(?) ids "
            "JOIN fact_revision f ON f.id=ids.value "
            f"JOIN {table} t ON t.revision_id=f.id "
            "JOIN fact_seal seal ON seal.fact_id=f.id",
            (canonical(identifiers),),
        )
    )


def _directory(connection, period, manifest, *, historic_heads=None):
    """Build exact reverse keys using only frozen versions in this close."""
    proof = manifest["material_coverage"]
    files = proof["file_summaries"]
    source_ids = {item["source_id"] for item in files}
    keys = defaultdict(set)
    evidence_sources = defaultdict(set)
    source_revisions = _version_rows(
        connection, proof["source_versions"], "fact_material_source_v2"
    )
    if len(source_revisions) != len(proof["source_versions"]):
        _invalid(period, "frozen_source_version_missing")
    source_evidence = (
        dict(
            connection.execute(
                "SELECT t.revision_id,t.evidence_digest FROM json_each(?) ids "
                "CROSS JOIN fact_material_source_v2 t ON t.revision_id=ids.value",
                (canonical(proof["source_versions"]),),
            )
        )
        if proof["source_versions"]
        else {}
    )
    for fact_id, subject_id, _ in source_revisions:
        if subject_id not in source_ids:
            _invalid(period, "source_summary_missing")
        keys[f"business:{subject_id}"].add(subject_id)
        evidence = source_evidence[fact_id]
        evidence_sources[evidence].add(subject_id)
        keys[f"evidence:{evidence}"].add(subject_id)
    for label, table in (
        ("allocation_versions", "fact_material_period_allocation"),
        ("resolution_versions", "fact_material_resolution_v2"),
        ("group_versions", "fact_material_group_resolution"),
    ):
        revisions = _version_rows(connection, proof[label], table)
        if len(revisions) != len(proof[label]):
            _invalid(period, "frozen_disposition_version_missing")
        sources = {fact_id: source_id for fact_id, _, source_id in revisions}
        for _fact_id, subject_id, source_id in revisions:
            if source_id not in source_ids:
                _invalid(period, "disposition_source_missing")
            keys[f"business:{subject_id}"].add(source_id)
            keys[f"business:{source_id}"].add(source_id)
        if label in ("resolution_versions", "group_versions") and proof[label]:
            for revision_id, subject_id, fact_id, calculation_id in connection.execute(
                "SELECT l.revision_id,l.subject_id,l.fact_id,l.calculation_id "
                "FROM json_each(?) ids "
                f"CROSS JOIN {table}_links l ON l.revision_id=ids.value",
                (canonical(proof[label]),),
            ):
                keys[f"business:{subject_id}"].add(sources[revision_id])
                if historic_heads is not None and (
                    historic_heads["fact"].get(subject_id) != fact_id
                    or historic_heads["calculation"].get(subject_id) != calculation_id
                ):
                    _invalid(period, "frozen_link_adoption_mismatch")
        if label == "resolution_versions" and proof[label]:
            for revision_id, duplicate in connection.execute(
                "SELECT t.revision_id,t.duplicate_source_id FROM json_each(?) ids "
                "CROSS JOIN fact_material_resolution_v2 t ON t.revision_id=ids.value "
                "WHERE t.duplicate_source_id IS NOT NULL",
                (canonical(proof[label]),),
            ):
                source_id = sources[revision_id]
                keys[f"business:{duplicate}"].add(source_id)
                keys[f"business:{source_id}"].add(duplicate)
    frozen_inventories = proof["inventory_versions"]
    inventory_ids = [item["inventory_id"] for item in frozen_inventories]
    if len(inventory_ids) != len(set(inventory_ids)):
        _invalid(period, "frozen_inventory_duplicate")
    inventory_rows = (
        dict(
            (ident, (ordinal, category))
            for ident, ordinal, category in connection.execute(
                "SELECT m.id,m.period,m.category FROM json_each(?) ids "
                "CROSS JOIN material_revision m ON m.id=ids.value",
                (canonical(inventory_ids),),
            )
        )
        if inventory_ids
        else {}
    )
    if len(inventory_rows) != len(inventory_ids):
        _invalid(period, "frozen_inventory_missing")
    inventory_keys = {}
    for item in frozen_inventories:
        inventory_id = item["inventory_id"]
        row = inventory_rows[inventory_id]
        if item["period"] != str(YearMonth.from_ordinal(row[0])) or item["category"] != row[1]:
            _invalid(period, "frozen_inventory_missing")
        inventory_keys[inventory_id] = f"inventory:{row[0]}:{row[1]}"
    if inventory_ids:
        for inventory_id, evidence in connection.execute(
            "SELECT i.inventory_id,lower(hex(i.evidence_digest)) "
            "FROM json_each(?) ids CROSS JOIN material_item i ON i.inventory_id=ids.value",
            (canonical(inventory_ids),),
        ):
            keys[inventory_keys[inventory_id]].update(evidence_sources.get(evidence, ()))

    # A change to one member of a shared evidence/business/duplicate group can
    # alter another member's competing-use result. Map every key to its whole
    # connected component instead of relying on a mutable reverse index later.
    parent = {source_id: source_id for source_id in source_ids}

    def find(source_id):
        while parent[source_id] != source_id:
            parent[source_id] = parent[parent[source_id]]
            source_id = parent[source_id]
        return source_id

    for members in keys.values():
        present = sorted(members & source_ids)
        for source_id in present[1:]:
            parent[find(source_id)] = find(present[0])
    components = defaultdict(set)
    for source_id in source_ids:
        components[find(source_id)].add(source_id)
    return sorted(source_ids), {
        key: sorted(
            {member for source_id in members & source_ids for member in components[find(source_id)]}
        )
        for key, members in keys.items()
    }


class _HistoricHeads:
    """Advance one authenticated journal through successive close highwaters."""

    def __init__(self, connection):
        self.events = changes_since(connection, 0)
        self.latest = self.events[-1].sequence if self.events else 0
        self.index = 0
        self.highwater = 0
        self.selected = {"fact": {}, "calculation": {}}

    def through(self, period, highwater):
        if (
            type(highwater) is not int
            or highwater < self.highwater
            or highwater > self.latest
        ):
            _invalid(period, "journal_highwater_invalid")
        while self.index < len(self.events) and self.events[self.index].sequence <= highwater:
            event = self.events[self.index]
            selected = self.selected.get(event.source)
            if selected is not None:
                if selected.get(event.target_id) != event.before_ref:
                    _invalid(period, "journal_head_chain_mismatch")
                if event.after_ref is None:
                    selected.pop(event.target_id, None)
                else:
                    selected[event.target_id] = event.after_ref
            self.index += 1
        self.highwater = highwater
        return self.selected


def _prepared(
    connection, period, manifest, close_digest, highwater, *, historic=False, historic_heads=None
):
    if digest(manifest) != close_digest or manifest["period"] != str(
        YearMonth.from_ordinal(period)
    ):
        _invalid(period, "logical_close_mismatch")
    selected = None
    if historic:
        selected = historic_heads if historic_heads is not None else heads_at(connection, highwater)
    sources, directory = _directory(connection, period, manifest, historic_heads=selected)
    grouped = defaultdict(dict)
    for key, members in directory.items():
        grouped[_bucket(key)][key] = members
    buckets = tuple(
        (number, canonical(content), digest(content)) for number, content in sorted(grouped.items())
    )
    grouped_directories = defaultdict(list)
    for number, _, checksum in buckets:
        grouped_directories[number >> 8].append([number, checksum.hex()])
    directories = tuple(
        (number, canonical(content), digest(content))
        for number, content in sorted(grouped_directories.items())
    )
    root = {
        "contract": CONTRACT,
        "period": period,
        "close_digest": close_digest.hex(),
        "highwater": highwater,
        "source_ids": sources,
        "directories": [[number, checksum.hex()] for number, _, checksum in directories],
    }
    return PreparedMaterialWatch(
        period, close_digest, highwater, canonical(root), digest(root), directories, buckets
    )


def prepare_material_watch(engine, connection, period, logical_manifest, logical_close_digest):
    """Prepare a directory in the same transaction as the logical close."""
    del engine
    return _prepared(connection, period, logical_manifest, logical_close_digest, head(connection))


def _header(connection, period):
    row = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
    if row is None:
        _invalid(period, "close_missing")
    header = verified_header(connection, row)
    anchor = header.root.get("source_changes")
    if (
        not isinstance(anchor, dict)
        or anchor.get("contract") != JOURNAL_CONTRACT
        or type(anchor.get("highwater")) is not int
    ):
        _invalid(period, "source_change_anchor_missing")
    return row, header, anchor["highwater"]


def persist_material_watch(connection, prepared):
    """Write a repairable directory only after its private root is committed."""
    row, header, highwater = _header(connection, prepared.period)
    if (
        row["digest"] != prepared.close_digest
        or derived_root(header, "material_watch") != prepared.root_digest
        or highwater != prepared.highwater
        or _text_digest(prepared.content) != prepared.root_digest
    ):
        _invalid(prepared.period, "prepared_root_mismatch")
    connection.execute(
        "INSERT INTO material_watch_root VALUES(?,?,?)",
        (prepared.period, prepared.content, prepared.root_digest),
    )
    connection.executemany(
        "INSERT INTO material_watch_directory VALUES(?,?,?,?)",
        (
            (prepared.period, number, content, checksum)
            for number, content, checksum in prepared.directories
        ),
    )
    connection.executemany(
        "INSERT INTO material_watch_bucket VALUES(?,?,?,?)",
        (
            (prepared.period, number, content, checksum)
            for number, content, checksum in prepared.buckets
        ),
    )


def _stored_root(connection, period):
    row, header, highwater = _header(connection, period)
    stored = connection.execute(
        "SELECT content,root_digest FROM material_watch_root WHERE period=?", (period,)
    ).fetchone()
    try:
        expected = derived_root(header, "material_watch")
        if (
            expected is None
            or bytes(stored["root_digest"]) != expected
            or _text_digest(stored["content"]) != expected
        ):
            _invalid(period, "stored_root_mismatch")
        root = json.loads(stored["content"])
        if (
            root["contract"] != CONTRACT
            or root["period"] != period
            or root["close_digest"] != row["digest"].hex()
            or root["highwater"] != highwater
            or root["source_ids"] != sorted(set(root["source_ids"]))
            or root["directories"] != sorted(root["directories"])
            or len({item[0] for item in root["directories"]}) != len(root["directories"])
        ):
            _invalid(period, "stored_root_mismatch")
    except (TypeError, KeyError, ValueError, json.JSONDecodeError):
        _invalid(period, "stored_root_invalid")
    return root


def _read_keys(connection, period, root, keys):
    expected_directories = dict(root["directories"])
    result = set()
    keys_by_bucket = defaultdict(list)
    for key in keys:
        keys_by_bucket[_bucket(key)].append(key)
    wanted_directories = sorted({number >> 8 for number in keys_by_bucket})
    if not wanted_directories:
        return result
    directory_rows = {
        row[0]: row
        for row in connection.execute(
            "SELECT m.directory,m.content,m.content_digest FROM json_each(?) ids "
            "CROSS JOIN material_watch_directory m "
            "WHERE m.period=? AND m.directory=ids.value",
            (canonical(wanted_directories), period),
        )
    }
    expected_buckets = {}
    for directory_number in wanted_directories:
        directory_row = directory_rows.get(directory_number)
        if directory_number not in expected_directories:
            if directory_row is not None:
                _invalid(period, "unexpected_directory")
            continue
        try:
            if (
                bytes(directory_row[2]).hex() != expected_directories[directory_number]
                or _text_digest(directory_row[1]) != bytes(directory_row[2])
            ):
                _invalid(period, "directory_mismatch")
            directory = json.loads(directory_row[1])
            if (
                not isinstance(directory, list)
                or directory != sorted(directory)
                or len({item[0] for item in directory}) != len(directory)
                or any(item[0] >> 8 != directory_number for item in directory)
            ):
                _invalid(period, "directory_mismatch")
        except (TypeError, KeyError, ValueError, json.JSONDecodeError):
            _invalid(period, "directory_invalid")
        expected_buckets.update(dict(directory))
    bucket_rows = {
        row[0]: row
        for row in connection.execute(
            "SELECT b.bucket,b.content,b.content_digest FROM json_each(?) ids "
            "CROSS JOIN material_watch_bucket b WHERE b.period=? AND b.bucket=ids.value",
            (canonical(sorted(keys_by_bucket)), period),
        )
    }
    for number, wanted_keys in keys_by_bucket.items():
        row = bucket_rows.get(number)
        if number not in expected_buckets:
            if row is not None:
                _invalid(period, "unexpected_bucket")
            continue
        try:
            if (
                bytes(row[2]).hex() != expected_buckets[number]
                or _text_digest(row[1]) != bytes(row[2])
            ):
                _invalid(period, "bucket_mismatch")
            content = json.loads(row[1])
            if (
                not isinstance(content, dict)
                or any(_bucket(key) != number for key in content)
            ):
                _invalid(period, "bucket_mismatch")
        except (TypeError, KeyError, ValueError, json.JSONDecodeError):
            _invalid(period, "bucket_invalid")
        for key in wanted_keys:
            members = content.get(key, [])
            if not isinstance(members, list) or members != sorted(set(members)):
                _invalid(period, "bucket_members_invalid")
            result.update(members)
    if not result.issubset(root["source_ids"]):
        _invalid(period, "bucket_source_invalid")
    return result


def _change_keys(connection, events, closed_through, *, inspection_cache=None):
    """Resolve changed source identities with bounded SQL, independent of event count."""
    keys = {
        f"business:{event.target_id}"
        for event in events
        if event.source
        in {"fact", "calculation", "pending", "identity", "duplicate", "publication"}
    }
    fact_targets = {}
    for event in events:
        if event.source != "fact":
            continue
        for reference in (event.before_ref, event.after_ref):
            if reference is None:
                continue
            if reference in fact_targets and fact_targets[reference] != event.target_id:
                _invalid(closed_through, "changed_fact_owner_mismatch")
            fact_targets[reference] = event.target_id
    if fact_targets:
        references = canonical(sorted(fact_targets))
        fact_rows = tuple(
            connection.execute(
                "SELECT f.id,f.subject_id,s.kind,f.revision,"
                "(SELECT sequence FROM source_change WHERE source='fact' "
                "AND after_ref=f.id ORDER BY sequence LIMIT 1) "
                "FROM json_each(?) ids "
                "CROSS JOIN fact_revision f ON f.id=ids.value "
                "JOIN subject s ON s.id=f.subject_id",
                (references,),
            )
        )
        kinds = {
            row[0]: row[2]
            for row in fact_rows
            if row[1] == fact_targets[row[0]]
        }
        if set(kinds) != set(fact_targets):
            _invalid(closed_through, "changed_fact_revision_missing")
        references_by_kind = {
            kind: canonical(
                sorted(ident for ident, found_kind in kinds.items() if found_kind == kind)
            )
            for kind in (
                "material_source_v2",
                "material_period_allocation",
                "material_resolution_v2",
                "material_group_resolution",
            )
        }
        matched = set()
        for revision_id, evidence in connection.execute(
            "SELECT t.revision_id,t.evidence_digest FROM json_each(?) ids "
            "CROSS JOIN fact_material_source_v2 t ON t.revision_id=ids.value",
            (references_by_kind["material_source_v2"],),
        ):
            matched.add(revision_id)
            keys.add(f"business:{fact_targets[revision_id]}")
            keys.add(f"evidence:{evidence}")
        for kind, table in (
            ("material_period_allocation", "fact_material_period_allocation"),
            ("material_resolution_v2", "fact_material_resolution_v2"),
            ("material_group_resolution", "fact_material_group_resolution"),
        ):
            for revision_id, source_id in connection.execute(
                "SELECT t.revision_id,t.source_id FROM json_each(?) ids "
                f"CROSS JOIN {table} t ON t.revision_id=ids.value",
                (references_by_kind[kind],),
            ):
                matched.add(revision_id)
                keys.add(f"business:{source_id}")
        for kind, table in (
            ("material_resolution_v2", "fact_material_resolution_v2_links"),
            ("material_group_resolution", "fact_material_group_resolution_links"),
        ):
            keys.update(
                f"business:{row[0]}"
                for row in connection.execute(
                    "SELECT DISTINCT l.subject_id FROM json_each(?) ids "
                    f"CROSS JOIN {table} l ON l.revision_id=ids.value",
                    (references_by_kind[kind],),
                )
            )
        keys.update(
            f"business:{row[0]}"
            for row in connection.execute(
                "SELECT DISTINCT t.duplicate_source_id FROM json_each(?) ids "
                "CROSS JOIN fact_material_resolution_v2 t ON t.revision_id=ids.value "
                "WHERE t.duplicate_source_id IS NOT NULL",
                (references_by_kind["material_resolution_v2"],),
            )
        )
        if any(
            kind.startswith("material_") and ident not in matched for ident, kind in kinds.items()
        ):
            _invalid(closed_through, "changed_material_fact_missing")
        # A business first created after this close cannot be a dependency of
        # its complete frozen material proof. Authenticate first creation via
        # the retained journal, not merely before_ref=NULL: an old deleted
        # fact can be restored later. Keep evidence and referenced old-business
        # keys from new dispositions, so competing uses still invalidate them.
        creation_rows = tuple((row[0], row[1], row[3], row[4]) for row in fact_rows)
        created = (
            first_created_subjects(connection, events, _fact_rows=creation_rows)
            if inspection_cache is None or not hasattr(inspection_cache, "first_created_subjects")
            else inspection_cache.first_created_subjects(
                connection, events, _fact_rows=creation_rows
            )
        )
        keys.difference_update(f"business:{subject_id}" for subject_id in created)
    inventory_items = {}
    for event in events:
        if event.source == "inventory":
            ordinal, separator, category = event.target_id.partition(":")
            if not separator or not ordinal.isdecimal() or not category:
                _invalid(closed_through, "inventory_target_invalid")
            if int(ordinal) <= closed_through:
                keys.add(f"inventory:{event.target_id}")
        elif event.source == "inventory_item":
            if not event.target_id.isdecimal():
                _invalid(closed_through, "inventory_item_target_invalid")
            inventory_items[int(event.target_id)] = event.after_ref
            keys.add(f"evidence:{event.after_ref}")
    if inventory_items:
        found = set()
        for inventory_id, period, category in connection.execute(
            "SELECT m.id,m.period,m.category FROM json_each(?) ids "
            "CROSS JOIN material_revision m ON m.id=ids.value",
            (canonical(sorted(inventory_items)),),
        ):
            found.add(inventory_id)
            if period <= closed_through:
                keys.add(f"inventory:{period}:{category}")
        if found != set(inventory_items):
            _invalid(closed_through, "inventory_item_source_missing")
    return keys


def changed_material_sources(connection, closed_through, *, _inspection_cache=None):
    """Authenticate touched reverse buckets, returning frozen and changed IDs."""
    root = _stored_root(connection, closed_through)
    events = (
        changes_since(connection, root["highwater"])
        if _inspection_cache is None
        else _inspection_cache.source_changes_since(connection, root["highwater"])
    )
    keys = _change_keys(connection, events, closed_through, inspection_cache=_inspection_cache)
    affected = _read_keys(connection, closed_through, root, keys)
    return frozenset(root["source_ids"]), frozenset(affected)


def compare_material_watch(engine, connection, *, through_period=None, _verified_closes=None):
    """Rebuild every directory from closed authority and compare every stored row."""
    del engine
    rows = tuple(
        connection.execute(
            "SELECT * FROM period_close"
            + (" WHERE period<=?" if through_period is not None else "")
            + " ORDER BY period",
            () if through_period is None else (through_period,),
        )
    )
    verified = None
    if _verified_closes is not None:
        from .verified_close_archive import VerifiedCloseArchive

        if isinstance(_verified_closes, VerifiedCloseArchive):
            verified = _verified_closes.lookup(connection, rows)
        else:
            verified = {row["period"]: (row, manifest) for row, manifest in _verified_closes}
            if set(verified) != {row["period"] for row in rows}:
                _invalid(-1, "verified_close_set_mismatch")
    historical_heads = _HistoricHeads(connection) if rows else None
    for row in rows:
        period = row["period"]
        _, header, highwater = _header(connection, period)
        adopted = None if verified is None else verified[period]
        manifest = decode_close(connection, row) if adopted is None else adopted[1]
        if adopted is not None and tuple(adopted[0]) != tuple(row):
            _invalid(period, "verified_close_row_mismatch")
        expected = _prepared(
            connection,
            period,
            manifest,
            row["digest"],
            highwater,
            historic=True,
            historic_heads=historical_heads.through(period, highwater),
        )
        if derived_root(header, "material_watch") != expected.root_digest:
            _invalid(period, "frozen_root_mismatch")
        _stored_root(connection, period)
        actual_content = connection.execute(
            "SELECT content FROM material_watch_root WHERE period=?", (period,)
        ).fetchone()[0]
        if actual_content != expected.content:
            _invalid(period, "root_content_mismatch")
        actual = tuple(
            connection.execute(
                "SELECT bucket,content,content_digest FROM material_watch_bucket "
                "WHERE period=? ORDER BY bucket",
                (period,),
            )
        )
        if tuple(tuple(item) for item in actual) != expected.buckets:
            _invalid(period, "bucket_content_mismatch")
        actual_directories = tuple(
            connection.execute(
                "SELECT directory,content,content_digest FROM material_watch_directory "
                "WHERE period=? ORDER BY directory",
                (period,),
            )
        )
        if tuple(tuple(item) for item in actual_directories) != expected.directories:
            _invalid(period, "directory_content_mismatch")
    stored = {
        row[0]
        for row in connection.execute(
            "SELECT period FROM material_watch_root"
            + (" WHERE period<=?" if through_period is not None else ""),
            () if through_period is None else (through_period,),
        )
    }
    if stored != {row["period"] for row in rows}:
        _invalid(-1, "root_set_mismatch")


def require_material_watch(engine, connection, *, through_period=None, _verified_closes=None):
    compare_material_watch(
        engine, connection, through_period=through_period, _verified_closes=_verified_closes
    )


def repair_material_watch(engine, connection):
    """Rebuild repairable rows solely from authority and committed close roots."""
    del engine
    prepared = []
    rows = tuple(connection.execute("SELECT * FROM period_close ORDER BY period"))
    historical_heads = _HistoricHeads(connection) if rows else None
    for row in rows:
        _, header, highwater = _header(connection, row["period"])
        manifest = decode_close(connection, row)
        item = _prepared(
            connection,
            row["period"],
            manifest,
            row["digest"],
            highwater,
            historic=True,
            historic_heads=historical_heads.through(row["period"], highwater),
        )
        if derived_root(header, "material_watch") != item.root_digest:
            _invalid(row["period"], "frozen_root_mismatch")
        prepared.append(item)
    before = (
        tuple(connection.execute("SELECT * FROM material_watch_root ORDER BY period")),
        tuple(
            connection.execute("SELECT * FROM material_watch_directory ORDER BY period,directory")
        ),
        tuple(connection.execute("SELECT * FROM material_watch_bucket ORDER BY period,bucket")),
    )
    connection.execute("DELETE FROM material_watch_bucket")
    connection.execute("DELETE FROM material_watch_directory")
    connection.execute("DELETE FROM material_watch_root")
    for item in prepared:
        persist_material_watch(connection, item)
    after = (
        tuple(connection.execute("SELECT * FROM material_watch_root ORDER BY period")),
        tuple(
            connection.execute("SELECT * FROM material_watch_directory ORDER BY period,directory")
        ),
        tuple(connection.execute("SELECT * FROM material_watch_bucket ORDER BY period,bucket")),
    )
    return before != after
