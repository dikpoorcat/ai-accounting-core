"""Only committed replay progress shortens the resident worker's idle wait."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ai_accounting.kernel import jobs


class FakeEvent:
    def __init__(self, stop_after):
        self.stop_after = stop_after
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, timeout):
        self.waits.append(timeout)
        if len(self.waits) >= self.stop_after:
            self.set()
        return self.stopped


class InlineThread:
    def __init__(self, *, target, name, daemon):
        self.target = target
        self.alive = False
        self.join_timeout = None

    def start(self):
        self.alive = True
        self.target()
        self.alive = False

    def is_alive(self):
        return self.alive

    def join(self, timeout):
        self.join_timeout = timeout


def run_worker(monkeypatch, outcomes, *, interval=5.0):
    runner = jobs.JobRunner(SimpleNamespace(), interval=interval)
    runner.stop_event = FakeEvent(len(outcomes))
    iterator = iter(outcomes)

    def run_once():
        value = next(iterator)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(runner, "run_once", run_once)
    monkeypatch.setattr(jobs.threading, "Thread", InlineThread)
    runner.start()
    return runner


@pytest.mark.parametrize("extra", [{}, {"response_lost": True}])
def test_committed_replay_progress_yields_briefly_then_idle_resumes_backoff(monkeypatch, extra):
    runner = run_worker(
        monkeypatch,
        [
            [{"run_id": "run", "step_id": "step", "status": "completed", **extra}],
            [],
        ],
        interval=7.0,
    )
    assert runner.stop_event.waits == [0.01, 7.0]


@pytest.mark.parametrize(
    "outcomes",
    [
        [],
        [{"run_id": "run", "step_id": "step", "status": "blocked"}],
        [{"run_id": "run", "step_id": "step", "status": "failed"}],
        [{"run_id": "run", "status": "completed"}],
        [{"step_id": "step", "status": "completed"}],
        [{"run_id": "", "step_id": "step", "status": "completed"}],
        [{"run_id": "run", "step_id": 1, "status": "completed"}],
        [{"job_id": "ordinary", "status": "completed"}],
        [None, "completed"],
    ],
)
def test_only_explicit_replay_node_commit_shortens_wait(monkeypatch, outcomes):
    runner = run_worker(monkeypatch, [outcomes], interval=7.0)
    assert runner.stop_event.waits == [7.0]


def test_worker_error_uses_idle_backoff_after_previous_progress(monkeypatch):
    runner = run_worker(
        monkeypatch,
        [
            [{"run_id": "run", "step_id": "step", "status": "completed"}],
            RuntimeError("synthetic worker failure"),
            [],
        ],
    )
    assert runner.stop_event.waits == [0.01, 5.0, 5.0]
    assert runner.last_error == "RuntimeError"


def test_stop_during_node_work_does_not_start_another_iteration(monkeypatch):
    runner = jobs.JobRunner(SimpleNamespace())
    runner.stop_event = FakeEvent(100)
    calls = []

    def finish_then_stop():
        calls.append("committed")
        runner.stop_event.set()
        return [{"run_id": "run", "step_id": "step", "status": "completed"}]

    monkeypatch.setattr(runner, "run_once", finish_then_stop)
    monkeypatch.setattr(jobs.threading, "Thread", InlineThread)
    runner.start()
    runner.stop()
    assert calls == ["committed"]
    assert runner.stop_event.is_set()
    assert runner.stop_event.waits == [0.01]
    assert runner.thread.join_timeout == 5


def test_each_worker_round_advances_each_company_once_and_ordinary_jobs_keep_limits(monkeypatch):
    catalog = SimpleNamespace(
        companies=lambda: [{"id": "one"}, {"id": "two"}],
        bind=lambda ident: SimpleNamespace(path=ident, bundle="synthetic"),
    )
    ordinary_calls, replay_calls = [], []

    @contextmanager
    def unlocked(path):
        yield True

    def ordinary(name):
        def run(target, **kwargs):
            path = target if isinstance(target, str) else target.store.path
            ordinary_calls.append((name, path, kwargs))
            return [{"job_id": name + "-" + path, "status": "completed"}]

        return run

    def replay(engine):
        company = engine.store.path
        replay_calls.append(company)
        return [{"run_id": "run-" + company, "step_id": "step-" + company, "status": "completed"}]

    monkeypatch.setattr(jobs, "_worker_lock", unlocked)
    monkeypatch.setattr(jobs, "Engine", lambda store: SimpleNamespace(store=store))
    for name in ("run_backup_jobs", "run_export_jobs", "run_report_jobs", "run_tax_import_jobs"):
        monkeypatch.setattr(jobs, name, ordinary(name))
    monkeypatch.setattr(jobs.threading, "Thread", InlineThread)
    runner = jobs.JobRunner(catalog, callbacks=(replay,))
    runner.stop_event = FakeEvent(2)
    runner.start()
    assert replay_calls == ["one", "two", "one", "two"]
    assert runner.stop_event.waits == [0.01, 0.01]
    assert len(ordinary_calls) == 16
    assert all(kwargs["limit"] == 1 for _, _, kwargs in ordinary_calls)
