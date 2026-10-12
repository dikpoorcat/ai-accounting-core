"""Output selectors bind actual typed receipts in a synthetic replay company."""

import json

import pytest
from test_payroll import contribution_policy

from ai_accounting.kernel.replay_runner import run_ready
from ai_accounting.kernel.types import digest

pytest_plugins = ("test_replay_close_range",)


@pytest.mark.parametrize("failure", [None, "absent", "missing_field"])
def test_durable_selector_resolves_dependency_and_rolls_back_missing_output(replay_book, failure):
    book = replay_book
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
    entity_ref = {"$replay_ref": "identity", "field": "entity_id"}
    selector = {
        "array_path": "results",
        "match_field": "subject_id",
        "match_value": "absent" if failure == "absent" else entity_ref,
        "field": "missing" if failure == "missing_field" else "fact_id",
    }
    steps = [
        {
            "step_id": "identity",
            "command": "register_entity",
            "payload": {"kind": "organization", "data": {}, "source": "Synthetic policy identity"},
            "output_ref": "identity",
            "output_fields": ["entity_id"],
        },
        {
            "step_id": "policies",
            "command": "save_facts",
            "payload": {
                "facts": [
                    {
                        "kind": "payroll_contribution_policy",
                        "subject_id": "other-policy",
                        "data": contribution_policy().model_dump(mode="json"),
                        "evidence": [book.proof],
                        "expected_revision": 0,
                    },
                    {
                        "kind": "payroll_contribution_policy",
                        "subject_id": entity_ref,
                        "data": contribution_policy().model_dump(mode="json"),
                        "evidence": [book.proof],
                        "expected_revision": 0,
                    },
                ]
            },
            "output_ref": "policies",
            "output_fields": ["selected"],
            "output_selectors": {"selected": selector},
            "depends_on": ["identity"],
        },
    ]
    previous = "policies"
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
        "request_id": "selector-plan",
    }
    plan = book.call("prepare_replay", request=request)
    queued = book.call(
        "confirm_replay",
        request={
            "plan_id": plan["plan_id"],
            "plan_digest": plan["plan_digest"],
            "request_id": "selector-confirm",
        },
    )
    assert run_ready(book.app, company_id=book.company)[0]["status"] == "completed"
    outcome = run_ready(book.app, company_id=book.company)[0]
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        saved = connection.execute(
            "SELECT count(*) FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind='payroll_contribution_policy'"
        ).fetchone()[0]
        step = connection.execute(
            "SELECT * FROM replay_step WHERE run_id=? AND step_id='policies'", (queued["run_id"],)
        ).fetchone()
        bindings = list(
            connection.execute(
                "SELECT * FROM replay_binding WHERE run_id=? AND reference='policies'",
                (queued["run_id"],),
            )
        )
        if failure:
            assert outcome["status"] == "blocked"
            assert outcome["code"] == "replay_output_missing"
            assert saved == 0 and bindings == [] and step["response"] is None
            assert (
                connection.execute(
                    "SELECT count(*) FROM request WHERE id=?", (step["request_id"],)
                ).fetchone()[0]
                == 0
            )
        else:
            assert outcome["status"] == "completed" and saved == 2
            response = json.loads(step["response"])
            resolved = json.loads(step["output_fields"])["selectors"]["selected"]
            assert isinstance(resolved["match_value"], str)
            actual = next(
                item
                for item in response["results"]
                if item["subject_id"] == resolved["match_value"]
            )
            assert json.loads(bindings[0]["value"]) == actual["fact_id"]
    if not failure:
        # A resumed run retains this resolved selector and the committed node.
        book.call("resume_replay", run_id=queued["run_id"])
        receipt = book.call(
            "replay_status", request={"run_id": queued["run_id"], "step_id": "policies"}
        )
        assert receipt["receipt"]["response"] == response
