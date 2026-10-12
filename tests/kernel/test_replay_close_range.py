"""Explicit replay capability closes synthetic months through full production checks."""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import dataclass

import pytest
from close_storage_fixture import replace_stored_manifest
from draft_bundle_fixture import synthetic_draft_bundle
from test_integrity_content import damage
from test_new_company_reports import profile, zero_tax

from ai_accounting.kernel import schema_bundle
from ai_accounting.kernel.backup import run_backup_jobs, verify_portable
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.opening import CATEGORIES
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.read_state import advance_repair_revision
from ai_accounting.kernel.security import IdentityError
from ai_accounting.kernel.service import LocalService

PASSWORD = "Synthetic-replay-range-owner-123"
NEW_PASSWORD = "Synthetic-replay-range-owner-456"


@dataclass
class ReplayBook:
    app: LocalService
    token: object
    company: str
    database: str
    proof: str
    config: dict

    def call(self, command, **payload):
        return self.app.dispatch(
            command, {"company_id": self.company, **payload}, session_token=self.token
        )

    def preview(self, **changes):
        return self.call(
            "preview_replay_close_range",
            **(
                {
                    "first_period": "2026-01",
                    "last_period": "2026-03",
                    "owner_confirmation": self.proof,
                }
                | changes
            ),
        )

    def payload(self, preview, request_id="synthetic-range-close"):
        return {
            "first_period": "2026-01",
            "last_period": "2026-03",
            "owner_confirmation": self.proof,
            "preview_digest": preview["digest"],
            "epochs": preview["epochs"],
            "request_id": request_id,
        }

    def closes(self):
        with self.app.engine(self.company).store.connection(read_only=True) as connection:
            return connection.execute("SELECT count(*) FROM period_close").fetchone()[0]


@pytest.fixture
def replay_book(tmp_path, monkeypatch):
    bundle = synthetic_draft_bundle(tmp_path / "replay-draft-contracts")
    # Keep the explicit test-only replay capability on a draft company through
    # every service restart, regardless of the installed production release.
    monkeypatch.setattr(schema_bundle, "production_bundle", lambda: bundle)
    root = tmp_path / "synthetic-replay-only"
    bootstrap = LocalService(root)
    bootstrap.security.provision("owner", PASSWORD)
    token = bootstrap.security.login("owner", PASSWORD).session_token
    company = bootstrap.dispatch(
        "create_company",
        {"taxpayer_id": "91310000123456789A", "name": "Synthetic replay range company"},
        session_token=token,
    )["id"]
    engine = bootstrap.engine(company)
    proof = engine.register_evidence(
        b"Synthetic owner confirms empty formation and no business in January-March 2026",
        "text/plain",
        "synthetic original owner confirmation",
        request_id="original-proof",
    )["digest"]
    counter = itertools.count()

    def save(kind, subject, data):
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=0,
            request_id=f"source-{next(counter)}",
        )

    save(
        "opening_package",
        "opening",
        {
            "period": "2026-01",
            "package_id": "opening",
            "counts": dict.fromkeys(CATEGORIES.values(), 0),
            "members": [],
            "completeness_confirmed": True,
        },
    )
    opening = engine.preview(["opening"])
    engine.confirm(
        ["opening"],
        preview_digest=opening["digest"],
        epochs=opening["epochs"],
        request_id="publish-empty-formation",
    )
    profile(save, "2026-01")
    zero_tax(save, 2026, 1)
    for month in ("2026-01", "2026-02", "2026-03"):
        for category in MATERIAL_CATEGORIES:
            Periods(engine).inventory(
                month,
                category,
                evidence=[],
                expected=0,
                no_business=True,
                confirmation_evidence=proof,
                request_id=f"inventory-{month}-{category}",
            )
    config = {
        "format": "ai-accounting-kernel/2/replay-close-scope/1",
        "catalog_instance_id": bootstrap.security.catalog_instance_id,
        "targets": [
            {
                "company_id": company,
                "database_id": engine.store.database_id,
                "first_period": "2026-01",
                "last_period": "2026-03",
                "owner_confirmation": proof,
            }
        ],
    }
    database = engine.store.database_id
    bootstrap.close()
    app = LocalService(root, replay_scope=config)
    book = ReplayBook(app, token, company, database, proof, config)
    try:
        yield book
    finally:
        app.close()



def test_normal_service_explicitly_rejects_both_range_commands(replay_book):
    book = replay_book
    normal = LocalService(book.app.catalog.root)
    try:
        for command, extra in (
            ("preview_replay_close_range", {}),
            ("confirm_replay_close_range", {
                "preview_digest": "a" * 64,
                "epochs": {"accounting": 0, "material": 0, "management": 0},
                "request_id": "normal-must-not-close",
            }),
        ):
            with pytest.raises(KernelError) as error:
                normal.dispatch(
                    command,
                    {"company_id": book.company, "first_period": "2026-01",
                     "last_period": "2026-03", "owner_confirmation": book.proof, **extra},
                    session_token=book.token,
                )
            assert error.value.code == "replay_mode_required"
        assert book.closes() == 0
    finally:
        normal.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"first_period": "2025-12"},
        {"last_period": "2026-04"},
        {"first_period": "2026-03", "last_period": "2026-02"},
        {"owner_confirmation": "f" * 64},
    ],
)
def test_preview_cannot_expand_runtime_range_or_confirmation(replay_book, changes):
    with pytest.raises(KernelError):
        replay_book.preview(**changes)
    assert replay_book.closes() == 0


def test_legacy_close_range_facade_queues_durable_typed_steps(replay_book):
    from ai_accounting.kernel.replay_runner import run_ready
    from ai_accounting.kernel.replay_contracts import REPLAY_COMMANDS

    book = replay_book
    assert REPLAY_COMMANDS <= set(book.app.command_models)
    preview = book.preview()
    request = book.payload(preview, request_id="legacy-range-facade")
    queued = book.call("confirm_replay_close_range", **request)
    assert queued["status"] == "pending"
    assert book.closes() == 0
    assert book.call("confirm_replay_close_range", **request) == queued

    for _ in range(8):
        run_ready(book.app, limit=1, company_id=book.company)
    status = book.call("replay_status", request={"run_id": queued["run_id"]})
    assert status["status"] == "completed"
    assert book.closes() == 3
    assert [
        book.call("replay_status", request={
            "run_id": queued["run_id"], "step_id": f"close-2026{month:02}"
        })["receipt"]["response"]["backup_job"]
        for month in (1, 2, 3)
    ][:2] == [None, None]
    assert status["progress"]["backup_task"]["status"] == "pending"


def test_live_session_revocation_stops_the_remaining_typed_steps(replay_book):
    from ai_accounting.kernel.replay_runner import run_ready

    book = replay_book
    preview = book.preview()
    queued = book.call(
        "confirm_replay_close_range",
        **book.payload(preview, request_id="revocation-stops-run"),
    )
    assert run_ready(book.app, limit=1, company_id=book.company)[0]["status"] == "completed"
    book.app.security.logout(book.token, request_id="owner-revokes-replay-session")
    blocked = run_ready(book.app, limit=1, company_id=book.company)
    assert blocked[0]["status"] == "blocked"
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        status = connection.execute(
            "SELECT status,error_code FROM replay_run WHERE id=?", (queued["run_id"],)
        ).fetchone()
    assert status["status"] == "blocked"
    assert status["error_code"] == "replay_context_changed"
    assert book.closes() == 0


def test_source_change_preserves_closed_prefix_and_blocks_next_month(replay_book):
    from ai_accounting.kernel.replay_runner import run_ready

    book = replay_book
    preview = book.preview()
    queued = book.call(
        "confirm_replay_close_range",
        **book.payload(preview, request_id="source-change-blocks-next-month"),
    )
    assert run_ready(book.app, limit=1, company_id=book.company)[0]["status"] == "completed"
    assert run_ready(book.app, limit=1, company_id=book.company)[0]["status"] == "completed"
    assert book.closes() == 1
    book.app.engine(book.company).register_evidence(
        b"source evidence appended after first closed month",
        "text/plain", "post-close source mutation", request_id="post-close-source-change",
    )
    blocked = run_ready(book.app, limit=1, company_id=book.company)
    assert blocked[0]["status"] == "blocked"
    status = book.call("replay_status", request={"run_id": queued["run_id"]})
    assert status["status"] == "blocked"
    assert status["progress"]["closed_through"] == "2026-01"
    assert book.closes() == 1
