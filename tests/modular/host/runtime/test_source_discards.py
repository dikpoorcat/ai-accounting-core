"""Resume original cleanup only after lease recovery and independent owner work."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.runtime._background import Background
from ai_accounting_modular.host.runtime.public import create_runtime
from ai_accounting_modular.records.public import SourceDiscardCleanup, SourceDiscardError

from .conftest import business_request
from .test_recovery import eventually


def original(runtime, company, name):
    path = company.company_root / "originals/pending" / (name + ".txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("合成原件" + name, encoding="utf-8")
    result = runtime.kernel.application.accounting_sources(
        {
            "company_id": company.company_id,
            "action": "import",
            "sources": [
                {
                    "kind": "file",
                    "relative_path": path.relative_to(company.company_root).as_posix(),
                    "archive_month": "2026-01",
                }
            ],
        }
    )
    return result["sources"][0]


def pending_original(runtime, company, monkeypatch, name="待清理"):
    source = original(runtime, company, name)
    path = company.company_root / source["relative_path"]
    unlink = Path.unlink

    def unavailable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic original temporarily locked")
        return unlink(self, *args, **kwargs)

    with monkeypatch.context() as blocked:
        blocked.setattr(Path, "unlink", unavailable)
        result = runtime.kernel.application.accounting_sources(
            {
                "company_id": company.company_id,
                "action": "discard",
                "source_ids": [source["source_id"]],
            }
        )
    assert result["discarded_source_ids"] == [source["source_id"]]
    assert result["file_cleanup"]["state"] == "pending"
    assert path.exists()
    return path


def test_actual_runtime_restart_finishes_durable_pending_original(local, monkeypatch):
    runtime, company, _ = local
    path = pending_original(runtime, company, monkeypatch)
    catalog = runtime.catalog_path
    runtime.close()
    with create_runtime(catalog, window_opener=lambda _: None) as restarted:
        eventually(lambda: not path.exists())
        with restarted.kernel.storage.read_unit(company.company_id) as read:
            assert not restarted.kernel.records.has_source_discards(read)
        assert company.company_id in restarted.background.recovered


def test_restart_restores_real_backup_protection_before_original_cleanup(local, monkeypatch):
    runtime, company, _ = local
    app = runtime.kernel.application
    assert app.accounting_apply(business_request(company.company_id))["completion"] == "complete"
    keep = original(runtime, company, "备份仍需原件")
    discarded = pending_original(runtime, company, monkeypatch)
    task = app.accounting_backup(
        {"company_id": company.company_id, "expected_database_id": company.database_id}
    )
    assert task["state"] == "pending"
    runtime.close()
    restarted = create_runtime(runtime.catalog_path, window_opener=lambda _: None)
    events = []
    try:
        recover = restarted.kernel.files.recover
        cleanup = restarted.kernel.application.finish_source_discards

        def restore_leases(company_id):
            recover(company_id)
            events.append("recovered")

        def check_protection(company_id, **kwargs):
            assert events == ["recovered", "files-work"]
            with restarted.kernel.storage.read_unit(company_id) as read:
                plans = restarted.kernel.files.retained_input_plans("backup", read)
                assert any(
                    item["source_root"] == "company"
                    and item["relative_path"] == keep["relative_path"]
                    for plan in plans
                    for item in plan["inputs"]
                )
            events.append("cleanup")
            return cleanup(company_id, **kwargs)

        monkeypatch.setattr(restarted.kernel.files, "recover", restore_leases)
        monkeypatch.setattr(
            restarted.kernel.files, "run_pending", lambda company: events.append("files-work")
        )
        monkeypatch.setattr(
            restarted.kernel.application, "finish_source_discards", check_protection
        )
        restarted.background.cycle()
        assert events == ["recovered", "files-work", "cleanup"]
        assert not discarded.exists()
        assert (company.company_root / keep["relative_path"]).is_file()
        with pytest.raises(ContractError) as used:
            restarted.kernel.application.accounting_sources(
                {
                    "company_id": company.company_id,
                    "action": "discard",
                    "source_ids": [keep["source_id"]],
                }
            )
        assert used.value.code == "source_in_use"
    finally:
        restarted.close()


def test_cleanup_error_is_reported_without_holding_actual_reading(local, monkeypatch):
    runtime, company, _ = local
    assert (
        runtime.kernel.application.accounting_apply(business_request(company.company_id))[
            "completion"
        ]
        == "complete"
    )
    path = pending_original(runtime, company, monkeypatch)
    marker = next((company.company_root / "runtime/source-discards").glob("*.json"))
    marker.write_bytes(b"broken synthetic marker")
    query = {
        "company_id": company.company_id,
        "year": "2026",
        "quarter": "1",
        "preparation": "deferred",
    }
    with runtime.kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
        assert response.body["state"] == "preparing"
    errors = []
    monkeypatch.setattr(runtime, "failure", errors.append)
    runtime.background.cycle()
    with runtime.kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
        assert response.body["state"] == "ready"
        assert response.body["response"]["summary"]["current_net_profit_fen"] == "-100000"
    assert any(
        isinstance(error, ContractError) and error.code == "source_cleanup_invalid"
        for error in errors
    )
    assert marker.exists() and path.exists()


def test_idle_cycles_never_open_a_cleanup_writer_or_expand_backup_plans(local, monkeypatch):
    runtime, company, _ = local
    assert (
        runtime.kernel.application.accounting_apply(business_request(company.company_id))[
            "completion"
        ]
        == "complete"
    )
    runtime.background.cycle()
    errors = []

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "empty original cleanup cannot open a writer or read retained backup plans"
        )

    monkeypatch.setattr(runtime.kernel.storage, "write_unit", forbidden)
    monkeypatch.setattr(runtime.kernel.files, "retained_input_plans", forbidden)
    monkeypatch.setattr(runtime, "failure", errors.append)
    for _ in range(3):
        runtime.background.cycle()
    assert errors == []


def scheduler():
    events, failures = [], []
    companies = [SimpleNamespace(company_id=name) for name in ("bad", "good")]
    app = SimpleNamespace(
        accounting_documents=lambda request: (
            events.append((request["company_id"], "documents")) or {"state": "ready"}
        )
    )
    files = SimpleNamespace(
        recover=lambda company: events.append((company, "recover")),
        run_pending=lambda company: events.append((company, "files")),
    )
    kernel = SimpleNamespace(
        application=app,
        files=files,
        reading=SimpleNamespace(
            run_pending=lambda company, **kwargs: events.append((company, "reading"))
        ),
        storage=SimpleNamespace(list_companies=lambda: companies),
    )
    return Background(SimpleNamespace(kernel=kernel, failure=failures.append)), events, failures


def test_pages_advance_and_wrap_by_company_without_repeating_recovery():
    work, events, failures = scheduler()
    calls = []
    pages = {
        "bad": [
            SourceDiscardCleanup((), ("pinned",), next_marker_id="first-end"),
            SourceDiscardCleanup(("later",), ()),
            SourceDiscardCleanup((), ()),
        ],
        "good": [SourceDiscardCleanup((), ())] * 3,
    }

    def cleanup(company, **kwargs):
        calls.append((company, kwargs))
        events.append((company, "cleanup"))
        return pages[company].pop(0)

    work.runtime.kernel.application.finish_source_discards = cleanup
    for _ in range(3):
        work.cycle()
    assert [kwargs["after_marker_id"] for company, kwargs in calls if company == "bad"] == [
        None,
        "first-end",
        None,
    ]
    assert all(kwargs["limit"] == 500 for _, kwargs in calls)
    assert events[:5] == [
        ("bad", item) for item in ("recover", "documents", "reading", "files", "cleanup")
    ]
    assert events.count(("bad", "recover")) == events.count(("good", "recover")) == 1
    assert failures == [] and work.source_discard_cursors == {}


@pytest.mark.parametrize("broken", ["recovery", "cleanup"])
def test_one_company_failure_does_not_block_another_or_independent_reading(broken):
    work, events, failures = scheduler()
    error = ContractError("temporary_failure", "合成清理失败")
    if broken == "recovery":

        def recover(company):
            if company == "bad":
                raise error
            events.append((company, "recover"))

        work.runtime.kernel.files.recover = recover

    def cleanup(company, **kwargs):
        events.append((company, "cleanup"))
        if company == "bad":
            raise error
        return SourceDiscardCleanup((), ())

    work.runtime.kernel.application.finish_source_discards = cleanup
    work.cycle()
    assert ("bad", "reading") in events
    assert ("good", "cleanup") in events
    assert failures == [error]
    assert (("bad", "cleanup") in events) is (broken == "cleanup")


def test_failed_page_retries_same_cursor_and_reported_errors_do_not_stop_next_company():
    work, events, failures = scheduler()
    work.source_discard_cursors["bad"] = "prior-page"
    cursors = []
    first = True

    def cleanup(company, **kwargs):
        nonlocal first
        events.append((company, "cleanup"))
        if company == "good":
            return SourceDiscardCleanup((), ())
        cursors.append(kwargs["after_marker_id"])
        if first:
            first = False
            raise OSError("synthetic directory temporarily unavailable")
        return SourceDiscardCleanup(
            (),
            ("invalid-marker",),
            errors=(
                SourceDiscardError("invalid-marker", "source_cleanup_invalid", "合成标识损坏"),
            ),
            next_marker_id="later-page",
        )

    work.runtime.kernel.application.finish_source_discards = cleanup
    work.cycle()
    work.cycle()
    assert cursors == ["prior-page", "prior-page"]
    assert events.count(("good", "cleanup")) == 2
    assert isinstance(failures[0], OSError)
    assert failures[1].code == "source_cleanup_invalid"
    assert work.source_discard_cursors == {"bad": "later-page"}
