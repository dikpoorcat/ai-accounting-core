"""Brief page reuses only proofs from its own live read transaction."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_banking import book as book_fixture
from test_banking import funding
from test_integrity_content import damage
from test_payroll_preparation import company as payroll_company

from ai_accounting.kernel import close_review
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads

book = book_fixture


def _funded_book(book):
    engine, save, publish, _proof = book
    funding(save, publish)
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT c.id,c.fact_id FROM calculation c WHERE c.subject_id='funding'"
        ).fetchone()
    return engine, row["id"], row["fact_id"]


def test_brief_adopted_basis_keeps_response_and_avoids_second_fact_load(book, monkeypatch):
    engine, calculation_id, fact_id = _funded_book(book)
    original = close_review.business_adopted_basis

    def unshared(connection, selected_engine, identifiers, *, reads=None):
        return original(connection, selected_engine, identifiers)

    with monkeypatch.context() as old:
        old.setattr(close_review, "business_adopted_basis", unshared)
        previous = Dashboard(engine).brief("2026-09", preparation="deferred")
    current = Dashboard(engine).brief("2026-09", preparation="deferred")
    previous["data"].pop("generated_at")
    current["data"].pop("generated_at")
    assert current == previous

    with QueryReads.snapshot(engine) as reads:
        reads.facts((fact_id,))
        statements = []
        reads.connection.set_trace_callback(statements.append)
        separate = original(reads.connection, engine, (calculation_id,))
        separate_sql = tuple(statements)
        statements.clear()
        shared = original(reads.connection, engine, (calculation_id,), reads=reads)
        shared_sql = tuple(statements)
        reads.connection.set_trace_callback(None)
        assert shared == separate
        assert len(shared_sql) < len(separate_sql)
        assert any("fact_funding" in sql for sql in separate_sql)
        assert not any("fact_funding" in sql for sql in shared_sql)
        with QueryReads.snapshot(engine) as other:
            with pytest.raises(ValueError, match="this engine and read snapshot"):
                original(reads.connection, engine, (calculation_id,), reads=other)


def test_brief_adopted_basis_damaged_fact_cannot_be_cached_as_verified(book):
    engine, calculation_id, fact_id = _funded_book(book)
    damage(
        engine,
        "fact_funding",
        "UPDATE fact_funding SET funding_kind='invalid' WHERE revision_id=?",
        (fact_id,),
    )
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                close_review.business_adopted_basis(
                    reads.connection, engine, (calculation_id,), reads=reads
                )
            assert failure.value.code == "content_integrity_failed"
            assert failure.value.response()["status"] == "rejected"
            assert "fact_issues" not in failure.value.response()
            assert fact_id not in reads._fact_versions


def test_brief_adopted_basis_reads_stored_outcome_not_mutable_decoded_value(book, monkeypatch):
    engine, calculation_id, _fact_id = _funded_book(book)
    with QueryReads.snapshot(engine) as reads:
        reads.prime_calculations((calculation_id,))
        reads.verify_selected_content((calculation_id,))
        # The decoded calculation is an ordinary mutable caller-facing value;
        # it must never replace the stored adoption source.
        reads.calculation(calculation_id)["outcome"] = {"values": {"payroll_confirmation": {}}}
        statements = []
        decoded = []
        monkeypatch.setattr(
            close_review,
            "json",
            SimpleNamespace(loads=lambda raw: decoded.append(raw) or json.loads(raw)),
        )
        reads.connection.set_trace_callback(statements.append)
        shared = close_review.business_adopted_basis(
            reads.connection, engine, (calculation_id,), reads=reads
        )
        reads.connection.set_trace_callback(None)
        assert decoded == []
        assert any(
            "CASE WHEN kind IN" in sql and "THEN outcome ELSE NULL" in sql for sql in statements
        )
        assert not any("SELECT id,fact_id,kind,outcome,digest" in sql for sql in statements)
        assert shared == close_review.business_adopted_basis(
            reads.connection, engine, (calculation_id,)
        )


def test_brief_adopted_basis_rejects_unreadable_payroll_result_body(tmp_path):
    engine = payroll_company(tmp_path).engine
    with engine.store.connection(read_only=True) as connection:
        calculation_id = connection.execute(
            "SELECT id FROM calculation WHERE kind='payroll' LIMIT 1"
        ).fetchone()[0]
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET outcome='[]' WHERE id=?",
        (calculation_id,),
    )
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                close_review.business_adopted_basis(
                    reads.connection, engine, (calculation_id,), reads=reads
                )
            assert failure.value.code == "content_integrity_failed"
            assert failure.value.response()["status"] == "rejected"
            assert "fact_issues" not in failure.value.response()
            assert calculation_id not in reads._verified_source_contents


def test_brief_adopted_basis_keeps_exact_payroll_confirmation(tmp_path):
    instance = payroll_company(tmp_path)
    engine = instance.engine
    with QueryReads.snapshot(engine) as reads:
        calculation_id = reads.connection.execute(
            "SELECT id FROM calculation WHERE kind='payroll' LIMIT 1"
        ).fetchone()[0]
        reads.prime_calculations((calculation_id,))
        reads.verify_selected_content((calculation_id,))
        shared = close_review.business_adopted_basis(
            reads.connection, engine, (calculation_id,), reads=reads
        )
        separate = close_review.business_adopted_basis(reads.connection, engine, (calculation_id,))
        assert shared == separate
        assert len(shared["payroll_confirmations"]) == 1
