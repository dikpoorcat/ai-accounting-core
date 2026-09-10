from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.accounting_periods import canonical_sha256
from ai_accounting.company_notes import read_company_notes, update_company_notes
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.config import Settings
from ai_accounting.material_schemas import (
    RegisterPeriodMaterialsRequest,
    UpdatePeriodMaterialInventoryRequest,
)
from ai_accounting.material_service import MaterialService
from ai_accounting.models import BusinessEventComponent, Evidence, OpenItem, PeriodMaterialInventory


@pytest.fixture
def material_case(session, organization, tmp_path, monkeypatch):
    settings = Settings(finance_storage_dir=tmp_path, finance_evidence_dir=tmp_path / "evidence")
    settings.finance_evidence_dir.mkdir()
    monkeypatch.setattr("ai_accounting.company_notes.get_settings", lambda: settings)
    monkeypatch.setattr("ai_accounting.material_reader.get_settings", lambda: settings)
    period = AccountingPeriodService(
        session, current_date=date(2026, 9, 10)
    ).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id, period_month="2026-08", idempotency_key="aug"
        )
    )
    assert period.status == "posted", period
    september = AccountingPeriodService(
        session, current_date=date(2026, 9, 10)
    ).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id, period_month="2026-09", idempotency_key="sep"
        )
    )
    assert september.status == "posted"
    path = settings.finance_evidence_dir / "报销.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "报销"
    sheet.append(["姓名", "报销款"])
    sheet.append(["罗正宏", "45873.21"])
    sheet.append(["姜涛", "11824.65"])
    sheet.append(["杨彪", "3076.87"])
    sheet.append(["合计", "60774.73"])
    workbook.save(path)
    raw = path.read_bytes()
    evidence = Evidence(
        org_id=organization.id,
        original_name=path.name,
        storage_path=str(path),
        sha256=sha256(raw).hexdigest(),
        size_bytes=len(raw),
        source="test",
    )
    session.add(evidence)
    session.flush()
    service = MaterialService(session)
    request = RegisterPeriodMaterialsRequest(
        org_id=organization.id,
        period_id=period.period_id,
        expected_revision=0,
        idempotency_key="intake",
        sources=[
            {
                "evidence_id": evidence.id,
                "columns": [{"column": "A", "role": "context"}, {"column": "B", "role": "amount"}],
                "total_rows": [5],
            }
        ],
    )
    registered = service.register(request)
    assert len(registered["completeness"]["items"]) == 3
    return service, period.period_id, evidence, request


def post_expense(session, org, evidence, key, amount, month="2026-08", posting_date="2026-08-31"):
    result = ComponentService(session).record(
        RecordEventRequest(
            org_id=org.id,
            idempotency_key=key,
            posting_date=posting_date,
            evidence_references=[evidence.id],
            components=[
                {
                    "key": "expense",
                    "kind": "expense",
                    "recognition_period": month,
                    "amount_fen": amount,
                    "expense_class": "general_expense",
                    "payment_basis": "supplier_credit",
                }
            ],
        )
    )
    assert result.status == "posted", result
    return session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == result.event_id,
            BusinessEventComponent.key == "expense",
        )
    )


def resolve_case(session, org, case, *, count=3):
    service, period_id, evidence, _ = case
    result = service.check(org.id, period_id)
    resolutions = []
    for i, item in enumerate(result["items"][:count]):
        component = post_expense(session, org, evidence, f"expense-{i}", item["amount_fen"])
        resolutions.append(
            {
                "item_key": item["key"],
                "treatment": "recognize",
                "business_kind": "expense",
                "recognition_period": "2026-08",
                "links": [
                    {
                        "source": {"component_id": component.id},
                        "amount_fen": item["amount_fen"],
                        "expected_facts_hash": canonical_sha256(component.facts),
                    }
                ],
            }
        )
    return service.update(
        UpdatePeriodMaterialInventoryRequest(
            org_id=org.id,
            period_id=period_id,
            expected_revision=result["revision"],
            idempotency_key="resolve",
            reviewed_notes_hash=read_company_notes(org)["sha256"],
            resolutions=resolutions,
        )
    )


def test_missing_third_reimbursement_cannot_confirm_complete(session, organization, material_case):
    result = resolve_case(session, organization, material_case, count=2)
    assert not result["completeness"]["satisfied"]
    pending = [i for i in result["completeness"]["items"] if not i["satisfied"]]
    assert len(pending) == 1 and pending[0]["amount_fen"] == 307687
    assert pending[0]["location"] == "报销!B4"
    assert "杨彪" in pending[0]["excerpt"]

    from ai_accounting.accounting_period_schemas import PreviewAccountingPeriodCloseRequest
    from ai_accounting.models import AccountingPeriod
    from ai_accounting.owner_workflow import OwnerWorkflowService
    from ai_accounting.owner_workflow_schemas import ConfirmPeriodMaterialCompletenessRequest

    owner = OwnerWorkflowService(session)
    period = session.get(AccountingPeriod, material_case[1])
    snapshot = owner._material_snapshot(organization.id, period)
    confirmation = owner.confirm_period_material_completeness(
        ConfirmPeriodMaterialCompletenessRequest(
            org_id=organization.id,
            period_id=period.id,
            activity_snapshot_hash=snapshot["hash"],
            idempotency_key="cannot-bypass",
            confirmation_note="没有其他资料",
        )
    )
    assert confirmation["status"] == "needs_information"
    close = AccountingPeriodService(session).preview_accounting_period_close(
        PreviewAccountingPeriodCloseRequest(
            org_id=organization.id, period_id=period.id, closing_date=period.end_date
        )
    )
    assert "ACCOUNTING_PERIOD_MATERIAL_INCOMPLETE" in close.data["blocker_codes"]


def test_all_recognized_without_payment_and_notes_edit_reopens(
    session, organization, material_case
):
    result = resolve_case(session, organization, material_case)
    assert result["completeness"]["satisfied"], result
    before = read_company_notes(organization)
    update_company_notes(
        organization, before["sha256"], before["content"] + "\n8月报销范围有变化。\n"
    )
    changed = material_case[0].check(organization.id, material_case[1])
    assert "MATERIAL_NOTES_REVIEW_REQUIRED" in {i["code"] for i in changed["issues"]}
    assert not changed["satisfied"]


@pytest.mark.parametrize("shortfall", [0, 1])
def test_multiple_material_rows_share_only_original_component_capacity(
    session, organization, material_case, shortfall
):
    service, period_id, evidence, _ = material_case
    items = service.check(organization.id, period_id)["items"]
    amount = sum(item["amount_fen"] for item in items)
    component = post_expense(
        session, organization, evidence, "combined-expenses", amount - shortfall
    )
    reviewed = service.update(
        UpdatePeriodMaterialInventoryRequest(
            org_id=organization.id,
            period_id=period_id,
            expected_revision=1,
            idempotency_key="combine-materials",
            reviewed_notes_hash=read_company_notes(organization)["sha256"],
            resolutions=[
                {
                    "item_key": item["key"],
                    "treatment": "recognize",
                    "business_kind": "expense",
                    "recognition_period": "2026-08",
                    "links": [
                        {
                            "source": {"component_id": component.id},
                            "amount_fen": item["amount_fen"],
                            "expected_facts_hash": canonical_sha256(component.facts),
                        }
                    ],
                }
                for item in items
            ],
        )
    )
    assert reviewed["completeness"]["satisfied"] == (shortfall == 0)
    if shortfall:
        assert "MATERIAL_COMPONENT_OVERALLOCATED" in {
            issue["code"] for issue in reviewed["completeness"]["issues"]
        }


def test_inventory_revision_and_idempotency(session, organization, material_case):
    service, period_id, _, request = material_case
    assert service.register(request)["idempotent_replay"]
    bad = request.model_copy(update={"idempotency_key": "other"})
    with pytest.raises(ValueError, match="MATERIAL_INVENTORY_STALE"):
        service.register(bad)
    assert len(list(session.scalars(select(PeriodMaterialInventory)))) == 1


@pytest.mark.parametrize("missing", ["amount_fen", "evidence_references"])
def test_failed_posting_keeps_previously_registered_pending_material(
    session, organization, material_case, missing
):
    from sqlalchemy import func

    from ai_accounting.models import BusinessEvent

    service, period_id, evidence, _ = material_case
    before = session.scalar(select(func.count()).select_from(BusinessEvent))
    failed = ComponentService(session).record(
        RecordEventRequest(
            org_id=organization.id,
            idempotency_key="missing-credit-amount",
            posting_date="2026-08-31",
            evidence_references=[] if missing == "evidence_references" else [evidence.id],
            components=[
                {
                    "key": "pass",
                    "kind": "pass_through",
                    "recognition_basis": "credit",
                    "recognition_period": "2026-08",
                    "amount_fen": None if missing == "amount_fen" else 10000,
                }
            ],
        )
    )
    assert failed.status == "needs_information", failed
    assert failed.data["fact_issues"][0]["fields"] == [f"components.pass.{missing}"]
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == before
    checked = service.check(organization.id, period_id)
    assert checked["revision"] == 1 and len(checked["items"]) == 3
    assert not any(item["satisfied"] for item in checked["items"])


def test_later_posting_cannot_satisfy_prior_month(session, organization, material_case):
    service, period_id, evidence, _ = material_case
    item = service.check(organization.id, period_id)["items"][0]
    component = post_expense(
        session, organization, evidence, "late", item["amount_fen"], posting_date="2026-09-10"
    )
    result = service.update(
        UpdatePeriodMaterialInventoryRequest(
            org_id=organization.id,
            period_id=period_id,
            expected_revision=1,
            idempotency_key="late-link",
            reviewed_notes_hash=read_company_notes(organization)["sha256"],
            resolutions=[
                {
                    "item_key": item["key"],
                    "treatment": "recognize",
                    "business_kind": "expense",
                    "recognition_period": "2026-08",
                    "links": [
                        {
                            "source": {"component_id": component.id},
                            "amount_fen": item["amount_fen"],
                            "expected_facts_hash": canonical_sha256(component.facts),
                        }
                    ],
                }
            ],
        )
    )
    assert "MATERIAL_POSTING_PERIOD_CONFLICT" in {
        i["code"] for i in result["completeness"]["issues"]
    }


def test_notes_compare_and_swap_preserves_user_edit(session, organization, material_case):
    current = read_company_notes(organization)
    Path(current["path"]).write_text("负责人直接编辑", encoding="utf-8")
    result = update_company_notes(organization, current["sha256"], "AI旧版本")
    assert result["status"] == "rejected"
    assert read_company_notes(organization)["content"] == "负责人直接编辑"


def test_source_unassigned_is_not_zero_business(session, organization, material_case):
    resolve_case(session, organization, material_case)
    session.add(
        Evidence(
            org_id=organization.id,
            original_name="漏掉的票据.pdf",
            storage_path="missing",
            sha256="b" * 64,
            size_bytes=1,
            source="test",
        )
    )
    session.flush()
    result = material_case[0].check(organization.id, material_case[1])
    assert "MATERIAL_EVIDENCE_UNREVIEWED" in {i["code"] for i in result["issues"]}


def test_unknown_source_amount_can_be_confirmed_without_changing_original(
    session, organization, material_case
):
    service, period_id, original, _ = material_case
    resolve_case(session, organization, material_case)
    path = Path(original.storage_path).parent / "待确认.csv"
    path.write_text("姓名,报销款\n杨彪,不详\n合计,100.00\n", encoding="utf-8")
    raw = path.read_bytes()
    evidence = Evidence(
        org_id=organization.id,
        original_name=path.name,
        storage_path=str(path),
        sha256=sha256(raw).hexdigest(),
        size_bytes=len(raw),
        source="test",
    )
    session.add(evidence)
    session.flush()
    result = service.register(
        RegisterPeriodMaterialsRequest(
            org_id=organization.id,
            period_id=period_id,
            expected_revision=2,
            idempotency_key="unknown-amount",
            sources=[
                {
                    "evidence_id": evidence.id,
                    "columns": [
                        {"column": "A", "role": "context"},
                        {"column": "B", "role": "amount"},
                    ],
                    "total_rows": [3],
                }
            ],
        )
    )
    assert not result["completeness"]["satisfied"]
    item = next(
        item for item in result["completeness"]["items"] if item["source_id"] == str(evidence.id)
    )
    component = post_expense(session, organization, evidence, "confirmed-amount", 10000)
    reviewed = service.update(
        UpdatePeriodMaterialInventoryRequest(
            org_id=organization.id,
            period_id=period_id,
            expected_revision=3,
            idempotency_key="amount-confirmation",
            reviewed_notes_hash=read_company_notes(organization)["sha256"],
            resolutions=[
                {
                    "item_key": item["key"],
                    "treatment": "recognize",
                    "business_kind": "expense",
                    "recognition_period": "2026-08",
                    "amount_fen": 10000,
                    "basis": "负责人确认本项100元，与原件合计相符。",
                    "links": [
                        {
                            "source": {"component_id": component.id},
                            "amount_fen": 10000,
                            "expected_facts_hash": canonical_sha256(component.facts),
                        }
                    ],
                }
            ],
        )
    )
    assert reviewed["completeness"]["satisfied"], reviewed
    assert path.read_bytes() == raw
    assert (
        service.latest(organization.id, period_id).content["items"][item["key"]]["amount_fen"]
        is None
    )


def test_transfer_scope_change_reopens_original_month(session, organization, material_case):
    from ai_accounting.models import AccountingPeriod

    service, period_id, _, _ = material_case
    result = resolve_case(session, organization, material_case, count=2)
    item = result["completeness"]["items"][2]
    september = session.scalar(
        select(AccountingPeriod).where(
            AccountingPeriod.org_id == organization.id,
            AccountingPeriod.calendar_month == 9,
        )
    )
    transferred = service.update(
        UpdatePeriodMaterialInventoryRequest(
            org_id=organization.id,
            period_id=period_id,
            expected_revision=2,
            idempotency_key="scope-transfer",
            reviewed_notes_hash=read_company_notes(organization)["sha256"],
            resolutions=[
                {
                    "item_key": item["key"],
                    "treatment": "other_period",
                    "recognition_period": "2026-09",
                    "target_period_id": september.id,
                    "basis": "确认属于9月。",
                }
            ],
        )
    )
    assert transferred["completeness"]["satisfied"], transferred
    service.update(
        UpdatePeriodMaterialInventoryRequest(
            org_id=organization.id,
            period_id=september.id,
            expected_revision=1,
            idempotency_key="scope-changed",
            reviewed_notes_hash=read_company_notes(organization)["sha256"],
            resolutions=[
                {
                    "item_key": item["key"],
                    "treatment": "pending",
                    "recognition_period": "2026-08",
                    "basis": "重新核对资料，可能仍属于8月。",
                }
            ],
        )
    )
    checked = service.check(organization.id, period_id)
    assert not checked["satisfied"]
    assert "MATERIAL_TRANSFER_SCOPE_CHANGED" in {issue["code"] for issue in checked["issues"]}


def test_notes_backup_preserves_original_bytes(organization, material_case):
    from ai_accounting.company_notes import read_company_notes_bytes

    path = Path(read_company_notes(organization)["path"])
    raw = b"\xef\xbb\xbf" + "# 长期规则\r\n负责人直接保存的文件。\r\n".encode()
    path.write_bytes(raw)
    assert read_company_notes_bytes(organization) == raw
    assert read_company_notes(organization)["sha256"] == sha256(raw).hexdigest()


def test_credit_pass_through_then_receipt_and_payment(session, organization, material_case):
    _, _, evidence, _ = material_case
    original = ComponentService(session).record(
        RecordEventRequest(
            org_id=organization.id,
            idempotency_key="credit-pass",
            posting_date="2026-08-31",
            evidence_references=[evidence.id],
            components=[
                {
                    "key": "pass",
                    "kind": "pass_through",
                    "recognition_basis": "credit",
                    "recognition_period": "2026-08",
                    "amount_fen": 10000,
                }
            ],
        )
    )
    assert original.status == "posted", original
    items = list(
        session.scalars(select(OpenItem).where(OpenItem.source_event_id == original.event_id))
    )
    assert len(items) == 2
    for index, direction in enumerate(("receipt", "payment")):
        kind = "receivable_settlement" if direction == "receipt" else "payable_settlement"
        key = "receivable" if direction == "receipt" else "primary"
        result = ComponentService(session).record(
            RecordEventRequest(
                org_id=organization.id,
                idempotency_key=f"settle-{index}",
                posting_date="2026-09-10",
                evidence_references=[evidence.id],
                components=[
                    {
                        "key": "settle",
                        "kind": kind,
                        "allocations": [
                            {
                                "source_event_key": "credit-pass",
                                "source_component_key": "pass",
                                "source_open_item_key": key,
                                "amount_fen": 10000,
                            }
                        ],
                    }
                ],
                funds=[
                    {
                        "key": "cash",
                        "account_code": "1001",
                        "direction": direction,
                        "payment_date": "2026-09-10",
                        "amount_fen": 10000,
                        "allocations": [{"component_key": "settle", "amount_fen": 10000}],
                    }
                ],
            )
        )
        assert result.status == "posted", result
    assert all(item.settled_amount_fen == 10000 for item in items)


def test_duplicate_evidence_and_split_preserve_amounts(session, organization, material_case):
    service, period_id, evidence, request = material_case
    item = service.check(organization.id, period_id)["items"][2]
    result = service.update(
        UpdatePeriodMaterialInventoryRequest(
            org_id=organization.id,
            period_id=period_id,
            expected_revision=1,
            idempotency_key="split",
            reviewed_notes_hash=read_company_notes(organization)["sha256"],
            splits=[
                {
                    "item_key": item["key"],
                    "basis": "费用和代收代付分别核对",
                    "parts": [{"amount_fen": 100000}, {"amount_fen": 207687}],
                }
            ],
        )
    )
    assert len(result["completeness"]["items"]) == 5
    service.register(
        request.model_copy(update={"idempotency_key": "same-file-again", "expected_revision": 2})
    )
    assert len(service.check(organization.id, period_id)["items"]) == 5
    latest = service.latest(organization.id, period_id)
    assert len(latest.content["items"][item["key"]]["split_children"]) == 2


def test_transfer_persists_pending_in_target_and_is_atomic(session, organization, material_case):
    from ai_accounting.models import AccountingPeriod

    service, period_id, _, _ = material_case
    september = session.scalar(
        select(AccountingPeriod).where(
            AccountingPeriod.org_id == organization.id, AccountingPeriod.calendar_month == 9
        )
    )
    item = service.check(organization.id, period_id)["items"][2]
    request = UpdatePeriodMaterialInventoryRequest(
        org_id=organization.id,
        period_id=period_id,
        expected_revision=1,
        idempotency_key="transfer",
        reviewed_notes_hash=read_company_notes(organization)["sha256"],
        resolutions=[
            {
                "item_key": item["key"],
                "treatment": "other_period",
                "recognition_period": "2026-09",
                "target_period_id": september.id,
                "basis": "负责人确认费用属于9月",
            }
        ],
    )
    bad = request.model_copy(
        update={
            "resolutions": [
                *request.resolutions,
                request.resolutions[0].model_copy(update={"item_key": "missing"}),
            ]
        }
    )
    with pytest.raises(ValueError, match="MATERIAL_ITEM_NOT_FOUND"):
        service.update(bad)
    assert service.latest(organization.id, september.id) is None
    service.update(request)
    transferred = service.check(organization.id, september.id)
    assert any(i["key"] == item["key"] and not i["satisfied"] for i in transferred["items"])
    assert (
        service.register(
            material_case[3].model_copy(
                update={
                    "period_id": september.id,
                    "expected_revision": 1,
                    "idempotency_key": "same-file-future",
                }
            )
        )["revision"]
        == 2
    )
    assert len(service.check(organization.id, september.id)["items"]) == 1


def test_source_tampering_and_deleted_entry_reopen_review(session, organization, material_case):
    from ai_accounting.event_amendment_schemas import DeleteEventRequest
    from ai_accounting.event_amendments import EventAmendmentService
    from ai_accounting.models import BusinessEvent

    service, period_id, evidence, _ = material_case
    resolve_case(session, organization, material_case)
    original = Path(evidence.storage_path).read_bytes()
    Path(evidence.storage_path).write_bytes(b"changed")
    assert "MATERIAL_SOURCE_CHANGED" in {
        i["code"] for i in service.check(organization.id, period_id)["issues"]
    }
    Path(evidence.storage_path).write_bytes(original)
    component = session.scalar(
        select(BusinessEventComponent).where(BusinessEventComponent.kind == "expense")
    )
    event = session.get(BusinessEvent, component.event_id)
    deleted = EventAmendmentService(session).amend(
        DeleteEventRequest(
            org_id=organization.id,
            event_id=event.id,
            expected_facts_hash=canonical_sha256(event.facts),
            idempotency_key="delete-material-source",
        )
    )
    assert deleted["status"] == "deleted", deleted
    assert not service.check(organization.id, period_id)["satisfied"]


def test_portable_inventory_has_no_database_uuids(session, organization, material_case, tmp_path):
    import json

    from ai_accounting import replay_cli
    from ai_accounting.material_replay import export_material_operations

    resolve_case(session, organization, material_case)
    from collections import defaultdict

    from ai_accounting.models import BusinessEvent

    maps = defaultdict(dict)
    maps["evidence"][str(material_case[2].id)] = {
        "$ref": "evidence",
        "sha256": material_case[2].sha256,
    }
    for component in session.scalars(select(BusinessEventComponent)):
        event = session.get(BusinessEvent, component.event_id)
        maps["component"][str(component.id)] = {
            "$ref": "component",
            "source_replay_key": replay_cli._semantic_replay_key(event.idempotency_key),
            "component_key": component.key,
        }
    operations = export_material_operations(session, organization.id, maps, tmp_path)
    serialized = json.dumps(operations, ensure_ascii=False)
    assert str(material_case[2].id) not in serialized
    for component in session.scalars(select(BusinessEventComponent)):
        assert str(component.id) not in serialized
    assert not operations[-1]["requires_notes_review"]
    assert (tmp_path / "业务说明.md").exists()


@pytest.mark.parametrize("export_category", ["reimbursement", "labor"])
def test_three_person_complete_export_cannot_omit_yang_biao(
    session, organization, material_case, tmp_path, export_category
):
    from test_mybank_export import workbook

    from ai_accounting.models import AccountingPeriod, Counterparty, Employee
    from ai_accounting.mybank_schemas import PreviewMybankExportRequest
    from ai_accounting.mybank_service import MybankExportService
    from ai_accounting.owner_workflow import OwnerWorkflowService
    from ai_accounting.owner_workflow_schemas import ConfirmWorkforceReviewRequest

    service, period_id, evidence, _ = material_case
    employees = []
    for index, name in enumerate(("罗正宏", "姜涛", "杨彪")):
        party = Counterparty(org_id=organization.id, kind="employee", name=name)
        session.add(party)
        session.flush()
        employee = Employee(
            org_id=organization.id,
            counterparty_id=party.id,
            name=name,
            employee_code=f"P{index}",
            employment_start_date=date(2026, 8, 1),
            status="active",
        )
        session.add(employee)
        employees.append(employee)
    session.flush()
    period = session.get(AccountingPeriod, period_id)
    owner = OwnerWorkflowService(session)
    workforce = owner._workforce_snapshot(organization.id, period)
    zero = owner.confirm_workforce_review(
        ConfirmWorkforceReviewRequest(
            org_id=organization.id,
            period_id=period_id,
            workforce_snapshot_hash=workforce["hash"],
            change_state="changes_resolved",
            idempotency_key="no-wages-this-month",
            confirmation_note="本测试月份三人仅办理报销，没有工资及个税事项。",
            evidence_references=[evidence.id],
            regular_payroll_items=[
                {
                    "employee_id": e.id,
                    "wage_tax_scope": "contributions_only",
                    "accounting_gross_salary_fen": 0,
                    "special_additional_deduction_fen": 0,
                    "other_legal_deduction_fen": 0,
                    "tax_relief_fen": 0,
                }
                for e in employees
            ],
        )
    )
    assert zero["status"] == "confirmed", zero
    template = tmp_path / "bank.xltx"
    template.write_bytes(
        workbook(
            [
                [
                    "收款方名称（必输，文本格式）",
                    "收款方账号（必输，本文格式）",
                    "金额（必输，文本格式）",
                    "附言/用途（必输，文本格式，最多40字）",
                ]
            ],
            template=True,
        )
    )
    recipients = tmp_path / "recipients.xlsx"
    recipients.write_bytes(
        workbook(
            [
                ["姓名", "账号"],
                *[[e.name, f"00123456789012345{i}"] for i, e in enumerate(employees)],
            ]
        )
    )
    items = service.check(organization.id, period_id)["items"]
    payables = []
    for index, (employee, item) in enumerate(zip(employees, items, strict=True)):
        result = ComponentService(session).record(
            RecordEventRequest(
                org_id=organization.id,
                idempotency_key=f"real-reimbursement-{index}",
                posting_date="2026-08-31",
                evidence_references=[evidence.id],
                components=[
                    {
                        "key": "expense",
                        "kind": "expense",
                        "recognition_period": "2026-08",
                        "amount_fen": item["amount_fen"],
                        "expense_class": "general_expense",
                        "payment_basis": "person_advance",
                        "payer": {"id": employee.counterparty_id},
                        "metadata": {"purpose": "发票报销"},
                    }
                ],
            )
        )
        assert result.status == "posted", result
        component = session.scalar(
            select(BusinessEventComponent).where(BusinessEventComponent.event_id == result.event_id)
        )
        payable = session.scalar(
            select(OpenItem).where(OpenItem.source_component_id == component.id)
        )
        payables.append(payable.id)
        updated = service.update(
            UpdatePeriodMaterialInventoryRequest(
                org_id=organization.id,
                period_id=period_id,
                expected_revision=index + 1,
                idempotency_key=f"review-person-{index}",
                reviewed_notes_hash=read_company_notes(organization)["sha256"],
                resolutions=[
                    {
                        "item_key": item["key"],
                        "treatment": "recognize",
                        "business_kind": "expense",
                        "recognition_period": "2026-08",
                        "employee_id": employee.id,
                        "export_category": export_category,
                        "links": [
                            {
                                "source": {"component_id": component.id},
                                "amount_fen": item["amount_fen"],
                                "expected_facts_hash": canonical_sha256(component.facts),
                            }
                        ],
                    }
                ],
            )
        )
        if index == 1:
            request = PreviewMybankExportRequest(
                org_id=organization.id,
                payroll_period="2026-08",
                template_path=str(template),
                recipients_path=str(recipients),
            )
            partial = MybankExportService(session).preview(request)
            assert partial["status"] == "needs_information"
            missing = next(
                i
                for i in partial["missing_information"]
                if i["code"] == "MYBANK_MATERIAL_INCOMPLETE"
            )
            assert any(i.get("amount_fen") == 307687 for i in missing["material_issues"])
    assert updated["completeness"]["satisfied"], updated
    incomplete = MybankExportService(session).preview(
        request.model_copy(
            update={
                "scope": "complete",
                "include_salary": False,
                "reimbursement_open_item_ids": payables[:2],
            }
        )
    )
    assert "MYBANK_COMPLETE_SCOPE_REQUIRED" in {
        i["code"] for i in incomplete["missing_information"]
    }
    selected = MybankExportService(session).preview(
        request.model_copy(
            update={
                "scope": "selected",
                "include_salary": False,
                "reimbursement_open_item_ids": payables[:2],
            }
        )
    )
    assert selected["status"] == "ready" and selected["scope"] == "selected"
    complete = MybankExportService(session).preview(request)
    assert complete["status"] == "ready", complete
    assert complete["totals"][f"{export_category}_fen"] == 6077473
