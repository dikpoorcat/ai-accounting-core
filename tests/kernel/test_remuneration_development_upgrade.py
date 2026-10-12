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


def create_archived_company(path, source):
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
            "INSERT INTO identity VALUES(1,'synthetic-co','synthetic-tax','synthetic-db')"
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
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == 1
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
