"""Copy a completed synthetic checkpoint without inheriting content qualification.

Only fresh Stage 9 roots beside a sealed source snapshot are accepted. SQLite
backup preserves the source snapshot; company identity and business rows are
never recalculated. Each resulting book still requires its own full verifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from contextlib import ExitStack, closing
from pathlib import Path

if __package__:
    from .reseed_stage9_book import _columns, _objects, _table_digest, _tables
    from .snapshot_stage9_source import _digest, _inventory
    from .stage9_source import configure_source, require_source_module
else:
    from reseed_stage9_book import _columns, _objects, _table_digest, _tables
    from snapshot_stage9_source import _digest, _inventory
    from stage9_source import configure_source, require_source_module

IDENTITIES = {
    ("阶段九合成规模企业", "91310000123456789S"): (
        "mixed_cumulative",
        "stage9-builder.json",
        "employees_count",
        "employee_count",
    ),
    ("阶段九合成独立业务企业", "91310000123456789I"): (
        "independent_local_pairs",
        "stage9-independent-builder.json",
        "objects_count",
        "registered_object_count",
    ),
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), "Expected a JSON object")
    return value


def _sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _safe_path(path, workspace, *, directory=False):
    # Reject links before resolution, including a junction in any ancestor.
    selected = _reject_path(path)
    temporary = _reject_path(workspace / ".tmp")
    _require(
        selected.parent == temporary and selected.name.startswith("stage9-"),
        "Only direct Stage 9 synthetic paths under workspace .tmp are allowed",
    )
    if directory:
        _require(selected.is_dir(), "Source synthetic directory is missing")
    return selected


def _reject_path(path):
    selected = Path(os.path.abspath(path))
    for candidate in (*reversed(selected.parents), selected):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        _require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "Synthetic copy rejects symlink/reparse paths",
        )
    return selected


def _manifest(source):
    manifest = _json(_reject_path(source / "source-manifest.json"))
    files = manifest.get("files")
    _require(
        manifest.get("status") == "complete"
        and manifest.get("target") == str(source)
        and isinstance(files, dict)
        and bool(files)
        and manifest.get("file_count") == len(files)
        and manifest.get("sha256") == _digest(files),
        "Source manifest is incomplete or changed",
    )
    for name in files:
        _require(
            isinstance(name, str) and not Path(name).is_absolute() and ".." not in Path(name).parts,
            "Source manifest path escaped",
        )
        _reject_path(source / name)
    _require(_inventory(source) == files, "Fixed source inventory changed")
    return manifest["sha256"]


def _readonly(path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _digests(connection):
    return {
        name: dict(
            zip(
                ("rows", "sha256"),
                _table_digest(connection, name, _columns(connection, name)),
                strict=True,
            )
        )
        for name in _tables(connection)
    }


def _check_sqlite(connection):
    _require(
        connection.execute("PRAGMA foreign_key_check").fetchone() is None,
        "Copied database violates foreign keys",
    )
    _require(
        connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
        "Copied database fails SQLite integrity",
    )


def _write_new(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _copy_database(original, destination, *, kind, bundle):
    from ai_accounting.kernel.backup import copy_to_unpublished_database
    from ai_accounting.kernel.permissions import create_private_file
    from ai_accounting.kernel.versions import verify_schema

    create_private_file(destination)
    with closing(sqlite3.connect(destination)) as target:
        target.execute("PRAGMA foreign_keys=ON")
        copy_to_unpublished_database(original, target)
        verify_schema(target, bundle=bundle, kind=kind)
        _require(_objects(original) == _objects(target), "Copied actual SQL differs")
        _check_sqlite(target)
        return _digests(target)


def _require_checkpoint(report, checkpoint, company):
    identity = (company.get("name"), company.get("taxpayer_id"))
    _require(identity in IDENTITIES, "Unknown synthetic company identity")
    distribution, _, checkpoint_count, report_count = IDENTITIES[identity]
    months, snapshots = report.get("months"), report.get("snapshots")
    requested, businesses = report.get("requested_months"), report.get("monthly_business_count")
    _require(
        report.get("status") in {"built_not_verified", "complete"}
        and report.get("distribution") == checkpoint.get("distribution") == distribution
        and checkpoint.get("company") == company
        and checkpoint.get("month_stats") == months
        and checkpoint.get("snapshots") == snapshots
        and type(requested) is int
        and requested > 0
        and isinstance(months, list)
        and len(months) == requested
        and isinstance(snapshots, dict)
        and len(snapshots) == requested
        and type(businesses) is int
        and businesses > 0
        and checkpoint.get("businesses") == businesses
        and report.get("business_count") == requested * businesses
        and type(report.get(report_count)) is int
        and report[report_count] > 0
        and checkpoint.get(checkpoint_count) == report[report_count]
        and isinstance(checkpoint.get("epochs"), list)
        and all(type(value) is int for value in checkpoint["epochs"]),
        "Report/checkpoint identity, dimensions or completed months differ",
    )
    count = (
        len(checkpoint.get("business_subjects", []))
        if distribution == "mixed_cumulative"
        else checkpoint.get("business_count")
    )
    _require(count == requested * businesses, "Checkpoint business count is incomplete")
    for index, month in enumerate(months):
        period = f"{2016 + index // 12:04}-{index % 12 + 1:02}"
        snapshot = snapshots.get(period, {})
        _require(
            month.get("period") == period
            and month.get("business_count") == businesses
            and month.get("closed") is (index < requested - 1)
            and snapshot.get("closed") is month["closed"]
            and bool(snapshot.get("owner_confirmation"))
            and bool(snapshot.get("preview_digest")),
            "Incomplete month or invalid open/closed sequence",
        )


def copy_checkpoint(
    source_root: Path, target_root: Path, source_report: Path, source: Path, workspace: Path
) -> dict:
    """Retain an exact construction boundary for independent later verification."""
    workspace = _reject_path(workspace)
    _require(
        workspace.is_absolute() and (workspace / ".tmp").is_dir(),
        "Workspace must have an existing .tmp directory",
    )
    source = _safe_path(source, workspace, directory=True)
    source_root = _safe_path(source_root, workspace, directory=True)
    target_root = _safe_path(target_root, workspace)
    source_report = _reject_path(source_report)
    relative = source_report.relative_to(workspace / ".tmp")
    _require(
        relative.parts[0].startswith("stage9-")
        and source_report.suffix == ".json"
        and source_report.is_file(),
        "Only a synthetic Stage 9 JSON report is allowed",
    )
    _require(
        len({source, source_root, target_root}) == 3 and not target_root.exists(),
        "Copy target must be absent and distinct from source roots",
    )
    source_sha = _manifest(source)
    executors = [
        Path(__file__),
        *(
            Path(sys.modules[function.__module__].__file__)
            for function in (_columns, _inventory, configure_source)
        ),
    ]
    _require(
        all(_sha(path) == _sha(source / "scripts" / path.name) for path in executors),
        "Copy executor/helpers must match the fixed source",
    )
    configure_source(source, workspace)
    from ai_accounting.kernel import schema_bundle
    from ai_accounting.kernel.permissions import reject_reparse_path
    from ai_accounting.kernel.types import YearMonth
    from ai_accounting.kernel.versions import verify_schema

    require_source_module(schema_bundle, source, "src/ai_accounting/kernel/schema_bundle.py")
    bundle = schema_bundle.production_bundle()
    report_sha = _sha(source_report)
    report = _json(source_report)
    _require(
        report.get("root") == str(source_root) and report.get("source") == str(source),
        "Report must name the same absolute synthetic root and fixed source",
    )
    company = report.get("company", {})
    identity = company.get("name"), company.get("taxpayer_id")
    _require(identity in IDENTITIES, "Unknown synthetic company identity")
    checkpoint_name = IDENTITIES[identity][1]
    checkpoint_path = reject_reparse_path(source_root / checkpoint_name)
    checkpoint_sha = _sha(checkpoint_path)
    checkpoint = _json(checkpoint_path)
    _require_checkpoint(report, checkpoint, company)
    source_db = reject_reparse_path(source_root / company["taxpayer_id"] / "company.sqlite")
    catalog_db = reject_reparse_path(source_root / "catalog.sqlite")
    _require(company.get("path") == str(source_db), "Company path differs from its synthetic root")
    target_company = {
        **company,
        "path": str(target_root / company["taxpayer_id"] / "company.sqlite"),
    }
    provenance = {
        "mode": "same_source_synthetic_checkpoint_copy",
        "source_root": str(source_root),
        "source_report": str(source_report),
        "source_report_sha256": report_sha,
        "source_checkpoint_sha256": checkpoint_sha,
        "source_snapshot_sha256": source_sha,
        "old_qualification_inherited": False,
        "relocated_fields": ["catalog.company.path", "checkpoint.company.path"],
    }
    with ExitStack() as stack:
        catalog = stack.enter_context(closing(_readonly(catalog_db)))
        business = stack.enter_context(closing(_readonly(source_db)))
        versions = [
            (connection, connection.execute("PRAGMA data_version").fetchone()[0])
            for connection in (catalog, business)
        ]
        verify_schema(catalog, bundle=bundle, kind="catalog")
        verify_schema(business, bundle=bundle)
        _require(
            [dict(row) for row in catalog.execute("SELECT * FROM company")] == [company]
            and not catalog.execute("SELECT 1 FROM company_setting LIMIT 1").fetchone()
            and not catalog.execute(
                "SELECT 1 FROM company_operation WHERE status!='succeeded' LIMIT 1"
            ).fetchone(),
            "Source catalog must contain only the isolated completed synthetic company",
        )
        _require(
            [list(row) for row in business.execute("SELECT * FROM state")] == [checkpoint["epochs"]]
            and [tuple(row) for row in business.execute("SELECT * FROM identity")]
            == [(1, company["id"], company["taxpayer_id"], company["database_id"])],
            "Source identity or epochs changed after checkpoint",
        )
        closes = {
            str(YearMonth.from_ordinal(row[0])): row[1]
            for row in business.execute(
                "SELECT period,json_extract(manifest,'$.preview_digest') FROM period_close"
            )
        }
        _require(
            closes
            == {
                period: item["preview_digest"]
                for period, item in checkpoint["snapshots"].items()
                if item["closed"]
            },
            "Frozen close digests differ from checkpoint",
        )
        from ai_accounting.kernel.permissions import ensure_private_directory

        # Reserve exclusively before permission setup; a concurrently appearing
        # target must never be adopted as our unpublished directory.
        target_root.mkdir(mode=0o700)
        try:
            ensure_private_directory(target_root)
            original_digests = {"catalog": _digests(catalog), "company": _digests(business)}
            ensure_private_directory(target_root / company["taxpayer_id"])
            copied = {
                "catalog": _copy_database(
                    catalog, target_root / "catalog.sqlite", kind="catalog", bundle=bundle
                ),
                "company": _copy_database(
                    business, Path(target_company["path"]), kind="company", bundle=bundle
                ),
            }
            _require(copied == original_digests, "Copied rowid or saved content differs")
            with closing(sqlite3.connect(target_root / "catalog.sqlite")) as target_catalog:
                target_catalog.execute("PRAGMA foreign_keys=ON")
                target_catalog.execute(
                    "UPDATE company SET path=? WHERE id=?", (target_company["path"], company["id"])
                )
                target_catalog.commit()
                _require(
                    [
                        dict(
                            zip(
                                ("id", "taxpayer_id", "name", "path", "database_id"),
                                row,
                                strict=True,
                            )
                        )
                        for row in target_catalog.execute(
                            "SELECT id,taxpayer_id,name,path,database_id FROM company"
                        )
                    ]
                    == [target_company],
                    "Relocated catalog company differs",
                )
                _check_sqlite(target_catalog)
                relocated = _digests(target_catalog)
                _require(
                    all(
                        relocated[name] == copied["catalog"][name]
                        for name in relocated
                        if name != "company"
                    ),
                    "Catalog relocation changed unrelated content",
                )
                copied["catalog"] = relocated
            relocated_checkpoint = {**checkpoint, "company": target_company}
            for key, value in (("root", str(target_root)), ("source", str(source))):
                if key in relocated_checkpoint:
                    relocated_checkpoint[key] = value
            _write_new(target_root / checkpoint_name, relocated_checkpoint)
            _require(
                _sha(source_report) == report_sha
                and _sha(checkpoint_path) == checkpoint_sha
                and _manifest(source) == source_sha,
                "Source files changed during copy",
            )
            for connection, version in versions:
                connection.rollback()
                _require(
                    connection.execute("PRAGMA data_version").fetchone()[0] == version,
                    "Live source database changed during copy",
                )
            result = {
                key: report[key]
                for key in (
                    "employee_count",
                    "monthly_business_count",
                    "business_count",
                    "requested_months",
                    "distribution",
                    "months",
                    "snapshots",
                    "counts",
                    "construction_integrity",
                )
                if key in report
            }
            if "registered_object_count" in report:
                result["registered_object_count"] = report["registered_object_count"]
            result.update(
                status="built_not_verified",
                content_verified=False,
                source=str(source),
                workspace=str(workspace),
                root=str(target_root),
                company=target_company,
                copy_provenance=provenance,
                required_next_step="fresh registered full verification and actual open preview",
            )
            result["construction_integrity"] = {
                **result.get("construction_integrity", {}),
                "final_full_verification_required": True,
            }
            _write_new(
                target_root / "stage9-checkpoint-copy-attestation.json",
                {
                    "status": "built_not_verified",
                    "provenance": provenance,
                    "source_table_digests": original_digests,
                    "target_table_digests": copied,
                },
            )
            pending_report = target_root / "stage9-checkpoint-copy-book.json.pending"
            _write_new(pending_report, result)
            pending_report.rename(target_root / "stage9-checkpoint-copy-book.json")
            return result
        except BaseException as error:
            _write_new(
                target_root / "stage9-checkpoint-copy-failed.json",
                {
                    "status": "failed_content_unverified",
                    "provenance": provenance,
                    "error": {"type": type(error).__name__, "message": str(error)},
                },
            )
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "target-root", "source-report", "source", "workspace"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    result = copy_checkpoint(**vars(args))
    print(
        json.dumps(
            {
                "status": result["status"],
                "root": result["root"],
                "book_report": str(Path(result["root"]) / "stage9-checkpoint-copy-book.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
