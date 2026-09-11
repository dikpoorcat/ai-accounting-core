"""Deletion follows publication history and current, including unpublished, dependencies."""

import itertools

import pytest
from material_fixture import supporting_text

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth, canonical


@pytest.fixture
def book(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite", default_registry(), "company", "911100000000000001", "db"
        )
    )
    proof = engine.register_evidence(
        b"Synthetic source and recording-error evidence", "text/plain", "proof", request_id="proof"
    )["digest"]
    supporting_text(engine, proof)
    counter = itertools.count()

    def save(kind, subject, data, revision=0, *, amend=False):
        args = {
            "evidence": (proof,),
            "expected_revision": revision,
            "request_id": f"save-{next(counter)}",
        }
        if amend:
            return engine.amend_fact(kind, subject, data, recording_error_confirmed=True, **args)
        return engine.save_fact(kind, subject, data, **args)

    def publish(*subjects, correction_period=None):
        plan = engine.preview(list(subjects), correction_period=correction_period)
        return engine.confirm(
            list(subjects),
            preview_digest=plan["digest"],
            epochs=plan["epochs"],
            request_id=f"publish-{next(counter)}",
            correction_period=correction_period,
        )

    def close(period):
        periods = Periods(engine)
        with engine.store.connection(read_only=True) as connection:
            kinds = {
                r[0]
                for r in connection.execute(
                    "SELECT s.kind FROM subject s JOIN fact_current a ON a.subject_id=s.id "
                    "JOIN fact_revision f ON f.id=a.fact_id WHERE f.period=?",
                    (YearMonth(period).ordinal,),
                )
            }
        categories = {
            engine.store.registry.models[k].material_category
            for k in kinds
            if k in engine.store.registry.evaluators
        }
        for category in MATERIAL_CATEGORIES:
            busy = category in categories
            periods.inventory(
                period,
                category,
                evidence=[proof] if busy else [],
                expected=int(busy),
                no_business=not busy,
                confirmation_evidence=proof,
                request_id=f"inventory-{next(counter)}",
            )
        plan = periods.preview_close(period, owner_confirmation=proof)
        return periods.close(
            period,
            owner_confirmation=proof,
            preview_digest=plan["digest"],
            epochs=plan["epochs"],
            request_id=f"close-{next(counter)}",
        )

    def withdraw(subject, *, request_id=None):
        plan = engine.preview_delete(subject, recording_error_evidence=proof)
        args = {
            "preview_digest": plan["digest"],
            "epochs": plan["epochs"],
            "recording_error_evidence": proof,
            "request_id": request_id or f"withdraw-{next(counter)}",
        }
        return engine.delete(subject, **args), args

    return engine, save, publish, close, withdraw, proof


def expense(period="2026-01", amount=100):
    return {
        "period": period,
        "counterparty_id": "supplier",
        "amount_fen": amount,
        "expense_class": "administration",
        "creditor_kind": "supplier",
    }


def payment(source="expense"):
    return {
        "period": "2026-01",
        "actual_date": "2026-01-10",
        "cash_account_id": "cash",
        "counterparty_id": "supplier",
        "direction": "outflow",
        "amount_fen": 100,
        "allocations": [
            {
                "source_kind": "expense",
                "source_id": source,
                "obligation": "primary",
                "amount_fen": 100,
            }
        ],
    }


def prepare_payment(book):
    _engine, save, publish, *_ = book
    save(
        "cash_funding",
        "capital",
        {
            "period": "2026-01",
            "actual_date": "2026-01-01",
            "cash_account_id": "cash",
            "owner_id": "owner",
            "amount_fen": 500,
            "funding_kind": "capital",
        },
    )
    save("expense", "expense", expense())
    publish("capital", "expense")
    save("cash_payment", "payment", payment())


def test_closed_original_cannot_be_deleted_after_open_compensation_and_fact_period_move(book):
    engine, save, publish, close, _withdraw, proof = book
    save("expense", "expense", expense())
    publish("expense")
    close("2026-01")
    frozen = Periods(engine).closed_report("2026-01")
    save("expense", "expense", expense(amount=200), revision=1)
    publish("expense", correction_period="2026-02")
    save("expense", "expense", expense(period="2026-03", amount=200), revision=2, amend=True)
    with pytest.raises(KernelError) as failure:
        engine.preview_delete("expense", recording_error_evidence=proof)
    assert failure.value.code == "closed_period"
    assert Periods(engine).closed_report("2026-01") == frozen
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher_current").fetchone()[0] == 3


def test_closed_zero_result_is_protected_by_publication_period_without_any_voucher(book):
    engine, save, publish, close, _withdraw, proof = book
    fact = {
        "period": "2026-01",
        "year": 2026,
        "assessment_basis": "confirmed_provision",
        "cumulative_assessed_fen": 0,
    }
    save("income_tax_assessment", "zero", fact)
    publish("zero")
    close("2026-01")
    save("income_tax_assessment", "zero", fact | {"period": "2026-02"}, revision=1, amend=True)
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher_version").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM calculation_publication").fetchone()[0] == 1
    with pytest.raises(KernelError) as failure:
        engine.preview_delete("zero", recording_error_evidence=proof)
    assert failure.value.code == "closed_period"


def test_confirmed_unpublished_actual_payment_blocks_upstream_deletion(book):
    engine, _save, _publish, _close, _withdraw, proof = book
    prepare_payment(book)
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM calculation_current WHERE subject_id='payment'"
            ).fetchone()
            is None
        )
    with pytest.raises(KernelError) as failure:
        engine.preview_delete("expense", recording_error_evidence=proof)
    assert failure.value.code == "has_dependents"
    assert failure.value.details["subjects"] == ["payment"]


def test_old_payment_dependency_stops_blocking_after_current_payment_is_relinked(book):
    engine, save, publish, _close, withdraw, _proof = book
    prepare_payment(book)
    publish("payment")
    save("expense", "replacement", expense())
    publish("replacement")
    save("cash_payment", "payment", payment("replacement"), revision=1, amend=True)
    publish("payment")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM dependency_calculation d JOIN calculation c "
                "ON c.id=d.upstream_id WHERE c.subject_id='expense'"
            ).fetchone()[0]
            > 0
        )
    assert withdraw("expense")[0]["status"] == "withdrawn"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT 1 FROM fact_current WHERE subject_id='payment'").fetchone()
            is not None
        )


def test_withdrawn_unpublished_payment_leaves_no_permanent_fact_read_blocker(book):
    _engine, _save, _publish, _close, withdraw, _proof = book
    prepare_payment(book)
    assert withdraw("payment")[0]["status"] == "withdrawn"
    assert withdraw("expense")[0]["status"] == "withdrawn"


def test_open_business_without_dependents_deletes_idempotently_and_retains_history(book):
    engine, save, publish, _close, withdraw, _proof = book
    original = save("expense", "expense", expense())
    publish("expense")
    result, args = withdraw("expense", request_id="withdraw-once")
    assert engine.delete("expense", **args) == result
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.fact(connection, original["fact_id"]).fact.amount_fen == 100
        assert (
            connection.execute(
                "SELECT count(*) FROM fact_current c JOIN subject s ON s.id=c.subject_id "
                "WHERE s.kind IN (SELECT value FROM json_each(?))",
                (canonical([
                    kind for kind, model in engine.store.registry.models.items()
                    if model.lane == "accounting"
                ]),),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM calculation_current").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM voucher_current").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM voucher_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM disposition WHERE action='withdrawn'"
            ).fetchone()[0]
            == 1
        )


def test_equal_outcome_review_can_delete_reused_open_voucher_via_publication(book):
    engine, save, publish, _close, withdraw, _proof = book
    save("expense", "expense", expense())
    publish("expense")
    save("expense", "expense", expense(), revision=1, amend=True)
    publish("expense")
    assert withdraw("expense")[0]["status"] == "withdrawn"
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher_current").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM monthly_account").fetchone()[0] == 0
