"""Prepare only the pinned r22 main120 fixture for an explicit released/1 source.

This copies synthetic rows without recalculation; it is not a database upgrade.
Both check-only and copied outputs remain content-unverified. Run the registered
verifier in a separate process against the copied root before browser timing.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import traceback
from contextlib import closing
from pathlib import Path

import rebase_stage9_synthetic_book as original
import reseed_stage9_book as reseed
from snapshot_stage9_source import _digest, _inventory
from stage9_source import configure_source, require_source_module, synthetic_path, workspace_root

CASE_NAME = "stage9-release-main-120"
CASE = original.CASES[CASE_NAME]


def require_manifest(tree, *, expected=None, historical=False):
    """Check historical listed files without imposing a later inventory layout."""
    manifest = original._read_json(tree / "source-manifest.json")
    files = manifest.get("files")
    if (
        manifest.get("status") != "complete"
        or manifest.get("target") != str(tree)
        or not isinstance(files, dict)
        or not files
        or manifest.get("file_count") != len(files)
        or manifest.get("sha256") != _digest(files)
        or (expected is not None and manifest["sha256"] != expected)
    ):
        raise ValueError("Incomplete or changed source manifest")
    for name, sha in files.items():
        if not isinstance(name, str):
            raise ValueError("Manifest path must be text")
        path = tree / name
        if (
            not isinstance(name, str)
            or Path(name).is_absolute()
            or ".." in Path(name).parts
            or path.is_symlink()
            or not path.resolve().is_relative_to(tree)
            or not path.is_file()
            or original._sha(path) != sha
        ):
            raise ValueError("Manifest file escaped, disappeared or changed")
    if not historical and _inventory(tree) != files:
        raise ValueError("Current candidate inventory differs from its manifest")
    return manifest["sha256"]


def selected_paths(args, workspace):
    old_root = (workspace / ".tmp" / CASE_NAME).resolve(strict=True)
    old_tree = (workspace / ".tmp" / CASE["source_tree"]).resolve(strict=True)
    report = (workspace / ".tmp" / CASE["report"]).resolve(strict=True)
    target = synthetic_path(args.target_root, workspace)
    candidate = synthetic_path(args.target_source, workspace)
    if (
        not target.name.startswith("stage9-main120-candidate-")
        or target.exists()
        or target.with_name(target.name + ".staging").exists()
        or candidate in {old_root, old_tree}
        or target == candidate
    ):
        raise ValueError("Use a new main120 candidate root and a distinct fixed source")
    candidate.resolve(strict=True)
    return old_root, old_tree, report, target, candidate


def require_factory_sql(bundle):
    from ai_accounting.kernel.catalog import catalog_sql
    from ai_accounting.kernel.schema import schema_sql
    from ai_accounting.kernel.versions import contract

    if bundle.status != "released" or bundle.current_versions != {"company": 1, "catalog": 1}:
        raise ValueError("Current candidate must be released/1")
    for kind, sql in (("company", schema_sql(bundle.registry)), ("catalog", catalog_sql())):
        if contract(sql) != bundle.current(kind)["objects"]:
            raise ValueError("Current factory SQL differs from its declared contract")


def require_copy_helpers(candidate):
    for module in (original, reseed):
        helper = Path(module.__file__)
        if original._sha(helper) != original._sha(candidate / "scripts" / helper.name):
            raise ValueError("Copy helpers differ from the fixed current candidate")


def prepare(args):
    workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
    old_root, old_tree, report_path, target_root, candidate = selected_paths(args, workspace)
    _, checkpoint_name, checkpoint, _, period = original._source_guard(
        old_root, report_path, old_tree, CASE
    )
    old_sha = require_manifest(old_tree, expected=CASE["source_snapshot"], historical=True)
    target_sha = require_manifest(candidate)
    require_copy_helpers(candidate)
    content_path = Path("src/ai_accounting/kernel/schema_contracts/content-v1.json")
    if original._read_json(old_tree / content_path)["sha256"] != CASE["source_content"]:
        raise ValueError("Pinned historical content contract changed")
    configure_source(candidate, workspace)
    from ai_accounting.kernel import schema_bundle
    from ai_accounting.kernel.catalog import Catalog
    from ai_accounting.kernel.permissions import reject_reparse_path
    from ai_accounting.kernel.storage import Store
    from ai_accounting.kernel.types import YearMonth
    from ai_accounting.kernel.versions import verify_schema

    require_source_module(schema_bundle, candidate, "src/ai_accounting/kernel/schema_bundle.py")
    for path in (old_root, old_tree, report_path, target_root, candidate):
        reject_reparse_path(path)
    reject_reparse_path(old_root / "catalog.sqlite")
    reject_reparse_path(old_root / checkpoint["company"]["taxpayer_id"] / "company.sqlite")
    bundle = schema_bundle.production_bundle()
    require_factory_sql(bundle)
    old_bundle = schema_bundle.load_bundle(
        bundle.registry,
        old_tree / "src/ai_accounting/kernel/schema_contracts",
        family=schema_bundle.FAMILY,
        application_id=schema_bundle.APPLICATION_ID,
        status="released",
        current_versions={"company": 1, "catalog": 1},
    )
    if (
        old_bundle.current("company")["sha256"] != CASE["company_contract"]
        or old_bundle.current("catalog")["sha256"] != original.CATALOG
        or bundle.current("catalog")["sha256"] != original.CATALOG
    ):
        raise ValueError("Pinned company or unchanged catalog contract differs")
    old, new = (
        original._map(old_bundle.current("company")),
        original._map(bundle.current("company")),
    )
    original._require_delta(old, new, pre_cover=True)
    company = checkpoint["company"]
    with closing(
        sqlite3.connect((old_root / "catalog.sqlite").as_uri() + "?mode=ro", uri=True)
    ) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN")
        verify_schema(c, bundle=old_bundle, kind="catalog")
        if [dict(row) for row in c.execute("SELECT * FROM company")] != [company]:
            raise ValueError("Pinned catalog company changed")
        if c.execute("SELECT 1 FROM company_setting LIMIT 1").fetchone():
            raise ValueError("Source contains external company settings")
        omitted_security = {
            name: c.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("security_owner", "security_session", "security_audit")
        }
    provenance = {
        "mode": "synthetic_raw_copy_not_migration",
        "source_root": str(old_root),
        "source_tree": str(old_tree),
        "source_report_sha256": original._sha(report_path),
        "source_checkpoint_sha256": original._sha(old_root / checkpoint_name),
        "source_snapshot_sha256": old_sha,
        "target_snapshot_sha256": target_sha,
        "target_source": str(candidate),
        "source_status": CASE["status"],
        "source_content_contract": CASE["source_content"],
        "target_content_contract": original._read_json(candidate / content_path)["sha256"],
        "source_company_contract": CASE["company_contract"],
        "target_company_contract": bundle.current("company")["sha256"],
        "omitted_source_catalog_security_rows": omitted_security,
        "executor_sha256": original._sha(Path(__file__).resolve()),
        "copy_helpers_sha256": original._sha(Path(original.__file__)),
    }
    source_db = old_root / company["taxpayer_id"] / "company.sqlite"
    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        verify_schema(source, bundle=old_bundle)
        if original._objects(source) != old:
            raise ValueError("Actual historical SQL differs from its pinned contract")
        if [tuple(row) for row in source.execute("SELECT * FROM state")] != [
            tuple(checkpoint["epochs"])
        ]:
            raise ValueError("Historical epochs changed")
        if any(
            source.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
            for name in ("jobs", "security_close_approval")
        ):
            raise ValueError("Source contains delivery or approval references")
        closes = {
            str(YearMonth.from_ordinal(row[0])): row[1]
            for row in source.execute(
                "SELECT period,json_extract(manifest,'$.preview_digest') FROM period_close"
            )
        }
        if closes != {
            p: s["preview_digest"] for p, s in checkpoint["snapshots"].items() if s["closed"]
        }:
            raise ValueError("Frozen closes differ from pinned checkpoint")
        if args.check_only:
            return {
                "status": "checked_content_unverified",
                "content_verified": False,
                "target_created": False,
                "provenance": provenance,
            }
        target_root.mkdir()
        try:
            catalog = Catalog(target_root, bundle)
            target_db = target_root / company["taxpayer_id"] / "company.sqlite"
            target_db.parent.mkdir()
            target_company = {**company, "path": str(target_db)}
            store = Store.create(
                target_db, bundle, company["id"], company["taxpayer_id"], company["database_id"]
            )
            with store.connection() as target:
                original._copy_rows(source, target, old, new, pre_cover=True)
                verify_schema(target, bundle=bundle)
                _, source_digests, target_digests = original._attest_copy(
                    source, target, old, new, pre_cover=True
                )
            original._source_guard(old_root, report_path, old_tree, CASE)
            require_manifest(old_tree, expected=old_sha, historical=True)
            require_manifest(candidate, expected=target_sha)
            with catalog.connection() as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "INSERT INTO company(id,taxpayer_id,name,path,database_id) VALUES(?,?,?,?,?)",
                    tuple(
                        target_company[k]
                        for k in ("id", "taxpayer_id", "name", "path", "database_id")
                    ),
                )
                c.commit()
            original._write_json_new(
                target_root / checkpoint_name, {**checkpoint, "company": target_company}
            )
            result = {
                "status": "copied_content_unverified",
                "content_verified": False,
                "source": str(candidate),
                "root": str(target_root),
                "period": period,
                "provenance": provenance,
                "all_source_table_digests": source_digests,
                "all_target_table_digests": target_digests,
                "required_next_step": (
                    "independent benchmark_stage9.py --read-existing --verify-only"
                ),
            }
            original._write_json_new(target_root / "stage9-main120-copy-report.json", result)
            return result
        except BaseException as error:
            original._write_json_new(
                target_root / f"stage9-main120-failed-{time.time_ns()}.json",
                {
                    "status": "failed_content_unverified",
                    "content_verified": False,
                    "error": str(error),
                    "traceback": traceback.format_exc(),
                    "provenance": provenance,
                },
            )
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--target-source", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
