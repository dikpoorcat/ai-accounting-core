"""Settlement projection may omit relation ancestry only for proven empty roots."""

import pytest
from test_integrity_content import damage
from test_payroll_corrections import actual
from test_payroll_corrections import company as company  # noqa: F401
from test_settlement_period_scopes import setup

from ai_accounting.kernel import settlement_projection, settlement_projection_v1
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.query_reads import QueryReads


@pytest.mark.parametrize("module", [settlement_projection, settlement_projection_v1])
def test_candidate_gate_retains_malformed_unknown_settlement_and_binding(module):
    known = {"bank_reconciliation", "expense", "payment", "opening_identity_binding"}
    check = module._may_contribute_settlement
    assert not check({"kind": "bank_reconciliation", "outcome": {"values": {}}}, known)
    assert not check({"kind": "expense", "outcome": {"values": {"obligations": []}}}, known)
    assert check({"kind": "expense", "outcome": {"values": {"obligations": [{}]}}}, known)
    assert check({"kind": "expense", "outcome": {"values": {"obligations": None}}}, known)
    assert check({"kind": "expense", "outcome": {"values": {"obligations": {}}}}, known)
    assert check({"kind": "expense", "outcome": {"values": None}}, known)
    assert check({"kind": "unregistered_kind", "outcome": {"values": {}}}, known)
    assert check({"kind": "payment", "outcome": {"values": {}}}, known)
    assert check({"kind": "opening_identity_binding", "outcome": {"values": {}}}, known)


@pytest.mark.parametrize("module", [settlement_projection, settlement_projection_v1])
def test_partial_verified_map_keeps_full_ancestry_path(module):
    class Reads:
        def __init__(self):
            self.called = None

        def prime_raw_calculations(self, ids, *, verified_calculations):
            self.called = set(ids)
            return {ident: {"id": ident} for ident in ids}

    reads = Reads()
    records, filtered = module._projection_calculations(reads, {"one", "two"}, {"one": {}})
    assert reads.called == {"one", "two"}
    assert not filtered
    assert set(records) == {"one", "two"}


def test_missing_noncontributing_root_header_is_not_treated_as_empty(tmp_path):
    company = setup(tmp_path)
    with company.engine.store.connection(read_only=True) as connection:
        ident = connection.execute(
            "SELECT id FROM calculation WHERE kind='cash_funding' LIMIT 1"
        ).fetchone()[0]
    damage(
        company.engine, "calculation", "DELETE FROM calculation WHERE id=?", (ident,),
        foreign_keys=False,
    )
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as failure:
            settlement_projection.expected_settlement_projection(company.engine, connection)
        assert failure.value.code == "content_integrity_failed"


def test_custom_relation_reader_keeps_all_roots(tmp_path, monkeypatch):
    company = setup(tmp_path)
    seen = []

    class CustomReads(QueryReads):
        def relations_many(self, calculations, **kwargs):
            seen.append(set(calculations))
            return super().relations_many(calculations, **kwargs)

    monkeypatch.setattr(settlement_projection, "QueryReads", CustomReads)
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        roots = {
            ident
            for tranche in settlement_projection._active_tranches(connection)
            for ident in (
                tranche.get("calculation_id"), tranche.get("baseline_calculation_id")
            )
            if ident is not None
        }
        settlement_projection.expected_settlement_projection(company.engine, connection)
    assert seen == [roots]


def test_damaged_empty_outcome_still_fails_full_source_verification(tmp_path):
    company = setup(tmp_path)
    with company.engine.store.connection(read_only=True) as connection:
        ident = connection.execute(
            "SELECT id FROM calculation WHERE kind='cash_funding' LIMIT 1"
        ).fetchone()[0]
    damage(
        company.engine,
        "calculation",
        "UPDATE calculation SET outcome=? WHERE id=?",
        ('{"values":{"obligations":null}}', ident),
    )
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError):
            verify_integrity(company.engine, connection)


def test_closed_correction_keeps_baseline_and_projection_seal(company):
    company.publish("january", "february")
    company.close("2026-01")
    company.save(actual(), "actual")
    company.publish("actual", posting_period="2026-03")
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        assert not settlement_projection.compare_settlement_projection(
            company.engine, connection
        )["changed"]
        assert connection.execute(
            "SELECT count(*) FROM calculation_publication WHERE mode='closed_correction'"
        ).fetchone()[0] > 0
