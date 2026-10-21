from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records.public import RecordsChange, RecordsContext


def _store_source(storage, company, records, source):
    with storage.write_unit(company.company_id) as unit:
        plan = records.plan_change(
            RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
        )
        records.apply_current(plan, unit)


def test_statement_content_is_fixed_and_retries_do_not_change_head(records_case):
    storage, (company, other), records = records_case
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {"kind": "owner_statement", "text": "老板说明\r\n真实费用", "archive_month": "2026-01"},
            read,
        )
    assert source["text"] == "老板说明\n真实费用"
    assert source["sha256"] == hashlib.sha256(source["text"].encode()).hexdigest()
    _store_source(storage, company, records, source)
    _store_source(storage, company, records, source)
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1
        assert (
            records.prepare_source(
                {"kind": "owner_statement", "text": "老板说明\r\n真实费用"}, read
            )
            == source
        )
        assert records.source_locations((source["source_id"],), read) == [
            {"source_id": source["source_id"]}
        ]
        with records.lease_source(source["source_id"], read) as lease:
            assert lease.read() == source["text"].encode()
        with pytest.raises(ValueError):
            lease.read()
    with storage.read_unit(other.company_id) as read:
        assert records.resolve_sources((source["source_id"],), read) == ()
        with pytest.raises(ContractError, match="当前公司"):
            with records.lease_source(source["source_id"], read):
                pass
    replacement = source | {
        "text": "不同事实",
        "sha256": hashlib.sha256("不同事实".encode()).hexdigest(),
    }
    with pytest.raises(ContractError) as error:
        _store_source(storage, company, records, replacement)
    assert error.value.code == "conflict"


def test_file_import_is_company_scoped_and_lease_is_fixed_verified_bytes(records_case):
    storage, (company, _), records = records_case
    incoming = company.company_root / "originals/unassigned/资料.txt"
    incoming.parent.mkdir(parents=True)
    original = "这是一份真实原字节\r\n100.00".encode()
    incoming.write_bytes(original)
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {
                "kind": "file",
                "relative_path": "originals/unassigned/资料.txt",
                "archive_month": "2026-01",
            },
            read,
        )
    assert source["relative_path"] == f"originals/2026-01/{source['source_id']}/资料.txt"
    _store_source(storage, company, records, source)
    path = company.company_root / source["relative_path"]
    with storage.read_unit(company.company_id) as read:
        assert (
            records.prepare_source(
                {"kind": "file", "relative_path": "originals/unassigned/资料.txt"}, read
            )
            == source
        )
        with records.lease_source(source["source_id"], read) as lease:
            path.write_bytes(b"externally changed")
            assert b"".join(lease.iter_chunks(3)) == original
        with pytest.raises(ContractError) as error:
            with records.lease_source(source["source_id"], read):
                pass
        assert error.value.code == "source_corrupt"


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "originals/../../secret.txt",
        "C:/secret.txt",
        "originals\\secret.txt",
        "/tmp/secret",
        "originals//bad.txt",
    ],
)
def test_source_import_rejects_host_paths_and_parent_traversal(records_case, path):
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError):
            records.prepare_source({"kind": "file", "relative_path": path}, read)


def test_sources_and_uses_rollback_together_and_equal_uses_are_noop(records_case):
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source({"kind": "owner_statement", "text": "当前真实业务"}, read)
    business_id = str(uuid4())
    with pytest.raises(RuntimeError):
        with storage.write_unit(company.company_id) as unit:
            plan = records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            )
            records.apply_current(plan, unit)
            records.replace_source_uses("business", business_id, (source["source_id"],), unit)
            raise RuntimeError("synthetic failure")
    with storage.read_unit(company.company_id) as read:
        assert records.resolve_sources((source["source_id"],), read) == ()
        assert read.head == 0
    _store_source(storage, company, records, source)
    with storage.write_unit(company.company_id) as unit:
        records.replace_source_uses("business", business_id, (source["source_id"],), unit)
    with storage.write_unit(company.company_id) as unit:
        records.replace_source_uses("business", business_id, (source["source_id"],) * 1000, unit)
    with storage.read_unit(company.company_id) as read:
        assert read.head == 2


def test_source_symlink_outside_company_is_rejected(records_case, tmp_path):
    storage, (company, _), records = records_case
    outside = tmp_path / "outside.txt"
    outside.write_text("不能读取")
    link = company.company_root / "originals/unassigned/link.txt"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前Windows账号不能创建符号链接")
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as error:
            records.prepare_source(
                {"kind": "file", "relative_path": "originals/unassigned/link.txt"}, read
            )
        assert error.value.code == "invalid_path"


def test_availability_is_a_cheap_observation_and_download_verifies_bytes(records_case, monkeypatch):
    storage, (company, _), records = records_case
    incoming = company.company_root / "originals/unassigned/source.txt"
    incoming.parent.mkdir(parents=True)
    incoming.write_bytes(b"correct")
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {"kind": "file", "relative_path": "originals/unassigned/source.txt"}, read
        )
        statement = records.prepare_source({"kind": "owner_statement", "text": "实际说明"}, read)
    _store_source(storage, company, records, source)
    _store_source(storage, company, records, statement)
    path = company.company_root / source["relative_path"]
    absent = str(uuid4())
    with storage.read_unit(company.company_id) as read:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", lambda *a, **kw: pytest.fail("列表不可读取原件正文"))
            statuses = records.source_status(
                (source["source_id"], statement["source_id"], absent), read
            )
        by_id = {item.source_id: item for item in statuses}
        assert by_id[source["source_id"]].state == "present"
        assert by_id[source["source_id"]].observed_size == 7
        assert by_id[statement["source_id"]].observed_size == len("实际说明".encode())
        assert by_id[absent].state == "not_found"
        path.write_bytes(b"changed")
        assert records.source_status((source["source_id"],), read)[0].state == "present"
        with pytest.raises(ContractError) as error:
            with records.lease_source(source["source_id"], read):
                pass
        assert error.value.code == "source_corrupt"
        path.write_bytes(b"short")
        status = records.source_status((source["source_id"],), read)[0]
        assert (status.state, status.observed_size) == ("content_mismatch", 5)
        path.unlink()
        status = records.source_status((source["source_id"],), read)[0]
        assert (status.state, status.observed_size) == ("missing_file", None)
        path.mkdir()
        with pytest.raises(ContractError) as error:
            records.source_status((source["source_id"],), read)
        assert error.value.code == "source_unavailable"
