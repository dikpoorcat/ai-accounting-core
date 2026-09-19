from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from ai_accounting.kernel import backup, runtime
from ai_accounting.kernel.catalog import Catalog, catalog_sql
from ai_accounting.kernel.diagnostics import error_response
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.migration_steps import MigrationStep
from ai_accounting.kernel.permissions import PrivatePathError
from ai_accounting.kernel.schema import initialize, schema_sql
from ai_accounting.kernel.schema_bundle import (
    APPLICATION_ID,
    load_bundle,
    production_bundle,
    verify_current_company,
)
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import contract, fingerprint

TAXPAYER = "91330100MA00000001"
COMPANY = "company-a"
DATABASE = "database-a"
FAMILY = "ai-accounting-kernel-backup-test/2"


@pytest.fixture
def company(tmp_path: Path) -> Path:
    path = tmp_path / "company.sqlite"
    connection = runtime.connect(path)
    try:
        initialize(connection, production_bundle(), COMPANY, TAXPAYER, DATABASE)
    finally:
        connection.close()
    return path


@pytest.fixture
def portable(company: Path, tmp_path: Path) -> Path:
    return Path(backup.create_portable(company, tmp_path / "backups")["path"])


def entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {item.filename: archive.read(item) for item in archive.infolist()}


def rewrite_manifest(source: Path, target: Path, change) -> None:
    content = entries(source)
    manifest = json.loads(content[backup.MANIFEST_MEMBER])
    change(manifest)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(backup.MANIFEST_MEMBER, json.dumps(manifest))
        archive.writestr(backup.DATABASE_MEMBER, content[backup.DATABASE_MEMBER])


def test_manifest_v2_has_exact_format_identity_and_size(portable: Path) -> None:
    content = entries(portable)
    manifest = json.loads(content[backup.MANIFEST_MEMBER])
    assert set(manifest) == backup._MANIFEST_KEYS
    assert manifest["format"] == backup.FORMAT
    assert manifest["format_version"] == 2
    assert set(manifest["database_format"]) == backup._DATABASE_FORMAT_KEYS
    current = production_bundle().current("company")
    assert manifest["database_format"] == {
        key: current[key] for key in ("family", "kind", "status", "version")
    } | {"fingerprint": current["sha256"]}
    assert set(manifest["identity"]) == {"company_id", "taxpayer_id", "database_id"}
    assert manifest["database_bytes"] == len(content[backup.DATABASE_MEMBER])
    verified = backup.verify_portable(portable)
    assert verified["database_format"] == manifest["database_format"]
    assert verified["identity"] == manifest["identity"]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda value: value.__setitem__("format_version", 1), "backup_format_unsupported"),
        (lambda value: value.__setitem__("unexpected", True), "backup_manifest_invalid"),
        (
            lambda value: value["database_format"].__setitem__("family", "old-family"),
            "backup_format_unsupported",
        ),
        (
            lambda value: value["database_format"].__setitem__("fingerprint", "0" * 64),
            "backup_schema_unsupported",
        ),
        (
            lambda value: value["database_format"].update(status="released", version=1),
            "backup_schema_unsupported",
        ),
        (lambda value: value["identity"].pop("database_id"), "backup_manifest_invalid"),
        (lambda value: value.__setitem__("database_bytes", 1), "backup_manifest_invalid"),
    ],
)
def test_manifest_contract_rejects_old_or_inexact_packages(
    portable: Path, tmp_path: Path, change, code: str
) -> None:
    forged = tmp_path / f"{code}.zip"
    rewrite_manifest(portable, forged, change)
    with pytest.raises(backup.BackupError) as rejected:
        backup.verify_portable(forged)
    assert rejected.value.code == code


def test_old_format_is_rejected_before_database_extraction(
    portable: Path, tmp_path: Path, monkeypatch
) -> None:
    old = tmp_path / "old.zip"
    rewrite_manifest(portable, old, lambda value: value.__setitem__("format_version", 1))
    extracted = False

    def unexpected_extract(_path):
        nonlocal extracted
        extracted = True
        raise AssertionError("database member was extracted")

    monkeypatch.setattr(backup, "create_private_file", unexpected_extract)
    with pytest.raises(backup.BackupError) as rejected:
        backup.verify_portable(old)
    assert rejected.value.code == "backup_format_unsupported"
    assert not extracted


def test_catalog_restore_rejects_linked_archive_input(portable: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked.finance-company.zip"
    try:
        linked.symlink_to(portable)
    except OSError:
        pytest.skip("platform does not permit synthetic symlinks")

    catalog = Catalog(tmp_path / "target")
    with pytest.raises(PrivatePathError):
        catalog.restore_company(str(linked), taxpayer_id=TAXPAYER, name="合成公司")
    assert catalog.operations() == []


@pytest.mark.parametrize(
    "code",
    [
        "backup_target_exists",
        "restore_target_exists",
        "backup_identity_mismatch",
        "backup_content_invalid",
        "backup_manifest_invalid",
        "backup_format_unsupported",
        "backup_schema_unsupported",
    ],
)
def test_backup_error_keeps_stable_diagnostic_code_without_details(code) -> None:
    secret = "synthetic-secret-token-and-private-path"
    response = error_response(backup.BackupError(code, secret))
    assert response["status"] == "rejected"
    assert response["code"] == code
    assert secret not in str(response)
    assert any("\u4e00" <= character <= "\u9fff" for character in response["message"])


def full_contract(script: str, *, kind: str, version: int) -> dict:
    objects = contract(script)
    return {
        "family": FAMILY,
        "kind": kind,
        "status": "released",
        "version": version,
        "application_id": APPLICATION_ID,
        "objects": objects,
        "sha256": fingerprint(objects).hex(),
    }


def write_contract(directory: Path, value: dict) -> None:
    folder = directory / value["kind"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"v{value['version']}.json").write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )


def incremental_contract(base: dict, target: dict) -> dict:
    before = {(item["type"], item["name"]): item for item in base["objects"]}
    after = {(item["type"], item["name"]): item for item in target["objects"]}
    added = after.keys() - before.keys()
    removed = before.keys() - after.keys()
    replaced = {key for key in before.keys() & after.keys() if before[key] != after[key]}
    return {
        key: target[key]
        for key in ("family", "kind", "status", "version", "application_id", "sha256")
    } | {
        "base_version": base["version"],
        "base_sha256": base["sha256"],
        "add": [after[key] for key in sorted(added)],
        "remove": [
            {"type": before[key]["type"], "name": before[key]["name"]} for key in sorted(removed)
        ],
        "replace": [after[key] for key in sorted(replaced)],
    }


def released_bundles(directory: Path):
    registry = default_registry()
    company_v1 = full_contract(schema_sql(registry), kind="company", version=1)
    company_v2 = full_contract(
        schema_sql(registry) + "CREATE INDEX evidence_name_v2 ON evidence(name);",
        kind="company",
        version=2,
    )
    catalog_v1 = full_contract(catalog_sql(), kind="catalog", version=1)
    catalog_v2 = full_contract(catalog_sql(), kind="catalog", version=2)
    for value in (
        company_v1,
        incremental_contract(company_v1, company_v2),
        catalog_v1,
        incremental_contract(catalog_v1, catalog_v2),
    ):
        write_contract(directory, value)
    step = MigrationStep(
        FAMILY,
        "company",
        1,
        company_v1["sha256"],
        2,
        company_v2["sha256"],
        lambda connection: connection.execute("CREATE INDEX evidence_name_v2 ON evidence(name)"),
        lambda connection: connection.execute("SELECT count(*) FROM evidence").fetchone(),
    )
    options = {
        "family": FAMILY,
        "application_id": APPLICATION_ID,
        "status": "released",
        "steps": (step,),
        "company_verifiers": {
            1: verify_current_company,
            2: verify_current_company,
        },
    }
    source = load_bundle(
        registry,
        directory,
        current_versions={"company": 1, "catalog": 1},
        **options,
    )
    target = load_bundle(
        registry,
        directory,
        current_versions={"company": 2, "catalog": 2},
        **options,
    )
    return source, target


def test_released_restore_verifies_source_then_migrates_and_reverifies(tmp_path: Path) -> None:
    source_bundle, target_bundle = released_bundles(tmp_path / "contracts")
    source = tmp_path / "source.sqlite"
    connection = runtime.connect(source)
    try:
        initialize(connection, source_bundle, COMPANY, TAXPAYER, DATABASE)
    finally:
        connection.close()
    evidence = b"synthetic released migration evidence"
    registered = Engine(Store(source, source_bundle, COMPANY, DATABASE)).register_evidence(
        evidence,
        "application/pdf",
        "migration-evidence.pdf",
        request_id="released-migration-evidence",
    )
    package = Path(
        backup.create_portable(source, tmp_path / "backups", _bundle=source_bundle)["path"]
    )
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    package_sha256 = hashlib.sha256(package.read_bytes()).hexdigest()
    assert backup.verify_portable(package, _bundle=target_bundle)["database_format"]["version"] == 1

    restored = tmp_path / "restored.sqlite"
    result = backup.restore_portable(package, restored, _bundle=target_bundle)
    assert result["source_database_format"]["version"] == 1
    assert result["database_format"]["version"] == 2
    assert result["identity"] == {
        "company_id": COMPANY,
        "taxpayer_id": TAXPAYER,
        "database_id": DATABASE,
    }
    with sqlite3.connect(restored) as connection:
        assert connection.execute(
            "SELECT content,media_type,name FROM evidence WHERE digest=?",
            (bytes.fromhex(registered["digest"]),),
        ).fetchone() == (evidence, "application/pdf", "migration-evidence.pdf")
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='index' AND name='evidence_name_v2'"
        ).fetchone()
        assert [row[0] for row in connection.execute("SELECT version FROM schema_history")] == [
            1,
            2,
        ]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha256
    assert hashlib.sha256(package.read_bytes()).hexdigest() == package_sha256


def test_released_restore_requires_source_content_verifier_and_is_atomic(tmp_path: Path) -> None:
    source_bundle, target_bundle = released_bundles(tmp_path / "contracts")
    source = tmp_path / "source.sqlite"
    connection = runtime.connect(source)
    try:
        initialize(connection, source_bundle, COMPANY, TAXPAYER, DATABASE)
    finally:
        connection.close()
    package = Path(
        backup.create_portable(source, tmp_path / "backups", _bundle=source_bundle)["path"]
    )
    target_bundle = replace(
        target_bundle,
        company_verifiers=MappingProxyType({2: verify_current_company}),
    )
    restored = tmp_path / "restored.sqlite"
    with pytest.raises(backup.BackupError) as rejected:
        backup.restore_portable(package, restored, _bundle=target_bundle)
    assert rejected.value.code == "backup_schema_unsupported"
    assert not restored.exists()


def test_released_migration_failure_never_publishes_target(tmp_path: Path) -> None:
    source_bundle, target_bundle = released_bundles(tmp_path / "contracts")
    source = tmp_path / "source.sqlite"
    connection = runtime.connect(source)
    try:
        initialize(connection, source_bundle, COMPANY, TAXPAYER, DATABASE)
    finally:
        connection.close()
    package = Path(
        backup.create_portable(source, tmp_path / "backups", _bundle=source_bundle)["path"]
    )

    def fail(_connection):
        raise RuntimeError("synthetic migration failure")

    broken_step = replace(target_bundle.steps[0], apply=fail)
    target_bundle = replace(target_bundle, steps=(broken_step,))
    restored = tmp_path / "restored.sqlite"
    with pytest.raises(backup.BackupError) as rejected:
        backup.restore_portable(package, restored, _bundle=target_bundle)
    assert rejected.value.code == "backup_content_invalid"
    assert not restored.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", 2.0),
        ("format_version", True),
        ("format_version", "2"),
        ("status", []),
        ("status", {}),
        ("status", None),
        ("version", False),
        ("version", 0.0),
        ("family", []),
        ("fingerprint", {}),
        ("kind", []),
    ],
)
def test_manifest_rejects_wrong_scalar_types_before_extraction(
    portable, tmp_path, monkeypatch, field, value
):
    forged = tmp_path / "wrong-type.zip"

    def change(manifest):
        if field == "format_version":
            manifest[field] = value
        else:
            manifest["database_format"][field] = value

    rewrite_manifest(portable, forged, change)
    monkeypatch.setattr(
        backup, "create_private_file", lambda *a, **k: pytest.fail("must not extract")
    )
    with pytest.raises(backup.BackupError) as error:
        backup.verify_portable(forged)
    assert error.value.code == (
        "backup_format_unsupported" if field == "format_version" else "backup_manifest_invalid"
    )
