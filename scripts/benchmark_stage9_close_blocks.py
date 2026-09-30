"""Read-only draft close-block prototype on a synthetic Stage 9 company.

This deliberately does not modify the source company or production schema. It
round-trips existing logical v4 manifests through an isolated SQLite storage
codec, then measures selected subject reads and a ten-year material-size model.
The ten-year row replication is a size model, not a valid accounting history.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import canonical

BLOCK_SIZE = 128
ENCODING = "stage9-close-block-prototype/1"
FIELDS = (
    "adopted_results",
    "vouchers",
    "material_coverage.coverage",
    "material_coverage.file_summaries",
    "material_coverage.fact_ids",
    "material_coverage.resolution_versions",
    "material_coverage.inventory_versions",
    "material_coverage.allocation_versions",
    "material_coverage.source_versions",
    "material_coverage.group_versions",
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bucket(value: str) -> int:
    return hashlib.sha256(value.encode("utf-8")).digest()[0]


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


def _field_buckets(manifest: dict, path: str):
    values = _path(manifest, path)
    subjects = {
        item["calculation_id"]: item["subject_id"]
        for item in manifest["adopted_results"]
    }
    result = defaultdict(list)
    for index, value in enumerate(values):
        if path == "adopted_results":
            key = value["subject_id"]
        elif path == "vouchers":
            key = subjects[value["adopted_calculation_id"]]
        elif path in (
            "material_coverage.coverage",
            "material_coverage.file_summaries",
        ):
            key = value["source_id"]
        elif path == "material_coverage.inventory_versions":
            key = str(value["inventory_id"])
        else:
            key = value
        result[_bucket(key)].append([index, value])
    return result


def _encode(connection: sqlite3.Connection, period: int, manifest: dict):
    small = copy.deepcopy(manifest)
    directories = {}
    for field in FIELDS:
        buckets = _field_buckets(manifest, field)
        _set(small, field, [])
        directories[field] = []
        for bucket, entries in sorted(buckets.items()):
            parts = []
            for part, start in enumerate(range(0, len(entries), BLOCK_SIZE)):
                rows = entries[start : start + BLOCK_SIZE]
                content = canonical(rows)
                hashed = _hash(content)
                connection.execute(
                    "INSERT INTO block VALUES(?,?,?,?,?,?)",
                    (period, field, bucket, part, content, hashed),
                )
                parts.append([part, hashed, len(rows)])
            directory = canonical(parts)
            hashed = _hash(directory)
            connection.execute(
                "INSERT INTO directory VALUES(?,?,?,?,?)",
                (period, field, bucket, directory, hashed),
            )
            directories[field].append([bucket, hashed, len(entries)])

    # The existing presentation summary is retained in the compact root. Its
    # growing key lists are already fixed blocks in the logical owner review.
    # Move only the keys; restore them before checking the logical v4 digest.
    for section in small["owner_review"]["collections"]:
        field = "owner_review_keys:" + section["section"]
        directories[field] = []
        for block in section["blocks"]:
            bucket = block["index"]
            content = canonical(block.pop("keys"))
            hashed = _hash(content)
            connection.execute(
                "INSERT INTO block VALUES(?,?,?,?,?,?)",
                (period, field, bucket, 0, content, hashed),
            )
            directory = canonical([[0, hashed, block["count"]]])
            dir_hash = _hash(directory)
            connection.execute(
                "INSERT INTO directory VALUES(?,?,?,?,?)",
                (period, field, bucket, directory, dir_hash),
            )
            directories[field].append([bucket, dir_hash, block["count"]])

    logical = canonical(manifest)
    families = {
        "accounting": {
            "directories": {key: directories[key] for key in ("adopted_results", "vouchers")}
        },
        "material": {
            "small": small.pop("material_coverage"),
            "directories": {
                key: value for key, value in directories.items()
                if key.startswith("material_coverage.")
            },
        },
        "management": {
            "management_snapshot": small.pop("management_snapshot"),
            "readiness": small.pop("readiness"),
        },
        "review": {
            "owner_review": small.pop("owner_review"),
            "directories": {
                key: value for key, value in directories.items()
                if key.startswith("owner_review_keys:")
            },
        },
    }
    committed = {}
    family_bytes = {}
    for family, value in families.items():
        family_content = canonical(value)
        family_digest = _hash(family_content)
        connection.execute(
            "INSERT INTO subroot VALUES(?,?,?,?)",
            (period, family, family_content, family_digest),
        )
        committed[family] = family_digest
        family_bytes[family] = len(family_content)
    root = {
        "encoding": ENCODING,
        "period": period,
        "logical_digest": _hash(logical),
        "small": small,
        "subroots": committed,
    }
    content = canonical(root)
    hashed = _hash(content)
    connection.execute(
        "INSERT INTO root VALUES(?,?,?,?,?)",
        (period, content, hashed, root["logical_digest"], root["logical_digest"]),
    )
    return len(content), family_bytes, sum(len(_path(manifest, field)) for field in FIELDS)


def _read_root(connection, period):
    row = connection.execute(
        "SELECT content,storage_digest,logical_digest,marker FROM root WHERE period=?",
        (period,),
    ).fetchone()
    if row is None or _hash(row[0]) != row[1]:
        raise AssertionError("root SHA mismatch")
    root = json.loads(row[0])
    if root["logical_digest"] != row[2] or row[2] != row[3]:
        raise AssertionError("logical digest or marker mismatch")
    if root["encoding"] != ENCODING or root["period"] != period:
        raise AssertionError("storage identity mismatch")
    return root, len(row[0])


def _read_family(connection, root, period, family):
    row = connection.execute(
        "SELECT content,digest FROM subroot WHERE period=? AND family=?",
        (period, family),
    ).fetchone()
    if row is None or _hash(row[0]) != row[1] or row[1] != root["subroots"][family]:
        raise AssertionError("missing or damaged committed subroot")
    return json.loads(row[0]), len(row[0])


def _read_bucket(connection, root, period, field, bucket):
    expected = next(
        (item for item in root["directories"][field] if item[0] == bucket), None
    )
    if expected is None:
        return [], 0
    directory = connection.execute(
        "SELECT content,digest FROM directory WHERE period=? AND field=? AND bucket=?",
        (period, field, bucket),
    ).fetchone()
    if directory is None or _hash(directory[0]) != directory[1] or directory[1] != expected[1]:
        raise AssertionError("missing or damaged committed directory")
    entries = []
    byte_count = len(directory[0])
    parts = json.loads(directory[0])
    if sum(part[2] for part in parts) != expected[2]:
        raise AssertionError("directory count mismatch")
    for part, hashed, count in parts:
        row = connection.execute(
            "SELECT content,digest FROM block WHERE period=? AND field=? AND bucket=? AND part=?",
            (period, field, bucket, part),
        ).fetchone()
        if row is None or _hash(row[0]) != row[1] or row[1] != hashed:
            raise AssertionError("missing or damaged committed block")
        decoded = json.loads(row[0])
        if field.startswith("owner_review_keys:"):
            if len(decoded) != count:
                raise AssertionError("owner key count mismatch")
        elif len(decoded) != count:
            raise AssertionError("block count mismatch")
        entries.extend(decoded if not field.startswith("owner_review_keys:") else [decoded])
        byte_count += len(row[0])
    return entries, byte_count


def _decode(connection, period):
    root, read_bytes = _read_root(connection, period)
    manifest = root["small"]
    families = {}
    for family in ("accounting", "material", "management", "review"):
        families[family], size = _read_family(connection, root, period, family)
        read_bytes += size
    manifest["material_coverage"] = families["material"]["small"]
    manifest["management_snapshot"] = families["management"]["management_snapshot"]
    manifest["readiness"] = families["management"]["readiness"]
    manifest["owner_review"] = families["review"]["owner_review"]
    for field in FIELDS:
        values = []
        family = "accounting" if field in ("adopted_results", "vouchers") else "material"
        subroot = families[family]
        for bucket, _, _ in subroot["directories"][field]:
            entries, byte_count = _read_bucket(connection, subroot, period, field, bucket)
            values.extend(entries)
            read_bytes += byte_count
        _set(manifest, field, [value for _, value in sorted(values)])
    for section in manifest["owner_review"]["collections"]:
        field = "owner_review_keys:" + section["section"]
        for block in section["blocks"]:
            entries, byte_count = _read_bucket(
                connection, families["review"], period, field, block["index"]
            )
            if len(entries) != 1:
                raise AssertionError("owner key block missing")
            block["keys"] = entries[0]
            read_bytes += byte_count
    expected_directories = {
        (field, bucket)
        for family in ("accounting", "material", "review")
        for field, buckets in families[family]["directories"].items()
        for bucket, _, _ in buckets
    }
    actual_directories = set(connection.execute(
        "SELECT field,bucket FROM directory WHERE period=?", (period,)
    ))
    if actual_directories != expected_directories:
        raise AssertionError("complete directory membership mismatch")
    expected_blocks = set()
    for field, bucket in expected_directories:
        raw = connection.execute(
            "SELECT content FROM directory WHERE period=? AND field=? AND bucket=?",
            (period, field, bucket),
        ).fetchone()[0]
        expected_blocks.update((field, bucket, part) for part, _, _ in json.loads(raw))
    actual_blocks = set(connection.execute(
        "SELECT field,bucket,part FROM block WHERE period=?", (period,)
    ))
    if actual_blocks != expected_blocks:
        raise AssertionError("complete block membership mismatch")
    if _hash(canonical(manifest)) != root["logical_digest"]:
        raise AssertionError("logical v4 digest mismatch")
    return manifest, read_bytes


def _scoped(connection, period_subjects):
    read_bytes = 0
    selected = []
    for period, subjects in period_subjects.items():
        root, size = _read_root(connection, period)
        accounting, accounting_bytes = _read_family(connection, root, period, "accounting")
        read_bytes += size + accounting_bytes
        for bucket in sorted({_bucket(subject) for subject in subjects}):
            bucket_subjects = {subject for subject in subjects if _bucket(subject) == bucket}
            adopted, amount = _read_bucket(
                connection, accounting, period, "adopted_results", bucket
            )
            vouchers, voucher_bytes = _read_bucket(
                connection, accounting, period, "vouchers", bucket
            )
            read_bytes += amount + voucher_bytes
            found = {item["subject_id"] for _, item in adopted}
            for subject in bucket_subjects:
                if subject not in found:
                    raise AssertionError("published subject absent from committed block")
            selected.extend(
                item for _, item in adopted if item["subject_id"] in bucket_subjects
            )
            # Each selected voucher still requires exact publication/adoption
            # identity proof against the immutable company rows in real code.
            selected_calculations = {
                item["calculation_id"] for _, item in adopted
                if item["subject_id"] in bucket_subjects
            }
            selected.extend(
                item for _, item in vouchers
                if item["adopted_calculation_id"] in selected_calculations
            )
    return len(selected), read_bytes


def _project_material(latest, years):
    result = copy.deepcopy(latest)
    coverage = latest["material_coverage"]
    projected = result["material_coverage"]
    for field in ("coverage", "file_summaries"):
        projected[field] = []
        for year in range(years):
            for value in coverage[field]:
                clone = copy.deepcopy(value)
                clone["source_id"] = f"year-{year:02d}:" + clone["source_id"]
                projected[field].append(clone)
    for field in (
        "fact_ids", "resolution_versions", "inventory_versions",
        "allocation_versions", "source_versions", "group_versions",
    ):
        projected[field] = [
            (
                {**value, "inventory_id": value["inventory_id"] + year * 1_000_000}
                if field == "inventory_versions"
                else f"year-{year:02d}:{value}"
            )
            for year in range(years)
            for value in coverage[field]
        ]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.with_suffix(".json").exists():
        raise SystemExit("prototype output already exists; choose a new path")
    report = json.loads(args.source_report.read_text(encoding="utf-8"))
    source_path = Path(report["company"]["path"])
    company = report["company"]
    store = Store(
        source_path,
        production_bundle(),
        company["id"],
        company["database_id"],
        taxpayer_id=company["taxpayer_id"],
    )
    with store.connection(read_only=True) as source:
        measure(source, args, source_path)


def measure(source, args, source_path):
    manifest_rows = source.execute(
        "SELECT period,manifest,digest FROM period_close ORDER BY period"
    ).fetchall()
    if len(manifest_rows) < 10:
        raise SystemExit(f"expected at least 10 sealed synthetic months, got {len(manifest_rows)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(args.output)
    target.executescript(
        "CREATE TABLE root(period INTEGER PRIMARY KEY,content TEXT,storage_digest TEXT,"
        "logical_digest TEXT,marker TEXT);"
        "CREATE TABLE subroot(period INTEGER,family TEXT,content TEXT,digest TEXT,"
        "PRIMARY KEY(period,family));"
        "CREATE TABLE directory(period INTEGER,field TEXT,bucket INTEGER,content TEXT,digest TEXT,"
        "PRIMARY KEY(period,field,bucket));"
        "CREATE TABLE block(period INTEGER,field TEXT,bucket INTEGER,part INTEGER,content TEXT,"
        "digest TEXT,PRIMARY KEY(period,field,bucket,part));"
    )
    sizes = []
    started = time.perf_counter()
    for row in manifest_rows:
        if hashlib.sha256(row["manifest"].encode("utf-8")).digest() != row["digest"]:
            raise AssertionError("source logical close SHA damaged")
        manifest = json.loads(row["manifest"])
        root_bytes, family_bytes, rows = _encode(target, row["period"], manifest)
        sizes.append({"period": row["period"], "logical_bytes": len(row["manifest"]),
                      "root_bytes": root_bytes, "subroot_bytes": family_bytes,
                      "chunked_rows": rows})
    target.commit()
    encode_ms = (time.perf_counter() - started) * 1000
    full = []
    for row in manifest_rows:
        started = time.perf_counter()
        decoded, read_bytes = _decode(target, row["period"])
        if canonical(decoded) != canonical(json.loads(row["manifest"])):
            raise AssertionError("decoded logical close differs from source")
        full.append({"period": row["period"], "milliseconds": (time.perf_counter()-started)*1000,
                     "read_bytes": read_bytes})

    # This scope is obtained from the authoritative publication table, not a
    # reverse close-reference index. Pick the 25 most recurring real subjects.
    selected_subjects = [row[0] for row in source.execute(
        "SELECT subject_id FROM calculation_publication GROUP BY subject_id "
        "ORDER BY count(DISTINCT posting_period) DESC,subject_id LIMIT 25"
    )]
    sealed_periods = {row["period"] for row in manifest_rows}
    period_subjects = defaultdict(set)
    for row in source.execute(
        "SELECT DISTINCT subject_id,posting_period FROM calculation_publication "
        "WHERE subject_id IN (SELECT value FROM json_each(?))",
        (canonical(selected_subjects),),
    ):
        if row["posting_period"] in sealed_periods:
            period_subjects[row["posting_period"]].add(row["subject_id"])
    partial = []
    for _ in range(3):
        started = time.perf_counter()
        selected_count, read_bytes = _scoped(target, period_subjects)
        partial.append({"milliseconds": (time.perf_counter()-started)*1000,
                        "selected_count": selected_count, "read_bytes": read_bytes})

    latest = json.loads(manifest_rows[-1]["manifest"])
    projections = []
    for years in (1, 2, 4, 6, 8, 10):
        projected = _project_material(latest, years)
        period = manifest_rows[-1]["period"] + years * 12
        root_bytes, family_bytes, rows = _encode(target, period, projected)
        projections.append({"months_modeled": years * 12, "root_bytes": root_bytes,
                            "subroot_bytes": family_bytes,
                            "logical_bytes": len(canonical(projected)), "chunked_rows": rows})
    target.commit()
    # Damages on a separate scratch transaction must be detected by a partial
    # read, and rollback must return the prototype to its measured contents.
    damage = {}
    sample_period = max(period_subjects)
    root, _ = _read_root(target, sample_period)
    accounting, _ = _read_family(target, root, sample_period, "accounting")
    sample_bucket = _bucket(next(iter(period_subjects[sample_period])))
    target.execute("BEGIN")
    target.execute("DELETE FROM directory WHERE period=? AND field=? AND bucket=?",
                   (sample_period, "adopted_results", sample_bucket))
    try:
        _read_bucket(target, accounting, sample_period, "adopted_results", sample_bucket)
    except AssertionError as exc:
        damage["missing_directory"] = str(exc)
    target.rollback()
    target.execute("BEGIN")
    target.execute("UPDATE block SET content='[]' WHERE period=? AND field=? AND bucket=?",
                   (sample_period, "adopted_results", sample_bucket))
    try:
        _read_bucket(target, accounting, sample_period, "adopted_results", sample_bucket)
    except AssertionError as exc:
        damage["damaged_block"] = str(exc)
    target.rollback()
    target.execute("BEGIN")
    target.execute("UPDATE root SET content='{}' WHERE period=?", (sample_period,))
    try:
        _read_root(target, sample_period)
    except AssertionError as exc:
        damage["damaged_root"] = str(exc)
    target.rollback()

    # Even if an attacker rehashes a storage root after omitting a bucket,
    # the authoritative publication subject still demands that exact bucket.
    selected_subject = next(iter(period_subjects[sample_period]))
    selected_bucket = _bucket(selected_subject)
    target.execute("BEGIN")
    rewritten_accounting = copy.deepcopy(accounting)
    rewritten_accounting["directories"]["adopted_results"] = [
        entry for entry in rewritten_accounting["directories"]["adopted_results"]
        if entry[0] != selected_bucket
    ]
    accounting_content = canonical(rewritten_accounting)
    accounting_digest = _hash(accounting_content)
    target.execute(
        "UPDATE subroot SET content=?,digest=? WHERE period=? AND family='accounting'",
        (accounting_content, accounting_digest, sample_period),
    )
    rewritten_root = copy.deepcopy(root)
    rewritten_root["subroots"]["accounting"] = accounting_digest
    root_content = canonical(rewritten_root)
    root_digest = _hash(root_content)
    target.execute(
        "UPDATE root SET content=?,storage_digest=? WHERE period=?",
        (root_content, root_digest, sample_period),
    )
    target.execute(
        "DELETE FROM block WHERE period=? AND field='adopted_results' AND bucket=?",
        (sample_period, selected_bucket),
    )
    target.execute(
        "DELETE FROM directory WHERE period=? AND field='adopted_results' AND bucket=?",
        (sample_period, selected_bucket),
    )
    try:
        _scoped(target, {sample_period: {selected_subject}})
    except AssertionError as exc:
        damage["coherently_resealed_missing_subject"] = str(exc)
    try:
        _decode(target, sample_period)
    except AssertionError as exc:
        damage["full_decode_missing_subject"] = str(exc)
    target.rollback()
    target.execute("BEGIN")
    target.execute(
        "INSERT INTO block VALUES(?,?,?,?,?,?)",
        (sample_period, "extra", 0, 0, "[]", _hash("[]")),
    )
    try:
        _decode(target, sample_period)
    except AssertionError as exc:
        damage["extra_block"] = str(exc)
    target.rollback()
    target.execute("BEGIN")
    target.execute(
        "INSERT INTO directory VALUES(?,?,?,?,?)",
        (sample_period, "extra", 0, "[]", _hash("[]")),
    )
    try:
        _decode(target, sample_period)
    except AssertionError as exc:
        damage["extra_directory"] = str(exc)
    target.rollback()
    if set(damage) != {
        "missing_directory", "damaged_block", "damaged_root",
        "coherently_resealed_missing_subject", "full_decode_missing_subject",
        "extra_block", "extra_directory",
    }:
        raise AssertionError("damage probes did not fail closed")
    result = {
        "source": str(source_path), "prototype_database": str(args.output),
        "encoding": ENCODING, "block_size": BLOCK_SIZE,
        "actual_sealed_months": sizes, "encode_ms": encode_ms,
        "full_logical_v4_roundtrips": full, "scoped_subjects": selected_subjects,
        "scoped_period_count": len(period_subjects), "scoped_reads": partial,
        "material_size_model": (
            "Repeated nonempty month-12 source rows with year-prefixed IDs; "
            "not a valid accounting history"
        ),
        "material_projection": projections, "damage_checks": damage,
        "storage_bytes": args.output.stat().st_size,
        "directory_rows": target.execute("SELECT count(*) FROM directory").fetchone()[0],
        "block_rows": target.execute("SELECT count(*) FROM block").fetchone()[0],
    }
    args.output.with_suffix(".json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
