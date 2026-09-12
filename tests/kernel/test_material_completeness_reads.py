"""One material check reuses reads without changing coverage or discovery."""

from collections import Counter, defaultdict

import pytest
from test_material_joint_groups import pool, resolve
from test_materials import Company, codes

from ai_accounting.kernel import materials
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth


class UncachedReads:
    """Use the existing independent helpers, without duplicating their SQL."""

    def __init__(self, connection, registry):
        self.connection, self.registry = connection, registry

    def fact(self, fact_id):
        return Store.fact(self, self.connection, fact_id)

    def facts(self, kind, scope):
        return materials._facts(self.connection, self.registry, kind, scope)


def full_check(company, period):
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return materials.check_completeness(
            connection, YearMonth(period).ordinal, company.engine.store.registry
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

    fact_calls, scope_calls, scopes_by_version = Counter(), Counter(), defaultdict(set)
    original_fact, original_facts = Store.fact, materials._facts

    def counted_fact(reader, connection, fact_id):
        fact_calls[fact_id] += 1
        return original_fact(reader, connection, fact_id)

    def counted_facts(connection, registry, kind, scope, **kwargs):
        scope_calls[kind, scope] += 1
        versions = original_facts(connection, registry, kind, scope, **kwargs)
        for version in versions:
            scopes_by_version[version.id].add((kind, scope))
        return versions

    with monkeypatch.context() as observe:
        observe.setattr(Store, "fact", counted_fact)
        observe.setattr(materials, "_facts", counted_facts)
        actual = full_check(company, period)

    # The same saved version is discovered through distinct scope queries.
    assert any(len(scopes) > 1 for scopes in scopes_by_version.values())
    assert fact_calls and set(fact_calls.values()) == {1}
    assert scope_calls and set(scope_calls.values()) == {1}
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


def test_failed_fact_load_can_retry_and_independent_helper_stays_uncached(tmp_path, monkeypatch):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
    original, calls = Store.fact, []

    def interrupted(reader, connection, fact_id):
        calls.append(fact_id)
        if len(calls) == 1:
            raise RuntimeError("synthetic interrupted read")
        return original(reader, connection, fact_id)

    monkeypatch.setattr(Store, "fact", interrupted)
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
        for _ in range(2):
            assert (
                materials._facts(connection, registry, materials.MaterialSource.kind, "2026-01")
                == recovered
            )
        assert len(calls) == 4
