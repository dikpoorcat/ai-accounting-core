"""Computed tax storage advances the declared draft without changing its book."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from test_tax_point_development_upgrade import original_rows, previous_book

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.backup import _retained_history_digest, verify_file, verify_portable
from ai_accounting.kernel.catalog import ensure_catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.development_contracts import (
    HISTORY_PRIOR_FINGERPRINT,
    INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT,
    INCOME_TAX_COMPUTATION_TARGET_FINGERPRINT,
    REMUNERATION_SOURCE_FINGERPRINT,
    REMUNERATION_TARGET_FINGERPRINT,
    TAX_DECLARATION_SOURCE_FINGERPRINT,
    TAX_POINT_SOURCE_FINGERPRINT,
)
from ai_accounting.kernel.domains.taxes import HistoricalIncomeTaxAssessment
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.permissions import ensure_private_directory
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.security.service import SecurityService
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import fingerprint, objects, verify_schema

_NEW_TABLES = (
    "fact_income_tax_assessment_computed", "fact_income_tax_policy",
    "fact_income_tax_prepayment_basis",
)


@pytest.fixture
def preserved_computation_book(tmp_path):
    target = production_bundle()
    source = development.source_bundle(target, INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT)
    engine = previous_book(tmp_path / "preserved-income-tax.sqlite", source)
    proof = engine.register_evidence(
        b"synthetic confirmed income-tax provision", "text/plain", "income-tax-proof",
        request_id="income-tax-proof",
    )["digest"]
    saved = engine.save_fact(
        "income_tax_assessment", "original-tax",
        {"period": "2026-03", "year": 2026, "assessment_basis": "confirmed_provision",
         "cumulative_assessed_fen": 1500},
        evidence=(proof,), expected_revision=0, request_id="save-original-tax",
    )
    preview = engine.preview(["original-tax"])
    engine.confirm(
        ["original-tax"], preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id="publish-original-tax",
    )
    return engine, source, target, saved["fact_id"]


@pytest.mark.parametrize("source_fingerprint", [
    REMUNERATION_SOURCE_FINGERPRINT, REMUNERATION_TARGET_FINGERPRINT,
    HISTORY_PRIOR_FINGERPRINT, TAX_POINT_SOURCE_FINGERPRINT,
    TAX_DECLARATION_SOURCE_FINGERPRINT, INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT,
])
def test_historical_registry_uses_only_original_typed_tables(source_fingerprint):
    source = development.source_bundle(production_bundle(), source_fingerprint)
    registry = source.registry
    assert "income_tax_policy" not in registry.models
    assert "income_tax_prepayment_basis" not in registry.models
    assert registry.models["income_tax_assessment"] is HistoricalIncomeTaxAssessment
    assert registry.fact_bodies("income_tax_assessment") == (
        (HistoricalIncomeTaxAssessment, "fact_income_tax_assessment"),
    )
    tables = {item["name"] for item in source.current()["objects"] if item["type"] == "table"}
    assert all(
        table in tables for kind in registry.models for _, table in registry.fact_bodies(kind)
    )


def test_computation_upgrade_preserves_tax_vouchers_frozen_history_and_all_original_rows(
    preserved_computation_book,
):
    engine, source, target, original_fact = preserved_computation_book
    with closing(connect(engine.store.path)) as connection:
        before = original_rows(connection, source.current())
        history = _retained_history_digest(connection)
        original_objects = objects(connection)
        result = development.upgrade_company(connection, target)
        assert result["source_fingerprint"] == INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT
        assert result["target_fingerprint"] == INCOME_TAX_COMPUTATION_TARGET_FINGERPRINT
        assert len(result["steps"]) == 1
        assert original_rows(connection, source.current()) == before
        assert _retained_history_digest(connection) == history
        assert before["fact_income_tax_assessment"] and before["voucher_line"]
        assert before["period_close"] and before["audit"]
        for table in _NEW_TABLES:
            assert connection.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
        assert all(item in objects(connection) for item in original_objects
                   if item["name"] != "fact_seal_shape")
        verify_schema(connection, bundle=target)
        store = Store(engine.store.path, target, "synthetic-co", "synthetic-db")
        assert type(store.fact(connection, original_fact).fact) is HistoricalIncomeTaxAssessment
        after = tuple(connection.iterdump())
        assert development.upgrade_company(connection, target)["status"] == "verified_skip"
        assert tuple(connection.iterdump()) == after
    assert verify_file(engine.store.path, _bundle=target)["verification"]["status"] == "verified"


@pytest.mark.parametrize("stage", [
    *("after_income_tax_computation_object:table:" + table for table in _NEW_TABLES),
    "after_income_tax_computation_object:trigger:fact_seal_shape",
    "after_income_tax_computation_ddl", "before_commit",
])
def test_computation_upgrade_failure_rolls_back_storage_seals_and_receipts(
    preserved_computation_book, stage,
):
    engine, source, target, _ = preserved_computation_book
    with closing(connect(engine.store.path)) as connection:
        before = tuple(connection.iterdump())

        def fail(actual):
            if actual == stage:
                raise RuntimeError("synthetic income-tax-computation upgrade failure")

        with pytest.raises(RuntimeError, match="synthetic income-tax-computation"):
            development.upgrade_company(connection, target, fault=fail)
        assert not connection.in_transaction
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert tuple(connection.iterdump()) == before
        verify_schema(connection, bundle=source)
    assert verify_file(engine.store.path, _bundle=source)["verification"]["status"] == "verified"


def test_computation_upgrade_rejects_extra_sql_even_with_forged_target_transition(
    preserved_computation_book,
):
    engine, source, target, _ = preserved_computation_book
    altered = dict(target.current())
    altered["objects"] = sorted([
        *altered["objects"], dict(type="index", name="unapproved_computation_index",
                                sql="CREATE INDEX unapproved_computation_index ON state(accounting)"),
    ], key=lambda item: (item["type"], item["name"]))
    altered["sha256"] = fingerprint(altered["objects"]).hex()
    transitions = target.draft_transitions["company"] | {
        (INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT, altered["sha256"]),
    }
    unsafe = replace(
        target, contracts={**target.contracts, "company": {0: altered}},
        draft_transitions={**target.draft_transitions, "company": transitions},
    )
    with closing(connect(engine.store.path)) as connection:
        before = tuple(connection.iterdump())
        with pytest.raises(KernelError) as failed:
            development.upgrade_company(connection, unsafe)
        assert failed.value.code == "migration_not_declared"
        assert tuple(connection.iterdump()) == before
        verify_schema(connection, bundle=source)


def test_root_computation_upgrade_checks_backup_before_ddl_and_preserves_owner(
    tmp_path, monkeypatch,
):
    target = production_bundle()
    source = development.source_bundle(target, INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT)
    root = ensure_private_directory(tmp_path / "computed-income-tax-root")
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
    original_apply = development._apply_income_tax_computation_step

    def checked_apply(connection, differences, *, fault=None):
        backups = list((root / ".development-upgrades").glob("*/*.finance-company.zip"))
        assert len(backups) == 1
        assert verify_portable(backups[0], _bundle=source)["verification"]["status"] == "verified"
        with closing(connect(backups[0].parent / "catalog.sqlite", read_only=True)) as saved:
            assert tuple(saved.iterdump()) == catalog_before
        checked.append(backups[0])
        return original_apply(connection, differences, fault=fault)

    monkeypatch.setattr(development, "_apply_income_tax_computation_step", checked_apply)
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


def test_prior_tax_declaration_source_chains_through_both_declared_steps(tmp_path):
    target = production_bundle()
    source = development.source_bundle(target, TAX_DECLARATION_SOURCE_FINGERPRINT)
    path = tmp_path / "two-tax-steps.sqlite"
    previous_book(path, source)
    with closing(connect(path)) as connection:
        result = development.upgrade_company(connection, target)
        assert [(step["source_fingerprint"], step["target_fingerprint"])
                for step in result["steps"]] == [
            (TAX_DECLARATION_SOURCE_FINGERPRINT, INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT),
            (INCOME_TAX_COMPUTATION_SOURCE_FINGERPRINT, INCOME_TAX_COMPUTATION_TARGET_FINGERPRINT),
        ]
        verify_schema(connection, bundle=target)
