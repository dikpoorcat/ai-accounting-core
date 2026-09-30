"""The CLI's offline path uses released synthetic contracts and the resident lock."""

import json
import sys
from dataclasses import replace
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

from ai_accounting.kernel import cli, content_v1, offline_upgrade, query_reads, service
from ai_accounting.kernel.catalog import Catalog, catalog_sql
from ai_accounting.kernel.contracts import Fact, KernelError, Registry
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.migration_steps import MigrationStep
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.runtime import private_file_lock
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, load_bundle, verify_current_company


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


def test_nonempty_v1_frozen_adoption_survives_changed_v2_current_model(
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
    directory = tmp_path / "contracts"
    steps = []
    for kind, source_sql, added_sql in (
        (
            "company",
            schema_sql(old_registry),
            "CREATE INDEX stage9_company_old_source ON fact_revision(subject_id)",
        ),
        ("catalog", catalog_sql(), "CREATE INDEX stage9_catalog_old_source ON company(name)"),
    ):
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

    def bundle(registry, version, verifiers):
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

    old = bundle(old_registry, 1, {1: verify_current_company})
    root = tmp_path / "root"
    catalog = Catalog(root, old)
    company = catalog.create_company("91310000123456789A", "冻结采用测试")
    engine = Engine(catalog.bind(company["id"]))
    proof = evidence(engine)
    ready(engine, proof, first="2026-01", last="2026-01")
    save(engine)
    publish(engine)
    close_months(Periods(engine), proof, first="2026-01", last="2026-01")
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] > 0
        assert connection.execute("SELECT count(*) FROM period_close").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM calculation_publication").fetchone()[0] > 0
        assert connection.execute(
            "SELECT count(*) FROM report_open_contribution_anchor"
        ).fetchone()[0] > 0
        assert connection.execute(
            "SELECT count(*) FROM read_index_source WHERE source_kind='audit'"
        ).fetchone()[0] > 0
        flow = json.loads(
            connection.execute("SELECT content FROM report_period_flow").fetchone()[0]
        )
        assert flow["profit"]["14"] != 0

    contract_dir = tmp_path / "schema_contracts"
    contract_dir.mkdir()
    (contract_dir / "content-v1.json").write_text(
        json.dumps(content_v1.content_contract(old_registry), ensure_ascii=False), "utf-8"
    )
    monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))
    monkeypatch.setattr(service, "default_registry", lambda: new_registry)
    content_v1.v1_registry.cache_clear()
    try:
        target = bundle(
            new_registry,
            2,
            {1: content_v1.verify_v1_company, 2: content_v1.verify_v1_company},
        )
        offline_upgrade._inspect_root(
            root,
            target,
            require_current=False,
            check_content=True,
            require_completed=True,
        )
        monkeypatch.setattr(offline_upgrade, "production_bundle", lambda: target)
        monkeypatch.setattr(sys, "argv", ["finance-local", "--root", str(root), "upgrade"])
        cli.main()
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "upgraded"
        assert result["companies"][0]["status"] == "upgraded"
        from ai_accounting.kernel import (
            close_contract,
            close_review,
            close_storage,
            content_history_context,
            contracts,
            dashboard,
            period_balance_freeze,
            projections,
            publication,
            query_semantics,
            read_indexes,
            report_flow,
            report_open_contribution,
            report_projection,
            report_semantics,
            reports,
            settlement_freeze,
            settlement_projection,
            storage,
            types,
        )

        def future_reader(*_args, **_kwargs):
            raise AssertionError("v1 content must not use the future close reader")

        with monkeypatch.context() as future:
            future.setattr(close_storage, "decode_close", future_reader)
            future.setattr(close_storage, "verified_header", future_reader)
            future.setattr(close_contract, "require_close_contract", future_reader)
            future.setattr(close_review, "verify_owner_review_integrity", future_reader)
            future.setattr(dashboard, "_position", future_reader)
            future.setattr(dashboard._Snapshot, "__init__", future_reader)
            future.setattr(query_semantics, "classify_financial_position", future_reader)
            future.setattr(settlement_projection, "settlement_position_rows", future_reader)
            future.setattr(settlement_projection, "compare_settlement_projection", future_reader)
            future.setattr(settlement_projection, "expected_settlement_projection", future_reader)
            future.setattr(publication, "active_tranches", future_reader)
            future.setattr(publication, "verify_record", future_reader)
            future.setattr(period_balance_freeze, "compare_balance_freeze", future_reader)
            future.setattr(storage, "decode_fields", future_reader)
            future.setattr(storage, "_stored_sequence", future_reader)
            future.setattr(types.YearMonth, "from_ordinal", classmethod(future_reader))
            future.setattr(contracts, "Read", future_reader)
            future.setattr(report_projection, "compare_report_projection", future_reader)
            future.setattr(report_projection, "require_report_projection", future_reader)
            future.setattr(report_flow, "compare_report_flow", future_reader)
            future.setattr(report_flow, "require_report_flow", future_reader)
            future.setattr(report_open_contribution, "prepare_open_contribution", future_reader)
            future.setattr(report_open_contribution, "compare_open_contributions", future_reader)
            future.setattr(query_semantics, "resolve_calculation_relations", future_reader)
            future.setattr(report_semantics, "require_report_semantics", future_reader)
            future.setattr(report_semantics, "compare_report_semantics", future_reader)
            future.setattr(report_semantics, "compact_line_fields", future_reader)
            future.setattr(report_semantics, "immutable_line_fields", future_reader)
            future.setattr(reports, "_profit_rows", future_reader)
            future.setattr(reports, "_cash_rows", future_reader)
            future.setattr(settlement_freeze, "require_frozen_settlement_projection", future_reader)
            future.setattr(settlement_freeze, "_read_root", future_reader)
            future.setattr(settlement_freeze, "_build_prepared", future_reader)
            offline_upgrade.check_root_current(root, bundle=target)
            offline_upgrade.upgrade_root(root, bundle=target)
            cli.main()
            assert json.loads(capsys.readouterr().out)["companies"][0]["status"] == "verified_skip"
        target_engine = Engine(Catalog(root, target).bind(company["id"]))
        with target_engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            with monkeypatch.context() as future:
                future.setattr(types, "canonical", future_reader)
                future.setattr(types, "digest", future_reader)
                future.setattr(types, "checked", future_reader)
                future.setattr(types, "sum_fen", future_reader)
                assert projections.checked is content_history_context.source_checked
                assert projections.canonical is content_history_context.source_canonical
                assert read_indexes.canonical is content_history_context.source_canonical
                content_v1.verify_v1_company(connection, target)
            from ai_accounting.kernel import close_storage_v1, report_flow_v1

            close = connection.execute("SELECT * FROM period_close").fetchone()
            manifest = close_storage_v1.decode_close(connection, close)
            wrong_row = dict(close) | {"digest": bytes(32)}
            with pytest.raises(KernelError, match="关账集合不一致"):
                report_flow_v1.compare_report_flow(
                    target_engine, connection, _verified_closes=((wrong_row, manifest),)
                )
    finally:
        content_v1.v1_registry.cache_clear()


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


def test_v1_identity_correction_is_verified_after_current_expense_model_changes(
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
    directory = tmp_path / "contracts"
    steps = []
    for kind, sql, added in (
        (
            "company",
            schema_sql(old_registry),
            "CREATE INDEX stage9_v2_identity ON fact_revision(id)",
        ),
        ("catalog", catalog_sql(), "CREATE INDEX stage9_v2_catalog ON company(id)"),
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

    def bundle(registry, version, verifiers):
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

    root = tmp_path / "root"
    catalog = Catalog(root, bundle(old_registry, 1, {1: verify_current_company}))
    company = catalog.create_company("91310000123456789A", "身份纠错旧版")
    engine = Engine(catalog.bind(company["id"]))
    proof = evidence(engine)
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
        adopted = engine.store.current_fact(connection, "expense")
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
                    subject_id="expense",
                    fact_kind="expense",
                    fact_id=adopted.id,
                    calculation_id=calculation_id,
                    amount_field="fact.amount_fen",
                    amount_fen=100,
                    recognition_period="2026-01",
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

    contract_dir = tmp_path / "schema_contracts"
    contract_dir.mkdir()
    (contract_dir / "content-v1.json").write_text(
        json.dumps(content_v1.content_contract(old_registry), ensure_ascii=False), "utf-8"
    )
    from ai_accounting.kernel.entity_references import DECLARATIONS

    monkeypatch.setitem(DECLARATIONS, "expense", [])
    monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))
    monkeypatch.setattr(service, "default_registry", lambda: new_registry)
    content_v1.v1_registry.cache_clear()
    try:
        target = bundle(
            new_registry,
            2,
            {
                1: content_v1.verify_v1_company,
                2: content_v1.verify_v1_company,
            },
        )
        result = offline_upgrade.upgrade_root(root, bundle=target)
        assert result["companies"][0]["status"] == "upgraded"
    finally:
        content_v1.v1_registry.cache_clear()


def test_v1_frozen_opening_and_cumulative_identity_survive_changed_v2_methods_and_helpers(
    tmp_path, monkeypatch, capsys
):
    from test_payroll import opening as payroll_opening

    from ai_accounting.kernel import (
        entity_references,
        identity_corrections,
        integrity,
        opening_adoption,
        periods,
        storage,
        types,
    )
    from ai_accounting.kernel.contracts import Fact
    from ai_accounting.kernel.domains.opening import OpeningCash, OpeningPayrollState
    from ai_accounting.kernel.identity_corrections import (
        OpeningBasisCorrection,
        OpeningIdentityBinding,
    )

    old_registry = service.default_registry()

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
    directory = tmp_path / "contracts"
    steps = []
    for kind, sql, added in (
        (
            "company",
            schema_sql(old_registry),
            "CREATE INDEX stage9_v2_opening ON fact_revision(id)",
        ),
        ("catalog", catalog_sql(), "CREATE INDEX stage9_v2_opening_catalog ON company(id)"),
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

    def bundle(registry, version, verifiers):
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

    root = tmp_path / "root"
    catalog = Catalog(root, bundle(old_registry, 1, {1: verify_current_company}))
    company = catalog.create_company("91310000123456789A", "期初累计冻结升级")
    engine = Engine(catalog.bind(company["id"]))
    proof = evidence(engine)
    entities = Entities(engine)
    cash_ids = [
        entities.register_entity(
            "fund_account", {}, account_type="cash", source="synthetic", request_id=f"cash-{index}"
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

    contract_dir = tmp_path / "schema_contracts"
    contract_dir.mkdir()
    (contract_dir / "content-v1.json").write_text(
        json.dumps(content_v1.content_contract(old_registry), ensure_ascii=False), "utf-8"
    )
    monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))
    monkeypatch.setattr(service, "default_registry", lambda: new_registry)

    def current_binding_must_not_run(*_args):
        raise AssertionError("v1 verification used the current correction calculator")

    monkeypatch.setattr(
        identity_corrections, "calculate_opening_binding", current_binding_must_not_run
    )
    monkeypatch.setattr(
        identity_corrections, "calculate_opening_basis_correction", current_binding_must_not_run
    )
    monkeypatch.setattr(entity_references, "_at", current_binding_must_not_run)
    monkeypatch.setattr(entity_references, "_current_bindings", current_binding_must_not_run)
    monkeypatch.setattr(identity_corrections, "_assignment_data", current_binding_must_not_run)
    monkeypatch.setattr(identity_corrections, "_entity_changes", current_binding_must_not_run)
    monkeypatch.setattr(Fact, "scopes_for", current_binding_must_not_run)
    monkeypatch.setattr(Fact, "reads_for", current_binding_must_not_run)
    monkeypatch.setattr(opening_adoption, "_package_contract", current_binding_must_not_run)
    monkeypatch.setattr(opening_adoption, "_detail_shape", current_binding_must_not_run)
    monkeypatch.setattr(periods, "MATERIAL_CATEGORIES", ("future_v2_category",))
    monkeypatch.setattr(storage, "_field_codecs", current_binding_must_not_run)
    monkeypatch.setattr(storage, "sequence_model", current_binding_must_not_run)
    monkeypatch.setattr(integrity, "sequence_model", current_binding_must_not_run)
    for method in ("facts", "fact_versions", "calculation", "calculations", "relations_many"):
        monkeypatch.setattr(query_reads.QueryReads, method, current_binding_must_not_run)
    content_v1.v1_registry.cache_clear()
    try:
        target = bundle(
            new_registry,
            2,
            {
                1: content_v1.verify_v1_company,
                2: content_v1.verify_v1_company,
            },
        )
        monkeypatch.setattr(offline_upgrade, "production_bundle", lambda: target)
        monkeypatch.setattr(sys, "argv", ["finance-local", "--root", str(root), "upgrade"])
        cli.main()
        assert json.loads(capsys.readouterr().out)["companies"][0]["status"] == "upgraded"
        target_engine = Engine(Catalog(root, target).bind(company["id"]))
        with target_engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            with monkeypatch.context() as future:
                future.setattr(types, "canonical", current_binding_must_not_run)
                future.setattr(types, "digest", current_binding_must_not_run)
                future.setattr(types, "checked", current_binding_must_not_run)
                future.setattr(types, "sum_fen", current_binding_must_not_run)
                future.setattr(OpeningIdentityBinding, "kind", "future_opening_binding")
                future.setattr(OpeningBasisCorrection, "kind", "future_opening_basis")
                content_v1.verify_v1_company(connection, target)
        cli.main()
        assert json.loads(capsys.readouterr().out)["companies"][0]["status"] == "verified_skip"
    finally:
        content_v1.v1_registry.cache_clear()
