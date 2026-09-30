"""Rehouse one named Stage 9 synthetic book under a new, index-only company DDL.

This is a test fixture operation, not an application import or an upgrade. It
never edits its source or an existing destination. A destination is published
only after exact raw-row comparison and the target's registered full verifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

_NEW_INDEXES = frozenset({"subject_id_kind_cover", "fact_id_subject_cover"})
_SOURCE_ROOTS = frozenset(
    {
        "stage9-release-main-12",
        "stage9-release-main-48",
        "stage9-release-main-120",
        "stage9-release-independent-120",
    }
)
_COMPANIES = {
    "main": ("91310000123456789S", "阶段九合成规模企业", "stage9-builder.json"),
    "independent": (
        "91310000123456789I",
        "阶段九合成独立业务企业",
        "stage9-independent-builder.json",
    ),
}
_GENERATED = frozenset({"identity", "schema_meta", "schema_history"})
_INITIAL_ROWS = {
    "state": (1, 1, 0, 0, 0, 1, 0),
    "source_change_head": (1, 1, 0),
}
_INITIAL_NONEMPTY = _GENERATED | frozenset(_INITIAL_ROWS)


def _sha(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected object: {path}")
    return value


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _objects(connection) -> dict[tuple[str, str], str]:
    return {
        (kind, name): sql
        for kind, name, sql in connection.execute(
            "SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


def _require_index_only(source, target) -> None:
    old, new = _objects(source), _objects(target)
    added = new.keys() - old.keys()
    if added != {("index", name) for name in _NEW_INDEXES}:
        raise ValueError(f"Company DDL does not add exactly two approved indexes: {added}")
    if old.keys() - new.keys() or any(old[key] != new[key] for key in old.keys() & new.keys()):
        raise ValueError("Company DDL changed an existing table, index, view or trigger")
    for name in _NEW_INDEXES:
        if not new[("index", name)].startswith("CREATE INDEX "):
            raise ValueError("New index is not an ordinary covering index")


def _tables(connection) -> list[str]:
    return [
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _columns(connection, table: str) -> list[str]:
    values = list(connection.execute(f"PRAGMA table_xinfo({_quoted(table)})"))
    if not values or any(row[1].lower() in {"rowid", "_rowid_", "oid"} for row in values):
        raise ValueError(f"Unsupported synthetic table shape: {table}")
    return [row[1] for row in values if row[6] == 0]


def _rows(connection, table: str, columns: list[str]):
    fields = ",".join(_quoted(name) for name in columns)
    return connection.execute(f"SELECT rowid,{fields} FROM {_quoted(table)} ORDER BY rowid")


def _feed(hashed, value):
    if value is None:
        tag, raw = b"n", b""
    elif isinstance(value, bytes):
        tag, raw = b"b", value
    elif type(value) is int:
        tag, raw = b"i", str(value).encode("ascii")
    elif isinstance(value, str):
        tag, raw = b"s", value.encode("utf-8")
    elif type(value) is float:
        tag, raw = b"f", repr(value).encode("ascii")
    else:
        raise ValueError(f"Unsupported saved SQLite value type: {type(value)}")
    hashed.update(tag)
    hashed.update(len(raw).to_bytes(8, "big"))
    hashed.update(raw)


def _table_digest(connection, table: str, columns: list[str]) -> tuple[int, str]:
    hashed, count = hashlib.sha256(), 0
    # Include stored generated columns as well as the explicit rowid. Only
    # insertable columns are used by the copy, but every saved value is checked.
    for row in connection.execute(f"SELECT rowid,* FROM {_quoted(table)} ORDER BY rowid"):
        for value in row:
            _feed(hashed, value)
        count += 1
    return count, hashed.hexdigest()


def _copy_rows(source, target, *, progress=None) -> dict[str, dict]:
    """Copy raw Stage 9 rows only; rollback restores the target's initial DDL."""
    _require_index_only(source, target)
    tables = _tables(source)
    if tables != _tables(target):
        raise ValueError("Source and destination table inventories differ")
    columns = {name: _columns(source, name) for name in tables}
    if any(columns[name] != _columns(target, name) for name in tables):
        raise ValueError("Source and destination insertable columns differ")
    for name in _GENERATED:
        if name not in tables:
            raise ValueError(f"Missing generated database identity table: {name}")
    if any(name not in tables for name in _INITIAL_ROWS):
        raise ValueError("Fresh company is missing an initialized state or journal head")
    if target.in_transaction:
        raise ValueError("Row copy needs its own target transaction")
    # Store.create installs exactly these five rows. Unknown new seed rows must
    # fail closed rather than collide with copied source rows or survive as
    # unnoticed target-only content.
    initial_nonempty = {
        name
        for name in tables
        if target.execute(f"SELECT 1 FROM {_quoted(name)} LIMIT 1").fetchone() is not None
    }
    if (
        initial_nonempty != _INITIAL_NONEMPTY
        or any(
            [tuple(row) for row in _rows(target, name, columns[name])] != [expected]
            for name, expected in _INITIAL_ROWS.items()
        )
        or any(
            target.execute(f"SELECT count(*) FROM {_quoted(name)}").fetchone()[0] != 1
            for name in _GENERATED
        )
    ):
        raise ValueError("Fresh target has unexpected initialized company rows")
    triggers = [(name, sql) for (kind, name), sql in _objects(target).items() if kind == "trigger"]
    target.execute("PRAGMA foreign_keys=OFF")
    if target.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
        raise ValueError("Unable to isolate target foreign keys during raw copy")
    try:
        target.execute("BEGIN IMMEDIATE")
        for name, _ in triggers:
            target.execute(f"DROP TRIGGER {_quoted(name)}")
        copied = {}
        for table in tables:
            fields = columns[table]
            source_count, source_hash = _table_digest(source, table, fields)
            if table in _GENERATED:
                if table == "identity":
                    if list(_rows(source, table, fields)) != list(_rows(target, table, fields)):
                        raise ValueError("Fresh target identity differs from source")
                continue
            if table in _INITIAL_ROWS:
                if source_count != 1:
                    raise ValueError(f"Stage 9 company needs one {table} row")
                values = tuple(list(_rows(source, table, fields))[0])
                if values[0] != 1 or values[1] != 1:
                    raise ValueError(f"Stage 9 {table} identity changed")
                assignments = ",".join(f"{_quoted(name)}=?" for name in fields[1:])
                target.execute(f"UPDATE {_quoted(table)} SET {assignments} WHERE id=1", values[2:])
            else:
                names = ",".join(("rowid", *(_quoted(name) for name in fields)))
                marks = ",".join("?" for _ in range(len(fields) + 1))
                statement = f"INSERT INTO {_quoted(table)}({names}) VALUES({marks})"
                cursor = _rows(source, table, fields)
                while batch := cursor.fetchmany(256):
                    target.executemany(statement, batch)
            count, actual = _table_digest(target, table, fields)
            if (count, actual) != (source_count, source_hash):
                raise ValueError(f"Raw rows changed during copy: {table}")
            copied[table] = {"rows": count, "sha256": actual}
            if progress is not None:
                progress("table_copied", table=table, rows=count, sha256=actual)
        for _, sql in triggers:
            target.execute(sql)
        _require_index_only(source, target)
        if target.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Copied Stage 9 company violates a foreign key")
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Copied Stage 9 company failed SQLite integrity check")
        target.commit()
        return copied
    except BaseException:
        target.rollback()
        raise
    finally:
        try:
            target.execute("PRAGMA foreign_keys=ON")
            if target.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise ValueError("Target foreign-key enforcement was not restored")
        except BaseException:
            target.close()
            raise


class _Progress:
    def __init__(self, path: Path):
        self.path = path

    def __call__(self, phase: str, **fields) -> None:
        with self.path.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps({"phase": phase, "unix": time.time(), **fields}, ensure_ascii=False)
                + "\n"
            )
            output.flush()
            os.fsync(output.fileno())


def _synthetic_root(root: Path, workspace: Path, *, source: bool) -> Path:
    resolved = root.resolve()
    if resolved.parent != (workspace / ".tmp").resolve():
        raise ValueError("Stage 9 roots must be immediate children of workspace .tmp")
    if source:
        if resolved.name not in _SOURCE_ROOTS:
            raise ValueError("Only the four named Stage 9 synthetic sources are accepted")
    elif not resolved.name.startswith("stage9-reseed-"):
        raise ValueError("Destination must have a distinct stage9-reseed-* name")
    return resolved


def _source_guard(root: Path, report_path: Path, source_tree: Path, *, allow_unverified: bool):
    kind = "independent" if root.name == "stage9-release-independent-120" else "main"
    taxpayer, name, checkpoint_name = _COMPANIES[kind]
    checkpoint_path = root / checkpoint_name
    checkpoint = _read_json(checkpoint_path)
    report = _read_json(report_path)
    company = checkpoint.get("company")
    if (
        not isinstance(company, dict)
        or (company.get("taxpayer_id"), company.get("name")) != (taxpayer, name)
        or company.get("path") != str(root / taxpayer / "company.sqlite")
        or report.get("company") != company
        or report.get("root") != str(root)
        or report.get("source") != str(source_tree)
        or checkpoint.get("snapshots") != report.get("snapshots")
        or not isinstance(checkpoint.get("epochs"), list)
        or len(checkpoint["epochs"]) != 6
        or any(type(value) is not int for value in checkpoint["epochs"])
        or not isinstance(checkpoint.get("month_stats"), list)
        or len(checkpoint["month_stats"]) != report.get("requested_months")
    ):
        raise ValueError("Synthetic checkpoint, company and source report differ")
    if report.get("months") != checkpoint.get("month_stats"):
        raise ValueError("Source report and monthly checkpoint differ")
    if report.get("business_count") != (
        len(checkpoint["business_subjects"]) if kind == "main" else checkpoint.get("business_count")
    ):
        raise ValueError("Source business count differs from checkpoint")
    if report.get("status") == "complete":
        integrity = report.get("integrity", {})
        if (
            integrity.get("status") != "verified"
            or integrity.get("limitations")
            or set(integrity.get("coverage", {}).values()) != {"verified"}
        ):
            raise ValueError("Source report does not attest complete content verification")
    elif not (
        allow_unverified
        and root.name == "stage9-release-main-120"
        and report.get("status") == "built_not_verified"
    ):
        raise ValueError("Source has no accepted complete verification report")
    if not checkpoint["month_stats"] or not checkpoint["snapshots"]:
        raise ValueError("Synthetic source has no completed monthly checkpoint")
    last_period = checkpoint["month_stats"][-1]["period"]
    if checkpoint["snapshots"].get(last_period, {}).get("closed") is not False:
        raise ValueError("Synthetic source lacks an open final month")
    return kind, checkpoint_name, checkpoint, report, last_period


def _write_json_new(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def reseed(args):
    from stage9_source import configure_source, workspace_root

    workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
    old_root = _synthetic_root(args.source_root, workspace, source=True)
    target_root = _synthetic_root(args.target_root, workspace, source=False)
    staging = target_root.with_name(target_root.name + ".staging")
    source_tree = args.source.resolve(strict=True)
    target_tree = args.target_source.resolve(strict=True)
    if (
        any(
            tree.parent != (workspace / ".tmp").resolve()
            or re.fullmatch(r"stage9-build-source-r[0-9]+-release", tree.name) is None
            for tree in (source_tree, target_tree)
        )
        or source_tree == target_tree
    ):
        raise ValueError("Only distinct fixed Stage 9 source trees under .tmp are accepted")
    if target_root.exists() or staging.exists() or old_root == target_root:
        raise ValueError("Destination or its staging root already exists; never overwrite it")
    report_path = args.source_report.resolve(strict=True)
    if report_path.parent != (workspace / ".tmp").resolve() or not report_path.name.startswith(
        "stage9-"
    ):
        raise ValueError("Only an existing Stage 9 source report is accepted")
    kind, checkpoint_name, checkpoint, old_report, period = _source_guard(
        old_root, report_path, source_tree, allow_unverified=args.allow_unverified_construction
    )
    source_tree = source_tree.resolve(strict=True)
    configure_source(target_tree, workspace)
    from stage9_verified_open_preview import verify_book_open_preview

    from ai_accounting.kernel.catalog import Catalog
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.migration_contracts import load_contracts
    from ai_accounting.kernel.schema_bundle import APPLICATION_ID, FAMILY, production_bundle
    from ai_accounting.kernel.storage import Store
    from ai_accounting.kernel.versions import objects, verify_schema

    target_bundle = production_bundle()
    if target_bundle.status != "released" or target_bundle.current_versions != {
        "company": 1,
        "catalog": 1,
    }:
        raise ValueError("Only a fixed released/1 target source may be reseeded")
    old_contract_dir = source_tree / "src/ai_accounting/kernel/schema_contracts"
    previous = {
        name: load_contracts(
            old_contract_dir / name, name, family=FAMILY, application_id=APPLICATION_ID
        )[1]
        for name in ("company", "catalog")
    }
    if any(item["status"] != "released" for item in previous.values()):
        raise ValueError("Source is not an exact released/1 synthetic contract")
    target_contract = target_bundle.current("company")
    source_map = {(x["type"], x["name"]): x["sql"] for x in previous["company"]["objects"]}
    target_map = {(x["type"], x["name"]): x["sql"] for x in target_contract["objects"]}
    if (
        target_map.keys() - source_map.keys() != {("index", name) for name in _NEW_INDEXES}
        or source_map.keys() - target_map.keys()
        or any(source_map[key] != target_map[key] for key in source_map.keys() & target_map.keys())
        or previous["catalog"]["objects"] != target_bundle.current("catalog")["objects"]
    ):
        raise ValueError("Package contracts differ beyond the two approved company indexes")
    company = checkpoint["company"]
    source_db = old_root / company["taxpayer_id"] / "company.sqlite"
    if not source_db.is_file() or Path(company["path"]).resolve() != source_db.resolve():
        raise ValueError("Stage 9 company path or identity changed")
    old_catalog_path = old_root / "catalog.sqlite"
    with closing(sqlite3.connect(old_catalog_path.as_uri() + "?mode=ro", uri=True)) as old_catalog:
        old_catalog.row_factory = sqlite3.Row
        old_catalog.execute("BEGIN")
        from ai_accounting.kernel.schema_bundle import load_bundle

        old_bundle = load_bundle(
            target_bundle.registry,
            old_contract_dir,
            family=FAMILY,
            application_id=APPLICATION_ID,
            status="released",
            current_versions={"company": 1, "catalog": 1},
        )
        verify_schema(old_catalog, bundle=old_bundle, kind="catalog")
        rows = [dict(row) for row in old_catalog.execute("SELECT * FROM company ORDER BY id")]
        if company not in rows:
            raise ValueError("Source catalog no longer contains the checkpoint company")
        if old_catalog.execute("SELECT 1 FROM company_setting LIMIT 1").fetchone():
            raise ValueError("Source has catalog settings with old external paths")
        omitted_security = {
            table: old_catalog.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("security_owner", "security_session", "security_audit")
        }
    staging.mkdir()
    progress = _Progress(staging / "stage9-reseed-progress.jsonl")
    progress("staging_created", source_status=old_report["status"])
    catalog = Catalog(staging, target_bundle)
    staged_db = staging / company["taxpayer_id"] / "company.sqlite"
    staged_db.parent.mkdir()
    Store.create(
        staged_db, target_bundle, company["id"], company["taxpayer_id"], company["database_id"]
    )
    copied = {}
    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        verify_schema(source, bundle=old_bundle)
        if [tuple(row) for row in source.execute("SELECT * FROM state")] != [
            tuple(checkpoint["epochs"])
        ]:
            raise ValueError("Source state or repair revision changed after checkpoint")
        if source.execute("SELECT 1 FROM jobs LIMIT 1").fetchone():
            raise ValueError("Source jobs or delivery payloads need a separate path review")
        if source.execute("SELECT 1 FROM security_close_approval LIMIT 1").fetchone():
            raise ValueError("Source approval still refers to the old catalog security identity")
        from ai_accounting.kernel.types import YearMonth

        closed = {
            str(YearMonth.from_ordinal(row[0])): row[1]
            for row in source.execute(
                "SELECT period,json_extract(manifest,'$.preview_digest') FROM period_close"
            )
        }
        expected_closed = {
            month: value["preview_digest"]
            for month, value in checkpoint["snapshots"].items()
            if value["closed"] is True
        }
        if closed != expected_closed:
            raise ValueError("Closed source rows differ from the completed-month checkpoint")
        target_store = Store(
            staged_db,
            target_bundle,
            company["id"],
            company["database_id"],
            taxpayer_id=company["taxpayer_id"],
        )
        with target_store.connection() as target:
            progress("copy_started")
            copied = _copy_rows(source, target, progress=progress)
            verify_schema(target, bundle=target_bundle)
            if objects(target) != target_bundle.current("company")["objects"]:
                raise ValueError("Target structure changed during raw row copy")
            progress("copy_verified", tables=len(copied))
        if [tuple(row) for row in source.execute("SELECT * FROM state")] != [
            tuple(checkpoint["epochs"])
        ]:
            raise ValueError("Source changed while its read transaction was held")
    staged_company = {**company, "path": str(staged_db)}
    staged_checkpoint = {**checkpoint, "company": staged_company}
    _write_json_new(staging / checkpoint_name, staged_checkpoint)
    snapshots = checkpoint["snapshots"]
    # Construction inputs are not verifier inputs. The large mixed-book
    # checkpoint is reloaded only after verification to publish its new path.
    del staged_checkpoint, checkpoint
    with catalog.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO company(id,taxpayer_id,name,path,database_id) VALUES(?,?,?,?,?)",
            tuple(
                staged_company[field]
                for field in ("id", "taxpayer_id", "name", "path", "database_id")
            ),
        )
        connection.commit()
    catalog.bind(company["id"])
    progress("full_verification_started")
    proof = verify_book_open_preview(
        Engine(target_store),
        checkpoint_path=staging / checkpoint_name,
        company=staged_company,
        snapshots=snapshots,
        period=period,
        source=target_tree,
    )
    if proof["integrity"].get("status") != "verified" or proof["integrity"].get("limitations"):
        raise ValueError("Target registered content verifier did not finish without limitations")
    progress(
        "full_verification_and_preview_verified",
        integrity_ms=proof["integrity_ms"],
        preview_digest=proof["verified_open_preview"]["digest"],
    )
    final_company = {
        **company,
        "path": str(target_root / company["taxpayer_id"] / "company.sqlite"),
    }
    with catalog.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE company SET path=? WHERE id=?", (final_company["path"], company["id"])
        )
        connection.commit()
    checkpoint = _read_json(staging / checkpoint_name)
    final_checkpoint = {**checkpoint, "company": final_company}
    (staging / checkpoint_name).write_text(
        json.dumps(final_checkpoint, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    new_report = {
        "status": "complete",
        "source": str(target_tree),
        "workspace": str(workspace),
        "root": str(target_root),
        "company": final_company,
        "requested_months": old_report["requested_months"],
        "default_page_limit": 100,
        "employee_count": old_report["employee_count"],
        "monthly_business_count": old_report["monthly_business_count"],
        "business_count": old_report["business_count"],
        "distribution": old_report["distribution"],
        "construction_integrity": {"mode": "synthetic_raw_row_reseed"},
        "snapshots": checkpoint["snapshots"],
        "months": checkpoint["month_stats"],
        "counts": old_report["counts"],
        "measurements": {},
        "integrity_contract": proof["integrity_contract"],
        "integrity": proof["integrity"],
        "integrity_ms": proof["integrity_ms"],
        "verified_open_preview": proof["verified_open_preview"],
        "reseed_provenance": {
            "mode": "synthetic_raw_rows_index_only_not_upgrade",
            "source_root": str(old_root),
            "source_tree": str(source_tree),
            "source_report": str(report_path),
            "source_report_sha256": _sha(report_path),
            "source_checkpoint_sha256": _sha(old_root / checkpoint_name),
            "source_status": old_report["status"],
            "source_construction": old_report.get("construction_integrity"),
            "source_company_contract": previous["company"]["sha256"],
            "target_company_contract": target_contract["sha256"],
            "new_indexes": sorted(_NEW_INDEXES),
            "source_catalog_company_count": len(rows),
            "copied_catalog_company_count": 1,
            "omitted_source_catalog_security_rows": omitted_security,
            "all_company_row_digests": copied,
        },
    }
    if kind == "independent":
        new_report["registered_object_count"] = old_report["registered_object_count"]
    else:
        new_report["kinds"] = old_report["kinds"]
    for table, name in (
        ("subject", "subject"),
        ("fact_revision", "fact_revision"),
        ("calculation", "calculation"),
        ("calculation_publication", "calculation_publication"),
        ("voucher_version", "voucher_version"),
        ("period_close", "period_close"),
        ("evidence", "evidence"),
        ("entity", "entity"),
    ):
        if name in new_report["counts"] and new_report["counts"][name] != copied[table]["rows"]:
            raise ValueError(f"Source report count changed before reseed: {table}")
    _write_json_new(staging / "stage9-reseed-report.json", new_report)
    _write_json_new(
        staging / "stage9-reseed-finished.json",
        {
            "target_root": str(target_root),
            "company": final_company,
            "target_source": str(target_tree),
            "report_sha256": _sha(staging / "stage9-reseed-report.json"),
            "checkpoint_sha256": _sha(staging / checkpoint_name),
        },
    )
    # All connections and transactions are closed before the same-parent rename.
    os.rename(staging, target_root)
    _Progress(target_root / "stage9-reseed-progress.jsonl")("published")
    return target_root / "stage9-reseed-report.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True, help="frozen creator source")
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument(
        "--target-source", type=Path, required=True, help="frozen index-only source"
    )
    parser.add_argument("--allow-unverified-construction", action="store_true")
    args = parser.parse_args()
    print(reseed(args))


if __name__ == "__main__":
    main()
