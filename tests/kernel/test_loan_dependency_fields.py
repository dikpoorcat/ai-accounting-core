"""Loan interest derives its contract from the verified principal source."""

import pytest
from pydantic import ValidationError
from test_banking import book as book

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.assets import LoanInterest


def principal(book):
    engine, save, publish, _ = book
    agreement = {
        "period": "2026-09", "lender_id": "lender", "lender_is_licensed": True,
        "currency": "CNY", "annual_rate_percent": "3.65", "day_count_basis": "actual_365",
        "maturity_date": "2027-01-01", "loan_term": "short_term",
    }
    save("loan_agreement", "agreement", agreement)
    save("loan_drawdown", "drawdown", {
        "period": "2026-09", "agreement_id": "agreement", "principal_fen": 1000000,
        "actual_date": "2026-09-01", "bank_account_id": "bank",
    })
    publish("drawdown")
    interest = {
        "period": "2026-09", "drawdown_id": "drawdown",
        "period_start": "2026-09-01", "period_end_exclusive": "2026-10-01",
    }
    save("loan_interest", "interest", interest)
    return agreement, interest


def test_contract_is_read_once_from_principal_and_is_a_versioned_dependency(book, monkeypatch):
    engine, save, publish, _ = book
    agreement, interest = principal(book)
    with pytest.raises(ValidationError):
        LoanInterest.model_validate(interest | {"agreement_id": "agreement"})
    requested = []
    original = engine.store.select_many
    def watched(connection, reads, **kwargs):
        reads = tuple(reads)
        requested.extend(reads)
        return original(connection, reads, **kwargs)
    monkeypatch.setattr(engine.store, "select_many", watched)
    preview = engine.preview(["interest"])
    contracts = [read for read in requested if read.kind == "loan_agreement"]
    assert len(contracts) == 1 and contracts[0].key == "@agreement"
    save("loan_agreement", "agreement", agreement | {"annual_rate_percent": "7.30"}, revision=1)
    with pytest.raises(KernelError) as stale:
        engine.confirm(["interest"], preview_digest=preview["digest"],
                       epochs=preview["epochs"], request_id="stale-interest")
    assert stale.value.code in {"preview_expired", "pending_upstream"}
    publish("drawdown", "interest")
    with engine.store.connection(read_only=True) as connection:
        assert {row[0] for row in connection.execute(
            "SELECT f.subject_id FROM dependency_fact d JOIN fact_revision f ON f.id=d.fact_id "
            "JOIN calculation_current c ON c.calculation_id=d.calculation_id "
            "WHERE c.subject_id='interest'"
        )} >= {"agreement", "drawdown"}
        assert "agreement_id" not in {row["name"] for row in connection.execute(
            "PRAGMA table_info(fact_loan_interest)"
        )}
    save("loan_agreement", "agreement", agreement, revision=2)
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT 1 FROM pending WHERE subject_id='interest'").fetchone()


def test_second_stage_publish_failure_is_atomic(book):
    engine, _, _, _ = book
    principal(book)
    preview = engine.preview(["interest"])
    def fail(stage, connection):
        if stage == "published":
            raise RuntimeError("synthetic publication failure")
    engine.fault = fail
    with pytest.raises(RuntimeError, match="synthetic publication"):
        engine.confirm(["interest"], preview_digest=preview["digest"], epochs=preview["epochs"],
                       request_id="atomic-interest")
    with engine.store.connection(read_only=True) as connection:
        assert not connection.execute(
            "SELECT 1 FROM calculation_current WHERE subject_id='interest'"
        ).fetchone()
        assert not connection.execute(
            "SELECT 1 FROM calculation WHERE subject_id='interest'"
        ).fetchone()
