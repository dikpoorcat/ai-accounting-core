"""Real synthetic delivery work, including independent late checks."""

import json
import sqlite3

import pytest
from test_dashboard_async import _publish_snapshot_loans
from test_replay_close_range import replay_book as _replay_book

from ai_accounting.kernel import dashboard_read_model
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_async import _produce, _store_path
from ai_accounting.kernel.dashboard_model_runtime import authenticated_model

replay_book = _replay_book


def test_default_delivery_block_work(replay_book, monkeypatch, record_property):
    engine = _publish_snapshot_loans(replay_book)
    _produce(engine, "2026-01")
    _produce(engine, "2026-03")  # the latest registered month supplies Q1
    original = dashboard_read_model._decode_finite_block_value
    original_raw = dashboard_read_model._load_raw
    decoded = []
    loaded_bytes = []

    def decode(raw, reference, *args, **kwargs):
        decoded.append(reference.key)
        return original(raw, reference, *args, **kwargs)

    def load_raw(*args, **kwargs):
        raw = original_raw(*args, **kwargs)
        loaded_bytes.append(len(raw.encode("utf-8")))
        return raw

    monkeypatch.setattr(dashboard_read_model, "_decode_finite_block_value", decode)
    monkeypatch.setattr(dashboard_read_model, "_load_raw", load_raw)
    samples = []
    for page in ("brief", "funds", "employees", "assets", "quarterly_report"):
        payload = {"company_id": replay_book.company}
        payload.update({"year": 2026, "quarter": 1} if page == "quarterly_report"
                       else {"period": "2026-01"})
        if page == "funds":
            payload["movement_account_selection"] = "first"
        for response_format in ("native", "http", "http_json"):
            decoded.clear()
            loaded_bytes.clear()
            response = replay_book.app.dispatch(
                "dashboard_" + page, payload, session_token=replay_book.token,
                response_format=response_format,
            )
            wire = json.loads(response) if response_format == "http_json" else response
            assert wire["state"] == "ready"
            body = wire["response"]
            if page == "quarterly_report":
                assert {item["key"] for item in body["statements"]} == {
                    "balance_sheet", "profit_statement", "cash_flow_statement",
                }
                assert all(item["rows"] for item in body["statements"])
            else:
                assert body["data"] is not None
            # Bound delivery work without requiring the former triple decode.
            # Independent late checks are exercised below with actual mutations.
            assert decoded and len(decoded) <= 15
            samples.append({"page": page, "format": response_format,
                            "decoded_blocks": len(decoded),
                            "loaded_utf8_bytes": sum(loaded_bytes)})
    decoded.clear()
    loaded_bytes.clear()
    assert Dashboard(engine).brief("2026-01")["data"] is not None
    assert decoded and len(decoded) <= 15
    samples.append({"page": "brief", "format": "direct_dashboard",
                    "decoded_blocks": len(decoded),
                    "loaded_utf8_bytes": sum(loaded_bytes)})
    record_property("dashboard_delivery_block_work", json.dumps(samples))
    print(json.dumps(samples))


def test_checked_block_copy_and_owned_transaction_guard(replay_book):
    engine = _publish_snapshot_loans(replay_book)
    _produce(engine, "2026-01")
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-01") as snap:
        model = authenticated_model(dashboard, snap)
        key = "brief/default/overview/0"
        first = model.read_page_block(key)
        expected_version = first.payload["schema_version"]
        first.payload["schema_version"] = -1
        assert model.read_page_block(key).payload["schema_version"] == expected_version
        for operation in (
            lambda: model.connection.execute("COMMIT"),
            lambda: model.connection.executescript("SELECT 1"),
            lambda: model.connection.set_authorizer(None),
            lambda: setattr(model.connection, "isolation_level", ""),
        ):
            with pytest.raises(sqlite3.DatabaseError):
                operation()
        assert model.read_page_block(key).payload["schema_version"] == expected_version
    with pytest.raises(sqlite3.ProgrammingError):
        model.read_page_block(key)


def test_default_block_work_does_not_grow_with_unrelated_future_business(
    replay_book, monkeypatch, record_property,
):
    from entity_fixture import seed_entities

    engine = _publish_snapshot_loans(replay_book)
    original = dashboard_read_model._decode_finite_block_value
    decoded = []

    def decode(raw, reference, *args, **kwargs):
        decoded.append(reference.key)
        return original(raw, reference, *args, **kwargs)

    monkeypatch.setattr(dashboard_read_model, "_decode_finite_block_value", decode)
    observations = []
    for future_count in (0, 40):
        if future_count:
            seed_entities(engine, [
                (f"future-owner-{index}", "person", None) for index in range(future_count)
            ])
            subjects = [f"future-funding-{index}" for index in range(future_count)]
            engine.save_facts([
                {"kind": "cash_funding", "subject_id": subject,
                 "data": {"period": "2027-01", "actual_date": "2027-01-10",
                          "owner_id": f"future-owner-{index}", "amount_fen": 100 + index,
                          "funding_kind": "loan", "cash_account_id": "snapshot-cash"},
                 "evidence": [replay_book.proof], "expected_revision": 0}
                for index, subject in enumerate(subjects)
            ], request_id="publish-unrelated-future-facts")
            preview = engine.preview(subjects)
            engine.confirm(
                subjects, preview_digest=preview["digest"], epochs=preview["epochs"],
                request_id="publish-unrelated-future-results",
            )
        _produce(engine, "2026-01")
        _produce(engine, "2026-03")
        page_work = {}
        for page in ("brief", "funds", "employees", "assets", "quarterly_report"):
            payload = {"company_id": replay_book.company}
            payload.update({"year": 2026, "quarter": 1} if page == "quarterly_report"
                           else {"period": "2026-01"})
            if page == "funds":
                payload["movement_account_selection"] = "first"
            decoded.clear()
            response = replay_book.app.dispatch(
                "dashboard_" + page, payload, session_token=replay_book.token,
            )
            assert response["state"] == "ready"
            page_work[page] = len(decoded)
            if page == "funds":
                assert response["response"]["data"]["net_change_fen"] == 6
                assert response["response"]["data"]["movement_count"] == 3
        observations.append({"unrelated_businesses": future_count, "page_work": page_work})
    baseline = observations[0]["page_work"]
    expanded = observations[1]["page_work"]
    # Forty unrelated businesses must not add per-business decoding. Allow one
    # bounded default batch for a changed valid cache/publication implementation.
    assert all(0 < expanded[page] <= baseline[page] + 5 for page in baseline)
    assert all(work <= 15 for work in expanded.values())
    record_property("unrelated_default_block_work", json.dumps(observations))


def test_default_hit_corruption_still_fails(replay_book):
    from test_integrity_content import damage

    engine = _publish_snapshot_loans(replay_book)
    _produce(engine, "2026-01")
    damage(
        engine, "calculation",
        "UPDATE calculation SET outcome=json_set(outcome,'$.values.amount_fen',999) "
        "WHERE subject_id='snapshot-loan-0'",
    )
    assert replay_book.call(
        "dashboard_funds", period="2026-01", movement_account_selection="first",
    )["reason"] == "corrupt"


def test_failed_complete_batch_is_not_remembered(replay_book, monkeypatch):
    engine = _publish_snapshot_loans(replay_book)
    _produce(engine, "2026-01")
    with sqlite3.connect(_store_path(engine)) as connection:
        # Damage a late block: no successful prefix may become a reusable batch.
        connection.execute(
            "UPDATE dashboard_read_model_block SET payload='{}' "
            "WHERE json_extract(payload,'$.kind')='dashboard_quarterly_report'"
        )
    published = []
    decoded = []
    original = dashboard_read_model.LoadedModel._remember_checked_blocks
    original_decode = dashboard_read_model._decode_finite_block_value

    def remember(*args, **kwargs):
        published.append(1)
        return original(*args, **kwargs)

    def decode(value, reference, *args, **kwargs):
        decoded.append(reference.key)
        return original_decode(value, reference, *args, **kwargs)

    monkeypatch.setattr(dashboard_read_model.LoadedModel, "_remember_checked_blocks", remember)
    monkeypatch.setattr(dashboard_read_model, "_decode_finite_block_value", decode)
    with pytest.raises(KernelError) as error:
        Dashboard(engine).brief("2026-01")
    assert error.value.code == "content_integrity_failed"
    assert decoded  # actual successful block prefix preceded the damaged block
    assert published == []


@pytest.mark.parametrize("late_change", ["source", "model_block"])
def test_delivery_post_check_remains_independent(replay_book, monkeypatch, late_change):
    engine = _publish_snapshot_loans(replay_book)
    _produce(engine, "2026-01")
    original = replay_book.app._dispatch

    def dispatch(command, payload, **options):
        result = original(command, payload, **options)
        if command == "dashboard_brief":
            if late_change == "source":
                engine.save_fact(
                    "cash_funding", "late-source",
                    {"period": "2026-01", "actual_date": "2026-01-04",
                     "owner_id": "snapshot-owner", "amount_fen": 4,
                     "funding_kind": "loan", "cash_account_id": "snapshot-cash"},
                    evidence=(replay_book.proof,), expected_revision=0,
                    request_id="delivery-late-source",
                )
            else:
                with sqlite3.connect(_store_path(engine)) as connection:
                    connection.execute("UPDATE dashboard_read_model_block SET payload='{}'")
        return result

    monkeypatch.setattr(replay_book.app, "_dispatch", dispatch)
    # Existing delivery semantics require another attempt after a late change;
    # the newly opened post snapshot must not borrow the response's proof.
    assert replay_book.call("dashboard_brief", period="2026-01")["state"] == "preparing"
