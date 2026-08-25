from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.models import (
    AuditLog,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Employee,
    EmployeePayrollProfileVersion,
    Evidence,
    OpenItem,
    PayrollBatch,
    PayrollBatchEvidence,
    PayrollEventLink,
    PayrollLine,
    PayrollPolicyVersion,
    PayrollWithholdingEntitlement,
    PayrollWithholdingPaymentAllocation,
    Voucher,
)
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _policy_parameters() -> dict[str, object]:
    return {
        "contribution_rules": [
            {
                "code": "pension",
                "base_kind": "social_insurance",
                "employee_rate": "0.08",
                "employer_rate": "0.16",
                "minimum_base_fen": 0,
                "maximum_base_fen": 10_000_000,
                "rounding_rule": "half_up",
            },
            {
                "code": "housing_fund",
                "base_kind": "housing_fund",
                "employee_rate": "0.07",
                "employer_rate": "0.07",
                "minimum_base_fen": 0,
                "maximum_base_fen": 10_000_000,
                "rounding_rule": "half_up",
            },
        ],
        "income_tax": {
            "version": "r2-stdio-income-tax-2025",
            "primary_source_url": (
                "https://www.chinatax.gov.cn/chinatax/n810341/n810765/"
                "n3359382/201812/c4182700/content.html"
            ),
            "legal_basis_source_url": (
                "https://www.chinatax.gov.cn/chinatax/n810341/n810765/"
                "n3359382/201812/c4182700/content.html"
            ),
            "effective_from": "2025-07-01",
            "effective_to": "2026-06-30",
            "monthly_standard_deduction_fen": 500_000,
            "brackets": [
                {"upper_bound_fen": 3_600_000, "rate": "0.03", "quick_deduction_fen": 0},
                {
                    "upper_bound_fen": None,
                    "rate": "0.45",
                    "quick_deduction_fen": 18_192_000,
                },
            ],
        },
        "annual_bonus": {
            "version": "r2-stdio-annual-bonus-2025",
            "primary_source_url": "https://m.mof.gov.cn/czxw/202308/t20230828_3904328.htm",
            "effective_from": "2023-01-01",
            "effective_to": "2027-06-30",
            "brackets": [
                {
                    "upper_monthly_average_fen": 3_000_000,
                    "rate": "0.03",
                    "quick_deduction_fen": 0,
                },
                {
                    "upper_monthly_average_fen": None,
                    "rate": "0.45",
                    "quick_deduction_fen": 18_192_000,
                },
            ],
        },
        "payment_targets": {
            "social_insurance": {"agency_code": "SOCIAL-01", "agency_name": "社保局"},
            "housing_fund": {"agency_code": "HOUSING-01", "agency_name": "公积金中心"},
            "individual_income_tax": {"agency_code": "TAX-01", "agency_name": "税务局"},
        },
    }


def _stdio_environment(
    database_url: str,
    evidence_dir: Path,
    bank_import_dir: Path | None = None,
) -> dict[str, str]:
    repository_root = Path(__file__).parents[1]
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    environment["FINANCE_EVIDENCE_DIR"] = str(evidence_dir)
    if bank_import_dir is not None:
        environment["FINANCE_BANK_IMPORT_DIR"] = str(bank_import_dir)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            [
                str(repository_root / "src"),
                str(site_packages),
                str(site_packages / "win32"),
                str(site_packages / "win32" / "lib"),
                str(site_packages / "pywin32_system32"),
                environment.get("PYTHONPATH"),
            ],
        )
    )
    return environment


def test_r5_008_stdio_postgresql_full_payroll_lifecycle_and_salary_bank_reuse(
    tmp_path: Path,
    authenticated_stdio_bank_scope: Any,
) -> None:
    """Exercise the R5 lifecycle through STDIO and independently prove every write."""

    statement = tmp_path / "r2-payroll-bank.csv"
    statement.write_text(
        "date,amount,counterparty,memo,reference\n"
        "2026-03-05,-4250.00,员工,第一笔工资,R2-SALARY-1\n"
        "2026-03-06,-4145.00,员工,第二笔工资,R2-SALARY-2\n"
        "2026-03-07,-2400.00,社保局,社保缴纳,R2-SOCIAL\n"
        "2026-03-07,-1400.00,公积金中心,公积金缴纳,R2-HOUSING\n"
        "2026-03-07,-105.00,税务局,个税缴纳,R2-IIT\n",
        encoding="utf-8-sig",
    )
    expected_bank_rows = (
        (date(2026, 3, 5), -425_000, "员工", "第一笔工资", "R2-SALARY-1"),
        (date(2026, 3, 6), -414_500, "员工", "第二笔工资", "R2-SALARY-2"),
        (date(2026, 3, 7), -240_000, "社保局", "社保缴纳", "R2-SOCIAL"),
        (date(2026, 3, 7), -140_000, "公积金中心", "公积金缴纳", "R2-HOUSING"),
        (date(2026, 3, 7), -10_500, "税务局", "个税缴纳", "R2-IIT"),
    )
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="R2 STDIO 全生命周期企业",
                )
                scope_evidence = Evidence(
                    org_id=organization.id,
                    sha256="8" * 64,
                    original_name="r2-stdio-bank-scope.txt",
                    media_type="text/plain",
                    source="r2-stdio-test",
                    size_bytes=1,
                    storage_path="stdio/r2-stdio-bank-scope.txt",
                )
                session.add(scope_evidence)
                session.flush()
                stdio_args = authenticated_stdio_bank_scope(
                    session,
                    organization,
                    scope_evidence.id,
                    [
                        {
                            "bank_account_code": "1002",
                            "account_name": "银行存款",
                            "start_date": "2026-03-01",
                        }
                    ],
                )
                session.commit()
                org_id = str(organization.id)
                scope_evidence_id = str(scope_evidence.id)

            async def run_stdio_lifecycle() -> dict[str, Any]:
                parameters = StdioServerParameters(
                    command=getattr(sys, "_base_executable", sys.executable),
                    args=stdio_args,
                    cwd=Path(__file__).parents[1],
                    env=_stdio_environment(
                        database_url,
                        tmp_path / "evidence",
                        tmp_path,
                    ),
                )
                async with stdio_client(parameters) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as client:
                        await client.initialize()

                        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                            response = await client.call_tool(name, arguments)
                            assert response.isError is False, response.content
                            assert len(response.content) == 1
                            result = json.loads(response.content[0].text)
                            assert result.get("status") not in {
                                "rejected",
                                "needs_information",
                            }, result
                            return result

                        def assert_batch_status(batch_id: str, expected_status: str) -> None:
                            """Read through an independent connection after each MCP write."""

                            with Session(engine) as verification:
                                batch = verification.get(PayrollBatch, uuid.UUID(batch_id))
                                assert batch is not None
                                assert batch.status == expected_status

                        def assert_posted_event(event_id: str, expected_type: str) -> None:
                            with Session(engine) as verification:
                                event = verification.get(BusinessEvent, uuid.UUID(event_id))
                                assert event is not None
                                assert event.status == "posted"
                                assert event.event_type == expected_type
                                voucher = verification.scalar(
                                    select(Voucher).where(Voucher.event_id == event.id)
                                )
                                assert voucher is not None and voucher.status == "posted"

                        def assert_reversal(original_event_id: str, reversal_event_id: str) -> None:
                            with Session(engine) as verification:
                                original = verification.get(
                                    BusinessEvent, uuid.UUID(original_event_id)
                                )
                                reversal = verification.get(
                                    BusinessEvent, uuid.UUID(reversal_event_id)
                                )
                                assert original is not None and reversal is not None
                                assert original.status == "reversed"
                                assert original.reversed_by_event_id == reversal.id
                                assert reversal.status == "posted"

                        def assert_bank_pointer(bank_id: str, event_id: str | None) -> None:
                            with Session(engine) as verification:
                                bank = verification.get(BankTransaction, uuid.UUID(bank_id))
                                assert bank is not None
                                assert (
                                    str(bank.matched_event_id)
                                    if bank.matched_event_id is not None
                                    else None
                                ) == event_id

                        def assert_bank_history(
                            bank_id: str,
                            current_event_id: str | None,
                            expected_history: set[tuple[str, str | None]],
                        ) -> None:
                            """Check current pointer and append-only matches for one import."""

                            with Session(engine) as verification:
                                bank = verification.get(BankTransaction, uuid.UUID(bank_id))
                                assert bank is not None
                                matches = verification.scalars(
                                    select(BankTransactionMatch).where(
                                        BankTransactionMatch.org_id == bank.org_id,
                                        BankTransactionMatch.bank_transaction_id == bank.id,
                                    )
                                ).all()
                                actual_history = {
                                    (
                                        str(match.event_id),
                                        (
                                            str(match.invalidated_by_event_id)
                                            if match.invalidated_by_event_id is not None
                                            else None
                                        ),
                                    )
                                    for match in matches
                                }
                                assert len(matches) == len(actual_history) == len(expected_history)
                                assert actual_history == expected_history
                                for match in matches:
                                    assert (match.invalidated_at is None) == (
                                        match.invalidated_by_event_id is None
                                    )
                                active = [
                                    match
                                    for match in matches
                                    if match.invalidated_by_event_id is None
                                ]
                                assert len(active) == (0 if current_event_id is None else 1)
                                assert (
                                    str(bank.matched_event_id)
                                    if bank.matched_event_id is not None
                                    else None
                                ) == current_event_id
                                if active:
                                    assert str(active[0].event_id) == current_event_id

                        def assert_registered_employee(employee_id: str) -> None:
                            with Session(engine) as verification:
                                employee_row = verification.get(Employee, uuid.UUID(employee_id))
                                assert employee_row is not None
                                assert employee_row.org_id == uuid.UUID(org_id)
                                assert employee_row.employee_code == "R2-STDIO-E-001"
                                assert employee_row.name == "R2 工资员工"
                                assert employee_row.employment_start_date == date(2026, 3, 1)
                                assert employee_row.tax_withholding_start_date == date(2026, 3, 1)
                                assert employee_row.status == "active"

                        def assert_profile(employee_id: str, profile_version_id: str) -> None:
                            with Session(engine) as verification:
                                profile = verification.scalar(
                                    select(EmployeePayrollProfileVersion).where(
                                        EmployeePayrollProfileVersion.org_id == uuid.UUID(org_id),
                                        EmployeePayrollProfileVersion.employee_id
                                        == uuid.UUID(employee_id),
                                    )
                                )
                                assert profile is not None
                                assert str(profile.id) == profile_version_id
                                assert profile.effective_from == date(2026, 3, 1)
                                assert profile.effective_to is None
                                assert profile.expense_role == "payroll_management_expense"
                                assert profile.social_insurance_base_fen == 1_000_000
                                assert profile.housing_fund_base_fen == 1_000_000
                                assert profile.resident_employee is True

                        def assert_policy(policy_version_id: str) -> None:
                            with Session(engine) as verification:
                                policy = verification.scalar(
                                    select(PayrollPolicyVersion).where(
                                        PayrollPolicyVersion.org_id == uuid.UUID(org_id),
                                        PayrollPolicyVersion.version == "r2-stdio-2025",
                                    )
                                )
                                assert policy is not None
                                assert str(policy.id) == policy_version_id
                                assert policy.region == "R2-STDIO"
                                assert policy.effective_from == date(2025, 7, 1)
                                assert policy.effective_to == date(2026, 6, 30)
                                assert policy.source_url == (
                                    "https://www.chinatax.gov.cn/chinatax/n810341/"
                                    "n810765/n3359382/201812/c4182700/content.html"
                                )
                                assert (
                                    policy.parameters["payment_targets"]
                                    == _policy_parameters()["payment_targets"]
                                )
                                assert policy.parameters["income_tax"]["version"] == (
                                    "r2-stdio-income-tax-2025"
                                )
                                assert policy.parameters["annual_bonus"]["version"] == (
                                    "r2-stdio-annual-bonus-2025"
                                )
                                assert {
                                    (item["code"], item["base_kind"])
                                    for item in policy.parameters["contribution_rules"]
                                } == {
                                    ("pension", "social_insurance"),
                                    ("housing_fund", "housing_fund"),
                                }

                        def assert_exact_payroll_links(
                            event_id: str,
                            expected: set[tuple[str, str, str | None, str | None]],
                        ) -> None:
                            """Prove each canonical source edge for one event."""

                            with Session(engine) as verification:
                                links = verification.scalars(
                                    select(PayrollEventLink).where(
                                        PayrollEventLink.org_id == uuid.UUID(org_id),
                                        PayrollEventLink.event_id == uuid.UUID(event_id),
                                    )
                                ).all()
                                actual = {
                                    (
                                        link.link_kind,
                                        str(link.payroll_batch_id),
                                        (
                                            str(link.source_payment_event_id)
                                            if link.source_payment_event_id is not None
                                            else None
                                        ),
                                        (
                                            str(link.source_open_item_id)
                                            if link.source_open_item_id is not None
                                            else None
                                        ),
                                    )
                                    for link in links
                                }
                                assert len(links) == len(actual) == len(expected)
                                assert actual == expected

                        def assert_statutory_source_links(
                            event_id: str,
                            batch_id: str,
                            expected_categories: set[str],
                        ) -> None:
                            """Verify each expected statutory source relation exactly once."""

                            with Session(engine) as verification:
                                event = verification.get(BusinessEvent, uuid.UUID(event_id))
                                assert event is not None and event.org_id == uuid.UUID(org_id)
                                links = verification.scalars(
                                    select(PayrollEventLink).where(
                                        PayrollEventLink.org_id == event.org_id,
                                        PayrollEventLink.event_id == event.id,
                                        PayrollEventLink.link_kind == "statutory_payment",
                                    )
                                ).all()
                                expected_items = verification.scalars(
                                    select(OpenItem).where(
                                        OpenItem.org_id == event.org_id,
                                        OpenItem.payable_category.in_(expected_categories),
                                        OpenItem.status == "settled",
                                    )
                                ).all()
                                assert expected_items
                                expected = {
                                    (
                                        "statutory_payment",
                                        batch_id,
                                        str(item.source_event_id),
                                        str(item.id),
                                    )
                                    for item in expected_items
                                }
                                actual = {
                                    (
                                        link.link_kind,
                                        str(link.payroll_batch_id),
                                        (
                                            str(link.source_payment_event_id)
                                            if link.source_payment_event_id is not None
                                            else None
                                        ),
                                        (
                                            str(link.source_open_item_id)
                                            if link.source_open_item_id is not None
                                            else None
                                        ),
                                    )
                                    for link in links
                                }
                                assert len(links) == len(actual) == len(expected)
                                assert actual == expected

                        def assert_payment_reversal_links(
                            original_event_id: str, reversal_event_id: str
                        ) -> None:
                            """A payment reversal must invert each normalized source edge."""

                            with Session(engine) as verification:
                                original_links = verification.scalars(
                                    select(PayrollEventLink).where(
                                        PayrollEventLink.org_id == uuid.UUID(org_id),
                                        PayrollEventLink.event_id == uuid.UUID(original_event_id),
                                        PayrollEventLink.link_kind.in_(
                                            ("salary_payment", "statutory_payment")
                                        ),
                                    )
                                ).all()
                                assert original_links
                                expected = {
                                    (
                                        "reversal",
                                        str(link.payroll_batch_id),
                                        original_event_id,
                                        (
                                            str(link.source_open_item_id)
                                            if link.source_open_item_id is not None
                                            else None
                                        ),
                                    )
                                    for link in original_links
                                }
                            assert_exact_payroll_links(reversal_event_id, expected)

                        def assert_evidence_is_finally_immutable(evidence_id: str) -> None:
                            """The transport succeeded; now attack the sealed evidence directly."""

                            with Session(engine) as verification:
                                evidence_row = verification.get(Evidence, uuid.UUID(evidence_id))
                                assert evidence_row is not None
                                evidence_row.sha256 = "0" * 64
                                with pytest.raises(DBAPIError) as error:
                                    verification.commit()
                                assert getattr(error.value.orig, "sqlstate", None) == "P0001"
                                verification.rollback()

                        generated_period = await call(
                            "finance_generate_accounting_period",
                            {
                                "request": {
                                    "org_id": org_id,
                                    "period_month": "2026-03",
                                    "idempotency_key": "r2-stdio-generate-2026-03",
                                    "confirmation_note": "STDIO 全生命周期显式生成三月期间",
                                    "evidence_references": [scope_evidence_id],
                                }
                            },
                        )
                        assert generated_period["status"] == "posted"

                        evidence = await call(
                            "finance_register_evidence",
                            {
                                "request": {
                                    "org_id": org_id,
                                    "source": "r2_stdio_payroll_input",
                                    "content_base64": base64.b64encode(
                                        "工资、代扣和缴款依据".encode()
                                    ).decode(),
                                    "original_name": "r2-payroll-input.txt",
                                    "media_type": "text/plain",
                                    "metadata": {"purpose": "r2-008"},
                                }
                            },
                        )
                        with Session(engine) as verification:
                            evidence_row = verification.get(
                                Evidence, uuid.UUID(evidence["evidence_id"])
                            )
                            assert evidence_row is not None
                            assert evidence_row.org_id == uuid.UUID(org_id)
                            assert evidence_row.sha256 == evidence["sha256"]
                        import_request = {
                            "org_id": org_id,
                            "bank_account_code": "1002",
                            "source_file_name": statement.name,
                            "file_format": "csv",
                            "column_mapping": {
                                "booking_date": "date",
                                "amount": "amount",
                                "counterparty": "counterparty",
                                "memo": "memo",
                                "external_id": "reference",
                            },
                        }
                        import_preview = await call(
                            "finance_preview_bank_statement_import",
                            {
                                "request": import_request,
                            },
                        )
                        assert import_preview["status"] == "calculated"
                        imported = await call(
                            "finance_confirm_bank_statement_import",
                            {
                                "request": import_request
                                | {
                                    "calculation_hash": import_preview["calculation_hash"],
                                    "idempotency_key": "r2-stdio-bank-import",
                                }
                            },
                        )
                        imported_ids = imported["data"]["imported_transaction_ids"]
                        assert imported["status"] == "posted"
                        assert imported["data"]["imported_count"] == 5
                        assert len(imported_ids) == len(expected_bank_rows)
                        assert len(set(imported_ids)) == len(expected_bank_rows)
                        with Session(engine) as verification:
                            for bank_id, expected in zip(
                                imported_ids, expected_bank_rows, strict=True
                            ):
                                bank = verification.get(BankTransaction, uuid.UUID(bank_id))
                                assert bank is not None
                                assert bank.org_id == uuid.UUID(org_id)
                                assert (
                                    bank.booking_date,
                                    bank.amount_fen,
                                    bank.counterparty_name,
                                    bank.memo,
                                    bank.external_id,
                                ) == expected
                                assert bank.bank_account_code == "1002"
                                assert bank.currency == "CNY"
                                assert bank.matched_event_id is None
                                assert (
                                    verification.scalars(
                                        select(BankTransactionMatch).where(
                                            BankTransactionMatch.org_id == bank.org_id,
                                            BankTransactionMatch.bank_transaction_id == bank.id,
                                        )
                                    ).all()
                                    == []
                                )
                        bank_ids = iter(imported_ids)
                        salary_bank_first = next(bank_ids)
                        salary_bank_second = next(bank_ids)
                        social_bank = next(bank_ids)
                        housing_bank = next(bank_ids)
                        income_tax_bank = next(bank_ids)

                        employee = await call(
                            "finance_register_employee",
                            {
                                "request": {
                                    "org_id": org_id,
                                    "employee_code": "R2-STDIO-E-001",
                                    "name": "R2 工资员工",
                                    "employment_start_date": "2026-03-01",
                                    "tax_withholding_start_date": "2026-03-01",
                                    "status": "active",
                                }
                            },
                        )
                        employee_id = employee["employee_id"]
                        assert_registered_employee(employee_id)
                        profile_registration = await call(
                            "finance_register_employee_profile_version",
                            {
                                "request": {
                                    "org_id": org_id,
                                    "employee_id": employee_id,
                                    "effective_from": "2026-03-01",
                                    "expense_role": "payroll_management_expense",
                                    "social_insurance_base_fen": 1_000_000,
                                    "housing_fund_base_fen": 1_000_000,
                                    "resident_employee": True,
                                }
                            },
                        )
                        assert profile_registration["status"] == "registered"
                        assert_profile(employee_id, profile_registration["profile_version_id"])
                        policy_registration = await call(
                            "finance_register_payroll_policy_version",
                            {
                                "request": {
                                    "org_id": org_id,
                                    "region": "R2-STDIO",
                                    "effective_from": "2025-07-01",
                                    "effective_to": "2026-06-30",
                                    "version": "r2-stdio-2025",
                                    "source_url": (
                                        "https://www.chinatax.gov.cn/chinatax/n810341/"
                                        "n810765/n3359382/201812/c4182700/content.html"
                                    ),
                                    "parameters": _policy_parameters(),
                                }
                            },
                        )
                        assert policy_registration["status"] == "registered"
                        assert_policy(policy_registration["policy_version_id"])
                        preview = await call(
                            "finance_preview_payroll",
                            {
                                "request": {
                                    "org_id": org_id,
                                    "idempotency_key": "r2-stdio-preview",
                                    "batch_kind": "regular",
                                    "payroll_period": "2026-03",
                                    "posting_date": "2026-03-05",
                                    "payment_date": "2026-03-05",
                                    "evidence_references": [evidence["evidence_id"]],
                                    "employee_items": [
                                        {
                                            "employee_id": employee_id,
                                            "tax_reported_salary_fen": 1_000_000,
                                            "special_additional_deduction_fen": 0,
                                            "other_legal_deduction_fen": 0,
                                        }
                                    ],
                                }
                            },
                        )
                        assert preview["calculation_hash"]
                        assert preview["trace"]
                        assert len(preview["data"]["lines"]) == 1
                        assert_batch_status(preview["batch_id"], "calculated")
                        with Session(engine) as verification:
                            batch = verification.get(PayrollBatch, uuid.UUID(preview["batch_id"]))
                            profile = verification.scalar(
                                select(EmployeePayrollProfileVersion).where(
                                    EmployeePayrollProfileVersion.org_id == uuid.UUID(org_id),
                                    EmployeePayrollProfileVersion.employee_id
                                    == uuid.UUID(employee_id),
                                )
                            )
                            policy = verification.scalar(
                                select(PayrollPolicyVersion).where(
                                    PayrollPolicyVersion.org_id == uuid.UUID(org_id),
                                    PayrollPolicyVersion.version == "r2-stdio-2025",
                                )
                            )
                            assert batch is not None and profile is not None and policy is not None
                            assert batch.org_id == uuid.UUID(org_id)
                            assert batch.status == "calculated"
                            assert batch.batch_kind == "regular"
                            assert batch.payroll_period == "2026-03"
                            assert batch.posting_date == date(2026, 3, 5)
                            assert batch.payment_date == date(2026, 3, 5)
                            assert batch.calculation_hash == preview["calculation_hash"]
                            assert batch.request_payload_hash is not None
                            assert batch.policy_version_id == policy.id
                            assert batch.policy_snapshot["id"] == str(policy.id)
                            assert batch.calculation_trace == preview["trace"]
                            assert batch.calculation_input["request"]["evidence_references"] == [
                                evidence["evidence_id"]
                            ]
                            lines = verification.scalars(
                                select(PayrollLine).where(PayrollLine.payroll_batch_id == batch.id)
                            ).all()
                            assert len(lines) == 1
                            line = lines[0]
                            assert line.org_id == batch.org_id
                            assert line.employee_id == uuid.UUID(employee_id)
                            assert line.employee_payroll_profile_version_id == profile.id
                            assert line.tax_reported_salary_fen == 1_000_000
                            assert line.calculation_trace == preview["data"]["lines"][0]["trace"]
                            evidence_edges = verification.scalars(
                                select(PayrollBatchEvidence).where(
                                    PayrollBatchEvidence.org_id == batch.org_id,
                                    PayrollBatchEvidence.payroll_batch_id == batch.id,
                                )
                            ).all()
                            assert [str(edge.evidence_id) for edge in evidence_edges] == [
                                evidence["evidence_id"]
                            ]
                        confirmed = await call(
                            "finance_confirm_payroll",
                            {
                                "request": {
                                    "org_id": org_id,
                                    "batch_id": preview["batch_id"],
                                    "calculation_hash": preview["calculation_hash"],
                                    "idempotency_key": "r2-stdio-confirm",
                                }
                            },
                        )
                        assert_posted_event(confirmed["event_id"], "payroll_accrual")
                        assert_batch_status(preview["batch_id"], "posted")
                        assert_exact_payroll_links(
                            confirmed["event_id"],
                            {("payroll_accrual", preview["batch_id"], None, None)},
                        )
                        assert_evidence_is_finally_immutable(evidence["evidence_id"])
                        lifecycle = await call(
                            "finance_get_payroll_batch",
                            {"org_id": org_id, "batch_id": preview["batch_id"]},
                        )
                        assert lifecycle["lifecycle"]["evidence"] == [
                            {
                                "id": evidence["evidence_id"],
                                "sha256": evidence["sha256"],
                                "original_name": "r2-payroll-input.txt",
                                "source": "r2_stdio_payroll_input",
                                "media_type": "text/plain",
                                "size_bytes": len("工资、代扣和缴款依据".encode()),
                            }
                        ]
                        assert lifecycle["lifecycle"]["policy"]["official_sources"]
                        items = lifecycle["lifecycle"]["open_items"]
                        salary = next(
                            item for item in items if item["payable_category"] == "salary"
                        )

                        def salary_payment(
                            *,
                            key: str,
                            amount_fen: int,
                            bank_id: str,
                            tax_fen: int,
                        ) -> dict[str, Any]:
                            return {
                                "request": {
                                    "org_id": org_id,
                                    "idempotency_key": key,
                                    "event_type": "salary_payment",
                                    "bank_account_code": "1002",
                                    "business_dates": {
                                        "business_date": "2026-03-05",
                                        "posting_date": "2026-03-05",
                                        "payment_date": "2026-03-05",
                                    },
                                    "amounts": {"amount_fen": amount_fen},
                                    "allocations": [
                                        {"open_item_id": salary["id"], "amount_fen": 500_000}
                                    ],
                                    "salary_withholding_allocations": [
                                        {
                                            "open_item_id": salary["id"],
                                            "employee_social_insurance_items": {"pension": 40_000},
                                            "employee_housing_fund_items": {"housing_fund": 35_000},
                                            "individual_income_tax_fen": tax_fen,
                                        }
                                    ],
                                    "bank_transaction_references": [{"id": bank_id}],
                                }
                            }

                        salary_first = await call(
                            "finance_record_event",
                            salary_payment(
                                key="r2-stdio-salary-1",
                                amount_fen=425_000,
                                bank_id=salary_bank_first,
                                tax_fen=0,
                            ),
                        )
                        assert_posted_event(salary_first["event_id"], "salary_payment")
                        assert_bank_pointer(salary_bank_first, salary_first["event_id"])
                        assert_bank_history(
                            salary_bank_first,
                            salary_first["event_id"],
                            {(salary_first["event_id"], None)},
                        )
                        assert_exact_payroll_links(
                            salary_first["event_id"],
                            {
                                (
                                    "salary_payment",
                                    preview["batch_id"],
                                    None,
                                    salary["id"],
                                )
                            },
                        )
                        salary_first_reversal = await call(
                            "finance_reverse_event",
                            {
                                "request": {
                                    "org_id": org_id,
                                    "event_id": salary_first["event_id"],
                                    "idempotency_key": "r5-stdio-salary-1-reversal",
                                    "reason": "R5 工资流水纠错",
                                    "posting_date": "2026-03-06",
                                }
                            },
                        )
                        assert_reversal(salary_first["event_id"], salary_first_reversal["event_id"])
                        assert_bank_pointer(salary_bank_first, None)
                        assert_bank_history(
                            salary_bank_first,
                            None,
                            {
                                (
                                    salary_first["event_id"],
                                    salary_first_reversal["event_id"],
                                )
                            },
                        )
                        assert_exact_payroll_links(
                            salary_first_reversal["event_id"],
                            {
                                (
                                    "reversal",
                                    preview["batch_id"],
                                    salary_first["event_id"],
                                    salary["id"],
                                )
                            },
                        )
                        salary_reissued = await call(
                            "finance_record_event",
                            salary_payment(
                                key="r5-stdio-salary-1-reissued",
                                amount_fen=425_000,
                                bank_id=salary_bank_first,
                                tax_fen=0,
                            ),
                        )
                        assert_posted_event(salary_reissued["event_id"], "salary_payment")
                        assert_bank_pointer(salary_bank_first, salary_reissued["event_id"])
                        assert_bank_history(
                            salary_bank_first,
                            salary_reissued["event_id"],
                            {
                                (
                                    salary_first["event_id"],
                                    salary_first_reversal["event_id"],
                                ),
                                (salary_reissued["event_id"], None),
                            },
                        )
                        assert_exact_payroll_links(
                            salary_reissued["event_id"],
                            {
                                (
                                    "salary_payment",
                                    preview["batch_id"],
                                    None,
                                    salary["id"],
                                )
                            },
                        )
                        salary_second = await call(
                            "finance_record_event",
                            salary_payment(
                                key="r2-stdio-salary-2",
                                amount_fen=414_500,
                                bank_id=salary_bank_second,
                                tax_fen=10_500,
                            ),
                        )
                        assert_posted_event(salary_second["event_id"], "salary_payment")
                        assert_bank_pointer(salary_bank_second, salary_second["event_id"])
                        assert_bank_history(
                            salary_bank_second,
                            salary_second["event_id"],
                            {(salary_second["event_id"], None)},
                        )
                        assert_exact_payroll_links(
                            salary_second["event_id"],
                            {
                                (
                                    "salary_payment",
                                    preview["batch_id"],
                                    None,
                                    salary["id"],
                                )
                            },
                        )
                        lifecycle_before_reversal = await call(
                            "finance_get_payroll_batch",
                            {"org_id": org_id, "batch_id": preview["batch_id"]},
                        )
                        payable_items = lifecycle_before_reversal["lifecycle"]["open_items"]

                        def statutory_payment(
                            event_type: str,
                            key: str,
                            bank_id: str,
                            categories: set[str],
                        ) -> dict[str, Any]:
                            allocations = [
                                {
                                    "open_item_id": item["id"],
                                    "amount_fen": item["original_amount_fen"],
                                }
                                for item in payable_items
                                if item["payable_category"] in categories
                                and item["status"] in {"open", "partial"}
                            ]
                            return {
                                "request": {
                                    "org_id": org_id,
                                    "idempotency_key": key,
                                    "event_type": event_type,
                                    "bank_account_code": "1002",
                                    "business_dates": {
                                        "business_date": "2026-03-07",
                                        "posting_date": "2026-03-07",
                                        "payment_date": "2026-03-07",
                                    },
                                    "amounts": {
                                        "amount_fen": sum(
                                            item["amount_fen"] for item in allocations
                                        )
                                    },
                                    "allocations": allocations,
                                    "bank_transaction_references": [{"id": bank_id}],
                                }
                            }

                        social = await call(
                            "finance_record_event",
                            statutory_payment(
                                "social_insurance_payment",
                                "r2-stdio-social",
                                social_bank,
                                {"employer_social", "withheld_employee_social"},
                            ),
                        )
                        assert_posted_event(social["event_id"], "social_insurance_payment")
                        assert_bank_pointer(social_bank, social["event_id"])
                        assert_bank_history(
                            social_bank, social["event_id"], {(social["event_id"], None)}
                        )
                        assert_statutory_source_links(
                            social["event_id"],
                            preview["batch_id"],
                            {"employer_social", "withheld_employee_social"},
                        )
                        housing = await call(
                            "finance_record_event",
                            statutory_payment(
                                "housing_fund_payment",
                                "r2-stdio-housing",
                                housing_bank,
                                {"employer_housing", "withheld_employee_housing"},
                            ),
                        )
                        assert_posted_event(housing["event_id"], "housing_fund_payment")
                        assert_bank_pointer(housing_bank, housing["event_id"])
                        assert_bank_history(
                            housing_bank, housing["event_id"], {(housing["event_id"], None)}
                        )
                        assert_statutory_source_links(
                            housing["event_id"],
                            preview["batch_id"],
                            {"employer_housing", "withheld_employee_housing"},
                        )
                        income_tax = await call(
                            "finance_record_event",
                            statutory_payment(
                                "individual_income_tax_payment",
                                "r2-stdio-income-tax",
                                income_tax_bank,
                                {"individual_income_tax"},
                            ),
                        )
                        assert_posted_event(income_tax["event_id"], "individual_income_tax_payment")
                        assert_bank_pointer(income_tax_bank, income_tax["event_id"])
                        assert_bank_history(
                            income_tax_bank,
                            income_tax["event_id"],
                            {(income_tax["event_id"], None)},
                        )
                        assert_statutory_source_links(
                            income_tax["event_id"],
                            preview["batch_id"],
                            {"individual_income_tax"},
                        )
                        settled_lifecycle = await call(
                            "finance_get_payroll_batch",
                            {"org_id": org_id, "batch_id": preview["batch_id"]},
                        )
                        active_open_items = [
                            item
                            for item in settled_lifecycle["lifecycle"]["open_items"]
                            if item["status"] in {"open", "partial"}
                        ]
                        assert not active_open_items, [
                            (
                                item["payable_category"],
                                item["original_amount_fen"],
                                item["settled_amount_fen"],
                                item["status"],
                            )
                            for item in active_open_items
                        ]
                        assert all(
                            (
                                item["status"] == "settled"
                                and item["settled_amount_fen"] == item["original_amount_fen"]
                            )
                            or (item["status"] == "reversed" and item["settled_amount_fen"] == 0)
                            for item in settled_lifecycle["lifecycle"]["open_items"]
                        )
                        reverse_order = [
                            income_tax,
                            housing,
                            social,
                            salary_second,
                        ]
                        bank_by_reversed_event = {
                            income_tax["event_id"]: income_tax_bank,
                            housing["event_id"]: housing_bank,
                            social["event_id"]: social_bank,
                            salary_second["event_id"]: salary_bank_second,
                        }
                        reversals = []
                        for index, item in enumerate(reverse_order, start=1):
                            reversal = await call(
                                "finance_reverse_event",
                                {
                                    "request": {
                                        "org_id": org_id,
                                        "event_id": item["event_id"],
                                        "idempotency_key": f"r2-stdio-reverse-{index}",
                                        "reason": "R2-008 生命周期冲正",
                                        "posting_date": "2026-03-08",
                                    }
                                },
                            )
                            assert_reversal(item["event_id"], reversal["event_id"])
                            assert_bank_pointer(bank_by_reversed_event[item["event_id"]], None)
                            assert_bank_history(
                                bank_by_reversed_event[item["event_id"]],
                                None,
                                {(item["event_id"], reversal["event_id"])},
                            )
                            assert_payment_reversal_links(item["event_id"], reversal["event_id"])
                            reversals.append(reversal)
                        return {
                            "evidence": evidence,
                            "preview": preview,
                            "confirmed": confirmed,
                            "salary_first": salary_first,
                            "salary_first_reversal": salary_first_reversal,
                            "salary_reissued": salary_reissued,
                            "salary_bank_first": salary_bank_first,
                            "salary_second": salary_second,
                            "social": social,
                            "housing": housing,
                            "income_tax": income_tax,
                            "settled_lifecycle": settled_lifecycle,
                            "reversals": reversals,
                        }

            result = asyncio.run(run_stdio_lifecycle())
            batch_id = result["preview"]["batch_id"]
            with Session(engine) as session:
                batch = session.get(PayrollBatch, batch_id)
                assert batch is not None and batch.status == "posted"
                assert batch.business_event_id == uuid.UUID(result["confirmed"]["event_id"])
                assert batch.confirmed_by is None
                assert batch.confirmed_at is not None
                assert batch.calculation_hash == result["preview"]["calculation_hash"]
                evidence_edges = session.scalars(
                    select(PayrollBatchEvidence).where(
                        PayrollBatchEvidence.org_id == batch.org_id,
                        PayrollBatchEvidence.payroll_batch_id == batch.id,
                    )
                ).all()
                assert [
                    (edge.payroll_batch_id, str(edge.evidence_id)) for edge in evidence_edges
                ] == [(batch.id, result["evidence"]["evidence_id"])]
                evidence = session.get(Evidence, uuid.UUID(result["evidence"]["evidence_id"]))
                assert evidence is not None
                assert evidence.org_id == batch.org_id
                assert evidence.sha256 == result["evidence"]["sha256"]
                assert evidence.original_name == "r2-payroll-input.txt"
                assert evidence.source == "r2_stdio_payroll_input"
                assert evidence.media_type == "text/plain"
                assert evidence.size_bytes == len("工资、代扣和缴款依据".encode())
                assert evidence.metadata_json == {"purpose": "r2-008"}
                lines = session.scalars(
                    select(PayrollLine).where(PayrollLine.payroll_batch_id == batch.id)
                ).all()
                assert len(lines) == 1
                line = lines[0]
                assert (
                    line.gross_salary_fen,
                    line.net_salary_fen,
                    line.employee_social_insurance_fen,
                    line.employer_social_insurance_fen,
                    line.employee_housing_fund_fen,
                    line.employer_housing_fund_fen,
                    line.individual_income_tax_fen,
                ) == (1_000_000, 839_500, 80_000, 160_000, 70_000, 70_000, 10_500)
                assert line.employee_social_insurance_items == {"pension": 80_000}
                assert line.employee_housing_fund_items == {"housing_fund": 70_000}
                assert line.individual_income_tax_fen == 10_500
                entitlements = session.scalars(
                    select(PayrollWithholdingEntitlement).where(
                        PayrollWithholdingEntitlement.org_id == batch.org_id,
                        PayrollWithholdingEntitlement.payroll_line_id == line.id,
                    )
                ).all()
                allocations = session.scalars(
                    select(PayrollWithholdingPaymentAllocation).where(
                        PayrollWithholdingPaymentAllocation.org_id == batch.org_id
                    )
                ).all()
                entitlement_by_kind = {
                    (item.contribution_group, item.insurance_kind): item for item in entitlements
                }
                assert {
                    key: (item.payroll_line_id, item.amount_fen)
                    for key, item in entitlement_by_kind.items()
                } == {
                    ("employee_social_insurance", "pension"): (line.id, 80_000),
                    ("employee_housing_fund", "housing_fund"): (line.id, 70_000),
                    ("individual_income_tax", "individual_income_tax"): (line.id, 10_500),
                }
                assert len({item.id for item in entitlements}) == len(entitlements) == 3
                expected_payment_allocations = {
                    (
                        result["salary_first"]["event_id"],
                        entitlement_by_kind[("employee_social_insurance", "pension")].id,
                        40_000,
                        result["salary_first_reversal"]["event_id"],
                    ),
                    (
                        result["salary_first"]["event_id"],
                        entitlement_by_kind[("employee_housing_fund", "housing_fund")].id,
                        35_000,
                        result["salary_first_reversal"]["event_id"],
                    ),
                    (
                        result["salary_reissued"]["event_id"],
                        entitlement_by_kind[("employee_social_insurance", "pension")].id,
                        40_000,
                        None,
                    ),
                    (
                        result["salary_reissued"]["event_id"],
                        entitlement_by_kind[("employee_housing_fund", "housing_fund")].id,
                        35_000,
                        None,
                    ),
                    (
                        result["salary_second"]["event_id"],
                        entitlement_by_kind[("employee_social_insurance", "pension")].id,
                        40_000,
                        result["reversals"][3]["event_id"],
                    ),
                    (
                        result["salary_second"]["event_id"],
                        entitlement_by_kind[("employee_housing_fund", "housing_fund")].id,
                        35_000,
                        result["reversals"][3]["event_id"],
                    ),
                    (
                        result["salary_second"]["event_id"],
                        entitlement_by_kind[("individual_income_tax", "individual_income_tax")].id,
                        10_500,
                        result["reversals"][3]["event_id"],
                    ),
                }
                actual_payment_allocations = {
                    (
                        str(item.payment_event_id),
                        item.entitlement_id,
                        item.amount_fen,
                        str(item.reversed_by_event_id)
                        if item.reversed_by_event_id is not None
                        else None,
                    )
                    for item in allocations
                }
                assert (
                    len(allocations)
                    == len(actual_payment_allocations)
                    == len(expected_payment_allocations)
                )
                assert actual_payment_allocations == expected_payment_allocations
                for item in allocations:
                    assert item.org_id == batch.org_id
                    assert item.reversed is (item.reversed_by_event_id is not None)
                links = session.scalars(
                    select(PayrollEventLink).where(PayrollEventLink.org_id == batch.org_id)
                ).all()
                assert {item.link_kind for item in links} >= {
                    "payroll_accrual",
                    "salary_payment",
                    "statutory_payment",
                }
                bank_rows = session.scalars(
                    select(BankTransaction).where(BankTransaction.org_id == batch.org_id)
                ).all()
                bank_matches = session.scalars(
                    select(BankTransactionMatch).where(BankTransactionMatch.org_id == batch.org_id)
                ).all()
                assert len(bank_rows) == 5
                salary_reuse_row = next(
                    row for row in bank_rows if str(row.id) == result["salary_bank_first"]
                )
                salary_reuse_matches = [
                    match
                    for match in bank_matches
                    if match.bank_transaction_id == salary_reuse_row.id
                ]
                assert len(salary_reuse_matches) == 2
                assert (
                    str(salary_reuse_row.matched_event_id) == result["salary_reissued"]["event_id"]
                )
                assert {str(match.event_id) for match in salary_reuse_matches} == {
                    result["salary_first"]["event_id"],
                    result["salary_reissued"]["event_id"],
                }
                assert (
                    sum(match.invalidated_by_event_id is None for match in salary_reuse_matches)
                    == 1
                )
                assert any(
                    str(match.invalidated_by_event_id)
                    == result["salary_first_reversal"]["event_id"]
                    for match in salary_reuse_matches
                )
                other_rows = [row for row in bank_rows if row.id != salary_reuse_row.id]
                other_matches = [
                    match
                    for match in bank_matches
                    if match.bank_transaction_id != salary_reuse_row.id
                ]
                assert len(other_rows) == len(other_matches) == 4
                assert all(row.matched_event_id is None for row in other_rows)
                assert all(match.invalidated_by_event_id is not None for match in other_matches)
                vouchers = session.scalars(
                    select(Voucher).where(Voucher.org_id == batch.org_id)
                ).all()
                assert vouchers and all(
                    sum(line.debit_fen for line in voucher.lines)
                    == sum(line.credit_fen for line in voucher.lines)
                    and sum(line.debit_fen for line in voucher.lines) > 0
                    for voucher in vouchers
                )
                events = session.scalars(
                    select(BusinessEvent).where(BusinessEvent.org_id == batch.org_id)
                ).all()
                assert all(event.request_payload_hash for event in events)
                assert any(event.reversed_by_event_id for event in events)
                assert session.scalars(
                    select(AuditLog).where(AuditLog.org_id == batch.org_id)
                ).all()
                assert session.scalars(
                    select(OpenItem).where(OpenItem.org_id == batch.org_id)
                ).all()
        finally:
            engine.dispose()


def test_r7_005_stdio_bank_import_errors_are_structured_and_redacted(
    tmp_path: Path,
) -> None:
    """The STDIO boundary must not echo malformed statement content or paths."""

    sentinels = {
        "date": "R7_DATE_SENTINEL_" + "X" * 160,
        "amount": "R7_AMOUNT_SENTINEL_" + "Y" * 160,
        "column": "R7_COLUMN_SENTINEL_" + "Z" * 160,
        "file": "R7_FILE_SENTINEL_" + "Q" * 160,
    }
    invalid_date = tmp_path / "r7-invalid-date.csv"
    invalid_date.write_text(f"date,amount\n{sentinels['date']},100.00\n", encoding="utf-8")
    invalid_amount = tmp_path / "r7-invalid-amount.csv"
    invalid_amount.write_text(f"date,amount\n2025-09-05,{sentinels['amount']}\n", encoding="utf-8")
    missing_column = tmp_path / "r7-missing-column.csv"
    missing_column.write_text("date,amount\n2025-09-05,100.00\n", encoding="utf-8")
    malformed_xlsx = tmp_path / "r7-malformed.xlsx"
    malformed_xlsx.write_bytes(sentinels["file"].encode("utf-8"))

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                    name="R7 STDIO 导入错误企业",
                )
                session.commit()
                org_id = str(organization.id)

            async def run_stdio_import_errors() -> dict[str, tuple[dict[str, Any], str]]:
                parameters = StdioServerParameters(
                    command=getattr(sys, "_base_executable", sys.executable),
                    args=["-m", "ai_accounting.mcp_server"],
                    cwd=Path(__file__).parents[1],
                    env=_stdio_environment(database_url, tmp_path / "evidence"),
                )
                async with stdio_client(parameters) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as client:
                        await client.initialize()

                        async def import_statement(
                            path: Path, mapping: dict[str, str]
                        ) -> tuple[dict[str, Any], str]:
                            response = await client.call_tool(
                                "finance_import_bank_statement",
                                {
                                    "request": {
                                        "org_id": org_id,
                                        "file_path": str(path),
                                        "column_mapping": mapping,
                                    }
                                },
                            )
                            assert response.isError is False, response.content
                            assert len(response.content) == 1
                            text = response.content[0].text
                            return json.loads(text), text

                        return {
                            "date": await import_statement(
                                invalid_date,
                                {"booking_date": "date", "amount": "amount"},
                            ),
                            "amount": await import_statement(
                                invalid_amount,
                                {"booking_date": "date", "amount": "amount"},
                            ),
                            "column": await import_statement(
                                missing_column,
                                {
                                    "booking_date": sentinels["column"],
                                    "amount": "amount",
                                },
                            ),
                            "file": await import_statement(
                                malformed_xlsx,
                                {"booking_date": "date", "amount": "amount"},
                            ),
                        }

            results = asyncio.run(run_stdio_import_errors())
            assert results["date"][0] == {
                "status": "ok",
                "source_sha256": results["date"][0]["source_sha256"],
                "imported_count": 0,
                "duplicate_count": 0,
                "error_count": 1,
                "imported_ids": [],
                "duplicate_ids": [],
                "errors": [
                    {
                        "row": 2,
                        "field": "booking_date",
                        "code": "BANK_STATEMENT_INVALID_DATE",
                    }
                ],
            }
            assert results["amount"][0] == {
                "status": "ok",
                "source_sha256": results["amount"][0]["source_sha256"],
                "imported_count": 0,
                "duplicate_count": 0,
                "error_count": 1,
                "imported_ids": [],
                "duplicate_ids": [],
                "errors": [
                    {
                        "row": 2,
                        "field": "amount",
                        "code": "BANK_STATEMENT_INVALID_AMOUNT",
                    }
                ],
            }
            assert results["column"][0] == {
                "status": "rejected",
                "errors": [
                    {
                        "code": "BANK_STATEMENT_MISSING_COLUMN",
                        "field": "booking_date",
                    }
                ],
            }
            assert results["file"][0] == {
                "status": "rejected",
                "errors": [{"code": "BANK_STATEMENT_PARSE_FAILED"}],
            }
            for _kind, (_result, response_text) in results.items():
                assert "error" not in _result.get("errors", [{}])[0]
                for sentinel in sentinels.values():
                    assert sentinel not in response_text
                assert str(tmp_path) not in response_text
                assert "postgresql://" not in response_text
        finally:
            engine.dispose()
