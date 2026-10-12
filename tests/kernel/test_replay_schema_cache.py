"""A fixed command contract is reused while replay authorization stays live."""

import cProfile
import json
import pstats
import time

from test_payroll import contribution_policy

from ai_accounting.kernel.jobs import JobRunner
from ai_accounting.kernel.replay_runner import _replay_command_schema, run_ready
from ai_accounting.kernel.types import digest

pytest_plugins = ("test_replay_close_range",)


def prepare_policy_replay(book, count=6):
    source = [
        {
            "source_id": "confirmation",
            "kind": "confirmation",
            "period": None,
            "evidence_digest": book.proof,
            "disposition": "reference",
            "depends_on": [],
        }
    ]
    steps = []
    previous = None
    for index in range(count):
        data = contribution_policy().model_dump(mode="json")
        data["version"] = "synthetic-profile-policy-" + str(index)
        ident = "policy-" + str(index)
        steps.append(
            {
                "step_id": ident,
                "command": "save_facts",
                "payload": {
                    "facts": [
                        {
                            "kind": "payroll_contribution_policy",
                            "subject_id": ident,
                            "data": data,
                            "evidence": [book.proof],
                            "expected_revision": 0,
                        }
                    ]
                },
                "depends_on": [previous] if previous else [],
            }
        )
        previous = ident
    for period in ("2026-01", "2026-02", "2026-03"):
        suffix = period.replace("-", "")
        review = "review-" + suffix
        close = "close-" + suffix
        steps.extend(
            [
                {
                    "step_id": review,
                    "command": "preview_close",
                    "payload": {"period": period, "owner_confirmation": book.proof},
                    "depends_on": [previous],
                    "output_ref": review,
                    "output_fields": ["digest", "epochs"],
                },
                {
                    "step_id": close,
                    "command": "close",
                    "payload": {
                        "period": period,
                        "owner_confirmation": book.proof,
                        "preview_digest": {"$replay_ref": review, "field": "digest"},
                        "epochs": {"$replay_ref": review, "field": "epochs"},
                    },
                    "depends_on": [review],
                },
            ]
        )
        previous = close
    request = {
        "company_id": book.company,
        "database_id": book.database,
        "first_period": "2026-01",
        "last_period": "2026-03",
        "owner_confirmation": book.proof,
        "source_manifest_digest": digest(source).hex(),
        "source_manifest": source,
        "preserved_evidence": [book.proof],
        "steps": steps,
        "request_id": "schema-profile-plan",
    }
    plan = book.call("prepare_replay", request=request)
    return book.call(
        "confirm_replay",
        request={
            "plan_id": plan["plan_id"],
            "plan_digest": plan["plan_digest"],
            "request_id": "schema-profile-confirm",
        },
    )


def test_profile_repeated_schema_replay_rounds(replay_book, monkeypatch):
    book = replay_book
    queued = prepare_policy_replay(book)
    adapter = book.app.command_models["save_facts"]
    original = adapter.json_schema
    schema_elapsed = []

    def measured_schema(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            schema_elapsed.append(time.perf_counter() - started)

    monkeypatch.setattr(adapter, "json_schema", measured_schema)
    runner = JobRunner(
        book.app.catalog,
        callbacks=(lambda engine: run_ready(book.app, company_id=engine.store.company_id),),
    )
    profiler = cProfile.Profile()
    rounds = []
    for _ in range(6):
        started = time.perf_counter()
        outcomes = profiler.runcall(runner.run_once)
        rounds.append(time.perf_counter() - started)
        assert any(
            item.get("run_id") == queued["run_id"] and item.get("status") == "completed"
            for item in outcomes
        )
    stats = pstats.Stats(profiler)
    live_guards = {
        name: sum(value[1] for key, value in stats.stats.items() if key[2] == name)
        for name in ("validate_authority", "_schema_fingerprint", "_boundary")
    }
    assert all(count >= 6 for count in live_guards.values())
    top = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:12]
    print(
        json.dumps(
            {
                "rounds": len(rounds),
                "round_total_seconds": round(sum(rounds), 6),
                "schema_calls": len(schema_elapsed),
                "schema_total_seconds": round(sum(schema_elapsed), 6),
                "live_guard_calls": live_guards,
                "top_cumulative": [
                    {"function": key[2], "calls": value[1], "seconds": round(value[3], 6)}
                    for key, value in top
                ],
            }
        )
    )
    # Preparation has captured this service's fixed save_facts contract already.
    # Replay still validates actual commands and commits each node independently.
    assert schema_elapsed == []
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM replay_step WHERE run_id=? AND status='completed'",
                (queued["run_id"],),
            ).fetchone()[0]
            == 6
        )


def test_command_schema_cache_stays_with_registry_models_and_actual_adapter():
    from types import SimpleNamespace

    class Adapter:
        def __init__(self, field):
            self.field, self.calls = field, 0

        def json_schema(self):
            self.calls += 1
            return {"properties": {self.field: {"type": "string"}}}

    first = Adapter("original")
    other = Adapter("other")
    service = SimpleNamespace(registry=object(), command_models={"write": first, "read": other})
    assert _replay_command_schema(service, "write") == {
        "properties": {"original": {"type": "string"}}
    }
    assert _replay_command_schema(service, "write") == _replay_command_schema(service, "write")
    assert first.calls == 1 and other.calls == 0
    replacement = Adapter("replacement")
    service.command_models["write"] = replacement
    assert "replacement" in _replay_command_schema(service, "write")["properties"]
    assert replacement.calls == 1
    service.registry = object()
    _replay_command_schema(service, "write")
    assert replacement.calls == 2
    service.command_models = dict(service.command_models)
    _replay_command_schema(service, "write")
    assert replacement.calls == 3
    fresh = SimpleNamespace(registry=service.registry, command_models=service.command_models)
    _replay_command_schema(fresh, "write")
    assert replacement.calls == 4
