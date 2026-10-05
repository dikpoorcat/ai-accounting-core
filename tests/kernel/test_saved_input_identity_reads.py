"""Bounded saved input identities protect consumed historical owner/source results."""

import json

import pytest
from test_engine import Charge, calculate, publish, save
from test_engine import engine as _engine
from test_integrity_content import damage
from test_settlement_late_reviews import prepared as payroll_company
from test_settlement_late_reviews import review

from ai_accounting.kernel.contracts import KernelError, Read
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import canonical, digest

engine = _engine


def prepared(engine):
    source = save(engine, kind="test_source", subject="source", amount=25, request="source")
    own = save(engine)
    publish(engine)
    with engine.store.connection(read_only=True) as connection:
        row = dict(
            connection.execute(
                "SELECT c.* FROM calculation_current h JOIN calculation c ON c.id=h.calculation_id "
                "WHERE h.subject_id='charge'"
            ).fetchone()
        )
    return row, own["fact_id"], source["fact_id"]


@pytest.mark.parametrize("own_selected", [False, True])
def test_saved_input_identity_accepts_both_actual_writer_variants(
    engine, monkeypatch, own_selected
):
    if own_selected:
        monkeypatch.setattr(
            Charge,
            "reads",
            lambda self: (
                Read("fact", "test_source", str(self.period)),
                Read("fact", "test_charge", str(self.period)),
            ),
        )

        def evaluator(version, context):
            context.facts("test_charge", str(version.fact.period))
            return calculate(version, context)

        monkeypatch.setitem(engine.store.registry.evaluators, "test_charge", evaluator)
    row, _, _ = prepared(engine)
    with QueryReads.snapshot(engine) as reads:
        reads.verify_selected_content((row["id"],))
        statements = []
        reads.connection.set_trace_callback(statements.append)
        result = reads.verify_saved_input_identity((row["id"],))
        assert result[row["id"]]["values"]["amount"] == 125
        assert len(statements) == 4
        assert all("outcome" not in sql for sql in statements)
        statements.clear()
        assert reads.verify_saved_input_identity((row["id"],)) == result
        assert statements == []


@pytest.mark.parametrize("change", ["body_and_digest", "scope", "version", "own_fact", "program"])
def test_saved_input_identity_rejects_changed_content_or_saved_reads(engine, change):
    row, own_fact, source_fact = prepared(engine)
    ident = row["id"]
    if change == "body_and_digest":
        outcome = json.loads(row["outcome"])
        outcome["values"]["amount"] = 999
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
            (canonical(outcome), digest(outcome), ident),
        )
    elif change == "scope":
        damage(
            engine,
            "dependency_scope",
            "DELETE FROM dependency_scope WHERE calculation_id=?",
            (ident,),
        )
    elif change in {"version", "own_fact"}:
        damage(
            engine,
            "dependency_fact",
            "DELETE FROM dependency_fact WHERE calculation_id=? AND fact_id=?",
            (ident, source_fact if change == "version" else own_fact),
        )
    else:
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET program_version='other' WHERE id=?",
            (ident,),
        )
    with QueryReads.snapshot(engine) as reads:
        # Body+self digest damage is valid saved JSON but lacks its c_ID seal.
        reads.verify_selected_content((ident,))
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.verify_saved_input_identity((ident,))
            assert failure.value.code == "content_integrity_failed"
            assert failure.value.details["reason"] == (
                "own_fact_dependency_missing"
                if change == "own_fact"
                else "calculation_input_digest_mismatch"
            )
            assert reads._verified_saved_input_identities == set()


def test_saved_input_identity_preserves_noncanonical_json_and_transaction_boundary(engine):
    row, _, _ = prepared(engine)
    ident = row["id"]
    noncanonical = json.dumps(json.loads(row["outcome"]), indent=2)
    damage(
        engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?", (noncanonical, ident)
    )
    with QueryReads.snapshot(engine) as reads:
        assert reads.verify_saved_input_identity((ident,))[ident] == json.loads(noncanonical)
    assert reads._verified_saved_input_identities == set()
    damage(
        engine, "dependency_scope", "DELETE FROM dependency_scope WHERE calculation_id=?", (ident,)
    )
    with QueryReads.snapshot(engine) as fresh:
        with pytest.raises(KernelError) as failure:
            fresh.verify_saved_input_identity((ident,))
        assert failure.value.details["reason"] == "calculation_input_digest_mismatch"


def test_failed_input_identity_batch_does_not_cache_successful_prefix(engine):
    good, _, _ = prepared(engine)
    save(engine, subject="other", amount=150, request="other")
    publish(engine, ["other"], request="publish-other")
    with engine.store.connection(read_only=True) as connection:
        bad = dict(
            connection.execute(
                "SELECT c.* FROM calculation_current h JOIN calculation c ON c.id=h.calculation_id "
                "WHERE h.subject_id='other'"
            ).fetchone()
        )
    body = json.loads(bad["outcome"])
    body["values"]["amount"] = 999
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
        (canonical(body), digest(body), bad["id"]),
    )
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            reads.verify_saved_input_identity((good["id"], bad["id"]))
        assert reads._verified_saved_input_identities == set()
        assert (
            reads.verify_saved_input_identity((good["id"],))[good["id"]]["values"]["amount"] == 125
        )


def test_old_voucher_owner_survives_real_no_impact_review(tmp_path, monkeypatch):
    company = payroll_company(tmp_path)
    with company.engine.store.connection(read_only=True) as connection:
        old_owner = connection.execute(
            "SELECT v.calculation_id FROM voucher_current h JOIN voucher_version v "
            "ON v.id=h.version_id JOIN calculation c ON c.id=v.calculation_id "
            "WHERE c.subject_id='january'"
        ).fetchone()[0]
    reviewed = review(company, monkeypatch, 1)
    assert reviewed != old_owner
    with QueryReads.snapshot(company.engine) as reads:
        statements = []
        reads.connection.set_trace_callback(statements.append)
        results = reads.verify_saved_input_identity((old_owner, reviewed))
        assert set(results) == {old_owner, reviewed}
        assert results[old_owner]["values"] == results[reviewed]["values"]
        # Dependency references do not request ancestor result payloads.
        assert not any("WITH RECURSIVE" in sql.upper() for sql in statements)


def test_small_input_identity_lookup_does_not_enumerate_warm_content_cache(engine):
    row, _, _ = prepared(engine)
    ident = row["id"]

    class ExactMembership(dict):
        def keys(self):
            raise AssertionError("selected content enumerated unrelated warm proofs")

    with QueryReads.snapshot(engine) as reads:
        reads.verify_selected_content((ident,))
        reads._verified_source_contents = ExactMembership(
            reads._verified_source_contents,
            **{f"unrelated-{index}": {} for index in range(12000)},
        )
        assert reads.verify_saved_input_identity((ident,))[ident]["values"]["amount"] == 125


@pytest.mark.parametrize("change", ["ancestor_body", "missing_dependency"])
def test_saved_calculation_dependency_uses_version_id_without_ancestor_body(
    engine, monkeypatch, change
):
    upstream, _, _ = prepared(engine)
    key = "#" + upstream["id"]
    monkeypatch.setattr(
        Charge,
        "reads",
        lambda self: (
            Read("fact", "test_source", str(self.period)),
            Read("calculation", "test_charge", key),
        ),
    )

    def evaluator(version, context):
        context.calculations("test_charge", key)
        return calculate(version, context)

    monkeypatch.setitem(engine.store.registry.evaluators, "test_charge", evaluator)
    save(engine, subject="downstream", request="downstream")
    publish(engine, ["downstream"], request="publish-downstream")
    with engine.store.connection(read_only=True) as connection:
        ident = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='downstream'"
        ).fetchone()[0]
    if change == "ancestor_body":
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=? WHERE id=?",
            (canonical({"lines": [], "values": {"amount": 0}}), upstream["id"]),
        )
        with QueryReads.snapshot(engine) as reads:
            # This is a requested-ID identity proof, not complete source-graph
            # verification; the ancestor body is deliberately not consumed.
            assert reads.verify_saved_input_identity((ident,))[ident]["values"]["amount"] == 125
            with pytest.raises(KernelError):
                reads.verify_saved_input_identity((upstream["id"],))
    else:
        damage(
            engine,
            "dependency_calculation",
            "DELETE FROM dependency_calculation WHERE calculation_id=?",
            (ident,),
        )
        with QueryReads.snapshot(engine) as reads:
            with pytest.raises(KernelError) as failure:
                reads.verify_saved_input_identity((ident,))
            assert failure.value.details["reason"] == "calculation_input_digest_mismatch"
