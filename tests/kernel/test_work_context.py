"""One bounded discovery snapshot reuses sources without deciding readiness."""

from contextlib import contextmanager

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.discovery import Discovery
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.work_context import WorkContext


@pytest.fixture
def company(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite", production_bundle(), "company", "911100000000000001", "db"
        )
    )
    engine.proof = engine.register_evidence(
        b"owner confirmation", "text/plain", "confirmation.txt", request_id="proof"
    )["digest"]
    engine.vendor = Entities(engine).register_entity(
        "organization",
        {"display_name": "Vendor", "note": "provided background"},
        source="owner",
        request_id="vendor",
        evidence_digest=engine.proof,
    )["entity_id"]
    return engine


def expense(engine, name, period="2026-02", *, evidence=None, publish=False):
    engine.expense_sequence = getattr(engine, "expense_sequence", 0) + 1
    saved = engine.save_fact(
        "expense",
        name,
        {
            "period": period,
            "counterparty_id": engine.vendor,
            "amount_fen": 100 + engine.expense_sequence,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        evidence=(evidence or engine.proof,),
        expected_revision=0,
        request_id=name,
    )
    if publish:
        preview = engine.preview([name])
        engine.confirm(
            [name],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="publish-" + name,
        )
    return saved


def source(engine, name, *, received="2026-01", recognition="2026-02", area="transactions"):
    raw = f"name,amount,period\n{name},1.00,{recognition}\n".encode()
    evidence = engine.register_evidence(
        raw, "text/csv", name + ".csv", request_id="evidence-" + name
    )["digest"]
    saved = Materials(engine).receive(
        name,
        {
            "period": received,
            "evidence_digest": evidence,
            "category": area,
            "purpose": "business",
            "specification": {
                "format": "csv",
                "columns": [
                    {"column": "A", "role": "context"},
                    {"column": "B", "role": "amount"},
                    {"column": "C", "role": "recognition_period"},
                ],
            },
        },
        evidence=(evidence, engine.proof),
        expected_revision=0,
        request_id="receive-" + name,
    )
    return saved, evidence


def query(engine, **changes):
    return WorkContext(engine).query(period="2026-02", work_area="transactions", **changes)


def test_facts_company_evidence_objects_and_adoptions_match_existing_reads(company):
    Discovery(company).update_company_note(
        "owner business note", expected_revision=0, request_id="note"
    )
    expense(company, "published", publish=True)
    expense(company, "pending")
    expense(company, "other-month", period="2026-01")
    result = query(company)
    expected = Discovery(company).find_facts(period_from="2026-02", period_to="2026-02")
    for actual, existing in zip(result["items"], expected["items"], strict=True):
        for field in existing.keys() - {"identity_matches"}:
            assert actual[field] == existing[field]
        assert actual["identity_matches"] == [
            {
                "entity_id": company.vendor,
                "role": "counterparty",
                "path": "counterparty_id",
                "identity_match": "current",
            }
        ]
    assert result["company_context"] == Discovery(company).company_context()
    old_object = Entities(company).find_entities()["items"][0]
    assert result["entities"] == [
        {
            "entity_id": old_object["entity_id"],
            "kind": old_object["kind"],
            "account_type": old_object["account_type"],
            "profile": old_object["profile"],
        }
    ]
    assert {item["subject_id"]: item["pending"] for item in result["items"]} == {
        "pending": True,
        "published": False,
    }
    assert result["materials"] == []
    assert result["has_more"] is False


def test_cross_month_and_unknown_material_scopes_do_not_infer_receipt_month(company):
    cross, _ = source(company, "cross-month", received="2026-01", recognition="2026-02")
    unknown, _ = source(company, "unknown-month", received="2025-12", recognition="")
    source(company, "other-known-month", received="2025-12", recognition="2026-03")
    source(company, "other-area", recognition="2026-02", area="tax")
    result = query(company)
    assert {item["source"]["subject_id"] for item in result["materials"]} == {
        "cross-month",
        "unknown-month",
    }
    assert {item["source"]["fact_id"] for item in result["materials"]} == {
        cross["fact_id"],
        unknown["fact_id"],
    }
    assert {item["received_period"] for item in result["materials"]} == {"2026-01", "2025-12"}
    allocations = [item for item in result["items"] if item["kind"] == "material_period_allocation"]
    periods = [
        entry["recognition_period"] for item in allocations for entry in item["data"]["entries"]
    ]
    assert set(periods) == {"2026-02", None}
    assert all("不是核算所属期" in item["period_semantics"] for item in result["materials"])


def test_explicit_source_ids_keep_cross_area_original_without_reclassifying(company):
    saved, _ = source(
        company, "bank-shared", received="2026-01", recognition="2026-01", area="bank"
    )
    assert query(company)["items"] == []
    result = query(company, source_ids=["bank-shared"])
    assert {item["kind"] for item in result["items"]} == {
        "material_source_v2", "material_period_allocation",
    }
    assert result["materials"][0]["source"]["fact_id"] == saved["fact_id"]
    assert result["materials"][0]["source"]["data"]["category"] == "bank"
    assert result["materials"][0]["received_period"] == "2026-01"
    assert result["work_area"] == "transactions"


def test_closed_carry_is_retrievable_by_exact_worklist_source_reference(company):
    original, _ = source(company, "late-closed-source", recognition="2026-01")
    assert query(company)["items"] == []
    result = query(company, source_ids=["late-closed-source"])
    assert result["materials"][0]["source"]["fact_id"] == original["fact_id"]
    assert all(item["period"] == "2026-01" for item in result["items"])


def test_explicit_old_subject_reuses_its_actual_source_and_keeps_historical_refs(company):
    original, evidence = source(company, "older-source", received="2025-12", recognition="2026-01")
    expense(company, "old-expense", "2026-01", evidence=evidence, publish=True)
    result = query(company, subject_ids=["old-expense"])
    old = next(item for item in result["items"] if item["subject_id"] == "old-expense")
    assert old["period"] == "2026-01"
    assert old["adoption"]["current"] is True
    assert old["calculation_dependencies"] is not None
    assert result["materials"][0]["source"]["fact_id"] == original["fact_id"]


def test_page_limit_precedes_body_hydration_and_exact_related_sources(company, monkeypatch):
    for index in range(12):
        expense(company, f"expense-{index}")
    original = Store._fact_data_many_from_headers
    hydrated = []

    def bounded(self, connection, rows):
        hydrated.extend(row["id"] for row in rows)
        return original(self, connection, rows)

    monkeypatch.setattr(Store, "_fact_data_many_from_headers", bounded)
    first = query(company, limit=3)
    assert len(first["items"]) == 3
    assert first["has_more"] is True
    assert set(hydrated) == {item["fact_id"] for item in first["items"]}
    assert "本页没有不等于资料缺失" in first["page_semantics"]
    visited = {item["fact_id"] for item in first["items"]}
    page = first
    while page["next_cursor"]:
        page = query(company, limit=3, cursor=page["next_cursor"])
        new = {item["fact_id"] for item in page["items"]}
        assert not visited & new
        visited.update(new)
    assert len(visited) == 12


@pytest.mark.parametrize("source_count", [1, 25, 75])
def test_repeated_digest_source_growth_does_not_expand_page_bodies(
    company, monkeypatch, source_count
):
    first_source, evidence = source(
        company, "shared-source", received="2025-12", recognition="2026-01"
    )
    data = next(
        item["data"]
        for item in Discovery(company).find_facts()["items"]
        if item["fact_id"] == first_source["fact_id"]
    )
    for index in range(1, source_count):
        Materials(company).receive(
            f"same-digest-{index}",
            data,
            evidence=(evidence, company.proof),
            expected_revision=0,
            request_id=f"same-source-{index}",
        )
    expense(company, "expense-with-shared-original", evidence=evidence)
    original = Store._fact_data_many_from_headers
    hydrated = set()

    def bounded(self, connection, rows):
        hydrated.update(row["id"] for row in rows)
        return original(self, connection, rows)

    monkeypatch.setattr(Store, "_fact_data_many_from_headers", bounded)
    first = query(company, limit=1)
    assert [item["subject_id"] for item in first["items"]] == ["expense-with-shared-original"]
    assert first["materials"] == []
    assert hydrated == {first["items"][0]["fact_id"]}
    assert first["has_more"]
    hydrated.clear()
    second = query(company, limit=1, cursor=first["next_cursor"])
    assert second["items"][0]["kind"] in {"material_source_v2", "material_period_allocation"}
    assert len(second["materials"]) == 1
    assert hydrated <= {
        second["items"][0]["fact_id"],
        second["materials"][0]["source"]["fact_id"],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"period": "2026-03"},
        {"work_area": "tax"},
        {"subject_ids": ["one"]},
        {"source_ids": ["source"]},
    ],
)
def test_cursor_rejects_changed_scope(company, change):
    expense(company, "one")
    expense(company, "two")
    first = query(company, limit=1)
    with pytest.raises(KernelError) as error:
        WorkContext(company).query(
            **(
                {
                    "period": "2026-02",
                    "work_area": "transactions",
                    "limit": 1,
                    "cursor": first["next_cursor"],
                }
                | change
            )
        )
    assert error.value.code == "work_context_cursor_stale"


def test_cursor_rejects_epoch_repair_and_database_identity_changes(company, tmp_path):
    expense(company, "one")
    expense(company, "two")
    first = query(company, limit=1)
    Discovery(company).update_company_note("new note", expected_revision=0, request_id="note")
    with pytest.raises(KernelError, match="版本已变化"):
        query(company, limit=1, cursor=first["next_cursor"])
    first = query(company, limit=1)
    with company.store.connection() as connection:
        connection.execute(
            "UPDATE state SET read_repair_revision=read_repair_revision+1 WHERE id=1"
        )
    with pytest.raises(KernelError, match="版本已变化"):
        query(company, limit=1, cursor=first["next_cursor"])
    first = query(company, limit=1)
    for name, company_id in (("another-database", "company"), ("another-company", "another")):
        other = Engine(
            Store.create(
                tmp_path / (name + ".sqlite"),
                production_bundle(),
                company_id,
                "911100000000000002",
                name,
            )
        )
        with pytest.raises(KernelError, match="版本已变化"):
            query(other, limit=1, cursor=first["next_cursor"])


def test_material_original_exact_version_stays_available_after_mapping_revision(company):
    original, evidence = source(company, "source")
    Materials(company).resolve(
        "source-resolution",
        {
            "period": "2026-01",
            "source_id": "source",
            "source_fact_id": original["fact_id"],
            "location": "CSV!B2",
            "treatment": "no_accounting",
            "reason": "owner confirmed",
            "non_accounting_reason": "not_company_business",
        },
        evidence=(company.proof,),
        expected_revision=0,
        request_id="resolve",
    )
    existing = next(
        item
        for item in Discovery(company).find_facts()["items"]
        if item["fact_id"] == original["fact_id"]
    )
    changed = existing["data"]
    changed["specification"]["columns"][0]["label"] = "owner label"
    revised = Materials(company).receive(
        "source",
        changed,
        evidence=(evidence, company.proof),
        expected_revision=1,
        request_id="remap",
    )
    result = query(company)
    versions = {material["source"]["fact_id"]: material for material in result["materials"]}
    assert set(versions) == {original["fact_id"], revised["fact_id"]}
    assert versions[original["fact_id"]]["source"]["is_current"] is False
    assert (
        versions[original["fact_id"]]["page_related_fact_refs"][0]["subject_id"]
        == "source-resolution"
    )
    assert (
        versions[revised["fact_id"]]["source"]["data"]["specification"]["columns"][0]["label"]
        == "owner label"
    )


def test_one_owned_snapshot_no_public_read_chains_or_business_checkers(company, monkeypatch):
    source(company, "source")
    expense(company, "one")
    connections = []
    original = company.store._snapshot_connection

    @contextmanager
    def count():
        with original() as connection:
            connections.append(connection)
            yield connection

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "aggregator must not invoke independent public reads or business checks"
        )

    monkeypatch.setattr(company.store, "_snapshot_connection", count)
    monkeypatch.setattr(Discovery, "company_context", forbidden)
    monkeypatch.setattr(Discovery, "find_facts", forbidden)
    monkeypatch.setattr(Entities, "find_entities", forbidden)
    from ai_accounting.kernel import materials
    from ai_accounting.kernel.workflow import Workflow
    from ai_accounting.kernel.worklist import Worklist

    monkeypatch.setattr(materials, "inspect_bytes", forbidden)
    monkeypatch.setattr(materials, "check_completeness_many", forbidden)
    monkeypatch.setattr(Worklist, "query", forbidden)
    monkeypatch.setattr(Workflow, "query", forbidden)
    result = query(company)
    assert len(connections) == 1
    assert result["materials"][0]["original"]["media_type"] == "text/csv"
    assert "content" not in result["materials"][0]["original"]


def test_concurrent_change_cannot_mix_company_versions_and_fact_pages(company, monkeypatch):
    expense(company, "before-snapshot")
    original = Discovery._company_context
    changed = False

    def change_after_context(self, connection):
        nonlocal changed
        result = original(self, connection)
        if not changed:
            changed = True
            expense(company, "after-snapshot")
            Discovery(company).update_company_note(
                "updated during read", expected_revision=0, request_id="concurrent-note"
            )
        return result

    monkeypatch.setattr(Discovery, "_company_context", change_after_context)
    first = query(company)
    assert first["company_context"]["company_note"]["revision"] == 0
    assert [item["subject_id"] for item in first["items"]] == ["before-snapshot"]
    second = query(company)
    assert second["company_context"]["company_note"]["revision"] == 1
    assert {item["subject_id"] for item in second["items"]} == {
        "before-snapshot",
        "after-snapshot",
    }


def test_explicit_id_normalization_cursor_validation_and_limit(company):
    expense(company, "one")
    expense(company, "two")
    first = query(company, subject_ids=["two", "one", "one"], limit=1)
    assert query(company, subject_ids=["one", "two"], cursor=first["next_cursor"], limit=1)["items"]
    with pytest.raises(KernelError) as error:
        query(company, cursor="not-a-cursor")
    assert error.value.code == "work_context_cursor_invalid"
    for limit in (0, 501, True):
        with pytest.raises(ValueError):
            query(company, limit=limit)
    with pytest.raises(ValueError):
        query(company, subject_ids=["id"] * 501)


def test_selected_fact_source_seal_and_object_integrity_are_still_checked(company):
    saved = expense(company, "one")
    with company.store.connection() as connection:
        connection.execute(
            "UPDATE discovery_fact_history SET revision=2 WHERE fact_id=?", (saved["fact_id"],)
        )
    with pytest.raises(KernelError) as error:
        query(company)
    assert error.value.code == "content_integrity_failed"
    assert error.value.details["reason"] == "discovery_source_mismatch"


def test_empty_context_does_not_infer_next_work_or_business_fact(company):
    result = query(company)
    assert result["period"] == "2026-02"
    assert result["work_area"] == "transactions"
    assert result["items"] == result["materials"] == result["entities"] == []
    assert result["next_cursor"] is None
    assert "status" not in result
    with pytest.raises(TypeError):
        WorkContext(company).query(work_area="transactions")


def test_payroll_management_month_sources_are_discoverable_without_known_subjects(tmp_path):
    from test_payroll import payroll
    from test_payroll_preparation import company as prepared_company
    from test_payroll_preparation import confirm_no_change, plan_references

    from ai_accounting.kernel import payroll_preparation, tax_import
    from ai_accounting.kernel.domains.payroll import PayrollBounded

    instance = prepared_company(tmp_path)
    confirm_no_change(instance)
    instance.save(
        payroll_preparation.PayrollChangeNotice(
            period="2026-02",
            employee_id="employee",
            changed_fields=("salary",),
        ),
        "notice",
    )
    wage = payroll(period="2026-02")
    instance.save(
        payroll_preparation.PayrollPlan(
            period="2026-02",
            employee_id="employee",
            payroll=wage,
            **plan_references(instance),
        ),
        "regular-plan",
    )
    bounded = PayrollBounded.model_validate(wage.model_dump(mode="json"))
    instance.save(
        payroll_preparation.BoundedPayrollPlan(
            period="2026-02",
            employee_id="employee",
            payroll=bounded,
            **plan_references(instance),
        ),
        "bounded-plan",
    )
    instance.save(
        tax_import.TaxImportMapping(
            period="2026-02",
            pension_code="provided-pension-code",
            medical_code=None,
            unemployment_code=None,
        ),
        "mapping",
    )
    instance.save(
        tax_import.TaxImportDetails(
            period="2026-02",
            employee_id="employee",
            payroll_result_digest=instance.current("january").result_digest,
            cumulative_special_fen={key: 0 for key in tax_import.SPECIAL_COLUMNS},
            current_other_fen={key: 0 for key in tax_import.OTHER_COLUMNS},
            cumulative_personal_pension_fen=0,
            tax_relief_fen=0,
            treaty_relief_fen=0,
        ),
        "details",
    )
    items = WorkContext(instance.engine).query(period="2026-02", work_area="payroll")["items"]
    kinds = {item["kind"] for item in items}
    assert {
        "payroll_plan_v2",
        "payroll_plan_bounded",
        "payroll_change_notice_v2",
        "payroll_no_change_v2",
        "tax_import_mapping_v2",
        "tax_import_details_v2",
    } <= kinds
    assert "profile" not in {item["subject_id"] for item in items}
    assert all(
        item["adoption"] is None
        for item in items
        if item["kind"]
        in {
            "payroll_plan_v2",
            "payroll_plan_bounded",
            "payroll_no_change_v2",
        }
    )


def test_declaration_and_disbursement_use_explicit_tax_month_not_recording_month(tmp_path):
    from test_payroll_preparation import company as prepared_company

    from ai_accounting.kernel.payroll_tax_declarations import (
        PayrollDisbursementBasis,
        PayrollTaxDeclarationActual,
    )

    instance = prepared_company(tmp_path)
    declaration = instance.save(
        PayrollTaxDeclarationActual(
            period="2026-02",
            employee_id="employee",
            tax_period="2026-01",
            income_category="wages",
            declared_tax_fen=instance.current("january").values["tax_fen"],
            declaration_confirmed=True,
            declaration_date="2026-02-03",
        ),
        "february-declaration",
    )
    instance.save(
        PayrollDisbursementBasis(
            period="2026-02",
            employee_id="employee",
            tax_period="2026-01",
            payroll_kind="payroll",
            payroll_id="january",
            declaration_id=declaration["subject_id"],
            declaration_fact_id=declaration["fact_id"],
            use_declared_tax_for_disbursement=True,
        ),
        "february-disbursement",
    )
    january = WorkContext(instance.engine).query(period="2026-01", work_area="payroll")["items"]
    selected = [item for item in january if item["subject_id"].startswith("february-")]
    assert {item["subject_id"] for item in selected} == {
        "february-declaration",
        "february-disbursement",
    }
    assert all(
        item["period"] == "2026-02" and item["data"]["tax_period"] == "2026-01" for item in selected
    )
    february = WorkContext(instance.engine).query(period="2026-02", work_area="payroll")["items"]
    assert not {"february-declaration", "february-disbursement"} & {
        item["subject_id"] for item in february
    }


def test_tax_identity_object_scope_current_semantics_and_page_hydration(company, monkeypatch):
    from ai_accounting.kernel.contracts import Read

    employee = Entities(company).register_entity(
        "person",
        {"display_name": "Employee"},
        source="owner",
        request_id="employee",
    )["entity_id"]
    company.save_fact(
        "payroll_change_notice_v2",
        "notice",
        {
            "period": "2026-02",
            "employee_id": employee,
            "changed_fields": ["salary"],
        },
        evidence=(company.proof,),
        expected_revision=0,
        request_id="notice",
    )
    identities = []
    for index in range(7):
        person = (
            employee
            if index == 0
            else Entities(company).register_entity(
                "person",
                {"display_name": f"Other {index}"},
                source="owner",
                request_id=f"other-{index}",
            )["entity_id"]
        )
        data = {
            "period": "2026-01",
            "employee_id": person,
            "employee_code": str(index),
            "name": f"Provided {index}",
            "document_type": "居民身份证",
            "document_number": f"provided-document-{index}",
        }
        saved = company.save_fact(
            "tax_import_identity_v2",
            f"identity-{index}",
            data,
            evidence=(company.proof,),
            expected_revision=0,
            request_id=f"identity-{index}",
        )
        identities.append(saved)
        if index == 0:
            saved = company.save_fact(
                "tax_import_identity_v2",
                "identity-0",
                data | {"period": "2026-03", "name": "Current provided name"},
                evidence=(company.proof,),
                expected_revision=1,
                request_id="identity-revision",
            )
            current_identity = saved["fact_id"]
    original = Store._fact_data_many_from_headers
    hydrated = set()

    def bounded(self, connection, rows):
        hydrated.update(row["id"] for row in rows)
        return original(self, connection, rows)

    monkeypatch.setattr(Store, "_fact_data_many_from_headers", bounded)
    first = WorkContext(company).query(period="2026-02", work_area="payroll", limit=1)
    assert [item["fact_id"] for item in first["items"]] == [current_identity]
    assert hydrated == {current_identity}
    assert first["items"][0]["period"] == "2026-03"
    assert first["items"][0]["data"]["name"] == "Current provided name"
    assert first["items"][0]["adoption"] is None
    second = WorkContext(company).query(
        period="2026-02",
        work_area="payroll",
        limit=1,
        cursor=first["next_cursor"],
    )
    assert [item["subject_id"] for item in second["items"]] == ["notice"]
    assert second["next_cursor"] is None
    with company.store.connection(read_only=True) as connection:
        current = company.store.select(
            connection,
            Read(
                "fact",
                "tax_import_identity_v2",
                "tax-identity:" + employee,
            ),
        )
        assert [item.id for item in current] == [current_identity]
