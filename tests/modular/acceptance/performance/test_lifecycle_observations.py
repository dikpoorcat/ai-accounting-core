"""Instrumentation failures retain the already observed endpoint, never qualify it."""

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
    monkeypatch.setattr(scenarios, "restore", lambda *args: None)
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
