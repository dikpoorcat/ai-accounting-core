"""Continuation trusts real, inactive parent receipts without replaying them."""

import json
from dataclasses import replace

import pytest
from test_replay_close_range import PASSWORD
from test_replay_schema_cache import prepare_policy_replay

from ai_accounting.kernel import runtime_snapshot
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.replay_runner import run_ready
from ai_accounting.kernel.response_contracts import validate_response

pytest_plugins = ("test_replay_close_range",)


def partial_parent(book, monkeypatch, *, count=3):
    queued = prepare_policy_replay(book, count=count)
    assert run_ready(book.app, company_id=book.company)[0]["status"] == "completed"
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT p.payload FROM replay_plan p JOIN replay_run r ON r.plan_id=p.id WHERE r.id=?",
            (queued["run_id"],),
        ).fetchone()
        original = json.loads(row[0])["request"]
    old = runtime_snapshot.capture_runtime_snapshot()
    current = replace(old, build_id=old.build_id + "-synthetic-next")
    monkeypatch.setattr(runtime_snapshot, "capture_runtime_snapshot", lambda: current)
    remaining = {
        **original,
        "request_id": "continuation-guards",
        "continuation_from": queued["run_id"],
        "steps": [item for item in original["steps"] if item["step_id"] != "policy-0"],
    }
    return queued, remaining, old, current


def test_continuation_rejects_unknown_pending_running_and_forged_parent_references(
    replay_book, monkeypatch
):
    book = replay_book
    queued, request, old, current = partial_parent(book, monkeypatch)
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        parent_plan = connection.execute(
            "SELECT plan_id FROM replay_run WHERE id=?", (queued["run_id"],)
        ).fetchone()[0]
        original_step = dict(
            connection.execute(
                "SELECT * FROM replay_step WHERE run_id=? AND step_id='policy-0'",
                (queued["run_id"],),
            ).fetchone()
        )

    def rejected(body, code):
        with pytest.raises(KernelError) as failure:
            book.call("prepare_replay", request=body)
        assert failure.value.code == code

    rejected(
        {key: value for key, value in request.items() if key != "continuation_from"},
        "invalid_replay_plan",
    )
    rejected({**request, "continuation_from": "unknown-parent"}, "replay_continuation_missing")
    pending = {
        **request,
        "steps": [item for item in request["steps"] if item["step_id"] != "policy-1"],
    }
    rejected(pending, "invalid_replay_plan")
    monkeypatch.setattr(runtime_snapshot, "capture_runtime_snapshot", lambda: old)
    rejected(request, "replay_continuation_active")
    monkeypatch.setattr(runtime_snapshot, "capture_runtime_snapshot", lambda: current)
    for field, changed, code in (
        ("command", "receive_material", "replay_continuation_invalid"),
        ("request_id", "forged-request", "replay_continuation_invalid"),
        ("response", None, "replay_continuation_invalid"),
        ("status", "running", "replay_continuation_running"),
    ):
        with book.app.engine(book.company).store.connection() as connection:
            connection.execute(
                f"UPDATE replay_step SET {field}=? WHERE run_id=? AND step_id='policy-0'",
                (changed, queued["run_id"]),
            )
            connection.commit()
        rejected(request, code)
        with book.app.engine(book.company).store.connection() as connection:
            connection.execute(
                f"UPDATE replay_step SET {field}=? WHERE run_id=? AND step_id='policy-0'",
                (original_step[field], queued["run_id"]),
            )
            connection.commit()
    for field, changed in (
        ("company_id", "foreign-company"),
        ("database_id", "foreign-database"),
        ("authority", "{}"),
        ("scope_digest", b"x" * 32),
        ("schema_fingerprint", "foreign-schema"),
    ):
        with book.app.engine(book.company).store.connection() as connection:
            original = connection.execute(
                f"SELECT {field} FROM replay_plan WHERE id=?", (parent_plan,)
            ).fetchone()[0]
            connection.execute(
                f"UPDATE replay_plan SET {field}=? WHERE id=?", (changed, parent_plan)
            )
            connection.commit()
        rejected(request, "replay_continuation_changed")
        with book.app.engine(book.company).store.connection() as connection:
            connection.execute(
                f"UPDATE replay_plan SET {field}=? WHERE id=?", (original, parent_plan)
            )
            connection.commit()
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        assert (
            dict(
                connection.execute(
                    "SELECT * FROM replay_step WHERE run_id=? AND step_id='policy-0'",
                    (queued["run_id"],),
                ).fetchone()
            )
            == original_step
        )
        assert connection.execute("SELECT count(*) FROM replay_run").fetchone()[0] == 1


def test_confirmation_rechecks_new_real_readonly_parent_receipt(replay_book, monkeypatch):
    book = replay_book
    queued, request, old, current = partial_parent(book, monkeypatch, count=1)
    prepared = book.call("prepare_replay", request=request)
    monkeypatch.setattr(runtime_snapshot, "capture_runtime_snapshot", lambda: old)
    completed = run_ready(book.app, company_id=book.company)
    assert completed[0]["status"] == "completed" and completed[0]["step_id"] == "review-202601"
    monkeypatch.setattr(runtime_snapshot, "capture_runtime_snapshot", lambda: current)
    with pytest.raises(KernelError) as changed:
        book.call(
            "confirm_replay",
            request={
                "plan_id": prepared["plan_id"],
                "plan_digest": prepared["plan_digest"],
                "request_id": "must-not-confirm-stale-parent",
            },
        )
    assert changed.value.code == "replay_continuation_changed"
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM replay_run").fetchone()[0] == 1


def test_explicit_two_generation_chain_keeps_ancestor_dependencies_validation_only(
    replay_book, monkeypatch
):
    book = replay_book
    queued, request, old, current = partial_parent(book, monkeypatch)
    prepared = book.call("prepare_replay", request=request)
    child = book.call(
        "confirm_replay",
        request={
            "plan_id": prepared["plan_id"],
            "plan_digest": prepared["plan_digest"],
            "request_id": "first-child",
        },
    )
    assert run_ready(book.app, company_id=book.company)[0]["status"] == "blocked"
    assert run_ready(book.app, company_id=book.company)[0]["step_id"] == "policy-1"
    next_snapshot = replace(current, build_id=current.build_id + "-another")
    monkeypatch.setattr(runtime_snapshot, "capture_runtime_snapshot", lambda: next_snapshot)
    next_request = {
        **request,
        "continuation_from": child["run_id"],
        "request_id": "next-generation",
        "steps": [item for item in request["steps"] if item["step_id"] != "policy-1"],
    }
    next_plan = book.call("prepare_replay", request=next_request)
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        compiled = json.loads(
            connection.execute(
                "SELECT payload FROM replay_plan WHERE id=?", (next_plan["plan_id"],)
            ).fetchone()[0]
        )
        assert [item["run_id"] for item in compiled["context"]["continuation"]["chain"]] == [
            child["run_id"],
            queued["run_id"],
        ]
        assert {item["step_id"] for item in compiled["ordered_steps"]}.isdisjoint(
            {"policy-0", "policy-1"}
        )
        assert compiled["ordered_steps"][0]["dependencies"] == []
        assert next_request["steps"][0]["depends_on"] == ["policy-1"]
    next_run = book.call(
        "confirm_replay",
        request={
            "plan_id": next_plan["plan_id"],
            "plan_digest": next_plan["plan_digest"],
            "request_id": "next-child",
        },
    )
    assert run_ready(book.app, company_id=book.company)[0]["status"] == "blocked"
    outcome = run_ready(book.app, company_id=book.company)[0]
    assert outcome["run_id"] == next_run["run_id"] and outcome["step_id"] == "policy-2"
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
                "WHERE s.kind='payroll_contribution_policy'"
            ).fetchone()[0]
            == 3
        )


def test_new_continuation_session_preserves_parent_authority_and_exact_resume_guard(
    replay_book, monkeypatch
):
    book = replay_book
    queued, request, _, _ = partial_parent(book, monkeypatch)
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        old_authority = connection.execute(
            "SELECT p.authority FROM replay_plan p JOIN replay_run r ON r.plan_id=p.id "
            "WHERE r.id=?",
            (queued["run_id"],),
        ).fetchone()[0]
    book.app.security.logout(book.token)
    book.token = book.app.security.login("owner", PASSWORD).session_token
    status = book.call("replay_status", request={"run_id": queued["run_id"]})
    assert "authority" in status["context_issues"]
    with pytest.raises(KernelError) as invalid:
        book.call("resume_replay", run_id=queued["run_id"])
    assert invalid.value.code == "replay_context_changed"
    prepared = book.call("prepare_replay", request=request)
    child = book.call("confirm_replay", request={
        "plan_id": prepared["plan_id"], "plan_digest": prepared["plan_digest"],
        "request_id": "new-session-child",
    })
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        unchanged = connection.execute(
            "SELECT p.authority FROM replay_plan p JOIN replay_run r ON r.plan_id=p.id "
            "WHERE r.id=?",
            (queued["run_id"],),
        ).fetchone()[0]
        new_authority = connection.execute(
            "SELECT authority FROM replay_plan WHERE id=?", (prepared["plan_id"],)
        ).fetchone()[0]
    assert unchanged == old_authority
    assert json.loads(new_authority)["session_id"] != json.loads(old_authority)["session_id"]
    assert book.call("replay_status", request={"run_id": child["run_id"]})["context_valid"]


def test_batched_receipts_are_exact_ordered_and_bounded(replay_book):
    from pydantic import ValidationError

    from ai_accounting.kernel.replay_contracts import ReplayStatusRequest

    book = replay_book
    queued = prepare_policy_replay(book, count=2)
    assert run_ready(book.app, company_id=book.company)[0]["status"] == "completed"
    request = {"run_id": queued["run_id"], "step_ids": ["policy-1", "policy-0"]}
    batch = book.call("replay_status", request=request)
    validate_response("replay_status", batch)
    assert [item["step_id"] for item in batch["receipts"]] == request["step_ids"]
    assert [item["status"] for item in batch["receipts"]] == ["pending", "completed"]
    assert batch["receipts"][0]["response"] is None
    single = book.call("replay_status", request={"run_id": queued["run_id"], "step_id": "policy-0"})
    assert batch["receipts"][1] == single["receipt"]
    assert "receipts" not in single and "receipt" not in batch
    with pytest.raises(KernelError) as missing:
        book.call("replay_status", request={**request, "step_ids": ["policy-0", "missing"]})
    assert missing.value.code == "replay_step_missing"
    for changes in (
        {"step_ids": [f"step-{index}" for index in range(201)]},
        {"step_ids": ["policy-0", "policy-0"]},
        {"step_id": "policy-0", "step_ids": ["policy-1"]},
    ):
        with pytest.raises(ValidationError):
            ReplayStatusRequest.model_validate_json(json.dumps({**request, **changes}))
