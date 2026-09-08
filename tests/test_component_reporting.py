from __future__ import annotations

import hashlib
from datetime import date

from sqlalchemy.orm import Session

from ai_accounting.financial_statements import FinancialStatementService
from ai_accounting.ledger import (
    CashFlowPlan,
    ComponentPostingPlan,
    Entry,
    commit_posting_plan,
)
from ai_accounting.models import Account, BusinessEvent, Organization, Voucher
from ai_accounting.tax import calculate_tax_period


def _event(
    session: Session,
    organization: Organization,
    key: str,
    components: list[ComponentPostingPlan],
    *,
    posting_date: date = date(2026, 3, 5),
) -> tuple[BusinessEvent, Voucher]:
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key=key,
        request_payload_hash=hashlib.sha256(key.encode()).hexdigest(),
        event_type="composite",
        status="draft",
        description=key,
        facts={"components": [plan.key for plan in components]},
        business_date=posting_date,
        posting_date=posting_date,
        rule_trace=[],
        rule_version="test-components",
    )
    voucher = commit_posting_plan(
        session,
        event=event,
        components=components,
        posting_date=posting_date,
        description=key,
    )
    return event, voucher


def _plan(
    key: str,
    kind: str,
    entries: list[Entry],
    *,
    facts: dict | None = None,
    derived: dict | None = None,
    cash_flows: list[CashFlowPlan] | None = None,
) -> ComponentPostingPlan:
    return ComponentPostingPlan(
        key=key,
        kind=kind,
        facts=facts or {"key": key, "kind": kind},
        derived=derived or {},
        entries=entries,
        cash_flows=cash_flows or [],
        rule_version="test-components",
    )


def test_cash_flow_uses_multiple_component_allocations_and_reverses_them(
    session: Session, organization: Organization
) -> None:
    event, voucher = _event(
        session,
        organization,
        "mixed-cash-flow",
        [
            _plan(
                "sale",
                "service_sale",
                [Entry(account_role="service_revenue", credit_fen=100)],
                cash_flows=[CashFlowPlan("1002", "cash_flow_1", 100)],
            ),
            _plan(
                "expense",
                "expense",
                [Entry(account_role="general_expense", debit_fen=40)],
                cash_flows=[CashFlowPlan("1002", "cash_flow_6", -40)],
            ),
            _plan("funds.in", "funds", [Entry(account_role="bank", debit_fen=100)]),
            _plan("funds.out", "funds", [Entry(account_role="bank", credit_fen=40)]),
        ],
    )
    _event(
        session,
        organization,
        "internal-transfer",
        [
            _plan(
                "transfer",
                "funds_transfer",
                [],
                cash_flows=[
                    CashFlowPlan("1002", "internal_transfer", -25),
                    CashFlowPlan("1001", "internal_transfer", 25),
                ],
            ),
            _plan(
                "funds",
                "funds",
                [
                    Entry(account_role="bank", credit_fen=25),
                    Entry(account_role="cash", debit_fen=25),
                ],
            ),
        ],
    )
    service = FinancialStatementService(session)
    rows = service._ledger_rows(organization.id, date(2026, 3, 31))
    missing = []
    statement = service._cash_flow_statement(
        rows,
        organization.id,
        date(2026, 1, 1),
        date(2026, 1, 1),
        date(2026, 3, 31),
        missing,
    )
    assert not missing
    assert statement[1]["current_fen"] == 100
    assert statement[6]["current_fen"] == 40
    assert statement[20]["current_fen"] == 60
    assert statement[22]["current_fen"] == 60

    reversal = BusinessEvent(
        org_id=organization.id,
        idempotency_key="mixed-cash-flow-reversal",
        request_payload_hash=hashlib.sha256(b"mixed-cash-flow-reversal").hexdigest(),
        event_type="reversal",
        status="draft",
        description="reverse",
        facts={"original_event_id": str(event.id)},
        business_date=date(2026, 3, 6),
        posting_date=date(2026, 3, 6),
        rule_trace=[],
    )
    session.add(reversal)
    session.flush()
    from ai_accounting.component_lifecycle import reversal_plans

    commit_posting_plan(
        session,
        event=reversal,
        posting_date=date(2026, 3, 6),
        description="reverse",
        reversal_of=voucher,
        components=reversal_plans(session, event, voucher),
    )
    event.status = "reversed"
    voucher.status = "reversed"
    session.flush()

    rows = service._ledger_rows(organization.id, date(2026, 3, 31))
    missing = []
    reversed_statement = service._cash_flow_statement(
        rows,
        organization.id,
        date(2026, 1, 1),
        date(2026, 1, 1),
        date(2026, 3, 31),
        missing,
    )
    assert not missing
    assert reversed_statement[1]["current_fen"] == 0
    assert reversed_statement[6]["current_fen"] == 0
    assert reversed_statement[20]["current_fen"] == 0
    assert reversed_statement[22]["current_fen"] == 0


def test_cash_flow_reconciles_each_account_and_direction_before_netting(
    session: Session, organization: Organization
) -> None:
    _event(
        session,
        organization,
        "gross-direction-mismatch",
        [
            _plan(
                "net-only",
                "service_sale",
                [Entry(account_role="service_revenue", credit_fen=60)],
                cash_flows=[CashFlowPlan("1002", "cash_flow_1", 60)],
            ),
            _plan(
                "funds",
                "funds",
                [
                    Entry(account_role="bank", debit_fen=100),
                    Entry(account_role="bank", credit_fen=40),
                ],
            ),
        ],
    )
    service = FinancialStatementService(session)
    missing = []
    service._cash_flow_statement(
        service._ledger_rows(organization.id, date(2026, 3, 31)),
        organization.id,
        date(2026, 1, 1),
        date(2026, 1, 1),
        date(2026, 3, 31),
        missing,
    )
    requirement = next(
        item for item in missing if item.code == "FINANCIAL_STATEMENT_CASH_ALLOCATION_MISMATCH"
    )
    assert {
        (row["direction"], row["ledger_fen"], row["allocated_fen"])
        for row in requirement.data["accounts"]
    } == {
        ("receipt", 100, 60),
        ("payment", 40, 0),
    }


def test_profit_uses_detail_business_class_and_component_exceptions(
    session: Session, organization: Organization
) -> None:
    detail = Account(
        org_id=organization.id,
        code="560209",
        name="管理费用—差旅费",
        category="expense",
        normal_side="debit",
        business_class="general_expense",
    )
    session.add(detail)
    session.flush()
    _event(
        session,
        organization,
        "component-profit",
        [
            _plan(
                "travel",
                "expense",
                [Entry(account_code=detail.code, debit_fen=20)],
                facts={"key": "travel", "kind": "expense", "expense_class": "general_expense"},
            ),
            _plan(
                "interest",
                "other_income",
                [Entry(account_role="finance_expense", credit_fen=5)],
                facts={"key": "interest", "kind": "other_income", "income_kind": "bank_interest"},
            ),
            _plan(
                "relief",
                "tax_period_adjustment",
                [Entry(account_role="tax_relief_income", credit_fen=3)],
                derived={"tax_relief_fen": 3},
            ),
            _plan(
                "balance",
                "funds",
                [
                    Entry(account_role="bank", debit_fen=8),
                    Entry(account_role="paid_in_capital", credit_fen=20),
                ],
            ),
        ],
    )
    service = FinancialStatementService(session)
    rows = service._ledger_rows(organization.id, date(2026, 3, 31))
    travel = next(row for row in rows if row.account.code == detail.code)
    assert travel.role == "general_expense"
    assert travel.component_kind == "expense"
    assert travel.component_facts["expense_class"] == "general_expense"

    classifications, requirements = service._classification_state(
        organization.id, rows, date(2026, 1, 1), date(2026, 3, 31)
    )
    assert not classifications
    assert [item.data["voucher_line_id"] for item in requirements] == [str(travel.line.id)]

    missing = []
    statement = service._profit_statement(
        rows,
        {},
        date(2026, 1, 1),
        date(2026, 1, 1),
        date(2026, 3, 31),
        organization.id,
        missing,
    )
    assert statement[14]["current_fen"] == 20
    assert statement[18]["current_fen"] == -5
    assert statement[19]["current_fen"] == -5
    assert statement[22]["current_fen"] == 3
    assert statement[23]["current_fen"] == 3


def test_tax_period_keeps_multiple_component_sources_and_component_reversal(
    session: Session, organization: Organization
) -> None:
    taxable_date = "2026-03-05"
    first_event, _ = _event(
        session,
        organization,
        "two-tax-components",
        [
            _plan(
                "sale-a",
                "service_sale",
                [
                    Entry(account_role="accounts_receivable", debit_fen=101),
                    Entry(account_role="service_revenue", credit_fen=100),
                    Entry(account_role="vat_payable", credit_fen=1),
                ],
                derived={
                    "tax_obligation_date": taxable_date,
                    "taxable_gross_fen": 101,
                    "net_sales_fen": 100,
                    "vat_fen": 1,
                    "exemption_eligible": True,
                },
            ),
            _plan(
                "sale-b",
                "service_sale",
                [
                    Entry(account_role="accounts_receivable", debit_fen=202),
                    Entry(account_role="service_revenue", credit_fen=200),
                    Entry(account_role="vat_payable", credit_fen=2),
                ],
                derived={
                    "tax_obligation_date": taxable_date,
                    "taxable_gross_fen": 202,
                    "net_sales_fen": 200,
                    "vat_fen": 2,
                    "exemption_eligible": True,
                },
            ),
        ],
    )
    reversal_event, _ = _event(
        session,
        organization,
        "tax-component-reversal",
        [
            _plan(
                "refund-a",
                "customer_refund",
                [
                    Entry(account_role="service_revenue", debit_fen=100),
                    Entry(account_role="vat_payable", debit_fen=1),
                    Entry(account_role="accounts_receivable", credit_fen=101),
                ],
                derived={
                    "tax_obligation_date": taxable_date,
                    "taxable_gross_fen": -101,
                    "net_sales_fen": -100,
                    "vat_fen": -1,
                    "exemption_eligible": True,
                },
            )
        ],
        posting_date=date(2026, 3, 6),
    )

    result = calculate_tax_period(
        session,
        organization,
        date(2026, 1, 1),
        date(2026, 3, 31),
        date(2026, 3, 31),
    )
    assert (result.gross_sales_fen, result.net_sales_fen, result.vat_accrued_fen) == (
        202,
        200,
        2,
    )
    identities = [(row["event_id"], row["component_id"]) for row in result.source_events]
    assert identities == sorted(identities)
    assert len(identities) == 3
    assert result.to_dict()["source_events"] == sorted(
        {str(first_event.id), str(reversal_event.id)}
    )
    assert all({"event_id", "component_id"} <= row.keys() for row in result.source_events)
