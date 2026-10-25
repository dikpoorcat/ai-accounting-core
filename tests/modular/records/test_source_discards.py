"""Explicit unused-source retirement and resumable original cleanup, never a sweep."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from test_documents import _first_facts, _prepare, _publish, company_identity

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records import _sources
from ai_accounting_modular.records.public import RecordsChange, RecordsContext, create_records


def store(case, *, file=False, label="真实资料"):
    storage, (company, _), records = case
    with storage.read_unit(company.company_id) as read:
        if file:
            relative = "originals/unassigned/" + label + ".txt"
            incoming = company.company_root / relative
            incoming.parent.mkdir(parents=True, exist_ok=True)
            incoming.write_text(label, encoding="utf-8")
            source = records.prepare_source({"kind": "file", "relative_path": relative}, read)
        else:
            source = records.prepare_source({"kind": "owner_statement", "text": label}, read)
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
    return source


def discard(case, ids, *, retained=(), protected=()):
    storage, (company, _), records = case
    with storage.write_unit(company.company_id) as unit:
        return records.discard_sources(
            tuple(ids), unit, retained_source_ids=retained, protected_paths=protected
        )


def finish(case, **kwargs):
    storage, (company, _), records = case
    with storage.write_unit(company.company_id) as unit:
        return records.finish_source_discards(unit, protected_paths=(), **kwargs)


def marker(case, marker_id):
    return case[1][0].company_root / "runtime/source-discards" / (marker_id + ".json")


def test_statement_retirement_is_explicit_atomic_and_idempotent(records_case):
    storage, (company, _), records = records_case
    source = store(records_case)
    unprocessed = store(records_case, label="尚未处理的说明")
    missing = str(uuid4())
    with storage.read_unit(company.company_id) as read:
        before = read.head
        targets = read.repository("records").targets()
    result = discard(records_case, (source["source_id"], missing))
    assert result.discarded_source_ids == (source["source_id"],)
    assert result.missing_source_ids == (missing,)
    assert result.marker_ids == ()
    with storage.read_unit(company.company_id) as read:
        assert read.head == before + 1
        assert records.resolve_sources((source["source_id"],), read) == ()
        assert records.resolve_sources((unprocessed["source_id"],), read) == (unprocessed,)
        assert read.repository("records").targets() == targets
    retry = discard(records_case, (source["source_id"], missing))
    assert retry.discarded_source_ids == ()
    assert set(retry.missing_source_ids) == {source["source_id"], missing}
    assert finish(records_case).pending_marker_ids == ()
    with storage.read_unit(company.company_id) as read:
        assert read.head == before + 1


@pytest.mark.parametrize("owner_kind", ["business", "object", "issue", "note", "retained"])
def test_any_current_or_retained_reference_rejects_the_whole_batch(records_case, owner_kind):
    storage, (company, _), records = records_case
    unused = store(records_case, file=True, label="未用原件")
    used = store(records_case, label="必要依据")
    retained = ()
    if owner_kind == "retained":
        retained = (used["source_id"],)
    else:
        with storage.write_unit(company.company_id) as unit:
            records.replace_source_uses(
                owner_kind,
                "company" if owner_kind == "note" else str(uuid4()),
                (used["source_id"],),
                unit,
            )
    with pytest.raises(ContractError) as error:
        discard(records_case, (unused["source_id"], used["source_id"]), retained=retained)
    assert error.value.code == "source_in_use"
    with storage.read_unit(company.company_id) as read:
        assert len(records.resolve_sources((used["source_id"], unused["source_id"]), read)) == 2
    assert not (company.company_root / "runtime/source-discards").exists()


def test_commit_precedes_unlink_and_cleanup_does_not_change_business_head(records_case):
    storage, (company, _), records = records_case
    source = store(records_case, file=True)
    path = company.company_root / source["relative_path"]
    with storage.write_unit(company.company_id) as unit:
        result = records.discard_sources(
            (source["source_id"],), unit, retained_source_ids=(), protected_paths=()
        )
        assert path.exists()
        premature = records.finish_source_discards(
            unit, marker_ids=result.marker_ids, protected_paths=()
        )
        assert premature.pending_marker_ids == result.marker_ids
        assert path.exists()
    with storage.read_unit(company.company_id) as read:
        head = read.head
    cleaned = finish(records_case, marker_ids=result.marker_ids)
    assert cleaned.completed_marker_ids == result.marker_ids
    assert cleaned.pending_marker_ids == ()
    assert not path.exists()
    assert not marker(records_case, result.marker_ids[0]).exists()
    assert finish(records_case, marker_ids=result.marker_ids).pending_marker_ids == ()
    with storage.read_unit(company.company_id) as read:
        assert read.head == head


def test_rollback_marker_is_canceled_on_restart_without_deleting_original(records_case):
    storage, (company, _), records = records_case
    source = store(records_case, file=True)
    path = company.company_root / source["relative_path"]
    with pytest.raises(RuntimeError):
        with storage.write_unit(company.company_id) as unit:
            result = records.discard_sources(
                (source["source_id"],), unit, retained_source_ids=(), protected_paths=()
            )
            raise RuntimeError("synthetic crash before commit")
    restarted = create_records(lambda read: storage.company_metadata(read).company_root)
    with storage.write_unit(company.company_id) as unit:
        cleaned = restarted.finish_source_discards(unit, protected_paths=())
    assert cleaned.completed_marker_ids == result.marker_ids
    assert path.exists()
    with storage.read_unit(company.company_id) as read:
        assert records.resolve_sources((source["source_id"],), read) == (source,)


def test_file_cleanup_preserves_reimported_source_and_uses_distinct_markers(records_case):
    storage, (company, _), records = records_case
    source = store(records_case, file=True)
    path = company.company_root / source["relative_path"]
    first = discard(records_case, (source["source_id"],))
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
    assert finish(records_case, marker_ids=first.marker_ids).pending_marker_ids == ()
    assert path.exists()
    second = discard(records_case, (source["source_id"],))
    assert first.marker_ids != second.marker_ids
    finish(records_case, marker_ids=first.marker_ids)
    assert path.exists()
    assert marker(records_case, second.marker_ids[0]).exists()
    finish(records_case, marker_ids=second.marker_ids)
    assert not path.exists()


@pytest.mark.parametrize("protection", ["prepared", "accepted"])
def test_pinned_original_is_rejected_before_database_delete(records_case, protection):
    storage, (company, _), records = records_case
    source = store(records_case, file=True)
    with ExitStack() as lifetime:
        protected = (source["relative_path"],) if protection == "accepted" else ()
        if protection == "prepared":
            with storage.read_unit(company.company_id) as read:
                lifetime.enter_context(records.prepare_sources((source,), read))
        with pytest.raises(ContractError) as error:
            discard(records_case, (source["source_id"],), protected=protected)
        assert error.value.code == "source_in_use"
    assert discard(records_case, (source["source_id"],)).discarded_source_ids


@pytest.mark.parametrize("protection", ["prepared", "accepted"])
def test_late_pin_defers_cleanup_and_completed_source_lease_remains_readable(
    records_case, protection
):
    storage, (company, _), records = records_case
    source = store(records_case, file=True)
    path = company.company_root / source["relative_path"]
    with ExitStack() as lifetime:
        with storage.read_unit(company.company_id) as read:
            lease = lifetime.enter_context(records.lease_source(source["source_id"], read))
        result = discard(records_case, (source["source_id"],))
        with ExitStack() as pin:
            protected = (source["relative_path"],) if protection == "accepted" else ()
            if protection == "prepared":
                with storage.read_unit(company.company_id) as read:
                    pin.enter_context(records.prepare_sources((source,), read))
            with storage.write_unit(company.company_id) as unit:
                pending = records.finish_source_discards(
                    unit, marker_ids=result.marker_ids, protected_paths=protected
                )
            assert pending.pending_marker_ids == result.marker_ids
            assert path.exists()
        finish(records_case)
        assert not path.exists()
        assert lease.read() == "真实资料".encode()


@pytest.mark.parametrize("damage", ["missing", "content", "io"])
def test_missing_file_is_complete_but_changed_or_locked_file_remains_pending(
    records_case, monkeypatch, damage
):
    source = store(records_case, file=True)
    path = records_case[1][0].company_root / source["relative_path"]
    result = discard(records_case, (source["source_id"],))
    if damage == "missing":
        path.unlink()
    elif damage == "content":
        path.write_bytes(b"X" * source["byte_size"])
    else:
        original = Path.unlink

        def locked(self, *args, **kwargs):
            if self == path:
                raise PermissionError("synthetic original lock")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", locked)
    cleaned = finish(records_case)
    if damage == "missing":
        assert cleaned.completed_marker_ids == result.marker_ids
        assert not marker(records_case, result.marker_ids[0]).exists()
    else:
        assert cleaned.pending_marker_ids == result.marker_ids
        assert path.exists()
        assert marker(records_case, result.marker_ids[0]).exists()


def test_marker_cannot_target_another_database_or_escape_originals(records_case):
    source = store(records_case, file=True)
    result = discard(records_case, (source["source_id"],))
    target = marker(records_case, result.marker_ids[0])
    raw = json.loads(target.read_text("utf-8"))
    raw["database_id"] = str(uuid4())
    target.write_text(json.dumps(raw), "utf-8")
    assert finish(records_case).pending_marker_ids == result.marker_ids
    raw["database_id"] = records_case[1][0].database_id
    raw["relative_path"] = "../outside.txt"
    target.write_text(json.dumps(raw), "utf-8")
    assert finish(records_case).pending_marker_ids == result.marker_ids
    assert (records_case[1][0].company_root / source["relative_path"]).exists()


def test_marker_write_failure_precedes_any_database_removal(records_case, monkeypatch):
    storage, (company, _), records = records_case
    sources = (
        store(records_case, file=True, label="甲"),
        store(records_case, file=True, label="乙"),
    )
    original = _sources.atomic_write
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic marker failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(_sources, "atomic_write", fail_second)
    with pytest.raises(OSError):
        discard(records_case, tuple(source["source_id"] for source in sources))
    with storage.read_unit(company.company_id) as read:
        assert (
            len(records.resolve_sources(tuple(source["source_id"] for source in sources), read))
            == 2
        )
    assert not finish(records_case).pending_marker_ids
    assert all((company.company_root / source["relative_path"]).exists() for source in sources)


def test_repeated_discard_finds_its_pending_marker_even_with_broken_body(records_case):
    source = store(records_case, file=True)
    first = discard(records_case, (source["source_id"],))
    marker(records_case, first.marker_ids[0]).write_bytes(b"broken marker body")
    retried = discard(records_case, (source["source_id"],))
    assert retried.marker_ids == first.marker_ids
    assert retried.missing_source_ids == (source["source_id"],)
    assert not discard(records_case, (str(uuid4()),)).marker_ids
    cleanup = finish(records_case, marker_ids=retried.marker_ids)
    assert cleanup.pending_marker_ids == first.marker_ids
    assert cleanup.pending_source_ids == (source["source_id"],)
    assert cleanup.errors[0].marker_id == first.marker_ids[0]


def test_cleanup_pages_continue_past_a_pinned_first_marker_and_do_not_scan_unknown_files(
    records_case,
):
    sources = tuple(store(records_case, file=True, label=str(number)) for number in range(3))
    result = discard(records_case, tuple(source["source_id"] for source in sources))
    first_id = result.marker_ids[0]
    pinned_source = next(source for source in sources if first_id.startswith(source["source_id"]))
    unknown = marker(records_case, "not-a-cleanup-task")
    unknown.write_text("keep", encoding="utf-8")
    storage, (company, _), records = records_case
    with storage.write_unit(company.company_id) as unit:
        page = records.finish_source_discards(
            unit, protected_paths=(pinned_source["relative_path"],), limit=1
        )
    assert page.pending_marker_ids == (first_id,)
    assert page.pending_source_ids == (pinned_source["source_id"],)
    seen = list(page.pending_marker_ids)
    while page.next_marker_id is not None:
        page = finish(records_case, limit=1, after_marker_id=page.next_marker_id)
        seen.extend(page.completed_marker_ids)
    assert seen == list(result.marker_ids)
    assert unknown.read_text("utf-8") == "keep"
    assert marker(records_case, first_id).exists()
    assert finish(records_case).completed_marker_ids == (first_id,)


def test_one_corrupt_marker_does_not_block_other_original_cleanup(records_case):
    sources = tuple(store(records_case, file=True, label=str(number)) for number in range(2))
    result = discard(records_case, tuple(source["source_id"] for source in sources))
    bad, good = result.marker_ids
    marker(records_case, bad).write_bytes(b"bad")
    cleanup = finish(records_case)
    assert cleanup.completed_marker_ids == (good,)
    assert cleanup.pending_marker_ids == (bad,)
    assert cleanup.pending_source_ids == (bad.split("--")[0],)
    assert len(cleanup.errors) == 1


@pytest.mark.parametrize("rollback", [False, True])
def test_a_second_real_writer_waits_for_commit_or_rollback_before_cleanup(records_case, rollback):
    storage, (company, _), records = records_case
    source = store(records_case, file=True)
    path = company.company_root / source["relative_path"]
    marker_ready, release, cleanup_started = Event(), Event(), Event()

    def first_writer():
        try:
            with storage.write_unit(company.company_id) as unit:
                records.discard_sources(
                    (source["source_id"],), unit, retained_source_ids=(), protected_paths=()
                )
                marker_ready.set()
                assert release.wait(10)
                if rollback:
                    raise RuntimeError("synthetic transaction rollback")
        except RuntimeError:
            if not rollback:
                raise

    def second_writer():
        cleanup_started.set()
        return finish(records_case)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_writer)
        assert marker_ready.wait(10)
        second = executor.submit(second_writer)
        try:
            assert cleanup_started.wait(10)
            assert not second.done()
            assert path.exists()
        finally:
            release.set()
        first.result(timeout=10)
        assert not second.result(timeout=10).pending_marker_ids
    assert path.exists() is rollback


def test_unused_retirement_leaves_final_documents_unchanged_and_next_backup_has_no_junk(
    records_case,
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    source = store(records_case, file=True, label="确认无用的导入原件")
    statement = store(records_case, label="已被替换的错误说明")
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    manifest = (company.company_root / "standard/current.json").read_bytes()
    with storage.read_unit(company.company_id) as read:
        before = records.verify_snapshot(company_identity(company), [fact], [obj], read)
        assert source["relative_path"] in {file.relative_path for file in before.files}
        with records.capture_backup_materials(read, str(uuid4())) as captured:
            assert source["relative_path"] in {file["relative_path"] for file in captured.inputs}
            statement_result = discard(records_case, (statement["source_id"],))
            assert statement_result.discarded_source_ids == (statement["source_id"],)
            assert not statement_result.marker_ids
            with pytest.raises(ContractError, match="仍被"):
                discard(records_case, (source["source_id"],))
    discard(records_case, (source["source_id"], statement["source_id"]))
    finish(records_case)
    assert (company.company_root / "standard/current.json").read_bytes() == manifest
    with storage.read_unit(company.company_id) as read:
        assert records.document_status(read)["state"] == "ready"
        after = records.verify_snapshot(company_identity(company), [fact], [obj], read)
        assert after.document_set_id == before.document_set_id
        assert source["relative_path"] not in {file.relative_path for file in after.files}
        with records.capture_backup_materials(read, str(uuid4())) as captured:
            assert source["relative_path"] not in {
                file["relative_path"] for file in captured.inputs
            }
        assert records.resolve_sources((statement["source_id"], source["source_id"]), read) == ()


def test_read_only_units_cannot_retire_or_finish_sources(records_case):
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as error:
            records.discard_sources((), read, retained_source_ids=(), protected_paths=())
        assert error.value.code == "invalid_operation"
        with pytest.raises(ContractError) as error:
            records.finish_source_discards(read, protected_paths=())
        assert error.value.code == "invalid_operation"


@pytest.mark.parametrize("protection", ["current", "retained", "path", "lease"])
def test_retain_mode_uses_shared_current_protection_and_still_retires_other_sources(
    records_case, protection
):
    storage, (company, _), records = records_case
    retained = store(records_case, file=True, label="本次应保留的原件")
    unused = store(records_case, file=True, label="本次可以退出的原件")
    retained_ids = (retained["source_id"],) if protection == "retained" else ()
    protected_paths = (retained["relative_path"],) if protection == "path" else ()
    wanted = (retained["source_id"], unused["source_id"])
    if protection == "current":
        with storage.write_unit(company.company_id) as unit:
            records.replace_source_uses("note", "company", (retained["source_id"],), unit)
    with ExitStack() as stack:
        if protection == "lease":
            read = stack.enter_context(storage.read_unit(company.company_id))
            assert (
                records.source_discard_protection(
                    wanted, read, retained_source_ids=(), protected_paths=()
                )
                == ()
            )
            stack.enter_context(records.prepare_sources((retained,), read))
        with storage.read_unit(company.company_id) as read:
            assert records.source_discard_protection(
                wanted,
                read,
                retained_source_ids=retained_ids,
                protected_paths=protected_paths,
            ) == (retained["source_id"],)
        with storage.write_unit(company.company_id) as unit:
            result = records.discard_sources(
                wanted,
                unit,
                retained_source_ids=retained_ids,
                protected_paths=protected_paths,
                retain_protected=True,
            )
        assert result.retained_source_ids == (retained["source_id"],)
        assert result.discarded_source_ids == (unused["source_id"],)
        assert result.missing_source_ids == ()
        assert all(value.startswith(unused["source_id"]) for value in result.marker_ids)
        assert (company.company_root / unused["relative_path"]).exists()
        with storage.write_unit(company.company_id) as unit:
            cleaned = records.finish_source_discards(
                unit, marker_ids=result.marker_ids, protected_paths=()
            )
        assert cleaned.pending_marker_ids == ()
        assert not (company.company_root / unused["relative_path"]).exists()
        assert (company.company_root / retained["relative_path"]).exists()
        with storage.read_unit(company.company_id) as read:
            assert records.resolve_sources(wanted, read) == (retained,)


def test_default_discard_rejects_a_live_preparation_lease_atomically(records_case):
    storage, (company, _), records = records_case
    retained = store(records_case, file=True, label="正在核验的原件")
    unused = store(records_case, file=True, label="同批未用原件")
    wanted = (unused["source_id"], retained["source_id"])
    with storage.read_unit(company.company_id) as read:
        before = read.head
        with records.prepare_sources((retained,), read):
            with pytest.raises(ContractError) as error:
                discard(records_case, wanted)
            assert error.value.code == "source_in_use"
    with storage.read_unit(company.company_id) as read:
        assert read.head == before
        assert set(value["source_id"] for value in records.resolve_sources(wanted, read)) == set(
            wanted
        )
    assert not (company.company_root / "runtime/source-discards").exists()
    assert (company.company_root / retained["relative_path"]).exists()
    assert (company.company_root / unused["relative_path"]).exists()


@pytest.mark.parametrize("committed", [False, True])
def test_real_process_loss_before_or_after_commit_resumes_exact_original_cleanup(
    records_case, committed
):
    storage, (company, _), records = records_case
    source = store(records_case, file=True)
    path = company.company_root / source["relative_path"]
    script = """
import os
import sys
from pathlib import Path
import ai_accounting_modular
from ai_accounting_modular.records.public import create_records, repository_factory
from ai_accounting_modular.storage.public import Storage
assert Path(ai_accounting_modular.__file__).resolve().is_relative_to(Path(sys.argv[5]).resolve())
storage = Storage(sys.argv[1], repositories=(repository_factory(),))
records = create_records(lambda read: storage.company_metadata(read).company_root)
with storage.write_unit(sys.argv[2]) as unit:
    records.discard_sources((sys.argv[3],), unit, retained_source_ids=(), protected_paths=())
    if sys.argv[4] == 'before':
        os._exit(38)
os._exit(39)
"""
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(storage.catalog_path),
            company.company_id,
            source["source_id"],
            "after" if committed else "before",
            str(Path(__file__).resolve().parents[3] / "src"),
        ],
        capture_output=True,
        timeout=60,
    )
    assert crashed.returncode == (39 if committed else 38), crashed.stderr.decode("utf-8")
    assert path.exists()
    restarted = create_records(lambda read: storage.company_metadata(read).company_root)
    with storage.write_unit(company.company_id) as unit:
        cleanup = restarted.finish_source_discards(unit, protected_paths=())
    assert len(cleanup.completed_marker_ids) == 1
    assert cleanup.pending_marker_ids == ()
    assert path.exists() is (not committed)
    with storage.read_unit(company.company_id) as read:
        assert bool(records.resolve_sources((source["source_id"],), read)) is (not committed)


def test_pending_hint_reads_only_known_marker_names_not_bodies_or_originals(
    records_case, monkeypatch
):
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        assert records.has_source_discards(read) is False
    source = store(records_case, file=True)
    discarded = discard(records_case, (source["source_id"],))
    target = marker(records_case, discarded.marker_ids[0])
    raw = target.read_bytes()
    target.write_bytes(b"corrupt")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", lambda *a, **kw: pytest.fail("提示不能读取正文"))
        with storage.read_unit(company.company_id) as read:
            assert records.has_source_discards(read) is True
    target.write_bytes(raw)
    finish(records_case)
    marker(records_case, "unknown-file").write_text("keep", encoding="utf-8")
    with storage.read_unit(company.company_id) as read:
        assert records.has_source_discards(read) is False
