"""Add confirmed-tax bodies while retaining the exact current draft's source history."""

from contextlib import closing
from dataclasses import replace

import pytest
from entity_fixture import seed_entities

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.backup import _retained_history_digest, verify_file
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.development_contracts import REMUNERATION_SOURCE_FINGERPRINT
from ai_accounting.kernel.domains.opening import CATEGORIES
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.payroll_history import HistoricalPayrollProfile
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.runtime import connect, initialize_file
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import install_metadata, objects, verify_schema


def create_archived_company(
    path, source, *, taxpayer_id="synthetic-tax",
    company_id="synthetic-co", database_id="synthetic-db",
):
    """Create an isolated database using the exact declared original contract."""
    contract = source.current("company")

    def initialize(connection):
        connection.execute("BEGIN IMMEDIATE")
        for kind in ("table", "index", "trigger"):
            for item in contract["objects"]:
                if item["type"] == kind:
                    connection.execute(item["sql"])
        connection.execute("INSERT INTO state VALUES(1,0,0,0,1,0)")
        connection.execute("INSERT INTO source_change_head VALUES(1,0)")
        connection.execute(
            "INSERT INTO identity VALUES(1,?,?,?)",
            (company_id, taxpayer_id, database_id),
        )
        install_metadata(connection, source, "company")
        connection.commit()

    initialize_file(path, initialize, lambda connection: verify_schema(connection, bundle=source))


@pytest.fixture
def prior_company(tmp_path):
    target = production_bundle()
    source = development.source_bundle(target, REMUNERATION_SOURCE_FINGERPRINT)
    path = tmp_path / "preserved-draft.sqlite"
    create_archived_company(path, source)
    engine = Engine(Store(path, source, "synthetic-co", "synthetic-db"))
    evidence = engine.register_evidence(
        b"explicit synthetic opening and source profile",
        "text/plain",
        "synthetic source",
        request_id="source-proof",
    )["digest"]
    # A formal opening is explicitly adopted before this test freezes a month.
    engine.save_fact(
        "opening_package",
        "opening",
        {
            "period": "2026-01",
            "package_id": "opening",
            "members": [],
            "counts": dict.fromkeys(CATEGORIES.values(), 0),
            "completeness_confirmed": True,
        },
        evidence=(evidence,),
        expected_revision=0,
        request_id="save-opening",
    )
    preview = engine.preview(["opening"])
    assert preview["status"] == "preview", preview
    engine.confirm(
        ["opening"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="publish-opening",
    )
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-01",
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=evidence,
            request_id="source-inventory-" + category,
        )
    close = periods.preview_close("2026-01", owner_confirmation=evidence)
    assert "digest" in close, close
    periods.close(
        "2026-01",
        owner_confirmation=evidence,
        preview_digest=close["digest"],
        epochs=close["epochs"],
        request_id="source-close",
    )
    seed_entities(engine, [("employee", "person", None)])
    # This is a synthetic preserved source installation. Its original public
    # grammar is enabled only while constructing the isolated historical row.
    fixture_registry = replace_registry_for_source_fixture(source.registry)
    fixture_store = Store(
        path, replace(source, registry=fixture_registry), "synthetic-co", "synthetic-db"
    )
    profile = HistoricalPayrollProfile(
        period="2026-08",
        employee_id="employee",
        effective_from="2026-08",
        effective_to=None,
        withholding_start_date="2026-08",
        social_insurance_base_fen=None,
        housing_fund_base_fen=None,
        social_insurance_participating=False,
        housing_fund_participating=False,
        contribution_shortfall="reject",
    )
    saved = Engine(fixture_store).save_fact(
        "payroll_profile",
        "profile",
        profile.model_dump(mode="json"),
        evidence=(evidence,),
        expected_revision=0,
        request_id="saved-original-profile",
    )
    assert verify_file(path, _bundle=source)["verification"]["status"] == "verified"
    return path, source, target, saved["fact_id"], profile


def replace_registry_for_source_fixture(original):
    """Explicit isolated installation setup, never a production write route."""
    import copy

    result = copy.copy(original)
    result.historical_only = original.historical_only - {"payroll_profile"}
    return result


def test_forward_adjustment_preserves_all_original_rows_sql_and_frozen_history(prior_company):
    path, source, target, fact_id, profile = prior_company
    with closing(connect(path)) as connection:
        retained = _retained_history_digest(connection)
        rows = development._all_existing_rows(connection, source.current())
        historical_sql = next(
            item for item in objects(connection) if item["name"] == "fact_payroll_profile"
        )
        frozen = connection.execute("SELECT * FROM period_close").fetchall()
        result = development.upgrade_company(connection, target)
        assert result["status"] == "upgraded"
        assert result["source_fingerprint"] == REMUNERATION_SOURCE_FINGERPRINT
        assert result["target_fingerprint"] == target.current()["sha256"]
        assert result["original_rows_digest"] == rows[0]
        assert _retained_history_digest(connection) == retained
        assert connection.execute("SELECT * FROM period_close").fetchall() == frozen
        assert historical_sql in objects(connection)
        assert (
            connection.execute("SELECT count(*) FROM fact_payroll_profile_confirmed").fetchone()[0]
            == 0
        )
        assert [
            tuple(row) for row in connection.execute("SELECT hex(fingerprint) FROM schema_history")
        ] == [
            (REMUNERATION_SOURCE_FINGERPRINT.upper(),),
        ]
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == 3
        verify_schema(connection, bundle=target)
        store = Store(path, target, "synthetic-co", "synthetic-db")
        assert store.fact_data(connection, fact_id) == profile.model_dump(mode="json")
        assert type(store.fact(connection, fact_id).fact) is HistoricalPayrollProfile
        assert development.upgrade_company(connection, target)["status"] == "verified_skip"
    assert verify_file(path, _bundle=target)["verification"]["status"] == "verified"


@pytest.mark.parametrize(
    "stage",
    [
        "after_remuneration_object:table:fact_payroll_confirmed",
        "after_remuneration_object:trigger:fact_seal_shape",
        "before_commit",
    ],
)
def test_any_adjustment_fault_rolls_back_exact_structure_and_history(prior_company, stage):
    path, source, target, _, _ = prior_company
    with closing(connect(path)) as connection:
        before = tuple(connection.iterdump())

        def fault(actual):
            if actual == stage:
                raise RuntimeError("synthetic migration fault")

        with pytest.raises(RuntimeError, match="synthetic migration fault"):
            development.upgrade_company(connection, target, fault=fault)
        assert not connection.in_transaction
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert tuple(connection.iterdump()) == before
        verify_schema(connection, bundle=source)


def test_forward_adjustment_rejects_undeclared_extra_sql_without_writes(prior_company):
    path, _, target, _, _ = prior_company
    with closing(connect(path)) as connection:
        connection.execute("CREATE INDEX synthetic_unknown ON state(accounting)")
        connection.commit()
        before = tuple(connection.iterdump())
        with pytest.raises(KernelError) as failure:
            development.upgrade_company(connection, target)
        assert failure.value.code == "schema_fingerprint_mismatch"
        assert tuple(connection.iterdump()) == before


def test_prior_confirmed_shape_moves_forward_by_adding_empty_history_bodies(tmp_path):
    """An e7 installation is itself a declared source, without replaying 3ead."""
    from ai_accounting.kernel.development_contracts import REMUNERATION_TARGET_FINGERPRINT

    target = production_bundle()
    source = development.source_bundle(target, REMUNERATION_TARGET_FINGERPRINT)
    path = tmp_path / "prior-confirmed.sqlite"
    create_archived_company(path, source)
    with closing(connect(path)) as connection:
        before = connection.execute("SELECT * FROM schema_history").fetchall()
        result = development.upgrade_company(connection, target)
        assert len(result["steps"]) == 2
        assert result["source_fingerprint"] == REMUNERATION_TARGET_FINGERPRINT
        assert connection.execute("SELECT * FROM schema_history").fetchall() == before
        assert connection.execute(
            "SELECT count(*) FROM fact_payroll_pre_revision"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM source_change_retained_boundary"
        ).fetchone()[0] == 0
    assert verify_file(path)["verification"]["status"] == "verified"


def test_prior_history_source_index_upgrade_runs_through_real_cli(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    from ai_accounting.kernel.catalog import ensure_catalog
    from ai_accounting.kernel.development_contracts import HISTORY_PRIOR_FINGERPRINT
    from ai_accounting.kernel.permissions import ensure_private_directory

    target = production_bundle()
    source = development.source_bundle(target, HISTORY_PRIOR_FINGERPRINT)
    root = ensure_private_directory(tmp_path / "prior-history-root")
    ensure_catalog(root)
    directory = ensure_private_directory(root / "91310000123456789A")
    path = directory / "company.sqlite"
    create_archived_company(path, source, taxpayer_id="91310000123456789A")
    with closing(connect(root / "catalog.sqlite")) as connection:
        connection.execute(
            "INSERT INTO company VALUES('synthetic-co','91310000123456789A','synthetic',?,"
            "'synthetic-db')", (str(path),),
        )
    with closing(connect(path)) as connection:
        original_history = connection.execute("SELECT * FROM schema_history").fetchall()
        planned = development._declared_changes(target)
        index_step = [step for step in planned if step[0]["sha256"] == HISTORY_PRIOR_FINGERPRINT]
        assert len(index_step) == 1
        assert [(item["type"], item["name"]) for item in index_step[0][2]] == [
            ("index", "material_resolution_pre_reference_source")
        ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(development.__file__).resolve().parents[2])
    module_source = subprocess.run(
        [sys.executable, "-X", "utf8", "-c",
         "from ai_accounting.kernel import cli; print(cli.__file__)"],
        capture_output=True, text=True, encoding="utf-8", env=environment,
    )
    assert module_source.returncode == 0, module_source.stdout + module_source.stderr
    assert Path(module_source.stdout.strip()).resolve() == Path(development.__file__).with_name(
        "cli.py"
    ).resolve()
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "ai_accounting.kernel.cli", "--root", str(root),
         "upgrade-development"], capture_output=True, text=True, encoding="utf-8", env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout)
    assert response["status"] == "upgraded"
    assert response["companies"][0]["source_fingerprint"] == HISTORY_PRIOR_FINGERPRINT
    assert response["companies"][0]["target_fingerprint"] == target.current()["sha256"]
    assert len(response["companies"][0]["steps"]) == 2
    with closing(connect(path)) as connection:
        assert connection.execute("SELECT * FROM schema_history").fetchall() == original_history
        verify_schema(connection, bundle=target)
    assert verify_file(path)["verification"]["status"] == "verified"
