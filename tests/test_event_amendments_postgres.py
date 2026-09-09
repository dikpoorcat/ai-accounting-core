from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date
from decimal import Decimal

import pytest
import test_event_amendments as cases
from _postgres_helpers import catalog_owner_authority
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_business_deletions import delete_event
from test_service import sale_request
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.event_amendments import EventAmendmentService, _graph, _json
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventAmendment,
    Evidence,
    Organization,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]
IMAGE = "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"


def annual_bonus_amendment_with_evidence(session, organization, method, evidence_id):
    """Exercise the annual-bonus amendment with a complete final evidence graph."""
    from test_payroll_service import preview_and_confirm

    from ai_accounting.models import PayrollLine, PayrollTaxStateSlot
    from ai_accounting.schemas import ConfirmPayrollRequest, PreviewPayrollRequest

    service, regular = preview_and_confirm(session, organization)
    employee_id = session.scalar(select(PayrollLine.employee_id))
    request = PreviewPayrollRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "bonus-preview",
            "batch_kind": "annual_bonus",
            "payroll_period": "2026-03",
            "posting_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "tax_method": method,
            "evidence_references": [evidence_id],
            "employee_items": [
                {
                    "employee_id": employee_id,
                    "annual_bonus_fen": 100_000,
                    **(
                        {"regular_payroll_batch_id": regular.batch_id}
                        if method == "combined"
                        else {}
                    ),
                }
            ],
        }
    )
    missing_request = request.model_copy(
        update={
            "idempotency_key": f"bonus-missing-preview-{method}",
            "evidence_references": [],
        }
    )
    missing_preview = service.preview_payroll(missing_request)
    missing = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=missing_preview.batch_id,
            calculation_hash=missing_preview.calculation_hash,
            idempotency_key=f"bonus-missing-confirm-{method}",
        )
    )
    assert missing.status == "needs_information", missing
    assert missing.missing_information == [
        {
            "field": "evidence_references",
            "reason": "正式工资或年终奖入账需要预览时登记的原始依据",
        }
    ]
    preview = service.preview_payroll(request)
    assert preview.status == "calculated", preview
    source = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="bonus-confirm",
        )
    )
    assert source.status == "posted", source
    request.employee_items[0].annual_bonus_fen = 200_000
    result = EventAmendmentService(session).amend(cases.amendment(session, source, request))
    assert result["status"] == "posted", result
    slot = session.scalar(select(PayrollTaxStateSlot))
    assert slot.final_batch_id == (source.batch_id if method == "combined" else regular.batch_id)


def fixed_asset_batch_amendment_with_two_components(session, organization, evidence_id):
    """A batch component must own depreciation projections for at least two assets."""
    from test_fixed_asset_service import _acquisition_request

    from ai_accounting.fixed_asset_service import FixedAssetService
    from ai_accounting.models import FixedAssetDepreciation
    from ai_accounting.schemas import (
        ActivateFixedAssetRequest,
        ConfirmFixedAssetDepreciationBatchRequest,
        PreviewFixedAssetDepreciationBatchRequest,
    )

    evidence = session.get(Evidence, evidence_id)
    service = FixedAssetService(session)
    for index in range(2):
        acquisition = _acquisition_request(
            organization, evidence, key=f"batch-asset-{index}"
        ).model_copy(
            update={"asset_code": f"FA-BATCH-{index}", "asset_name": f"Batch asset {index}"}
        )
        acquired = service.acquire_fixed_asset(acquisition)
        activated = service.activate_fixed_asset(
            ActivateFixedAssetRequest(
                org_id=organization.id,
                asset_id=acquired.asset_id,
                idempotency_key=f"batch-activate-{index}",
                activation_date=date(2026, 1, 10),
                posting_date=date(2026, 1, 10),
                useful_life_months=13,
                residual_value_fen=10_000,
                benefit_area="management",
                evidence_references=[evidence.id],
            )
        )
        assert acquired.status == activated.status == "posted"
    request = PreviewFixedAssetDepreciationBatchRequest(
        org_id=organization.id,
        depreciation_period="2026-02",
        posting_date=date(2026, 2, 28),
    )
    preview = service.preview_fixed_asset_depreciation_batch(request)
    source = service.confirm_fixed_asset_depreciation_batch(
        ConfirmFixedAssetDepreciationBatchRequest(
            **request.model_dump(),
            calculation_hash=preview.calculation_hash,
            idempotency_key="batch-depreciate",
        )
    )
    assert source.status == "posted", source
    result = EventAmendmentService(session).amend(cases.amendment(session, source, request))
    assert result["status"] == "posted", result
    projections = session.scalars(
        select(FixedAssetDepreciation).where(FixedAssetDepreciation.event_id == source.event_id)
    ).all()
    assert len(projections) == 2
    assert len({row.component_id for row in projections}) == 1


def banked_checks(engine, authority, evidence_id, tmp_path):
    from test_borrowing_service import _draw_request
    from test_payroll_service import payment_request, preview_and_confirm

    from ai_accounting.bank_statement_schemas import (
        ConfirmBankReconciliationScopeRequest,
        ConfirmBankStatementFileImportRequest,
        PreviewBankReconciliationScopeRequest,
        PreviewBankStatementFileImportRequest,
    )
    from ai_accounting.bank_statement_service import BankStatementService
    from ai_accounting.borrowing_service import BorrowingService
    from ai_accounting.component_schemas import RecordEventRequest
    from ai_accounting.config import Settings
    from ai_accounting.models import Account, BankTransaction, OpenItem

    context = authority.context

    def attributed(session, tool):
        return authority.attributed_call(session, tool_name=tool)

    with (
        Session(engine) as session,
        session.begin(),
        attributed(session, "finance_confirm_bank_reconciliation_scope"),
    ):
        service = BankStatementService(session)
        request = PreviewBankReconciliationScopeRequest(
            org_id=context.org_id,
            action_type="initial_confirmation",
            accounts=[
                {
                    "bank_account_code": "1002",
                    "account_name": session.scalar(
                        select(Account.name).where(
                            Account.org_id == context.org_id, Account.code == "1002"
                        )
                    ),
                    "start_date": date(2026, 1, 1),
                }
            ],
            explanation="Test scope",
            evidence_references=[evidence_id],
        )
        preview = service.preview_bank_reconciliation_scope(request)
        assert preview.status == "calculated", preview
        result = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                request.model_dump()
                | {"calculation_hash": preview.calculation_hash, "idempotency_key": "bank-scope"}
            )
        )
        assert result.status == "posted", result

    def import_bank(session, amount, booking_date, key):
        filename = key + ".csv"
        (tmp_path / filename).write_text(
            f"date,amount,reference\n{booking_date},{Decimal(amount) / Decimal(100):.2f},{key}\n",
            encoding="utf-8",
        )
        service = BankStatementService(
            session,
            settings=Settings(
                finance_bank_import_dir=tmp_path,
                finance_evidence_dir=tmp_path / "evidence",
            ),
        )
        request = PreviewBankStatementFileImportRequest(
            org_id=context.org_id,
            bank_account_code="1002",
            source_file_name=filename,
            file_format="csv",
            column_mapping={"booking_date": "date", "amount": "amount", "external_id": "reference"},
        )
        preview = service.preview_bank_statement_import(request)
        assert preview.status == "calculated", preview
        with attributed(session, "finance_confirm_bank_statement_import"):
            result = service.confirm_bank_statement_import(
                ConfirmBankStatementFileImportRequest.model_validate(
                    request.model_dump()
                    | {"calculation_hash": preview.calculation_hash, "idempotency_key": key}
                )
            )
            assert result.status == "posted", result
        return session.get(BankTransaction, uuid.UUID(result.data["imported_transaction_ids"][0]))

    for kind in ("borrowing", "salary", "payout"):
        with Session(engine) as session:
            transaction = session.begin()
            org = session.get(Organization, context.org_id)
            amount, day = {
                "borrowing": (1_000_000, "2026-01-01"),
                "salary": (-839_500, "2026-03-05"),
                "payout": (-420_000, "2026-09-05"),
            }[kind]
            bank = import_bank(session, amount, day, kind)
            with attributed(session, "finance_amend_event"):
                if kind == "borrowing":
                    request = _draw_request(org, session.get(Evidence, evidence_id), bank)
                    source = BorrowingService(session).draw_borrowing(request)
                    replacement = request.model_copy(update={"contract_name": "Corrected loan"})
                elif kind == "salary":
                    _, payroll = preview_and_confirm(session, org)
                    item = session.scalar(
                        select(OpenItem).where(
                            OpenItem.source_event_id == payroll.event_id,
                            OpenItem.payable_category == "salary",
                        )
                    )
                    request = payment_request(
                        org,
                        event_type="salary_payment",
                        amount_fen=839_500,
                        allocations=[{"open_item_id": item.id, "amount_fen": 1_000_000}],
                        salary_withholdings=[
                            {
                                "open_item_id": item.id,
                                "employee_social_insurance_items": {"pension": 80_000},
                                "employee_housing_fund_items": {"housing_fund": 70_000},
                                "individual_income_tax_fen": 10_500,
                            }
                        ],
                        bank=bank,
                        key="salary",
                    )
                    source = FinanceService(session).record_event(request)
                    replacement = request.model_copy(update={"description": "Corrected salary"})
                else:
                    cases.test_labor_batch_recalculates_tax_and_preserves_batch(session, org)
                    item = session.scalar(
                        select(OpenItem).where(OpenItem.payable_category == "labor_remuneration")
                    )
                    request = RecordEventRequest.model_validate(
                        {
                            "org_id": org.id,
                            "idempotency_key": "labor-payment",
                            "posting_date": day,
                            "evidence_references": [evidence_id],
                            "components": [
                                {
                                    "key": "labor",
                                    "kind": "labor_settlement",
                                    "business_date": day,
                                    "payment_date": day,
                                    "source_open_item_id": item.id,
                                    "amount_fen": 500000,
                                    "settlement_mode": "net_after_withholding",
                                    "metadata": {
                                        "withholding_agency_code": "tax-office",
                                        "withholding_agency_name": "Tax office",
                                    },
                                }
                            ],
                            "funds": [
                                {
                                    "key": "bank",
                                    "account_code": "1002",
                                    "direction": "payment",
                                    "payment_date": day,
                                    "amount_fen": 420000,
                                    "allocations": [
                                        {"component_key": "labor", "amount_fen": 420000}
                                    ],
                                    "bank_transaction_references": [{"id": bank.id}],
                                }
                            ],
                        }
                    )
                    source = FinanceService(session).record_event(request)
                    replacement = request.model_copy(update={"description": "Corrected payout"})
                assert source.status == "posted", source
                result = EventAmendmentService(session).amend(
                    cases.amendment(session, source, replacement, key=kind + "-amend")
                )
                assert result["status"] == "posted", result
                assert result["voucher_id"] == str(source.voucher_id)
                assert session.get(BankTransaction, bank.id).matched_event_id == source.event_id
                session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
                from ai_accounting.bank_import_withdrawals import BankImportWithdrawalService
                from ai_accounting.event_amendment_schemas import WithdrawBankImportRequest
                from ai_accounting.models import BankStatementImportAction

                action = session.get(BankStatementImportAction, bank.import_action_id)
                with attributed(session, "finance_withdraw_bank_statement_import"):
                    blocked = BankImportWithdrawalService(session).withdraw(
                        WithdrawBankImportRequest(
                            org_id=org.id,
                            action_id=action.id,
                            expected_calculation_hash=action.calculation_hash,
                            idempotency_key="blocked-withdraw",
                            reason="In use",
                        )
                    )
                    assert blocked["errors"] == ["BANK_IMPORT_TRANSACTIONS_IN_USE"]
                with attributed(session, "finance_delete_event"):
                    delete_event(session, session.get(BusinessEvent, source.event_id))
                    assert session.get(BankTransaction, bank.id).matched_event_id is None
                    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            transaction.rollback()

    from ai_accounting.bank_import_withdrawals import BankImportWithdrawalService
    from ai_accounting.event_amendment_schemas import WithdrawBankImportRequest
    from ai_accounting.models import BankStatementImportAction

    with Session(engine) as session:
        transaction = session.begin()
        bank = import_bank(session, 22704, "2026-08-26", "wrong-import")
        action = session.get(BankStatementImportAction, bank.import_action_id)
        request = WithdrawBankImportRequest(
            org_id=context.org_id,
            action_id=action.id,
            expected_calculation_hash=action.calculation_hash,
            idempotency_key="withdraw-import",
        )
        with attributed(session, "finance_withdraw_bank_statement_import"):
            result = BankImportWithdrawalService(session).withdraw(request)
            assert result["status"] == "withdrawn", result
            assert result["removed_count"] == 1
            assert session.get(BankTransaction, bank.id) is None
            assert BankImportWithdrawalService(session).withdraw(request)["idempotent_replay"]
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        replacement = import_bank(session, 22704, "2026-08-26", "correct-import")
        assert replacement.id != bank.id
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        transaction.rollback()

    def import_rows(session, key, rows):
        filename = key + ".csv"
        (tmp_path / filename).write_text(
            "date,amount,reference\n"
            + "\n".join(
                f"2026-08-26,{Decimal(amount) / Decimal(100):.2f},{external_id}"
                for external_id, amount in rows
            )
            + "\n",
            encoding="utf-8",
        )
        service = BankStatementService(
            session,
            settings=Settings(
                finance_bank_import_dir=tmp_path, finance_evidence_dir=tmp_path / "evidence"
            ),
        )
        request = PreviewBankStatementFileImportRequest(
            org_id=context.org_id,
            bank_account_code="1002",
            source_file_name=filename,
            file_format="csv",
            column_mapping={"booking_date": "date", "amount": "amount", "external_id": "reference"},
        )
        preview = service.preview_bank_statement_import(request)
        assert preview.status == "calculated", preview
        with attributed(session, "finance_confirm_bank_statement_import"):
            confirmed = service.confirm_bank_statement_import(
                ConfirmBankStatementFileImportRequest.model_validate(
                    request.model_dump()
                    | {"calculation_hash": preview.calculation_hash, "idempotency_key": key}
                )
            )
        assert confirmed.status == "posted", confirmed
        return confirmed

    with Session(engine) as session:
        transaction = session.begin()
        original_rows = [(f"bank-{i}", 10000 + i) for i in range(8)]
        original = import_rows(session, "eight-original", original_rows)
        wrong = import_rows(
            session,
            "nine-wrong",
            [(f"unique-{i}", amount) for i, (_, amount) in enumerate(original_rows)]
            + [("unique-8", 22704)],
        )
        assert wrong.data["imported_count"] == 9
        with attributed(session, "finance_withdraw_bank_statement_import"):
            result = BankImportWithdrawalService(session).withdraw(
                WithdrawBankImportRequest(
                    org_id=context.org_id,
                    action_id=wrong.action_id,
                    expected_calculation_hash=wrong.calculation_hash,
                    idempotency_key="remove-nine",
                    reason="Wrong identifier column",
                )
            )
            assert result["status"] == "withdrawn", result
            assert result["removed_count"] == 9
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        corrected = import_rows(session, "nine-correct", original_rows + [("bank-new", 22704)])
        assert corrected.data["imported_count"] == 1
        assert corrected.data["duplicate_count"] == 8
        assert set(corrected.data["duplicate_transaction_ids"]) == set(
            original.data["imported_transaction_ids"]
        )
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        transaction.rollback()


def test_v3_baseline_and_all_posting_families(tmp_path):
    with PostgresContainer(IMAGE, driver="psycopg") as postgres, ExitStack() as authority_stack:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = url
        command.upgrade(config, "head")
        command.check(config)
        engine = create_engine(url)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0005_payroll_provenance"
            )
        with Session(engine) as session:
            org = seed_organization(
                session,
                name="Amendments",
                taxpayer_identification_number="91330106MA1234567T",
                accounting_period_control_enabled=False,
            )
            org_id = org.id
            session.commit()
            authority = authority_stack.enter_context(catalog_owner_authority(session, org))
        with (
            Session(engine) as session,
            session.begin(),
            authority.attributed_call(
                session,
                tool_name="finance_generate_accounting_period",
            ),
        ):
            path = tmp_path / "period.txt"
            path.write_bytes(b"period")
            evidence = Evidence(
                org_id=org_id,
                sha256=hashlib.sha256(b"period").hexdigest(),
                original_name="period.txt",
                source="test",
                size_bytes=6,
                storage_path=str(path),
            )
            session.add(evidence)
            session.flush()
            evidence_id = evidence.id
            for month in range(1, 10):
                result = AccountingPeriodService(session).generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month=f"2026-{month:02d}",
                        idempotency_key=f"month-{month}",
                        confirmation_note="Test period",
                        evidence_references=[evidence.id],
                    )
                )
                assert result.status == "posted", result
        checks = [
            (cases.test_sale_replaces_voucher_and_open_item_with_audit_and_replay, ()),
            (
                cases.test_asset_acquisition_amendment_replaces_projection_and_recalculates_cost,
                ("fixed",),
            ),
            (
                cases.test_asset_acquisition_amendment_replaces_projection_and_recalculates_cost,
                ("intangible",),
            ),
            (cases.test_payroll_amendment_recalculates_same_batch_and_liabilities, ()),
        ]
        checks += [
            (cases.test_enterprise_income_tax_confirmation_amendment, ()),
        ]
        checks += [(cases.test_income_tax_result_amendment_recalculates_delta_in_same_voucher, ())]
        checks += [
            (annual_bonus_amendment_with_evidence, (method, evidence_id))
            for method in ("combined", "separate")
        ]
        checks += [
            (cases.test_asset_lifecycle_amendment, (kind,))
            for kind in (
                "activation",
                "depreciation",
                "disposal",
                "amortization",
            )
        ]
        checks += [(fixed_asset_batch_amendment_with_two_components, (evidence_id,))]
        for check, args in checks:
            with Session(engine) as session:
                transaction = session.begin()
                with authority.attributed_call(session, tool_name="finance_amend_event"):
                    check(session, session.get(Organization, org_id), *args)
                    try:
                        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                    except DBAPIError as exc:
                        exc.add_note(f"amendment case: {check.__name__}{args!r}")
                        raise
                session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
                latest = session.scalar(
                    select(BusinessEventAmendment).order_by(
                        BusinessEventAmendment.created_at.desc()
                    )
                )
                with authority.attributed_call(session, tool_name="finance_delete_event"):
                    delete_event(session, session.get(BusinessEvent, latest.event_id))
                    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                    with pytest.raises(DBAPIError, match="DELETED_EVENT_IMMUTABLE"):
                        with session.begin_nested():
                            session.execute(
                                text("UPDATE business_events SET status='posted' WHERE id=:id"),
                                {"id": latest.event_id},
                            )
                transaction.rollback()
        with (
            Session(engine) as session,
            session.begin(),
            authority.attributed_call(
                session,
                tool_name="finance_record_event",
            ),
        ):
            source_request = sale_request(
                session.get(Organization, org_id), event_type="service_credit_sale"
            )
            source = FinanceService(session).record_event(source_request)
            request = cases.amendment(
                session,
                source,
                source_request.model_copy(update={"description": "Edited"}),
                key="concurrent-edit",
            )

        def writer(key):
            with (
                Session(engine) as session,
                session.begin(),
                authority.attributed_call(
                    session,
                    tool_name="finance_amend_event",
                ),
            ):
                return EventAmendmentService(session).amend(
                    request.model_copy(update={"idempotency_key": key})
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, ["same-edit", "same-edit"]))
        assert all(result["status"] == "posted" for result in results), results
        assert sum(bool(result.get("idempotent_replay")) for result in results) == 1
        with Session(engine) as session:
            source_event = session.get(BusinessEvent, source.event_id)
            assert source_event.description == "Edited"
            request = cases.amendment(
                session,
                source,
                source_request.model_copy(
                    update={
                        "description": "Second edit",
                        "components": [
                            source_request.components[0].model_copy(
                                update={"amount_fen": source_request.components[0].amount_fen + 100}
                            )
                        ],
                    }
                ),
                key="conflict",
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, ["edit-a", "edit-b"]))
        assert sorted(result["status"] for result in results) == ["posted", "rejected"]
        assert next(result for result in results if result["status"] == "rejected")["errors"] == [
            "AMENDMENT_FACTS_STALE"
        ]

        with Session(engine) as session:
            with pytest.raises(DBAPIError, match="immutable"):
                session.execute(
                    text("UPDATE business_events SET status='draft' WHERE id=:id"),
                    {"id": source.event_id},
                )
            session.rollback()
            with pytest.raises(DBAPIError, match="AMENDMENT_AUDIT_IMMUTABLE"):
                session.execute(
                    text(
                        "UPDATE business_event_amendments SET reason='tampered' WHERE event_id=:id"
                    ),
                    {"id": source.event_id},
                )
            session.rollback()

        with Session(engine) as session:
            with pytest.raises(DBAPIError, match="AMENDMENT_INCOMPLETE"):
                with (
                    session.begin(),
                    authority.attributed_call(
                        session,
                        tool_name="finance_amend_event",
                    ),
                ):
                    source_event = session.get(BusinessEvent, source.event_id)
                    session.add(
                        BusinessEventAmendment(
                            org_id=org_id,
                            event_id=source.event_id,
                            revision=3,
                            idempotency_key="incomplete",
                            request_hash="a" * 64,
                            reason="Incomplete",
                            before_state={"tables": _json(_graph(session, source_event))},
                        )
                    )
                    session.flush()
                    source_event.status = "draft"
                    session.flush()
                    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        for additional_check, additional_args in (
            (cases.test_asset_lifecycle_amendment, ("retirement",)),
            (cases.test_tax_snapshot_amendment_and_locked_source, ()),
            (cases.test_labor_batch_recalculates_tax_and_preserves_batch, ()),
        ):
            with Session(engine) as session:
                transaction = session.begin()
                with authority.attributed_call(session, tool_name="finance_amend_event"):
                    additional_check(session, session.get(Organization, org_id), *additional_args)
                    labor_diagnostics = None
                    if (
                        additional_check
                        == cases.test_labor_batch_recalculates_tax_and_preserves_batch
                    ):
                        labor_diagnostics = session.execute(
                            text(
                                "SELECT b.id, b.status, b.business_event_id, "
                                "(SELECT count(*) FROM event_evidence ee "
                                " WHERE ee.event_id=b.business_event_id) AS event_evidence_count, "
                                "(SELECT count(*) FROM labor_remuneration_batch_evidence be "
                                " WHERE be.batch_id=b.id) AS batch_evidence_count, "
                                "c.kind, a.business_class, a.system_role "
                                "FROM labor_remuneration_batches b "
                                "JOIN business_event_components c "
                                "ON c.event_id=b.business_event_id "
                                "JOIN voucher_lines l ON l.component_id=c.id "
                                "JOIN accounts a ON a.id=l.account_id "
                                "ORDER BY b.id, c.key, a.code"
                            )
                        ).all()
                        assert all(row.event_evidence_count > 0 for row in labor_diagnostics)
                        assert all(row.batch_evidence_count > 0 for row in labor_diagnostics)
                    try:
                        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                    except DBAPIError as exc:
                        exc.add_note(
                            f"amendment case: {additional_check.__name__}{additional_args!r}"
                        )
                        if labor_diagnostics is not None:
                            exc.add_note(f"labor graph before constraints: {labor_diagnostics!r}")
                        raise
                transaction.rollback()
        banked_checks(engine, authority, evidence_id, tmp_path)
        engine.dispose()
