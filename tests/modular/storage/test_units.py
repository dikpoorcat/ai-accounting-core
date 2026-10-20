from __future__ import annotations

import sqlite3
import threading
import time
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, ReadContext, UnitOfWork
from ai_accounting_modular.storage.public import CompanySpec, Storage


def test_single_company_transaction_and_repository_lifetime(storage_case):
    storage, company = storage_case
    with storage.write_unit(company.company_id) as unit:
        assert isinstance(unit, UnitOfWork)
        assert unit.head == 0 and unit.next_head == 1
        assert unit.start_period == company.start_period
        assert not any(hasattr(unit, name) for name in ("connection", "commit", "execute"))
        repository = unit.repository("identity")
        repository.add(name="甲")
        repository.add(name="乙")
    with storage.read_unit(company.company_id) as read:
        assert isinstance(read, ReadContext)
        assert not isinstance(read, UnitOfWork)
        assert read.head == 1
        assert read.start_period == company.start_period
        assert read.repository("identity").names() == ["乙", "甲"]
    with pytest.raises(ContractError, match="已结束"):
        repository.names()


def test_failed_body_and_commit_check_both_roll_back(storage_case):
    storage, company = storage_case
    for fail_in_check in (False, True):
        with pytest.raises(RuntimeError, match="synthetic failure"):
            with storage.write_unit(company.company_id) as unit:
                unit.repository("identity").add()
                if fail_in_check:
                    unit.check_before_commit(
                        lambda: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
                    )
                else:
                    raise RuntimeError("synthetic failure")
        with storage.read_unit(company.company_id) as read:
            assert read.head == 0
            assert read.repository("identity").names() == []


def test_preview_runs_final_checks_without_saving_any_rows_or_head(storage_case):
    storage, company = storage_case
    checked = []
    with storage.preview_unit(company.company_id) as unit:
        repository = unit.repository("identity")
        repository.add(name="仅供核对的演算")
        unit.check_before_commit(lambda: checked.extend(repository.names()))
        with pytest.raises(ContractError, match="嵌套"):
            with storage.write_unit(company.company_id):
                pass
    assert checked == ["仅供核对的演算"]
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0
        assert read.repository("identity").names() == []
    with pytest.raises(ContractError, match="已结束"):
        repository.names()
    with storage.write_unit(company.company_id) as writer:
        writer.repository("identity").add(name="正式办理")
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1
        assert read.repository("identity").names() == ["正式办理"]


def test_preview_does_not_hide_failed_accounting_checks(storage_case):
    storage, company = storage_case
    with pytest.raises(RuntimeError, match="核算错误"):
        with storage.preview_unit(company.company_id) as unit:
            unit.repository("identity").add()
            unit.check_before_commit(lambda: (_ for _ in ()).throw(RuntimeError("核算错误")))
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0
        assert read.repository("identity").names() == []


def test_preview_cannot_capture_a_database_with_uncommitted_calculation(storage_case, tmp_path):
    storage, company = storage_case
    with storage.preview_unit(company.company_id) as unit:
        unit.repository("identity").add()
        with pytest.raises(ContractError, match="只读一致点"):
            storage.capture_database(unit, tmp_path / "not-a-snapshot.sqlite3")
    assert not (tmp_path / "not-a-snapshot.sqlite3").exists()


def test_caught_statement_failure_cannot_commit_partial_work(storage_case):
    storage, company = storage_case
    with pytest.raises(ContractError, match="部分结果"):
        with storage.write_unit(company.company_id) as unit:
            repo = unit.repository("identity")
            ident = repo.add()
            with pytest.raises(sqlite3.IntegrityError):
                repo.add(ident)
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0 and read.repository("identity").names() == []


def test_read_only_and_owner_write_boundaries(storage_case):
    storage, company = storage_case
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError):
            read.repository("identity").add()
    with pytest.raises(ContractError):
        with storage.write_unit(company.company_id) as unit:
            unit.repository("identity").session.execute(
                "UPDATE company_profile SET legal_name='wrong'"
            )
    assert [item.company_id for item in storage.list_companies()] == [company.company_id]


def test_maintenance_has_only_derived_tables_and_does_not_advance_head(storage_case):
    storage, company = storage_case
    with storage.maintenance_unit(company.company_id) as unit:
        assert unit.next_head == unit.head
        unit.repository("records").publish("c" * 64)
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0
    with pytest.raises(ContractError):
        with storage.maintenance_unit(company.company_id) as unit:
            unit.repository("records").source()
    with pytest.raises(ContractError, match="维护仓储"):
        with storage.maintenance_unit(company.company_id) as unit:
            unit.repository("identity")
    with storage.write_unit(company.company_id) as unit:
        unit.repository("records").source()
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1


def test_empty_work_does_not_advance_head_and_nested_write_rejected(storage_case):
    storage, company = storage_case
    with storage.write_unit(company.company_id):
        with pytest.raises(ContractError, match="嵌套"):
            with storage.write_unit(company.company_id):
                pass
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0


def test_returning_write_advances_head_after_cursor_finishes(storage_case):
    storage, company = storage_case
    with storage.write_unit(company.company_id) as unit:
        repo = unit.repository("identity")
        ident = repo.add(name="原姓名")
        row = repo.session.fetchone(
            "UPDATE identity_object SET name='当前姓名' WHERE object_id=? RETURNING name", (ident,)
        )
        assert row["name"] == "当前姓名"
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1 and read.repository("identity").names() == ["当前姓名"]


@pytest.mark.parametrize("sql", ["COMMIT", "BEGIN", "SAVEPOINT bypass", "PRAGMA foreign_keys=OFF"])
def test_repository_cannot_take_over_transaction(storage_case, sql):
    storage, company = storage_case
    with pytest.raises(ContractError):
        with storage.write_unit(company.company_id) as unit:
            repo = unit.repository("identity")
            repo.add()
            repo.session.execute(sql)
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0 and read.repository("identity").names() == []


def test_company_isolation_and_foreign_key_enforcement(storage_case, tmp_path):
    storage, company = storage_case
    other = storage.create_company(
        CompanySpec(
            str(uuid4()),
            str(uuid4()),
            "922222222222222222",
            "合成公司乙",
            "2026-02",
            tmp_path / "company-b",
        )
    )
    with storage.write_unit(company.company_id) as unit:
        unit.repository("identity").add(name="只属于甲")
    with storage.read_unit(other.company_id) as read:
        assert read.head == 0 and read.repository("identity").names() == []
    with pytest.raises(sqlite3.IntegrityError):
        with storage.write_unit(company.company_id) as unit:
            unit.repository("records").session.execute(
                "INSERT INTO source_use VALUES('business',?,?, '')",
                (str(uuid4()), str(uuid4())),
            )


def test_snapshot_reads_remain_stable_across_other_commit(storage_case):
    storage, company = storage_case
    other = Storage(storage.catalog_path, repositories=storage._factories.values())
    with storage.read_unit(company.company_id) as before:
        assert before.repository("identity").names() == []
        with other.write_unit(company.company_id) as writer:
            writer.repository("identity").add(name="后来写入")
        assert before.head == 0 and before.repository("identity").names() == []
    with storage.read_unit(company.company_id) as after:
        assert after.head == 1 and after.repository("identity").names() == ["后来写入"]


def test_busy_is_bounded_and_retryable(storage_case):
    storage, company = storage_case
    acquired, release = threading.Event(), threading.Event()
    failures = []

    def hold_write():
        try:
            with storage.write_unit(company.company_id):
                acquired.set()
                release.wait(10)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=hold_write)
    thread.start()
    assert acquired.wait(3)
    started = time.monotonic()
    try:
        with pytest.raises(ContractError) as error:
            with storage.write_unit(company.company_id):
                pass
        assert error.value.code == "busy"
        assert 2.5 <= time.monotonic() - started < 5
    finally:
        release.set()
        thread.join(5)
    assert not failures and not thread.is_alive()


@pytest.mark.parametrize(
    "view,as_of",
    [("current", "2026-01"), ("as_posted", None), ("unknown", None), ("as_posted", "2026-13")],
)
def test_read_semantics_reject_inconsistent_scope(storage_case, view, as_of):
    storage, company = storage_case
    with pytest.raises(ContractError):
        with storage.read_unit(company.company_id, view=view, as_of=as_of):
            pass
