"""Material checks reuse authoritative current heads and do not trust scope indexes."""

import json
import sqlite3
from collections import Counter

import pytest
from test_integrity_content import damage
from test_material_joint_groups import pool, resolve
from test_materials import Company, codes

from ai_accounting.kernel import materials
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, canonical


def test_material_amount_projection_preserves_exact_json_values():
    # Only amount fields are consumed by material links. Keep bad amount types
    # bad, and never round integers while narrowing the fetched JSON object.
    values = {
        "maximum_fen": 2**63 - 1,
        "minimum_fen": -(2**63),
        "overflow_fen": 2**70,
        "float_fen": 0.1234567890123456,
        "negative_zero_fen": -0.0,
        "bool_fen": True,
        "unknown_fen": None,
        "text_fen": "未核定汉字",
        "object_fen": {"amount": 2**70},
        "array_fen": [1, False, None],
        'quoted"_fen': 99,
        "unused_details": [{"amount_fen": index} for index in range(100)],
    }
    with sqlite3.connect(":memory:") as connection:
        statement = (
            "WITH c(outcome) AS (VALUES(?)) SELECT "
            + materials._MATERIAL_AMOUNT_VALUES_SQL
            + " FROM c"
        )
        raw = connection.execute(statement, (canonical({"values": values}),)).fetchone()[0]
        result = json.loads(raw)
        expected = {key: value for key, value in values.items() if key.endswith("_fen")}
        assert result == expected
        assert {key: type(value) for key, value in result.items()} == {
            key: type(value) for key, value in expected.items()
        }
        assert len(raw) < len(canonical(values)) // 2
        # A corrupted values container retains the old decoder's input;
        # the narrow read cannot turn it into an apparently valid empty object.
        for invalid in (None, [], 17, "invalid"):
            payload = canonical({"values": invalid})
            previous = connection.execute(
                "SELECT json_extract(?,'$.values')", (payload,)
            ).fetchone()[0]
            assert connection.execute(statement, (payload,)).fetchone()[0] == previous


def test_material_head_seek_preserves_unknown_sources_without_scanning_closed_rows():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT NOT NULL);"
        "CREATE INDEX subject_kind ON subject(kind,id);"
        "CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY,fact_id TEXT UNIQUE);"
        "CREATE TABLE fact_material_resolution_v2(revision_id TEXT PRIMARY KEY,source_id TEXT);"
        "CREATE TABLE fact_material_resolution_v2_links(revision_id TEXT,fact_id TEXT);"
        + materials.MATERIAL_READ_INDEX_DDL["material_resolution_v2"]
    )
    subjects = [(f"s-{number}", "material_resolution_v2") for number in range(12000)]
    connection.executemany("INSERT INTO subject VALUES(?,?)", subjects)
    connection.executemany(
        "INSERT INTO fact_current VALUES(?,?)",
        [(subject, f"f-{subject}") for subject, _ in subjects],
    )
    connection.executemany(
        "INSERT INTO fact_material_resolution_v2 VALUES(?,?)",
        [(f"f-s-{number}", f"source-{number//1000}") for number in range(12000)],
    )
    # The unknown source has no registry row, yet its current resolution must
    # remain visible. Historical revisions and withdrawn heads must not leak in.
    connection.execute("UPDATE fact_material_resolution_v2 SET source_id='missing-source' "
                       "WHERE revision_id='f-s-11000'")
    connection.execute("INSERT INTO fact_material_resolution_v2 VALUES('old','missing-source')")
    connection.execute("DELETE FROM fact_current WHERE subject_id='s-11999'")
    excluded = {f"source-{number}" for number in range(11)}
    ticks = [0]

    def counted():
        ticks[0] += 1
        return 0

    connection.set_progress_handler(counted, 100)
    expected = connection.execute(
        "SELECT c.fact_id FROM fact_current c JOIN subject s ON s.id=c.subject_id "
        "JOIN fact_material_resolution_v2 t ON t.revision_id=c.fact_id "
        "WHERE s.kind=? AND t.source_id NOT IN (SELECT value FROM json_each(?)) "
        "ORDER BY c.subject_id",
        ("material_resolution_v2", canonical(sorted(excluded))),
    ).fetchall()
    previous_work = ticks[0]
    ticks[0] = 0
    actual = materials._unfrozen_material_heads(connection, "material_resolution_v2", excluded)
    connection.set_progress_handler(None, 0)
    assert actual == expected
    assert len(actual) == 999
    assert ("f-s-11000",) in actual
    assert ("old",) not in actual
    assert ticks[0] < previous_work // 4
    connection.close()


class UncachedReads:
    """Independently read current heads without the production caches."""

    def __init__(self, connection, registry, *, query_reads=None):
        self.connection, self.registry = connection, registry

    def fact(self, fact_id):
        return Store.fact(self, self.connection, fact_id)

    def versions(self, fact_ids):
        return {fact_id: self.fact(fact_id) for fact_id in fact_ids}

    def facts(self, kind, scope):
        if kind in {
            materials.MaterialSource.kind,
            materials.MaterialResolution.kind,
            materials.MaterialPeriodAllocation.kind,
            materials.MaterialGroupResolution.kind,
        }:
            return tuple(item for item in self.current_facts(kind) if scope in item.fact.scopes())
        return materials._facts(self.connection, self.registry, kind, scope)

    def current_facts(self, kind):
        rows = self.connection.execute(
            "SELECT c.fact_id FROM fact_current c JOIN subject s ON s.id=c.subject_id "
            "WHERE s.kind=? ORDER BY c.subject_id",
            (kind,),
        ).fetchall()
        return tuple(self.fact(row[0]) for row in rows)


def full_check(company, period):
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return materials.check_completeness(
            connection, YearMonth(period).ordinal, company.engine.store.registry
        )


def full_check_many(company, *periods):
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return materials.check_completeness_many(
            connection,
            (YearMonth(period).ordinal for period in periods),
            company.engine.store.registry,
        )


@pytest.mark.parametrize("case", ("rows", "group", "unknown", "cross_period"))
def test_complete_result_matches_independent_reads_and_versions_load_once(
    tmp_path, monkeypatch, case
):
    company = Company(tmp_path)
    if case in {"group", "unknown"}:
        _, proof, data = pool(company, extra=case == "unknown")
        resolve(company, data, proof)
        period = "2026-02"
    else:
        source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
        link = company.expense("expense", 1000)
        company.resolve(source, "CSV!B2", [link])
        period = "2026-01"
        if case == "cross_period":
            later, _ = company.source(
                b"name,amount,period\nagain,10,2026-01\n", "later", period="2026-02"
            )
            company.resolve(later, "CSV!B2", [link], subject="later-resolution", period="2026-02")

    fact_calls, batch_calls, current_calls = Counter(), Counter(), Counter()
    original_fact, original_facts = Store.fact, Store.facts
    original_current = materials._CompletenessReads.current_facts

    def counted_fact(reader, connection, fact_id):
        fact_calls[fact_id] += 1
        return original_fact(reader, connection, fact_id)

    def counted_facts(reader, connection, fact_ids):
        identifiers = tuple(fact_ids)
        batch_calls.update(identifiers)
        return original_facts(reader, connection, identifiers)

    def counted_current(reader, kind):
        current_calls[kind] += 1
        return original_current(reader, kind)

    with monkeypatch.context() as observe:
        observe.setattr(Store, "fact", counted_fact)
        observe.setattr(Store, "facts", counted_facts)
        observe.setattr(materials._CompletenessReads, "current_facts", counted_current)
        actual = full_check(company, period)

    assert batch_calls
    assert set((fact_calls + batch_calls).values()) == {1}
    assert set(current_calls) == {
        materials.MaterialSource.kind,
        materials.MaterialResolution.kind,
        materials.MaterialPeriodAllocation.kind,
        materials.MaterialGroupResolution.kind,
    }
    assert all(count >= 1 for count in current_calls.values())
    with monkeypatch.context() as independent:
        independent.setattr(materials, "_CompletenessReads", UncachedReads)
        expected = full_check(company, period)
    assert actual == expected  # Includes all rows, issue order, versions and coverage_digest.
    if case == "unknown":
        assert "material_period_unknown" in codes(actual)
    elif case == "cross_period":
        assert "material_business_overallocated" in codes(actual)
    else:
        assert actual["status"] == "complete"


def test_missing_scope_rows_cannot_hide_current_source_or_stale_resolution(tmp_path):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
    link = company.expense("expense", 1000)
    resolution = company.resolve(source, "CSV!B2", [link])
    company.expense("expense", 2000, revision=1)
    damage(
        company.engine,
        "fact_scope",
        "DELETE FROM fact_scope WHERE fact_id IN(?,?)",
        (source["fact_id"], resolution["fact_id"]),
    )
    result = full_check(company, "2026-01")
    assert "material_result_stale" in codes(result)
    assert source["fact_id"] in result["source_versions"]
    assert resolution["fact_id"] in result["resolution_versions"]


def test_shared_amount_basis_is_checked_once_per_exact_source_per_call(tmp_path, monkeypatch):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\n" + b"part,1,2026-01\n" * 12)
    link = company.expense("whole-expense", 1200) | {"amount_fen": 100}
    for position in range(2, 14):
        company.resolve(source, f"CSV!B{position}", [link])
    calls = []
    original = materials._amount_basis

    def counted(fact, calculation, path):
        calls.append((calculation.id, path))
        return original(fact, calculation, path)

    monkeypatch.setattr(materials, "_amount_basis", counted)
    first = full_check(company, "2026-01")
    assert first["status"] == "complete"
    assert len(first["coverage"]) == 12
    assert len(calls) == 1
    assert full_check(company, "2026-01") == first
    assert len(calls) == 2  # A separate read checks the source again.
    company.expense("whole-expense", 1300, revision=1)
    assert "material_result_stale" in codes(full_check(company, "2026-01"))


def test_material_check_shares_fact_decoding_with_other_snapshot_consumers(tmp_path, monkeypatch):
    from ai_accounting.kernel.query_reads import QueryReads

    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
    link = company.expense("expense", 1000)
    company.resolve(source, "CSV!B2", [link])
    expected = full_check(company, "2026-01")
    loaded = Counter()
    original = Store.facts

    def counted(store, connection, identifiers):
        identifiers = tuple(identifiers)
        loaded.update(identifiers)
        return original(store, connection, identifiers)

    monkeypatch.setattr(Store, "facts", counted)
    with QueryReads.snapshot(company.engine) as reads:
        reads.fact_versions([source["fact_id"], link["fact_id"]])
        actual = materials.check_completeness(
            reads.connection, YearMonth("2026-01").ordinal, company.engine.store.registry,
            _query_reads=reads,
        )
        assert actual == expected
        assert set(loaded.values()) == {1}
        assert reads.fact_version(link["fact_id"]).id == link["fact_id"]
        assert set(loaded.values()) == {1}


def test_batch_months_parse_each_cross_period_source_once(tmp_path, monkeypatch):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\njan,10,2026-01\nfeb,20,2026-02\n")
    company.resolve(source, "CSV!B2", [company.expense("jan", 1000)])
    company.resolve(
        source,
        "CSV!B3",
        [company.expense("feb", 2000, "2026-02")],
        subject="resolution-february",
        recognition_period="2026-02",
    )
    original, calls = materials.inspect_bytes, Counter()

    def counted(raw, specification):
        calls[source["subject_id"]] += 1
        return original(raw, specification)

    monkeypatch.setattr(materials, "inspect_bytes", counted)
    results = full_check_many(company, "2026-01", "2026-02")
    assert calls == {source["subject_id"]: 1}
    assert [results[YearMonth(period).ordinal]["status"] for period in ("2026-01", "2026-02")] == [
        "complete",
        "complete",
    ]
    assert [
        results[YearMonth(period).ordinal]["coverage"][0]["responsibility"]
        for period in ("2026-01", "2026-02")
    ] == ["direct", "direct"]


def test_joint_group_batch_reuses_current_pending_and_competitor_reads(tmp_path):
    company = Company(tmp_path)
    _, proof, data = pool(company)
    resolve(company, data, proof)
    statements = []
    with company.engine.store.connection(read_only=True) as connection:
        connection.set_trace_callback(statements.append)
        connection.execute("BEGIN")
        results = materials.check_completeness_many(
            connection,
            (YearMonth("2026-02").ordinal, YearMonth("2026-03").ordinal),
            company.engine.store.registry,
        )
    assert all(result["status"] == "complete" for result in results.values())
    assert sum(" JOIN pending p " in statement for statement in statements) == 1
    assert (
        sum(
            "LEFT JOIN fact_current f ON f.subject_id=ids.value" in statement
            for statement in statements
        )
        == 1
    )
    assert not any(
        "FROM pending WHERE subject_id=" in statement
        or "SELECT fact_id FROM fact_current WHERE subject_id=" in statement
        for statement in statements
    )


def test_later_check_sees_new_resolution_and_changed_business_version(tmp_path):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
    before = full_check(company, "2026-01")
    assert "material_item_unresolved" in codes(before)
    link = company.expense("expense", 1000)
    first = company.resolve(source, "CSV!B2", [link])
    complete = full_check(company, "2026-01")
    assert complete["status"] == "complete"
    assert complete["coverage_digest"] != before["coverage_digest"]

    changed = company.expense("expense", 2000, revision=1)
    assert "material_result_stale" in codes(full_check(company, "2026-01"))
    second = company.resolve(source, "CSV!B2", [changed | {"amount_fen": 1000}], revision=1)
    refreshed = full_check(company, "2026-01")
    assert refreshed["status"] == "complete"
    assert refreshed["resolution_versions"] == [second["fact_id"]]
    assert first["fact_id"] not in refreshed["fact_ids"]
    assert refreshed["coverage_digest"] != complete["coverage_digest"]


def test_failed_batch_load_can_retry_without_poisoning_the_snapshot_cache(tmp_path, monkeypatch):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
    original, calls = Store.facts, []

    def interrupted(reader, connection, fact_ids):
        identifiers = tuple(fact_ids)
        calls.append(identifiers)
        if len(calls) == 1:
            raise RuntimeError("synthetic interrupted read")
        return original(reader, connection, identifiers)

    monkeypatch.setattr(Store, "facts", interrupted)
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        registry = company.engine.store.registry
        reads = materials._CompletenessReads(connection, registry)
        with pytest.raises(RuntimeError, match="synthetic interrupted read"):
            reads.facts(materials.MaterialSource.kind, "2026-01")
        recovered = reads.facts(materials.MaterialSource.kind, "2026-01")
        assert recovered[0].id == source["fact_id"]
        assert reads.facts(materials.MaterialSource.kind, "2026-01") == recovered
        assert len(calls) == 2


def test_readiness_and_duplicate_checks_share_successful_original_parsing(tmp_path, monkeypatch):
    from ai_accounting.kernel.periods import Periods

    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
    company.resolve(source, "CSV!B2", [company.expense("expense", 1000)])
    inspect, calls = materials.inspect_bytes, []

    def count(raw, specification):
        calls.append(raw)
        return inspect(raw, specification)

    monkeypatch.setattr(materials, "inspect_bytes", count)
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        result = Periods(company.engine).collect_current_readiness(connection, "2026-01")
    assert result["materials"]["coverage"]["status"] == "complete"
    assert len(calls) == 1


def test_failed_original_inspection_is_not_reused(tmp_path, monkeypatch):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        version = company.engine.store.fact(connection, source["fact_id"])
        cache = materials._CompletenessInspectionCache(connection)
        inspect, calls = materials.inspect_bytes, []

        def interrupted(raw, specification):
            calls.append(raw)
            if len(calls) == 1:
                raise ValueError("synthetic interrupted inspection")
            return inspect(raw, specification)

        monkeypatch.setattr(materials, "inspect_bytes", interrupted)
        arguments = (
            connection, version.id, version.fact.evidence_digest,
            version.fact.specification, b"name,amount,period\na,10,2026-01\n",
        )
        with pytest.raises(ValueError, match="synthetic interrupted"):
            cache.inspect(*arguments)
        assert cache.inspect(*arguments)["items"]
        assert len(calls) == 2


def test_completed_check_releases_loaded_sources_without_waiting_for_gc(tmp_path, monkeypatch):
    import gc
    import weakref

    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
    company.resolve(source, "CSV!B2", [company.expense("expense", 1000)])
    instances = []
    initialize = materials._CompletenessReads.__init__

    def observed(reader, *args, **kwargs):
        initialize(reader, *args, **kwargs)
        instances.append(weakref.ref(reader))

    monkeypatch.setattr(materials._CompletenessReads, "__init__", observed)
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        result = full_check(company, "2026-01")
        assert result["status"] == "complete"
        assert result["coverage"][0]["complete"] is True
        assert instances and all(reference() is None for reference in instances)
    finally:
        if was_enabled:
            gc.enable()
