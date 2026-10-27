"""Only isolated synthetic structure-3 copies exercise the explicit development converter."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.storage.public import company_structure_contract, verify_database
from tests.modular.reading._support import assemble, expense
from tools import convert_reading_copy as converter


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode("utf-8")).hexdigest()


def legacy_contract():
    value = deepcopy(company_structure_contract())
    value["version"] = 3
    value["objects"] = [item for item in value["objects"] if item["name"] != "read_scope_pending"]
    for item in value["objects"]:
        if item["name"] == "system_meta":
            item["sql"] = item["sql"].replace("CHECK(schema_version=4)", "CHECK(schema_version=3)")
    value["fingerprint"] = digest(value["objects"])
    return value


def module_rows(contract):
    rows = []
    for owner in sorted({item["owner"] for item in contract["objects"]}):
        sql = [
            {key: item[key] for key in ("type", "name", "table", "sql")}
            for item in contract["objects"]
            if item["owner"] == owner
        ]
        fingerprint = digest(sql)
        header = {"owner": owner, "version": 1, "sql_fingerprint": fingerprint}
        rows.append((owner, 1, digest(header), fingerprint))
    return rows


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / "synthetic"
    root.mkdir()
    book = assemble(root)
    book.submit(*(expense(f"expense-{index}", str(index + 1)) for index in range(40)))
    with book.storage.read_unit(book.company) as read:
        metadata = book.storage.company_metadata(read)
    old = legacy_contract()
    # The fixture really replaces the CHECK DDL; changing the stored version alone
    # would fail the current structure-4 table's constraint.
    keeper = sqlite3.connect(metadata.database_path, isolation_level=None)
    keeper.row_factory = sqlite3.Row
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("BEGIN IMMEDIATE")
    system = dict(keeper.execute("SELECT * FROM system_meta").fetchone())
    keeper.execute("DROP TABLE system_meta")
    keeper.execute(next(item["sql"] for item in old["objects"] if item["name"] == "system_meta"))
    keeper.execute(
        "INSERT INTO system_meta VALUES(?,?,?,?,?,?)",
        (
            1,
            old["format_family"],
            system["database_id"],
            3,
            old["fingerprint"],
            system["global_seq"],
        ),
    )
    keeper.execute("DROP INDEX read_scope_pending")
    keeper.execute("DELETE FROM module_schema")
    keeper.executemany("INSERT INTO module_schema VALUES(?,?,?,?)", module_rows(old))
    keeper.execute(
        "INSERT INTO read_scope_head(scope_key,component_key,wanted_head,format_version,state) "
        "VALUES('synthetic-old','funds',0,1,'preparing')"
    )
    keeper.execute("INSERT INTO read_component VALUES('synthetic-old','funds',0,'{}')")
    keeper.execute(
        "INSERT INTO read_item(scope_key,collection_key,item_id,component_key,"
        "head,sort_key,payload_json) "
        "VALUES('synthetic-old','old-collection','one','funds',0,'one','{}')"
    )
    keeper.commit()
    assert Path(str(metadata.database_path) + "-wal").stat().st_size > 0
    try:
        yield SimpleNamespace(
            source=metadata.database_path,
            contract=old,
            keeper=keeper,
            metadata=metadata,
            target=tmp_path / "converted-copy",
        )
    finally:
        keeper.close()


def source_files(source):
    return {
        suffix: (
            path.stat().st_mtime_ns,
            path.stat().st_size,
            sha256(path.read_bytes()).hexdigest(),
        )
        if path.exists()
        else None
        for suffix in ("", "-wal", "-shm", "-journal")
        for path in (Path(str(source) + suffix),)
    }


def run(legacy):
    return converter.convert_copy(
        source=legacy.source,
        target_directory=legacy.target,
        source_contract=legacy.contract,
    )


def test_wal_copy_replaces_real_check_ddl_preserves_all_formal_rows_and_clears_only_reading(legacy):
    before = source_files(legacy.source)
    result = run(legacy)
    assert source_files(legacy.source) == before
    target = Path(result["database_path"])
    verified = verify_database(target)
    assert (verified.company_id, verified.database_id) == (
        legacy.metadata.company_id,
        legacy.metadata.database_id,
    )
    assert result["registered"] is False and result["structure_version"] == 4
    assert result["formal_tables"]["business_head"] == 41
    assert result["formal_tables"]["adopted_result"] >= 40
    assert result["formal_tables"]["voucher_line"] >= 80
    assert result["formal_tables"]["identity_object"] == 2
    with sqlite3.connect(target) as connection:
        metadata = connection.execute(
            "SELECT schema_version,global_seq FROM system_meta"
        ).fetchone()
        assert metadata == (4, verified.head)
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='system_meta'"
        ).fetchone()[0]
        assert "CHECK(schema_version=4)" in sql and "CHECK(schema_version=3)" not in sql
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name='read_scope_pending'"
            ).fetchone()
            is not None
        )
        for table in ("read_item", "read_component", "read_scope_head"):
            assert connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE system_meta SET schema_version=3")
    assert legacy.keeper.execute("SELECT schema_version FROM system_meta").fetchone()[0] == 3


@pytest.mark.parametrize("damage", ["fingerprint", "extra_field", "other_sql", "wrong_owner"])
def test_rejects_untrusted_or_other_transition_contracts_without_touching_source(legacy, damage):
    before = source_files(legacy.source)
    old = legacy.contract
    if damage == "fingerprint":
        old["fingerprint"] = "0" * 64
    elif damage == "extra_field":
        old["parent_contract"] = "not-accepted"
    else:
        item = next(item for item in old["objects"] if item["name"] == "company_profile")
        item["sql" if damage == "other_sql" else "owner"] += "unexpected"
        old["objects"].sort(key=lambda item: (item["owner"], item["type"], item["name"]))
        old["fingerprint"] = digest(old["objects"])
    with pytest.raises(ContractError):
        run(legacy)
    assert not legacy.target.exists() and source_files(legacy.source) == before


@pytest.mark.parametrize("damage", ["extra_schema", "module_record", "system_fingerprint"])
def test_checks_the_copied_source_against_all_old_schema_and_metadata(legacy, damage):
    if damage == "extra_schema":
        legacy.keeper.execute("CREATE INDEX synthetic_extra ON company_profile(legal_name)")
    elif damage == "module_record":
        legacy.keeper.execute(
            "UPDATE module_schema SET sql_fingerprint=? WHERE module_key='reading'", ("0" * 64,)
        )
    else:
        legacy.keeper.execute("UPDATE system_meta SET structure_fingerprint=?", ("0" * 64,))
    before = source_files(legacy.source)
    with pytest.raises(ContractError):
        run(legacy)
    assert not legacy.target.exists() and source_files(legacy.source) == before


def test_failure_during_formal_row_comparison_cannot_publish_or_change_source(legacy, monkeypatch):
    before = source_files(legacy.source)
    original = converter._same_formal_rows

    def altered(origin, destination, contract):
        destination.execute("UPDATE company_profile SET legal_name='不能留下的变化'")
        return original(origin, destination, contract)

    monkeypatch.setattr(converter, "_same_formal_rows", altered)
    with pytest.raises(ContractError, match="逐行核对失败"):
        run(legacy)
    assert not legacy.target.exists() and source_files(legacy.source) == before


def test_existing_target_is_never_replaced(legacy):
    legacy.target.mkdir()
    marker = legacy.target / "existing.txt"
    marker.write_text("已有内容", encoding="utf-8")
    before = source_files(legacy.source)
    with pytest.raises(ContractError) as error:
        run(legacy)
    assert error.value.code == "target_exists"
    assert marker.read_text(encoding="utf-8") == "已有内容"
    assert source_files(legacy.source) == before


def test_copy_detects_a_changed_source_sidecar_and_does_not_publish(legacy, monkeypatch):
    original = converter.shutil.copyfile
    changed = []

    def copy(source, target):
        result = original(source, target)
        if not changed:
            legacy.keeper.execute("UPDATE company_profile SET legal_name='复制期间发生外部变化'")
            changed.append(True)
        return result

    monkeypatch.setattr(converter.shutil, "copyfile", copy)
    with pytest.raises(ContractError, match="复制期间发生变化"):
        run(legacy)
    assert not legacy.target.exists() and changed == [True]


def test_sqlite_connections_are_only_to_owned_copies(legacy, monkeypatch):
    original, connected = converter.sqlite3.connect, []

    def connect(path, *args, **kwargs):
        connected.append(path)
        if isinstance(path, Path):
            assert path.resolve() != legacy.source.resolve()
        else:
            assert not str(path).startswith(legacy.source.resolve().as_uri())
        return original(path, *args, **kwargs)

    monkeypatch.setattr(converter.sqlite3, "connect", connect)
    run(legacy)
    assert len(connected) >= 3


def test_cli_requires_explicit_paths_and_delivers_one_json_result(legacy, tmp_path):
    contract = tmp_path / "exported-source-contract.json"
    contract.write_text(json.dumps(legacy.contract, ensure_ascii=False), encoding="utf-8")
    before = source_files(legacy.source)
    process = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(Path(converter.__file__).resolve()),
            "--source",
            str(legacy.source),
            "--target-directory",
            str(legacy.target),
            "--source-contract",
            str(contract),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    response = json.loads(process.stdout)
    assert response["status"] == "converted" and response["registered"] is False
    assert source_files(legacy.source) == before


def test_relative_paths_and_missing_source_are_refused_without_a_target(tmp_path):
    old = legacy_contract()
    for source in (Path("company.sqlite3"), tmp_path / "missing.sqlite3"):
        with pytest.raises(ContractError):
            converter.convert_copy(
                source=source, target_directory=tmp_path / "new", source_contract=old
            )
        assert not (tmp_path / "new").exists()
