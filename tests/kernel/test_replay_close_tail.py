"""A new replay subrange continues the verified close prefix exactly once."""

import copy

import pytest

from ai_accounting.kernel.backup import run_backup_jobs
from ai_accounting.kernel.replay_runner import run_ready

pytest_plugins = ("test_replay_close_range",)


def queue_range(book, first, last, request_id):
    preview = book.preview(first_period=first, last_period=last)
    return book.call("confirm_replay_close_range", **{
        **book.payload(preview, request_id=request_id), "first_period": first, "last_period": last,
    })


def close_january_february(book):
    queued = queue_range(book, "2026-01", "2026-02", "real-prefix")
    for _ in range(5):
        run_ready(book.app, company_id=book.company)
    status = book.call("replay_status", request={"run_id": queued["run_id"]})
    assert status["status"] == "completed"
    # This synthetic first range ends in February, so finish its real final
    # backup before another write, just as the ordinary snapshot guard requires.
    store = book.app.engine(book.company).store
    assert run_backup_jobs(store.path, _bundle=store.bundle)[0]["status"] == "succeeded"
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT period,digest,manifest FROM period_close ORDER BY period"
        )]


def test_new_subrange_closes_march_after_actual_prefix_without_repeating_old_closes(replay_book):
    book = replay_book
    retained = close_january_february(book)
    queued = queue_range(book, "2026-03", "2026-03", "march-only")
    outcome = run_ready(book.app, company_id=book.company)[0]
    assert outcome["status"] == "completed" and outcome["step_id"] == "preview-202603", outcome
    receipt = book.call("replay_status", request={
        "run_id": queued["run_id"], "step_id": "preview-202603",
    })["receipt"]["response"]
    assert receipt["manifest"]["opening_calculation_id"] is None
    assert receipt["manifest"]["previous_close_period"] == "2026-02"
    assert receipt["manifest"]["previous_close_digest"] == retained[-1][1].hex()
    closed = run_ready(book.app, company_id=book.company)[0]
    assert closed["status"] == "completed", closed
    run_ready(book.app, company_id=book.company)
    assert book.call("replay_status", request={"run_id": queued["run_id"]})["status"] == "completed"
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        actual = [tuple(row) for row in connection.execute(
            "SELECT period,digest,manifest FROM period_close ORDER BY period"
        )]
    assert len(actual) == 3 and actual[:2] == retained


@pytest.mark.parametrize("failure", ["missing_opening", "invented_digest", "wrong_month"])
def test_no_opening_requires_an_exact_actual_preceding_close(replay_book, monkeypatch, failure):
    book = replay_book
    if failure == "missing_opening":
        queued = queue_range(book, "2026-01", "2026-03", "no-opening")
        retained = []
    else:
        retained = close_january_february(book)
        queued = queue_range(book, "2026-03", "2026-03", "invalid-prefix")
    dispatch = book.app._dispatch

    def changed_response(command, payload, **kwargs):
        result = dispatch(command, payload, **kwargs)
        if command == "preview_close":
            result = copy.deepcopy(result)
            result["manifest"]["opening_calculation_id"] = None
            if failure == "invented_digest":
                result["manifest"]["previous_close_digest"] = "a" * 64
            elif failure == "wrong_month":
                result["manifest"]["previous_close_period"] = "2026-01"
        return result

    monkeypatch.setattr(book.app, "_dispatch", changed_response)
    outcome = run_ready(book.app, company_id=book.company)[0]
    assert outcome["status"] == "blocked"
    assert outcome["code"] == "replay_opening_adoption_required", outcome
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT period,digest,manifest FROM period_close ORDER BY period"
        )] == retained
        assert connection.execute(
            "SELECT 1 FROM replay_binding WHERE run_id=?", (queued["run_id"],)
        ).fetchone() is None
