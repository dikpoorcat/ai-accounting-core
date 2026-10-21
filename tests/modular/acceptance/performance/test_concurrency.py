"""A reduced real overlap probe; no browser timings or acceptance budgets are asserted."""

from collections import Counter
from time import monotonic, sleep, time_ns
from types import SimpleNamespace

import pytest

from ai_accounting_modular.host.runtime.public import create_runtime

from .._support import COMPANY, http
from .concurrency import _future_month, _Workers, prepare, qualify_overlap
from .dataset import build, count_businesses, fixed_clock, open_dataset
from .projections import PAGES, parameters


@pytest.mark.parametrize(
    ("period", "future"),
    (("2026-01", "2026-04"), ("2026-09", "2026-10"), ("2035-12", "2036-01")),
)
def test_work_month_is_after_the_entire_observed_quarter(period, future):
    assert _future_month(period) == future


def test_overlap_requires_real_completed_work_and_real_complete_formal_intervals():
    activities = [
        {"kind": kind, "started_epoch_ms": 100, "finished_epoch_ms": 200, "status": "complete"}
        for kind in ("write", "model", "backup")
    ]
    evidence = {"status": "passed", "activities": activities}
    attempt = dict(kind="sample", status="complete", start_epoch_ms=110, end_epoch_ms=190)
    assert qualify_overlap(evidence, [attempt])["status"] == "passed"
    assert qualify_overlap(evidence, [attempt | {"kind": "warmup"}])["status"] == "failed"
    assert qualify_overlap(evidence, [attempt | {"start_epoch_ms": 300}])["status"] == "failed"
    assert qualify_overlap(evidence, [attempt | {"start_epoch_ms": None}])["status"] == "failed"
    assert qualify_overlap(evidence | {"status": "failed"}, [attempt])["status"] == "failed"
    assert (
        qualify_overlap(evidence | {"activities": activities[:2]}, [attempt])["status"] == "failed"
    )
    assert (
        qualify_overlap(
            evidence, [attempt, attempt | {"start_epoch_ms": 300, "end_epoch_ms": 400}]
        )["status"]
        == "failed"
    )


def test_actual_producer_keys_are_opaque_and_old_scope_production_is_reported():
    scopes, calls = ["opaque future scope"], []

    def produce(company, *, limit):
        calls.append((company, limit))
        return tuple(scopes)

    prepared = SimpleNamespace(period="2026-01", work_period="2026-04", future_scopes=tuple(scopes))
    runtime = SimpleNamespace(kernel=SimpleNamespace(reading=SimpleNamespace(run_pending=produce)))
    workers = _Workers(prepared, runtime)
    assert workers.produce(COMPANY, limit=5) == ("opaque future scope",)
    assert calls == [(COMPANY, 5)]
    assert workers.evidence["activities"][0]["produced_scopes"] == scopes
    assert "unexpected_model_scopes" not in workers.evidence
    scopes[:] = ["opaque old scope"]
    workers.produce(COMPANY)
    assert workers.evidence["unexpected_model_scopes"] == scopes


def test_restored_same_company_writes_models_and_backups_overlap_unchanged_old_pages(tmp_path):
    original = tmp_path / "source"
    manifest = build(original, 1, probe_business_count=240)
    with prepare(original, "aggregate_join") as prepared:
        clone = prepared.root
        assert clone != original and prepared.work_period == "2026-04"
        with fixed_clock(manifest) as clock:
            runtime = create_runtime(clone / "catalog.sqlite3", window_opener=lambda _: None)
            runtime.kernel.dashboard.now = clock.now
            try:
                runtime.start()
                with prepared.during(runtime) as evidence:
                    start = time_ns() / 1_000_000
                    deadline = monotonic() + 30
                    while True:
                        for page in PAGES:
                            response = http(
                                runtime.kernel, page, parameters(COMPANY, "2026-01", page)
                            )
                            assert response["http_status"] == 200
                            assert response["body"]["state"] == "ready", response
                            assert response["body"]["response"] == prepared.baseline[page]
                        completed = Counter(
                            item["kind"]
                            for item in evidence["activities"]
                            if item["status"] == "complete"
                        )
                        if (
                            completed["write"] >= 2
                            and completed["model"] >= 2
                            and completed["backup"]
                        ):
                            break
                        assert not evidence.get("failure"), evidence
                        assert monotonic() < deadline, evidence
                        sleep(0.01)
                    end = time_ns() / 1_000_000
                assert evidence["status"] == "passed" and evidence["old_scope_unchanged"], evidence
                assert evidence["company_id"] == COMPANY
                assert all(item["status"] == "complete" for item in evidence["activities"])
                models = evidence["future_model_versions"]
                assert len(models) == len(set(models)) >= 2
                backups = [item for item in evidence["activities"] if item["kind"] == "backup"]
                assert backups and all(
                    item["byte_size"] > 0 and len(item["sha256"]) == 64 for item in backups
                )
                assert count_businesses(runtime.kernel, "2026-04") == 2
                proof = qualify_overlap(
                    evidence,
                    [
                        dict(
                            kind="sample", status="complete", start_epoch_ms=start, end_epoch_ms=end
                        )
                    ],
                )
                assert proof["status"] == "passed", proof
            finally:
                runtime.close()
    assert not clone.exists()
    with open_dataset(original, "original") as (kernel, _):
        assert count_businesses(kernel, "2026-01") == 240
        assert count_businesses(kernel, "2026-04") == 0
