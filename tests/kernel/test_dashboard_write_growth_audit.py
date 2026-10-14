"""Observe production work for an unattached proof, without timing claims."""

import copy
import re
from contextlib import contextmanager

import pytest
from dashboard_model_fixture import model_current, produce
from test_banking import book as book  # noqa: F401
from test_banking import funding, opening
from test_dashboard_model_transactions import raw_read, source_body_work, state

from ai_accounting.kernel import dashboard_model_materializer as materializer
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.storage import _active_fact_reads


@contextmanager
def sql_data_work(engine, monkeypatch):
    """Observe real SQLite VM work and consumed result values, not raw hooks.

    Value bytes are UTF-8 strings, actual BLOBs and decimal scalar encodings;
    they measure returned data rather than disk I/O or Python allocations.
    """
    work = {"vm_steps": 0, "returned_rows": 0, "returned_value_bytes": 0,
            "source_rows": 0, "source_value_bytes": 0, "model_rows": 0,
            "model_value_bytes": 0, "source_selects": 0}
    original = engine.store.connection

    class ObservedCursor:
        def __init__(self, cursor, sql):
            self.cursor = cursor
            tables = re.findall(r'\b(?:FROM|JOIN)\s+["\[]?([\w]+)', sql, re.I)
            self.source = any(table.startswith(("fact_", "calculation", "voucher"))
                              for table in tables)
            self.model = "dashboard_read_model_block" in tables
            if self.source and cursor.description is not None:
                work["source_selects"] += 1

        def observed(self, row):
            if row is not None:
                size = sum(len(value) if isinstance(value, bytes) else
                           len(str(value).encode("utf-8")) if value is not None else 0
                           for value in row)
                work["returned_rows"] += 1
                work["returned_value_bytes"] += size
                for category in ("source", "model"):
                    if getattr(self, category):
                        work[category + "_rows"] += 1
                        work[category + "_value_bytes"] += size
            return row

        def fetchone(self):
            return self.observed(self.cursor.fetchone())

        def fetchall(self):
            return [self.observed(row) for row in self.cursor.fetchall()]

        def __iter__(self):
            return (self.observed(row) for row in self.cursor)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            return ObservedCursor(self.connection.execute(sql, parameters), sql)

        def executemany(self, sql, parameters):
            return ObservedCursor(self.connection.executemany(sql, parameters), sql)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def observed_connection(**kwargs):
        with original(**kwargs) as connection:
            def progress():
                work["vm_steps"] += 100
                return 0

            connection.set_progress_handler(progress, 100)
            yield ObservedConnection(connection)

    with monkeypatch.context() as scope:
        scope.setattr(engine.store, "connection", observed_connection)
        yield work


def comparable(value):
    value = copy.deepcopy(value)
    value.pop("read_context", None)
    value.pop("snapshot_version", None)
    value.pop("checked_at", None)
    if value.get("data"):
        value["data"].pop("generated_at", None)
    return value


def owner_pages(engine, period):
    dashboard = Dashboard(engine)
    year, month = map(int, period.split("-"))
    return [comparable(dashboard.brief(period)),
            comparable(dashboard.funds(period, movement_account_selection="first")),
            comparable(dashboard.employees(period)), comparable(dashboard.assets(period)),
            comparable(dashboard.quarterly_report(year, (month - 1) // 3 + 1))]


def native_pages(engine, period):
    with raw_read():
        return owner_pages(engine, period)


@pytest.mark.parametrize("months", [1, 3, 6])
def test_same_unattached_evidence_keeps_models_without_synchronous_rebuilds(
    book, monkeypatch, months,
):
    engine, save, publish, proof = book
    opening(save, publish, month="2026-01")
    funding(save, publish, amount=17, day="2026-01-01")
    periods = [f"2026-{1 + index:02}" for index in range(months)]
    for period in periods[1:]:
        Periods(engine).inventory(
            period, "payroll", evidence=[], expected=0, no_business=True,
            confirmation_evidence=proof, request_id="register-" + period,
        )
    for period in periods:
        produce(engine, period)
    before_business = {period: native_pages(engine, period) for period in periods}
    before_models = {period: model_current(engine, period) for period in periods}
    work = {"periods": [], "frames": 0, "typed_facts": set(), "typed_calculations": set(),
            "decoded_outcomes": 0}
    build, frame = materializer.build_model, materializer._native_frame
    stored_outcome = QueryReads._stored_outcome

    def observed_build(dashboard, period, **kwargs):
        active = _active_fact_reads.get()
        assert active is not None and active._snapshot_active
        assert active.connection.in_transaction and active.engine is engine
        work["periods"].append(period)
        result = build(dashboard, period, **kwargs)
        work["typed_facts"].update(active._fact_versions)
        work["typed_calculations"].update(active._typed_calculations)
        return result

    def observed_frame(*args, **kwargs):
        work["frames"] += 1
        return frame(*args, **kwargs)

    def observed_outcome(raw):
        work["decoded_outcomes"] += 1
        return stored_outcome(raw)

    monkeypatch.setattr(materializer, "build_model", observed_build)
    monkeypatch.setattr(materializer, "_native_frame", observed_frame)
    monkeypatch.setattr(QueryReads, "_stored_outcome", staticmethod(observed_outcome))
    with source_body_work(monkeypatch) as bodies, sql_data_work(engine, monkeypatch) as data_work:
        result = engine.register_evidence(
            b"same independent new proof", "text/plain", "unattached",
            request_id="independent-proof",
        )
    assert result["digest"]
    assert work["periods"] == [] and work["frames"] == 0
    assert work["typed_facts"] == work["typed_calculations"] == set()
    assert work["decoded_outcomes"] == 0 and bodies["facts"] == bodies["outcomes"] == []
    assert data_work["source_selects"] == data_work["source_rows"] == 0
    assert data_work["source_value_bytes"] == 0
    # Evidence registration neither reads nor rewrites certified sidecar bytes.
    # Source authority may inspect publication summaries for the known periods.
    assert data_work["model_rows"] == data_work["model_value_bytes"] == 0
    print({"registered_months": months, "builds": 0, "native_frames": 0,
           "sql": engine.last_metrics["sql_statements"], **data_work})
    assert {period: model_current(engine, period) for period in periods} == before_models
    assert {period: native_pages(engine, period) for period in periods} == before_business
    assert {period: owner_pages(engine, period) for period in periods} == before_business
    before = state(engine)
    calls = list(work["periods"])
    assert engine.register_evidence(
        b"same independent new proof", "text/plain", "unattached", request_id="independent-proof",
    ) == result
    assert work["periods"] == calls and state(engine) == before
    # Independent projection production does not weaken the real evidence write.
    if months == 3:
        def interrupted(stage, _connection):
            if stage == "published":
                raise RuntimeError("synthetic interrupted evidence write")

        monkeypatch.setattr(engine, "fault", interrupted)
        with pytest.raises(RuntimeError, match="interrupted evidence write"):
            engine.register_evidence(
                b"failed independent proof", "text/plain", "unattached", request_id="failed-proof",
            )
        assert state(engine) == before
        assert {period: model_current(engine, period) for period in periods} == before_models
