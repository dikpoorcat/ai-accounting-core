"""Typed metadata revisions remain verifiable through replay and close."""

import json

import pytest
from test_integrity_content import damage

from ai_accounting.kernel.change_journal import changes_since, head, verify_journal
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.discovery import Discovery
from ai_accounting.kernel.display import BusinessProfile, Display
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.replay_runner import run_ready
from ai_accounting.kernel.types import digest

pytest_plugins = ("test_replay_close_range",)


def metadata_revisions(book):
    engine = book.app.engine(book.company)
    entities, display, discovery = Entities(engine), Display(engine), Discovery(engine)
    entity = entities.register_entity(
        "organization", {"display_name": "Synthetic party"}, source="synthetic proof",
        evidence_digest=book.proof, request_id="metadata-party",
    )["entity_id"]
    entities.update_entity_profile(
        entity, {"note": "Second explicit profile"}, source="synthetic proof",
        expected_revision=1, evidence_digest=book.proof, request_id="metadata-party-second",
    )
    for revision in range(2):
        discovery.update_company_note(
            f"Synthetic company note {revision + 1}", expected_revision=revision,
            request_id=f"metadata-note-{revision}", evidence_digest=book.proof,
        )
        display.save_display_profile(
            BusinessProfile(kind="business", entity_id="opening", note=f"Note {revision}",
                            source="synthetic proof", evidence_digest=book.proof),
            expected_revision=revision, request_id=f"metadata-display-{revision}",
        )
        context = display.preview_period_commentary("2026-01")
        display.update_period_commentary(
            "2026-01", f"Synthetic conclusion {revision}", context_digest=context["context_digest"],
            source="synthetic proof", expected_revision=revision,
            request_id=f"metadata-commentary-{revision}", evidence_digest=book.proof,
        )
    return engine, entity


def test_metadata_revisions_then_real_replay_actor_and_preview_close(replay_book):
    book = replay_book
    engine, entity = metadata_revisions(book)
    with engine.store.connection(read_only=True) as connection:
        assert verify_journal(connection) == head(connection)
        events = changes_since(connection, 0)
        assert {event.source for event in events if event.before_ref is not None} >= {
            "entity_profile", "company_note", "display_profile", "commentary",
        }
        # A suffix can begin with revision two; its predecessor remains a real source.
        event = next(item for item in events if item.source == "entity_profile" and item.before_ref)
        assert changes_since(connection, event.sequence - 1)[0] == event
    sources = [{"source_id": "confirmation", "kind": "confirmation", "period": None,
                "evidence_digest": book.proof, "disposition": "reference", "depends_on": []}]
    steps = [
        {"step_id": "note-third", "command": "update_company_note",
         "payload": {"text": "Third sourced note", "expected_revision": 2,
                     "evidence_digest": book.proof}},
        {"step_id": "profile-third", "command": "update_entity_profile",
         "payload": {"entity_id": entity, "data": {"note": "Third sourced profile"},
                     "source": "synthetic proof", "expected_revision": 2,
                     "evidence_digest": book.proof}, "depends_on": ["note-third"]},
    ]
    previous = "profile-third"
    for period in ("2026-01", "2026-02", "2026-03"):
        review, close = "review-" + period, "close-" + period
        steps.extend([
            {"step_id": review, "command": "preview_close",
             "payload": {"period": period, "owner_confirmation": book.proof},
             "depends_on": [previous], "output_ref": review, "output_fields": ["digest", "epochs"]},
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
        "preserved_evidence": [book.proof], "steps": steps, "request_id": "metadata-replay",
    })
    queued = book.call("confirm_replay", request={
        "plan_id": prepared["plan_id"], "plan_digest": prepared["plan_digest"],
        "request_id": "metadata-replay-confirm",
    })
    for ident in ("note-third", "profile-third", "review-2026-01", "close-2026-01"):
        outcome = run_ready(book.app, company_id=book.company)[0]
        assert outcome["step_id"] == ident and outcome["status"] == "completed", outcome
    with engine.store.connection(read_only=True) as connection:
        assert verify_journal(connection) == head(connection)
        rows = connection.execute(
            "SELECT a.payload FROM replay_step s JOIN audit a ON a.request_id=s.request_id "
            "WHERE s.run_id=? AND s.command IN ('update_company_note','update_entity_profile')",
            (queued["run_id"],),
        ).fetchall()
        assert len(rows) == 2
        for row in rows:
            actor = json.loads(row[0])["actor"]
            assert actor["authorization_method"] == "replay_scope"
            assert actor["replay_run_id"] == queued["run_id"]
            assert actor["replay_step_id"] in {"note-third", "profile-third"}
        assert book.closes() == 1


@pytest.mark.parametrize(
    "source", ["company_note", "entity_profile", "display_profile", "commentary"]
)
def test_revision_journal_does_not_accept_a_forged_predecessor(replay_book, source):
    engine, _ = metadata_revisions(replay_book)
    damage(engine, "source_change", "UPDATE source_change SET before_ref=NULL "
           "WHERE source=? AND before_ref IS NOT NULL", (source,))
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as invalid:
            verify_journal(connection)
        assert invalid.value.details["reason"] == "journal_revision_chain_mismatch"


def test_revision_number_gap_is_rejected_even_with_matching_journal_refs(replay_book):
    engine, _ = metadata_revisions(replay_book)
    damage(engine, "company_note_revision",
           "UPDATE company_note_revision SET revision=3 WHERE revision=2")
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as invalid:
            verify_journal(connection)
        assert invalid.value.details["reason"] == "journal_revision_source_mismatch"


def test_append_only_sources_still_reject_predecessors(replay_book):
    engine = replay_book.app.engine(replay_book.company)
    damage(engine, "source_change", "UPDATE source_change SET before_ref='invented' "
           "WHERE source='evidence'")
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as invalid:
            changes_since(connection, 0)
        assert invalid.value.details["reason"] == "journal_entry_invalid"
