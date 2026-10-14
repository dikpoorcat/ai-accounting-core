"""Exact history transfer, empty guards and rollback using isolated source SQL."""

import json
from contextlib import closing

import pytest

from ai_accounting.kernel.backup import verify_file
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.runtime import connect, initialize_file
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.versions import install_metadata, objects, verify_schema
from scripts import stage9_history_adjustment as adjustment


@pytest.mark.parametrize("extra_accounting", [0, 1])
def test_adjustment_checkpoint_binds_new_state_and_preserves_original(tmp_path, extra_accounting):
    from scripts.adjust_stage9_samples import update_construction_checkpoint
    from scripts.stage9_verified_open_preview import require_preview_state

    company = tmp_path / "company.sqlite"
    original_file(company, "company")
    original = {
        "company": {"id": "synthetic-company", "database_id": "synthetic-database"},
        "epochs": [1, 0, 0, 0, 1, 0],
        "snapshots": {"2016-02": {"preview_digest": "original-preview"}},
        "attestation": {"digest": "original-attestation"},
    }
    checkpoint_path = tmp_path / "stage9-builder.json"
    raw = json.dumps(original).encode()
    checkpoint_path.write_bytes(raw)
    with closing(connect(company)) as connection:
        connection.execute(
            "UPDATE state SET accounting=?,material=1,management=2 WHERE id=1",
            (extra_accounting,),
        )
        connection.commit()
    result = {
        "workforce_format": {"regular_employees": 2},
        "target_verification": {"status": "verified", "limitations": []},
    }
    if extra_accounting:
        with pytest.raises(ValueError, match="exact authorized"):
            update_construction_checkpoint(tmp_path, company, result)
        assert checkpoint_path.read_bytes() == raw
        return
    binding = update_construction_checkpoint(tmp_path, company, result)
    updated = json.loads(checkpoint_path.read_bytes())
    receipt = json.loads((tmp_path / binding["receipt_file"]).read_bytes())
    assert (tmp_path / receipt["original_checkpoint_file"]).read_bytes() == raw
    assert updated["snapshots"] == original["snapshots"]
    assert updated["attestation"] == original["attestation"]
    assert receipt["qualification_required"] is True
    assert updated["epochs"] == [1, 0, 1, 2, 1, 0]
    assert update_construction_checkpoint(tmp_path, company, result) == binding
    import hashlib

    checked = {
        "state": updated["epochs"], "identity": receipt["identity"],
        "checkpoint_path": checkpoint_path,
        "checkpoint_sha256": hashlib.sha256(checkpoint_path.read_bytes()).hexdigest(),
    }
    with closing(connect(company, read_only=True)) as connection:
        require_preview_state(connection, checked)
        with pytest.raises(ValueError, match="state"):
            require_preview_state(connection, {**checked, "state": original["epochs"]})


def original_file(path, kind):
    source = adjustment.source_bundle()

    def initialize(connection):
        connection.execute("BEGIN IMMEDIATE")
        for typ in ("table", "index", "trigger", "view"):
            for item in source.current(kind)["objects"]:
                if item["type"] == typ:
                    connection.execute(item["sql"])
        if kind == "company":
            connection.execute("INSERT INTO state VALUES(1,0,0,0,1,0)")
            connection.execute("INSERT INTO source_change_head VALUES(1,0)")
            connection.execute(
                "INSERT INTO identity VALUES(1,'synthetic-company',"
                "'91310000123456789S','synthetic-database')"
            )
        else:
            connection.execute("INSERT INTO catalog_identity VALUES(1,'synthetic-catalog')")
        install_metadata(connection, source, kind)
        connection.commit()

    initialize_file(path, initialize, lambda c: verify_schema(c, bundle=source, kind=kind))
    return source


def test_real_second_writer_invalidates_prebackup_source_guard(tmp_path):
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.storage import Store

    path = tmp_path / "company.sqlite"
    source = original_file(path, "company")
    with closing(connect(path, read_only=True)) as snapshot:
        snapshot.execute("BEGIN")
        guard = adjustment.capture_source_guard(snapshot, kind="company")
    engine = Engine(Store(path, source, "synthetic-company", "synthetic-database"))
    engine.register_evidence(b"independent writer", "text/plain", "new source", request_id="writer")
    assert verify_file(path, _bundle=source)["verification"]["status"] == "verified"
    with closing(connect(path)) as connection:
        connection.execute("BEGIN")
        after_writer = adjustment.capture_source_guard(connection, kind="company")
        connection.rollback()
        with pytest.raises(KernelError, match="备份来源"):
            adjustment.adjust_connection(connection, kind="company", expected_source_guard=guard)
        connection.execute("BEGIN")
        assert adjustment.capture_source_guard(connection, kind="company") == after_writer
        connection.rollback()


def test_failed_foreign_key_restore_closes_connection(tmp_path):
    import sqlite3

    path = tmp_path / "company.sqlite"
    original_file(path, "company")
    connection = connect(path)

    def fault(at):
        if at in {"after_drop", "before_restore_foreign_keys"}:
            raise RuntimeError(at)

    with pytest.raises(RuntimeError, match="after_drop") as failure:
        adjustment.adjust_connection(connection, kind="company", fault=fault)
    assert failure.value.migration_state["outcome"] == "rolled_back"
    assert failure.value.migration_state["cleanup_errors"] == [
        {"phase": "foreign_keys_restore", "error_type": "RuntimeError"}
    ]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with closing(connect(path)) as reopened:
        assert adjustment.plan_connection(reopened, kind="company")["status"] == "planned"


def test_failed_root_keeps_committed_progress_in_final_report(tmp_path, monkeypatch):
    from scripts import adjust_stage9_samples as tool

    monkeypatch.setattr(tool, "WORKSPACE", tmp_path)

    def fail(key, phase, output, *, progress):
        progress({"sample": key, "verified_company_backup": "preserved.zip",
                  "company_adjustment": {"status": "adjusted"}})
        raise RuntimeError("catalog failure")

    monkeypatch.setattr(tool, "run_sample", fail)
    with pytest.raises(RuntimeError, match="catalog failure"):
        tool.main(["--phase", "apply", "--samples", "main12", "--run-id", "failure-proof"])
    prefix = tmp_path / ".tmp" / "stage9-adjustment-failure-proof"
    report = json.loads(prefix.with_suffix(".json").read_bytes())
    progress = json.loads(prefix.with_name(prefix.name + "-main12-progress.json").read_bytes())
    assert report["status"] == "failed"
    assert report["samples"]["main12"] == progress
    assert progress["verified_company_backup"] == "preserved.zip"
    assert progress["company_adjustment"]["status"] == "adjusted"


def test_storage_budget_includes_actual_wal_and_checks_current_free(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts import adjust_stage9_samples as tool

    company = tmp_path / "company.sqlite"
    catalog = tmp_path / "catalog.sqlite"
    company.write_bytes(b"x" * 100)
    catalog.write_bytes(b"x" * 20)
    company.with_name(company.name + "-wal").write_bytes(b"x" * 50)
    monkeypatch.setattr(tool.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    budget = tool.storage_budget(company, catalog)
    assert budget["company_main_bytes"] == 100
    assert budget["company_wal_bytes"] == 50
    assert budget["migration_peak_bytes"] == budget["zip_upper_bound_bytes"] + 300
    assert budget["sufficient"] is False


def test_storage_budget_independently_rejects_full_verification_temporary_disk(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from scripts import adjust_stage9_samples as tool

    company = tmp_path / "company.sqlite"
    catalog = tmp_path / "catalog.sqlite"
    company.write_bytes(b"x" * 100)
    catalog.write_bytes(b"x" * 20)
    temporary = tmp_path / "verification-temporary"
    temporary.mkdir()
    monkeypatch.setattr(tool.tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.setattr(
        tool.shutil, "disk_usage",
        lambda path: SimpleNamespace(free=1 if path == temporary else 10**12),
    )
    budget = tool.storage_budget(company, catalog)
    assert budget["free_bytes"] >= budget["additional_free_required_bytes"]
    assert budget["verification_temporary_directory"] == str(temporary)
    assert budget["verification_temporary_free_bytes"] == 1
    assert budget["verification_temporary_free_required_bytes"] > 1
    assert budget["sufficient"] is False


@pytest.mark.parametrize("kind", ["company", "catalog"])
def test_exact_source_adjusts_and_preserves_installation_ancestry(tmp_path, kind):
    path = tmp_path / (kind + ".sqlite")
    source = original_file(path, kind)
    if kind == "company":
        assert verify_file(path, _bundle=source)["verification"]["status"] == "verified"
    with closing(connect(path)) as connection:
        original_history = connection.execute("SELECT * FROM schema_history").fetchall()
        result = adjustment.adjust_connection(connection, kind=kind)
        assert result["status"] == "adjusted"
        assert connection.execute("SELECT * FROM schema_history").fetchall() == original_history
        verify_schema(connection, bundle=adjustment.target_bundle(), kind=kind)
        assert adjustment.adjust_connection(connection, kind=kind)["status"] == "already_adjusted"
    if kind == "company":
        assert verify_file(path, _bundle=adjustment.target_bundle())["verification"]["status"] == (
            "verified"
        )


def test_historical_adjustment_stops_at_its_original_contract_before_explicit_tax_point_upgrade(
    tmp_path,
):
    from ai_accounting.kernel.development_contracts import (
        HISTORY_TARGET_FINGERPRINT,
        TAX_POINT_TARGET_FINGERPRINT,
    )
    from ai_accounting.kernel.offline_development_upgrade import upgrade_company

    path = tmp_path / "historical-then-current.sqlite"
    original_file(path, "company")
    with closing(connect(path)) as connection:
        installed = connection.execute("SELECT * FROM schema_history").fetchall()
        result = adjustment.adjust_connection(connection, kind="company")
        assert result["target_fingerprint"] == HISTORY_TARGET_FINGERPRINT
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='fact_tax_point_basis'"
        ).fetchone() is None
        assert connection.execute("SELECT * FROM schema_history").fetchall() == installed
        verify_schema(connection, bundle=adjustment.target_bundle())
        with pytest.raises(KernelError) as mismatch:
            verify_schema(connection, bundle=production_bundle())
        assert mismatch.value.code == "schema_fingerprint_mismatch"
        separate = upgrade_company(connection, production_bundle())
        assert separate["source_fingerprint"] == HISTORY_TARGET_FINGERPRINT
        assert separate["target_fingerprint"] == TAX_POINT_TARGET_FINGERPRINT
        assert len(separate["steps"]) == 1
        assert connection.execute("SELECT count(*) FROM fact_tax_point_basis").fetchone()[0] == 0
        assert connection.execute("SELECT * FROM schema_history").fetchall() == installed
    assert verify_file(path)["verification"]["status"] == "verified"


@pytest.mark.parametrize("phase", ["after_history_copy", "after_drop", "before_commit"])
def test_company_fault_rolls_back_exact_sql_and_all_rows(tmp_path, phase):
    path = tmp_path / "company.sqlite"
    original_file(path, "company")
    with closing(connect(path)) as connection:
        before_objects = objects(connection)
        before = {
            item["name"]: connection.execute('SELECT * FROM "' + item["name"] + '"').fetchall()
            for item in before_objects
            if item["type"] == "table"
        }

        def fault(at):
            if at == phase:
                raise RuntimeError("injected history transfer failure")

        with pytest.raises(RuntimeError, match="injected"):
            adjustment.adjust_connection(connection, kind="company", fault=fault)
        assert not connection.in_transaction
        assert objects(connection) == before_objects
        for table, rows in before.items():
            assert connection.execute('SELECT * FROM "' + table + '"').fetchall() == rows
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_nonempty_management_note_is_rejected_before_any_ddl(tmp_path):
    path = tmp_path / "company.sqlite"
    original_file(path, "company")
    with closing(connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO subject VALUES('synthetic-subject','expense')")
        connection.execute(
            "INSERT INTO management_revision "
            "VALUES(1,'synthetic-subject',1,'preserve me',NULL,NULL)"
        )
        connection.commit()
        before = objects(connection)
        with pytest.raises(KernelError, match="management_revision"):
            adjustment.plan_connection(connection, kind="company")
        assert objects(connection) == before
        assert (
            connection.execute("SELECT note FROM management_revision").fetchone()[0]
            == "preserve me"
        )


def test_undeclared_sql_is_rejected_without_touching_rows(tmp_path):
    path = tmp_path / "company.sqlite"
    original_file(path, "company")
    with closing(connect(path)) as connection:
        connection.execute("CREATE INDEX undeclared_index ON state(accounting)")
        before = objects(connection)
        with pytest.raises(KernelError, match="实际SQL"):
            adjustment.adjust_connection(connection, kind="company")
        assert objects(connection) == before


def test_nonempty_catalog_import_is_rejected(tmp_path):
    path = tmp_path / "catalog.sqlite"
    original_file(path, "catalog")
    with closing(connect(path)) as connection:
        connection.execute(
            "INSERT INTO security_owner "
            "VALUES(?,1,'owner','owner','active',?,1,0,NULL,0,NULL,0,0,NULL)",
            ("owner", "$argon2id$v=19$m=65536,t=3,p=4$".ljust(97, "x")),
        )
        connection.execute(
            "INSERT INTO security_identity_import VALUES('source-catalog','owner',0)"
        )
        before = objects(connection)
        with pytest.raises(KernelError, match="security_identity_import"):
            adjustment.plan_connection(connection, kind="catalog")
        assert objects(connection) == before


@pytest.mark.parametrize("fault_phase", [None, "after_history_copy", "after_drop", "before_commit"])
def test_nonempty_original_wage_and_plan_keep_exact_fact_hashes(tmp_path, fault_phase, monkeypatch):
    """Fixture installation allows old input only locally; runtime remains read-only."""
    import copy
    from dataclasses import replace

    import entity_fixture

    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.entity_profile_history import PreEventProfile
    from ai_accounting.kernel.source24_history import PayrollPlanPreRevision, PayrollPreRevision
    from ai_accounting.kernel.storage import Store

    path = tmp_path / "company.sqlite"
    source = original_file(path, "company")
    registry = copy.copy(source.registry)
    registry.historical_only = source.registry.historical_only - {"payroll", "payroll_plan_v2"}
    fixture = replace(source, registry=registry)
    engine = Engine(Store(path, fixture, "synthetic-company", "synthetic-database"))
    current_models = entity_fixture.entity_profile_models()
    monkeypatch.setattr(
        entity_fixture,
        "entity_profile_models",
        lambda: {
            **current_models,
            "person": PreEventProfile,
        },
    )
    entity_fixture.seed_entities(engine, [("synthetic-employee", "person", None)])
    proof = engine.register_evidence(
        b"explicit synthetic original wage and plan",
        "text/plain",
        "original fixture",
        request_id="synthetic-proof",
    )["digest"]
    wage = PayrollPreRevision(
        period="2026-01",
        employee_id="synthetic-employee",
        profile_id="profile",
        contribution_policy_id="contributions",
        income_tax_policy_id="tax-policy",
        accounting_gross_salary_fen=600_000,
        tax_reported_salary_fen=600_000,
        tax_exempt_income_fen=0,
        special_additional_deduction_fen=0,
        other_legal_deduction_fen=0,
        tax_relief_fen=0,
        expense_class="management",
        contribution_basis="policy_until_actual",
    )
    plan = PayrollPlanPreRevision(
        period="2026-01",
        employee_id="synthetic-employee",
        payroll=wage,
        profile_revision={"subject_id": "profile", "revision": 1},
        contribution_policy_revision={"subject_id": "contributions", "revision": 1},
        income_tax_policy_revision={"subject_id": "tax-policy", "revision": 1},
        change_notice_revisions=(),
    )
    saved = []
    for kind, subject, data in [("payroll", "wage", wage), ("payroll_plan_v2", "plan", plan)]:
        saved.append(
            engine.save_fact(
                kind,
                subject,
                data.model_dump(mode="json"),
                evidence=(proof,),
                expected_revision=0,
                request_id="save-" + subject,
            )["fact_id"]
        )
    assert verify_file(path, _bundle=source)["verification"]["status"] == "verified"
    with closing(connect(path)) as connection:
        original_sql = objects(connection)
        original_headers = connection.execute("SELECT * FROM fact_revision ORDER BY id").fetchall()
        original_bodies = [
            adjustment._row_digest(connection, name, adjustment._columns(connection, name))
            for name in ("fact_payroll", "fact_payroll_plan_v2")
        ]
        if fault_phase is not None:

            def fault(at):
                if at == fault_phase:
                    raise RuntimeError("injected nonempty transfer failure")

            with pytest.raises(RuntimeError, match="injected nonempty"):
                adjustment.adjust_connection(connection, kind="company", fault=fault)
            assert objects(connection) == original_sql
            for name, before in zip(
                ("fact_payroll", "fact_payroll_plan_v2"),
                original_bodies,
                strict=True,
            ):
                assert (
                    adjustment._row_digest(
                        connection,
                        name,
                        adjustment._columns(connection, name),
                    )
                    == before
                )
            assert (
                connection.execute("SELECT * FROM fact_revision ORDER BY id").fetchall()
                == original_headers
            )
            return
        assert adjustment.adjust_connection(connection, kind="company")["status"] == "adjusted"
        assert (
            connection.execute("SELECT * FROM fact_revision ORDER BY id").fetchall()
            == original_headers
        )
        for name, before in zip(
            ("fact_payroll_pre_revision", "fact_payroll_plan_v2_pre_revision"),
            original_bodies,
            strict=True,
        ):
            assert (
                adjustment._row_digest(connection, name, adjustment._columns(connection, name))
                == before
            )
        store = Store(path, adjustment.target_bundle(), "synthetic-company", "synthetic-database")
        assert [type(store.fact(connection, ident).fact) for ident in saved] == [
            PayrollPreRevision,
            PayrollPlanPreRevision,
        ]
        assert connection.execute("SELECT count(*) FROM fact_payroll_confirmed").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM fact_remuneration_tax_basis").fetchone()[0]
            == 0
        )
    assert verify_file(path, _bundle=adjustment.target_bundle())["verification"]["status"] == (
        "verified"
    )
    from scripts.adjust_stage9_samples import append_workforce_format

    with closing(connect(path, read_only=True)) as connection:
        original_state = tuple(connection.execute("SELECT * FROM state").fetchone())
    with pytest.raises(ValueError, match="explicit regular active"):
        append_workforce_format(path, expected_employees=1)
    with closing(connect(path, read_only=True)) as connection:
        assert tuple(connection.execute("SELECT * FROM state").fetchone()) == original_state


@pytest.mark.parametrize("fault_phase", [None, "after_fund_initialization", "before_commit"])
def test_new_synthetic_fund_source_and_audit_are_atomic(tmp_path, fault_phase):
    from entity_fixture import seed_entities

    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.storage import Store
    from scripts.adjust_stage9_samples import synthetic_fund_settings

    path = tmp_path / "company.sqlite"
    source = original_file(path, "company")
    engine = Engine(Store(path, source, "synthetic-company", "synthetic-database"))
    seed_entities(engine, [("synthetic-cash", "fund_account", "cash")])
    assert verify_file(path, _bundle=source)["verification"]["status"] == "verified"
    with closing(connect(path)) as connection:
        settings = synthetic_fund_settings(connection, company_name="synthetic company")
        source_objects = objects(connection)
        originals = {
            item["name"]: connection.execute('SELECT * FROM "' + item["name"] + '"').fetchall()
            for item in source_objects
            if item["type"] == "table"
        }

        def fault(at):
            if at == fault_phase:
                raise RuntimeError("injected metadata failure")

        if fault_phase is not None:
            with pytest.raises(RuntimeError, match="metadata failure"):
                adjustment.adjust_connection(
                    connection,
                    kind="company",
                    fund_accounts=settings["accounts"],
                    first_fund_period=settings["first_period"],
                    fault=fault,
                )
            assert objects(connection) == source_objects
            for table, rows in originals.items():
                assert connection.execute('SELECT * FROM "' + table + '"').fetchall() == rows
        else:
            result = adjustment.adjust_connection(
                connection,
                kind="company",
                fund_accounts=settings["accounts"],
                first_fund_period=settings["first_period"],
            )
            assert result["synthetic_metadata_state_delta"]["material"] == 1
            assert result["synthetic_metadata_state_delta"]["accounting"] == 0
            proof = result["new_synthetic_metadata_evidence"]
            assert (
                connection.execute(
                    "SELECT action FROM audit WHERE request_id=?",
                    (proof["request_id"],),
                ).fetchone()[0]
                == "evidence"
            )
            assert (
                connection.execute("SELECT evidence_digest FROM fund_account_definition")
                .fetchone()[0]
                .hex()
                == proof["digest"]
            )
    expected_bundle = source if fault_phase else adjustment.target_bundle()
    assert verify_file(path, _bundle=expected_bundle)["verification"]["status"] == "verified"
