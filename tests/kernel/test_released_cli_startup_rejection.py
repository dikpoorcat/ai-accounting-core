"""Actual copied released-v1 CLI and daemon reject incompatible synthetic roots."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from schema_fixture import full_contract, write_contract

from ai_accounting.kernel.catalog import Catalog, catalog_sql
from ai_accounting.kernel.migration_steps import MigrationStep
from ai_accounting.kernel.permissions import create_private_file, ensure_private_directory
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import (
    APPLICATION_ID,
    FAMILY,
    load_bundle,
    verify_current_company,
)
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import upgrade, verify_schema


@pytest.fixture(scope="module")
def released_cli(tmp_path_factory):
    """Reuse the first-release exporter path without changing repository contracts."""
    source = tmp_path_factory.mktemp("released-cli-source")
    repository = Path(__file__).resolve().parents[2]
    package = source / "src/ai_accounting"
    shutil.copytree(
        repository / "src/ai_accounting",
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    selection = package / "kernel/schema_bundle.py"
    content = selection.read_text("utf-8").replace('STATUS = "draft"', 'STATUS = "released"')
    selection.write_text(content.replace("VERSION = 0", "VERSION = 1"), encoding="utf-8")
    contracts = package / "kernel/schema_contracts"
    for name in ("company/v1.json", "catalog/v1.json", "content-v1.json"):
        (contracts / name).unlink(missing_ok=True)
    script = source / "scripts/export_kernel_schema_contracts.py"
    script.parent.mkdir()
    shutil.copyfile(repository / "scripts/export_kernel_schema_contracts.py", script)
    environment = dict(os.environ, PYTHONPATH=str(source / "src"), PYTHONNOUSERSITE="1")
    environment.pop("FINANCE_DATA_ROOT", None)

    def run(*arguments):
        return subprocess.run(
            [sys.executable, "-B", "-X", "utf8", *arguments],
            cwd=source,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=40,
        )

    frozen = run(str(script), "--freeze-v1")
    assert frozen.returncode == 0, frozen.stderr
    checked = run(str(script), "--check")
    assert checked.returncode == 0, checked.stderr
    identity = run(
        "-c",
        "from ai_accounting.kernel.schema_bundle import production_bundle; "
        "b=production_bundle(); assert b.status=='released'; "
        "assert dict(b.current_versions)=={'company':1,'catalog':1}; "
        "assert {v for v,c in b.contracts['company'].items() if c['status']=='released'}=={1}; "
        "assert set(b.company_verifiers)=={1}; print('released-v1-only')",
    )
    assert identity.returncode == 0 and identity.stdout.strip() == "released-v1-only"
    return run, contracts


def bundle_for(contracts, *, status="released"):
    return load_bundle(
        default_registry(),
        contracts,
        family=FAMILY,
        application_id=APPLICATION_ID,
        status=status,
        current_versions={"company": 1, "catalog": 1}
        if status == "released"
        else {"company": 0, "catalog": 0},
        company_verifiers={1 if status == "released" else 0: verify_current_company},
    )


def draft_bundle(directory):
    registry = default_registry()
    write_contract(directory, full_contract(schema_sql(registry), kind="company", family=FAMILY))
    write_contract(directory, full_contract(catalog_sql(), kind="catalog", family=FAMILY))
    return bundle_for(directory, status="draft")


def migrate_isolated_company_to_v2(catalog, company, directory, released):
    """A valid test-only v2 company, never a contract in the copied production factory."""
    source = released.current("company")
    added_sql = "CREATE INDEX stage9_isolated_v2 ON fact_revision(subject_id)"
    target = full_contract(
        schema_sql(released.registry) + added_sql + ";",
        kind="company",
        status="released",
        version=2,
        family=FAMILY,
    )
    write_contract(directory, source)
    write_contract(directory, released.current("catalog"))
    write_contract(
        directory,
        {k: v for k, v in target.items() if k != "objects"}
        | {
            "base_version": 1,
            "base_sha256": source["sha256"],
            "add": [obj for obj in target["objects"] if obj not in source["objects"]],
            "remove": [],
            "replace": [],
        },
    )
    step = MigrationStep(
        FAMILY,
        "company",
        1,
        source["sha256"],
        2,
        target["sha256"],
        lambda connection: connection.execute(added_sql),
        lambda connection: None,
    )
    isolated = load_bundle(
        released.registry,
        directory,
        family=FAMILY,
        application_id=APPLICATION_ID,
        status="released",
        current_versions={"company": 2, "catalog": 1},
        steps=(step,),
        company_verifiers={1: verify_current_company, 2: verify_current_company},
    )
    with catalog.bind(company["id"]).connection() as connection:
        assert upgrade(
            connection,
            bundle=isolated,
            verify_source_content=lambda c: verify_current_company(c, released),
            verify_target=lambda c: verify_current_company(c, isolated),
        )
        assert verify_schema(connection, bundle=isolated) == 2


def root_snapshot(root):
    """Read exact source bytes and every identity/security row without service APIs."""
    files, rows = {}, {}
    for path in sorted(root.rglob("*.sqlite")):
        files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        with closing(connect(path, read_only=True)) as connection:
            names = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
            }
            selected = names & {
                "identity",
                "catalog_identity",
                "old_identity",
                "company",
                "schema_meta",
                "schema_history",
                "security_owner",
                "security_session",
                "security_audit",
                "security_recovery",
                "security_identity_import",
            }
            rows[path.relative_to(root).as_posix()] = {
                name: connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
                for name in sorted(selected)
            }
    return files, rows


@pytest.mark.parametrize(
    "case,code",
    [
        ("draft_catalog", "schema_version_unsupported"),
        ("draft_company", "schema_version_unsupported"),
        ("old_catalog", "schema_family_unsupported"),
        ("same_version_drift", "schema_fingerprint_mismatch"),
        ("mixed_company_versions", "schema_version_unsupported"),
    ],
)
@pytest.mark.parametrize("entry", ["call", "daemon"])
def test_released_cli_and_daemon_reject_without_changing_sources(
    tmp_path, released_cli, case, code, entry
):
    run, contracts = released_cli
    released = bundle_for(contracts)
    root = tmp_path / "root"
    ensure_private_directory(root, parents=True)
    if case == "old_catalog":
        create_private_file(root / "catalog.sqlite")
        with closing(sqlite3.connect(root / "catalog.sqlite")) as connection:
            connection.executescript(
                "PRAGMA user_version=3; CREATE TABLE old_identity(id TEXT); "
                "INSERT INTO old_identity VALUES('retain-old-catalog-id');"
            )
            connection.commit()
    elif case == "draft_catalog":
        Catalog(root, draft_bundle(tmp_path / "draft-contracts"))
    else:
        catalog = Catalog(root, released)
        if case == "draft_company":
            taxpayer = "91310000123456789A"
            path = root / taxpayer / "company.sqlite"
            ensure_private_directory(path.parent)
            # Register an independently created draft fixture; never replace a file.
            Store.create(
                path,
                draft_bundle(tmp_path / "draft-contracts"),
                "synthetic-draft-company",
                taxpayer,
                "synthetic-draft-database",
            )
            with catalog.connection() as connection:
                connection.execute(
                    "INSERT INTO company(id,taxpayer_id,name,path,database_id) VALUES(?,?,?,?,?)",
                    (
                        "synthetic-draft-company",
                        taxpayer,
                        "合成开发公司",
                        str(path),
                        "synthetic-draft-database",
                    ),
                )
                connection.commit()
        else:
            company = catalog.create_company("91310000123456789A", "合成拒绝启动公司")
            if case == "same_version_drift":
                with catalog.bind(company["id"]).connection() as connection:
                    connection.execute("CREATE INDEX unexpected_index ON state(accounting)")
                    connection.commit()
            else:
                other = catalog.create_company("91310000123456789B", "合成隔离v2公司")
                migrate_isolated_company_to_v2(catalog, other, tmp_path / "isolated-v2", released)
    before = root_snapshot(root)
    artifacts = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    arguments = ["-m", "ai_accounting.kernel.cli", "--root", str(root)]
    arguments.extend(["call", "companies"] if entry == "call" else ["daemon"])
    result = run(*arguments)
    assert result.returncode == 1, (result.stdout, result.stderr)
    response = json.loads(result.stdout)
    assert response["status"] == "rejected" and response["code"] == code, response
    assert root_snapshot(root) == before
    after = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    # The daemon may create its empty coordination lock before rejecting a company.
    assert after - artifacts <= {".resident.lock"}
    assert not (root / ".service.json").exists()
    assert not (root / ".service-start.lock").exists()
    assert all(
        not values
        for database in before[1].values()
        for name, values in database.items()
        if name.startswith("security_")
    )
