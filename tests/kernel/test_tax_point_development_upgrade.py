"""The tax-point draft adds empty typed storage while preserving the installed book."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from entity_fixture import seed_entities
from monthly_close_fixture import close_months, ready
from test_remuneration_development_upgrade import create_archived_company

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.backup import (
    _retained_history_digest,
    verify_file,
    verify_portable,
)
from ai_accounting.kernel.catalog import ensure_catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.development_contracts import (
    TAX_POINT_SOURCE_FINGERPRINT,
    TAX_POINT_TARGET_FINGERPRINT,
)
from ai_accounting.kernel.domains.opening import CATEGORIES
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.permissions import ensure_private_directory
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.security.service import SecurityService
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import fingerprint, objects, verify_schema


def tax_point_target():
    """This historical migration has its own exact endpoint as later drafts evolve."""
    return development.source_bundle(production_bundle(), TAX_POINT_TARGET_FINGERPRINT)


def original_rows(connection, contract):
    """Compare each saved column independently from the upgrade's digest checks."""
    return {
        item["name"]: sorted(
            [tuple(row) for row in connection.execute('SELECT * FROM "' + item["name"] + '"')],
            key=repr,
        )
        for item in contract["objects"]
        if item["type"] == "table" and item["name"] != "schema_draft_history"
    }


def previous_book(path, source, *, taxpayer_id="91310000123456789A"):
    create_archived_company(path, source, taxpayer_id=taxpayer_id)
    engine = Engine(Store(path, source, "synthetic-co", "synthetic-db"))
    proof = engine.register_evidence(
        b"synthetic original close and owner capital source",
        "text/plain", "original-proof", request_id="original-proof",
    )["digest"]
    engine.save_fact(
        "opening_package", "original-opening",
        {
            "period": "2026-01", "package_id": "original-opening", "members": [],
            "counts": dict.fromkeys(CATEGORIES.values(), 0), "completeness_confirmed": True,
        },
        evidence=(proof,), expected_revision=0, request_id="original-opening",
    )
    opening = engine.preview(["original-opening"])
    assert opening["status"] == "preview", opening
    engine.confirm(
        ["original-opening"], preview_digest=opening["digest"], epochs=opening["epochs"],
        request_id="publish-original-opening",
    )
    ready(engine, proof, first="2026-01", last="2026-01")
    close_months(Periods(engine), proof, first="2026-01", last="2026-01")
    seed_entities(engine, [("owner", "person", None), ("bank", "fund_account", "bank")])
    engine.save_fact(
        "funding", "original-capital",
        {
            "period": "2026-02", "owner_id": "owner", "amount_fen": 12000,
            "funding_kind": "capital", "actual_date": "2026-02-01", "bank_account_id": "bank",
        },
        evidence=(proof,), expected_revision=0, request_id="original-capital",
    )
    preview = engine.preview(["original-capital"])
    assert preview["status"] == "preview", preview
    engine.confirm(
        ["original-capital"], preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id="publish-original-capital",
    )
    assert verify_file(path, _bundle=source)["verification"]["status"] == "verified"
    return engine


@pytest.fixture
def preserved_book(tmp_path):
    target = tax_point_target()
    source = development.source_bundle(target, TAX_POINT_SOURCE_FINGERPRINT)
    path = tmp_path / "original-tax-point-source.sqlite"
    engine = previous_book(path, source)
    return engine, source, target


def test_tax_point_adjustment_keeps_every_original_row_and_frozen_adoption(preserved_book):
    engine, source, target = preserved_book
    assert "tax_point_basis" not in source.registry.models
    with closing(connect(engine.store.path)) as connection:
        rows = original_rows(connection, source.current())
        receipts = [tuple(row) for row in connection.execute("SELECT * FROM schema_draft_history")]
        history = _retained_history_digest(connection)
        source_objects = objects(connection)
        result = development.upgrade_company(connection, target)
        assert result["status"] == "upgraded"
        assert result["source_fingerprint"] == TAX_POINT_SOURCE_FINGERPRINT
        assert len(result["steps"]) == 1
        assert original_rows(connection, source.current()) == rows
        assert _retained_history_digest(connection) == history
        assert rows["period_close"] and rows["voucher_line"] and rows["audit"]
        assert connection.execute("SELECT count(*) FROM fact_tax_point_basis").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM fact_tax_point_basis_candidates"
        ).fetchone()[0] == 0
        assert [tuple(row) for row in connection.execute(
            "SELECT * FROM schema_draft_history ORDER BY sequence"
        )][:len(receipts)] == receipts
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == (
            len(receipts) + 1
        )
        assert all(item in objects(connection) for item in source_objects
                   if item["name"] != "fact_seal_shape")
        verify_schema(connection, bundle=target)
        assert development.upgrade_company(connection, target)["status"] == "verified_skip"
    assert verify_file(engine.store.path, _bundle=target)["verification"]["status"] == "verified"


@pytest.mark.parametrize("stage", [
    "after_tax_point_object:table:fact_tax_point_basis",
    "after_tax_point_object:table:fact_tax_point_basis_candidates",
    "after_tax_point_object:trigger:fact_seal_shape",
    "after_tax_point_ddl",
    "before_commit",
])
def test_tax_point_adjustment_fault_rolls_back_tables_seals_and_receipts(preserved_book, stage):
    engine, source, target = preserved_book
    with closing(connect(engine.store.path)) as connection:
        before = tuple(connection.iterdump())

        def fail(actual):
            if actual == stage:
                raise RuntimeError("synthetic tax-point upgrade failure")

        with pytest.raises(RuntimeError, match="synthetic tax-point"):
            development.upgrade_company(connection, target, fault=fail)
        assert not connection.in_transaction
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert tuple(connection.iterdump()) == before
        verify_schema(connection, bundle=source)
    assert verify_file(engine.store.path, _bundle=source)["verification"]["status"] == "verified"


def test_tax_point_adjustment_rejects_extra_target_sql_even_with_registered_fingerprint(
    preserved_book,
):
    engine, source, target = preserved_book
    altered = dict(target.current())
    altered["objects"] = sorted(
        [*altered["objects"], dict(
            type="index", name="unapproved_tax_point_index",
            sql="CREATE INDEX unapproved_tax_point_index ON state(accounting)",
        )], key=lambda item: (item["type"], item["name"]),
    )
    altered["sha256"] = fingerprint(altered["objects"]).hex()
    unsafe = replace(
        target,
        contracts={**target.contracts, "company": {0: altered}},
        draft_transitions={
            **target.draft_transitions,
            "company": target.draft_transitions["company"] | {
                (TAX_POINT_SOURCE_FINGERPRINT, altered["sha256"]),
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


def test_root_tax_point_upgrade_verifies_backups_before_ddl_and_preserves_owner(
    tmp_path, monkeypatch,
):
    target = tax_point_target()
    source = development.source_bundle(target, TAX_POINT_SOURCE_FINGERPRINT)
    root = ensure_private_directory(tmp_path / "tax-point-root")
    catalog = ensure_catalog(root, bundle=source)
    security = SecurityService(catalog)
    security.provision("synthetic-owner", "synthetic-test-password")
    login = security.login("synthetic-owner", "synthetic-test-password")
    authority = login.authority
    company_directory = ensure_private_directory(root / "91310000123456789A")
    path = company_directory / "company.sqlite"
    previous_book(path, source)
    with closing(connect(catalog)) as connection:
        connection.execute(
            "INSERT INTO company VALUES('synthetic-co','91310000123456789A',"
            "'synthetic-company',?,'synthetic-db')", (str(path),),
        )
        catalog_before = tuple(connection.iterdump())
    checked = []
    apply = development._apply_tax_point_step

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

    monkeypatch.setattr(development, "_apply_tax_point_step", checked_apply)
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


def test_tax_point_upgrade_rejects_source_changed_since_backup(preserved_book):
    engine, source, target = preserved_book
    with closing(connect(engine.store.path)) as connection:
        before = tuple(connection.iterdump())
        with pytest.raises(KernelError) as rejected:
            development.upgrade_company(
                connection, target, expected_source_fingerprint="f" * 64,
            )
        assert rejected.value.code == "schema_fingerprint_mismatch"
        assert tuple(connection.iterdump()) == before
        verify_schema(connection, bundle=source)
