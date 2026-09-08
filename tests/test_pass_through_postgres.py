from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_business_event_amount_postgres import postgres_engine as postgres_engine

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationScopeRequest,
    ConfirmBankStatementFileImportRequest,
    PreviewBankReconciliationScopeRequest,
    PreviewBankStatementFileImportRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.coa import seed_organization
from ai_accounting.config import Settings
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutionContext, ExecutorKind
from ai_accounting.models import (
    BusinessEvent,
    Evidence,
    OrganizationDatabaseMetadata,
    Voucher,
)
from ai_accounting.schemas import BankTransactionReference
from ai_accounting.service import FinanceService

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


@pytest.fixture
def accounting(postgres_engine, tmp_path):
    with Session(postgres_engine) as session:
        org = seed_organization(
            session,
            name="Pass-through regression",
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
        )
        catalog_id = uuid.uuid4()
        session.add(
            OrganizationDatabaseMetadata(
                singleton_key=1,
                org_id=org.id,
                database_identity=uuid.uuid4(),
                current_catalog_instance_id=catalog_id,
                owner_approval_required=True,
            )
        )
        session.flush()
        context = ExecutionContext(
            org_id=org.id,
            owner_account_id=uuid.uuid4(),
            owner_session_id=uuid.uuid4(),
            owner_credential_version=1,
            executor_kind=ExecutorKind.AI_AGENT,
            executor_name="refund-regression",
            executor_version="1",
            request_correlation_id=uuid.uuid4(),
            catalog_instance_id=catalog_id,
        )

        def attributed(tool_name):
            return persist_execution_attribution(
                session,
                context=replace(context, request_correlation_id=uuid.uuid4()),
                tool_name=tool_name,
            )

        with attributed("finance_generate_accounting_period"):
            path = tmp_path / "scope.txt"
            path.write_bytes(b"scope")
            evidence = Evidence(
                org_id=org.id,
                sha256=hashlib.sha256(b"scope").hexdigest(),
                original_name="scope.txt",
                source="test",
                size_bytes=5,
                storage_path=str(path),
            )
            session.add(evidence)
            session.flush()
            generated = AccountingPeriodService(session).generate_accounting_period(
                GenerateAccountingPeriodRequest(
                    org_id=org.id,
                    period_month="2026-08",
                    idempotency_key="august",
                    confirmation_note="Test month",
                    evidence_references=[evidence.id],
                )
            )
            assert generated.status == "posted", generated
        with attributed("finance_confirm_bank_reconciliation_scope"):
            service = BankStatementService(session)
            scope = PreviewBankReconciliationScopeRequest(
                org_id=org.id,
                action_type="initial_confirmation",
                accounts=[
                    {
                        "bank_account_code": "1002",
                        "account_name": "银行存款",
                        "start_date": date(2026, 8, 1),
                    }
                ],
                explanation="Test scope",
                evidence_references=[evidence.id],
            )
            preview = service.preview_bank_reconciliation_scope(scope)
            assert preview.status == "calculated", preview
            confirmed = service.confirm_bank_reconciliation_scope(
                ConfirmBankReconciliationScopeRequest.model_validate(
                    scope.model_dump()
                    | {"calculation_hash": preview.calculation_hash, "idempotency_key": "scope"}
                )
            )
            assert confirmed.status == "posted", confirmed
        bank_service = BankStatementService(
            session,
            settings=Settings(
                finance_bank_import_dir=tmp_path, finance_evidence_dir=tmp_path / "evidence"
            ),
        )

        def bank(amount, day, key):
            filename = key + ".csv"
            (tmp_path / filename).write_text(
                "date,amount,reference\n" + f"{day},{Decimal(amount) / Decimal(100):.2f},{key}\n",
                encoding="utf-8",
            )
            request = PreviewBankStatementFileImportRequest(
                org_id=org.id,
                bank_account_code="1002",
                source_file_name=filename,
                file_format="csv",
                column_mapping={
                    "booking_date": "date",
                    "amount": "amount",
                    "external_id": "reference",
                },
            )
            preview = bank_service.preview_bank_statement_import(request)
            assert preview.status == "calculated", preview
            with attributed("finance_confirm_bank_statement_import"):
                result = bank_service.confirm_bank_statement_import(
                    ConfirmBankStatementFileImportRequest.model_validate(
                        request.model_dump()
                        | {"calculation_hash": preview.calculation_hash, "idempotency_key": key}
                    )
                )
                assert result.status == "posted", result
                return BankTransactionReference(id=result.data["imported_transaction_ids"][0])

        session.info["test_context"] = context
        yield session, org, evidence, attributed, bank


def receipt(org, evidence, *, amount=12_000_000, **changes):
    from ai_accounting.schemas import RecordEventRequest

    return RecordEventRequest.model_validate(
        {
            "org_id": org.id,
            "idempotency_key": "receipt",
            "event_type": "customer_receipt",
            "business_dates": {
                "business_date": "2026-08-09",
                "payment_date": "2026-08-09",
                "posting_date": "2026-08-09",
            },
            "counterparty": {"kind": "customer", "name": "Commission customer"},
            "amounts": {"amount_fen": amount},
            "bank_account_code": "1002",
            "evidence_references": [evidence.id],
            "description": "Commission and entrusted funds",
            **changes,
        }
    )


def split(key, amount, *, advance=False, evidence=None):
    party = {"kind": "other", "name": key}
    return {
        "key": key,
        "amount_fen": amount,
        "beneficiary": party,
        "creditor": {"kind": "employee", "name": "Advancing person"} if advance else party,
        "creditor_basis": "advance_reimbursement" if advance else "beneficiary",
        "purpose": "Entrusted beneficiary payment",
        **(
            {"advance_payment_date": "2026-08-08", "advance_evidence_ids": [evidence.id]}
            if advance
            else {}
        ),
    }


def payment(org, evidence, item, amount, **changes):
    from ai_accounting.schemas import RecordEventRequest

    return RecordEventRequest.model_validate(
        {
            "org_id": org.id,
            "idempotency_key": str(uuid.uuid4()),
            "event_type": "pass_through_payment",
            "business_dates": {
                "business_date": "2026-08-10",
                "payment_date": "2026-08-10",
                "posting_date": "2026-08-10",
            },
            "counterparty": {"id": item.counterparty_id},
            "amounts": {"amount_fen": amount},
            "bank_account_code": "1002",
            "evidence_references": [evidence.id],
            "description": "Settle entrusted funds",
            "allocations": [{"open_item_id": item.id, "amount_fen": amount}],
            **changes,
        }
    )


def record(session, attributed, request, status="posted"):
    with attributed("finance_record_event"):
        result = FinanceService(session).record_event(request)
        assert result.status == status, result
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    return result


def edit(session, attributed, event_id, replacement=None):
    from ai_accounting.accounting_periods import canonical_sha256
    from ai_accounting.event_amendment_schemas import AmendEventRequest, DeleteEventRequest
    from ai_accounting.event_amendments import EventAmendmentService

    envelope = dict(
        org_id=session.get(BusinessEvent, event_id).org_id,
        event_id=event_id,
        idempotency_key=str(uuid.uuid4()),
        reason="Correct entrusted funds",
        expected_facts_hash=canonical_sha256(session.get(BusinessEvent, event_id).facts),
    )
    request = (
        AmendEventRequest(**envelope, replacement=replacement)
        if replacement
        else DeleteEventRequest(**envelope)
    )
    with attributed("finance_amend_event" if replacement else "finance_delete_event"):
        result = EventAmendmentService(session).amend(request)
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    return result


def test_mixed_receipt_amend_pay_delete_reverse_and_bank_conservation(accounting):
    from ai_accounting.models import (
        Account,
        BankTransaction,
        BankTransactionMatch,
        OpenItem,
        VoucherLine,
    )
    from ai_accounting.schemas import RecordEventRequest, ReverseEventRequest

    session, org, evidence, attributed, bank = accounting
    credit = RecordEventRequest.model_validate(
        {
            "org_id": org.id,
            "idempotency_key": "commission",
            "event_type": "service_credit_sale",
            "business_dates": {
                "business_date": "2026-08-01",
                "fulfillment_date": "2026-08-01",
                "tax_obligation_date": "2026-08-01",
                "posting_date": "2026-08-01",
            },
            "counterparty": {"kind": "customer", "name": "Commission customer"},
            "amounts": {"gross_amount_fen": 9_657_350},
            "tax_facts": {
                "taxable": False,
                "rate_percent": "0",
                "invoice_type": "none",
                "waive_exemption": False,
                "tax_due_on_event": False,
            },
            "evidence_references": [evidence.id],
        }
    )
    commission = record(session, attributed, credit)
    receivable = session.scalar(
        select(OpenItem).where(OpenItem.source_event_id == commission.event_id)
    )
    incoming = bank(12_000_000, "2026-08-09", "mixed-receipt")
    req = receipt(
        org,
        evidence,
        details={"unallocated_treatment": "advance"},
        allocations=[{"open_item_id": receivable.id, "amount_fen": 9_657_350}],
        bank_transaction_references=[incoming],
    )
    old = record(session, attributed, req)
    replacement = req.model_dump() | {
        "details": {},
        "pass_through_items": [
            split("Beneficiary A", 1_789_965),
            split("Beneficiary B", 552_685, advance=True, evidence=evidence),
        ],
    }
    amended = edit(
        session, attributed, old.event_id, RecordEventRequest.model_validate(replacement)
    )
    assert amended["status"] == "posted", amended
    assert str(old.voucher_id) == amended["voucher_id"]
    assert old.voucher_number == amended["voucher_number"]
    assert receivable.settled_amount_fen == 9_657_350
    assert (
        session.scalar(
            select(func.count())
            .select_from(BankTransactionMatch)
            .where(
                BankTransactionMatch.bank_transaction_id == incoming.id,
                BankTransactionMatch.invalidated_at.is_(None),
            )
        )
        == 1
    )
    assert session.get(BankTransaction, incoming.id).matched_event_id == old.event_id
    items = session.scalars(
        select(OpenItem)
        .where(OpenItem.source_event_id == old.event_id)
        .order_by(OpenItem.pass_through_key)
    ).all()
    assert [i.original_amount_fen for i in items] == [1_789_965, 552_685]
    assert items[1].counterparty_id != items[1].pass_through_beneficiary_id
    roles = dict(
        session.execute(
            select(Account.system_role, func.sum(VoucherLine.credit_fen))
            .join(VoucherLine, VoucherLine.account_id == Account.id)
            .where(VoucherLine.voucher_id == old.voucher_id)
            .group_by(Account.system_role)
        ).all()
    )
    assert roles["pass_through_payable"] == 2_342_650
    assert roles.get("contract_liability", 0) == 0
    assert roles.get("service_revenue", 0) == 0
    p1 = payment(
        org,
        evidence,
        items[0],
        1_789_965,
        bank_transaction_references=[bank(-1_789_965, "2026-08-10", "beneficiary-payment")],
    )
    first = record(session, attributed, p1)
    assert record(session, attributed, p1).event_id == first.event_id
    blocked = edit(session, attributed, old.event_id)
    assert blocked["status"] == "rejected", blocked
    p2 = payment(
        org,
        evidence,
        items[1],
        552_685,
        bank_transaction_references=[bank(-552_685, "2026-08-10", "advance-repayment")],
    )
    second = record(session, attributed, p2)
    assert all(i.status == "settled" for i in items)
    assert edit(session, attributed, first.event_id)["status"] == "deleted"
    assert items[0].settled_amount_fen == 0
    again = record(session, attributed, p1.model_copy(update={"idempotency_key": "pay-again"}))
    with attributed("finance_reverse_event"):
        reversed_result = FinanceService(session).reverse_event(
            ReverseEventRequest(
                org_id=org.id,
                event_id=second.event_id,
                idempotency_key="reverse-second",
                posting_date=date(2026, 8, 11),
                reason="Payment returned",
            )
        )
        assert reversed_result.status == "posted", reversed_result
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert items[1].settled_amount_fen == 0
    assert again.event_id != first.event_id


@pytest.mark.parametrize(
    "case",
    ["overpayment", "wrong_creditor", "wrong_category", "foreign_company", "wrong_bank_total"],
)
def test_invalid_payment_rolls_back_settlements_and_bank_match(accounting, case):
    from ai_accounting.models import BankTransaction, OpenItem, Settlement

    session, org, evidence, attributed, bank = accounting
    source = record(
        session,
        attributed,
        receipt(org, evidence, amount=1000, pass_through_items=[split("Beneficiary", 1000)]),
    )
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == source.event_id))
    outflow = bank(-500, "2026-08-10", "payment")
    changes = {"bank_transaction_references": [outflow]}
    amount = 500
    if case == "overpayment":
        amount = 1001
        changes = {}
    elif case == "wrong_creditor":
        changes["counterparty"] = {"kind": "other", "name": "Wrong person"}
    elif case == "wrong_category":
        changes["event_type"] = "supplier_payment"
    elif case == "foreign_company":
        changes["org_id"] = uuid.uuid4()
    elif case == "wrong_bank_total":
        amount = 499
    record(session, attributed, payment(org, evidence, item, amount, **changes), "rejected")
    assert item.settled_amount_fen == 0
    assert (
        session.scalar(
            select(func.count()).select_from(Settlement).where(Settlement.open_item_id == item.id)
        )
        == 0
    )
    assert session.get(BankTransaction, outflow.id).matched_event_id is None


def test_missing_advance_relationship_needs_information_and_no_posting(accounting):
    session, org, evidence, attributed, bank = accounting
    data = split("Beneficiary", 1000)
    data.pop("creditor_basis")
    result = record(
        session,
        attributed,
        receipt(org, evidence, amount=1000, pass_through_items=[data]),
        "needs_information",
    )
    assert "pass_through_items.0.creditor_basis" in result.missing_information
    assert (
        session.scalar(
            select(func.count()).select_from(Voucher).where(Voucher.event_id == result.event_id)
        )
        == 0
    )


def test_partial_payment_and_later_employee_advance(accounting):
    from ai_accounting.models import OpenItem
    from ai_accounting.schemas import RecordEventRequest

    session, org, evidence, attributed, bank = accounting
    source = record(
        session,
        attributed,
        receipt(org, evidence, amount=1000, pass_through_items=[split("Beneficiary", 1000)]),
    )
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == source.event_id))
    record(session, attributed, payment(org, evidence, item, 400))
    assert item.status == "partial" and item.settled_amount_fen == 400
    data = payment(org, evidence, item, 600).model_dump() | {
        "event_type": "employee_reimbursement",
        "bank_account_code": None,
        "amounts": {"gross_amount_fen": 600},
        "counterparty": {"kind": "employee", "name": "Later advancing person"},
        "details": {"reimbursement_kind": "existing_payable", "paid_now": False},
    }
    transfer = record(session, attributed, RecordEventRequest.model_validate(data))
    employee_item = session.scalar(
        select(OpenItem).where(OpenItem.source_event_id == transfer.event_id)
    )
    assert employee_item.original_amount_fen == 600
    assert employee_item.counterparty_id != item.counterparty_id
    assert item.status == "settled"
    assert edit(session, attributed, transfer.event_id)["status"] == "deleted"
    assert item.status == "partial" and item.settled_amount_fen == 400


def test_sql_tampering_pass_through_amount_or_beneficiary_is_rejected(accounting):
    from ai_accounting.models import OpenItem

    session, org, evidence, attributed, bank = accounting
    source = record(
        session,
        attributed,
        receipt(org, evidence, amount=1000, pass_through_items=[split("Beneficiary", 1000)]),
    )
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == source.event_id))
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.execute(
                text("UPDATE open_items SET original_amount_fen=1001 WHERE id=:id"), {"id": item.id}
            )
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert session.get(OpenItem, item.id).original_amount_fen == 1000


def test_concurrent_payments_cannot_exceed_creditor_balance(accounting):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from ai_accounting.models import OpenItem, Settlement

    session, org, evidence, attributed, bank = accounting
    source = record(
        session,
        attributed,
        receipt(
            org, evidence, amount=1000, pass_through_items=[split("Concurrent beneficiary", 1000)]
        ),
    )
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == source.event_id))
    requests = [payment(org, evidence, item, 700) for _ in range(2)]
    item_id = item.id
    context = session.info["test_context"]
    engine = session.get_bind()
    session.commit()
    barrier = Barrier(2)

    def worker(request):
        with Session(engine) as worker_session:
            # Preload before the concurrent transaction to exercise stale ORM state too.
            cached = worker_session.get(OpenItem, item_id)
            assert cached.settled_amount_fen == 0
            barrier.wait(timeout=10)
            with persist_execution_attribution(
                worker_session,
                context=replace(context, request_correlation_id=uuid.uuid4()),
                tool_name="finance_record_event",
            ):
                result = FinanceService(worker_session).record_event(request)
                worker_session.commit()
                return result.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(worker, requests))
    assert sorted(statuses) == ["posted", "rejected"]
    with Session(engine) as verify:
        assert verify.get(OpenItem, item_id).settled_amount_fen == 700
        assert (
            verify.scalar(
                select(func.sum(Settlement.amount_fen)).where(
                    Settlement.open_item_id == item_id, Settlement.reversed.is_(False)
                )
            )
            == 700
        )
