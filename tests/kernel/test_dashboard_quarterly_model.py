"""Quarterly aggregates retain native statements, frozen sources and write atomicity."""

import copy
import json
from collections import Counter
from contextlib import contextmanager

import pytest
import test_reports as report_cases
from dashboard_model_fixture import derived_connection, produce
from test_dashboard_model_transactions import raw_read, source_body_work, state
from test_integrity_content import damage, verify

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_materializer import build_model
from ai_accounting.kernel.dashboard_model_runtime import producing_model
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.storage import _active_fact_reads
from ai_accounting.kernel.stored_json import loads_unique

book = report_cases.book
KEY = "quarterly_report/default/overview/0"


def comparable(value):
    value = copy.deepcopy(value)
    value.pop("checked_at")
    return value


def bootstrap(engine, period="2026-02", request="quarter-model"):
    Maintenance(engine).rebuild_dashboard_read_model(period, request_id=request)
    with engine.store.connection(read_only=True) as connection:
        periods = [
            row[0]
            for row in connection.execute(
                "SELECT period FROM dashboard_projection_request ORDER BY period"
            )
        ]
    for month in periods:
        produce(engine, month)


def quarterly_digest(engine):
    with derived_connection(engine) as connection:
        descriptor = loads_unique(
            connection.execute(
                "SELECT c.descriptor FROM dashboard_read_model_current p "
                "JOIN dashboard_read_model_commit c ON c.id=p.commit_id "
                "ORDER BY p.period DESC LIMIT 1",
            ).fetchone()[0]
        )
        root = loads_unique(
            connection.execute(
                "SELECT payload FROM dashboard_read_model_block WHERE digest=?",
                (bytes.fromhex(descriptor["root_digest"]),),
            ).fetchone()[0]
        )
        return bytes.fromhex(
            next(block["digest"] for block in root["blocks"] if block["key"] == KEY)
        )


@pytest.mark.parametrize("classification", (True, False))
@pytest.mark.parametrize("preparation", ("complete", "deferred"))
def test_complete_owner_quarter_matches_native_ready_and_blocked_response(
    book,
    classification,
    preparation,
):
    report_cases.scenario(book, classification=classification)
    engine = book[0]
    if classification:
        report_cases.close_quarter(book)
    dashboard = Dashboard(engine)
    bootstrap(engine)
    with raw_read():
        expected = dashboard.quarterly_report(2026, 1, preparation=preparation)
    actual = dashboard.quarterly_report(2026, 1, preparation=preparation)
    validate_response("dashboard_quarterly_report", actual)
    assert comparable(actual) == comparable(expected)
    assert actual["readiness_state"] == ("ready" if classification else "blocked")
    assert len(actual["statements"]) == 3
    assert all(statement["rows"] for statement in actual["statements"])


def test_quarter_model_uses_latest_registered_month_and_reads_no_raw_ancestors(book, monkeypatch):
    report_cases.scenario(book)
    engine = book[0]
    bootstrap(engine, "2026-01", "first-quarter-month")
    bootstrap(engine, "2026-02", "latest-quarter-month")
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.quarterly_report(2026, 1)
    before = state(engine)
    with engine.store.connection(read_only=True) as connection:
        latest = connection.execute(
            "SELECT max(period) FROM dashboard_projection_current "
            "WHERE period BETWEEN '2026-01' AND '2026-03'",
        ).fetchone()[0]
    assert latest == "2026-03"  # scenario contains actual March facts/publications.
    selected, snapshots = [], []

    from ai_accounting.kernel import dashboard_async

    original_load = dashboard_async.load_for_snapshot
    original_snapshot = QueryReads.snapshot

    def load(engine, snap):
        selected.append(snap.period)
        return original_load(engine, snap)

    @contextmanager
    def snapshot(engine):
        with original_snapshot(engine) as reads:
            snapshots.append(reads.connection)
            yield reads

    def unexpected(*_args, **_kwargs):
        raise AssertionError("certified quarterly aggregate rebuilt raw statements")

    with monkeypatch.context() as scope:
        scope.setattr(dashboard_async, "load_for_snapshot", load)
        scope.setattr(QueryReads, "snapshot", snapshot)
        scope.setattr(Reports, "_report", unexpected)
        with source_body_work(monkeypatch) as work:
            actual = dashboard.quarterly_report(2026, 1)
    assert selected == [latest]
    assert len(snapshots) == 1
    assert work == {"outcomes": [], "facts": [], "lineages": []}
    assert comparable(actual) == comparable(expected)
    assert state(engine) == before
    print(
        json.dumps(
            {"quarterly_report_source_work": {key: len(values) for key, values in work.items()}}
        )
    )


@pytest.mark.parametrize("period", ("2026-01", "2026-02", "2026-03"))
def test_model_producer_builds_native_quarter_in_an_owned_read_snapshot(
    book,
    monkeypatch,
    period,
):
    report_cases.scenario(book)
    engine = book[0]
    calls, base_periods, writers = [], [], []
    with engine.store.connection(read_only=True) as connection:
        registered = sorted(
            row[0]
            for row in connection.execute(
                "SELECT period FROM dashboard_projection_request",
            )
        )
    assert registered == ["2026-01", "2026-02", "2026-03"]
    original = Dashboard._quarterly_report_in_snapshot
    from ai_accounting.kernel import dashboard_model_materializer

    original_build = dashboard_model_materializer.build_model

    def observed_build(dashboard, requested_period, **options):
        base_periods.append(requested_period)
        return original_build(dashboard, requested_period, **options)

    def observed(self, year, quarter, *, connection, reads, as_of):
        assert producing_model()
        assert reads is _active_fact_reads.get()
        assert reads.connection is connection
        assert reads._snapshot_active and connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        calls.append((year, quarter))
        writers.append(connection)
        changes = connection.total_changes
        result = original(self, year, quarter, connection=connection, reads=reads, as_of=as_of)
        assert connection.total_changes == changes
        return result

    with monkeypatch.context() as scope:
        scope.setattr(Dashboard, "_quarterly_report_in_snapshot", observed)
        scope.setattr(dashboard_model_materializer, "build_model", observed_build)
        bootstrap(engine, period)
    # Maintenance independently rebuilds every registered current frame.
    # Unchanged exact scopes need no second production after the business audit.
    assert Counter(base_periods) == Counter(registered)
    assert calls == [(2026, 1)] * len(registered)
    assert len(writers) == len(registered)
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.quarterly_report(2026, 1)
    assert comparable(dashboard.quarterly_report(2026, 1)) == comparable(expected)
    model = build_model(dashboard, period)
    block = next(block for block in model.blocks if block.key == KEY)
    validate_response(block.kind, block.payload)
    assert comparable(block.payload) == comparable(expected)
    assert not block.sources and not block.fact_sources and not block.voucher_version_ids
    assert model.unmaterialized == ()


def test_closed_quarter_model_retains_exact_frozen_figures_and_export_preview(book, monkeypatch):
    report = report_cases.scenario(book)
    report_cases.close_quarter(book)
    engine, save, publish, _ = book
    bootstrap(engine, "2026-03")
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.quarterly_report(2026, 1)
    plan = report.preview_export(2026, 1)
    actual = dashboard.quarterly_report(2026, 1)
    assert actual["close_state"] == "closed" and actual["export"]["available"]
    assert comparable(actual) == comparable(expected)
    assert actual["export"]["preview_digest"] == plan["digest"]
    assert actual["export"]["epochs"] == plan["epochs"]

    # Later open facts refresh the root but cannot replace frozen Q1 amounts.
    report_cases.profile(save, publish, "2026-04", name="后续名称", subject="later-profile")
    save(
        "cash_funding",
        "later-capital",
        {
            "period": "2026-04",
            "actual_date": "2026-04-01",
            "owner_id": "owner",
            "funding_kind": "capital",
            "amount_fen": 12345,
            "cash_account_id": "cash",
        },
    )
    publish("later-capital")
    with raw_read():
        refreshed_native = dashboard.quarterly_report(2026, 1)
    refreshed_plan = report.preview_export(2026, 1)
    bootstrap(engine, "2026-03", "post-April-quarter-refresh")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("closed quarterly owner default bypassed its certified aggregate")

    with monkeypatch.context() as scope:
        scope.setattr(Reports, "_report", unexpected)
        refreshed = dashboard.quarterly_report(2026, 1)
    assert comparable(refreshed) == comparable(refreshed_native)
    assert refreshed["statements"] == actual["statements"]
    assert refreshed["summary"] == actual["summary"]
    assert refreshed["organization"] == actual["organization"]
    assert refreshed["export"]["preview_digest"] == refreshed_plan["digest"]
    assert refreshed["export"]["epochs"] == refreshed_plan["epochs"]
    # Explicit closed reports still invoke the native full report reader.
    native_calls = []
    original = Reports._report

    def native(self, *args, **kwargs):
        native_calls.append(kwargs["source"])
        return original(self, *args, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(Reports, "_report", native)
        assert report.report(2026, 1, source="closed")["statements"] == refreshed_plan["statements"]
    assert native_calls == ["closed"]


@pytest.mark.parametrize("closed", (False, True))
def test_corrupt_quarterly_block_is_rejected_without_native_fallback(book, monkeypatch, closed):
    report_cases.scenario(book)
    if closed:
        report_cases.close_quarter(book)
    engine = book[0]
    bootstrap(engine)
    block_digest = quarterly_digest(engine)
    with derived_connection(engine, read_only=False) as connection:
        connection.execute(
            "UPDATE dashboard_read_model_block SET payload=json_set("
            "payload,'$.payload.summary.assets_total_fen',123) WHERE digest=?",
            (block_digest,),
        )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("damaged quarterly production was silently recomputed")

    monkeypatch.setattr(Reports, "_report", unexpected)
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).quarterly_report(2026, 1)
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("damage_kind", ("typed_fact", "outcome"))
def test_raw_aggregate_ancestor_damage_is_caught_by_independent_full_verification(
    book,
    damage_kind,
):
    report_cases.scenario(book)
    engine = book[0]
    bootstrap(engine)
    dashboard = Dashboard(engine)
    expected = dashboard.quarterly_report(2026, 1)
    if damage_kind == "typed_fact":
        damage(
            engine,
            "fact_expense",
            "DELETE FROM fact_expense WHERE revision_id=("
            "SELECT fact_id FROM fact_current WHERE subject_id='cost')",
        )
    else:
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=json_set("
            "outcome,'$.values.amount',999) WHERE subject_id='cost'",
        )
    assert comparable(dashboard.quarterly_report(2026, 1)) == comparable(expected)
    with pytest.raises(KernelError) as failure:
        verify(engine)
    assert failure.value.code == "content_integrity_failed"
    with pytest.raises(KernelError) as failure:
        Maintenance(engine).rebuild_dashboard_read_model("2026-02", request_id="cannot-hide-damage")
    assert failure.value.code == "content_integrity_failed"


def test_new_date_and_unregistered_quarter_use_current_native_snapshots(book, monkeypatch):
    from ai_accounting.kernel import business_queries

    report_cases.scenario(book)
    engine = book[0]
    bootstrap(engine)
    dashboard = Dashboard(engine)
    original = Dashboard._quarterly_report_in_snapshot
    calls = []

    def native(self, year, quarter, *, connection, reads, as_of):
        assert reads.connection is connection and reads._snapshot_active
        calls.append((quarter, as_of))
        return original(self, year, quarter, connection=connection, reads=reads, as_of=as_of)

    before = state(engine)
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2099-01-01")
    with raw_read():
        expected = dashboard.quarterly_report(2026, 1)
        expected_other = dashboard.quarterly_report(2026, 2)
    monkeypatch.setattr(Dashboard, "_quarterly_report_in_snapshot", native)
    value = dashboard.quarterly_report(2026, 1)
    other = dashboard.quarterly_report(2026, 2)
    assert calls == [(1, "2099-01-01"), (2, "2099-01-01")]
    assert comparable(value) == comparable(expected)
    assert comparable(other) == comparable(expected_other)
    assert value["read_context"]["as_of"] == "2099-01-01"
    assert other["period"]["quarter"] == 2
    assert state(engine) == before


def test_quarterly_production_failure_preserves_committed_source_and_previous_publication(
    book,
    monkeypatch,
):
    report_cases.scenario(book)
    engine = book[0]
    bootstrap(engine)
    original = Dashboard._quarterly_report_in_snapshot

    def failure(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("synthetic quarterly production interruption")

    request = {
        "source": "synthetic owner update",
        "expected_revision": 1,
        "request_id": "retry-quarterly-write",
    }
    with monkeypatch.context() as scope:
        scope.setattr(Dashboard, "_quarterly_report_in_snapshot", failure)
        result = Entities(engine).update_entity_profile(
            "cash",
            {"display_name": "synthetic quarterly account update"},
            **request,
        )
        assert result["revision"] == 2
        committed_source = state(engine)
        with pytest.raises(RuntimeError, match="synthetic quarterly production interruption"):
            produce(engine, "2026-03")
        assert state(engine) == committed_source
        assert (
            Entities(engine).update_entity_profile(
                "cash",
                {"display_name": "synthetic quarterly account update"},
                **request,
            )
            == result
        )
    bootstrap(engine, "2026-03", "retried-quarterly-production")
    dashboard = Dashboard(engine)
    with raw_read():
        expected = dashboard.quarterly_report(2026, 1)
    assert comparable(dashboard.quarterly_report(2026, 1)) == comparable(expected)


@pytest.mark.parametrize("scope", ("different_connection", "expired_snapshot"))
def test_native_quarter_provider_rejects_an_unowned_or_expired_snapshot(book, scope):
    engine = book[0]
    dashboard = Dashboard(engine)
    with QueryReads.snapshot(engine) as reads:
        if scope == "different_connection":
            with engine.store.connection(read_only=True) as other:
                with pytest.raises(ValueError, match="another snapshot"):
                    dashboard._quarterly_report_in_snapshot(
                        2026,
                        1,
                        connection=other,
                        reads=reads,
                        as_of="2026-10-11",
                    )
            return
        connection = reads.connection
    with pytest.raises(ValueError, match="another snapshot"):
        dashboard._quarterly_report_in_snapshot(
            2026,
            1,
            connection=connection,
            reads=reads,
            as_of="2026-10-11",
        )
