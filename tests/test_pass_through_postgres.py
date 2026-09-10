from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal

import pytest
from _postgres_helpers import catalog_owner_authority
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
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.config import Settings
from ai_accounting.models import (
    BusinessEvent,
    Evidence,
    Organization,
    Voucher,
)
from ai_accounting.schemas import BankTransactionReference
from ai_accounting.service import FinanceService

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


@pytest.fixture(scope="module")
def accounting_base(postgres_engine, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("pass-through-base")
    with Session(postgres_engine) as session:
        org = seed_organization(
            session,
            name="Pass-through regression",
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
        )
        session.commit()
        with catalog_owner_authority(session, org) as authority:
            with authority.attributed_call(session, tool_name="finance_generate_accounting_period"):
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
                for month in ("2026-07", "2026-08"):
                    generated = AccountingPeriodService(session).generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=org.id,
                            period_month=month,
                            idempotency_key=month,
                            confirmation_note="Test month",
                            evidence_references=[evidence.id],
                        )
                    )
                    assert generated.status == "posted", generated
            with authority.attributed_call(
                session, tool_name="finance_confirm_bank_reconciliation_scope"
            ):
                service = BankStatementService(session)
                scope = PreviewBankReconciliationScopeRequest(
                    org_id=org.id,
                    action_type="initial_confirmation",
                    accounts=[
                        {
                            "bank_account_code": "1002",
                            "account_name": "银行存款",
                            "start_date": date(2026, 7, 1),
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
            session.commit()
            yield postgres_engine, org.id, evidence.id, authority


@pytest.fixture
def accounting(accounting_base, tmp_path):
    engine, org_id, evidence_id, authority = accounting_base
    with Session(engine) as session:
        org = session.get(Organization, org_id)
        evidence = session.get(Evidence, evidence_id)

        def attributed(tool_name):
            return authority.attributed_call(session, tool_name=tool_name)

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

        session.info["test_authority"] = authority
        yield session, org, evidence, attributed, bank


def receipt(org, evidence, *, amount=12_000_000, **changes):
    allocations = changes.pop("allocations", [])
    pass_through_items = changes.pop("pass_through_items", [])
    bank_references = changes.pop("bank_transaction_references", [])
    details = changes.pop("details", {})
    org_id = changes.pop("org_id", org.id)
    assert not changes, changes

    components = []
    funds_allocations = []
    allocated_fen = 0
    if allocations:
        settled_fen = sum(row["amount_fen"] for row in allocations)
        components.append(
            {
                "key": "receivable",
                "kind": "receivable_settlement",
                "business_date": "2026-08-09",
                "payment_date": "2026-08-09",
                "allocations": allocations,
                "metadata": {"counterparty": {"kind": "customer", "name": "Commission customer"}},
            }
        )
        funds_allocations.append({"component_key": "receivable", "amount_fen": settled_fen})
        allocated_fen += settled_fen
    for item in pass_through_items:
        component = {
            "kind": "pass_through",
            "business_date": "2026-08-09",
            "payment_date": "2026-08-09",
            **item,
        }
        components.append(component)
        funds_allocations.append(
            {"component_key": component["key"], "amount_fen": component["amount_fen"]}
        )
        allocated_fen += component["amount_fen"]
    if details.get("unallocated_treatment") == "advance":
        advance_fen = amount - allocated_fen
        components.append(
            {
                "key": "advance",
                "kind": "customer_advance",
                "business_date": "2026-08-09",
                "payment_date": "2026-08-09",
                "amount_fen": advance_fen,
                "tax_facts": {"tax_due_on_event": False},
                "metadata": {"counterparty": {"kind": "customer", "name": "Commission customer"}},
            }
        )
        funds_allocations.append({"component_key": "advance", "amount_fen": advance_fen})
        allocated_fen += advance_fen
    assert allocated_fen == amount
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": "receipt",
            "posting_date": "2026-08-09",
            "evidence_references": [evidence.id],
            "description": "Commission and entrusted funds",
            "components": components,
            "funds": [
                {
                    "key": "receipt",
                    "account_code": "1002",
                    "direction": "receipt",
                    "payment_date": "2026-08-09",
                    "amount_fen": amount,
                    "allocations": funds_allocations,
                    "bank_transaction_references": bank_references,
                }
            ],
        }
    )


def split(key, amount, *, advance=False, evidence=None):
    party = {"kind": "other", "name": key}
    return {
        "key": "pass-" + key.lower().replace(" ", "-"),
        "amount_fen": amount,
        "metadata": {"beneficiary": party, "purpose": "Entrusted beneficiary payment"},
    }


def payment(org, evidence, item, amount, **changes):
    bank_references = changes.pop("bank_transaction_references", [])
    counterparty = changes.pop("counterparty", None)
    component_kind = changes.pop("component_kind", "payable_settlement")
    org_id = changes.pop("org_id", org.id)
    funds_amount_fen = changes.pop("funds_amount_fen", amount)
    assert not changes, changes
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": str(uuid.uuid4()),
            "posting_date": "2026-08-10",
            "evidence_references": [evidence.id],
            "description": "Settle entrusted funds",
            "components": [
                {
                    "key": "settlement",
                    "kind": component_kind,
                    "business_date": "2026-08-10",
                    "payment_date": "2026-08-10",
                    "allocations": [{"open_item_id": item.id, "amount_fen": amount}],
                    "metadata": {"counterparty": counterparty},
                }
            ],
            "funds": [
                {
                    "key": "payment",
                    "account_code": "1002",
                    "direction": "payment",
                    "payment_date": "2026-08-10",
                    "amount_fen": funds_amount_fen,
                    "allocations": [
                        {"component_key": "settlement", "amount_fen": funds_amount_fen}
                    ],
                    "bank_transaction_references": bank_references,
                }
            ],
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
    from ai_accounting.schemas import ReverseEventRequest

    session, org, evidence, attributed, bank = accounting
    credit = RecordEventRequest.model_validate(
        {
            "org_id": org.id,
            "idempotency_key": "commission",
            "posting_date": "2026-08-01",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "commission",
                    "kind": "service_sale",
                    "business_date": "2026-08-01",
                    "fulfillment_date": "2026-08-01",
                    "tax_obligation_date": "2026-08-01",
                    "recognition_basis": "credit",
                    "amount_fen": 9657350,
                    "tax_facts": {
                        "taxable": False,
                        "rate_percent": "0",
                        "invoice_type": "none",
                        "waive_exemption": False,
                        "tax_due_on_event": False,
                    },
                    "metadata": {
                        "counterparty": {"kind": "customer", "name": "Commission customer"}
                    },
                }
            ],
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
    replacement = receipt(
        org,
        evidence,
        amount=12_000_000,
        allocations=[{"open_item_id": receivable.id, "amount_fen": 9_657_350}],
        pass_through_items=[
            split("Beneficiary A", 1_789_965),
            split("Beneficiary B", 552_685, advance=True, evidence=evidence),
        ],
        bank_transaction_references=[incoming],
    )
    amended = edit(session, attributed, old.event_id, replacement)
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
    assert all(i.counterparty_id is None and i.pass_through_beneficiary_id is None for i in items)
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
                reason="测试未关账误记付款的更正路由",
            )
        )
        assert reversed_result.status == "rejected", reversed_result
        assert reversed_result.data["route"] == "amend"
    assert edit(session, attributed, second.event_id)["status"] == "deleted"
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert items[1].settled_amount_fen == 0
    assert again.event_id != first.event_id


def test_credit_pass_through_has_two_obligations_without_income_or_cash(accounting):
    from ai_accounting.component_service import ComponentService
    from ai_accounting.material_service import MaterialService
    from ai_accounting.models import Account, AccountingPeriod, OpenItem, VoucherLine

    session, org, evidence, attributed, bank = accounting
    period = session.scalar(
        select(AccountingPeriod).where(
            AccountingPeriod.org_id == org.id, AccountingPeriod.calendar_month == 7
        )
    )
    with attributed("finance_record_event"):
        result = ComponentService(session).record(
            RecordEventRequest(
                org_id=org.id,
                idempotency_key="credit-before-receipt",
                posting_date="2026-07-31",
                evidence_references=[evidence.id],
                components=[
                    {
                        "key": "pass",
                        "kind": "pass_through",
                        "recognition_basis": "credit",
                        "recognition_period": "2026-07",
                        "amount_fen": 307687,
                    }
                ],
            )
        )
        assert result.status == "posted", result
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    items = list(
        session.scalars(select(OpenItem).where(OpenItem.source_event_id == result.event_id))
    )
    assert {(i.item_type, i.component_key, i.original_amount_fen) for i in items} == {
        ("receivable", "receivable", 307687),
        ("payable", "primary", 307687),
    }
    lines = session.execute(
        select(Account.system_role, VoucherLine.debit_fen, VoucherLine.credit_fen)
        .join(VoucherLine, VoucherLine.account_id == Account.id)
        .where(VoucherLine.voucher_id == result.voucher_id)
    ).all()
    assert set(lines) == {
        ("pass_through_receivable", 307687, 0),
        ("pass_through_payable", 0, 307687),
    }
    session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    for month in ("2026-09",):
        with attributed("finance_generate_accounting_period"):
            generated = AccountingPeriodService(session).generate_accounting_period(
                GenerateAccountingPeriodRequest(
                    org_id=org.id,
                    period_month=month,
                    idempotency_key=month,
                    evidence_references=[evidence.id],
                )
            )
        assert generated.status == "posted", generated
    for index, (direction, day, open_key) in enumerate(
        (("receipt", "2026-08-02", "receivable"), ("payment", "2026-09-02", "primary"))
    ):
        reference = bank(307687 if direction == "receipt" else -307687, day, f"future-{index}")
        pending_issues = []
        MaterialService(session)._subsequent_bank(org.id, period, {}, pending_issues)
        assert any(
            issue.get("bank_transaction_id") == str(reference.id)
            and issue["code"] == "MATERIAL_SUBSEQUENT_BANK_UNREVIEWED"
            for issue in pending_issues
        )
        with attributed("finance_record_event"):
            settled = ComponentService(session).record(
                RecordEventRequest(
                    org_id=org.id,
                    idempotency_key=f"future-settle-{index}",
                    posting_date=day,
                    evidence_references=[evidence.id],
                    components=[
                        {
                            "key": "settle",
                            "kind": "receivable_settlement"
                            if direction == "receipt"
                            else "payable_settlement",
                            "allocations": [
                                {
                                    "source_event_key": "credit-before-receipt",
                                    "source_component_key": "pass",
                                    "source_open_item_key": open_key,
                                    "amount_fen": 307687,
                                }
                            ],
                        }
                    ],
                    funds=[
                        {
                            "key": "bank",
                            "account_code": "1002",
                            "direction": direction,
                            "payment_date": day,
                            "amount_fen": 307687,
                            "bank_transaction_references": [reference],
                            "allocations": [{"component_key": "settle", "amount_fen": 307687}],
                        }
                    ],
                )
            )
        assert settled.status == "posted", settled
    issues = []
    reviewed = MaterialService(session)._subsequent_bank(org.id, period, {}, issues)
    assert len(reviewed) == 2 and all(item["reviewed"] for item in reviewed)
    assert not issues, issues
    assert all(item.original_amount_fen == item.settled_amount_fen == 307687 for item in items)
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


@pytest.mark.parametrize(
    "case",
    [
        "overpayment",
        "wrong_creditor",
        "wrong_obligation_kind",
        "foreign_company",
        "wrong_bank_total",
    ],
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
    elif case == "wrong_obligation_kind":
        changes["component_kind"] = "receivable_settlement"
    elif case == "foreign_company":
        changes["org_id"] = uuid.uuid4()
    elif case == "wrong_bank_total":
        changes["funds_amount_fen"] = 499
    if case == "wrong_creditor":
        record(session, attributed, payment(org, evidence, item, amount, **changes))
        assert item.settled_amount_fen == 500
        assert session.get(BankTransaction, outflow.id).matched_event_id is not None
        return
    record(session, attributed, payment(org, evidence, item, amount, **changes), "rejected")
    assert item.settled_amount_fen == 0
    assert (
        session.scalar(
            select(func.count()).select_from(Settlement).where(Settlement.open_item_id == item.id)
        )
        == 0
    )
    assert session.get(BankTransaction, outflow.id).matched_event_id is None


def test_no_beneficiary_or_advance_relationship_is_required(accounting):
    session, org, evidence, attributed, bank = accounting
    data = split("Beneficiary", 1000)
    data.pop("metadata")
    result = record(
        session,
        attributed,
        receipt(org, evidence, amount=1000, pass_through_items=[data]),
        "posted",
    )
    assert not result.missing_information
    assert (
        session.scalar(
            select(func.count()).select_from(Voucher).where(Voucher.event_id == result.event_id)
        )
        == 1
    )


def test_partial_payment_and_later_employee_advance(accounting):
    from ai_accounting.models import OpenItem

    session, org, evidence, attributed, bank = accounting
    source = record(
        session,
        attributed,
        receipt(org, evidence, amount=1000, pass_through_items=[split("Beneficiary", 1000)]),
    )
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == source.event_id))
    record(session, attributed, payment(org, evidence, item, 400))
    assert item.status == "partial" and item.settled_amount_fen == 400
    transfer = record(
        session,
        attributed,
        RecordEventRequest.model_validate(
            {
                "org_id": org.id,
                "idempotency_key": str(uuid.uuid4()),
                "posting_date": "2026-08-10",
                "evidence_references": [evidence.id],
                "components": [
                    {
                        "key": "employee-advance-transfer",
                        "kind": "debt_transfer",
                        "business_date": "2026-08-10",
                        "payment_date": "2026-08-10",
                        "payer": {"kind": "employee", "name": "Later advancing person"},
                        "allocations": [{"open_item_id": item.id, "amount_fen": 600}],
                    }
                ],
            }
        ),
    )
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
    authority = session.info["test_authority"]
    engine = session.get_bind()
    session.commit()
    barrier = Barrier(2)

    def worker(request):
        with Session(engine) as worker_session:
            # Preload before the concurrent transaction to exercise stale ORM state too.
            cached = worker_session.get(OpenItem, item_id)
            assert cached.settled_amount_fen == 0
            barrier.wait(timeout=10)
            with authority.attributed_call(worker_session, tool_name="finance_record_event"):
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
