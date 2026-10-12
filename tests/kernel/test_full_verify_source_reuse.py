"""Full verification shares completed raw proof, never an adoption shortcut."""

import sqlite3
from collections import Counter
from types import SimpleNamespace

import pytest
import test_payroll
from pydantic import ValidationError, field_validator
from test_integrity_content import damage
from test_payroll_corrections import Company
from test_reports import book as reports_book

from ai_accounting.kernel import (
    close_review,
    close_storage,
    dashboard_model_integrity,
    integrity,
    stored_json,
)
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.verified_source_lease import (
    verified_calculation_source,
    verified_source_lease,
)
from ai_accounting.kernel.verified_source_reads import (
    VerifiedSourceReads,
    readonly_verification,
)


@pytest.fixture
def book(tmp_path):
    return reports_book.__wrapped__(tmp_path)


def _verify(engine):
    with engine.store.connection(read_only=True) as connection:
        return integrity.verify_integrity(engine, connection)


def _closed_expense(book):
    engine, save, publish, close = book
    save("cash_funding", "capital", {
        "period": "2026-01", "actual_date": "2026-01-01", "owner_id": "owner",
        "funding_kind": "capital", "amount_fen": 1000, "cash_account_id": "cash",
    })
    save("expense", "cost", {
        "period": "2026-01", "counterparty_id": "supplier", "amount_fen": 100,
        "expense_class": "administration", "creditor_kind": "supplier",
    })
    publish("capital", "cost")
    close("2026-01")
    return engine


@pytest.mark.parametrize("months", [1, 2, 4])
def test_full_verify_decodes_each_source_once_across_monthly_payroll(
    tmp_path, monkeypatch, months,
):
    company = Company(tmp_path / "wages.sqlite")
    for fact, subject in (
        (test_payroll.profile(effective_to="2026-12"), "profile"),
        (test_payroll.contribution_policy(), "contributions"),
    ):
        company.save(fact, subject)
    for number in range(1, months + 1):
        period, subject = f"2026-{number:02}", f"wage-{number}"
        company.save(test_payroll.payroll(period=period), subject)
        company.tax_basis(subject, 25_200)
        company.confirm_payroll(subject)
        company.publish(subject)
        company.close(period)

    decoded, facts = Counter(), Counter()
    model_work = []
    original_object = integrity._object
    original_facts = Store._fact_data_many_from_headers
    original_outcome = QueryReads._stored_outcome
    original_checked_outcome = stored_json.verify_outcome_bytes
    original_models = dashboard_model_integrity.verify_models

    def models(*args, **kwargs):
        model_work.append(True)
        try:
            return original_models(*args, **kwargs)
        finally:
            model_work.pop()

    def counted_object(raw, component, ident):
        if component == "calculation" and not model_work:
            decoded[ident] += 1
        return original_object(raw, component, ident)

    def counted_facts(self, connection, rows):
        if not model_work:
            for row in rows:
                facts[row["id"]] += 1
        return original_facts(self, connection, rows)

    def duplicate_result(raw):
        if not model_work:
            pytest.fail("completed source outcome was decoded again")
        return original_outcome(raw)

    def duplicate_checked_result(*args, **kwargs):
        if not model_work:
            pytest.fail("completed source outcome was checked again")
        return original_checked_outcome(*args, **kwargs)

    monkeypatch.setattr(integrity, "_object", counted_object)
    monkeypatch.setattr(Store, "_fact_data_many_from_headers", counted_facts)
    monkeypatch.setattr(QueryReads, "_stored_outcome", staticmethod(duplicate_result))
    monkeypatch.setattr(stored_json, "verify_outcome_bytes", duplicate_checked_result)
    monkeypatch.setattr(dashboard_model_integrity, "verify_models", models)
    for _request in range(2):
        decoded.clear()
        facts.clear()
        result = _verify(company.engine)
        assert result["status"] == "verified"
        assert len(decoded) == result["counts"]["calculations"] == months + 1
        assert set(decoded.values()) == {1}
        assert len(facts) == result["counts"]["facts"]
        assert set(facts.values()) == {1}


def test_owner_review_reuse_matches_complete_native_review(book):
    engine = _closed_expense(book)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT * FROM period_close").fetchone()
        manifest = close_storage.decode_close(connection, row)
        native = close_review.build_owner_review(connection, engine, manifest)
        with readonly_verification(connection), verified_source_lease(connection):
            source = integrity._check_sources(engine, connection)
            reads = VerifiedSourceReads(
                engine, connection, verified_calculation_source(connection, source),
            )
            reused = close_review.build_owner_review(
                connection, engine, manifest, _source_reads=reads,
            )
            assert reused == native == manifest["owner_review"]
            assert reads._snapshot_active is False


@pytest.mark.parametrize("kind", ["fact", "outcome", "frozen", "adoption"])
def test_full_verify_rejects_damage_before_or_after_source_proof(book, monkeypatch, kind):
    engine = _closed_expense(book)
    minted = []
    original = VerifiedSourceReads.__init__

    def observed(self, *args, **kwargs):
        minted.append(True)
        original(self, *args, **kwargs)

    monkeypatch.setattr(VerifiedSourceReads, "__init__", observed)
    if kind == "fact":
        damage(engine, "fact_expense", "UPDATE fact_expense SET amount_fen=amount_fen+1")
    elif kind == "outcome":
        damage(
            engine, "calculation", "UPDATE calculation SET outcome=?||substr(outcome,2)",
            ('{"values":{},',),
        )
    elif kind == "frozen":
        damage(engine, "close_storage_block", "UPDATE close_storage_block SET content=content||' '")
    else:
        damage(engine, "close_reference", "DELETE FROM close_reference WHERE rowid IN "
               "(SELECT rowid FROM close_reference LIMIT 1)")
    with pytest.raises(KernelError):
        _verify(engine)
    assert bool(minted) is (kind in {"frozen", "adoption"})


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_source_reader_rejects_transaction_replacement_and_retained_cache(book, finish):
    engine = _closed_expense(book)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with readonly_verification(connection), verified_source_lease(connection):
            source = integrity._check_sources(engine, connection)
            reads = VerifiedSourceReads(
                engine, connection, verified_calculation_source(connection, source),
            )
            ident = next(iter(source["calculations"]))
            reads.calculation(ident)
            getattr(connection, finish)()
            connection.execute("BEGIN")
            with pytest.raises(ValueError, match="transaction has ended"):
                reads.calculation(ident)


def test_writer_full_verify_blocks_writes_and_restores_caller_transaction(book, monkeypatch):
    engine = _closed_expense(book)
    original = integrity._check_sources

    def checked(engine, connection, *args, **kwargs):
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE monthly_account SET debit=debit+1")
        return original(engine, connection, *args, **kwargs)

    monkeypatch.setattr(integrity, "_check_sources", checked)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SAVEPOINT caller")
        assert integrity.verify_integrity(engine, connection)["status"] == "verified"
        assert connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        connection.execute("RELEASE SAVEPOINT caller")
        connection.execute("UPDATE monthly_account SET debit=debit+1")
        connection.rollback()
    assert _verify(engine)["status"] == "verified"


def test_writer_failure_restores_protection_and_rolls_back_without_source_reuse(book, monkeypatch):
    engine = _closed_expense(book)

    def failed(*_args, **_kwargs):
        raise KernelError("content_integrity_failed", "synthetic failed source")

    monkeypatch.setattr(integrity, "_check_sources", failed)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute("SELECT sum(debit) FROM monthly_account").fetchone()[0]
        connection.execute("UPDATE monthly_account SET debit=debit+1")
        with pytest.raises(KernelError):
            integrity.verify_integrity(engine, connection)
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute("SELECT sum(debit) FROM monthly_account").fetchone()[0] == before


def test_source_reader_rejects_writes_even_if_query_only_was_reenabled(book):
    engine = _closed_expense(book)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        with readonly_verification(connection), verified_source_lease(connection):
            source = integrity._check_sources(engine, connection)
            reads = VerifiedSourceReads(
                engine, connection, verified_calculation_source(connection, source),
            )
            ident = next(iter(source["calculations"]))
            reads.calculation(ident)
            connection.execute("PRAGMA query_only=OFF")
            connection.execute("UPDATE monthly_account SET debit=debit+1")
            connection.execute("PRAGMA query_only=ON")
            with pytest.raises(KernelError, match="只读事务"):
                reads.calculation(ident)
        connection.rollback()


def test_current_source_reader_cannot_interpret_fixed_v1():
    engine = SimpleNamespace(store=SimpleNamespace(registry=SimpleNamespace(content_version=1)))
    with pytest.raises(ValueError, match="released v1"):
        VerifiedSourceReads(engine, None, None)


def test_source_reuse_keeps_native_typed_fact_presentation_validation(book, monkeypatch):
    engine = _closed_expense(book)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with verified_source_lease(connection):
            source = integrity._check_sources(engine, connection)
            reads = VerifiedSourceReads(
                engine, connection, verified_calculation_source(connection, source),
            )
            fact_id = next(ident for ident, row in source["facts"].items()
                           if row["kind"] == "expense")
            original = engine.store.registry.models["expense"]

            class ChangedPresentation(original):
                @field_validator("amount_fen")
                @classmethod
                def refuse_current_presentation(cls, value):
                    raise ValueError("synthetic changed presentation validation")

            monkeypatch.setitem(engine.store.registry.models, "expense", ChangedPresentation)
            # The exact body descriptor owns typed presentation, independently
            # of the public input model lookup.
            table = engine.store.registry.current_table("expense")
            monkeypatch.setitem(
                engine.store.registry.body_models, "expense", ((ChangedPresentation, table),)
            )
            # Raw authority has succeeded. Both typed consumers still apply
            # their original model validation rather than accepting raw data.
            for selected in (QueryReads(engine, connection), reads):
                with pytest.raises(ValidationError, match="changed presentation"):
                    selected.facts((fact_id,))


def test_readonly_verification_preserves_existing_authorizer():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE sample(value INTEGER)")
    connection.commit()
    observed = []

    def authorize(action, *_args):
        observed.append(action)
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DELETE else sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        connection.execute("BEGIN")
        with readonly_verification(connection):
            assert connection.execute("SELECT count(*) FROM sample").fetchone()[0] == 0
        with pytest.raises(sqlite3.DatabaseError, match="authorized"):
            connection.execute("DELETE FROM sample")
        assert sqlite3.SQLITE_DELETE in observed
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
    finally:
        connection.close()


def test_readonly_verification_closes_connection_when_restoration_fails():
    class FailedRestore:
        query_only = 0
        closed = False

        def execute(self, sql):
            if sql == "PRAGMA query_only":
                return SimpleNamespace(fetchone=lambda: (self.query_only,))
            if sql == "PRAGMA query_only=ON":
                self.query_only = 1
                return
            raise sqlite3.OperationalError("synthetic protection restoration failure")

        def close(self):
            self.closed = True

    connection = FailedRestore()
    with pytest.raises(sqlite3.OperationalError, match="restoration"):
        with readonly_verification(connection):
            assert connection.query_only == 1
    assert connection.closed
