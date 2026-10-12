"""Isolated TEST_FAMILY forward contracts, never production released-v1 fixtures."""

import json
import sys
from collections import Counter
from dataclasses import replace
from difflib import unified_diff
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

import pytest
from monthly_close_fixture import close_months, ready
from schema_fixture import TEST_FAMILY, full_contract, write_contract
from test_engine import Charge, Source, calculate, evidence, publish, save
from test_identity_corrections import confirm as confirm_identity
from test_identity_corrections import expense as save_expense
from test_identity_corrections import opening_package
from test_materials import csv_spec
from test_opening_continuation import _close_without_current_business

from ai_accounting.kernel import cli, close_storage, content_v1, offline_upgrade, service, storage
from ai_accounting.kernel.backup import (
    _retained_history_digest,
    create_portable,
    restore_portable,
    verify_portable,
)
from ai_accounting.kernel.catalog import Catalog, catalog_sql
from ai_accounting.kernel.contracts import Fact, KernelError, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.migration_steps import MigrationStep
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.runtime import private_file_lock
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import (
    APPLICATION_ID,
    load_bundle,
    verify_company_with_registry,
    verify_current_company,
)
from ai_accounting.kernel.storage import Store


def _fixture(tmp_path):
    registry = Registry()
    directory = tmp_path / "contracts"
    scripts = {
        "company": schema_sql(registry),
        "catalog": catalog_sql(),
    }
    steps = []
    for kind, source_sql in scripts.items():
        name = f"stage9_{kind}_synthetic"
        added_sql = f"CREATE INDEX {name} ON " + (
            "fact_revision(subject_id)" if kind == "company" else "company(name)"
        )
        source = full_contract(source_sql, kind=kind, status="released", version=1)
        target = full_contract(
            source_sql + added_sql + ";", kind=kind, status="released", version=2
        )
        write_contract(directory, source)
        write_contract(
            directory,
            {key: value for key, value in target.items() if key != "objects"}
            | {
                "base_version": 1,
                "base_sha256": source["sha256"],
                "add": [item for item in target["objects"] if item not in source["objects"]],
                "remove": [],
                "replace": [],
            },
        )
        steps.append(
            MigrationStep(
                TEST_FAMILY,
                kind,
                1,
                source["sha256"],
                2,
                target["sha256"],
                lambda connection, sql=added_sql: connection.execute(sql),
                lambda connection: None,
            )
        )

    def bundle(version):
        return load_bundle(
            registry,
            directory,
            family=TEST_FAMILY,
            application_id=APPLICATION_ID,
            status="released",
            current_versions={"company": version, "catalog": version},
            steps=tuple(steps),
            company_verifiers={1: verify_current_company, 2: verify_current_company},
        )

    root = tmp_path / "root"
    catalog = Catalog(root, bundle(1))
    companies = [
        catalog.create_company(taxpayer_id, "合成公司" + str(index))
        for index, taxpayer_id in enumerate(("91310000123456789A", "91310000123456789B"))
    ]
    return root, companies, bundle(2)


def test_cli_offline_upgrade_retries_completed_company_and_catalog_last(
    tmp_path, monkeypatch, capsys
):
    root, companies, target = _fixture(tmp_path)
    monkeypatch.setattr(offline_upgrade, "production_bundle", lambda: target)

    def interrupt(point):
        if point == "after_ddl" and interrupt.calls:
            raise RuntimeError("synthetic interruption")
        if point == "after_ddl":
            interrupt.calls += 1

    interrupt.calls = 0
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        offline_upgrade.upgrade_root(root, fault=interrupt)
    with pytest.raises(KernelError, match="版本不受当前程序支持"):
        offline_upgrade.check_root_current(root, bundle=target)
    monkeypatch.setattr(sys, "argv", ["finance-local", "--root", str(root), "upgrade"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "upgraded"
    assert [item["status"] for item in result["companies"]] == ["verified_skip", "upgraded"]
    assert result["catalog"] == "upgraded"
    offline_upgrade.check_root_current(root, bundle=target)
    cli.main()
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["catalog"] == "verified_skip"
    assert all(item["status"] == "verified_skip" for item in repeated["companies"])


def test_active_lock_and_incomplete_operation_reject_before_changes(tmp_path):
    root, companies, target = _fixture(tmp_path)
    with private_file_lock(root / ".resident.lock") as acquired:
        assert acquired
        with pytest.raises(KernelError, match="stop"):
            offline_upgrade.upgrade_root(root, bundle=target)
    catalog = Catalog(
        root,
        load_bundle(
            target.registry,
            tmp_path / "contracts",
            family=TEST_FAMILY,
            application_id=APPLICATION_ID,
            status="released",
            current_versions={"company": 1, "catalog": 1},
            steps=target.steps,
            company_verifiers=target.company_verifiers,
        ),
    )
    with catalog.connection() as connection:
        connection.execute(
            "INSERT INTO company_operation(id,taxpayer_id,kind,payload,status) "
            "VALUES('pending','91310000123456789C','create','{}','pending')"
        )
    with pytest.raises(KernelError, match="未完成"):
        offline_upgrade.upgrade_root(root, bundle=target)


def test_target_content_failure_rolls_back_company_and_source_verifier_is_required(tmp_path):
    root, companies, target = _fixture(tmp_path)

    def reject_target(connection, bundle):
        raise KernelError("synthetic_content_failure", "合成目标内容损坏")

    bad_target = replace(
        target,
        company_verifiers=MappingProxyType(
            {
                1: verify_current_company,
                2: reject_target,
            }
        ),
    )
    with pytest.raises(Exception, match="合成目标内容损坏"):
        offline_upgrade.upgrade_root(root, bundle=bad_target)
    source = replace(target, current_versions=MappingProxyType({"company": 1, "catalog": 1}))
    catalog = Catalog(root, source)
    with catalog.bind(companies[0]["id"]).connection(read_only=True):
        pass
    missing_source = replace(
        target, company_verifiers=MappingProxyType({2: verify_current_company})
    )
    with pytest.raises(Exception, match="No content verifier"):
        offline_upgrade.upgrade_root(root, bundle=missing_source)


def test_catalog_failure_preserves_upgraded_companies_for_retry(tmp_path):
    root, companies, target = _fixture(tmp_path)

    def interrupt(point):
        if point == "after_ddl" and interrupt.calls == 2:
            raise RuntimeError("catalog synthetic interruption")
        if point == "after_ddl":
            interrupt.calls += 1

    interrupt.calls = 0
    with pytest.raises(RuntimeError, match="catalog synthetic interruption"):
        offline_upgrade.upgrade_root(root, bundle=target, fault=interrupt)
    result = offline_upgrade.upgrade_root(root, bundle=target)
    assert result["catalog"] == "upgraded"
    assert all(item["status"] == "verified_skip" for item in result["companies"])


def _source_forward_bundles(tmp_path, source_registry, current_input_registry):
    """Index-only TEST_FAMILY contracts retain the exact source semantic registry.

    Both versions use the current physical codec. The target input registry is
    deliberately different; it cannot interpret retained source facts on behalf
    of either explicitly registered content verifier.
    """
    directory = tmp_path / "contracts"
    steps = []
    for kind, sql, added in (
        (
            "company",
            schema_sql(source_registry),
            "CREATE INDEX synthetic_source_forward ON fact_revision(id)",
        ),
        ("catalog", catalog_sql(), "CREATE INDEX synthetic_catalog_forward ON company(id)"),
    ):
        source = full_contract(sql, kind=kind, version=1, status="released")
        target = full_contract(sql + added + ";", kind=kind, version=2, status="released")
        write_contract(directory, source)
        write_contract(
            directory,
            {key: value for key, value in target.items() if key != "objects"}
            | {
                "base_version": 1,
                "base_sha256": source["sha256"],
                "add": [item for item in target["objects"] if item not in source["objects"]],
                "remove": [],
                "replace": [],
            },
        )
        steps.append(
            MigrationStep(
                TEST_FAMILY,
                kind,
                1,
                source["sha256"],
                2,
                target["sha256"],
                lambda connection, statement=added: connection.execute(statement),
                lambda connection: None,
            )
        )
    verified_versions = []

    def verifier(version):
        def verify(connection, bundle):
            verified_versions.append(version)
            return verify_company_with_registry(connection, bundle, source_registry)

        return verify

    verifiers = {1: verifier(1), 2: verifier(2)}

    def bundle(registry, version):
        return load_bundle(
            registry,
            directory,
            family=TEST_FAMILY,
            application_id=APPLICATION_ID,
            status="released",
            current_versions={"company": version, "catalog": version},
            steps=tuple(steps),
            company_verifiers=verifiers,
        )

    return bundle(source_registry, 1), bundle(current_input_registry, 2), verified_versions


def _source_state(store, source_registry):
    with store.connection(read_only=True) as connection:
        assert (
            verify_company_with_registry(connection, store.bundle, source_registry)["status"]
            == "verified"
        )
        manifests = tuple(
            close_storage.decode_close(connection, row)
            for row in connection.execute("SELECT * FROM period_close ORDER BY period")
        )
        assert manifests
        assert all(manifest["format_version"] == 5 for manifest in manifests)
        return _retained_history_digest(connection), manifests, tuple(connection.iterdump())


def _assert_restored_source_state(restored, original):
    """Every original source remains exact; restore appends one derived reset."""
    assert restored[:2] == original[:2]
    before, after = Counter(original[2]), Counter(restored[2])
    assert not (before - after), "\n".join(unified_diff(original[2], restored[2]))
    added = list((after - before).elements())
    assert len(added) == 1
    assert added[0].startswith('INSERT INTO "dashboard_projection_reset" VALUES(')
    assert ",'restore'," in added[0]


def _portable_source_copy(store, source_registry, directory, destination):
    before = _source_state(store, source_registry)
    created = create_portable(store.path, directory, _bundle=store.bundle)
    archive = Path(created["path"])
    archive_bytes = archive.read_bytes()
    verified = verify_portable(archive, _bundle=store.bundle)
    assert verified["database_format"]["family"] == TEST_FAMILY
    assert verified["database_format"]["version"] == store.bundle.current_versions["company"]
    assert verified["verification"]["status"] == "verified"
    assert not destination.exists()
    restored = restore_portable(
        archive,
        destination,
        expected_company_id=store.company_id,
        expected_database_id=store.database_id,
        _bundle=store.bundle,
    )
    assert restored["verification"]["status"] == "verified"
    copy = Store(destination, store.bundle, store.company_id, store.database_id)
    _assert_restored_source_state(_source_state(copy, source_registry), before)
    assert _source_state(store, source_registry) == before
    assert archive.read_bytes() == archive_bytes
    return archive, archive_bytes, before


def _forward_lifecycle(
    tmp_path,
    root,
    company,
    engine,
    target,
    source_registry,
    verified_versions,
    monkeypatch,
    *,
    watched_models,
):
    """Retain frozen bytes and independent ZIPs across a synthetic forward step."""
    decoded = set()
    original_decode = storage.decode_fields
    watched_kinds = {model.kind for model in watched_models}

    def observed_decode(model, values):
        if getattr(model, "kind", None) in watched_kinds:
            decoded.add(model)
        return original_decode(model, values)

    monkeypatch.setattr(storage, "decode_fields", observed_decode)
    source_archive, source_bytes, before = _portable_source_copy(
        engine.store, source_registry, tmp_path / "source-backup", tmp_path / "source-copy.sqlite"
    )
    offline_upgrade._inspect_root(
        root, target, require_current=False, check_content=True, require_completed=True
    )
    monkeypatch.setattr(offline_upgrade, "production_bundle", lambda: target)
    monkeypatch.setattr(sys, "argv", ["finance-local", "--root", str(root), "upgrade"])
    cli.main()
    target_store = Store(
        engine.store.path, target, engine.store.company_id, engine.store.database_id
    )
    assert _source_state(target_store, source_registry)[:2] == before[:2]
    with target_store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE name='synthetic_source_forward'"
        ).fetchone()
    _portable_source_copy(
        target_store, source_registry, tmp_path / "target-backup", tmp_path / "target-copy.sqlite"
    )
    offline_upgrade.check_root_current(root, bundle=target)
    result = offline_upgrade.upgrade_root(root, bundle=target)
    assert result["companies"][0]["status"] == "verified_skip"
    assert _source_state(target_store, source_registry)[:2] == before[:2]
    assert source_archive.read_bytes() == source_bytes
    assert (
        verify_portable(source_archive, _bundle=engine.store.bundle)["verification"]["status"]
        == "verified"
    )
    # The source package still restores at its declared source version after
    # the active root has advanced; no production old-format bridge is used.
    destination = tmp_path / "source-after-forward.sqlite"
    restore_portable(source_archive, destination, _bundle=engine.store.bundle)
    copy = Store(
        destination, engine.store.bundle, engine.store.company_id, engine.store.database_id
    )
    _assert_restored_source_state(_source_state(copy, source_registry), before)
    assert set(verified_versions) == {1, 2}
    assert decoded == set(watched_models)
    assert company["id"] == target_store.company_id


def test_synthetic_forward_preserves_frozen_source_when_current_input_model_changes(
    tmp_path, monkeypatch, capsys
):
    old_registry = Registry()
    old_registry.register(Source)
    old_registry.register(Charge, calculate)

    class ChangedSource(Fact):
        kind: ClassVar[str] = "test_source"
        amount: bool

    new_registry = Registry()
    new_registry.register(ChangedSource)
    new_registry.register(Charge, calculate)
    old, target, verified_versions = _source_forward_bundles(tmp_path, old_registry, new_registry)
    root = tmp_path / "root"
    catalog = Catalog(root, old)
    company = catalog.create_company("91310000123456789A", "合成冻结采用测试")
    engine = Engine(catalog.bind(company["id"]))
    proof = evidence(engine)
    ready(engine, proof, first="2026-01", last="2026-01")
    save(engine, subject="source", kind="test_source", amount=25, request="save-source")
    save(engine)
    publish(engine)
    close_months(Periods(engine), proof, first="2026-01", last="2026-01")
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] > 0
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM calculation_publication").fetchone()[0] > 0
        assert (
            connection.execute("SELECT count(*) FROM report_open_contribution_anchor").fetchone()[0]
            > 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM read_index_source WHERE source_kind='audit'"
            ).fetchone()[0]
            > 0
        )
        flow = json.loads(
            connection.execute("SELECT content FROM report_period_flow").fetchone()[0]
        )
        assert flow["profit"]["14"] == 125
        with pytest.raises(KernelError):
            verify_company_with_registry(connection, old, new_registry)
    monkeypatch.setattr(service, "default_registry", lambda: new_registry)
    _forward_lifecycle(
        tmp_path,
        root,
        company,
        engine,
        target,
        old_registry,
        verified_versions,
        monkeypatch,
        watched_models=(Source, Charge),
    )
    assert json.loads(capsys.readouterr().out)["companies"][0]["status"] == "upgraded"


def test_v1_content_contract_can_build_all_frozen_storage_decoders(tmp_path, monkeypatch):
    current = service.default_registry()
    contract_dir = tmp_path / "schema_contracts"
    contract_dir.mkdir()
    (contract_dir / "content-v1.json").write_text(
        json.dumps(content_v1.content_contract(current), ensure_ascii=False), "utf-8"
    )
    monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))
    monkeypatch.setattr(service, "default_registry", Registry)
    content_v1.v1_registry.cache_clear()
    try:
        frozen = content_v1.v1_registry()
        assert set(frozen.models) == set(current.models)
        assert all(frozen.models[kind] is not model for kind, model in current.models.items())
    finally:
        content_v1.v1_registry.cache_clear()


def test_v1_storage_decoder_keeps_old_base_month_and_actual_day(tmp_path, monkeypatch):
    from ai_accounting.kernel.types import ActualDate, YearMonth

    current = service.default_registry()
    contract_dir = tmp_path / "schema_contracts"
    contract_dir.mkdir()
    (contract_dir / "content-v1.json").write_text(
        json.dumps(content_v1.content_contract(current), ensure_ascii=False), "utf-8"
    )
    monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))
    content_v1.v1_registry.cache_clear()
    try:
        frozen = content_v1.v1_registry().models["bank_statement"]

        def changed_v2_input(*_args):
            raise AssertionError("v1 decoder used a future input type")

        monkeypatch.setattr(YearMonth, "__new__", changed_v2_input)
        monkeypatch.setattr(YearMonth, "from_ordinal", changed_v2_input)
        monkeypatch.setattr(ActualDate, "__new__", changed_v2_input)
        monkeypatch.setattr(Fact, "model_validate", changed_v2_input)
        statement = frozen.model_validate(
            {
                "period": "2026-01",
                "bank_account_id": "bank-1",
                "opening_fen": 0,
                "closing_fen": 10,
                "entries": [
                    {
                        "reference": "row-1",
                        "actual_date": "2026-01-03",
                        "signed_fen": 10,
                        "description": None,
                    }
                ],
            }
        )
        assert type(statement.period) is content_v1._V1YearMonth
        assert type(statement.entries[0].actual_date) is content_v1._V1ActualDate
        assert statement.entries[0].actual_date.period == statement.period
        assert statement.period.ordinal == (2026 - 1) * 12
        columns = dict.fromkeys(frozen._v1_fields)
        columns["period"] = statement.period.ordinal
        assert content_v1.decode_v1_fields(frozen, columns)["period"] == "2026-01"
    finally:
        content_v1.v1_registry.cache_clear()


def test_synthetic_forward_keeps_corrected_sources_with_changed_current_expense_model(
    tmp_path, monkeypatch
):
    from ai_accounting.kernel.domains.transactions import Expense

    old_registry = service.default_registry()

    class ChangedExpense(Expense):
        def scopes(self):
            return ("changed-v2-scope",)

    new_registry = Registry()
    new_registry.models = dict(old_registry.models)
    new_registry.models["expense"] = ChangedExpense
    old, target, verified_versions = _source_forward_bundles(tmp_path, old_registry, new_registry)
    root = tmp_path / "root"
    catalog = Catalog(root, old)
    company = catalog.create_company("91310000123456789A", "合成身份纠错前向合同")
    engine = Engine(catalog.bind(company["id"]))
    proof = evidence(engine)
    opening_package(engine, proof, [])
    entities = Entities(engine)
    first = entities.register_entity("person", {}, source="synthetic", request_id="person-a")[
        "entity_id"
    ]
    second = entities.register_entity("person", {}, source="synthetic", request_id="person-b")[
        "entity_id"
    ]
    data = save_expense(engine, proof, first)
    confirm_identity(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="expense",
                    expected_revision=1,
                    action="reassign",
                    data={**data, "counterparty_id": second},
                )
            ],
            evidence=[proof],
            reason="confirmed synthetic identity",
        ),
    )
    source_proof = engine.register_evidence(
        b"name,amount,period\nexpense,1.00,2026-01\n",
        "text/csv",
        "expense.csv",
        request_id="expense-source",
    )["digest"]
    materials = Materials(engine)
    source = materials.receive(
        "source",
        dict(
            period="2026-01",
            evidence_digest=source_proof,
            category="transactions",
            purpose="business",
            specification=csv_spec(),
        ),
        evidence=(source_proof, proof),
        expected_revision=0,
        request_id="material-source",
    )
    with engine.store.connection(read_only=True) as connection:
        calculation_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='expense'"
        ).fetchone()[0]
    materials.resolve(
        "resolve",
        dict(
            period="2026-01",
            source_id="source",
            source_fact_id=source["fact_id"],
            location="CSV!B2",
            treatment="recognize",
            recognition_period="2026-01",
            links=[
                dict(
                    calculation_id=calculation_id,
                    amount_field="fact.amount_fen",
                    amount_fen=100,
                )
            ],
        ),
        evidence=(proof,),
        expected_revision=0,
        request_id="resolve",
    )
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        active = category == "transactions"
        periods.inventory(
            "2026-01",
            category,
            evidence=[source_proof] if active else [],
            expected=int(active),
            no_business=not active,
            confirmation_evidence=proof,
            request_id="inventory-" + category,
        )
    close_months(periods, proof, first="2026-01", last="2026-01")
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM identity_correction").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1

    with engine.store.connection(read_only=True) as connection:
        fact = engine.store.current_fact(connection, "expense").fact
        assert type(fact) is old_registry.models["expense"]
        assert fact.counterparty_id == second
        assert fact.amount_fen == 100
        changed = ChangedExpense.model_validate(fact.model_dump(mode="json"))
        assert changed.scopes() != fact.scopes()
    monkeypatch.setattr(service, "default_registry", lambda: new_registry)
    _forward_lifecycle(
        tmp_path,
        root,
        company,
        engine,
        target,
        old_registry,
        verified_versions,
        monkeypatch,
        watched_models=(Expense,),
    )


def test_synthetic_forward_keeps_opening_identity_under_changed_current_scopes_and_reads(
    tmp_path, monkeypatch, capsys
):
    from test_payroll import opening as payroll_opening

    from ai_accounting.kernel.domains.opening import OpeningCash, OpeningPayrollState
    from ai_accounting.kernel.identity_corrections import (
        OpeningBasisCorrection,
        OpeningIdentityBinding,
    )

    old_registry = service.default_registry()
    # This isolated source installation explicitly predates retirement of the
    # cumulative opening grammar. It stores confirmed historical totals only;
    # the detail evaluator reads the adopted package without a PIT algorithm.
    from ai_accounting.kernel.domains.opening import calculate_detail

    old_registry.historical_only.remove("opening_payroll_state")
    old_registry.evaluators["opening_payroll_state"] = calculate_detail

    class ChangedOpeningCash(OpeningCash):
        def scopes(self):
            return ("v2-cash-scope",)

    class ChangedOpeningPayrollState(OpeningPayrollState):
        def scopes(self):
            return ("v2-payroll-scope",)

    class ChangedOpeningIdentityBinding(OpeningIdentityBinding):
        def reads(self):
            return ()

    class ChangedOpeningBasisCorrection(OpeningBasisCorrection):
        def reads(self):
            return ()

    new_registry = Registry()
    new_registry.models = dict(old_registry.models)
    new_registry.models.update(
        opening_cash=ChangedOpeningCash,
        opening_payroll_state=ChangedOpeningPayrollState,
        opening_identity_binding=ChangedOpeningIdentityBinding,
        opening_basis_correction=ChangedOpeningBasisCorrection,
    )
    old, target, verified_versions = _source_forward_bundles(tmp_path, old_registry, new_registry)
    root = tmp_path / "root"
    catalog = Catalog(root, old)
    company = catalog.create_company("91310000123456789A", "合成期初累计冻结前向合同")
    engine = Engine(catalog.bind(company["id"]))
    proof = evidence(engine)
    entities = Entities(engine)
    cash_ids = [
        entities.register_entity(
            "fund_account",
            {"display_name": f"合成现金 {index}"},
            account_type="cash",
            source="synthetic",
            evidence_digest=proof,
            request_id=f"cash-{index}",
        )["entity_id"]
        for index in range(2)
    ]
    people = [
        entities.register_entity("person", {}, source="synthetic", request_id=f"person-{index}")[
            "entity_id"
        ]
        for index in range(2)
    ]
    payroll_fields = payroll_opening(employee_id=people[0]).model_dump(mode="json")
    payroll_fields.pop("period")
    payroll_fields["separate_method_already_used"] = False
    opening_package(
        engine,
        proof,
        [
            ("opening_cash", "cash-original", dict(cash_account_id=cash_ids[0], balance_fen=100)),
            ("opening_cash", "cash-retained", dict(cash_account_id=cash_ids[1], balance_fen=120)),
            (
                "opening_equity",
                "capital-original",
                dict(
                    equity_kind="paid_in_capital",
                    balance_fen=100,
                    holder_or_basis_id=people[0],
                ),
            ),
            (
                "opening_equity",
                "capital-retained",
                dict(
                    equity_kind="paid_in_capital",
                    balance_fen=120,
                    holder_or_basis_id=people[1],
                ),
            ),
            ("opening_payroll_state", "payroll", payroll_fields),
        ],
    )
    _close_without_current_business(engine, "2026-01", proof)
    confirm_identity(
        engine,
        dict(
            changes=[
                dict(
                    subject_id="cash-original",
                    expected_revision=1,
                    action="supersede",
                    replacement_subject_id="cash-retained",
                ),
                dict(
                    subject_id="capital-original",
                    expected_revision=1,
                    action="supersede",
                    replacement_subject_id="capital-retained",
                ),
                dict(
                    subject_id="payroll",
                    expected_revision=1,
                    action="reassign",
                    data={
                        **payroll_fields,
                        "period": "2026-01",
                        "package_id": "opening",
                        "employee_id": people[1],
                    },
                ),
            ],
            evidence=[proof],
            reason="confirmed frozen source identities",
            posting_period="2026-02",
        ),
    )
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM identity_correction_item").fetchone()[0] == 3
        )
        assert (
            connection.execute("SELECT count(*) FROM fact_opening_basis_correction").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM calculation_publication").fetchone()[0] > 0

    with engine.store.connection(read_only=True) as connection:
        source_cash = engine.store.current_fact(connection, "cash-retained").fact
        assert type(source_cash) is OpeningCash
        assert source_cash.balance_fen == 120
        assert (
            ChangedOpeningCash.model_validate(source_cash.model_dump(mode="json")).scopes()
            != source_cash.scopes()
        )
        source_payroll = engine.store.current_fact(connection, "payroll").fact
        assert source_payroll.employee_id == people[0]
        assert (
            ChangedOpeningPayrollState.model_validate(
                source_payroll.model_dump(mode="json")
            ).scopes()
            != source_payroll.scopes()
        )
        correction_models = {
            "opening_identity_binding": ChangedOpeningIdentityBinding,
            "opening_basis_correction": ChangedOpeningBasisCorrection,
        }
        corrected_kinds = set()
        for row in connection.execute(
            "SELECT f.fact_id,s.kind FROM fact_current f JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind IN ('opening_identity_binding','opening_basis_correction')"
        ):
            fact = engine.store.fact(connection, row["fact_id"]).fact
            assert type(fact) is old_registry.models[row["kind"]]
            changed = correction_models[row["kind"]].model_validate_json(fact.model_dump_json())
            assert fact.reads()
            assert changed.reads() == ()
            corrected_kinds.add(row["kind"])
        assert corrected_kinds == set(correction_models)
        with pytest.raises(KernelError):
            verify_company_with_registry(connection, old, new_registry)
    scope_calls = []
    original_scopes = OpeningCash.scopes

    def source_scopes(self):
        scope_calls.append(self.cash_account_id)
        return original_scopes(self)

    monkeypatch.setattr(OpeningCash, "scopes", source_scopes)
    monkeypatch.setattr(service, "default_registry", lambda: new_registry)
    _forward_lifecycle(
        tmp_path,
        root,
        company,
        engine,
        target,
        old_registry,
        verified_versions,
        monkeypatch,
        watched_models=(
            OpeningCash,
            OpeningPayrollState,
            OpeningIdentityBinding,
            OpeningBasisCorrection,
        ),
    )
    assert scope_calls
    assert set(scope_calls) == set(cash_ids)
    assert json.loads(capsys.readouterr().out)["companies"][0]["status"] == "upgraded"
