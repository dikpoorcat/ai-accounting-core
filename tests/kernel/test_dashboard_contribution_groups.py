"""Contribution presentation identities use formal facts without changing obligations."""

import pytest

from ai_accounting.kernel.dashboard import _contribution_identity


COMPONENTS = ("employee_social", "employer_social", "employee_housing", "employer_housing")


def test_contribution_identity_reuses_employee_month_across_sources_and_opening():
    rows = [
        _contribution_identity("company", kind, fact, component)
        for component in COMPONENTS
        for kind, fact in (
            ("payroll", {"employee_id": "employee", "period": "2026-01"}),
            ("payroll_bounded", {"employee_id": "employee", "period": "2026-01"}),
            ("opening_payroll_payable", {
                "employee_id": "employee", "period": "2026-02", "payroll_period": "2026-01"
            }),
        )
    ]
    key = rows[0]["contribution_group_key"]
    assert key is not None and len(key) == 64
    assert {row["contribution_group_key"] for row in rows} == {key}
    assert {row["payroll_period"] for row in rows} == {"2026-01"}
    for company, employee, month in (
        ("other-company", "employee", "2026-01"),
        ("company", "other-employee", "2026-01"),
        ("company", "employee", "2026-02"),
    ):
        assert _contribution_identity(company, "payroll", {
            "employee_id": employee, "period": month, "display_name": "同名员工"
        }, "employee_social")["contribution_group_key"] != key


@pytest.mark.parametrize("fact,month", [
    ({"period": "2026-01"}, "2026-01"),
    ({"employee_id": "" , "period": "2026-01"}, "2026-01"),
    ({"employee_id": "employee"}, None),
    ({"employee_id": "employee", "period": "2026-13"}, None),
    ({"employee_id": "employee", "period": "2026-1"}, None),
])
def test_incomplete_identity_keeps_known_component_without_inferred_group(fact, month):
    assert _contribution_identity("company", "payroll", fact, "employer_housing") == {
        "contribution_group_key": None,
        "contribution_component": "employer_housing",
        "payroll_period": month,
    }


@pytest.mark.parametrize("kind,component", [
    ("payroll", "net"), ("payroll", "tax"), ("opening_payroll_payable", "withheld_tax"),
    ("payroll", None), ("payroll", "unknown"), ("annual_bonus", "employee_social"),
    ("reimbursement_acceptance", "employee_social"),
])
def test_other_obligations_have_no_contribution_metadata(kind, component):
    assert _contribution_identity("company", kind, {
        "employee_id": "employee", "period": "2026-01", "payroll_period": "2026-01"
    }, component) == {
        "contribution_group_key": None, "contribution_component": None, "payroll_period": None
    }
