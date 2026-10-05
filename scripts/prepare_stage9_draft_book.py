"""Construct a new draft Stage 9 fixture from identical released/1 synthetic SQL.

This is not an upgrade, downgrade, import API or content qualification. All
company rows except fresh installation metadata retain their exact rowid/bytes.
The source is read-only; only an absent workspace .tmp staging tree is written.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import traceback
from contextlib import closing
from pathlib import Path

import rebase_stage9_synthetic_book as copy
from benchmark_stage9_browser import qualification_dimensions, validate_book_report
from prepare_stage9_main120 import require_manifest
from stage9_source import configure_source, require_source_module, synthetic_path, workspace_root

COMPANY = "c9f9f7051bca67f1241ee5c89676fb9476bc819dc8f92c1a0c0bd1e940f459cb"
CATALOG = "b484d315272bb64de3856b458f04d2d4881da05db7552250864260916c69a4ee"
IDENTITIES = {
    copy.COMPANIES["main"][:4]: ("mixed_cumulative", 50, 1000, "stage9-builder.json"),
    copy.COMPANIES["independent"][:4]: (
        "independent_local_pairs", 50, 1000, "stage9-independent-builder.json",
    ),
    ("a28ca844c09e4c20b0088e44bfb873dd", "91310000123456789S", "阶段九合成规模企业",
     "38284843c52a4934960b9b1e533545c7"): (
         "mixed_cumulative", 200, 5000, "stage9-builder.json",
     ),
}
PAGE_CASES = {
    copy.COMPANIES["independent"][:4]: 120,
    ("a28ca844c09e4c20b0088e44bfb873dd", "91310000123456789S", "阶段九合成规模企业",
     "38284843c52a4934960b9b1e533545c7"): 12,
}
METADATA_TRIGGERS = (
    "immutable_schema_meta_update", "immutable_schema_meta_delete",
    "immutable_schema_history_update", "immutable_schema_history_delete",
)


def require_copy_mode(mode, identity, months):
    require(mode in {"rows", "pages"}, "Unknown fixture copy mode")
    if mode == "pages":
        require(identity in PAGE_CASES and type(months) is int
                and months == PAGE_CASES[identity],
                "Page copy only accepts the known independent120 or pressure12 case")


def _copy_pages(source, target, old, new, bundle):
    """Page-copy only an exact known synthetic identity into a fresh draft Store."""
    from ai_accounting.kernel.versions import install_metadata
    from ai_accounting.kernel.backup import copy_to_unpublished_database

    copy._require_actual(source, target, old, new, draft_fixture=True)
    identities = [tuple(row) for row in source.execute("SELECT * FROM identity")]
    allowed = {(1, identity[0], identity[1], identity[3]) for identity in PAGE_CASES}
    require(len(identities) == 1 and identities[0] in allowed
            and identities == [tuple(row) for row in target.execute("SELECT * FROM identity")],
            "Page copy requires the exact known synthetic business identity")
    require(source.in_transaction and source.execute("PRAGMA query_only").fetchone()[0] == 1,
            "Page source must own a read-only WAL snapshot")
    require(not target.in_transaction, "Page copy must own its target transaction")
    tables = copy._tables(target)
    nonempty = {name for name in tables if target.execute(
        f"SELECT 1 FROM {copy._quoted(name)} LIMIT 1"
    ).fetchone()}
    require(nonempty == copy._INITIAL_NONEMPTY
            and all([tuple(row) for row in copy._rows(target, name, copy._columns(target, name))]
                    == [expected] for name, expected in copy._INITIAL_ROWS.items())
            and all(target.execute(f"SELECT count(*) FROM {copy._quoted(name)}").fetchone()[0]
                    == 1 for name in copy.GENERATED),
            "Fresh page target has unexpected generated or seeded rows")
    require(target.execute("PRAGMA foreign_keys").fetchone()[0] == 1,
            "Page target must retain enabled foreign keys")
    guards = [(name, new[("trigger", name)]) for name in METADATA_TRIGGERS]
    copy_to_unpublished_database(source, target)
    # Only this unpublished copy changes installation metadata. Restore the exact
    # four saved guards before install_metadata's complete SQL fingerprint check.
    try:
        target.execute("BEGIN IMMEDIATE")
        for name, _ in guards:
            target.execute(f"DROP TRIGGER {copy._quoted(name)}")
        target.execute("DELETE FROM schema_meta")
        target.execute("DELETE FROM schema_history")
        for _, sql in guards:
            target.execute(sql)
        install_metadata(target, bundle, "company")
        target.commit()
    except BaseException:
        target.rollback()
        raise


def require(condition, message):
    if not condition:
        raise ValueError(message)


def readonly(path):
    connection = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def require_unchanged_sources(*sources):
    """Check live versions only after releasing the snapshots used for all copy proofs."""
    # In WAL mode data_version stays at the reader's snapshot until its transaction
    # ends. Digests/identity/epochs above belong to that original snapshot; do not
    # read business evidence from the new view opened by this final version check.
    for connection, _ in sources:
        connection.rollback()
    require(all(connection.execute("PRAGMA data_version").fetchone()[0] == version
                for connection, version in sources),
            "Live source database changed during copy")


def prepare(args):
    workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
    require(Path(sys.executable).resolve() == workspace / ".tmp-kernel-venv/Scripts/python.exe",
            "Use this workspace's repository virtual environment")
    source_tree = synthetic_path(args.source, workspace).resolve(strict=True)
    target_root = synthetic_path(args.target_root, workspace)
    staging = target_root.with_name(target_root.name + ".staging")
    report_path = args.book_report.resolve(strict=True)
    require(report_path.is_relative_to(workspace / ".tmp")
            and report_path.relative_to(workspace / ".tmp").parts[0].startswith("stage9-")
            and report_path.suffix == ".json", "Only a Stage 9 .tmp synthetic report is allowed")
    require(target_root.name.startswith("stage9-draft-fixture-")
            and not target_root.exists() and not staging.exists(),
            "Draft fixture and staging targets must be new dedicated paths")
    source_sha = require_manifest(source_tree, expected=args.source_sha256)
    require(copy._sha(report_path) == args.report_sha256, "Input report SHA changed")
    report = copy._read_json(report_path)
    require(report.get("status") in {"complete", "built_not_verified", "copied_content_unverified"},
            "Only a completed construction or previously qualified synthetic book is allowed")
    old_root = synthetic_path(Path(report["root"]), workspace).resolve(strict=True)
    old_tree = synthetic_path(Path(report["source"]), workspace).resolve(strict=True)
    require(len({old_root, old_tree, source_tree, target_root}) == 4,
            "Source book, source trees and new fixture must be distinct")
    old_sha = require_manifest(old_tree, historical=True)
    company = report["company"]
    identity = tuple(company[key] for key in ("id", "taxpayer_id", "name", "database_id"))
    require(identity in IDENTITIES, "Unknown synthetic company identity")
    distribution, objects, businesses, checkpoint_name = IDENTITIES[identity]
    months = report.get("requested_months")
    require(type(months) is int and months in {12, 48, 120}
            and (objects != 200 or months == 12)
            and report.get("distribution") == distribution
            and report.get("monthly_business_count") == businesses
            and report.get("business_count") == months * businesses
            and report.get("employee_count" if distribution == "mixed_cumulative"
                           else "registered_object_count") == objects,
            "Unknown or incomplete Stage 9 sample dimensions")
    copy_mode = getattr(args, "copy_mode", "rows")
    require_copy_mode(copy_mode, identity, months)
    period = validate_book_report(report, company_name=company["name"], require_verified=False,
                                  require_current_preview=False)
    dimensions = qualification_dimensions(report)
    checkpoint_path = old_root / checkpoint_name
    require(copy._sha(checkpoint_path) == args.checkpoint_sha256, "Input checkpoint SHA changed")
    checkpoint = copy._read_json(checkpoint_path)
    source_db = old_root / company["taxpayer_id"] / "company.sqlite"
    require(company["path"] == str(source_db) and checkpoint.get("company") == company
            and checkpoint.get("snapshots") == report["snapshots"]
            and all(checkpoint.get(key) == value for key, value in dimensions.items())
            and len(checkpoint.get("epochs", [])) == 6
            and all(type(value) is int for value in checkpoint["epochs"]),
            "Checkpoint identity, epochs or construction dimensions differ")
    if distribution == "mixed_cumulative":
        require(len(checkpoint.get("business_subjects", [])) == months * businesses,
                "Main/pressure checkpoint business subjects are incomplete")
    configure_source(source_tree, workspace)
    from ai_accounting.kernel import schema_bundle
    from ai_accounting.kernel.catalog import Catalog, catalog_sql
    from ai_accounting.kernel.permissions import reject_reparse_path
    from ai_accounting.kernel.schema import schema_sql
    from ai_accounting.kernel.storage import Store
    from ai_accounting.kernel.types import YearMonth
    from ai_accounting.kernel.versions import contract, verify_schema

    require_source_module(schema_bundle, source_tree, "src/ai_accounting/kernel/schema_bundle.py")
    for path in (source_tree, old_root, old_tree, target_root, staging, report_path,
                 checkpoint_path, source_db, old_root / "catalog.sqlite"):
        reject_reparse_path(path)
    bundle = schema_bundle.production_bundle()
    require(bundle.status == "draft" and bundle.current_versions == {"company": 0, "catalog": 0},
            "Target must use its sealed active draft/0 factory")
    old_bundle = schema_bundle.load_bundle(
        bundle.registry, old_tree / "src/ai_accounting/kernel/schema_contracts",
        family=bundle.family, application_id=bundle.application_id,
        status="released", current_versions={"company": 1, "catalog": 1},
    )
    for kind, fingerprint, sql in (("company", COMPANY, schema_sql(bundle.registry)),
                                   ("catalog", CATALOG, catalog_sql())):
        require(bundle.current(kind)["sha256"] == old_bundle.current(kind)["sha256"] == fingerprint
                and bundle.current(kind)["objects"] == old_bundle.current(kind)["objects"]
                and contract(sql) == bundle.current(kind)["objects"],
                "Only the identical complete c9/b484 SQL fixture branch is allowed")
    old, new = copy._map(old_bundle.current("company")), copy._map(bundle.current("company"))
    executor = Path(__file__).resolve()
    tool_paths = (executor, Path(copy.__file__).resolve(),
                  executor.with_name("reseed_stage9_book.py"))
    tool_shas = {str(path): copy._sha(path) for path in tool_paths}
    target_company = {
        **company, "path": str(target_root / company["taxpayer_id"] / "company.sqlite"),
    }
    provenance = {
        "mode": "synthetic_draft_fixture_construction_not_downgrade",
        "copy_mode": copy_mode,
        "source_root": str(old_root), "source_tree": str(old_tree),
        "source_snapshot_sha256": old_sha, "input_report": str(report_path),
        "input_report_sha256": args.report_sha256,
        "original_report_status": report.get("status"),
        "original_qualification_source": (report.get("verified_open_preview") or {}).get("source"),
        "input_checkpoint_sha256": args.checkpoint_sha256,
        "target_source": str(source_tree), "target_snapshot_sha256": source_sha,
        "source_company_format": old_bundle.database_format("company"),
        "target_company_format": bundle.database_format("company"),
        "tools_sha256": tool_shas, "old_qualification_inherited": False,
        "metadata_exceptions": ["schema_meta", "schema_history"],
        "source_catalog_security_imported": False,
    }
    with closing(readonly(old_root / "catalog.sqlite")) as old_catalog:
        verify_schema(old_catalog, bundle=old_bundle, kind="catalog")
        require([dict(row) for row in old_catalog.execute("SELECT * FROM company")] == [company]
                and not old_catalog.execute("SELECT 1 FROM company_setting LIMIT 1").fetchone(),
                "Source catalog is not the isolated single synthetic company")
        catalog_version = old_catalog.execute("PRAGMA data_version").fetchone()[0]
        with closing(readonly(source_db)) as source:
            verify_schema(source, bundle=old_bundle)
            data_version = source.execute("PRAGMA data_version").fetchone()[0]
            require([list(row) for row in source.execute("SELECT * FROM state")] == [
                checkpoint["epochs"]], "Source epochs changed")
            require([tuple(row) for row in source.execute("SELECT * FROM identity")] == [
                (1, company["id"], company["taxpayer_id"], company["database_id"])],
                "Source business identity changed")
            require(not source.execute("SELECT 1 FROM schema_draft_history LIMIT 1").fetchone(),
                    "Source draft ancestry must already be empty")
            closes = {str(YearMonth.from_ordinal(row[0])): row[1] for row in source.execute(
                "SELECT period,json_extract(manifest,'$.preview_digest') FROM period_close"
            )}
            require(closes == {key: value["preview_digest"] for key, value in
                               checkpoint["snapshots"].items() if value["closed"]},
                    "Source frozen close digests differ from construction checkpoint")
            staging.mkdir()
            try:
                catalog = Catalog(staging, bundle)
                staged_db = staging / company["taxpayer_id"] / "company.sqlite"
                staged_db.parent.mkdir()
                store = Store.create(staged_db, bundle, company["id"], company["taxpayer_id"],
                                     company["database_id"])
                with store.connection() as target:
                    if copy_mode == "pages":
                        _copy_pages(source, target, old, new, bundle)
                    else:
                        copy._copy_rows(source, target, old, new, draft_fixture=True)
                    verify_schema(target, bundle=bundle)
                    provenance["target_checkpoint"] = copy._checkpoint_new_target(
                        target, staging=staging, target_root=target_root,
                    )
                    _, source_digests, target_digests = copy._attest_copy(
                        source, target, old, new, draft_fixture=True,
                    )
                with catalog.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO company(id,taxpayer_id,name,path,database_id) "
                        "VALUES(?,?,?,?,?)",
                        tuple(target_company[key] for key in
                              ("id", "taxpayer_id", "name", "path", "database_id")),
                    )
                    connection.commit()
                copy._write_json_new(staging / checkpoint_name,
                                     {**checkpoint, "company": target_company})
                require(copy._sha(report_path) == args.report_sha256
                        and copy._sha(checkpoint_path) == args.checkpoint_sha256
                        and require_manifest(source_tree, expected=source_sha) == source_sha
                        and require_manifest(old_tree, expected=old_sha, historical=True) == old_sha
                        and all(copy._sha(Path(path)) == sha for path, sha in tool_shas.items()),
                        "Source bytes or tool/source inventory changed during copy")
                require_unchanged_sources((source, data_version), (old_catalog, catalog_version))
                result = {key: report[key] for key in (
                    "employee_count", "monthly_business_count", "business_count",
                    "requested_months", "distribution",
                )}
                if "registered_object_count" in report:
                    result["registered_object_count"] = report["registered_object_count"]
                result.update({
                    "status": "copied_content_unverified", "content_verified": False,
                    "source": str(source_tree), "workspace": str(workspace),
                    "root": str(target_root), "company": target_company,
                    "months": checkpoint["month_stats"], "snapshots": checkpoint["snapshots"],
                    "measurements": {}, "copy_provenance": provenance,
                    "counts": copy._expected_counts(target_digests),
                    "construction_integrity": {"final_full_verification_required": True},
                    "required_next_step": (
                        "fresh registered draft full verification and actual preview"
                    ),
                    "period": period,
                })
                copy._write_json_new(staging / "stage9-draft-copy-book.json", result)
                copy._write_json_new(staging / "stage9-draft-copy-attestation.json", {
                    "status": "copied_content_unverified", "provenance": provenance,
                    "all_source_table_digests": source_digests,
                    "all_target_table_digests": target_digests,
                    "target_checkpoint_sha256": copy._sha(staging / checkpoint_name),
                })
                require(not target_root.exists(), "Target appeared before publication")
                staging.rename(target_root)
                return {"status": result["status"], "content_verified": False,
                        "book_report": str(target_root / "stage9-draft-copy-book.json")}
            except BaseException:
                copy._write_json_new(staging / f"stage9-draft-copy-failed-{time.time_ns()}.json", {
                    "status": "failed_content_unverified", "provenance": provenance,
                    "traceback": traceback.format_exc(),
                })
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--book-report", type=Path, required=True)
    parser.add_argument("--report-sha256", required=True)
    parser.add_argument("--checkpoint-sha256", required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--copy-mode", choices=("rows", "pages"), default="rows",
                        help="pages is restricted to known independent120/pressure12 fixtures")
    print(json.dumps(prepare(parser.parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
