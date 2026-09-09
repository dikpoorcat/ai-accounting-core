from __future__ import annotations

import hashlib
import uuid
from datetime import date

import pytest
from sqlalchemy import select
from test_financial_statements import _evidence

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.enterprise_income_tax import EnterpriseIncomeTaxService, confirmation_effective
from ai_accounting.enterprise_income_tax_schemas import (
    ConfirmEnterpriseIncomeTaxResultRequest,
    PreviewEnterpriseIncomeTaxResultRequest,
    QueryEnterpriseIncomeTaxRequest,
)
from ai_accounting.financial_statement_schemas import ConfirmEnterpriseIncomeTaxQuarterRequest
from ai_accounting.financial_statements import FinancialStatementService
from ai_accounting.ledger import account_balance_fen
from ai_accounting.models import (
    BankTransaction,
    BusinessEvent,
    EnterpriseIncomeTaxQuarterConfirmation,
    EnterpriseIncomeTaxResult,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService


def root(session, org, evidence, *, year=2026, quarter=2, amount=10000):
    month = quarter * 3
    result = FinancialStatementService(session).confirm_enterprise_income_tax(
        ConfirmEnterpriseIncomeTaxQuarterRequest(
            org_id=org.id,
            year=year,
            quarter=quarter,
            treatment="accrue" if amount else "zero",
            amount_fen=amount,
            posting_date=date(year, month, 28) if amount else None,
            confirmation_note="原季度所得税",
            evidence_references=[evidence.id],
            idempotency_key=f"root:{year}:{quarter}",
        )
    )
    assert result.status == "posted", result
    return result.enterprise_income_tax_confirmation_id


def change(org, evidence, root_id, **kwargs):
    return PreviewEnterpriseIncomeTaxResultRequest.model_validate(
        {
            "org_id": org.id,
            "year": 2026,
            "quarter": 2,
            "original_confirmation_id": root_id,
            "declaration_date": "2026-08-05",
            "business_date": "2026-08-05",
            "posting_date": "2026-08-05",
            "declaration_reference": "更正申报回执",
            "amount_basis": "quarter",
            "declared_tax_fen": 15000,
            "confirmation_note": "核对二季度更正",
            "evidence_references": [evidence.id],
            **kwargs,
        }
    )


def confirm(service, request, key="correct"):
    preview = service.preview(request)
    assert preview["status"] == "calculated", preview
    confirm_request = ConfirmEnterpriseIncomeTaxResultRequest.model_validate(
        request.model_dump()
        | {"calculation_hash": preview["calculation_hash"], "idempotency_key": key}
    )
    result = service.confirm(confirm_request)
    assert result["status"] == "posted", result
    return result, confirm_request


def test_monthly_result_without_external_declaration_date(session, organization):
    evidence = _evidence(session, organization, "monthly-result.txt")
    root_id = root(session, organization, evidence)
    service = EnterpriseIncomeTaxService(session)
    request = change(
        organization,
        evidence,
        root_id,
        business_date=None,
        recognition_period="2026-08",
        posting_date="2026-08-31",
        declaration_date=None,
        declaration_reference=None,
        confirmation_note="",
    )
    preview = service.preview(request)
    decorated = request.model_copy(
        update={"declaration_date": date(2026, 9, 1), "declaration_reference": "管理编号"}
    )
    assert service.preview(decorated)["calculation_hash"] == preview["calculation_hash"]
    result, confirmed = confirm(service, request, key="monthly-cit")
    assert result["data"]["expense_adjustment_fen"] == 5000
    event = session.get(BusinessEvent, uuid.UUID(result["event_id"]))
    facts = event.facts["components"][0]
    assert facts["recognition_period"] == "2026-08"
    assert facts.get("business_date") is None
    assert facts.get("declaration_date") is None
    retry = service.confirm(confirmed.model_copy(update={"declaration_date": date(2026, 9, 1)}))
    assert retry["event_id"] == result["event_id"]


def payment(
    session,
    org,
    evidence,
    source_id,
    amount,
    *,
    key="payment",
    refund=False,
    posting_date=date(2026, 8, 8),
    allocations=None,
):
    bank = BankTransaction(
        org_id=org.id,
        bank_account_code="1002",
        fingerprint=hashlib.sha256(key.encode()).hexdigest(),
        booking_date=posting_date,
        amount_fen=amount if refund else -amount,
        currency="CNY",
        memo="缴退企业所得税",
        source_sha256="a" * 64,
    )
    if session.get_bind().dialect.name == "postgresql":
        from conftest import import_test_bank_transaction

        bank = import_test_bank_transaction(
            session, org, amount_fen=bank.amount_fen, key=key, booking_date=posting_date
        )
    else:
        session.add(bank)
        session.flush()
    request = RecordEventRequest.model_validate(
        {
            "org_id": org.id,
            "idempotency_key": key,
            "posting_date": posting_date,
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "income-tax-settlement",
                    "kind": "tax_settlement",
                    "business_date": posting_date,
                    "payment_date": posting_date,
                    "amount_fen": amount,
                    "tax_type": "enterprise_income_tax",
                    "settlement_kind": "refund" if refund else "payment",
                    "income_tax_allocations": allocations
                    or [{"source_id": source_id, "amount_fen": amount}],
                }
            ],
            "funds": [
                {
                    "key": "income-tax-funds",
                    "account_code": "1002",
                    "direction": "receipt" if refund else "payment",
                    "payment_date": posting_date,
                    "amount_fen": amount,
                    "allocations": [
                        {
                            "component_key": "income-tax-settlement",
                            "amount_fen": amount,
                        }
                    ],
                    "bank_transaction_references": [],
                }
            ],
        }
    )
    if session.get_bind().dialect.name == "postgresql":
        from conftest import _TEST_BANK_AUTHORITY_SESSION_KEY

        with session.info[_TEST_BANK_AUTHORITY_SESSION_KEY].attributed_call(
            session, tool_name="finance_record_event"
        ):
            return FinanceService(session).record_event(request), bank, request
    return FinanceService(session).record_event(request), bank, request


def test_q2_paid_then_august_correction_and_payment(session, organization):
    evidence = _evidence(session, organization, "q2-amend.txt")
    root_id = root(session, organization, evidence)
    first, bank, _ = payment(
        session, organization, evidence, root_id, 10000, posting_date=date(2026, 7, 10)
    )
    assert first.status == "posted", first
    assert bank.matched_event_id is None
    service = EnterpriseIncomeTaxService(session)
    corrected, request = confirm(service, change(organization, evidence, root_id))
    assert corrected["data"]["expense_adjustment_fen"] == 5000
    assert corrected["data"]["payable_fen"] == 5000
    assert corrected["event_id"]
    assert service.confirm(request)["data"]["idempotent_replay"]
    second, bank, req = payment(
        session, organization, evidence, corrected["result_id"], 5000, key="supplement"
    )
    assert second.status == "posted", second
    assert FinanceService(session).record_event(req).event_id == second.event_id
    assert bank.matched_event_id is None
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_payable") == 0
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_expense") == 15000
    original = session.get(EnterpriseIncomeTaxQuarterConfirmation, root_id)
    assert confirmation_effective(session, original, date(2026, 6, 30))
    assert confirmation_effective(session, original, date(2026, 9, 30))
    original_voucher = session.scalar(
        select(Voucher).where(Voucher.event_id == original.business_event_id)
    )
    assert original_voucher.posting_date == date(2026, 6, 28)
    assert (
        len(
            list(
                session.scalars(
                    select(VoucherLine).where(VoucherLine.voucher_id == original_voucher.id)
                )
            )
        )
        == 2
    )
    q2 = service.query(
        QueryEnterpriseIncomeTaxRequest(org_id=organization.id, as_of=date(2026, 6, 30))
    )
    assert q2["data"]["sources"][0]["recognized_tax_fen"] == 10000


def test_paid_reduction_refund_partial_and_repeated_correction(session, organization):
    evidence = _evidence(session, organization, "refund.txt")
    root_id = root(session, organization, evidence)
    paid, _, _ = payment(
        session, organization, evidence, root_id, 10000, posting_date=date(2026, 7, 10)
    )
    assert paid.status == "posted"
    service = EnterpriseIncomeTaxService(session)
    result, _ = confirm(service, change(organization, evidence, root_id, declared_tax_fen=7000))
    assert result["data"]["refundable_fen"] == 3000
    refunded, _, _ = payment(
        session, organization, evidence, result["result_id"], 2000, refund=True, key="refund"
    )
    assert refunded.status == "posted", refunded
    excess, _, _ = payment(
        session, organization, evidence, result["result_id"], 1001, refund=True, key="excess"
    )
    assert excess.status == "rejected"
    assert "CIT_SETTLEMENT_EXCEEDS_SOURCE_BALANCE" in excess.errors
    again, _ = confirm(
        service,
        change(
            organization,
            evidence,
            root_id,
            declared_tax_fen=6000,
            previous_result_id=result["result_id"],
            declaration_date="2026-08-10",
            business_date="2026-08-10",
            posting_date="2026-08-10",
        ),
        key="again",
    )
    assert again["data"]["refundable_fen"] == 2000
    assert again["data"]["expense_adjustment_fen"] == -1000
    blocked = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=refunded.event_id,
            posting_date=date(2026, 8, 11),
            reason="测试",
            idempotency_key="reverse-used-refund",
        )
    )
    assert "CIT_SETTLEMENT_USED_BY_LATER_RESULT" in blocked.errors


def test_latest_result_generic_reversal_restores_active_ancestor(session, organization):
    evidence = _evidence(session, organization, "result-reversal.txt")
    root_id = root(session, organization, evidence)
    paid, _, _ = payment(
        session,
        organization,
        evidence,
        root_id,
        3000,
        key="paid-before-result-reversal",
        posting_date=date(2026, 7, 10),
    )
    assert paid.status == "posted", paid
    service = EnterpriseIncomeTaxService(session)
    revised, _ = confirm(service, change(organization, evidence, root_id))
    original = session.get(EnterpriseIncomeTaxQuarterConfirmation, root_id)

    blocked = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=original.business_event_id,
            posting_date=date(2026, 8, 6),
            reason="仍有生效的后续结果",
            idempotency_key="reverse-result-ancestor-first",
        )
    )
    assert blocked.status == "rejected"

    reversed_result = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=uuid.UUID(revised["event_id"]),
            posting_date=date(2026, 8, 6),
            reason="撤销最新申报结果",
            idempotency_key="reverse-latest-result",
        )
    )
    assert reversed_result.status == "posted", reversed_result
    current = service.query(QueryEnterpriseIncomeTaxRequest(org_id=organization.id))["data"][
        "sources"
    ]
    assert len(current) == 1
    assert current[0]["source_id"] == str(root_id)
    assert current[0]["recognized_tax_fen"] == 10000
    assert current[0]["net_paid_fen"] == 3000
    assert current[0]["payable_fen"] == 7000
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_expense") == 10000

    replacement, _ = confirm(
        service,
        change(organization, evidence, root_id, declared_tax_fen=12000),
        key="result-after-reversal",
    )
    replacement_row = session.get(
        EnterpriseIncomeTaxResult,
        uuid.UUID(replacement["result_id"]),
    )
    assert replacement_row.revision == 2
    assert replacement["data"]["expense_adjustment_fen"] == 2000
    assert replacement["data"]["net_paid_fen"] == 3000
    assert replacement["data"]["payable_fen"] == 9000
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_expense") == 12000


def test_local_result_can_be_settled_by_two_later_siblings(session, organization):
    evidence = _evidence(session, organization, "local-result-payment.txt")
    root_id = root(session, organization, evidence, amount=0)
    service = EnterpriseIncomeTaxService(session)
    result_facts = change(
        organization,
        evidence,
        root_id,
        declared_tax_fen=1000,
    )
    preview = service.preview(result_facts)
    assert preview["status"] == "calculated", preview
    result_component = result_facts.model_dump(
        exclude={
            "org_id",
            "posting_date",
            "evidence_references",
            "declaration_reference",
            "confirmation_note",
        }
    ) | {
        "key": "result",
        "kind": "enterprise_income_tax_result",
        "business_date": result_facts.declaration_date,
        "calculation_hash": preview["calculation_hash"],
        "metadata": {
            "declaration_reference": result_facts.declaration_reference,
            "confirmation_note": result_facts.confirmation_note,
        },
    }
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "local-result-two-payments",
            "posting_date": result_facts.posting_date,
            "description": "确认申报结果并分两项支付",
            "evidence_references": [evidence.id],
            "components": [
                result_component,
                {
                    "key": "payment-a",
                    "kind": "tax_settlement",
                    "business_date": result_facts.posting_date,
                    "payment_date": result_facts.posting_date,
                    "amount_fen": 400,
                    "tax_type": "enterprise_income_tax",
                    "settlement_kind": "payment",
                    "income_tax_allocations": [
                        {
                            "source_component_key": "result",
                            "amount_fen": 400,
                        }
                    ],
                },
                {
                    "key": "payment-b",
                    "kind": "tax_settlement",
                    "business_date": result_facts.posting_date,
                    "payment_date": result_facts.posting_date,
                    "amount_fen": 600,
                    "tax_type": "enterprise_income_tax",
                    "settlement_kind": "payment",
                    "income_tax_allocations": [
                        {
                            "source_component_key": "result",
                            "amount_fen": 600,
                        }
                    ],
                },
            ],
            "funds": [
                {
                    "key": "bank",
                    "account_code": "1002",
                    "direction": "payment",
                    "payment_date": result_facts.posting_date,
                    "amount_fen": 1000,
                    "allocations": [
                        {"component_key": "payment-a", "amount_fen": 400},
                        {"component_key": "payment-b", "amount_fen": 600},
                    ],
                    "bank_transaction_references": [],
                }
            ],
        }
    )
    posted = FinanceService(session).record_event(request)
    assert posted.status == "posted", posted
    state = service.query(QueryEnterpriseIncomeTaxRequest(org_id=organization.id))["data"]
    assert state["sources"][0]["result_id"] is not None
    assert state["sources"][0]["net_paid_fen"] == 1000
    assert state["sources"][0]["balance_fen"] == 0
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_payable") == 0
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_expense") == 1000


def test_zero_noop_notice_and_stale_hash(session, organization):
    evidence = _evidence(session, organization, "zero.txt")
    root_id = root(session, organization, evidence, amount=0)
    service = EnterpriseIncomeTaxService(session)
    first, _ = confirm(service, change(organization, evidence, root_id, declared_tax_fen=0))
    assert first["event_id"] is None
    request = change(
        organization,
        evidence,
        root_id,
        previous_result_id=first["result_id"],
        amount_basis="adjustment_notice",
        declared_tax_fen=None,
        adjustment_fen=1000,
        previously_recognized_fen=0,
    )
    second, _ = confirm(service, request, key="notice")
    assert second["data"]["expense_adjustment_fen"] == 1000
    assert second["event_id"]
    req = change(organization, evidence, root_id, previous_result_id=second["result_id"])
    preview = service.preview(req)
    paid, _, _ = payment(session, organization, evidence, second["result_id"], 500, key="partial")
    assert paid.status == "posted"
    stale = service.confirm(
        ConfirmEnterpriseIncomeTaxResultRequest.model_validate(
            req.model_dump()
            | {"calculation_hash": preview["calculation_hash"], "idempotency_key": "stale"}
        )
    )
    assert stale["errors"] == ["CIT_CALCULATION_STALE"]
    assert (
        service.preview(request.model_copy(update={"evidence_references": []}))["status"]
        == "needs_information"
    )


def test_annual_refund_separate_from_next_year_payable(session, organization):
    evidence = _evidence(session, organization, "annual.txt")
    for q in range(1, 5):
        source = root(session, organization, evidence, year=2025, quarter=q, amount=1000)
        paid, _, _ = payment(
            session,
            organization,
            evidence,
            source,
            1000,
            key=f"paid-{q}",
            posting_date=date(2026, 1, q),
        )
        assert paid.status == "posted", paid
    service = EnterpriseIncomeTaxService(session)
    result, _ = confirm(
        service,
        change(
            organization,
            evidence,
            None,
            year=2025,
            quarter=0,
            amount_basis="annual",
            declared_tax_fen=3000,
        ),
    )
    assert result["data"]["expense_adjustment_fen"] == -1000
    assert result["data"]["refundable_fen"] == 1000
    source = root(session, organization, evidence, year=2026, quarter=2, amount=2000)
    paid, _, _ = payment(session, organization, evidence, source, 2000, key="next-year")
    assert paid.status == "posted", paid  # aggregate GL payable was only 1000
    refund, _, _ = payment(
        session, organization, evidence, result["result_id"], 1000, refund=True, key="annual-refund"
    )
    assert refund.status == "posted", refund
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_payable") == 0


def test_cumulative_result_and_strict_amounts(session, organization):
    evidence = _evidence(session, organization, "cumulative.txt")
    root(session, organization, evidence, quarter=1, amount=1000)
    root_id = root(session, organization, evidence, amount=2000)
    service = EnterpriseIncomeTaxService(session)
    result, _ = confirm(
        service,
        change(organization, evidence, root_id, amount_basis="year_to_date", declared_tax_fen=4000),
    )
    assert result["data"]["target_tax_fen"] == 3000
    assert result["data"]["expense_adjustment_fen"] == 1000
    for bad in (1.0, True, "100"):
        with pytest.raises(ValueError):
            change(organization, evidence, root_id, declared_tax_fen=bad)
    row = session.get(EnterpriseIncomeTaxResult, uuid.UUID(result["result_id"]))
    row.target_tax_fen = 1
    with pytest.raises(ValueError, match="IMMUTABLE"):
        session.flush()
    session.expire(row)


def test_annual_repeated_correction_only_adjusts_annual_difference(session, organization):
    evidence = _evidence(session, organization, "annual-correction.txt")
    for quarter in range(1, 5):
        root(session, organization, evidence, year=2025, quarter=quarter, amount=1000)
    service = EnterpriseIncomeTaxService(session)
    request = change(
        organization,
        evidence,
        None,
        year=2025,
        quarter=0,
        amount_basis="annual",
        declared_tax_fen=6000,
    )
    first, _ = confirm(service, request, key="annual-first")
    assert first["data"]["expense_adjustment_fen"] == 2000
    second, _ = confirm(
        service,
        request.model_copy(
            update={
                "previous_result_id": uuid.UUID(first["result_id"]),
                "declared_tax_fen": 5000,
            }
        ),
        key="annual-second",
    )
    assert second["data"]["expense_adjustment_fen"] == -1000
    assert second["event_id"] != first["event_id"]
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_payable") == -5000
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_expense") == 5000
    history = service.query(QueryEnterpriseIncomeTaxRequest(org_id=organization.id))["data"][
        "history"
    ]
    assert history[-1]["previous_result_id"] == first["result_id"]
    assert history[-1]["evidence_references"] == [str(evidence.id)]


def test_closed_q2_current_posting_and_atomic_rollback(session, organization, monkeypatch):
    from test_financial_statements import _close_quarter

    from ai_accounting import component_service
    from ai_accounting.models import (
        AccountingPeriod,
        AccountingPeriodAction,
        AccountingPeriodCalendar,
    )

    evidence = _evidence(session, organization, "closed-q2.txt")
    root_id = root(session, organization, evidence)
    calendar_row = AccountingPeriodCalendar(
        org_id=organization.id,
        calendar_year=2026,
        rule_version="test",
        rule_effective_from=date(2026, 1, 1),
        source_urls=["https://example.test"],
    )
    session.add(calendar_row)
    session.flush()
    action = AccountingPeriodAction(
        org_id=organization.id,
        action_type="period_generation",
        idempotency_key="q2-generation",
        request_payload_hash="a" * 64,
        status="posted",
        input_facts={},
        missing_information=[],
        errors=[],
        confirmed_by="test",
        confirmation_note="测试期间",
    )
    session.add(action)
    session.flush()
    period = AccountingPeriod(
        org_id=organization.id,
        calendar_id=calendar_row.id,
        generation_action_id=action.id,
        calendar_year=2026,
        calendar_month=6,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 30),
        status="open",
    )
    session.add(period)
    session.flush()
    _close_quarter(session, organization, [period])
    service = EnterpriseIncomeTaxService(session)
    blocked = service.preview(
        change(
            organization,
            evidence,
            root_id,
            posting_date="2026-06-30",
            declaration_date="2026-06-30",
            business_date="2026-06-30",
        )
    )
    assert blocked["errors"] == ["ACCOUNTING_PERIOD_CLOSED"]
    original = session.get(EnterpriseIncomeTaxQuarterConfirmation, root_id)
    prior_count = len(list(session.scalars(select(Voucher.id))))
    preview_request = change(organization, evidence, root_id)
    preview = service.preview(preview_request)
    command = ConfirmEnterpriseIncomeTaxResultRequest.model_validate(
        preview_request.model_dump()
        | {"calculation_hash": preview["calculation_hash"], "idempotency_key": "atomic"}
    )

    def fail(*args, **kwargs):
        raise ValueError("simulated replacement failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(component_service, "commit_posting_plan", fail)
        assert service.confirm(command)["status"] == "rejected"
    assert session.get(BusinessEvent, original.business_event_id).status == "posted"
    assert len(list(session.scalars(select(Voucher.id)))) == prior_count
    assert service.confirm(command)["status"] == "posted"
    assert period.status == "closed"
    assert account_balance_fen(session, organization.id, "enterprise_income_tax_expense") == 15000


def test_refund_cash_flow_and_reversal(session, organization):
    evidence = _evidence(session, organization, "cashflow.txt")
    source = root(session, organization, evidence)
    paid, _, _ = payment(
        session, organization, evidence, source, 10000, posting_date=date(2026, 7, 10)
    )
    assert paid.status == "posted"
    service = EnterpriseIncomeTaxService(session)
    revised, _ = confirm(service, change(organization, evidence, source, declared_tax_fen=7000))
    refunded, _, _ = payment(
        session,
        organization,
        evidence,
        revised["result_id"],
        3000,
        refund=True,
        key="cashflow-refund",
    )
    assert refunded.status == "posted"
    statements = FinancialStatementService(session)

    def cash():
        missing = []
        result = statements._cash_flow_statement(
            statements._ledger_rows(organization.id, date(2026, 9, 30)),
            organization.id,
            date(2026, 7, 1),
            date(2026, 1, 1),
            date(2026, 9, 30),
            missing,
        )
        assert missing == []
        return result

    assert cash()[2]["current_fen"] == 3000
    assert cash()[5]["current_fen"] == 10000
    reversed_result = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=refunded.event_id,
            posting_date=date(2026, 8, 9),
            reason="退款流水录错",
            idempotency_key="reverse-refund",
        )
    )
    assert reversed_result.status == "posted", reversed_result
    assert cash()[2]["current_fen"] == 0
    source_state = service.query(QueryEnterpriseIncomeTaxRequest(org_id=organization.id))["data"][
        "sources"
    ][0]
    assert source_state["refundable_fen"] == 3000


def test_foreign_source_and_invalid_date_are_rejected(session, organization):
    evidence = _evidence(session, organization, "foreign.txt")
    root_id = root(session, organization, evidence)
    service = EnterpriseIncomeTaxService(session)
    assert (
        service.preview(change(organization, evidence, uuid.uuid4()))["status"]
        == "needs_information"
    )
    assert service.preview(change(organization, evidence, root_id, business_date="2026-05-01"))[
        "errors"
    ] == ["CIT_RESULT_RECOGNITION_BEFORE_TAX_PERIOD_END"]
    invalid, _, _ = payment(session, organization, evidence, uuid.uuid4(), 1000)
    assert invalid.errors == ["CIT_CURRENT_SOURCE_REQUIRED"]


def test_combined_payment_and_unchanged_declared_amount(session, organization):
    evidence = _evidence(session, organization, "combined.txt")
    first = root(session, organization, evidence, quarter=1, amount=2000)
    second = root(session, organization, evidence, amount=3000)
    before_vouchers = len(list(session.scalars(select(Voucher.id))))
    service = EnterpriseIncomeTaxService(session)
    result, _ = confirm(service, change(organization, evidence, second, declared_tax_fen=3000))
    assert result["data"]["expense_adjustment_fen"] == 0
    assert len(list(session.scalars(select(Voucher.id)))) == before_vouchers
    paid, bank, _ = payment(
        session,
        organization,
        evidence,
        None,
        5000,
        allocations=[
            {"source_id": first, "amount_fen": 2000},
            {"source_id": result["result_id"], "amount_fen": 3000},
        ],
    )
    assert paid.status == "posted", paid
    assert bank.matched_event_id is None
    sources = service.query(QueryEnterpriseIncomeTaxRequest(org_id=organization.id))["data"][
        "sources"
    ]
    assert [v["balance_fen"] for v in sources] == [0, 0]


def test_pending_tax_keeps_external_reporting_proof(session, organization):
    from ai_accounting.owner_workflow import OwnerWorkflowService

    evidence = _evidence(session, organization, "pending.txt")
    source = root(session, organization, evidence, amount=0)
    confirm(EnterpriseIncomeTaxService(session), change(organization, evidence, source))
    workflow = OwnerWorkflowService(session)
    step = workflow._income_tax_settlement_step(
        organization,
        workflow._completed([{"kind": "external_obligation_confirmation", "id": "proof"}]),
        annual=False,
    )
    assert step["external_reporting_completion_state"] == "completed"
    assert step["completion_state"] == "incomplete"
    assert step["enterprise_income_tax_pending_settlements"][0]["payable_fen"] == 15000


def test_closed_quarter_report_hash_unchanged_after_replacement(session, organization, monkeypatch):
    import test_financial_statements as report_helpers

    from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
    from ai_accounting.accounting_period_service import AccountingPeriodService
    from ai_accounting.models import AccountingPeriod, Evidence

    # Prepare the existing balanced report fixture, delaying its close until the
    # quarter's positive income tax has been recognized.
    with monkeypatch.context() as scoped:
        scoped.setattr(report_helpers, "_close_quarter", lambda *args: None)
        statements, report_request = report_helpers._prepare_calculated_q1(session, organization)
    evidence = session.scalar(select(Evidence).where(Evidence.org_id == organization.id))
    source = session.scalar(select(EnterpriseIncomeTaxQuarterConfirmation.id))
    service = EnterpriseIncomeTaxService(session)
    first, _ = confirm(
        service,
        change(
            organization,
            evidence,
            source,
            quarter=1,
            declared_tax_fen=10000,
            declaration_date="2026-03-31",
            business_date="2026-03-31",
            posting_date="2026-03-31",
        ),
        key="before-close",
    )
    periods = list(session.scalars(select(AccountingPeriod)))
    report_helpers._close_quarter(session, organization, periods)
    before = statements.preview_quarterly(report_request)
    assert before.status == "calculated", before
    for month in range(4, 9):
        generated = AccountingPeriodService(session).generate_accounting_period(
            GenerateAccountingPeriodRequest(
                org_id=organization.id,
                period_month=f"2026-{month:02d}",
                idempotency_key=f"open-month-{month}",
                confirmation_note="明确在八月入账",
                evidence_references=[evidence.id],
            )
        )
        assert generated.status == "posted", generated
    after_result, _ = confirm(
        service,
        change(
            organization,
            evidence,
            source,
            quarter=1,
            previous_result_id=first["result_id"],
        ),
        key="after-close",
    )
    assert after_result["event_id"] != first["event_id"]
    after = statements.preview_quarterly(report_request)
    assert after.status == "calculated", after
    assert after.calculation_hash == before.calculation_hash
    assert after.data["statements"] == before.data["statements"]
