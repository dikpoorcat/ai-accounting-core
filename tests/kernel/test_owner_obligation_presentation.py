"""Owner details classify exact obligations without reading additional history."""

import pytest
from test_payroll_corrections import company as payroll_company

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_owner import obligation_view
from ai_accounting.kernel.obligation_classification import obligation_category
from ai_accounting.kernel.response_contracts import http_response

company = payroll_company


@pytest.mark.parametrize(
    "direction,account,kind,semantic,category",
    [
        ("receivable", "1122", "service_sale", None, "customer_receivables"),
        ("receivable", "1123", "supplier_advance", None, "supplier_advances"),
        ("receivable", "1221", "refundable_deposit", None, "refundable_deposit_receivables"),
        ("receivable", "1221", "expense", None, "other_receivables"),
        ("payable", "2202", "expense", None, "supplier_payables"),
        ("payable", "224101", "expense", None, "employee_payables"),
        ("payable", "224101", "reimbursed_deposit", None, "employee_payables"),
        ("receivable", "1221", "reimbursed_deposit", None, "refundable_deposit_receivables"),
        ("payable", "2241", "employee_advance", "employee", "employee_payables"),
        ("payable", "2241", "employee_advance", "owner", "other_payables"),
        ("payable", "2241", "reimbursement_acceptance", "employee", "employee_payables"),
        ("payable", "2241", "reimbursement_acceptance", "owner", "other_payables"),
        ("payable", "2241", "opening_obligation", "employee_reimbursement", "employee_payables"),
        ("payable", "2241", "opening_obligation", "owner_reimbursement", "other_payables"),
        (
            "receivable",
            "1221",
            "opening_obligation",
            "deposit_receivable",
            "refundable_deposit_receivables",
        ),
        ("receivable", "1221", "opening_obligation", "other_receivable", "other_receivables"),
        ("payable", "2211", "payroll", None, "payroll_payables"),
        ("payable", "2211", "annual_bonus", None, "payroll_payables"),
        ("payable", "2241", "labor_accrual", None, "labor_payables"),
        ("payable", "224104", "labor_project_cost", None, "labor_payables"),
        ("payable", "2241", "loan_drawdown", None, "other_payables"),
        (None, "2202", "expense", None, "unknown"),
        ("unverified", "2211", "payroll", None, "unknown"),
    ],
)
def test_owner_direction_uses_declared_category_and_shared_classifier(
    direction, account, kind, semantic, category
):
    source = {
        "key": "exact-obligation",
        "name": "primary",
        "category": direction,
        "account": account,
        "source_business": {"kind": kind},
        "classification_semantic": semantic,
        "source_period": "2026-09",
        "source_amount_fen": 9007199254740993,
        "paid_fen": None,
        "other_settled_fen": 0,
        "remaining_fen": None,
        "settlement_status": "unestablished",
    }
    result = obligation_view(source)
    assert result["direction"] == (
        direction if direction in {"receivable", "payable"} else "unknown"
    )
    assert result["category_key"] == category
    assert result["source_amount_fen"] == 9007199254740993
    assert result["paid_fen"] is None and result["remaining_fen"] is None
    assert {"account", "category", "source_business"}.isdisjoint(result)


@pytest.mark.parametrize(
    "direction,account,kind,semantic",
    [
        ("payable", "2241", "opening_obligation", None),
        ("receivable", "1221", "opening_obligation", "unspecified"),
        ("payable", "1221", "opening_obligation", "deposit_receivable"),
        ("receivable", "2241", "opening_obligation", "employee_reimbursement"),
        ("payable", "2241", "employee_advance", None),
        ("payable", "2241", "employee_advance", "supplier"),
        ("payable", "224101", "reimbursement_acceptance", "owner"),
    ],
)
def test_classification_rejects_missing_or_conflicting_declared_meaning(
    direction, account, kind, semantic
):
    with pytest.raises(KernelError) as error:
        obligation_category(direction, account, kind, semantic=semantic)
    assert error.value.code == "content_integrity_failed"


def test_current_and_closed_payroll_details_retain_exact_obligation_classification(company):
    company.publish("january", "february")
    dashboard = Dashboard(company.engine)
    before = dashboard.business_status("2026-01", "january")
    assert before["schema_version"] == 9
    historical = before["data"]["settlements"]["obligations"]
    assert historical
    assert all(
        item["direction"] == "payable" and item["category_key"] == "payroll_payables"
        for item in historical
    )
    company.close("2026-01")
    after = dashboard.business_status("2026-01", "january")
    assert after["data"]["settlements"]["obligations"] == historical
    wire = http_response("dashboard_business_status", after)
    assert all(
        isinstance(item["source_amount_fen"], str)
        for item in wire["data"]["settlements"]["obligations"]
    )
    assert all(
        item["direction"] == "payable"
        for item in wire["data"]["current_followups"]["settlements"]["obligations"]
    )
