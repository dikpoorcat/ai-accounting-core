"""Convert one verified structure-6 ZIP into a separate structure-7 company.

The caller selects the matching independent old source package. That package
verifies the original ZIP in a separate process before the current package
changes its isolated copy. Live catalogs and company locations are outside this
tool's responsibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_digest(source):
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".json", ".xlsx", ".js", ".css", ".html"}:
            digest.update(path.relative_to(source).as_posix().encode("utf-8"))
            digest.update(bytes.fromhex(digest_file(path)))
    return digest.hexdigest()


def stage_original(runtime_source, backup, stage):
    """Executed only in the old runtime's separate import graph."""
    runtime_sha256 = runtime_digest(runtime_source / "ai_accounting_modular")
    sys.path.insert(0, str(runtime_source))
    from ai_accounting_modular.host.public import create_offline_application
    from ai_accounting_modular.storage.public import company_structure_contract

    contract = company_structure_contract()
    if contract["version"] != 6:
        raise ValueError("The selected original runtime must use company structure 6")
    application = create_offline_application()
    with backup.open("rb") as stream, application.backup_verifier.open(stream) as verified:
        manifest = verified.manifest
        source_sha256 = verified.sha256
        for item in manifest["files"]:
            destination = stage / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(verified.company_root / item["path"], destination)
            if (digest_file(destination), destination.stat().st_size) != (
                item["sha256"],
                item["byte_size"],
            ):
                raise ValueError("The staged original bytes differ from the verified package")
    if runtime_digest(runtime_source / "ai_accounting_modular") != runtime_sha256:
        raise ValueError("The original runtime changed during verification")
    return {
        "manifest": manifest,
        "contract": contract,
        "source_sha256": source_sha256,
        "runtime_sha256": runtime_sha256,
    }


def table_digest(connection, name, *, columns=None):
    columns = (
        [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
        if columns is None
        else columns
    )
    fields = ",".join('"' + column + '"' for column in columns)
    rows = sorted(
        canonical(list(row)) for row in connection.execute(f'SELECT {fields} FROM "{name}"')
    )
    return hashlib.sha256(canonical([columns, rows]).encode("utf-8")).hexdigest()


def table_digests(connection, *, note_columns=None):
    """Compare every authoritative row, including exact frozen bodies and sources."""
    excluded = {
        "system_meta",
        "module_schema",
        "read_scope_head",
        "read_component",
        "read_item",
        "file_task",
    }
    names = [
        row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    ]
    result = {}
    for name in sorted(set(names) - excluded):
        columns = note_columns if name == "company_note" else None
        result[name] = table_digest(connection, name, columns=columns)
    return result


def convert_structure(database, source_contract):
    from ai_accounting_modular.storage.public import (
        company_structure_contract,
        module_schema,
        verify_database,
    )

    target = company_structure_contract()
    changed_tables = {"system_meta", "company_note"}
    if source_contract["version"] != 6 or target["version"] != 7:
        raise ValueError("This handoff requires the exact structure-6 to structure-7 transition")
    source_other = [
        item for item in source_contract["objects"] if item["name"] not in changed_tables
    ]
    target_other = [item for item in target["objects"] if item["name"] not in changed_tables]
    if source_other != target_other:
        raise ValueError("Other structure changes require their own explicitly verified handoff")
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        note_columns = [row[1] for row in connection.execute("PRAGMA table_info(company_note)")]
        before = table_digests(connection, note_columns=note_columns)
        system = dict(connection.execute("SELECT * FROM system_meta").fetchone())
        source_owners = {item["owner"] for item in source_contract["objects"]}
        target_owners = {item["owner"] for item in target["objects"]}
        if source_owners != target_owners:
            raise ValueError("Module ownership changed outside this handoff")
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for name in sorted(changed_tables):
                rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{name}"')]
                sql = next(item["sql"] for item in target["objects"] if item["name"] == name)
                connection.execute(f'DROP TABLE "{name}"')
                connection.execute(sql)
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
                for row in rows:
                    if name == "system_meta":
                        row.update(schema_version=7, structure_fingerprint=target["fingerprint"])
                    else:
                        row["purpose"] = None
                    fields = ",".join('"' + column + '"' for column in columns)
                    placeholders = ",".join("?" for _ in columns)
                    connection.execute(
                        f'INSERT INTO "{name}"({fields}) VALUES({placeholders})',
                        tuple(row[column] for column in columns),
                    )
            for owner in sorted(target_owners):
                schema = module_schema(owner)
                connection.execute(
                    "UPDATE module_schema SET version=?,contract_digest=?,sql_fingerprint=? "
                    "WHERE module_key=?",
                    (schema.version, schema.contract_digest, schema.sql_fingerprint, owner),
                )
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("The converted database has an invalid foreign key")
            if table_digests(connection, note_columns=note_columns) != before:
                raise ValueError("The conversion changed authoritative data")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    physical = verify_database(database)
    if (physical.database_id, physical.head) != (system["database_id"], system["global_seq"]):
        raise ValueError("The database identity or accounting head changed")
    return target, before


def verify_target(stage, manifest, contract, expected_rows):
    from ai_accounting_modular.files.public import reset_for_restore as reset_files
    from ai_accounting_modular.host.public import create_offline_application
    from ai_accounting_modular.reading.public import reset_for_restore as reset_reading
    from ai_accounting_modular.storage.public import open_database_unit

    application = create_offline_application()
    database = stage / "runtime/company.sqlite3"
    with open_database_unit(
        database, repositories=application.repositories, company_root=stage, mode="maintenance"
    ) as unit:
        reset_files(unit)
        reset_reading(unit)
    with open_database_unit(
        database, repositories=application.repositories, company_root=stage
    ) as read:
        snapshot = application.backup_verifier.verify_snapshot(read)
    if snapshot.document_set_id != manifest["document_set_id"]:
        raise ValueError("The standard document set changed during structure conversion")
    with closing(sqlite3.connect(database)) as connection:
        note_columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(company_note)")
            if row[1] != "purpose"
        ]
        if table_digests(connection, note_columns=note_columns) != expected_rows:
            raise ValueError("Target verification changed authoritative data")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint[0] != 0:
            raise ValueError("The converted database still has an active WAL writer")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("The converted database has unpublished transaction bytes")
    (stage / "contracts/company.json").write_text(canonical(contract) + "\n", encoding="utf-8")
    for item in manifest["files"]:
        path = stage / item["path"]
        item.update(sha256=digest_file(path), byte_size=path.stat().st_size)
    manifest["structure_fingerprint"] = contract["fingerprint"]
    output = (
        stage
        / "verified-backup"
        / (manifest["company_identity"]["credit_code"] + ".finance-company.zip")
    )
    output.parent.mkdir()
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", canonical(manifest))
        for item in manifest["files"]:
            archive.write(stage / item["path"], item["path"])
    with output.open("rb") as stream:
        application.backup_verifier(stream)
    return output.relative_to(stage).as_posix()


def upgrade(backup, runtime_source, target):
    backup, runtime_source, target = backup.resolve(), runtime_source.resolve(), target.resolve()
    if target.exists():
        raise FileExistsError("The new target must not already exist")
    if target.is_relative_to(runtime_source) or runtime_source.is_relative_to(target):
        raise ValueError("The runtime and target must be separate")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".description-upgrade-", dir=target.parent
    ) as temporary:
        stage = Path(temporary) / "company"
        stage.mkdir()
        worker = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "--stage-original",
                "--source-backup",
                str(backup),
                "--source-runtime",
                str(runtime_source),
                "--target",
                str(stage),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        original = json.loads(worker.stdout)
        contract, rows = convert_structure(stage / "runtime/company.sqlite3", original["contract"])
        output = verify_target(stage, original["manifest"], contract, rows)
        result = {
            "state": "verified",
            "company_id": original["manifest"]["company_id"],
            "database_id": original["manifest"]["database_id"],
            "database_head": original["manifest"]["database_head"],
            "source_sha256": original["source_sha256"],
            "source_runtime_sha256": original["runtime_sha256"],
            "structure_fingerprint": contract["fingerprint"],
            "target": str(target),
            "backup": str(target / output),
        }
        (stage / "handoff.json").write_text(canonical(result) + "\n", encoding="utf-8")
        os.rename(stage, target)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-backup", required=True, type=Path)
    parser.add_argument("--source-runtime", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--stage-original", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        result = (
            stage_original(
                args.source_runtime.resolve(), args.source_backup.resolve(), args.target.resolve()
            )
            if args.stage_original
            else upgrade(args.source_backup, args.source_runtime, args.target)
        )
    except Exception as error:
        message = getattr(error, "message", str(error))
        if isinstance(error, subprocess.CalledProcessError):
            message = "The selected original runtime could not verify the original package"
            if error.stdout:
                message = json.loads(error.stdout).get("message", message)
        print(canonical({"state": "failed", "error": type(error).__name__, "message": message}))
        return 1
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
