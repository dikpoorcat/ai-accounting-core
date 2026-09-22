from types import MappingProxyType

import pytest
from pydantic import ValidationError

from ai_accounting.kernel import dashboard, dashboard_funds, query_semantics
from ai_accounting.kernel.account_definitions import (
    ACCOUNT_NAMES,
    KNOWN_POSITION_ACCOUNTS,
    TAX_ACCOUNT_BY_KIND,
)
from ai_accounting.kernel.domains.cash import CashPayment
from ai_accounting.kernel.domains.money import (
    FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY,
    PAYMENT_FUNDS_ACCOUNT_BY_KIND,
    SETTLEMENT_PAYMENT_KINDS,
    payment_funds_account,
)
from ai_accounting.kernel.domains.payroll_reserve_payment import PayrollReservePayment
from ai_accounting.kernel.domains.platforms import PlatformPayment
from ai_accounting.kernel.domains.transactions import Payment
from ai_accounting.kernel.query_semantics import resolve_calculation_relations
from ai_accounting.kernel.workflow import OBLIGATION_DEFINITIONS, SOURCES, ObligationKind


def _calculation(ident, *, kind, subject_id=None, fact=None, lines=(), values=None):
    return {
        "id": ident,
        "kind": kind,
        "subject_id": subject_id or ident,
        "period": 24312,
        "fact_id": "fact-" + ident,
        "fact_data": fact or {},
        "outcome": {"lines": list(lines), "values": values or {}},
        "result_digest": "digest-" + ident,
    }


def test_account_and_funds_consumers_share_the_canonical_definitions():
    assert dashboard.ACCOUNT_NAMES is ACCOUNT_NAMES
    assert query_semantics.KNOWN_POSITION_ACCOUNTS is KNOWN_POSITION_ACCOUNTS
    assert dashboard.FUND_TYPES is FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY
    assert dashboard_funds.FUND_TYPES is FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY
    assert TAX_ACCOUNT_BY_KIND == {
        "vat": "222101",
        "surtax": "222102",
        "individual_income_tax": "222103",
        "enterprise_income_tax": "222106",
    }
    assert "222104" not in TAX_ACCOUNT_BY_KIND.values()
    assert "222105" not in TAX_ACCOUNT_BY_KIND.values()


def test_unknown_nonzero_rows_remain_visible_even_when_their_total_is_zero():
    position = query_semantics.classify_financial_position(
        (
            {"account": "999999", "amount": 40},
            {"account": "999999", "amount": -40},
        )
    )

    assert not position["complete"]
    assert [issue["account"] for issue in position["issues"]] == ["999999", "999999"]
    assert position["assets_fen"] is None
    assert position["equity_fen"] is None


def test_payment_models_take_their_ledger_account_from_the_shared_mapping():
    assert SETTLEMENT_PAYMENT_KINDS == (
        "payment",
        "cash_payment",
        "platform_payment",
        "payroll_reserve_payment",
    )
    assert Payment.funds_account == payment_funds_account(Payment.kind) == "1002"
    assert CashPayment.funds_account == payment_funds_account(CashPayment.kind) == "1001"
    assert PlatformPayment.funds_account == payment_funds_account(PlatformPayment.kind) == "1012"
    assert (
        PayrollReservePayment.funds_account
        == payment_funds_account(PayrollReservePayment.kind)
        == "1002"
    )
    assert set(PAYMENT_FUNDS_ACCOUNT_BY_KIND) == set(SETTLEMENT_PAYMENT_KINDS)


@pytest.mark.parametrize(
    ("kind", "funds_account", "direction"),
    [
        ("payment", "1002", "inflow"),
        ("payment", "1002", "outflow"),
        ("cash_payment", "1001", "inflow"),
        ("cash_payment", "1001", "outflow"),
        ("platform_payment", "1012", "inflow"),
        ("platform_payment", "1012", "outflow"),
        ("payroll_reserve_payment", "1002", "outflow"),
    ],
)
def test_settlement_query_accepts_each_typed_payment_funds_line(kind, funds_account, direction):
    outgoing = direction == "outflow"
    source = _calculation(
        "source",
        kind="expense",
        subject_id="source-business",
        values={
            "obligations": [
                {
                    "key": "source:primary",
                    "name": "primary",
                    "account": "2202" if outgoing else "1122",
                    "normal": "credit" if outgoing else "debit",
                    "amount_fen": 40,
                    "category": "payable" if outgoing else "receivable",
                    "counterparty_id": "party",
                }
            ]
        },
    )
    obligation_line = {
        "account": "2202" if outgoing else "1122",
        "debit": 40 if outgoing else 0,
        "credit": 0 if outgoing else 40,
    }
    funds_line = {
        "account": funds_account,
        "debit": 0 if outgoing else 40,
        "credit": 40 if outgoing else 0,
    }
    lines = [obligation_line, funds_line] if outgoing else [funds_line, obligation_line]
    values = {
        "direction": direction,
        "settlements": [
            {
                "source_calculation": "source",
                "obligation": "source:primary",
                "amount_fen": 40,
            }
        ],
    }
    if kind == "payroll_reserve_payment":
        values["reserve_expense_fen"] = 10
        lines.extend(
            (
                {"account": "5602", "debit": 10, "credit": 0},
                {"account": funds_account, "debit": 0, "credit": 10},
            )
        )
    payment = _calculation(
        "payment",
        kind=kind,
        fact={
            "payment_method": "individual",
            "counterparty_id": "party",
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "source-business",
                    "obligation": "primary",
                    "amount_fen": 40,
                }
            ],
        },
        lines=lines,
        values=values,
    )

    resolved = resolve_calculation_relations(
        payment,
        load_calculation={"source": source}.__getitem__,
        load_parents=lambda _ident: (),
    )

    assert resolved["issues"] == []
    assert resolved["settlements"][0]["state"] == "resolved"


def test_payroll_reserve_payment_remains_outflow_only():
    schema = PayrollReservePayment.model_json_schema()
    assert schema["properties"]["direction"]["const"] == "outflow"
    with pytest.raises(ValidationError):
        PayrollReservePayment.model_validate(
            {
                "period": "2026-01",
                "actual_date": "2026-01-01",
                "direction": "inflow",
            }
        )


def test_obligation_definitions_are_ordered_immutable_and_drive_legacy_sources():
    assert isinstance(OBLIGATION_DEFINITIONS, MappingProxyType)
    assert tuple(OBLIGATION_DEFINITIONS) == (
        "contribution_declaration",
        "individual_income_tax",
        "quarterly_tax_and_reports",
        "annual_income_tax",
        "annual_business_report",
    )
    assert ObligationKind.__args__ == tuple(OBLIGATION_DEFINITIONS)
    assert SOURCES == {
        kind: definition.basis_kinds for kind, definition in OBLIGATION_DEFINITIONS.items()
    }
    assert OBLIGATION_DEFINITIONS["contribution_declaration"].check_payroll_population
    income_tax = OBLIGATION_DEFINITIONS["individual_income_tax"]
    assert income_tax.check_payroll_population
    assert income_tax.exclude_not_started
    assert not OBLIGATION_DEFINITIONS["quarterly_tax_and_reports"].exclude_not_started
