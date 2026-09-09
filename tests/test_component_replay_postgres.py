from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from _postgres_helpers import (
    authenticated_business_database,
    catalog_owner_authority,
    isolated_postgres_url,
)
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from test_confirmed_accrual_components import _accruals, _calculated_batches
from test_round5_transactions import _register_second_employee
from test_tax_confirmation_components import _relief, _sale

from ai_accounting import replay_cli
from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import ConfigureAccountRequest, RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.labor_remuneration_schemas import PreviewLaborRemunerationBatchRequest
from ai_accounting.labor_remuneration_service import LaborRemunerationService
from ai_accounting.models import (
    Account,
    BusinessEvent,
    BusinessEventComponent,
    Evidence,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDepreciation,
    LaborRemunerationBatch,
    LaborRemunerationLine,
    OpenItem,
    Organization,
    OrganizationProfileVersion,
    PayrollBatch,
    PayrollLine,
    TaxPeriod,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import PreviewPayrollRequest
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@contextmanager
def _migrated_throwaway_business_database(prefix: str) -> Iterator[Engine]:
    with isolated_postgres_url(f"component_replay_{prefix}") as url:
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = url
        command.upgrade(config, "head")
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def _record_source_protocol_facts(engine: Engine, evidence_path: Path) -> uuid.UUID:
    org_id = uuid.uuid4()
    evidence_bytes = b"isolated component replay evidence\n"
    evidence_path.write_bytes(evidence_bytes)
    evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    supplier = {"kind": "supplier", "name": "Replay protocol supplier"}

    with Session(engine) as session:
        organization = seed_organization(
            session,
            org_id=org_id,
            taxpayer_identification_number="91330106MA1234567T",
            name="组合协议隔离回放源企业",
            accounting_period_control_enabled=False,
        )
        session.commit()
        with catalog_owner_authority(session, organization) as authority:
            with authority.attributed_call(session, tool_name="finance_configure_account"):
                configured = ComponentService(session).configure_account(
                    ConfigureAccountRequest(
                        org_id=org_id,
                        idempotency_key="source-replay-general-expense-detail",
                        code="560299",
                        name="回放验证管理费用明细",
                        business_class="general_expense",
                    )
                )
            assert configured["business_class"] == "general_expense"
            session.commit()
            evidence = Evidence(
                org_id=org_id,
                sha256=evidence_sha256,
                original_name="component-replay.txt",
                media_type="text/plain",
                source="isolated-postgres-test",
                size_bytes=len(evidence_bytes),
                storage_path=str(evidence_path.resolve()),
                metadata_json={"scope": "synthetic"},
            )
            with authority.attributed_call(
                session, tool_name="finance_component_replay_test_evidence"
            ):
                session.add(evidence)
                session.flush()
            session.commit()
            with authority.attributed_call(
                session, tool_name="finance_confirm_company_profile_change"
            ) as attribution:
                session.add(
                    OrganizationProfileVersion(
                        org_id=org_id,
                        effective_from=date(2026, 1, 1),
                        name=organization.name,
                        taxpayer_identification_number=(
                            organization.taxpayer_identification_number
                        ),
                        taxpayer_type="small_scale",
                        filing_cycle=organization.filing_cycle,
                        jurisdiction=organization.jurisdiction,
                        urban_maintenance_rate=Decimal("0.07"),
                        accounting_standard="small_enterprise",
                        confirmation_note="建立隔离回放测试公司的首版资料。",
                        lifecycle_action_id=None,
                        execution_attribution_id=attribution.id,
                    )
                )
            session.commit()

            purchase = RecordEventRequest(
                org_id=org_id,
                idempotency_key="source-replay-supplier-credit",
                posting_date=date(2026, 3, 5),
                description="同类费用组合形成供应商应付",
                evidence_references=[evidence.id],
                components=[
                    {
                        "key": "consulting",
                        "kind": "expense",
                        "recognition_period": "2026-02",
                        "amount_fen": 1200,
                        "expense_class": "general_expense",
                        "account_code": "560299",
                        "payment_basis": "supplier_credit",
                        "metadata": {"counterparty": supplier},
                    },
                    {
                        "key": "supplies",
                        "kind": "expense",
                        "business_date": "2026-03-05",
                        "amount_fen": 800,
                        "expense_class": "general_expense",
                        "payment_basis": "supplier_credit",
                        "metadata": {"counterparty": supplier},
                    },
                ],
            )
            with authority.attributed_call(session, tool_name="finance_record_event"):
                purchase_result = ComponentService(session).record(purchase)
            assert purchase_result.status == "posted", purchase_result
            session.commit()

            source_items = list(
                session.scalars(
                    select(OpenItem)
                    .where(OpenItem.source_event_id == purchase_result.event_id)
                    .order_by(OpenItem.original_amount_fen.desc())
                )
            )
            assert [item.original_amount_fen for item in source_items] == [1_200, 800]
            settlement = RecordEventRequest(
                org_id=org_id,
                idempotency_key="source-replay-settlement-and-fee",
                posting_date=date(2026, 3, 8),
                description="结清供应商应付并支付银行手续费",
                evidence_references=[evidence.id],
                components=[
                    {
                        "key": "pay_supplier",
                        "kind": "payable_settlement",
                        "business_date": "2026-03-08",
                        "payment_date": "2026-03-08",
                        "allocations": [
                            {"open_item_id": item.id, "amount_fen": item.original_amount_fen}
                            for item in source_items
                        ],
                        "metadata": {"counterparty": supplier},
                    },
                    {
                        "key": "bank_fee",
                        "kind": "expense",
                        "business_date": "2026-03-08",
                        "payment_date": "2026-03-08",
                        "amount_fen": 100,
                        "expense_class": "finance_expense",
                        "expense_nature": "bank_service_fee",
                        "payment_basis": "immediate",
                    },
                ],
                funds=[
                    {
                        "key": "cash_payment",
                        "account_code": "1001",
                        "direction": "payment",
                        "payment_date": "2026-03-08",
                        "amount_fen": 2100,
                        "allocations": [
                            {"component_key": "pay_supplier", "amount_fen": 2000},
                            {"component_key": "bank_fee", "amount_fen": 100},
                        ],
                    }
                ],
            )
            with authority.attributed_call(session, tool_name="finance_record_event"):
                settlement_result = ComponentService(session).record(settlement)
            assert settlement_result.status == "posted", settlement_result
            session.commit()
    return org_id


def test_new_component_protocol_replays_to_empty_postgres_with_stable_open_item_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Export v3 facts with monthly recognition and replay into an empty target."""

    with (
        _migrated_throwaway_business_database("source") as source_engine,
        _migrated_throwaway_business_database("target") as target_engine,
    ):
        org_id = _record_source_protocol_facts(source_engine, tmp_path / "source-evidence.txt")
        package_root = tmp_path / "new-protocol-export"
        package_root.mkdir()
        exported_company = replay_cli._export_company(
            engine=source_engine,
            registry={
                "org_id": org_id,
                "status": "active",
                "is_primary": True,
                "close_backup_directory": None,
            },
            package_root=package_root,
        )
        company_dir = package_root / exported_company["directory"]
        descriptor = json.loads((company_dir / "company.json").read_text(encoding="utf-8"))
        operations = [
            json.loads(line)
            for line in (company_dir / "operations.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        event_operations = [
            operation for operation in operations if operation.get("tool") == "finance_record_event"
        ]
        assert [operation["key"] for operation in event_operations] == [
            "source-replay-supplier-credit",
            "source-replay-settlement-and-fee",
        ]
        assert all(operation["source_event_type"] == "composite" for operation in event_operations)
        assert all("components" in operation["request"] for operation in event_operations)
        monthly_facts = event_operations[0]["request"]["components"][0]
        assert monthly_facts["recognition_period"] == "2026-02"
        assert monthly_facts.get("business_date") is None
        assert all("event_type" not in operation["request"] for operation in event_operations)
        assert not any(
            forbidden in operation["request"]
            for operation in event_operations
            for forbidden in ("entries", "journal_lines", "voucher_lines")
        )
        settlement_refs = event_operations[1]["request"]["components"][0]["allocations"]
        assert {allocation["open_item_id"]["$ref"] for allocation in settlement_refs} == {
            "component_open_item"
        }
        assert {
            allocation["open_item_id"]["source_replay_key"] for allocation in settlement_refs
        } == {event_operations[0]["key"]}
        evidence_sha256 = hashlib.sha256(b"isolated component replay evidence\n").hexdigest()
        replay_cli._verify_operation_references(
            event_operations,
            evidence_hashes={evidence_sha256},
            org_id=str(org_id),
        )

        with Session(target_engine) as target_session:
            assert target_session.scalar(select(func.count()).select_from(Account)) == 0
            target_org = seed_organization(
                target_session,
                org_id=org_id,
                taxpayer_identification_number=descriptor["organization"][
                    "taxpayer_identification_number"
                ],
                name=descriptor["organization"]["name"],
                accounting_period_control_enabled=False,
            )
            replay_cli._apply_account_controls(
                target_session,
                org_id=org_id,
                controls=descriptor["accounts"],
            )
            target_session.commit()
            custom_account = target_session.scalar(
                select(Account).where(Account.org_id == org_id, Account.code == "560299")
            )
            assert custom_account is not None
            assert custom_account.business_class == "general_expense"

            with catalog_owner_authority(target_session, target_org) as target_authority:
                exported_evidence = json.loads(
                    (company_dir / "evidence-manifest.jsonl").read_text(encoding="utf-8")
                )
                with target_authority.attributed_call(
                    target_session, tool_name="finance_component_replay_test_evidence"
                ):
                    target_session.add(
                        Evidence(
                            org_id=org_id,
                            sha256=exported_evidence["sha256"],
                            original_name=exported_evidence["original_name"],
                            media_type=exported_evidence["media_type"],
                            source=exported_evidence["source"],
                            size_bytes=exported_evidence["size_bytes"],
                            storage_path=str(company_dir / exported_evidence["relative_path"]),
                            metadata_json=exported_evidence["metadata"],
                        )
                    )
                target_session.commit()

                def authenticated_component_call(tool_name: str, request: dict) -> dict:
                    if tool_name == "finance_update_business_metadata":
                        from ai_accounting.business_metadata import (
                            UpdateBusinessMetadataRequest,
                            update_business_metadata,
                        )

                        with target_authority.attributed_call(target_session, tool_name=tool_name):
                            result = update_business_metadata(
                                target_session,
                                UpdateBusinessMetadataRequest.model_validate(request),
                            )
                        target_session.commit()
                        return result
                    typed_request = RecordEventRequest.model_validate(request)
                    if tool_name == "finance_preview_event":
                        with target_authority.attributed_call(
                            target_session, tool_name="finance_preview_event"
                        ):
                            result = ComponentService(target_session).preview(typed_request)
                        target_session.rollback()
                        return result.model_dump(mode="json")
                    assert tool_name == "finance_record_event"
                    with target_authority.attributed_call(
                        target_session, tool_name="finance_record_event"
                    ):
                        result = ComponentService(target_session).record(typed_request)
                    target_session.commit()
                    return result.model_dump(mode="json")

                monkeypatch.setattr(replay_cli, "_call_tool", authenticated_component_call)
                results: dict[str, dict] = {}
                resolver = replay_cli._ReplayResolver(
                    engine=target_engine,
                    org_id=org_id,
                    results=results,
                )
                with pytest.raises(replay_cli.ReplayError, match="REPLAY_COMPONENT_SOURCE_MISSING"):
                    resolver.materialize(event_operations[1]["request"])

                for operation in event_operations:
                    result = replay_cli._execute_operation(
                        operation,
                        package_company_dir=company_dir,
                        resolver=resolver,
                    )
                    results[operation["key"]] = {
                        "status": result["status"],
                        "event_id": result["event_id"],
                    }

                metadata_operations = [
                    o for o in operations if o.get("tool") == "finance_update_business_metadata"
                ]
                assert metadata_operations
                for operation in metadata_operations:
                    result = replay_cli._execute_operation(
                        operation, package_company_dir=company_dir, resolver=resolver
                    )
                    assert result["status"] == "updated"
                    assert replay_cli._execute_operation(
                        operation, package_company_dir=company_dir, resolver=resolver
                    )["idempotent_replay"]

                voucher_count = target_session.scalar(select(func.count()).select_from(Voucher))
                event_count = target_session.scalar(
                    select(func.count())
                    .select_from(BusinessEvent)
                    .where(BusinessEvent.org_id == org_id)
                )
                retry = replay_cli._execute_operation(
                    event_operations[1],
                    package_company_dir=company_dir,
                    resolver=resolver,
                )
                assert retry["event_id"] == results[event_operations[1]["key"]]["event_id"]
                assert (
                    target_session.scalar(select(func.count()).select_from(Voucher))
                    == voucher_count
                )
                assert (
                    target_session.scalar(
                        select(func.count())
                        .select_from(BusinessEvent)
                        .where(BusinessEvent.org_id == org_id)
                    )
                    == event_count
                    == 2
                )

        with Session(source_engine) as source_session, Session(target_engine) as target_session:
            assert replay_cli._account_balance_projection(
                target_session, org_id
            ) == replay_cli._account_balance_projection(source_session, org_id)
            assert (
                replay_cli._open_item_projection(target_session, org_id=org_id)
                == replay_cli._open_item_projection(source_session, org_id=org_id)
                == []
            )
            target_lines = list(
                target_session.scalars(select(VoucherLine).where(VoucherLine.org_id == org_id))
            )
            by_voucher: dict[uuid.UUID, list[VoucherLine]] = {}
            for line in target_lines:
                by_voucher.setdefault(line.voucher_id, []).append(line)
            assert len(by_voucher) == 2
            assert all(
                sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines)
                for lines in by_voucher.values()
            )
            balances = {
                row["account_code"]: row["ending_balance_fen"]
                for row in replay_cli._account_balance_projection(target_session, org_id)
            }
            assert balances["560299"] == 1_200
            assert balances["5602"] == 800
            assert balances["5603"] == 100
            assert balances["1001"] == -2_100
            assert balances["2202"] == 0


def test_calculated_accrual_components_repreview_with_new_batch_and_line_ids() -> None:
    """A real PG replay recreates payroll/labor drafts before the composite confirmation."""

    with (
        authenticated_business_database("calculated_component_replay") as source,
        authenticated_business_database("calculated_component_replay") as target,
    ):
        source_engine, source_org_id, source_evidence_id, source_authority = source
        target_engine, target_org_id, target_evidence_id, target_authority = target
        with Session(source_engine) as session:
            organization = session.get(Organization, source_org_id)
            evidence = session.get(Evidence, source_evidence_id)
            assert organization is not None and evidence is not None
            with source_authority.attributed_call(session, tool_name="finance_record_event"):
                payroll, payroll_proof, labor, labor_proof = _calculated_batches(
                    session, organization, evidence
                )
                payroll_line = session.scalar(
                    select(PayrollLine).where(PayrollLine.payroll_batch_id == payroll.batch_id)
                )
                labor_line = session.scalar(
                    select(LaborRemunerationLine).where(
                        LaborRemunerationLine.batch_id == labor.batch_id
                    )
                )
                assert payroll_line is not None and labor_line is not None
                second_employee_id = _register_second_employee(session, source_org_id)
                multi_regular = FinanceService(session).preview_payroll(
                    PreviewPayrollRequest.model_validate(
                        {
                            "org_id": source_org_id,
                            "idempotency_key": "component-multi-employee-regular-preview",
                            "batch_kind": "regular",
                            "payroll_period": "2026-03",
                            "posting_date": "2026-03-05",
                            "evidence_references": [source_evidence_id],
                            "employee_items": [
                                {
                                    "employee_id": payroll_line.employee_id,
                                    "tax_reported_salary_fen": 1_000_000,
                                    "special_additional_deduction_fen": 0,
                                    "other_legal_deduction_fen": 0,
                                },
                                {
                                    "employee_id": second_employee_id,
                                    "tax_reported_salary_fen": 1_000_000,
                                    "special_additional_deduction_fen": 0,
                                    "other_legal_deduction_fen": 0,
                                },
                            ],
                        }
                    )
                )
                assert multi_regular.status == "calculated", multi_regular
                payroll_line = session.scalar(
                    select(PayrollLine).where(
                        PayrollLine.payroll_batch_id == multi_regular.batch_id,
                        PayrollLine.employee_id == payroll_line.employee_id,
                    )
                )
                assert payroll_line is not None
                bonus = FinanceService(session).preview_payroll(
                    PreviewPayrollRequest.model_validate(
                        {
                            "org_id": source_org_id,
                            "idempotency_key": "component-bonus-preview",
                            "batch_kind": "annual_bonus",
                            "payroll_period": "2026-03",
                            "posting_date": "2026-03-05",
                            "payment_date": "2026-03-05",
                            "tax_method": "combined",
                            "evidence_references": [source_evidence_id],
                            "employee_items": [
                                {
                                    "employee_id": payroll_line.employee_id,
                                    "annual_bonus_fen": 100_000,
                                    "regular_payroll_batch_id": multi_regular.batch_id,
                                },
                                {
                                    "employee_id": second_employee_id,
                                    "annual_bonus_fen": 200_000,
                                    "regular_payroll_batch_id": multi_regular.batch_id,
                                },
                            ],
                        }
                    )
                )
                assert bonus.status == "calculated", bonus
                bonus_lines = list(
                    session.scalars(
                        select(PayrollLine).where(PayrollLine.payroll_batch_id == bonus.batch_id)
                    )
                )
                assert len(bonus_lines) == 2
                salary_source = {
                    "source_component_key": "payroll",
                    "source_open_item_key": f"salary:{payroll_line.id}",
                }
                request = RecordEventRequest.model_validate(
                    {
                        "org_id": source_org_id,
                        "idempotency_key": "calculated-accrual-replay-source",
                        "posting_date": "2026-03-05",
                        "evidence_references": [source_evidence_id],
                        "components": [
                            {
                                "key": "bonus",
                                "kind": "payroll_accrual",
                                "business_date": "2026-03-05",
                                "batch_id": bonus.batch_id,
                                "calculation_hash": bonus.calculation_hash,
                                "regular_payroll_component_keys": ["payroll"],
                                "evidence_references": [source_evidence_id],
                                "metadata": {"confirmation_note": "组合确认合并计税奖金"},
                            },
                            {
                                "key": "payroll",
                                "kind": "payroll_accrual",
                                "business_date": "2026-03-05",
                                "batch_id": multi_regular.batch_id,
                                "calculation_hash": multi_regular.calculation_hash,
                                "evidence_references": [source_evidence_id],
                                "metadata": {"confirmation_note": "组合确认两位员工正常工资"},
                            },
                            _accruals(payroll, payroll_proof, labor, labor_proof)[1],
                            {
                                "key": "salary",
                                "kind": "salary_settlement",
                                "business_date": "2026-03-05",
                                "payment_date": "2026-03-05",
                                "amount_fen": 839500,
                                "allocations": [{**salary_source, "amount_fen": 1000000}],
                                "withholding_allocations": [
                                    {
                                        **salary_source,
                                        "employee_social_insurance_items": {"pension": 80000},
                                        "employee_housing_fund_items": {"housing_fund": 70000},
                                        "individual_income_tax_fen": 10500,
                                    }
                                ],
                            },
                            {
                                "key": "labor",
                                "kind": "labor_settlement",
                                "business_date": "2026-03-05",
                                "payment_date": "2026-03-05",
                                "source_component_key": "labor-accrual",
                                "source_open_item_key": str(labor_line.id),
                                "amount_fen": 500000,
                                "settlement_mode": "net_after_withholding",
                                "metadata": {
                                    "withholding_agency_code": "TAX-LABOR-01",
                                    "withholding_agency_name": "测试税务局",
                                },
                            },
                        ],
                        "funds": [
                            {
                                "key": "cash",
                                "account_code": "1001",
                                "direction": "payment",
                                "payment_date": "2026-03-05",
                                "amount_fen": 1259500,
                                "allocations": [
                                    {"component_key": "salary", "amount_fen": 839500},
                                    {"component_key": "labor", "amount_fen": 420000},
                                ],
                            }
                        ],
                    }
                )
                posted = ComponentService(session).record(request)
                assert posted.status == "posted", posted
            session.commit()
            with source_authority.attributed_call(session, tool_name="finance_record_event"):
                tax_request = RecordEventRequest.model_validate(
                    {
                        "org_id": source_org_id,
                        "idempotency_key": "calculated-tax-replay-source",
                        "posting_date": "2026-03-31",
                        "evidence_references": [source_evidence_id],
                        "components": [
                            _relief("period", date(2026, 1, 1), date(2026, 3, 31)),
                            _sale("sale", date(2026, 3, 31), 101),
                        ],
                        "funds": [
                            {
                                "key": "cash",
                                "account_code": "1001",
                                "direction": "receipt",
                                "payment_date": "2026-03-31",
                                "amount_fen": 101,
                                "allocations": [{"component_key": "sale", "amount_fen": 101}],
                                "bank_transaction_references": [],
                            }
                        ],
                    }
                )
                tax_preview = ComponentService(session).preview(tax_request)
                assert tax_preview.status == "calculated", tax_preview
                tax_posted = ComponentService(session).record(
                    RecordEventRequest.model_validate(tax_preview.data["reviewed_request"])
                )
                assert tax_posted.status == "posted", tax_posted
            session.commit()
            source_batch_ids = {
                str(multi_regular.batch_id),
                str(bonus.batch_id),
                str(labor.batch_id),
            }
            source_line_ids = set(
                str(line_id)
                for line_id in session.scalars(
                    select(PayrollLine.id).where(PayrollLine.org_id == source_org_id)
                )
            ) | {str(labor_line.id)}
            maps = replay_cli._stable_maps(session, source_org_id)
            events = replay_cli._effective_events(session, source_org_id)
            event = next(
                row
                for row in events
                if row["idempotency_key"] == "calculated-accrual-replay-source"
            )
            operation = replay_cli._event_operation(session, event, org_id=source_org_id, maps=maps)
            tax_event = next(
                row for row in events if row["idempotency_key"] == "calculated-tax-replay-source"
            )
            tax_operation = replay_cli._event_operation(
                session, tax_event, org_id=source_org_id, maps=maps
            )

        assert operation["kind"] == "composite_event"
        assert {item["preview_tool"] for item in operation["preparations"]} == {
            "finance_preview_payroll",
            "finance_preview_labor_remuneration_batch",
        }
        preparation_keys = [item["key"] for item in operation["preparations"]]
        regular_preparation = "calculated-accrual-replay-source:prepare:payroll"
        bonus_preparation = "calculated-accrual-replay-source:prepare:bonus"
        assert preparation_keys.index(regular_preparation) < preparation_keys.index(
            bonus_preparation
        )
        bonus_preview_items = operation["preparations"][preparation_keys.index(bonus_preparation)][
            "preview_request"
        ]["employee_items"]
        assert {
            item["regular_payroll_batch_id"]["operation_key"] for item in bonus_preview_items
        } == {regular_preparation}
        bonus_component = next(
            item for item in operation["request"]["components"] if item["key"] == "bonus"
        )
        assert bonus_component["regular_payroll_component_keys"] == ["payroll"]
        encoded = json.dumps(replay_cli._jsonable(operation), sort_keys=True)
        assert not (source_batch_ids | source_line_ids) & set(
            replay_cli._UUID_TEXT.findall(encoded)
        )
        assert tax_operation["kind"] == "composite_event"
        tax_period = next(
            item for item in tax_operation["request"]["components"] if item["key"] == "period"
        )
        assert tax_period["calculation_hash"] == "0" * 64
        replay_cli._verify_operation_references(
            [operation, tax_operation],
            evidence_hashes={hashlib.sha256(b"calculated_component_replay").hexdigest()},
            org_id=str(source_org_id),
        )
        misordered = json.loads(json.dumps(replay_cli._jsonable(operation)))
        misordered["preparations"].sort(key=lambda item: item["key"] != bonus_preparation)
        with pytest.raises(
            replay_cli.ReplayError,
            match="REPLAY_PACKAGE_OPERATION_REFERENCE_MISSING",
        ):
            replay_cli._verify_operation_references(
                [misordered],
                evidence_hashes={hashlib.sha256(b"calculated_component_replay").hexdigest()},
                org_id=str(source_org_id),
            )

        with Session(target_engine) as session:
            organization = session.get(Organization, target_org_id)
            evidence = session.get(Evidence, target_evidence_id)
            assert organization is not None and evidence is not None
            with target_authority.attributed_call(session, tool_name="finance_record_event"):
                _calculated_batches(session, organization, evidence)
                _register_second_employee(session, target_org_id)
            session.commit()

            def routed_call(tool_name: str, raw_request: dict) -> dict:
                if tool_name == "finance_preview_payroll":
                    with target_authority.attributed_call(session, tool_name=tool_name):
                        result = FinanceService(session).preview_payroll(
                            PreviewPayrollRequest.model_validate(raw_request)
                        )
                    session.commit()
                    return result.model_dump(mode="json")
                if tool_name == "finance_preview_labor_remuneration_batch":
                    with target_authority.attributed_call(session, tool_name=tool_name):
                        result = LaborRemunerationService(session).preview_batch(
                            PreviewLaborRemunerationBatchRequest.model_validate(raw_request)
                        )
                    session.commit()
                    return result.model_dump(mode="json")
                typed = RecordEventRequest.model_validate(raw_request)
                if tool_name == "finance_preview_event":
                    from ai_accounting import mcp_server

                    return mcp_server._preview_with_ephemeral_attribution(
                        session,
                        target_authority.context,
                        lambda: ComponentService(session).preview(typed).model_dump(mode="json"),
                    )
                assert tool_name == "finance_record_event"
                with target_authority.attributed_call(session, tool_name=tool_name):
                    result = ComponentService(session).record(typed)
                session.commit()
                return result.model_dump(mode="json")

            original_call = replay_cli._call_tool
            replay_cli._call_tool = routed_call
            try:
                resolver = replay_cli._ReplayResolver(
                    engine=target_engine,
                    org_id=target_org_id,
                    results={},
                )
                replayed = replay_cli._execute_operation(
                    operation,
                    package_company_dir=Path.cwd(),
                    resolver=resolver,
                )
                resolver.results[operation["key"]] = replayed
                tax_replayed = replay_cli._execute_operation(
                    tax_operation,
                    package_company_dir=Path.cwd(),
                    resolver=resolver,
                )
            finally:
                replay_cli._call_tool = original_call
            assert replayed["status"] == "posted", replayed
            assert tax_replayed["status"] == "posted", tax_replayed
            assert replayed["event_id"] != str(posted.event_id)
            refreshed = {
                str(value["batch_id"])
                for value in resolver.results.values()
                if value.get("batch_id")
            }
            assert refreshed and refreshed.isdisjoint(source_batch_ids)

        with Session(source_engine) as source_session, Session(target_engine) as target_session:
            assert replay_cli._account_balance_projection(
                target_session, target_org_id
            ) == replay_cli._account_balance_projection(source_session, source_org_id)
            assert replay_cli._open_item_projection(
                target_session, org_id=target_org_id
            ) == replay_cli._open_item_projection(source_session, org_id=source_org_id)

            exported_refs = replay_cli._stable_maps(source_session, source_org_id)["open_item"]
            generated_line_refs = {
                source_id: reference
                for source_id, reference in exported_refs.items()
                if isinstance(reference.get("open_item_key"), dict)
            }
            assert generated_line_refs
            for source_id, reference in generated_line_refs.items():
                target_id = resolver.materialize(reference)
                assert target_id != source_id
                assert (
                    target_session.scalar(
                        select(OpenItem.org_id).where(OpenItem.id == uuid.UUID(target_id))
                    )
                    == target_org_id
                )

            def obligation_state(session: Session, org_id: uuid.UUID) -> list[tuple]:
                return list(
                    session.execute(
                        select(
                            OpenItem.item_type,
                            OpenItem.original_amount_fen,
                            OpenItem.settled_amount_fen,
                            OpenItem.status,
                            OpenItem.payable_category,
                        )
                        .where(OpenItem.org_id == org_id)
                        .order_by(
                            OpenItem.item_type,
                            OpenItem.original_amount_fen,
                            OpenItem.payable_category,
                        )
                    ).all()
                )

            assert obligation_state(target_session, target_org_id) == obligation_state(
                source_session, source_org_id
            )
            assert (
                source_session.scalar(
                    select(func.count())
                    .select_from(PayrollBatch)
                    .where(
                        PayrollBatch.org_id == source_org_id,
                        PayrollBatch.status == "posted",
                    )
                )
                == target_session.scalar(
                    select(func.count())
                    .select_from(PayrollBatch)
                    .where(
                        PayrollBatch.org_id == target_org_id,
                        PayrollBatch.status == "posted",
                    )
                )
                == 2
            )
            target_regular_batches = list(
                target_session.scalars(
                    select(PayrollBatch).where(
                        PayrollBatch.org_id == target_org_id,
                        PayrollBatch.status == "posted",
                        PayrollBatch.batch_kind == "regular",
                    )
                )
            )
            target_bonus = target_session.scalar(
                select(PayrollBatch).where(
                    PayrollBatch.org_id == target_org_id,
                    PayrollBatch.status == "posted",
                    PayrollBatch.batch_kind == "annual_bonus",
                )
            )
            assert len(target_regular_batches) == 1 and target_bonus is not None
            target_regular_ids = {str(batch.id) for batch in target_regular_batches}
            assert {
                item["regular_payroll_batch_id"]
                for item in target_bonus.calculation_input["request"]["employee_items"]
            } == target_regular_ids
            target_bonus_component = target_session.scalar(
                select(BusinessEventComponent).where(
                    BusinessEventComponent.org_id == target_org_id,
                    BusinessEventComponent.key == "bonus",
                )
            )
            assert target_bonus_component is not None
            local_proofs = target_bonus_component.derived["local_regular_payroll_proofs"]
            assert {proof["batch_id"] for proof in local_proofs} == target_regular_ids
            target_hashes = {
                str(batch.id): batch.calculation_hash for batch in target_regular_batches
            }
            assert all(
                proof["calculation_hash"] == target_hashes[proof["batch_id"]]
                and set(proof["employee_line_ids"]).isdisjoint(source_line_ids)
                for proof in local_proofs
            )
            assert (
                source_session.scalar(
                    select(func.count())
                    .select_from(LaborRemunerationBatch)
                    .where(
                        LaborRemunerationBatch.org_id == source_org_id,
                        LaborRemunerationBatch.status == "posted",
                    )
                )
                == target_session.scalar(
                    select(func.count())
                    .select_from(LaborRemunerationBatch)
                    .where(
                        LaborRemunerationBatch.org_id == target_org_id,
                        LaborRemunerationBatch.status == "posted",
                    )
                )
                == 1
            )
            source_period = source_session.scalar(
                select(TaxPeriod).where(TaxPeriod.org_id == source_org_id)
            )
            target_period = target_session.scalar(
                select(TaxPeriod).where(TaxPeriod.org_id == target_org_id)
            )
            assert source_period is not None and target_period is not None
            assert {
                key: target_period.calculation[key]
                for key in ("vat_accrued_fen", "vat_relief_fen", "vat_payable_fen")
            } == {
                key: source_period.calculation[key]
                for key in ("vat_accrued_fen", "vat_relief_fen", "vat_payable_fen")
            }


def test_combined_bonus_replay_prepares_two_regular_parents_before_one_formal_event() -> None:
    """Disjoint same-period regular batches remain distinct parents after replay."""

    with (
        authenticated_business_database("two_parent_bonus_replay") as source,
        authenticated_business_database("two_parent_bonus_replay") as target,
    ):
        source_engine, source_org_id, source_evidence_id, source_authority = source
        target_engine, target_org_id, target_evidence_id, target_authority = target

        def regular_request(org_id, evidence_id, employee_id, key):
            return PreviewPayrollRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": key,
                    "batch_kind": "regular",
                    "payroll_period": "2026-03",
                    "posting_date": "2026-03-05",
                    "evidence_references": [evidence_id],
                    "employee_items": [
                        {
                            "employee_id": employee_id,
                            "tax_reported_salary_fen": 1_000_000,
                            "special_additional_deduction_fen": 0,
                            "other_legal_deduction_fen": 0,
                        }
                    ],
                }
            )

        with Session(source_engine) as session:
            organization = session.get(Organization, source_org_id)
            assert organization is not None
            with source_authority.attributed_call(session, tool_name="finance_record_event"):
                from test_payroll_service import register_payroll_facts

                first_employee_id = register_payroll_facts(session, organization)
                second_employee_id = _register_second_employee(session, source_org_id)
                service = FinanceService(session)
                regular_one = service.preview_payroll(
                    regular_request(
                        source_org_id,
                        source_evidence_id,
                        first_employee_id,
                        "two-parent-regular-one",
                    )
                )
                regular_two = service.preview_payroll(
                    regular_request(
                        source_org_id,
                        source_evidence_id,
                        second_employee_id,
                        "two-parent-regular-two",
                    )
                )
                assert regular_one.status == regular_two.status == "calculated"
                bonus = service.preview_payroll(
                    PreviewPayrollRequest.model_validate(
                        {
                            "org_id": source_org_id,
                            "idempotency_key": "two-parent-bonus-preview",
                            "batch_kind": "annual_bonus",
                            "payroll_period": "2026-03",
                            "posting_date": "2026-03-05",
                            "payment_date": "2026-03-05",
                            "tax_method": "combined",
                            "evidence_references": [source_evidence_id],
                            "employee_items": [
                                {
                                    "employee_id": first_employee_id,
                                    "annual_bonus_fen": 100_000,
                                    "regular_payroll_batch_id": regular_one.batch_id,
                                },
                                {
                                    "employee_id": second_employee_id,
                                    "annual_bonus_fen": 200_000,
                                    "regular_payroll_batch_id": regular_two.batch_id,
                                },
                            ],
                        }
                    )
                )
                assert bonus.status == "calculated", bonus
                request = RecordEventRequest.model_validate(
                    {
                        "org_id": source_org_id,
                        "idempotency_key": "two-parent-bonus-source",
                        "posting_date": "2026-03-05",
                        "evidence_references": [source_evidence_id],
                        "components": [
                            {
                                "key": "bonus",
                                "kind": "payroll_accrual",
                                "business_date": "2026-03-05",
                                "batch_id": bonus.batch_id,
                                "calculation_hash": bonus.calculation_hash,
                                "regular_payroll_component_keys": ["regular-1", "regular-2"],
                                "metadata": {"confirmation_note": "组合确认双来源奖金"},
                            },
                            {
                                "key": "regular-2",
                                "kind": "payroll_accrual",
                                "business_date": "2026-03-05",
                                "batch_id": regular_two.batch_id,
                                "calculation_hash": regular_two.calculation_hash,
                                "metadata": {"confirmation_note": "组合确认正常工资二"},
                            },
                            {
                                "key": "regular-1",
                                "kind": "payroll_accrual",
                                "business_date": "2026-03-05",
                                "batch_id": regular_one.batch_id,
                                "calculation_hash": regular_one.calculation_hash,
                                "metadata": {"confirmation_note": "组合确认正常工资一"},
                            },
                        ],
                    }
                )
                preview = ComponentService(session).preview(request)
                assert preview.status == "calculated", preview
                posted = ComponentService(session).record(
                    RecordEventRequest.model_validate(preview.data["reviewed_request"])
                )
                assert posted.status == "posted", posted
            session.commit()
            source_batch_ids = {
                str(regular_one.batch_id),
                str(regular_two.batch_id),
                str(bonus.batch_id),
            }
            source_line_ids = {
                str(line_id)
                for line_id in session.scalars(
                    select(PayrollLine.id).where(
                        PayrollLine.payroll_batch_id.in_(
                            [regular_one.batch_id, regular_two.batch_id, bonus.batch_id]
                        )
                    )
                )
            }
            maps = replay_cli._stable_maps(session, source_org_id)
            event_row = next(
                row
                for row in replay_cli._effective_events(session, source_org_id)
                if row["idempotency_key"] == "two-parent-bonus-source"
            )
            operation = replay_cli._event_operation(
                session, event_row, org_id=source_org_id, maps=maps
            )

        preparation_keys = [item["key"] for item in operation["preparations"]]
        bonus_key = "two-parent-bonus-source:prepare:bonus"
        parent_keys = {
            "two-parent-bonus-source:prepare:regular-1",
            "two-parent-bonus-source:prepare:regular-2",
        }
        assert all(
            preparation_keys.index(key) < preparation_keys.index(bonus_key) for key in parent_keys
        )
        bonus_preparation = next(
            item for item in operation["preparations"] if item["key"] == bonus_key
        )
        assert {
            item["regular_payroll_batch_id"]["operation_key"]
            for item in bonus_preparation["preview_request"]["employee_items"]
        } == parent_keys
        encoded = json.dumps(replay_cli._jsonable(operation), sort_keys=True)
        assert not (source_batch_ids | source_line_ids) & set(
            replay_cli._UUID_TEXT.findall(encoded)
        )
        replay_cli._verify_operation_references(
            [operation],
            evidence_hashes={hashlib.sha256(b"two_parent_bonus_replay").hexdigest()},
            org_id=str(source_org_id),
        )

        with Session(target_engine) as session:
            organization = session.get(Organization, target_org_id)
            assert organization is not None
            with target_authority.attributed_call(session, tool_name="finance_record_event"):
                from test_payroll_service import register_payroll_facts

                register_payroll_facts(session, organization)
                _register_second_employee(session, target_org_id)
            session.commit()

            def routed_call(tool_name: str, raw_request: dict) -> dict:
                typed = (
                    PreviewPayrollRequest.model_validate(raw_request)
                    if tool_name == "finance_preview_payroll"
                    else RecordEventRequest.model_validate(raw_request)
                )
                if tool_name == "finance_preview_payroll":
                    with target_authority.attributed_call(session, tool_name=tool_name):
                        result = FinanceService(session).preview_payroll(typed)
                    session.commit()
                    return result.model_dump(mode="json")
                if tool_name == "finance_preview_event":
                    from ai_accounting import mcp_server

                    return mcp_server._preview_with_ephemeral_attribution(
                        session,
                        target_authority.context,
                        lambda: ComponentService(session).preview(typed).model_dump(mode="json"),
                    )
                assert tool_name == "finance_record_event"
                with target_authority.attributed_call(session, tool_name=tool_name):
                    result = ComponentService(session).record(typed)
                session.commit()
                return result.model_dump(mode="json")

            original_call = replay_cli._call_tool
            replay_cli._call_tool = routed_call
            try:
                resolver = replay_cli._ReplayResolver(
                    engine=target_engine,
                    org_id=target_org_id,
                    results={},
                )
                replayed = replay_cli._execute_operation(
                    operation,
                    package_company_dir=Path.cwd(),
                    resolver=resolver,
                )
            finally:
                replay_cli._call_tool = original_call
            assert replayed["status"] == "posted", replayed
            bonus_component = session.scalar(
                select(BusinessEventComponent).where(
                    BusinessEventComponent.event_id == uuid.UUID(replayed["event_id"]),
                    BusinessEventComponent.key == "bonus",
                )
            )
            assert bonus_component is not None
            proofs = bonus_component.derived["local_regular_payroll_proofs"]
            assert len(proofs) == 2
            assert {proof["component_key"] for proof in proofs} == {"regular-1", "regular-2"}
            assert {proof["batch_id"] for proof in proofs}.isdisjoint(source_batch_ids)

        with Session(source_engine) as source_session, Session(target_engine) as target_session:
            assert replay_cli._account_balance_projection(
                target_session, target_org_id
            ) == replay_cli._account_balance_projection(source_session, source_org_id)


@pytest.mark.parametrize(
    "depreciation_kind",
    ["fixed_asset_depreciation", "fixed_asset_depreciation_batch"],
)
def test_local_ready_asset_depreciation_replays_with_new_source_proof(
    depreciation_kind: str,
) -> None:
    """A late ready-asset fact and its first depreciation retain only their stable link."""

    source_prefix = f"local_asset_replay_{depreciation_kind}"
    with (
        authenticated_business_database(source_prefix) as source,
        authenticated_business_database(source_prefix) as target,
    ):
        source_engine, source_org_id, source_evidence_id, source_authority = source
        target_engine, target_org_id, _, target_authority = target
        depreciation_component = {
            "key": "first-depreciation",
            "kind": depreciation_kind,
            "business_date": "2026-03-01",
            "facts": {"depreciation_period": "2026-03", "calculation_hash": "0" * 64},
            "metadata": {"confirmation_note": "确认同笔启用来源及首月折旧"},
        }
        if depreciation_kind == "fixed_asset_depreciation":
            depreciation_component["activation_component_key"] = "ready-equipment"
            depreciation_component["facts"]["asset_id"] = None
        else:
            depreciation_component["activation_component_keys"] = ["ready-equipment"]
        source_request = RecordEventRequest.model_validate(
            {
                "org_id": source_org_id,
                "idempotency_key": "local-ready-asset-replay-source",
                "posting_date": "2026-03-31",
                "description": "补录已启用设备并计提首月折旧",
                "evidence_references": [source_evidence_id],
                "components": [
                    depreciation_component,
                    {
                        "key": "ready-equipment",
                        "kind": "fixed_asset_acquisition",
                        "business_date": "2026-02-15",
                        "facts": {
                            "category": "production_equipment",
                            "expected_use_over_one_year": True,
                            "cost_components": {
                                "purchase_price_fen": 120000,
                                "noncreditable_tax_fen": 0,
                                "transport_and_handling_fen": 0,
                                "installation_and_direct_cost_fen": 0,
                            },
                            "settlement_method": "payable",
                            "claims_creditable_input_vat": False,
                            "ready_for_use": {
                                "in_service_date": "2026-02-15",
                                "useful_life_months": 24,
                                "residual_value_fen": 0,
                                "benefit_area": "management",
                            },
                            "cost_fen": sum([120000, 0, 0, 0]),
                        },
                        "metadata": {
                            "asset_code": "FA-LOCAL-REPLAY",
                            "asset_name": "补录测试设备",
                            "counterparty": {"kind": "supplier", "name": "回放设备供应商"},
                            "due_date": "2026-04-30",
                        },
                    },
                ],
            }
        )

        with Session(source_engine) as session:
            with source_authority.attributed_call(session, tool_name="finance_record_event"):
                preview = ComponentService(session).preview(source_request)
                assert preview.status == "calculated", preview
                posted = ComponentService(session).record(
                    RecordEventRequest.model_validate(preview.data["reviewed_request"])
                )
                assert posted.status == "posted", posted
            session.commit()
            source_asset = session.scalar(
                select(FixedAsset).where(FixedAsset.org_id == source_org_id)
            )
            source_activation = session.scalar(
                select(FixedAssetActivation).where(FixedAssetActivation.org_id == source_org_id)
            )
            source_depreciation = session.scalar(
                select(FixedAssetDepreciation).where(FixedAssetDepreciation.org_id == source_org_id)
            )
            assert source_asset and source_activation and source_depreciation
            source_ids = {
                str(source_asset.id),
                str(source_activation.id),
                str(source_depreciation.id),
            }
            maps = replay_cli._stable_maps(session, source_org_id)
            event_row = next(
                row
                for row in replay_cli._effective_events(session, source_org_id)
                if row["idempotency_key"] == "local-ready-asset-replay-source"
            )
            operation = replay_cli._event_operation(
                session, event_row, org_id=source_org_id, maps=maps
            )

        assert operation["kind"] == "composite_event"
        assert operation["preparations"] == []
        exported_components = operation["request"]["components"]
        assert [item["key"] for item in exported_components] == [
            "first-depreciation",
            "ready-equipment",
        ]
        exported_depreciation = exported_components[0]
        if depreciation_kind == "fixed_asset_depreciation":
            assert exported_depreciation["activation_component_key"] == "ready-equipment"
            assert exported_depreciation["facts"]["asset_id"] is None
        else:
            assert exported_depreciation["activation_component_keys"] == ["ready-equipment"]
        assert exported_depreciation["facts"]["calculation_hash"] == "0" * 64
        encoded = json.dumps(replay_cli._jsonable(operation), sort_keys=True)
        assert not source_ids & set(replay_cli._UUID_TEXT.findall(encoded))
        replay_cli._verify_operation_references(
            [operation],
            evidence_hashes={hashlib.sha256(source_prefix.encode()).hexdigest()},
            org_id=str(source_org_id),
        )

        with Session(target_engine) as session:

            def routed_call(tool_name: str, raw_request: dict) -> dict:
                typed = RecordEventRequest.model_validate(raw_request)
                if tool_name == "finance_preview_event":
                    from ai_accounting import mcp_server

                    return mcp_server._preview_with_ephemeral_attribution(
                        session,
                        target_authority.context,
                        lambda: ComponentService(session).preview(typed).model_dump(mode="json"),
                    )
                assert tool_name == "finance_record_event"
                with target_authority.attributed_call(session, tool_name=tool_name):
                    result = ComponentService(session).record(typed)
                session.commit()
                return result.model_dump(mode="json")

            original_call = replay_cli._call_tool
            replay_cli._call_tool = routed_call
            try:
                resolver = replay_cli._ReplayResolver(
                    engine=target_engine,
                    org_id=target_org_id,
                    results={},
                )
                replayed = replay_cli._execute_operation(
                    operation,
                    package_company_dir=Path.cwd(),
                    resolver=resolver,
                )
            finally:
                replay_cli._call_tool = original_call
            assert replayed["status"] == "posted", replayed

            target_components = list(
                session.scalars(
                    select(BusinessEventComponent)
                    .where(
                        BusinessEventComponent.org_id == target_org_id,
                        BusinessEventComponent.event_id == uuid.UUID(replayed["event_id"]),
                    )
                    .order_by(BusinessEventComponent.ordinal)
                )
            )
            assert [item.key for item in target_components] == [
                "ready-equipment",
                "first-depreciation",
            ]
            depreciation_component = target_components[1]
            proofs = depreciation_component.derived["local_activation_proofs"]
            assert len(proofs) == 1
            assert proofs[0]["component_key"] == "ready-equipment"

            target_asset = session.scalar(
                select(FixedAsset).where(FixedAsset.org_id == target_org_id)
            )
            target_activation = session.scalar(
                select(FixedAssetActivation).where(FixedAssetActivation.org_id == target_org_id)
            )
            target_depreciation = session.scalar(
                select(FixedAssetDepreciation).where(FixedAssetDepreciation.org_id == target_org_id)
            )
            assert target_asset and target_activation and target_depreciation
            assert {
                str(target_asset.id),
                str(target_activation.id),
                str(target_depreciation.id),
            }.isdisjoint(source_ids)
            assert target_depreciation.asset_id == target_asset.id
            assert target_depreciation.activation_id == target_activation.id
            assert proofs[0]["asset_id"] == str(target_asset.id)
            assert proofs[0]["activation_id"] == str(target_activation.id)

        with Session(source_engine) as source_session, Session(target_engine) as target_session:
            assert replay_cli._account_balance_projection(
                target_session, target_org_id
            ) == replay_cli._account_balance_projection(source_session, source_org_id)
            assert replay_cli._open_item_projection(
                target_session, org_id=target_org_id
            ) == replay_cli._open_item_projection(source_session, org_id=source_org_id)
