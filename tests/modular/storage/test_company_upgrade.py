"""A known structure advances atomically without rewriting accounting facts."""

import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.files import public as files
from ai_accounting_modular.reading import public as reading
from ai_accounting_modular.storage import _upgrades
from ai_accounting_modular.storage._schema import module_contract
from ai_accounting_modular.storage.public import (
    company_structure_contract,
    module_schema,
    open_database_unit,
    restore_database_copy,
    upgrade_company_database,
    verify_database,
)


@pytest.fixture(params=[1, 2])
def previous(tmp_path, request):
    path = tmp_path / "company.sqlite3"
    contract = company_structure_contract(version=request.param)
    with closing(sqlite3.connect(path)) as connection:
        for typ in ("table", "index", "trigger"):
            for item in contract["objects"]:
                if item["type"] == typ:
                    connection.execute(item["sql"])
        connection.execute(
            "INSERT INTO system_meta VALUES(1,'ai-accounting-modular/1',?,?,?,7)",
            (str(uuid4()), contract["version"], contract["fingerprint"]),
        )
        connection.execute(
            "INSERT INTO company_profile VALUES(1,?,'913100000000000001','接续合成公司',"
            "'small_enterprise','CNY','2026-01',3)",
            (str(uuid4()),),
        )
        for owner in sorted({item["owner"] for item in contract["objects"]}):
            module = module_contract(contract, owner)
            connection.execute(
                "INSERT INTO module_schema VALUES(?,?,?,?,?)",
                (
                    owner,
                    module.version,
                    module.contract_digest,
                    module.parent_digest,
                    module.sql_fingerprint,
                ),
            )
        connection.execute(
            "INSERT INTO company_note(scope_key,note_text,basis_head,state,seq,latest_command_id,"
            "latest_expected_seq,content_digest) VALUES('company',?,NULL,'current',4,?,3,?)",
            ("*原样正文*\n<普通文字>\n", str(uuid4()), "a" * 64),
        )
        connection.execute(
            "INSERT INTO period_close VALUES('2026-01','封存依据','2026-02-01',"
            "'basis/1','{}','report/1','{}',?)",
            ("b" * 64,),
        )
        connection.commit()
        connection.execute("PRAGMA journal_mode=WAL")
    return path, contract


def rows(path):
    with closing(sqlite3.connect(path)) as connection:
        result = {}
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ):
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            if table == "company_note" and "note_format" in columns:
                columns.remove("note_format")
            result[table] = connection.execute(
                "SELECT " + ",".join('"' + column + '"' for column in columns) + f' FROM "{table}"'
            ).fetchall()
        return result


def test_exact_archived_contract_remains_detached_and_has_explicit_parent():
    old = company_structure_contract(version=1)
    previous, current = company_structure_contract(version=2), company_structure_contract()
    assert old["version"] == 1 and current["version"] == 3
    assert previous["parent_fingerprint"] == old["fingerprint"]
    assert current["parent_fingerprint"] == previous["fingerprint"]
    assert module_schema("records").version == module_schema("files").version == 2
    assert module_schema("storage").version == 3
    assert module_schema("ledger").version == 1
    assert module_schema("records").parent_digest == module_contract(old, "records").contract_digest
    old["objects"].clear()
    assert company_structure_contract(version=1)["objects"]
    with pytest.raises(ContractError):
        company_structure_contract(version=True)
    module_schema("files", structure_version=1)
    with pytest.raises(ContractError):
        module_schema("files", structure_version=True)


def test_old_open_is_rejected_without_automatic_upgrade(previous):
    path, _ = previous
    before = rows(path)
    for operation in (
        lambda: verify_database(path),
        lambda: open_database_unit(path, company_root=path.parent).__enter__(),
    ):
        with pytest.raises(ContractError) as error:
            operation()
        assert error.value.code == "structure_upgrade_required"
    assert rows(path) == before


def test_old_restore_maintenance_only_accepts_unchanged_owner_contracts(previous):
    path, contract = previous
    before = rows(path)
    with open_database_unit(
        path,
        company_root=path.parent,
        structure_version=contract["version"],
        mode="maintenance",
        repositories=(
            files.repository_factory(structure_version=contract["version"]),
            reading.repository_factory(),
        ),
    ) as unit:
        files.reset_for_restore(unit)
        reading.reset_for_restore(unit)
        with pytest.raises(ContractError):
            unit.repository("records")
    assert rows(path) == before
    for mode in ("read", "write"):
        with pytest.raises(ContractError, match="恢复维护"):
            with open_database_unit(
                path, company_root=path.parent, mode=mode, structure_version=contract["version"]
            ):
                pytest.fail("old structure must not become a business access path")
    with pytest.raises(ContractError, match="结构合同不匹配"):
        with open_database_unit(
            path,
            company_root=path.parent,
            structure_version=contract["version"],
            mode="maintenance",
            repositories=(files.repository_factory(),),
        ):
            pytest.fail("changed records contract must be rejected before opening")
    assert verify_database(path, structure_version=contract["version"]).head == 7


def test_explicit_upgrade_preserves_every_original_row_and_identity(previous, tmp_path):
    path, contract = previous
    original = verify_database(path, structure_version=contract["version"])
    copied = restore_database_copy(
        path, tmp_path / "physical-copy.sqlite3", structure_version=contract["version"]
    )
    assert copied.head == original.head == 7
    before = rows(path)
    upgraded = upgrade_company_database(
        path, expected_structure_fingerprint=contract["fingerprint"], expected_snapshot=original
    )
    assert upgraded == verify_database(path)
    assert (upgraded.company_id, upgraded.database_id, upgraded.head) == (
        original.company_id,
        original.database_id,
        original.head,
    )
    after = rows(path)
    assert {
        key: value for key, value in before.items() if key not in {"module_schema", "system_meta"}
    } == {key: value for key, value in after.items() if key not in {"module_schema", "system_meta"}}
    assert [
        row for row in before["module_schema"] if row[0] not in {"records", "files", "storage"}
    ] == [row for row in after["module_schema"] if row[0] not in {"records", "files", "storage"}]
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT note_format FROM company_note").fetchone() == (
            "plain_text",
        )
    assert (
        upgrade_company_database(
            path, expected_structure_fingerprint=contract["fingerprint"], expected_snapshot=original
        )
        == upgraded
    )
    assert rows(path) == after
    assert verify_database(copied.path, structure_version=contract["version"]).head == 7


def test_file_task_expansion_preserves_all_original_states_and_markdown(previous):
    path, contract = previous
    with closing(sqlite3.connect(path)) as connection:
        for state in ("pending", "running", "succeeded", "failed", "expired"):
            connection.execute(
                "INSERT INTO file_task(task_id,kind,state,scope_key,scope_head,creator_request_id,"
                "attempts,plan_schema,plan_json,output_path,output_sha256,output_bytes,"
                "error_code,error_message) VALUES(?,'report_export',?,?,?, ?,1,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    state,
                    state,
                    "c" * 64,
                    "request-" + state,
                    None if state == "expired" else "ReportExportPlan",
                    None if state == "expired" else '{"preserved":"exact original plan"}',
                    "runtime/result.xlsx" if state == "succeeded" else None,
                    "d" * 64 if state == "succeeded" else None,
                    31 if state == "succeeded" else None,
                    "test_failure" if state in {"failed", "expired"} else None,
                    "保留原状态" if state in {"failed", "expired"} else None,
                ),
            )
        if contract["version"] == 2:
            connection.execute("UPDATE company_note SET note_format='markdown'")
        connection.commit()
        before = connection.execute("SELECT * FROM file_task ORDER BY scope_key").fetchall()
    upgrade_company_database(path, expected_structure_fingerprint=contract["fingerprint"])
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT * FROM file_task ORDER BY scope_key").fetchall() == before
        assert connection.execute("SELECT note_format FROM company_note").fetchone()[0] == (
            "markdown" if contract["version"] == 2 else "plain_text"
        )
        connection.execute(
            "INSERT INTO file_task(task_id,kind,state,scope_key,scope_head,creator_request_id,"
            "plan_schema,plan_json) VALUES(?,'payment_export','pending','2026-09',?,'payment',"
            "'PaymentExportPlan','{}')",
            (str(uuid4()), "e" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE file_task SET capture_id=? WHERE kind='payment_export'", (str(uuid4()),)
            )


def test_second_transition_failure_rolls_back_the_entire_forward_chain(previous, monkeypatch):
    path, contract = previous
    before = rows(path)
    transition = _upgrades._transition

    def fail(connection, version):
        transition(connection, version)
        if version == 2:
            raise RuntimeError("injected second transition failure")

    monkeypatch.setattr(_upgrades, "_transition", fail)
    with pytest.raises(RuntimeError, match="second transition"):
        upgrade_company_database(path, expected_structure_fingerprint=contract["fingerprint"])
    assert rows(path) == before
    assert verify_database(path, structure_version=contract["version"]).head == 7


@pytest.mark.parametrize("failure", ["mid_ddl", "module_update", "final_check"])
def test_every_transition_failure_rolls_back_sql_data_and_module_records(
    previous, monkeypatch, failure
):
    path, contract = previous
    before = rows(path)
    if failure == "mid_ddl":

        def fail(connection, version):
            connection.execute("ALTER TABLE company_note ADD COLUMN accidental TEXT")
            raise RuntimeError("injected transition failure")

        monkeypatch.setattr(_upgrades, "_transition", fail)
    elif failure == "module_update":
        monkeypatch.setattr(
            _upgrades,
            "module_schema",
            lambda owner, **kwargs: (_ for _ in ()).throw(
                RuntimeError("injected transition failure")
            ),
        )
    else:
        original = _upgrades.verify_structure

        def fail(connection, kind, **kwargs):
            if not kwargs:
                raise RuntimeError("injected transition failure")
            return original(connection, kind, **kwargs)

        monkeypatch.setattr(_upgrades, "verify_structure", fail)
    with pytest.raises(RuntimeError, match="injected"):
        upgrade_company_database(path, expected_structure_fingerprint=contract["fingerprint"])
    assert rows(path) == before
    assert verify_database(path, structure_version=contract["version"]).head == 7


@pytest.mark.parametrize(
    "sql",
    [
        "DROP INDEX identity_object_kind_sort",
        "UPDATE module_schema SET contract_digest='" + "f" * 64 + "' WHERE module_key='ledger'",
        "UPDATE system_meta SET structure_fingerprint='" + "f" * 64 + "'",
    ],
)
def test_unknown_or_tampered_start_is_never_relabelled(previous, sql):
    path, contract = previous
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(sql)
        connection.commit()
    before = rows(path)
    with pytest.raises(ContractError) as error:
        upgrade_company_database(path, expected_structure_fingerprint=contract["fingerprint"])
    assert error.value.code == "storage_corrupt"
    assert rows(path) == before


def test_stale_head_or_wrong_start_refuses_before_any_change(previous):
    path, contract = previous
    snapshot = verify_database(path, structure_version=contract["version"])
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE system_meta SET global_seq=8")
        connection.commit()
    before = rows(path)
    with pytest.raises(ContractError) as error:
        upgrade_company_database(
            path, expected_structure_fingerprint=contract["fingerprint"], expected_snapshot=snapshot
        )
    assert error.value.code == "conflict" and rows(path) == before
    with pytest.raises(ContractError) as error:
        upgrade_company_database(path, expected_structure_fingerprint="f" * 64)
    assert error.value.code == "unsupported_structure" and rows(path) == before
