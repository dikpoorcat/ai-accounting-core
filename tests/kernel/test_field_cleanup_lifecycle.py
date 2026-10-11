"""Production contracts and synthetic sources survive the complete retained lifecycle."""

import json
from pathlib import Path

from test_dashboard_model_transactions import raw_read
from test_materials import csv_spec
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company

from ai_accounting.kernel.backup import restore_portable, run_backup_jobs, verify_portable
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import stable_payload
from ai_accounting.kernel.domains.banking import BankOpening, BankReconciliation, BankStatement
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.integrity import verify_integrity
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.materials import MaterialLink, Materials
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import verify_schema

MONTH = "2026-01"
NUMBER = "622200001234567890"


def assert_single_sources(engine, bank_id):
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        verify_schema(connection, bundle=engine.store.bundle)
        verify_integrity(engine, connection)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(management_revision)")}
        assert columns and "note" not in columns
        rows = connection.execute(
            "SELECT content FROM entity_profile_revision WHERE entity_id=?", (bank_id,)
        ).fetchall()
        assert len(rows) == 1
        bank_profile = json.loads(rows[0][0])
        assert (
            not {"display_number", "external_identifiers", "bank_account_number"}
            & bank_profile.keys()
        )
        wage = engine.store.current_fact(connection, "january").fact.model_dump(mode="json")
        assert not {"profile_id", "contribution_policy_id", "income_tax_policy_id"} & wage.keys()
        plans = connection.execute(
            "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind='payroll_plan_v2'"
        ).fetchall()
        assert len(plans) == 1
        assert set(engine.store.fact(connection, plans[0][0]).fact.model_dump()) == {
            "period",
            "payroll_revision",
            "change_notice_revisions",
        }
        resolution = engine.store.current_fact(connection, "wage-resolution").fact
        assert set(resolution.links[0].model_dump()) == {
            "calculation_id",
            "amount_field",
            "amount_fen",
        }


def pages(engine):
    dashboard = Dashboard(engine)
    return {
        "funds": stable_payload(dashboard.funds(MONTH)),
        "employees": stable_payload(dashboard.employees(MONTH, employee_filter="all")),
    }


def assert_native_and_materialized(engine):
    Maintenance(engine).rebuild_dashboard_read_model(
        MONTH, request_id="lifecycle-dashboard-rebuild"
    )
    materialized = pages(engine)
    with raw_read():
        native = pages(engine)
    assert materialized == native
    return materialized


def test_cleanup_public_inputs_close_backup_and_independent_restore(tmp_path):
    company = Company(tmp_path / "synthetic-company.sqlite")
    engine = company.engine
    directory = Entities(engine)
    bank = directory.register_entity(
        "fund_account",
        {
            "display_name": "合成工资账户",
            "bank_name": "合成银行",
            "bank_account_number": NUMBER,
            "account_holder_name": "合成测试公司",
        },
        account_type="bank",
        source="合成开户原件中的正式账号",
        evidence_digest=company.owner_confirmation,
        request_id=company.request(),
    )
    bank_id = bank["entity_id"]
    found = directory.find_entities(kind="fund_account", query=NUMBER)
    assert len(found["items"]) == 1 and found["items"][0]["entity_id"] == bank_id
    assert found["items"][0]["fund_account"]["identity"]["bank_account_number"] == NUMBER
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "tax-opening"),
        (payroll(), "january"),
        (
            BankOpening(period=MONTH, bank_account_id=bank_id, opening_fen=0, basis="new_account"),
            "bank-opening",
        ),
        (
            BankStatement(
                period=MONTH, bank_account_id=bank_id, opening_fen=0, closing_fen=0, entries=()
            ),
            "bank-statement",
        ),
        (
            BankReconciliation(
                period=MONTH, bank_account_id=bank_id, statement_id="bank-statement", matches=()
            ),
            "bank-reconciliation",
        ),
    ):
        company.save(fact, subject)
    company.confirm_payroll("january")
    company.publish("january", "bank-opening", "bank-statement", "bank-reconciliation")
    wage = company.current("january")
    assert wage.values["payroll_confirmation"]["mode"] == "monthly_plan"

    raw = b"name,amount,period\nsynthetic wage,10000.00,2026-01\n"
    proof = engine.register_evidence(
        raw, "text/csv", "synthetic-wage.csv", request_id=company.request()
    )["digest"]
    materials = Materials(engine)
    source = materials.receive(
        "wage-source",
        {
            "period": MONTH,
            "evidence_digest": proof,
            "category": "payroll",
            "purpose": "business",
            "specification": csv_spec(),
        },
        evidence=(proof,),
        expected_revision=0,
        request_id=company.request(),
    )
    link = MaterialLink(
        calculation_id=wage.id, amount_field="result.gross_fen", amount_fen=1_000_000
    )
    materials.resolve(
        "wage-resolution",
        {
            "period": MONTH,
            "source_id": source["subject_id"],
            "source_fact_id": source["fact_id"],
            "location": "CSV!B2",
            "treatment": "recognize",
            "recognition_period": MONTH,
            "links": [link.model_dump(mode="json")],
        },
        evidence=(proof,),
        expected_revision=0,
        request_id=company.request(),
    )
    company.materials["payroll"].append((MONTH, proof))
    assert not materials.check(MONTH)["issues"]
    before_close = assert_native_and_materialized(engine)
    accounts = before_close["funds"]["data"]["collections"]["accounts"]["items"]
    assert next(item for item in accounts if item["account_id"] == bank_id)["code"] == NUMBER
    assert_single_sources(engine, bank_id)

    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        evidence = sorted(
            {digest for month, digest in company.materials[category] if month <= MONTH}
        )
        periods.inventory(
            MONTH,
            category,
            evidence=evidence,
            expected=len(evidence),
            no_business=not evidence,
            confirmation_evidence=company.owner_confirmation,
            request_id=company.request(),
        )
    preview = periods.preview_close(MONTH, owner_confirmation=company.owner_confirmation)
    closed = periods.close(
        MONTH,
        owner_confirmation=company.owner_confirmation,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        backup_directory=str(tmp_path / "synthetic-backups"),
        request_id=company.request(),
    )
    assert closed["backup_job"]
    frozen = periods.closed_report(MONTH)
    closed_pages = pages(engine)
    worker = run_backup_jobs(engine.store.path)
    assert len(worker) == 1 and worker[0]["status"] == "succeeded"
    with engine.store.connection(read_only=True) as connection:
        job = connection.execute(
            "SELECT status,result FROM jobs WHERE id=?", (closed["backup_job"],)
        ).fetchone()
    receipt = json.loads(job["result"])
    assert job["status"] == "succeeded" and receipt["verification"]["status"] == "verified"
    archive = Path(receipt["path"])
    assert archive.is_file() and archive.suffix == ".zip"
    assert verify_portable(archive)["verification"]["status"] == "verified"
    target = tmp_path / "independent-restore.sqlite"
    assert not target.exists()
    restored = restore_portable(archive, target, expected_company_id=engine.store.company_id)
    assert restored["verification"]["status"] == "verified"
    restored_engine = Engine(
        Store(target, production_bundle(), engine.store.company_id, engine.store.database_id)
    )
    assert_single_sources(restored_engine, bank_id)
    assert pages(restored_engine) == closed_pages
    assert Periods(restored_engine).closed_report(MONTH) == frozen
