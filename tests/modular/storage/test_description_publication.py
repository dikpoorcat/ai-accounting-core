"""The explicit multi-company handoff keeps its original paths and crash recovery."""

import hashlib
import json
import os
import shutil
import sqlite3
from collections import Counter
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.storage.public import CompanySpec, company_structure_contract
from tests.modular.application._working_case import Case
from tests.modular.application._working_case import case as case
from tests.modular.storage.test_description_upgrade import OLD_NOTES, set_legacy_module_records
from tools import publish_business_description_upgrade as publication


def legacy_database(database):
    contract = deepcopy(company_structure_contract())
    contract["version"] = 6
    for item in contract["objects"]:
        if item["name"] == "system_meta":
            item["sql"] = item["sql"].replace("schema_version=7", "schema_version=6")
        if item["name"] == "company_note":
            item["sql"] = OLD_NOTES

    def digest(value):
        return hashlib.sha256(publication.handoff.canonical(value).encode()).hexdigest()

    contract["fingerprint"] = digest(contract["objects"])
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN")
        for name in ("company_note", "system_meta"):
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{name}"')]
            connection.execute(f'DROP TABLE "{name}"')
            connection.execute(
                next(item["sql"] for item in contract["objects"] if item["name"] == name)
            )
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
            for row in rows:
                if name == "system_meta":
                    row.update(schema_version=6, structure_fingerprint=contract["fingerprint"])
                values = ",".join("?" for _ in columns)
                connection.execute(
                    f'INSERT INTO "{name}"({",".join(columns)}) VALUES({values})',
                    [row[column] for column in columns],
                )
        set_legacy_module_records(connection, contract)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    publication.clear_sidecars(database)
    return contract


def old_package(root, path, manifest, contract):
    manifest = deepcopy(manifest)
    manifest["structure_fingerprint"] = contract["fingerprint"]
    with ZipFile(path, "x", compression=ZIP_DEFLATED) as archive:
        for item in manifest["files"]:
            raw = (
                (publication.handoff.canonical(contract) + "\n").encode()
                if item["kind"] == "contract"
                else (root / item["path"]).read_bytes()
            )
            item.update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
            archive.writestr(item["path"], raw)
        archive.writestr("manifest.json", publication.handoff.canonical(manifest))


@pytest.fixture
def batch(case, tmp_path):
    second_id = str(uuid4())
    case.kernel.storage.create_company(
        CompanySpec(
            second_id,
            str(uuid4()),
            "913100000000000002",
            "第二家合成公司",
            "2026-01",
            tmp_path / "company-b",
        )
    )
    cases = [case, Case(case.kernel, second_id, str(uuid4()), str(uuid4()))]
    companies, captures = [], []
    application = create_offline_application()
    for index, selected in enumerate(cases):
        fact = selected.seed()
        written = selected.app.accounting_apply(
            selected.request(
                notes=[
                    {
                        "scope": "business:" + fact["business_id"],
                        "text": "本项为合成维修费的完整业务说明。",
                        "evidence_ids": [selected.source_id],
                        "expected_seq": 0,
                    }
                ]
            )
        )
        assert written["completion"] == "complete", written
        assert (
            selected.app.accounting_documents({"company_id": selected.company, "action": "finish"})[
                "state"
            ]
            == "ready"
        )
        with selected.kernel.storage.read_unit(selected.company) as read:
            metadata = selected.kernel.storage.company_metadata(read)
            head = read.head
        task = selected.app.accounting_backup(
            {"company_id": selected.company, "expected_database_id": metadata.database_id}
        )
        result = selected.kernel.files.run(selected.company, task["task_id"])
        assert result["state"] == "succeeded", result
        archive_path = metadata.backup_directory / (metadata.credit_code + ".finance-company.zip")
        candidate = tmp_path / f"candidate-{index}"
        candidate.mkdir()
        with (
            archive_path.open("rb") as stream,
            application.backup_verifier.open(stream) as verified,
        ):
            manifest = deepcopy(verified.manifest)
            for item in manifest["files"]:
                target = candidate / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(verified.company_root / item["path"], target)
        backup = candidate / "verified-backup" / archive_path.name
        backup.parent.mkdir()
        shutil.copyfile(archive_path, backup)
        captures.append((metadata, head, candidate, backup, manifest))
    for index, (metadata, head, candidate, backup, manifest) in enumerate(captures):
        contract = legacy_database(metadata.database_path)
        previous = tmp_path / f"verified-legacy-{index}.zip"
        old_package(metadata.company_root, previous, manifest, contract)
        source_sha = publication.handoff.digest_file(previous)
        (candidate / "handoff.json").write_text(
            publication.handoff.canonical(
                {
                    "state": "verified",
                    "company_id": metadata.company_id,
                    "database_id": metadata.database_id,
                    "database_head": head,
                    "source_sha256": source_sha,
                    "structure_fingerprint": company_structure_contract()["fingerprint"],
                    "target": str(candidate),
                    "backup": str(backup),
                }
            ),
            encoding="utf-8",
        )
        companies.append(
            {
                "company_id": metadata.company_id,
                "company_root": str(metadata.company_root),
                "database_id": metadata.database_id,
                "database_head": head,
                "source_structure_fingerprint": contract["fingerprint"],
                "source_sha256": source_sha,
                "candidate_root": str(candidate),
            }
        )
    return {
        "format": publication.FORMAT,
        "batch_id": str(uuid4()),
        "catalog_path": str(case.kernel.storage.catalog_path),
        "companies": companies,
    }, tmp_path / "publication.json"


def schema(item):
    database = Path(item["company_root"]) / "runtime/company.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("SELECT schema_version FROM system_meta").fetchone()[0]


def test_two_companies_install_and_activate_without_changing_other_files(batch):
    plan, journal_path = batch
    protected = {
        path: publication.handoff.digest_file(path)
        for item in plan["companies"]
        for path in Path(item["company_root"]).rglob("*")
        if path.is_file() and "runtime" not in path.relative_to(item["company_root"]).parts
    }
    assert publication.publish(plan, journal_path)["state"] == "installed"
    assert [schema(item) for item in plan["companies"]] == [7, 7]
    journal = json.loads(journal_path.read_bytes())
    for item in journal["companies"]:
        paths = publication.company_paths(item, plan["batch_id"])
        assert publication.handoff.digest_file(paths["rollback"]) == item["old_sha256"]
        assert not paths["prepared"].exists()
    assert publication.publish(plan, journal_path, activate_only=True)["state"] == "active"
    assert all(
        publication.handoff.digest_file(path) == digest for path, digest in protected.items()
    )
    with pytest.raises(RuntimeError, match="已启用"):
        publication.publish(plan, journal_path)


def test_second_company_switch_failure_restores_entire_batch(batch, monkeypatch):
    plan, journal = batch
    database = Path(plan["companies"][1]["company_root"]) / "runtime/company.sqlite3"
    replace = os.replace

    def fail_second(source, target):
        if Path(target) == database and ".new.sqlite3" in str(source):
            raise OSError("synthetic second-company failure")
        return replace(source, target)

    monkeypatch.setattr(publication.os, "replace", fail_second)
    with pytest.raises(OSError, match="second-company"):
        publication.publish(plan, journal)
    assert [schema(item) for item in plan["companies"]] == [6, 6]
    assert json.loads(journal.read_bytes())["state"] == "rolled_back"


def test_restart_after_original_was_moved_restores_all_companies(batch, monkeypatch):
    plan, journal = batch
    database = Path(plan["companies"][0]["company_root"]) / "runtime/company.sqlite3"
    replace = os.replace

    class ProcessLoss(BaseException):
        pass

    def interrupt_after_move(source, target):
        if Path(target) == database and ".new.sqlite3" in str(source):
            raise ProcessLoss()
        return replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(publication.os, "replace", interrupt_after_move)
        with pytest.raises(ProcessLoss):
            publication.publish(plan, journal)
    assert not database.exists()
    assert publication.publish(plan, journal)["state"] == "rolled_back"
    assert [schema(item) for item in plan["companies"]] == [6, 6]


def test_new_write_prevents_rollback_of_every_company(batch):
    plan, journal = batch
    publication.publish(plan, journal)
    database = Path(plan["companies"][1]["company_root"]) / "runtime/company.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE system_meta SET global_seq=global_seq+1")
        connection.commit()
    with pytest.raises(RuntimeError, match="业务头"):
        publication.publish(plan, journal)
    assert [schema(item) for item in plan["companies"]] == [7, 7]
    assert json.loads(journal.read_bytes())["state"] == "installed"


def test_source_head_drift_rejects_before_installation(batch):
    plan, journal = batch
    database = Path(plan["companies"][1]["company_root"]) / "runtime/company.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE system_meta SET global_seq=global_seq+1")
        connection.commit()
    with pytest.raises(RuntimeError, match="业务头"):
        publication.publish(plan, journal)
    assert not journal.exists()
    assert [schema(item) for item in plan["companies"]] == [6, 6]


def test_running_resident_rejects_before_touching_company_files(batch):
    plan, journal = batch
    with publication.service_window(Path(plan["catalog_path"])):
        with pytest.raises(RuntimeError, match="仍在运行"):
            publication.publish(plan, journal)
    assert not journal.exists()
    assert [schema(item) for item in plan["companies"]] == [6, 6]


def test_stale_resident_probe_does_not_prevent_a_proven_stopped_window(batch):
    plan, journal = batch
    catalog = Path(plan["catalog_path"])
    catalog.with_name(catalog.name + ".modular-service").write_bytes(b"stale private probe")
    assert publication.publish(plan, journal)["state"] == "installed"


def test_plan_covers_the_actual_active_catalog_set(batch):
    plan, journal = batch
    incomplete = deepcopy(plan)
    incomplete["companies"].pop()
    with pytest.raises(RuntimeError, match="有效公司集合"):
        publication.publish(incomplete, journal)
    assert not journal.exists()
    assert [schema(item) for item in plan["companies"]] == [6, 6]


def test_candidate_with_new_purposes_is_not_a_structure_only_handoff(batch):
    plan, journal = batch
    database = Path(plan["companies"][0]["candidate_root"]) / "runtime/company.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE company_note SET purpose='合成维修费'")
        connection.commit()
    with pytest.raises(RuntimeError, match="简短事项写入"):
        publication.publish(plan, journal)
    assert not journal.exists()
    assert [schema(item) for item in plan["companies"]] == [6, 6]


def test_same_head_formal_content_drift_rejects_before_installation(batch):
    plan, journal = batch
    database = Path(plan["companies"][1]["company_root"]) / "runtime/company.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE company_note SET latest_expected_seq=latest_expected_seq+1")
        connection.commit()
    with pytest.raises(RuntimeError, match="正式内容已变化"):
        publication.publish(plan, journal)
    assert not journal.exists()
    assert [schema(item) for item in plan["companies"]] == [6, 6]


@pytest.mark.parametrize("field", ["contract_digest", "sql_fingerprint", "version", "missing"])
def test_same_head_module_metadata_corruption_preserves_original_batch(batch, field):
    plan, journal = batch
    database = Path(plan["companies"][1]["company_root"]) / "runtime/company.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        if field == "missing":
            connection.execute("DELETE FROM module_schema WHERE module_key='records'")
        else:
            connection.execute(
                f"UPDATE module_schema SET {field}=? WHERE module_key='records'",
                (2 if field == "version" else "0" * 64,),
            )
        connection.commit()
    publication.checkpoint(database)
    originals = {
        Path(item["company_root"]) / "runtime/company.sqlite3": publication.handoff.digest_file(
            Path(item["company_root"]) / "runtime/company.sqlite3"
        )
        for item in plan["companies"]
    }
    with pytest.raises(RuntimeError, match="模块结构记录"):
        publication.publish(plan, journal)
    assert not journal.exists()
    assert all(
        publication.handoff.digest_file(path) == digest for path, digest in originals.items()
    )
    assert [schema(item) for item in plan["companies"]] == [6, 6]


def test_corrupt_rollback_file_preserves_all_installed_databases(batch):
    plan, journal = batch
    publication.publish(plan, journal)
    item = plan["companies"][1]
    rollback = publication.company_paths(item, plan["batch_id"])["rollback"]
    with rollback.open("ab") as stream:
        stream.write(b"synthetic corruption")
    with pytest.raises(RuntimeError, match="回退文件已变化"):
        publication.publish(plan, journal)
    assert [schema(item) for item in plan["companies"]] == [7, 7]
    assert json.loads(journal.read_bytes())["state"] == "installed"


def test_corrupt_journal_never_drives_database_replacement(batch):
    plan, journal_path = batch
    publication.publish(plan, journal_path)
    journal = json.loads(journal_path.read_bytes())
    journal["companies"][0]["old_sha256"] = "0" * 64
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(RuntimeError, match="清单.*损坏"):
        publication.publish(plan, journal_path)
    assert [schema(item) for item in plan["companies"]] == [7, 7]


def test_active_window_preserves_following_writes(batch):
    plan, journal = batch
    publication.publish(plan, journal)
    publication.publish(plan, journal, activate_only=True)
    item = plan["companies"][0]
    database = Path(item["company_root"]) / "runtime/company.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE system_meta SET global_seq=global_seq+1")
        connection.commit()
    with pytest.raises(RuntimeError, match="已启用"):
        publication.publish(plan, journal)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT global_seq FROM system_meta").fetchone()[0] == (
            item["database_head"] + 1
        )
    assert [schema(item) for item in plan["companies"]] == [7, 7]


def test_restart_during_batch_restore_continues_from_actual_files(batch, monkeypatch):
    plan, journal = batch
    publication.publish(plan, journal)
    first = publication.company_paths(plan["companies"][0], plan["batch_id"])
    replace = os.replace

    class ProcessLoss(BaseException):
        pass

    def interrupt_after_restore(source, target):
        result = replace(source, target)
        if Path(source) == first["rollback"]:
            raise ProcessLoss()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(publication.os, "replace", interrupt_after_restore)
        with pytest.raises(ProcessLoss):
            publication.publish(plan, journal)
    assert [schema(item) for item in plan["companies"]] == [6, 7]
    assert publication.publish(plan, journal)["state"] == "rolled_back"
    assert [schema(item) for item in plan["companies"]] == [6, 6]


@pytest.mark.parametrize("version", [6, 7])
def test_formal_proof_reuses_unchanged_table_digests(batch, monkeypatch, version):
    plan, _ = batch
    item = plan["companies"][0]
    root = item["company_root"] if version == 6 else item["candidate_root"]
    database = Path(root) / "runtime/company.sqlite3"
    statements = []
    connect = sqlite3.connect

    def traced_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(publication.sqlite3, "connect", traced_connection)
        proof = publication.database_proof(database)
    reads = Counter(
        statement.rsplit(' FROM "', 1)[1][:-1]
        for statement in statements
        if statement.startswith("SELECT ") and ' FROM "' in statement
    )
    assert reads == Counter(
        {name: 2 if version == 7 and name == "company_note" else 1 for name in proof["rows"]}
    )
    with closing(connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        legacy = [
            row[1]
            for row in connection.execute("PRAGMA table_info(company_note)")
            if row[1] != "purpose"
        ]
        assert proof["rows"] == publication.handoff.table_digests(connection)
        assert proof["legacy_rows"] == publication.handoff.table_digests(
            connection, note_columns=legacy
        )
