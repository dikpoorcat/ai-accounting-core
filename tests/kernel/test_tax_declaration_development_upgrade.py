"""Actual declaration sources are additive and preserve the installed tax book."""

import copy
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from entity_fixture import seed_entities
from test_tax_point_development_upgrade import original_rows, previous_book

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.backup import (
    _retained_history_digest,
    verify_file,
    verify_portable,
)
from ai_accounting.kernel.catalog import ensure_catalog
from ai_accounting.kernel.contracts import BalanceEffect, KernelError, Line, Outcome, Read
from ai_accounting.kernel.development_contracts import (
    TAX_DECLARATION_SOURCE_FINGERPRINT,
    TAX_DECLARATION_TARGET_FINGERPRINT,
    TAX_POINT_SOURCE_FINGERPRINT,
)
from ai_accounting.kernel.domains import taxes
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.permissions import ensure_private_directory
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.security.service import SecurityService
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.tax_filing_data import TaxFilingData
from ai_accounting.kernel.versions import fingerprint, objects, verify_schema


@pytest.fixture
def preserved_declaration_book(tmp_path):
    target = development.source_bundle(production_bundle(), TAX_DECLARATION_TARGET_FINGERPRINT)
    source = development.source_bundle(target, TAX_DECLARATION_SOURCE_FINGERPRINT)
    path = tmp_path / "preserved-tax-declaration-source.sqlite"
    engine = previous_book(path, source)
    return engine, source, target


def test_actual_declaration_adjustment_keeps_every_original_row_and_frozen_adoption(
    preserved_declaration_book,
):
    engine, source, target = preserved_declaration_book
    kinds = {"vat_declaration_actual", "income_tax_declaration_actual"}
    assert not kinds & source.registry.models.keys()
    assert kinds <= target.registry.models.keys()
    assert not kinds & target.registry.evaluators.keys()
    with closing(connect(engine.store.path)) as connection:
        rows = original_rows(connection, source.current())
        receipts = [tuple(row) for row in connection.execute("SELECT * FROM schema_draft_history")]
        history = _retained_history_digest(connection)
        source_objects = objects(connection)
        result = development.upgrade_company(connection, target)
        assert result["status"] == "upgraded"
        assert result["source_fingerprint"] == TAX_DECLARATION_SOURCE_FINGERPRINT
        assert result["target_fingerprint"] == TAX_DECLARATION_TARGET_FINGERPRINT
        assert len(result["steps"]) == 1
        assert original_rows(connection, source.current()) == rows
        assert _retained_history_digest(connection) == history
        assert rows["period_close"] and rows["voucher_line"] and rows["audit"]
        for kind in kinds:
            assert connection.execute("SELECT count(*) FROM fact_" + kind).fetchone()[0] == 0
        after = [tuple(row) for row in connection.execute(
            "SELECT * FROM schema_draft_history ORDER BY sequence"
        )]
        assert after[:len(receipts)] == receipts
        assert len(after) == len(receipts) + 1
        assert all(item in objects(connection) for item in source_objects
                   if item["name"] != "fact_seal_shape")
        verify_schema(connection, bundle=target)
        settled = tuple(connection.iterdump())
        assert development.upgrade_company(connection, target)["status"] == "verified_skip"
        assert tuple(connection.iterdump()) == settled
    assert verify_file(engine.store.path, _bundle=target)["verification"]["status"] == "verified"


@pytest.mark.parametrize("stage", [
    "after_tax_declaration_object:table:fact_vat_declaration_actual",
    "after_tax_declaration_object:table:fact_income_tax_declaration_actual",
    "after_tax_declaration_object:trigger:fact_seal_shape",
    "after_tax_declaration_ddl",
    "before_commit",
])
def test_actual_declaration_adjustment_fault_rolls_back_tables_seals_and_receipts(
    preserved_declaration_book, stage,
):
    engine, source, target = preserved_declaration_book
    with closing(connect(engine.store.path)) as connection:
        before = tuple(connection.iterdump())

        def fail(actual):
            if actual == stage:
                raise RuntimeError("synthetic actual-declaration upgrade failure")

        with pytest.raises(RuntimeError, match="synthetic actual-declaration"):
            development.upgrade_company(connection, target, fault=fail)
        assert not connection.in_transaction
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert tuple(connection.iterdump()) == before
        verify_schema(connection, bundle=source)
    assert verify_file(engine.store.path, _bundle=source)["verification"]["status"] == "verified"


def test_actual_declaration_adjustment_rejects_extra_target_sql_with_declared_transition(
    preserved_declaration_book,
):
    engine, source, target = preserved_declaration_book
    altered = dict(target.current())
    altered["objects"] = sorted(
        [*altered["objects"], dict(
            type="index", name="unapproved_declaration_index",
            sql="CREATE INDEX unapproved_declaration_index ON state(accounting)",
        )], key=lambda item: (item["type"], item["name"]),
    )
    altered["sha256"] = fingerprint(altered["objects"]).hex()
    unsafe = replace(
        target,
        contracts={**target.contracts, "company": {0: altered}},
        draft_transitions={
            **target.draft_transitions,
            "company": target.draft_transitions["company"] | {
                (TAX_DECLARATION_SOURCE_FINGERPRINT, altered["sha256"]),
            },
        },
    )
    with closing(connect(engine.store.path)) as connection:
        before = tuple(connection.iterdump())
        with pytest.raises(KernelError) as rejected:
            development.upgrade_company(connection, unsafe)
        assert rejected.value.code == "migration_not_declared"
        assert tuple(connection.iterdump()) == before
        verify_schema(connection, bundle=source)


def test_actual_declaration_adjustment_rejects_extra_source_sql_without_writes(
    preserved_declaration_book,
):
    engine, _, target = preserved_declaration_book
    with closing(connect(engine.store.path)) as connection:
        connection.execute("CREATE INDEX undeclared_source_index ON state(accounting)")
        before = tuple(connection.iterdump())
        with pytest.raises(KernelError) as rejected:
            development.upgrade_company(connection, target)
        assert rejected.value.code == "schema_fingerprint_mismatch"
        assert tuple(connection.iterdump()) == before


def test_root_actual_declaration_upgrade_checks_backups_before_ddl_and_preserves_owner(
    tmp_path, monkeypatch,
):
    target = development.source_bundle(production_bundle(), TAX_DECLARATION_TARGET_FINGERPRINT)
    source = development.source_bundle(target, TAX_DECLARATION_SOURCE_FINGERPRINT)
    root = ensure_private_directory(tmp_path / "actual-declaration-root")
    catalog = ensure_catalog(root, bundle=source)
    security = SecurityService(catalog)
    security.provision("synthetic-owner", "synthetic-test-password")
    authority = security.login("synthetic-owner", "synthetic-test-password").authority
    directory = ensure_private_directory(root / "91310000123456789A")
    path = directory / "company.sqlite"
    previous_book(path, source)
    with closing(connect(catalog)) as connection:
        connection.execute(
            "INSERT INTO company VALUES('synthetic-co','91310000123456789A',"
            "'synthetic-company',?,'synthetic-db')", (str(path),),
        )
        catalog_before = tuple(connection.iterdump())
    checked = []
    apply = development._apply_tax_declaration_step

    def checked_apply(connection, differences, *, fault=None):
        directories = list((root / ".development-upgrades").iterdir())
        assert len(directories) == 1
        archives = list(directories[0].glob("*.finance-company.zip"))
        assert len(archives) == 1
        verified = verify_portable(archives[0], _bundle=source)
        assert verified["verification"]["status"] == "verified"
        assert verified["identity"]["database_id"] == "synthetic-db"
        with closing(connect(directories[0] / "catalog.sqlite", read_only=True)) as saved:
            assert tuple(saved.iterdump()) == catalog_before
        checked.append(archives[0])
        return apply(connection, differences, fault=fault)

    monkeypatch.setattr(development, "_apply_tax_declaration_step", checked_apply)
    result = development.upgrade_root(root, bundle=target)
    assert result["catalog"] == "verified_skip"
    assert result["companies"][0]["status"] == "upgraded"
    assert checked == [Path(result["companies"][0]["backup"])]
    with closing(connect(catalog, read_only=True)) as connection:
        assert tuple(connection.iterdump()) == catalog_before
    security.validate_authority(authority)
    assert security.login("synthetic-owner", "synthetic-test-password").authority.owner_id == (
        authority.owner_id
    )
    assert verify_file(path, _bundle=target)["verification"]["status"] == "verified"


def test_actual_declaration_upgrade_rejects_source_changed_since_backup(
    preserved_declaration_book,
):
    engine, source, target = preserved_declaration_book
    with closing(connect(engine.store.path)) as connection:
        before = tuple(connection.iterdump())
        with pytest.raises(KernelError) as rejected:
            development.upgrade_company(
                connection, target, expected_source_fingerprint="f" * 64,
            )
        assert rejected.value.code == "schema_fingerprint_mismatch"
        assert tuple(connection.iterdump()) == before
        verify_schema(connection, bundle=source)


def test_historical_tax_point_target_remains_a_valid_isolated_upgrade_endpoint(tmp_path):
    current = production_bundle()
    historical_target = development.source_bundle(current, TAX_DECLARATION_SOURCE_FINGERPRINT)
    source = development.source_bundle(current, TAX_POINT_SOURCE_FINGERPRINT)
    path = tmp_path / "historical-tax-point-endpoint.sqlite"
    previous_book(path, source)
    with closing(connect(path)) as connection:
        result = development.upgrade_company(connection, historical_target)
        assert len(result["steps"]) == 1
        assert result["target_fingerprint"] == TAX_DECLARATION_SOURCE_FINGERPRINT
        verify_schema(connection, bundle=historical_target)
    assert verify_file(path, _bundle=historical_target)["verification"]["status"] == "verified"


class ArchivedIncomeTaxAssessment(taxes.HistoricalIncomeTaxAssessment):
    """The unchanged source body with its pre-declaration read grammar."""

    def reads_for(self, subject_id):
        return self.reads()


def archived_income_evaluator(version, context):
    """Build an isolated original publication without any new actual read edge."""
    fact = version.fact
    assert len(context.facts(fact.kind, f"income-tax:{fact.year}:{fact.period}")) == 1
    previous = context.select(
        Read("calculation", fact.kind, f"income-tax:{fact.year}", fact.period)
    )
    assert not previous
    amount = fact.cumulative_assessed_fen
    obligation = {
        "name": "tax", "key": f"income_tax_assessment:{version.subject_id}:tax",
        "amount_fen": amount, "account": "222106", "normal": "credit",
        "category": "payable", "counterparty_id": None, "cashflow": "tax_payments",
    }
    return Outcome(
        (Line("5801", debit=amount), Line("222106", credit=amount)),
        {"cumulative_assessed_fen": amount, "assessment_basis": fact.assessment_basis,
         "year": fact.year, "change_fen": amount, "obligations": [obligation]},
        (BalanceEffect(obligation["key"], amount),),
    )


def test_first_actual_source_after_upgrade_invalidates_original_tax_and_payment(tmp_path):
    target = production_bundle()
    source = development.source_bundle(target, TAX_DECLARATION_SOURCE_FINGERPRINT)
    registry = copy.copy(source.registry)
    registry.models = dict(registry.models, income_tax_assessment=ArchivedIncomeTaxAssessment)
    registry.body_models = {
        **registry.body_models,
        "income_tax_assessment": ((ArchivedIncomeTaxAssessment, "fact_income_tax_assessment"),),
    }
    registry.evaluators = dict(registry.evaluators, income_tax_assessment=archived_income_evaluator)
    historical = replace(source, registry=registry)
    path = tmp_path / "old-tax-dependencies.sqlite"
    engine = previous_book(path, historical)
    seed_entities(engine, [("authority", "organization", None)])
    proof = engine.register_evidence(
        b"synthetic preserved tax and payment source", "text/plain", "old-tax-proof",
        request_id="old-tax-proof",
    )["digest"]
    engine.save_fact("income_tax_assessment", "archived-tax", {
        "period": "2026-09", "year": 2026, "cumulative_assessed_fen": 10000,
        "assessment_basis": "confirmed_provision",
    }, evidence=(proof,), expected_revision=0, request_id="save-old-tax")

    def publish(subject, request):
        preview = engine.preview([subject])
        assert preview["status"] == "preview", preview
        return engine.confirm(
            [subject], preview_digest=preview["digest"], epochs=preview["epochs"],
            request_id=request,
        )

    publish("archived-tax", "publish-old-tax")
    engine.save_fact("payment", "old-tax-payment", {
        "period": "2026-10", "actual_date": "2026-10-01", "bank_account_id": "bank",
        "amount_fen": 2000, "direction": "outflow", "counterparty_id": "authority",
        "allocations": [{"source_kind": "income_tax_assessment", "source_id": "archived-tax",
                         "obligation": "tax", "amount_fen": 2000}],
    }, evidence=(proof,), expected_revision=0, request_id="save-old-payment")
    publish("old-tax-payment", "publish-old-payment")
    with closing(connect(path)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM dependency_scope WHERE kind='income_tax_declaration_actual'"
        ).fetchone()[0] == 0
        before = original_rows(connection, source.current())
        development.upgrade_company(connection, target)
        assert original_rows(connection, source.current()) == before
    engine = Engine(Store(path, target, "synthetic-co", "synthetic-db"))
    actual = {
        "period": "2026-10", "year": 2026, "tax_period_end": "2026-09",
        "assessment_id": "archived-tax", "filing_evidence": proof,
        "basis_type": "filed_return", "cumulative_assessed_fen": 10000,
    }
    with engine.store.connection(read_only=True) as connection:
        before_save = tuple(connection.iterdump())

    def fail(stage, connection):
        if stage == "commit":
            raise RuntimeError("synthetic actual source save failure")

    engine.fault = fail
    with pytest.raises(RuntimeError, match="actual source save"):
        engine.save_fact("income_tax_declaration_actual", "actual-tax", actual,
                        evidence=(proof,), expected_revision=0, request_id="save-first-actual")
    with engine.store.connection(read_only=True) as connection:
        assert tuple(connection.iterdump()) == before_save
    engine.fault = lambda stage, connection: None
    saved = engine.save_fact("income_tax_declaration_actual", "actual-tax", actual,
                             evidence=(proof,), expected_revision=0, request_id="save-first-actual")
    assert saved["pending"] == ["archived-tax", "old-tax-payment"]
    with engine.store.connection(read_only=True) as connection:
        after_save = tuple(connection.iterdump())
    assert engine.save_fact("income_tax_declaration_actual", "actual-tax", actual,
                            evidence=(proof,), expected_revision=0,
                            request_id="save-first-actual") == saved
    with engine.store.connection(read_only=True) as connection:
        assert tuple(connection.iterdump()) == after_save
    query = TaxFilingData(engine).query(2026, 3)
    current = next(row for row in query["income_tax_assessments"]
                   if row["assessment_id"] == "archived-tax")
    assert current["status"] == "needs_information"
    assert current["cumulative_assessed_fen"] is None
    assert current["current_change_fen"] is None


@pytest.mark.parametrize("model,fields", [
    (taxes.VatDeclarationActual, {"period_start": "2026-01-01", "period_end": "2026-01-31"}),
    (taxes.IncomeTaxDeclarationActual, {"year": 2026, "tax_period_end": "2026-01"}),
])
def test_actual_source_does_not_invalidate_a_foreign_named_subject(
    preserved_declaration_book, model, fields,
):
    old_engine, _, target = preserved_declaration_book
    with closing(connect(old_engine.store.path)) as connection:
        development.upgrade_company(connection, target)
    engine = Engine(Store(old_engine.store.path, target, "synthetic-co", "synthetic-db"))
    proof = engine.register_evidence(
        b"synthetic mismatched actual target", "text/plain", "mismatched-target",
        request_id="mismatched-proof",
    )["digest"]
    with engine.store.connection(read_only=True) as connection:
        capital = tuple(connection.execute(
            "SELECT * FROM calculation_current WHERE subject_id='original-capital'"
        ).fetchone())
    result = engine.save_fact(model.kind, "mismatched-actual", {
        "period": "2026-02", "assessment_id": "original-capital",
        "filing_evidence": proof, "basis_type": "filed_return", **fields,
    }, evidence=(proof,), expected_revision=0, request_id="mismatched-actual")
    assert result["pending"] == []
    with engine.store.connection(read_only=True) as connection:
        assert tuple(connection.execute(
            "SELECT * FROM calculation_current WHERE subject_id='original-capital'"
        ).fetchone()) == capital
        assert not connection.execute(
            "SELECT 1 FROM pending WHERE subject_id='original-capital'"
        ).fetchone()
