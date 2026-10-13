"""Explicit historical body identities and source confirmations remain exact."""
from contextlib import closing

import pytest

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.entity_references import references_from_data
from ai_accounting.kernel.offline_development_upgrade import _catalog_bundle, _upgrade_catalog
from ai_accounting.kernel.payroll_confirmation import resolve_payroll_confirmation
from ai_accounting.kernel.payroll_history import HistoricalPayrollProfile
from ai_accounting.kernel.runtime import connect, initialize_file
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.source24_history import PayrollPlanPreRevision, PayrollPreRevision
from ai_accounting.kernel.versions import install_metadata, verify_schema


@pytest.mark.parametrize("source_name", ["SOURCE24_BASELINE_FINGERPRINT", "SOURCE24_FINGERPRINT"])
def test_exact_eight_source_journals_keep_full_authoritative_validation(tmp_path, source_name):
    from types import SimpleNamespace

    from archived_draft_fixture import archived_company

    from ai_accounting.kernel import development_contracts
    from ai_accounting.kernel.integrity import _verify_source_change_journal

    path, source = archived_company(tmp_path / "eight-source.sqlite",
                                    getattr(development_contracts, source_name))
    engine = SimpleNamespace(store=SimpleNamespace(bundle=source))
    with closing(connect(path)) as connection:
        assert _verify_source_change_journal(engine, connection) == 0
        # A well-shaped event with no authoritative fact still fails. Selecting
        # this fixed grammar never excuses coverage or sequence validation.
        connection.execute(
            "INSERT INTO source_change(sequence,source,target_id,before_ref,after_ref) "
            "VALUES(1,'fact','missing-subject',NULL,'missing-fact')"
        )
        with pytest.raises(KernelError) as failed:
            _verify_source_change_journal(engine, connection)
        assert failed.value.code == "content_integrity_failed"
        assert failed.value.details["component"] == "source_change"


def test_declared_eight_source_ancestors_share_the_exact_journal_contract():
    from ai_accounting.kernel.development_contracts import (
        SOURCE24_BASELINE_FINGERPRINT,
        SOURCE24_FINGERPRINT,
    )

    bundle = production_bundle()
    objects = []
    for fingerprint in (SOURCE24_BASELINE_FINGERPRINT, SOURCE24_FINGERPRINT):
        source = bundle.development_contracts["company"][fingerprint]
        objects.append({(item["type"], item["name"]): item["sql"]
                        for item in source["objects"] if "source_change" in item["sql"]})
    assert objects[0] == objects[1]
    assert len(objects[0]) == 22
    assert "source_change_retained_boundary" not in {name for _, name in objects[0]}
    assert "source IN('fact','calculation','pending','identity','duplicate','inventory'," \
           "'inventory_item','publication')" in objects[0]["table", "source_change"]


def wage():
    return PayrollPreRevision(
        period="2026-01", employee_id="employee", profile_id="profile",
        contribution_policy_id="contributions", income_tax_policy_id="tax-policy",
        accounting_gross_salary_fen=600_000, tax_reported_salary_fen=600_000,
        tax_exempt_income_fen=0, special_additional_deduction_fen=0,
        other_legal_deduction_fen=0, tax_relief_fen=0,
        expense_class="management", contribution_basis="policy_until_actual",
    )


def test_retained_material_body_is_independent_of_future_active_material_rules():
    """Reload against a changed active grammar in a separate interpreter."""
    import subprocess
    import sys

    program = """
import importlib
import json
from pydantic import ValidationError, create_model
from ai_accounting.kernel import materials, source24_history as history
raw = {'period': '2026-01', 'source_id': 'source', 'source_fact_id': 'source:v1',
       'location': 'row:1', 'treatment': 'recognize', 'amount_fen': 100,
       'recognition_period': '2026-01', 'links': [
           {'subject_id': 'business', 'fact_kind': 'funding', 'fact_id': 'business:v1',
            'calculation_id': 'calculation', 'amount_field': 'result.amount_fen',
            'amount_fen': 100, 'recognition_period': '2026-01'}]}
before = history.MaterialResolutionPreReference.model_validate_json(json.dumps(raw))
expected, scopes = before.model_dump(mode='json'), before.scopes()
future = create_model('FutureMaterialResolution', future_required=(str, ...),
                      __base__=materials.MaterialResolution)
materials.MaterialResolution = future
materials.Detail = create_model('FutureDetail', future_detail=(str, ...),
                                __base__=materials.Detail)
history = importlib.reload(history)
retained = history.MaterialResolutionPreReference.model_validate_json(json.dumps(raw))
assert retained.model_dump(mode='json') == expected
assert retained.scopes() == scopes
try:
    future.model_validate({'period': '2026-01', 'source_id': 'source',
                          'source_fact_id': 'source:v1', 'location': 'row:1',
                          'treatment': 'supporting'})
except ValidationError as error:
    assert any(item['loc'] == ('future_required',) for item in error.errors())
else:
    raise AssertionError('Future active registration must enforce its new field')
try:
    history.MaterialResolutionPreReference.model_validate_json(
        json.dumps({**raw, 'future_required': 'value'}))
except ValidationError:
    pass
else:
    raise AssertionError('Retained input must not accept new active fields')
"""
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", program],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def sources():
    from test_payroll import contribution_policy, income_tax_policy, version
    profile = HistoricalPayrollProfile(
        period="2026-01", employee_id="employee", effective_from="2026-01",
        effective_to=None, withholding_start_date="2026-01",
        social_insurance_base_fen=None, housing_fund_base_fen=None,
        social_insurance_participating=False, housing_fund_participating=False,
        contribution_shortfall="reject",
    )
    return (version(profile, "profile"), version(contribution_policy(), "contributions"),
            version(income_tax_policy(), "tax-policy"))


def confirmation_context(*, evidence=("exact-proof",), profile_revision=1):
    from test_payroll import context_for, version
    fact = wage()
    plan = PayrollPlanPreRevision(
        period=fact.period, employee_id=fact.employee_id, payroll=fact,
        profile_revision=dict(subject_id="profile", revision=profile_revision),
        contribution_policy_revision=dict(subject_id="contributions", revision=1),
        income_tax_policy_revision=dict(subject_id="tax-policy", revision=1),
        change_notice_revisions=(),
    )
    current = version(fact, "wage")
    return current, context_for(current, [*sources(), version(plan, "plan", evidence=evidence)])


def test_original_monthly_confirmation_preserves_its_exact_source_ids():
    current, context = confirmation_context()
    result = resolve_payroll_confirmation(current, context)
    assert result["confirmation_fact_id"] == "plan:v1"
    assert result["income_tax_policy_fact_id"] == "tax-policy:v1"
    assert result["profile_fact_id"] == "profile:v1"
    assert "tax_reported_salary_fen" in current.fact.model_dump(mode="json")


@pytest.mark.parametrize("change", [dict(evidence=()), dict(profile_revision=2)])
def test_original_confirmation_rejects_missing_proof_or_changed_source(change):
    current, context = confirmation_context(**change)
    with pytest.raises(NeedsInformation):
        resolve_payroll_confirmation(current, context)


def test_old_plan_reference_paths_preserve_embedded_source_and_employee_roles():
    current, _ = confirmation_context()
    plan = PayrollPlanPreRevision(
        period=current.fact.period, employee_id="employee", payroll=current.fact,
        profile_revision=dict(subject_id="profile", revision=1),
        contribution_policy_revision=dict(subject_id="contributions", revision=1),
        income_tax_policy_revision=dict(subject_id="tax-policy", revision=1),
        change_notice_revisions=(),
    )
    refs = references_from_data(plan.kind, plan.model_dump(mode="json"))
    assert {r["path"] for r in refs} == {
        "employee_id", "payroll.employee_id", "payroll.profile_id",
        "payroll.contribution_policy_id", "payroll.income_tax_policy_id",
        "profile_revision.subject_id", "contribution_policy_revision.subject_id",
        "income_tax_policy_revision.subject_id",
    }


@pytest.mark.parametrize("fail", [False, True])
def test_prior_catalog_adds_receipt_without_rewriting_installation(tmp_path, fail):
    from dataclasses import replace
    from types import MappingProxyType

    from ai_accounting.kernel.development_contracts import CATALOG_PRIOR_FINGERPRINT
    bundle = production_bundle()
    source = bundle.development_contracts["catalog"][CATALOG_PRIOR_FINGERPRINT]
    old = replace(bundle, contracts=MappingProxyType(
        {**bundle.contracts, "catalog": MappingProxyType({0: source})}
    ))
    path = tmp_path / "catalog.sqlite"
    def initialize(connection):
        connection.execute("BEGIN IMMEDIATE")
        for typ in ("table", "index", "trigger"):
            for item in source["objects"]:
                if item["type"] == typ:
                    connection.execute(item["sql"])
        connection.execute("INSERT INTO catalog_identity VALUES(1,'same-owner-catalog')")
        install_metadata(connection, old, "catalog")
        connection.commit()
    initialize_file(path, initialize, lambda c: verify_schema(c, bundle=old, kind="catalog"))
    with closing(connect(path)) as connection:
        original = tuple(connection.iterdump())
        history = connection.execute("SELECT * FROM schema_history").fetchall()
        def fault(at):
            if at == "before_catalog_commit":
                raise RuntimeError("catalog rollback")
        if fail:
            with pytest.raises(RuntimeError, match="rollback"):
                _upgrade_catalog(connection, bundle, fault=fault)
            assert tuple(connection.iterdump()) == original
            return
        assert _catalog_bundle(connection, bundle).current("catalog") == source
        assert _upgrade_catalog(connection, bundle) == "upgraded"
        assert connection.execute("SELECT * FROM schema_history").fetchall() == history
        assert connection.execute("SELECT instance_id FROM catalog_identity").fetchone()[0] \
            == "same-owner-catalog"
        assert _upgrade_catalog(connection, bundle) == "verified_skip"
        verify_schema(connection, bundle=bundle, kind="catalog")


def old_model_root():
    return {
        "format": "ai-accounting-kernel/2/dashboard-read-model/3", "content_version": 3,
        "scope": {"company_id": "same-company", "database_id": "same-db",
                  "period": "2026-01", "epochs": {"accounting": 0, "material": 0,
                  "management": 0}, "read_repair_revision": 0,
                  "rule_build": "source24-retained-producer", "as_of": "2026-01-05"},
        "blocks": [{"key": "employees/all/employees/0", "kind": "dashboard_employees",
                    "digest": "1" * 64, "item_count": 1, "total_count": 1,
                    "filtered_count": 1, "keys": [["person", "regular", "in_period", 1]],
                    "collection_version": "source24-roster"}],
        "date_basis": {"rule": "current-employment-month-or-day/1",
                       "valid_from": "2026-01-05", "next_change": "2026-02-01",
                       "employees": [{"employee_id": "person", "explicit_state": None,
                                      "employment_start": "2025-01", "employment_end": "2026-01",
                                      "employment_status": "active"}]},
    }


def test_original_model_calendar_parses_without_changing_saved_shape():
    from ai_accounting.kernel.dashboard_read_model import ModelRoot
    from ai_accounting.kernel.types import canonical

    raw = old_model_root()
    root = ModelRoot.model_validate_json(canonical(raw))
    assert root.model_dump(mode="json") == raw
    assert root.date_basis.rule == "current-employment-month-or-day/1"
    assert "period" not in root.date_basis.model_dump()


@pytest.mark.parametrize("damage", [
    "missing_field", "extra_current_field", "unknown_rule", "wrong_boundary",
    "duplicate_person", "other_production_day", "new_directory", "current_content_version",
])
def test_original_model_calendar_is_strict_and_cannot_be_current_content(damage):
    from pydantic import ValidationError

    from ai_accounting.kernel.dashboard_read_model import ModelRoot
    from ai_accounting.kernel.types import canonical

    raw = old_model_root()
    basis = raw["date_basis"]
    if damage == "missing_field":
        basis["employees"][0].pop("explicit_state")
    elif damage == "extra_current_field":
        basis["period"] = "2026-01"
    elif damage == "unknown_rule":
        basis["rule"] = "unregistered-rule/1"
    elif damage == "wrong_boundary":
        basis["next_change"] = "2026-02-02"
    elif damage == "duplicate_person":
        basis["employees"] *= 2
    elif damage == "other_production_day":
        basis["valid_from"] = "2026-01-06"
    elif damage == "new_directory":
        raw["blocks"][0]["keys"][0].append("new-current-predicate")
    else:
        raw.update(format="ai-accounting-kernel/2/dashboard-read-model/6", content_version=6)
    with pytest.raises(ValidationError):
        ModelRoot.model_validate_json(canonical(raw))


@pytest.mark.parametrize(
    "field", [None, "subject_id", "fact_kind", "fact_id", "recognition_period"]
)
def test_original_material_link_checks_all_seven_fields_against_exact_saved_result(tmp_path, field):
    from test_materials import Company

    from ai_accounting.kernel.materials import resolve_material_links
    from ai_accounting.kernel.source24_history import MaterialLinkPreReference
    from ai_accounting.kernel.types import canonical

    company = Company(tmp_path)
    data = company.expense("exact-expense", 1000)
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        calculation = connection.execute(
            "SELECT fact_id FROM calculation WHERE id=?", (data["calculation_id"],)
        ).fetchone()
        complete = data | {"subject_id": "exact-expense", "fact_kind": "expense",
                           "fact_id": calculation[0], "recognition_period": "2026-01"}
        if field is not None:
            complete[field] = "2026-02" if field == "recognition_period" else "wrong-exact-binding"
        link = MaterialLinkPreReference.model_validate_json(canonical(complete))
        if field is None:
            result = resolve_material_links(
                connection, (link,), registry=company.engine.store.registry
            )
            assert result[0].fact_id == calculation[0]
        else:
            with pytest.raises(KernelError) as rejected:
                resolve_material_links(connection, (link,), registry=company.engine.store.registry)
            assert rejected.value.code == "content_integrity_failed"


@pytest.mark.parametrize("missing_body", [False, True])
def test_full_verifier_selects_original_body_and_rejects_missing_body(tmp_path, missing_body):
    """Only isolated fixture construction enables the retained grammar as input."""
    import copy
    from dataclasses import replace

    from test_integrity_content import damage
    from test_materials import Company

    from ai_accounting.kernel.backup import BackupError, verify_file
    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.source24_history import MaterialResolutionPreReference
    from ai_accounting.kernel.storage import Store

    company = Company(tmp_path)
    data = company.expense("exact-expense", 1000)
    source, _ = company.source(b"name,amount,period\na,10.00,2026-01\n")
    with company.engine.store.connection(read_only=True) as connection:
        fact_id = connection.execute(
            "SELECT fact_id FROM calculation WHERE id=?", (data["calculation_id"],)
        ).fetchone()[0]
    store = company.engine.store
    registry = copy.copy(store.registry)
    registry.models = dict(store.registry.models)
    registry.body_models = dict(store.registry.body_models)
    kind = "material_resolution_v2"
    registry.models[kind] = MaterialResolutionPreReference
    registry.body_models[kind] = tuple(reversed(store.registry.fact_bodies(kind)))
    fixture_engine = Engine(Store(store.path, replace(store.bundle, registry=registry),
                                  store.company_id, store.database_id))
    saved = fixture_engine.save_fact(
        kind, "original-resolution",
        dict(period="2026-01", source_id=source["subject_id"], source_fact_id=source["fact_id"],
             location="CSV!B2", treatment="recognize", recognition_period="2026-01",
             links=[data | dict(subject_id="exact-expense", fact_kind="expense",
                                fact_id=fact_id, recognition_period="2026-01")]),
        evidence=(company.proof,), expected_revision=0, request_id=company.request(),
    )
    if missing_body:
        damage(company.engine, "fact_material_resolution_v2_pre_reference",
               "DELETE FROM fact_material_resolution_v2_pre_reference WHERE revision_id=?",
               (saved["fact_id"],), foreign_keys=False)
        with pytest.raises(KernelError):
            with store.connection(read_only=True) as connection:
                store.fact(connection, saved["fact_id"])
        with pytest.raises(BackupError) as rejected:
            verify_file(store.path)
        assert rejected.value.code == "backup_content_invalid"
    else:
        assert verify_file(store.path)["verification"]["status"] == "verified"
        with store.connection(read_only=True) as connection:
            assert type(store.fact(connection, saved["fact_id"]).fact) \
                is MaterialResolutionPreReference


def test_unfrozen_heads_seek_both_actual_body_source_indexes_without_row_growth(record_property):
    import sqlite3
    from types import SimpleNamespace

    from ai_accounting.kernel.materials import (
        MATERIAL_HISTORY_INDEX_DDL,
        _unfrozen_material_heads,
    )

    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
        "CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY,fact_id TEXT UNIQUE);"
        "CREATE TABLE fact_material_resolution_v2(revision_id TEXT PRIMARY KEY,source_id TEXT);"
        "CREATE TABLE fact_material_resolution_v2_pre_reference("
        "revision_id TEXT PRIMARY KEY,source_id TEXT);"
        "CREATE TABLE fact_material_resolution_v2_pre_reference_links("
        "revision_id TEXT,calculation_id TEXT);"
        "CREATE INDEX material_resolution_v2_source "
        "ON fact_material_resolution_v2(source_id,revision_id);" + MATERIAL_HISTORY_INDEX_DDL
    )
    tables = ("fact_material_resolution_v2", "fact_material_resolution_v2_pre_reference")
    registry = SimpleNamespace(fact_bodies=lambda kind: [(None, table) for table in tables])
    for number, table in enumerate(tables):
        for state in ("closed", "open", "withdrawn"):
            ident = f"{state}-{number}"
            connection.execute(f"INSERT INTO {table} VALUES(?,?)", (ident, state))
            connection.execute("INSERT INTO subject VALUES(?,'material_resolution_v2')", (ident,))
            if state != "withdrawn":
                connection.execute("INSERT INTO fact_current VALUES(?,?)", (ident, ident))
    statements = []
    connection.set_trace_callback(statements.append)
    assert {row[0] for row in _unfrozen_material_heads(
        connection, registry, "material_resolution_v2", {"closed"}
    )} == {"open-0", "open-1"}
    sql = statements[-1]
    connection.set_trace_callback(None)
    plan = "\n".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql))
    assert "material_resolution_v2_source" in plan
    assert "material_resolution_pre_reference_source" in plan
    assert "SCAN fact_material_resolution_v2_pre_reference" not in plan
    steps, previous_count = [], 0
    for count in (0, 100, 400):
        if count:
            connection.executemany(
                "INSERT INTO fact_material_resolution_v2_pre_reference VALUES(?,'closed')",
                [(f"unrelated-{number}",) for number in range(previous_count, count)],
            )
        executed = []
        connection.set_progress_handler(lambda sink=executed: sink.append(1) or 0, 1)
        assert {row[0] for row in _unfrozen_material_heads(
            connection, registry, "material_resolution_v2", {"closed"}
        )} == {"open-0", "open-1"}
        connection.set_progress_handler(None, 0)
        steps.append(len(executed))
        previous_count = count
    record_property("history_material_heads_vm_steps", steps)
    print("history_material_heads_vm_steps", steps)
    assert max(steps) - min(steps) <= 20
    connection.close()
