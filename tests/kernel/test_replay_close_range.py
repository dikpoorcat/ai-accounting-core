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
            (
                "confirm_replay_close_range",
                {
                    "preview_digest": "a" * 64,
                    "epochs": {"accounting": 0, "material": 0, "management": 0},
                    "request_id": "normal-must-not-close",
                },
            ),
        ):
            with pytest.raises(KernelError) as error:
                normal.dispatch(
                    command,
                    {
                        "company_id": book.company,
                        "first_period": "2026-01",
                        "last_period": "2026-03",
                        "owner_confirmation": book.proof,
                        **extra,
                    },
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


def test_runtime_target_must_match_actual_company_database(replay_book):
    book = replay_book
    config = copy.deepcopy(book.config)
    config["targets"][0]["database_id"] = "wrong-database"
    wrong = LocalService(book.app.catalog.root, replay_scope=config)
    try:
        with pytest.raises(KernelError):
            wrong.dispatch(
                "preview_replay_close_range",
                {
                    "company_id": book.company,
                    "first_period": "2026-01",
                    "last_period": "2026-03",
                    "owner_confirmation": book.proof,
                },
                session_token=book.token,
            )
        assert book.closes() == 0
    finally:
        wrong.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("catalog_instance_id", "other-catalog"),
        ("last_period", "2036-01"),
    ],
)
def test_runtime_scope_rejects_foreign_catalog_or_over_120_months(replay_book, field, value):
    book = replay_book
    config = copy.deepcopy(book.config)
    if field == "catalog_instance_id":
        config[field] = value
    else:
        config["targets"][0][field] = value
    with pytest.raises(KernelError):
        LocalService(book.app.catalog.root, replay_scope=config)
    assert book.closes() == 0


def test_scope_does_not_authorize_other_company(replay_book):
    book = replay_book
    other = book.app.dispatch(
        "create_company",
        {
            "taxpayer_id": "91310000123456789B",
            "name": "Other synthetic company",
        },
        session_token=book.token,
    )["id"]
    with pytest.raises(KernelError):
        book.app.dispatch(
            "preview_replay_close_range",
            {
                "company_id": other,
                "first_period": "2026-01",
                "last_period": "2026-03",
                "owner_confirmation": book.proof,
            },
            session_token=book.token,
        )
    assert book.closes() == 0


def test_registered_unrelated_confirmation_cannot_replace_scoped_original(replay_book):
    book = replay_book
    other = book.app.engine(book.company).register_evidence(
        b"Another registered synthetic owner statement, outside this replay scope",
        "text/plain",
        "unrelated original",
        request_id="other-owner-confirmation",
    )["digest"]
    with pytest.raises(KernelError) as error:
        book.preview(owner_confirmation=other)
    assert error.value.code == "replay_scope_mismatch"
    assert book.closes() == 0


def test_runtime_scope_is_not_mutated_by_callers_original_config(replay_book):
    book = replay_book
    book.config["targets"][0]["last_period"] = "2026-04"
    with pytest.raises(KernelError) as error:
        book.preview(last_period="2026-04")
    assert error.value.code == "replay_scope_mismatch"
    assert book.closes() == 0


def test_three_months_freeze_linked_sources_and_backups_without_password_grants(replay_book):
    book = replay_book
    preview = book.preview()
    assert preview["status"] == "replay_preview"
    assert preview["company_id"] == book.company and preview["database_id"] == book.database
    assert preview["periods"] == ["2026-01", "2026-02", "2026-03"]
    assert preview["first_month_review"]["manifest"]["period"] == "2026-01"
    result = book.call("confirm_replay_close_range", **book.payload(preview))
    assert result["status"] == "completed"
    assert result["closed_through"] == "2026-03"
    assert result["blocked_period"] is result["issue"] is None
    assert result["scope_digest"] == preview["scope_digest"]
    assert result["authorization_method"] == "replay_scope"
    assert len(result["results"]) == 3
    previous = None
    engine = book.app.engine(book.company)
    for month, receipt in zip(preview["periods"], result["results"], strict=True):
        assert receipt["period"] == month and receipt["status"] == "closed"
        assert receipt["request_id"] and receipt["backup_job"]
        frozen = Periods(engine).closed_report(month)
        assert frozen["previous_close_digest"] == previous
        assert frozen["approval"] is None
        assert set(frozen["inventories"]) == set(MATERIAL_CATEGORIES)
        assert frozen["owner_confirmation"] == book.proof
        assert frozen["owner_review"] and frozen["readiness"]
        if month == "2026-01":
            for field in (
                "adopted_results",
                "inventories",
                "material_coverage",
                "readiness",
                "management_snapshot",
                "owner_review",
                "read_version",
            ):
                assert frozen[field] == preview["first_month_review"]["manifest"][field]
        with engine.store.connection(read_only=True) as connection:
            job = connection.execute(
                "SELECT payload FROM jobs WHERE id=?", (receipt["backup_job"],)
            ).fetchone()
            assert json.loads(job[0])["close_digest"] == receipt["digest"]
            audit = connection.execute(
                "SELECT payload FROM audit WHERE request_id=?", (receipt["request_id"],)
            ).fetchone()
            assert audit is not None
            actor = json.loads(audit[0])["actor"]
            assert actor["owner_id"]
            assert actor["authorization_method"] == "replay_scope"
            assert actor["replay_scope_digest"] == preview["scope_digest"]
            assert actor["replay_batch_request_id"] == "synthetic-range-close"
            assert book.token.get_secret_value() not in audit[0]
        previous = receipt["digest"]
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM security_close_approval").fetchone()[0] == 0
    jobs = run_backup_jobs(engine.store.path, limit=3, _bundle=engine.store.bundle)
    assert len(jobs) == 3 and all(job["status"] == "succeeded" for job in jobs)
    latest = verify_portable(
        jobs[-1]["result"]["path"],
        expected_company_id=book.company,
        expected_database_id=book.database,
        expected_taxpayer_id="91310000123456789A",
        _bundle=engine.store.bundle,
    )
    assert latest["identity"]["database_id"] == book.database
    assert latest["manifest"]["request_id"] == jobs[-1]["id"]
    assert latest["latest_closed_period"] == "2026-03"
    assert latest["verification"]["status"] == "verified"
    assert latest["verification"]["counts"]["closes"] == 3


def test_missing_second_month_stops_after_first_without_skipping(replay_book):
    book = replay_book
    engine = book.app.engine(book.company)
    Periods(engine).inventory(
        "2026-02",
        "tax",
        evidence=[book.proof],
        expected=2,
        no_business=False,
        confirmation_evidence=book.proof,
        request_id="second-month-more-originals-expected",
    )
    preview = book.preview()
    result = book.call("confirm_replay_close_range", **book.payload(preview))
    assert result["status"] == "partial"
    assert result["closed_through"] == "2026-01"
    assert result["blocked_period"] == "2026-02"
    assert result["issue"]["code"] == "period_not_ready"
    assert [x["period"] for x in result["results"]] == ["2026-01"]
    assert book.closes() == 1
    with pytest.raises(KernelError):
        Periods(engine).closed_report("2026-03")


def test_lost_final_response_reuses_exact_child_requests_and_closes(replay_book):
    book = replay_book
    payload = book.payload(book.preview())
    first = book.call("confirm_replay_close_range", **payload)
    again = book.call("confirm_replay_close_range", **payload)
    assert again == first
    assert len({x["request_id"] for x in again["results"]}) == book.closes() == 3
    with book.app.engine(book.company).store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM jobs WHERE kind='portable_backup'").fetchone()[
                0
            ]
            == 3
        )


def test_service_restart_resumes_persisted_batch_after_commit(replay_book, monkeypatch):
    book = replay_book
    payload = book.payload(book.preview(), request_id="synthetic-restart-resume")
    original = Periods.close

    def disconnect_after_commit(periods, month, **kwargs):
        receipt = original(periods, month, **kwargs)
        if month == "2026-01":
            raise OSError("synthetic resident stopped after close commit")
        return receipt

    monkeypatch.setattr(Periods, "close", disconnect_after_commit)
    with pytest.raises(OSError):
        book.call("confirm_replay_close_range", **payload)
    monkeypatch.setattr(Periods, "close", original)
    root = book.app.catalog.root
    book.app.close()
    book.app = LocalService(root, replay_scope=book.config)
    result = book.call("confirm_replay_close_range", **payload)
    assert result["status"] == "completed" and book.closes() == 3


def test_resume_after_first_close_commit_response_is_lost(replay_book, monkeypatch):
    book = replay_book
    payload = book.payload(book.preview())
    original = Periods.close

    def lose_response(periods, month, **kwargs):
        receipt = original(periods, month, **kwargs)
        if month == "2026-01":
            raise OSError("synthetic transport lost after actual close commit")
        return receipt

    monkeypatch.setattr(Periods, "close", lose_response)
    with pytest.raises(OSError, match="after actual close commit"):
        book.call("confirm_replay_close_range", **payload)
    assert book.closes() == 1
    first_digest = Periods(book.app.engine(book.company)).closed_report("2026-01")
    monkeypatch.setattr(Periods, "close", original)
    resumed = book.call("confirm_replay_close_range", **payload)
    assert resumed["status"] == "completed" and book.closes() == 3
    assert Periods(book.app.engine(book.company)).closed_report("2026-01") == first_digest
    assert len({x["request_id"] for x in resumed["results"]}) == 3


def test_unrelated_management_mutation_invalidates_preview(replay_book):
    book = replay_book
    payload = book.payload(book.preview())
    Periods(book.app.engine(book.company)).management(
        "profile",
        note="New synthetic note",
        payment_period=None,
        payment_category=None,
        expected_revision=0,
        request_id="unrelated-management-change",
    )
    with pytest.raises(KernelError) as error:
        book.call("confirm_replay_close_range", **payload)
    assert error.value.code == "preview_expired" and book.closes() == 0


def test_mutation_between_months_preserves_closed_prefix_and_blocks_next(replay_book, monkeypatch):
    book = replay_book
    payload = book.payload(book.preview())
    original = Periods.close

    def close_then_mutate(periods, month, **kwargs):
        receipt = original(periods, month, **kwargs)
        if month == "2026-01":
            periods.management(
                "profile",
                note="Concurrent synthetic owner correction",
                payment_period=None,
                payment_category=None,
                expected_revision=0,
                request_id="between-months-management-change",
            )
        return receipt

    monkeypatch.setattr(Periods, "close", close_then_mutate)
    result = book.call("confirm_replay_close_range", **payload)
    assert result["status"] == "partial"
    assert result["closed_through"] == "2026-01" and result["blocked_period"] == "2026-02"
    assert result["issue"]["code"] == "preview_expired" and book.closes() == 1


def test_read_repair_between_months_blocks_without_business_epoch_change(replay_book, monkeypatch):
    book = replay_book
    payload = book.payload(book.preview())
    original = Periods.close

    def close_then_repair(periods, month, **kwargs):
        receipt = original(periods, month, **kwargs)
        if month == "2026-01":
            # Synthetic repair invalidation only; no real company or fabricated facts.
            with periods.engine.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                before = periods.engine.store.epochs(connection)
                assert advance_repair_revision(connection) == 1
                assert periods.engine.store.epochs(connection) == before
                connection.commit()
        return receipt

    monkeypatch.setattr(Periods, "close", close_then_repair)
    result = book.call("confirm_replay_close_range", **payload)
    assert result["status"] == "partial"
    assert result["closed_through"] == "2026-01" and result["blocked_period"] == "2026-02"
    assert result["issue"]["code"] == "preview_expired" and book.closes() == 1


@pytest.mark.parametrize("change_credentials", [False, True])
def test_revoked_session_cannot_confirm_or_resume(replay_book, change_credentials):
    book = replay_book
    payload = book.payload(book.preview())
    if change_credentials:
        book.app.security.change_password(book.token, PASSWORD, NEW_PASSWORD)
    else:
        book.app.security.logout(book.token)
    with pytest.raises(IdentityError):
        book.call("confirm_replay_close_range", **payload)
    assert book.closes() == 0


@pytest.mark.parametrize("change_credentials", [False, True])
def test_session_revocation_between_months_stops_before_second_commit(
    replay_book, monkeypatch, change_credentials
):
    book = replay_book
    payload = book.payload(book.preview())
    original = Periods.close

    def close_then_revoke(periods, month, **kwargs):
        receipt = original(periods, month, **kwargs)
        if month == "2026-01":
            if change_credentials:
                book.app.security.change_password(book.token, PASSWORD, NEW_PASSWORD)
            else:
                book.app.security.logout(book.token)
        return receipt

    monkeypatch.setattr(Periods, "close", close_then_revoke)
    with pytest.raises(IdentityError):
        book.call("confirm_replay_close_range", **payload)
    assert book.closes() == 1
    with pytest.raises(IdentityError):
        book.call("confirm_replay_close_range", **payload)


def test_same_request_key_with_changed_range_conflicts(replay_book):
    book = replay_book
    payload = book.payload(book.preview())
    book.call("confirm_replay_close_range", **payload)
    with pytest.raises(KernelError) as error:
        book.call("confirm_replay_close_range", **(payload | {"last_period": "2026-02"}))
    assert error.value.code == "idempotency_conflict"
    assert book.closes() == 3


def test_first_month_digest_change_blocks_even_when_epochs_are_unchanged(replay_book, monkeypatch):
    book = replay_book
    batch = book.preview()
    payload = book.payload(batch)
    original = Periods.preview_close

    def changed_review(periods, month, **kwargs):
        review = original(periods, month, **kwargs)
        assert review["epochs"] == batch["epochs"]
        altered = "f" * 64
        assert altered != review["digest"]
        return {**review, "digest": altered}

    monkeypatch.setattr(Periods, "preview_close", changed_review)
    result = book.call("confirm_replay_close_range", **payload)
    assert result["status"] == "blocked"
    assert result["closed_through"] is None and result["blocked_period"] == "2026-01"
    assert result["issue"]["code"] == "preview_expired"
    assert result["results"] == [] and book.closes() == 0


def test_released_company_cannot_use_replay_scope(replay_book, monkeypatch):
    from ai_accounting.kernel import versions

    book = replay_book
    batch = book.preview()
    original = versions.database_format

    def released_company(connection, *, bundle, kind, **kwargs):
        value = original(connection, bundle=bundle, kind=kind, **kwargs)
        return {**value, "status": "released"} if kind == "company" else value

    monkeypatch.setattr(versions, "database_format", released_company)
    for command, payload in (
        (
            "preview_replay_close_range",
            {
                "first_period": "2026-01",
                "last_period": "2026-03",
                "owner_confirmation": book.proof,
            },
        ),
        ("confirm_replay_close_range", book.payload(batch)),
    ):
        with pytest.raises(KernelError) as error:
            book.call(command, **payload)
        assert error.value.code == "replay_test_database_required"
    assert book.closes() == 0


def test_replay_schema_keeps_ordinary_close_password_contract(replay_book):
    book = replay_book
    schema = book.app.dispatch("schema", {})
    contract = schema["replay_close_contract"]
    assert contract["enabled"] and contract["normal_close_password_required"]
    assert contract["maximum_months"] == 120
    assert contract["scope_schema"]["properties"]["format"]["const"] == book.config["format"]
    assert "approval_id" in schema["command_schemas"]["close"]["required"]
    ordinary = book.call("preview_close", period="2026-01", owner_confirmation=book.proof)
    with pytest.raises(KernelError) as error:
        book.call(
            "close",
            period="2026-01",
            owner_confirmation=book.proof,
            preview_digest=ordinary["digest"],
            epochs=ordinary["epochs"],
            request_id="ordinary-close-requires-native-approval",
        )
    assert error.value.code == "invalid_command" and book.closes() == 0


@pytest.mark.parametrize("corrupt_child_request", [False, True])
def test_completed_retry_revalidates_corrupted_child_or_freeze(replay_book, corrupt_child_request):
    book = replay_book
    payload = book.payload(book.preview())
    completed = book.call("confirm_replay_close_range", **payload)
    engine = book.app.engine(book.company)
    with engine.store.connection(read_only=True) as connection:
        jobs = connection.execute("SELECT id FROM jobs ORDER BY id").fetchall()
    if corrupt_child_request:
        child = completed["results"][-1]["request_id"]
        altered = {**completed["results"][-1], "digest": "e" * 64}
        altered.pop("request_id")
        damage(
            engine,
            "request",
            "UPDATE request SET result=? WHERE id=?",
            (json.dumps(altered), child),
        )
    else:
        frozen = Periods(engine).closed_report("2026-03")
        # Preserve the current encoding and valid field shape while changing a frozen source.
        frozen["read_version"]["management"] += 1
        replace_stored_manifest(engine, frozen)
    with pytest.raises(KernelError) as error:
        book.call("confirm_replay_close_range", **payload)
    assert error.value.code == "request_content_invalid"
    assert book.closes() == 3
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT id FROM jobs ORDER BY id").fetchall() == jobs


def test_empty_company_cannot_skip_runtime_scope_first_month(replay_book):
    book = replay_book
    with pytest.raises(KernelError) as error:
        book.preview(first_period="2026-02")
    assert error.value.code == "replay_start_period" and book.closes() == 0


def test_completed_readonly_retry_allows_fresh_valid_owner_session(replay_book):
    book = replay_book
    payload = book.payload(book.preview())
    completed = book.call("confirm_replay_close_range", **payload)
    engine = book.app.engine(book.company)
    with engine.store.connection(read_only=True) as connection:
        jobs = connection.execute("SELECT id FROM jobs ORDER BY id").fetchall()
        epochs = engine.store.epochs(connection)
        audits = connection.execute("SELECT count(*) FROM audit").fetchone()[0]
    book.app.security.logout(book.token)
    book.token = book.app.security.login("owner", PASSWORD).session_token
    assert book.call("confirm_replay_close_range", **payload) == completed
    assert book.closes() == 3
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.epochs(connection) == epochs
        assert connection.execute("SELECT count(*) FROM audit").fetchone()[0] == audits
        assert connection.execute("SELECT id FROM jobs ORDER BY id").fetchall() == jobs
