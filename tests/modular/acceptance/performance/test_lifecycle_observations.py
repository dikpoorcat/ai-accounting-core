"""Instrumentation failures retain the already observed endpoint, never qualify it."""

import json
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from . import lifecycle, scenarios


@pytest.fixture
def endpoint(monkeypatch):
    @contextmanager
    def write_unit(company):
        yield object()

    kernel = SimpleNamespace(
        storage=SimpleNamespace(write_unit=write_unit),
        reading=SimpleNamespace(run_pending=lambda company: ()),
    )

    def apply(request):
        with kernel.storage.write_unit("synthetic"):
            pass
        return {"completion": "complete", "documents": "ready"}

    kernel.application = SimpleNamespace(accounting_apply=apply)
    times = iter((1_000_000, 12_000_000))
    monkeypatch.setattr(lifecycle, "perf_counter_ns", lambda: next(times))
    monkeypatch.setattr(lifecycle, "ready", lambda *args: {})
    return kernel


def test_schema_failure_keeps_actual_endpoint_time(endpoint, monkeypatch):
    def invalid(*args):
        raise ValueError("actual post-endpoint contract check failed")

    monkeypatch.setattr(lifecycle, "validate_contract", invalid)
    observation = {"elapsed_ms": None}
    with pytest.raises(ValueError, match="contract check failed"):
        lifecycle.submission(endpoint, {}, "2026-01", observation=observation)
    assert observation["elapsed_ms"] == 11


def test_full_month_post_endpoint_fact_failure_keeps_time(endpoint, monkeypatch):
    request = {"mutations": [{"fact": {"business_id": "actual", "period": "2026-01"}}]}
    planned = SimpleNamespace(requests=(), period="2026-01")
    monkeypatch.setattr(lifecycle, "defer_documents", lambda kernel: nullcontext())
    monkeypatch.setattr(lifecycle, "reconciliation_request", lambda kernel, planned: request)
    monkeypatch.setattr(lifecycle, "validate_contract", lambda *args: None)
    monkeypatch.setattr(lifecycle, "count_businesses", lambda *args: 0)
    observation = {"elapsed_ms": None}
    with pytest.raises(AssertionError):
        lifecycle.whole_month(endpoint, planned, observation=observation)
    assert observation["elapsed_ms"] == 11


def test_failed_scenario_retains_observed_time_but_cannot_be_a_successful_sample(
    tmp_path, monkeypatch
):
    manifest = {
        "intended_scale_months": 2,
        "pending_period": "2026-02",
        "months": 1,
        "businesses_per_month": 240,
        "ending_bank_fen": 1000,
    }
    monkeypatch.setattr(scenarios, "describe", lambda root: manifest)
    monkeypatch.setattr(scenarios, "package", lambda *args: None)
    monkeypatch.setattr(
        scenarios, "prepare_baseline", lambda *args, **kw: SimpleNamespace(cold_reading={})
    )
    monkeypatch.setattr(scenarios, "clone_baseline", lambda *args: None)
    monkeypatch.setattr(
        scenarios, "open_dataset", lambda *args, **kwargs: nullcontext((None, None))
    )
    monkeypatch.setattr(scenarios, "plan_month", lambda *args, **kwargs: None)

    def fail_after_endpoint(kernel, planned, *, observation):
        observation["elapsed_ms"] = 10001
        raise AssertionError("full facts differ after endpoint")

    monkeypatch.setattr(scenarios, "whole_month", fail_after_endpoint)
    result = scenarios.run(
        tmp_path / "source",
        tmp_path / "observations",
        "original",
        "month_archive_and_ready",
        probe=True,
    )
    assert result["status"] == result["summary"]["status"] == "failed"
    assert result["summary"]["samples"] == [None]
    assert [item["elapsed_ms"] for item in result["attempts"]] == [10001, 10001]
    assert all(item["status"] == "failed" for item in result["attempts"])
    assert (
        json.loads((tmp_path / "observations/lifecycle-progress.json").read_text("utf-8")) == result
    )


def test_lifecycle_orchestration_keeps_the_selected_independent_samples(tmp_path, monkeypatch):
    """A pure orchestration oracle, not measured accounting or qualification evidence."""
    manifest = {
        "intended_scale_months": 12,
        "pending_period": "2026-12",
        "months": 11,
        "people": 55,
        "employees": 50,
        "contractors": 5,
        "businesses_per_month": 1000,
        "ending_bank_fen": 1000,
    }
    prepared, copied, committed = [], [], []
    sentinel = SimpleNamespace(cold_reading={"elapsed_ms": 123, "period": "2026-11"})

    def baseline(*args, **kwargs):
        prepared.append((args, kwargs))
        return sentinel

    def clone(base, sample, *args):
        assert base is sentinel
        copied.append(sample)

    def execute(*args, observation):
        saved = json.loads((tmp_path / "report/lifecycle-progress.json").read_text("utf-8"))
        assert saved["status"] == saved["attempts"][-1]["status"] == "running"
        assert saved["attempts"][-1]["phase"] == "commit_and_finish"
        assert all(item["status"] == "complete" for item in saved["attempts"][:-1])
        committed.append(observation)
        return {"elapsed_ms": 60000}

    monkeypatch.setattr(scenarios, "describe", lambda root: manifest)
    monkeypatch.setattr(scenarios, "package", lambda *args: None)
    monkeypatch.setattr(scenarios, "prepare_baseline", baseline)
    monkeypatch.setattr(scenarios, "clone_baseline", clone)
    monkeypatch.setattr(scenarios, "open_dataset", lambda *args, **kw: nullcontext((None, None)))
    monkeypatch.setattr(scenarios, "plan_month", lambda *args, **kw: None)
    monkeypatch.setattr(scenarios, "whole_month", execute)
    result = scenarios.run(
        tmp_path / "source", tmp_path / "report", "original", "month_archive_and_ready"
    )
    assert len(prepared) == 1
    assert prepared[0][1] == {"prior_ready": True}
    assert result["cold_reading"] == sentinel.cold_reading
    assert len(set(copied)) == len(committed) == scenarios.WARMUPS + scenarios.SAMPLES
    assert [item["kind"] for item in result["attempts"]] == ["warmup"] * scenarios.WARMUPS + [
        "sample"
    ] * scenarios.SAMPLES
    assert result["warmups"] == scenarios.WARMUPS and result["sample_count"] == scenarios.SAMPLES
    assert result["status"] == "passed"
    assert result["summary"]["samples"] == [60000] * scenarios.SAMPLES
    assert result["summary"]["status"] == "measured" and "budget" not in result["summary"]
    assert "one verified isolated restore and closed baseline" in result["setup_excluded"]
    assert json.loads((tmp_path / "report/lifecycle-progress.json").read_text("utf-8")) == result


@pytest.mark.parametrize("failure", [ValueError("cold build failed"), KeyboardInterrupt()])
def test_cold_baseline_failure_is_reported_without_fabricating_formal_samples(
    tmp_path, monkeypatch, failure
):
    manifest = {"months": 1, "period_to": "2026-01"}
    monkeypatch.setattr(scenarios, "describe", lambda root: manifest)
    monkeypatch.setattr(scenarios, "package", lambda *args: None)

    def failed(*args, **kwargs):
        raise failure

    monkeypatch.setattr(scenarios, "prepare_baseline", failed)
    with pytest.raises(type(failure)):
        scenarios.run(
            tmp_path / "source", tmp_path / "report", "original", "commit_to_ready", probe=True
        )
    saved = json.loads((tmp_path / "report/lifecycle-progress.json").read_text("utf-8"))
    assert saved["status"] == ("failed" if isinstance(failure, Exception) else "interrupted")
    assert saved["attempts"] == [] and "summary" not in saved
    assert saved["cold_reading"]["state"] == "unavailable"
    assert saved["cold_reading"]["elapsed_ms"] is None
    assert saved["cold_reading"]["error_type"] == type(failure).__name__


def test_interruption_preserves_finished_attempt_and_marks_current_attempt_incomplete(
    tmp_path, monkeypatch
):
    manifest = {
        "intended_scale_months": 2,
        "pending_period": "2026-02",
        "months": 1,
        "businesses_per_month": 240,
        "ending_bank_fen": 1000,
    }
    for name in ("package", "prepare_baseline", "clone_baseline", "plan_month"):
        monkeypatch.setattr(scenarios, name, lambda *args, **kw: SimpleNamespace(cold_reading={}))
    monkeypatch.setattr(scenarios, "describe", lambda root: manifest)
    monkeypatch.setattr(scenarios, "open_dataset", lambda *args, **kw: nullcontext((None, None)))
    calls = []

    def interrupted(*args, observation):
        calls.append(observation)
        if len(calls) == 1:
            return {"elapsed_ms": 7}
        observation["elapsed_ms"] = 11
        raise KeyboardInterrupt

    monkeypatch.setattr(scenarios, "whole_month", interrupted)
    with pytest.raises(KeyboardInterrupt):
        scenarios.run(
            tmp_path / "source",
            tmp_path / "interrupted",
            "original",
            "month_archive_and_ready",
            probe=True,
        )
    saved = json.loads((tmp_path / "interrupted/lifecycle-progress.json").read_text("utf-8"))
    assert saved["status"] == "running" and "summary" not in saved
    assert [item["status"] for item in saved["attempts"]] == ["complete", "running"]
    assert [item["elapsed_ms"] for item in saved["attempts"]] == [7, 11]


def test_failed_atomic_checkpoint_keeps_the_previous_complete_json(tmp_path, monkeypatch):
    scenarios._checkpoint(tmp_path, {"attempts": [{"status": "complete", "elapsed_ms": 7}]})
    original = (tmp_path / "lifecycle-progress.json").read_bytes()

    def failed_replace(*args):
        raise OSError("interrupted atomic replacement")

    monkeypatch.setattr(type(tmp_path), "replace", failed_replace)
    with pytest.raises(OSError, match="atomic replacement"):
        scenarios._checkpoint(tmp_path, {"attempts": [{"status": "running", "elapsed_ms": None}]})
    assert (tmp_path / "lifecycle-progress.json").read_bytes() == original
    assert json.loads(original)["attempts"][0]["elapsed_ms"] == 7
