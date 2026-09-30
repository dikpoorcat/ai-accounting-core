"""Growing collection scopes must load only the selected page's identities."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

import ai_accounting.kernel.business_queries as business_query_module
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.workflow import Workflow


def _work(connection, callback):
    ticks = [0]
    connection.set_progress_handler(lambda: ticks.__setitem__(0, ticks[0] + 100), 100)
    try:
        result = callback()
    finally:
        connection.set_progress_handler(None, 0)
    return result, ticks[0]


def test_source_history_growth_loads_page_and_uses_fewer_vm_steps(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE fact_revision("
        "id TEXT PRIMARY KEY,subject_id TEXT,revision INTEGER,period INTEGER);"
        "CREATE INDEX fact_scope ON fact_revision(subject_id,revision,id);"
        "CREATE TABLE calculation(id TEXT,subject_id TEXT,fact_id TEXT);"
        "CREATE INDEX calculation_subject ON calculation(subject_id,id);"
        "CREATE TABLE calculation_publication(calculation_id TEXT);"
    )
    connection.executemany(
        "INSERT INTO fact_revision VALUES(?,?,?,?)",
        [
            (f"target-{index:04}", "target", index, 24227)
            for index in range(1000)
        ]
        + [
            (f"other-{index:04}", f"other-{index:04}", 1, 24227)
            for index in range(4000)
        ],
    )

    class Reads:
        def __init__(self):
            self.connection = connection
            self.loaded = []

        def facts(self, identifiers):
            self.loaded.extend(identifiers)
            return {
                ident: {"id": ident, "subject_id": "target", "revision": int(ident[-4:])}
                for ident in identifiers
            }

    reads = Reads()
    monkeypatch.setattr(business_query_module, "recorded_times", lambda *_: {})
    queries = BusinessQueries(SimpleNamespace(store=object()), reads=reads)
    baseline, old_vm = _work(
        connection,
        lambda: connection.execute(
            "SELECT f.id,f.subject_id,f.revision FROM fact_revision f "
            "WHERE f.subject_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY f.subject_id,f.revision,f.id",
            (json.dumps(["target"]),),
        ).fetchall(),
    )
    first, first_vm = _work(
        connection,
        lambda: queries.business_collection(
            connection, "target", "2019-12", section="source_history", limit=2
        ),
    )
    assert len(baseline) == 1000
    assert [item["id"] for item in first["items"]] == [row["id"] for row in baseline[:2]]
    assert first["page"]["total_count"] == 1000
    assert reads.loaded == [row["id"] for row in baseline[:2]]
    assert first_vm < old_vm
    second = queries.business_collection(
        connection,
        "target",
        "2019-12",
        section="source_history",
        after=first["page"]["next_cursor"],
        limit=2,
    )
    assert [item["id"] for item in second["items"]] == [row["id"] for row in baseline[2:4]]
    with pytest.raises(KernelError) as error:
        queries.business_collection(
            connection, "target", "2019-12", section="source_history", after="other-0000"
        )
    assert error.value.code == "dashboard_snapshot_changed"


def test_external_followup_growth_loads_only_page_and_uses_fewer_vm_steps(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
        "CREATE INDEX subject_kind ON subject(kind,id);"
        "CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY);"
    )
    connection.executemany(
        "INSERT INTO subject VALUES(?,?)",
        [(f"external-{index:04}", "external_obligation") for index in range(2000)]
        + [(f"other-{index:04}", "other_kind") for index in range(4000)],
    )
    connection.executemany(
        "INSERT INTO fact_current VALUES(?)",
        [(f"external-{index:04}",) for index in range(2000)]
        + [(f"other-{index:04}",) for index in range(4000)],
    )
    requested = []

    def selected_obligations(_self, _connection, _period, _as_of, *, obligation_ids, **_):
        requested.append(set(obligation_ids))
        return [
            {
                "id": ident,
                "kind": "quarterly_financial_report",
                "start_period": "2019-12",
                "end_period": "2019-12",
                "actual_completion_status": "not_completed",
                "basis_review_status": "not_reviewed",
                "basis_review_calculation_id": None,
                "due_date": None,
                "basis_issues": [],
            }
            for ident in sorted(obligation_ids)
        ]

    monkeypatch.setattr(Workflow, "_external_obligations", selected_obligations)
    dashboard = Dashboard(SimpleNamespace(store=object()))
    snap = SimpleNamespace(connection=connection, period="2019-12", as_of="2019-12-31", reads=None)
    baseline, old_vm = _work(
        connection,
        lambda: connection.execute(
            "SELECT s.id FROM subject s JOIN fact_current f ON f.subject_id=s.id "
            "WHERE s.kind='external_obligation' ORDER BY s.id"
        ).fetchall(),
    )
    first, first_vm = _work(connection, lambda: dashboard._external_collection(snap, None, 2))
    assert len(baseline) == 2000
    assert [item["obligation_id"] for item in first["items"]] == [
        row["id"] for row in baseline[:2]
    ]
    assert first["page"]["total_count"] == 2000
    assert requested == [{row["id"] for row in baseline[:2]}]
    assert first_vm < old_vm
    second = dashboard._external_collection(snap, first["page"]["next_cursor"], 2)
    assert [item["obligation_id"] for item in second["items"]] == [
        row["id"] for row in baseline[2:4]
    ]
    with pytest.raises(KernelError) as error:
        dashboard._external_collection(snap, "other-0000", 2)
    assert error.value.code == "dashboard_snapshot_changed"
