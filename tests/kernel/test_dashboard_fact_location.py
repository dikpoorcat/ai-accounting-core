"""Type/period filtering precedes potentially large frozen provenance lookups."""

import sqlite3
from types import SimpleNamespace

from test_engine import close, publish, save
from test_engine import engine as engine_fixture

from ai_accounting.kernel.dashboard import _Snapshot
from ai_accounting.kernel.types import YearMonth

engine = engine_fixture


def test_fact_location_keeps_frozen_revision_and_open_heads(engine):
    original = save(engine, "source", "test_source", request="source")
    charge = save(engine)
    publish(engine)
    close(engine)
    save(engine, "source", "test_source", amount=125, revision=1, request="changed-source")
    february = save(engine, "feb", period="2026-02", request="feb")
    future = save(engine, "future", period="2026-03", request="future")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        snapshot = SimpleNamespace(connection=connection, month=YearMonth("2026-02").ordinal)

        def find(*kinds, **options):
            return _Snapshot.fact_ids_of_kind(snapshot, *kinds, **options)

        assert find("test_source") == [original["fact_id"]]
        assert set(find("test_charge")) == {charge["fact_id"], february["fact_id"]}
        assert find("test_charge", period="2026-01") == [charge["fact_id"]]
        assert find("test_charge", period="2026-02") == [february["fact_id"]]
        assert future["fact_id"] not in find("test_charge", period="2026-03")
        assert find() == []


def test_absent_type_does_not_run_provenance_checks_for_unrelated_month_facts():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);
        CREATE INDEX subject_kind ON subject(kind,id);
        CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT,revision INTEGER,
                                   period INTEGER,UNIQUE(subject_id,revision));
        CREATE INDEX fact_period ON fact_revision(period,subject_id,id);
        CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY,fact_id TEXT UNIQUE);
        CREATE TABLE period_close(period INTEGER PRIMARY KEY);
        CREATE TABLE calculation(id TEXT PRIMARY KEY,subject_id TEXT,fact_id TEXT);
        CREATE INDEX calculation_subject ON calculation(subject_id,id);
        CREATE TABLE close_reference(reference_type TEXT,reference_id TEXT,close_period INTEGER);
        CREATE INDEX close_reference_lookup ON
            close_reference(reference_type,reference_id,close_period);
        CREATE TABLE dependency_fact(calculation_id TEXT,fact_id TEXT,
                                     PRIMARY KEY(calculation_id,fact_id));
    """)
    month = YearMonth("2026-02").ordinal
    connection.executemany(
        "INSERT INTO subject VALUES(?,'unrelated')", [(str(i),) for i in range(12000)]
    )
    connection.executemany(
        "INSERT INTO fact_revision VALUES(?,?,1,?)", [(str(i), str(i), month) for i in range(12000)]
    )
    snapshot = SimpleNamespace(connection=connection, month=month)
    steps = [0]

    def progress():
        steps[0] += 100
        return 0

    connection.set_progress_handler(progress, 100)
    try:
        assert _Snapshot.fact_ids_of_kind(snapshot, "bank_statement", period="2026-02") == []
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()
    assert steps[0] < 1000
