"""Read-only separation proposals require explicit, current material proof."""

import json

import pytest
from pydantic import ValidationError
from test_duplicates import expense, owner_review

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.duplicates import TypedDuplicateSeparationIntent, verify_duplicate_checks
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.replay_contracts import ReplayStep
from ai_accounting.kernel.replay_runner import _safe_step_payload, run_ready
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.types import digest

pytest_plugins = ("test_replay_close_range",)


def sourced_proposals(book):
    engine = book.app.engine(book.company)
    party = Entities(engine).register_entity(
        "organization", {"display_name": "Synthetic supplier"},
        source="synthetic explicit party", request_id="separation-party",
    )["entity_id"]
    locations, proofs = [], []
    for label in ("original-a", "original-b"):
        proof = engine.register_evidence(
            f"{label}: independent business row".encode(), "text/plain", label,
            request_id="proof-" + label,
        )["digest"]
        proofs.append(proof)
        source = Materials(engine).receive(
            label,
            {"period": "2026-01", "evidence_digest": proof, "category": "transactions",
             "purpose": "business", "specification": {
                 "format": "text", "all_pages_reviewed": True,
                 "passages": [{"location": "row-1", "page": 1,
                               "excerpt": f"{label}: independent business row"}],
             }},
            evidence=(proof,), expected_revision=0, request_id="receive-" + label,
        )
        locations.append({"source_id": label, "source_fact_id": source["fact_id"],
                          "evidence_digest": proof, "location": "row-1"})
    data = expense(engine, party).model_dump(mode="json")
    engine.save_fact(
        "expense", "expense-first", data, evidence=(book.proof, proofs[0]),
        expected_revision=0, request_id="separation-first", source_locations=(locations[0],),
    )
    proposed = {"kind": "expense", "subject_id": "expense-second", "data": data,
                "evidence": [book.proof, proofs[1]], "expected_revision": 0,
                "source_locations": [locations[1]]}
    intent = {"action": "create_separate", "explanation": "Two explicit independent original rows",
              "review_basis": [{"kind": "distinct_material_location", **locations[1]}]}
    return engine, proposed, intent


def test_preparation_has_nullable_review_and_verified_intent_writes_nothing(replay_book):
    book = replay_book
    engine, proposed, intent = sourced_proposals(book)
    with engine.store.connection(read_only=True) as connection:
        counts = tuple(connection.execute(
            "SELECT (SELECT count(*) FROM fact_revision),(SELECT count(*) FROM audit),"
            "(SELECT count(*) FROM business_duplicate_check)"
        ).fetchone())
    unresolved = book.call("prepare_fact_registration", **proposed)
    assert unresolved["strong_candidates"] and unresolved["review"] is None
    validate_response("prepare_fact_registration", unresolved)
    prepared = book.call("prepare_fact_registration", **proposed, separation_intent=intent)
    validate_response("prepare_fact_registration", prepared)
    assert prepared["review"]["candidate_digest"] == prepared["candidate_digest"]
    assert prepared["review"]["action"] == "create_separate"
    review_ref = {"$replay_ref": "prepared-separation", "field": "review"}
    step = ReplayStep.model_validate_json(json.dumps({
        "step_id": "save-prepared-separation", "command": "save_facts",
        "payload": {"facts": [{**proposed, "review": review_ref}]},
    }))
    frozen = _safe_step_payload(book.app, book.company, "stable-plan", step)
    assert frozen["facts"][0]["review"] == review_ref
    with engine.store.connection(read_only=True) as connection:
        assert tuple(connection.execute(
            "SELECT (SELECT count(*) FROM fact_revision),(SELECT count(*) FROM audit),"
            "(SELECT count(*) FROM business_duplicate_check)"
        ).fetchone()) == counts
    saved = engine.save_fact(
        **{**proposed, "evidence": tuple(proposed["evidence"])},
        request_id="separation-second", review=prepared["review"],
    )
    assert saved["subject_id"] == "expense-second"
    with engine.store.connection(read_only=True) as connection:
        verify_duplicate_checks(connection)
        assert connection.execute(
            "SELECT count(*) FROM subject WHERE id LIKE 'expense-%'"
        ).fetchone()[0] == 2
    clear = book.call("prepare_fact_registration", **{
        **proposed, "subject_id": "expense-clear",
        "data": {**proposed["data"], "amount_fen": 13000},
    }, separation_intent=intent)
    assert not clear["strong_candidates"] and clear["review"] is None
    validate_response("prepare_fact_registration", clear)


@pytest.mark.parametrize("change", ["same", "invented", "stale"])
def test_separation_cannot_claim_same_invented_or_stale_original(replay_book, change):
    book = replay_book
    engine, proposed, intent = sourced_proposals(book)
    basis = intent["review_basis"][0]
    if change == "same":
        with engine.store.connection(read_only=True) as connection:
            manifest = json.loads(connection.execute(
                "SELECT manifest FROM business_duplicate_check "
                "WHERE proposed_subject_id='expense-first'"
            ).fetchone()[0])
        basis = {"kind": "distinct_material_location", **manifest["source_locations"][0]}
    elif change == "invented":
        basis = {**basis, "location": "row-999"}
    else:
        basis = {**basis, "source_fact_id": "stale-source-version"}
    with pytest.raises(KernelError) as blocked:
        book.call("prepare_fact_registration", **proposed,
                  separation_intent={**intent, "review_basis": [basis]})
    assert blocked.value.code in {
        "duplicate_separation_basis_invalid", "duplicate_source_location_invalid",
    }
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM subject WHERE id='expense-second'"
        ).fetchone() is None


def test_separation_intent_does_not_default_to_owner_confirmation():
    with pytest.raises(ValidationError):
        TypedDuplicateSeparationIntent.model_validate_json(json.dumps({
            "action": "create_separate", "explanation": "unsupported inference",
            "review_basis": [{"kind": "owner_confirmation", "evidence_digest": "a" * 64}],
        }))


def test_partial_candidate_locations_cannot_prove_all_candidates_and_save_rechecks(replay_book):
    book = replay_book
    engine, proposed, intent = sourced_proposals(book)
    prepared = book.call("prepare_fact_registration", **proposed, separation_intent=intent)
    confirmation = engine.register_evidence(
        b"Synthetic owner confirms a third independent business "
        b"whose material row is not yet located",
        "text/plain", "Synthetic explicit third-business confirmation", request_id="third-proof",
    )["digest"]
    third = {**proposed, "subject_id": "expense-third", "source_locations": [],
             "evidence": [book.proof, confirmation]}
    candidates = book.call("prepare_fact_registration", **third)
    engine.save_fact(
        **{**third, "evidence": tuple(third["evidence"])}, request_id="separation-third",
        review=owner_review(candidates, confirmation).model_dump(mode="json"),
    )
    mixed = book.call("prepare_fact_registration", **proposed)
    assert len(mixed["strong_candidates"]) == 2
    assert any(item["material_sources"] for item in mixed["strong_candidates"])
    assert any(not item["material_sources"] for item in mixed["strong_candidates"])
    with pytest.raises(KernelError) as blocked:
        book.call("prepare_fact_registration", **proposed, separation_intent=intent)
    assert blocked.value.code == "needs_information"
    with pytest.raises(KernelError) as expired:
        engine.save_fact(
            **{**proposed, "evidence": tuple(proposed["evidence"])},
            request_id="stale-prepared-separation", review=prepared["review"],
        )
    assert expired.value.code == "duplicate_candidate_expired"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM subject WHERE id='expense-second'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM request WHERE id='stale-prepared-separation'"
        ).fetchone() is None


def test_replay_unresolved_strong_preparation_never_binds_null_review(replay_book):
    book = replay_book
    engine, proposed, _ = sourced_proposals(book)
    sources = [{"source_id": "confirmation", "kind": "confirmation",
                "period": None, "evidence_digest": book.proof,
                "disposition": "reference", "depends_on": []}]
    steps = [{"step_id": "unresolved-preparation", "command": "prepare_fact_registration",
              "payload": proposed, "output_ref": "unresolved-preparation",
              "output_fields": ["review"]}]
    previous = "unresolved-preparation"
    for period in ("2026-01", "2026-02", "2026-03"):
        review, close = "review-" + period, "close-" + period
        steps.extend([
            {"step_id": review, "command": "preview_close", "depends_on": [previous],
             "payload": {"period": period, "owner_confirmation": book.proof},
             "output_ref": review, "output_fields": ["digest", "epochs"]},
            {"step_id": close, "command": "close", "depends_on": [review],
             "payload": {"period": period, "owner_confirmation": book.proof,
                         "preview_digest": {"$replay_ref": review, "field": "digest"},
                         "epochs": {"$replay_ref": review, "field": "epochs"}}},
        ])
        previous = close
    prepared = book.call("prepare_replay", request={
        "company_id": book.company, "database_id": book.database,
        "first_period": "2026-01", "last_period": "2026-03", "owner_confirmation": book.proof,
        "source_manifest": sources, "source_manifest_digest": digest(sources).hex(),
        "preserved_evidence": [book.proof], "steps": steps, "request_id": "unresolved-plan",
    })
    queued = book.call("confirm_replay", request={
        "plan_id": prepared["plan_id"], "plan_digest": prepared["plan_digest"],
        "request_id": "unresolved-confirm",
    })
    outcome = run_ready(book.app, company_id=book.company)[0]
    assert outcome["status"] == "blocked" and outcome["code"] == "needs_information", outcome
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM replay_binding WHERE run_id=?", (queued["run_id"],)
        ).fetchone() is None
        receipt = connection.execute(
            "SELECT status,response FROM replay_step WHERE run_id=? AND step_id=?",
            (queued["run_id"], "unresolved-preparation"),
        ).fetchone()
        assert tuple(receipt) == ("blocked", None)
