"""Synthetic source-bound publication and recovery, independent of real books."""

import hashlib
import sqlite3
from contextlib import contextmanager

import pytest
from entity_fixture import seed_entities
from material_fixture import supporting_text
from test_replay_close_range import replay_book as _replay_book

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_async import (
    _fingerprint,
    _produce,
    _store_path,
    _unchanged,
    delivery_state,
    run_pending,
)
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.stored_json import loads_unique

replay_book = _replay_book


def _publish_snapshot_loans(replay_book):
    engine = replay_book.app.engine(replay_book.company)
    seed_entities(engine, [("snapshot-cash", "fund_account", "cash"),
                           ("snapshot-owner", "person", None)])
    subjects = [f"snapshot-loan-{index}" for index in range(3)]
    engine.save_facts([
        {"kind": "cash_funding", "subject_id": subject,
         "data": {"period": "2026-01", "actual_date": f"2026-01-{index + 1:02d}",
                  "owner_id": "snapshot-owner", "amount_fen": index + 1,
                  "funding_kind": "loan", "cash_account_id": "snapshot-cash"},
         "evidence": [replay_book.proof], "expected_revision": 0}
        for index, subject in enumerate(subjects)
    ], request_id="snapshot-loan-sources")
    preview = engine.preview(subjects)
    engine.confirm(subjects, preview_digest=preview["digest"], epochs=preview["epochs"],
                   request_id="snapshot-loan-publication")
    return engine


def test_async_overview_version_survives_native_details_and_voucher_continuation(
    replay_book, monkeypatch,
):
    from ai_accounting.kernel import dashboard_async

    engine = _publish_snapshot_loans(replay_book)
    publication = _produce(engine, "2026-01")
    authentications = []
    original = dashboard_async._anchored_model

    def authenticate(*args, **kwargs):
        authentications.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(dashboard_async, "_anchored_model", authenticate)
    dashboard = Dashboard(engine)
    overview = dashboard.brief("2026-01")
    version = overview["snapshot_version"]
    assert version == publication["model_version"]
    expected = {"expected_version": version, "limit": 1}

    first = dashboard.brief("2026-01", section="vouchers", **expected)
    collection = first["data"]["collections"]["vouchers"]
    assert collection["page"]["has_more"]
    following = dashboard.brief("2026-01", section="vouchers",
                                cursor=collection["page"]["next_cursor"], **expected)
    tail = dashboard.brief(
        "2026-01", section="vouchers",
        cursor=following["data"]["collections"]["vouchers"]["page"]["next_cursor"],
        **expected,
    )
    all_vouchers = dashboard.brief(
        "2026-01", section="vouchers", limit=500, expected_version=version,
    )
    pages = [response["data"]["collections"]["vouchers"]
             for response in (first, following, tail)]
    identifiers = [item["voucher_version_id"] for page in pages for item in page["items"]]
    complete = all_vouchers["data"]["collections"]["vouchers"]
    assert len(identifiers) == len(set(identifiers)) == complete["page"]["total_count"] == 3
    assert identifiers == [item["voucher_version_id"] for item in complete["items"]]
    for page in (pages[-1], complete):
        assert page["page"]["has_more"] is False and page["page"]["next_cursor"] is None
    responses = [first, following, tail, all_vouchers]
    for section in ("activity", "open_items"):
        response = dashboard.brief("2026-01", section=section, **expected)
        responses.append(response)
        groups = response["data"]["collections"][section]["items"]
        assert groups
        members = dashboard.brief_group(
            "2026-01", section=section, group_key=groups[0]["group_key"], **expected,
        )
        responses.append(members)
        member_page = members["data"]["collections"]["members"]["page"]
        if member_page["has_more"]:
            responses.append(dashboard.brief_group(
                "2026-01", section=section, group_key=groups[0]["group_key"],
                cursor=member_page["next_cursor"], **expected,
            ))
    responses.extend([
        dashboard.brief("2026-01", voucher_number=1, **expected),
        dashboard.funds("2026-01", section="movements", **expected),
        dashboard.employees("2026-01", section="employees", **expected),
        dashboard.assets("2026-01", section="assets", **expected),
        dashboard.business_status("2026-01", "snapshot-loan-0",
                                  section="settlement_events", **expected),
    ])
    for response in responses:
        assert response["snapshot_version"] == version
        assert response["read_context"] == overview["read_context"]
    assert len(authentications) == len(responses) + 1  # one proof per owned request snapshot


def test_async_details_reject_changed_sources_before_reproduction(replay_book):
    engine = _publish_snapshot_loans(replay_book)
    _produce(engine, "2026-01")
    dashboard = Dashboard(engine)
    overview = dashboard.brief("2026-01")
    engine.save_fact(
        "cash_funding", "snapshot-later-loan",
        {"period": "2026-01", "actual_date": "2026-01-04", "owner_id": "snapshot-owner",
         "amount_fen": 4, "funding_kind": "loan", "cash_account_id": "snapshot-cash"},
        evidence=(replay_book.proof,), expected_revision=0, request_id="snapshot-later-source",
    )
    with pytest.raises(KernelError) as error:
        dashboard.brief("2026-01", section="vouchers",
                        expected_version=overview["snapshot_version"])
    assert error.value.code == "dashboard_snapshot_changed"


def test_async_details_report_damaged_model_before_snapshot_mismatch(replay_book):
    engine = replay_book.app.engine(replay_book.company)
    _produce(engine, "2026-01")
    dashboard = Dashboard(engine)
    overview = dashboard.brief("2026-01")
    with sqlite3.connect(_store_path(engine)) as connection:
        connection.execute("UPDATE dashboard_read_model_block SET payload='{}'")
    with pytest.raises(KernelError) as error:
        dashboard.brief("2026-01", section="vouchers",
                        expected_version=overview["snapshot_version"])
    assert error.value.code == "content_integrity_failed"


def test_native_details_keep_overview_version_without_async_publication(replay_book):
    engine = _publish_snapshot_loans(replay_book)
    dashboard = Dashboard(engine)
    overview = dashboard.brief("2026-01")
    version = overview["snapshot_version"]
    first = dashboard.brief("2026-01", section="vouchers", limit=1, expected_version=version)
    following = dashboard.brief(
        "2026-01", section="vouchers", limit=1, expected_version=version,
        cursor=first["data"]["collections"]["vouchers"]["page"]["next_cursor"],
    )
    tail = dashboard.brief(
        "2026-01", section="vouchers", limit=1, expected_version=version,
        cursor=following["data"]["collections"]["vouchers"]["page"]["next_cursor"],
    )
    assert first["snapshot_version"] == following["snapshot_version"] == (
        tail["snapshot_version"]
    ) == version
    pages = [response["data"]["collections"]["vouchers"]
             for response in (first, following, tail)]
    identifiers = [item["voucher_version_id"] for page in pages for item in page["items"]]
    assert len(identifiers) == len(set(identifiers)) == pages[0]["page"]["total_count"] == 3
    assert pages[-1]["page"]["has_more"] is False
    assert pages[-1]["page"]["next_cursor"] is None


def test_business_commit_never_builds_dashboard_and_worker_publishes(replay_book):
    engine = replay_book.app.engine(replay_book.company)
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM dashboard_read_model_commit").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM dashboard_projection_publication").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM dashboard_projection_request").fetchone()[0]
            == 3
        )
    assert delivery_state(engine, "2026-01")["state"] == "preparing"
    outcome = _produce(engine, "2026-01")
    assert outcome["status"] == "ready"
    assert delivery_state(engine, "2026-01")["state"] == "ready"
    response = Dashboard(engine).brief("2026-01")
    assert response["snapshot_version"] == outcome["model_version"]
    assert response["data"] is not None
    assert _store_path(engine).is_file()


def test_unrelated_evidence_does_not_rebuild_and_corruption_is_failed(replay_book):
    engine = replay_book.app.engine(replay_book.company)
    outcome = _produce(engine, "2026-01")
    engine.register_evidence(
        b"unrelated original", "text/plain", "unrelated", request_id="unrelated"
    )
    state = delivery_state(engine, "2026-01")
    if state["state"] == "preparing":
        assert run_pending(engine, limit=1)[0]["status"] == "reused"
    assert delivery_state(engine, "2026-01")["model_version"] == outcome["model_version"]
    with sqlite3.connect(_store_path(engine)) as connection:
        connection.execute("UPDATE dashboard_read_model_block SET payload='{}'")
    assert delivery_state(engine, "2026-01") == {
        "state": "failed",
        "model_version": None,
        "reason": "corrupt",
    }


def test_empty_selector_fingerprint_changes_on_first_matching_source():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE source(period INTEGER, amount INTEGER)")
        sql = "SELECT amount FROM source WHERE period<=12 ORDER BY period"
        original = _fingerprint(connection, sql)
        connection.execute("INSERT INTO source VALUES(49,100)")
        assert _fingerprint(connection, sql) == original
        connection.execute("INSERT INTO source VALUES(12,50)")
        assert _fingerprint(connection, sql) != original


def test_ordered_funds_projection_revalidates_complete_sources_without_request_context(
    replay_book,
):
    engine = replay_book.app.engine(replay_book.company)
    proof = engine.register_evidence(
        b"Synthetic confirmation of 22 January cash capital receipts and their closing",
        "text/plain", "ordered funds confirmation", request_id="ordered-funds-proof",
    )["digest"]
    supporting_text(engine, proof)
    seed_entities(
        engine,
        [
            ("sort-cash-a", "fund_account", "cash"),
            ("sort-cash-b", "fund_account", "cash"),
            *((f"sort-owner-{index}", "person", None) for index in range(22)),
        ],
    )

    def publish(indices, request):
        facts = [
            {
                "kind": "cash_funding",
                "subject_id": f"sort-funding-{index}",
                "data": {
                    "period": "2026-01",
                    "actual_date": f"2026-01-{index + 1:02d}",
                    "owner_id": f"sort-owner-{index}",
                    "amount_fen": index + 1,
                    "funding_kind": "capital",
                    "cash_account_id": "sort-cash-a" if index < 21 else "sort-cash-b",
                },
                "evidence": [proof],
                "expected_revision": 0,
            }
            for index in indices
        ]
        engine.save_facts(facts, request_id=request + "-sources")
        subjects = [fact["subject_id"] for fact in facts]
        preview = engine.preview(subjects)
        engine.confirm(
            subjects,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=request + "-publication",
        )

    publish(range(22), "ordered-funds")
    periods = Periods(engine)
    periods.inventory(
        "2026-01", engine.store.registry.models["cash_funding"].material_category,
        evidence=[proof], expected=1, no_business=False,
        confirmation_evidence=proof, request_id="ordered-funds-inventory",
    )
    try:
        closing = periods.preview_close("2026-01", owner_confirmation=proof)
    except KernelError as exc:
        pytest.fail(str(exc.response()))
    periods.close(
        "2026-01", owner_confirmation=proof, preview_digest=closing["digest"],
        epochs=closing["epochs"], request_id="ordered-funds-close",
    )
    outcome = _produce(engine, "2026-01")
    assert outcome["status"] == "ready"
    assert delivery_state(engine, "2026-01")["state"] == "ready"
    response = Dashboard(engine).funds("2026-01", movement_account_selection="first")
    collection = response["data"]["collections"]["movements"]
    assert collection["page"]["total_count"] == 22
    assert collection["page"]["filtered_count"] == 21
    assert collection["page"]["has_more"] is True
    assert len(collection["items"]) == 20

    with sqlite3.connect(_store_path(engine).as_uri() + "?mode=ro", uri=True) as derived:
        dependencies = loads_unique(
            derived.execute("SELECT payload FROM projection_dependency").fetchone()[0]
        )
    assert all("dashboard_funds_ordered_page" not in item["sql"] for item in dependencies)
    assert all(not item["sql"].startswith("-- ") and "?" not in item["sql"]
               for item in dependencies)
    assert any("entity_profile_revision" in item["tables"] and "sort-owner-21" in item["sql"]
               for item in dependencies)
    source_selectors = [
        item for item in dependencies if item["sql"].startswith("SELECT * FROM (")
    ]
    assert source_selectors
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        publication = connection.execute(
            "SELECT p.*,c.validated_sequence FROM dashboard_projection_current c "
            "JOIN dashboard_projection_publication p ON p.id=c.publication_id "
            "WHERE c.period='2026-01'"
        ).fetchone()
        assert _unchanged(engine, connection, publication)
        sources = [connection.execute(item["sql"]).fetchall() for item in source_selectors]
        complete = next(rows for rows in sources if len(rows) == 22
                        and {row["balance_key"] for row in rows}
                        == {"sort-cash-a", "sort-cash-b"})
        sort_headers = [
            connection.execute(item["sql"]).fetchall()
            for item in dependencies
            if item["sql"].startswith("SELECT c.id,c.subject_id,c.fact_id,c.kind,c.period ")
        ]
        assert any({row["id"] for row in headers}
                   == {row["calculation_id"] for row in complete} for headers in sort_headers)
        assert all(_fingerprint(connection, item["sql"]) == item["digest"]
                   for item in source_selectors)


def test_http_preparing_has_no_money_and_ready_uses_typed_content(replay_book):
    result = replay_book.call("dashboard_brief", period="2026-01")
    assert result["state"] == "preparing"
    assert "response" not in result
    engine = replay_book.app.engine(replay_book.company)
    _produce(engine, "2026-01")
    result = replay_book.call("dashboard_brief", period="2026-01")
    assert result["state"] == "ready"
    assert result["model_version"] == result["response"]["snapshot_version"]


def test_source_change_before_publication_discards_then_recovers(replay_book):
    engine = replay_book.app.engine(replay_book.company)

    def concurrent_change(stage, connection):
        if stage == "dashboard_before_publish":
            connection.execute(
                "INSERT INTO evidence VALUES(?,?,?,?)",
                (hashlib.sha256(b"later source").digest(), b"later source", "text/plain", "later"),
            )

    engine.fault = concurrent_change
    assert _produce(engine, "2026-01")["status"] == "superseded"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM dashboard_projection_publication").fetchone()[
                0
            ]
            == 0
        )
    engine.fault = lambda stage, connection: None
    assert _produce(engine, "2026-01")["status"] == "ready"


def test_authorization_gate_only_wraps_commit_and_revocation_rolls_back(replay_book):
    engine = replay_book.app.engine(replay_book.company)
    gate = {"active": False, "revoked": False}

    @contextmanager
    def authority():
        assert not gate["active"]
        gate["active"] = True
        try:
            if gate["revoked"]:
                raise KernelError("permission_revoked", "synthetic authority revoked")
            yield
        finally:
            gate["active"] = False

    engine.commit_guard = authority

    def revoke(stage, connection):
        assert not gate["active"]
        if stage == "commit":
            gate["revoked"] = True

    engine.fault = revoke
    with pytest.raises(KernelError) as error:
        engine.register_evidence(b"must roll back", "text/plain", "revoked", request_id="revoked")
    assert error.value.code == "permission_revoked"
    assert engine.request_result("revoked")["status"] == "unknown"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM evidence WHERE digest=?",
                (hashlib.sha256(b"must roll back").digest(),),
            ).fetchone()
            is None
        )


def test_explicit_projection_repair_restarts_derived_generation(replay_book):
    from ai_accounting.kernel.maintenance import Maintenance

    engine = replay_book.app.engine(replay_book.company)
    _produce(engine, "2026-01")
    original = _store_path(engine)
    with sqlite3.connect(original) as connection:
        connection.execute("UPDATE dashboard_read_model_block SET payload='{}'")
    result = Maintenance(engine).rebuild_dashboard_read_model("2026-01", request_id="repair")
    assert result["status"] == "pending" and result["changed"] is True
    assert _store_path(engine) != original and original.exists()
    assert run_pending(engine, limit=1)[0]["status"] == "ready"
    assert delivery_state(engine, "2026-01")["state"] == "ready"
