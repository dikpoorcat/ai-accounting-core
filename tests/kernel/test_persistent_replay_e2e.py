"""A real synthetic company exercises durable replay through LocalService."""

from __future__ import annotations

import pytest

pytest_plugins = ("test_replay_close_range",)

from ai_accounting.kernel.backup import run_backup_jobs, verify_portable  # noqa: E402
from ai_accounting.kernel.contracts import KernelError  # noqa: E402
from ai_accounting.kernel.replay_runner import run_ready  # noqa: E402
from ai_accounting.kernel.service import LocalService  # noqa: E402
from ai_accounting.kernel.types import digest  # noqa: E402


def test_persistent_typed_replay_survives_response_loss_and_restart_and_seals_one_backup(
    replay_book, monkeypatch
):
    book = replay_book
    source_manifest = [
        {
            "source_id": "owner-confirmation",
            "kind": "confirmation",
            "period": None,
            "evidence_digest": book.proof,
            "disposition": "reference",
            "depends_on": [],
        }
    ]
    steps = []
    previous_close = None
    for month in ("2026-01", "2026-02", "2026-03"):
        suffix = month.replace("-", "")
        review_ref = f"review-{suffix}"
        steps.append(
            {
                "step_id": f"preview-{suffix}",
                "command": "preview_close",
                "payload": {"period": month, "owner_confirmation": book.proof},
                "depends_on": [previous_close] if previous_close else [],
                "output_ref": review_ref,
                "output_fields": ["digest", "epochs", "manifest.opening_calculation_id"],
            }
        )
        steps.append(
            {
                "step_id": f"close-{suffix}",
                "command": "close",
                "payload": {
                    "period": month,
                    "owner_confirmation": book.proof,
                    "preview_digest": {"$replay_ref": review_ref, "field": "digest"},
                    "epochs": {"$replay_ref": review_ref, "field": "epochs"},
                },
                "depends_on": [f"preview-{suffix}"],
            }
        )
        previous_close = f"close-{suffix}"
    prepare_request = {
        "company_id": book.company,
        "database_id": book.database,
        "first_period": "2026-01",
        "last_period": "2026-03",
        "owner_confirmation": book.proof,
        "source_manifest_digest": digest(source_manifest).hex(),
        "source_manifest": source_manifest,
        "preserved_evidence": [book.proof],
        "steps": steps,
        "request_id": "typed-three-month-plan",
    }
    root = book.app.catalog.root
    book.app.close()
    replay_scope = {**book.config, "targets": [dict(book.config["targets"][0])]}
    replay_scope["targets"][0]["last_period"] = "2026-04"
    planning = LocalService(root, replay_scope=replay_scope)
    plan = planning.dispatch(
        "prepare_replay",
        {"company_id": book.company, "request": prepare_request},
        session_token=book.token,
    )
    confirm_request = {
        "plan_id": plan["plan_id"],
        "plan_digest": plan["plan_digest"],
        "request_id": "typed-three-month-confirm",
    }
    queued = planning.dispatch(
        "confirm_replay",
        {"company_id": book.company, "request": confirm_request},
        session_token=book.token,
    )
    assert queued == {"status": "pending", "run_id": queued["run_id"]}
    # A lost HTTP response resubmits the exact typed body and recovers one run.
    assert (
        planning.dispatch(
            "confirm_replay",
            {"company_id": book.company, "request": confirm_request},
            session_token=book.token,
        )
        == queued
    )

    planning.close()
    resumed = LocalService(root, replay_scope=replay_scope)
    try:
        assert (
            resumed.dispatch(
                "resume_replay",
                {"company_id": book.company, "run_id": queued["run_id"]},
                session_token=book.token,
            )
            == queued
        )
        run_ready(resumed, limit=1, company_id=book.company)  # January preview.
        # Commit January close and lose its typed response. The Engine hook
        # must already have committed the original receipt and output bindings.
        import ai_accounting.kernel.replay_runner as replay_runner

        original_close = replay_runner._dispatch_replay_close
        lost = {"once": False}

        def drop_close_response(service, engine, authority, payload, run_id, scope):
            result = original_close(service, engine, authority, payload, run_id, scope)
            if not lost["once"]:
                lost["once"] = True
                raise OSError("synthetic response lost after close commit")
            return result

        monkeypatch.setattr(replay_runner, "_dispatch_replay_close", drop_close_response)
        first_close = run_ready(resumed, limit=1, company_id=book.company)
        assert first_close[0]["status"] == "completed"
        assert first_close[0]["response_lost"] is True
        # The committed close advanced the source boundary. Original prepare
        # and confirm retries still recover their persisted acknowledgements.
        prepared_retry = resumed.dispatch(
            "prepare_replay",
            {"company_id": book.company, "request": prepare_request},
            session_token=book.token,
        )
        assert prepared_retry["plan_id"] == plan["plan_id"]
        assert prepared_retry["plan_digest"] == plan["plan_digest"]
        assert (
            resumed.dispatch(
                "confirm_replay",
                {"company_id": book.company, "request": confirm_request},
                session_token=book.token,
            )
            == queued
        )
        conflicting = {**confirm_request, "plan_digest": "0" * 64}
        with pytest.raises(KernelError) as conflict:
            resumed.dispatch(
                "confirm_replay",
                {"company_id": book.company, "request": conflicting},
                session_token=book.token,
            )
        assert conflict.value.code == "idempotency_conflict"
        with resumed.engine(book.company).store.connection(read_only=True) as connection:
            assert connection.execute("SELECT count(*) FROM replay_run").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM replay_step").fetchone()[0] == 6
    finally:
        resumed.close()

    resumed = LocalService(root, replay_scope=replay_scope)
    try:
        assert (
            resumed.dispatch(
                "resume_replay",
                {"company_id": book.company, "run_id": queued["run_id"]},
                session_token=book.token,
            )
            == queued
        )
        for _ in range(7):
            run_ready(resumed, limit=1, company_id=book.company)
        status = resumed.dispatch(
            "replay_status",
            {"company_id": book.company, "request": {"run_id": queued["run_id"]}},
            session_token=book.token,
        )
        assert status["status"] == "completed", status
        assert [step["status"] for step in status["steps"]] == ["completed"] * 6
        assert status["progress"]["completed_steps"] == 6
        assert status["progress"]["total_steps"] == 6
        assert "write" in status["progress"]["step_timings_ms"]["close-202601"]
        with resumed.engine(book.company).store.connection(read_only=True) as connection:
            jobs = connection.execute(
                "SELECT id FROM jobs WHERE kind='portable_backup' ORDER BY rowid"
            ).fetchall()
            seals = connection.execute(
                "SELECT count(*) FROM backup_seal WHERE released_at IS NULL"
            ).fetchone()[0]
            close_count = connection.execute("SELECT count(*) FROM period_close").fetchone()[0]
        assert len(jobs) == seals == 1
        outputs = run_backup_jobs(
            resumed.engine(book.company).store.path,
            _bundle=resumed.engine(book.company).store.bundle,
        )
        assert outputs and outputs[0]["status"] == "succeeded"
        verified = verify_portable(
            outputs[0]["result"]["path"],
            _bundle=resumed.engine(book.company).store.bundle,
        )
        assert verified["latest_closed_period"] == "2026-03"
        assert close_count == 3

        # A successor plan starts from the frozen March prefix and carries the
        # completed source/run lineage into its immutable plan summary.
        successor = LocalService(root, replay_scope=replay_scope)
        try:
            successor_manifest = [
                {
                    "source_id": "owner-confirmation",
                    "kind": "confirmation",
                    "period": None,
                    "evidence_digest": book.proof,
                    "disposition": "reference",
                    "depends_on": [],
                }
            ]
            successor_steps = [
                {
                    "step_id": "preview-202604",
                    "command": "preview_close",
                    "payload": {"period": "2026-04", "owner_confirmation": book.proof},
                    "output_ref": "review-202604",
                    "output_fields": ["digest", "epochs", "manifest.opening_calculation_id"],
                },
                {
                    "step_id": "close-202604",
                    "command": "close",
                    "payload": {
                        "period": "2026-04",
                        "owner_confirmation": book.proof,
                        "preview_digest": {"$replay_ref": "review-202604", "field": "digest"},
                        "epochs": {"$replay_ref": "review-202604", "field": "epochs"},
                    },
                    "depends_on": ["preview-202604"],
                },
            ]
            successor_request = {
                "company_id": book.company,
                "database_id": book.database,
                "first_period": "2026-04",
                "last_period": "2026-04",
                "owner_confirmation": book.proof,
                "source_manifest_digest": digest(successor_manifest).hex(),
                "source_manifest": successor_manifest,
                "preserved_evidence": [book.proof],
                "steps": successor_steps,
                "request_id": "typed-april-successor-plan",
            }
            successor_plan = successor.dispatch(
                "prepare_replay",
                {"company_id": book.company, "request": successor_request},
                session_token=book.token,
            )
            assert successor_plan["source_summary"]["inherited_completed_history"] == [
                {
                    "plan_id": plan["plan_id"],
                    "run_id": queued["run_id"],
                    "request_id": "typed-three-month-plan",
                    "first_period": "2026-01",
                    "last_period": "2026-03",
                    "source_manifest_digest": digest(source_manifest).hex(),
                    "plan_digest": plan["plan_digest"],
                }
            ]
        finally:
            successor.close()
    finally:
        resumed.close()
