"""Source I/O is finished before atomic bookkeeping, without trusting stale inputs."""

import hashlib
import os
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records import _sources
from ai_accounting_modular.records.public import RecordsChange, RecordsContext


def file_source(case):
    storage, (company, _), records = case
    incoming = company.company_root / "originals/unassigned/input.txt"
    incoming.parent.mkdir(parents=True, exist_ok=True)
    incoming.write_bytes(b"verified original bytes")
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {"kind": "file", "relative_path": "originals/unassigned/input.txt"}, read
        )
    return source, company.company_root / source["relative_path"]


def test_source_preflight_hashes_without_making_unused_full_byte_copies(records_case, monkeypatch):
    storage, (company, _), records = records_case
    source, _ = file_source(records_case)

    def no_copy(*args, **kwargs):
        raise AssertionError("单纯核验不得复制整份原件到临时文件。")

    monkeypatch.setattr(_sources.tempfile, "SpooledTemporaryFile", no_copy)
    with storage.read_unit(company.company_id) as read:
        with records.prepare_sources((source,), read):
            pass


@pytest.mark.parametrize("when", ["before_plan", "after_plan", "before_commit"])
def test_equal_size_overwrite_with_restored_mtime_is_rejected(records_case, when):
    storage, (company, _), records = records_case
    source, path = file_source(records_case)

    def overwrite():
        before = path.stat()
        path.write_bytes(b"X" * source["byte_size"])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_mtime_ns == before.st_mtime_ns

    with ExitStack() as lifetime:
        with storage.read_unit(company.company_id) as read:
            prepared = lifetime.enter_context(records.prepare_sources((source,), read))
        with pytest.raises(ContractError, match="改变"):
            with storage.write_unit(company.company_id) as unit:
                if when == "before_plan":
                    overwrite()
                plan = records.plan_change(
                    RecordsChange(sources=(source,)),
                    RecordsContext(unit, str(uuid4()), prepared_sources=prepared),
                )
                if when == "after_plan":
                    overwrite()
                records.apply_current(plan, unit)
                if when == "before_commit":
                    overwrite()
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0
        assert records.resolve_sources((source["source_id"],), read) == ()


def test_prepared_sources_outlive_the_read_and_do_no_writer_file_reads(records_case, monkeypatch):
    storage, (company, _), records = records_case
    source, path = file_source(records_case)
    with ExitStack() as lifetime:
        with storage.read_unit(company.company_id) as read:
            prepared = lifetime.enter_context(records.prepare_sources((source,), read))
        with monkeypatch.context() as patch:
            original_open = Path.open

            def no_original_read(self, *args, **kwargs):
                assert self != path, "业务写事务不能重新读取原件正文。"
                return original_open(self, *args, **kwargs)

            patch.setattr(Path, "open", no_original_read)
            with storage.write_unit(company.company_id) as unit:
                plan = records.plan_change(
                    RecordsChange(sources=(source,)),
                    RecordsContext(unit, str(uuid4()), prepared_sources=prepared),
                )
                records.apply_current(plan, unit)
    with storage.read_unit(company.company_id) as read:
        assert records.resolve_sources((source["source_id"],), read) == (source,)
        assert read.head == 1


@pytest.mark.parametrize("when", ["before_plan", "after_plan", "before_commit"])
def test_changed_preverified_original_rejects_and_rolls_back(records_case, when):
    storage, (company, _), records = records_case
    source, path = file_source(records_case)
    with ExitStack() as lifetime:
        with storage.read_unit(company.company_id) as read:
            prepared = lifetime.enter_context(records.prepare_sources((source,), read))
        with pytest.raises(ContractError) as error:
            with storage.write_unit(company.company_id) as unit:
                if when == "before_plan":
                    path.write_bytes(b"changed")
                plan = records.plan_change(
                    RecordsChange(sources=(source,)),
                    RecordsContext(unit, str(uuid4()), prepared_sources=prepared),
                )
                if when == "after_plan":
                    path.unlink()
                records.apply_current(plan, unit)
                if when == "before_commit":
                    path.write_bytes(b"changed after save")
        assert error.value.code in {"source_changed", "source_unavailable"}
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0
        assert records.resolve_sources((source["source_id"],), read) == ()


@pytest.mark.parametrize("fault", ["closed", "company", "body", "closed_before_commit"])
def test_preparation_is_live_and_bound_to_exact_company_and_sources(records_case, fault):
    storage, (company, other), records = records_case
    source, _ = file_source(records_case)
    with ExitStack() as lifetime:
        with storage.read_unit(company.company_id) as read:
            prepared = lifetime.enter_context(records.prepare_sources((source,), read))
        if fault == "closed":
            lifetime.close()
        target = other if fault == "company" else company
        supplied = source | {"media_type": "application/pdf"} if fault == "body" else source
        with pytest.raises(ContractError) as error:
            with storage.write_unit(target.company_id) as unit:
                plan = records.plan_change(
                    RecordsChange(sources=(supplied,)),
                    RecordsContext(unit, str(uuid4()), prepared_sources=prepared),
                )
                records.apply_current(plan, unit)
                if fault == "closed_before_commit":
                    lifetime.close()
        assert error.value.code in {"invalid_operation", "conflict"}
    with storage.read_unit(target.company_id) as read:
        assert read.head == 0


def test_preflight_hashes_bytes_not_just_size_and_rejects_writer(records_case):
    storage, (company, _), records = records_case
    source, path = file_source(records_case)
    path.write_bytes(b"x" * source["byte_size"])
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as error, records.prepare_sources((source,), read):
            pass
        assert error.value.code == "source_corrupt"
    with storage.write_unit(company.company_id) as unit:
        with pytest.raises(ContractError) as error, records.prepare_sources((), unit):
            pass
        assert error.value.code == "invalid_operation"


def test_complete_internal_source_set_can_exceed_wire_submission_budget(records_case):
    storage, (company, _), records = records_case
    sources = tuple(
        {
            "source_id": str(uuid4()),
            "kind": "owner_statement",
            "text": str(index),
            "sha256": hashlib.sha256(str(index).encode()).hexdigest(),
        }
        for index in range(501)
    )
    with ExitStack() as lifetime:
        with storage.read_unit(company.company_id) as read:
            prepared = lifetime.enter_context(records.prepare_sources(sources, read))
        with storage.write_unit(company.company_id) as unit:
            records.apply_current(
                records.plan_change(
                    RecordsChange(sources=sources),
                    RecordsContext(unit, str(uuid4()), prepared_sources=prepared),
                ),
                unit,
            )
    with storage.read_unit(company.company_id) as read:
        found = tuple(
            source
            for offset in range(0, len(sources), 500)
            for source in records.resolve_sources(
                [value["source_id"] for value in sources[offset : offset + 500]], read
            )
        )
        assert {value["source_id"] for value in found} == {value["source_id"] for value in sources}
        assert read.head == 1
