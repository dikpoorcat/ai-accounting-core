"""Formal matters, exact payment slots and channel-independent display groups."""

import pytest
from test_banking import book as book
from test_dashboard_activity_classification import _allocation, _expense, _payment
from test_dashboard_open_groups import opening_book as opening_book
from test_dashboard_open_groups import snapshot
from test_dashboard_provenance import profile

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_brief_groups import activity_group_page
from ai_accounting.kernel.dashboard_matters import activity_matter, obligation_matter
from ai_accounting.kernel.dashboard_open_groups import open_group_page
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.response_contracts import validate_response


@pytest.mark.parametrize(
    "kind,component,data,key",
    [
        ("payroll", "net", {}, "salary"),
        ("payroll_bounded", "net", {}, "salary"),
        ("opening_payroll_payable", "primary", {"component": "net"}, "salary"),
        ("annual_bonus", "net", {}, "bonus"),
        ("payroll", "tax", {}, "individual-income-tax"),
        ("annual_bonus", "tax", {}, "individual-income-tax"),
        ("labor", "tax", {}, "individual-income-tax"),
        ("labor_accrual", "withheld_tax", {}, "individual-income-tax"),
        ("payroll", "employee_social", {}, "social"),
        ("payroll_bounded", "employer_social", {}, "social"),
        ("payroll", "employee_housing", {}, "housing"),
        ("opening_payroll_payable", "primary", {"component": "employer_housing"}, "housing"),
        ("tax_assessment", "vat", {}, "value-added-tax"),
        ("tax_assessment", "surtax", {}, "surtax"),
        ("income_tax_assessment", "tax", {}, "corporate-income-tax"),
        ("opening_tax", "primary", {"tax_kind": "enterprise_income_tax"}, "corporate-income-tax"),
        (
            "opening_obligation",
            "primary",
            {"nature": "supplier_service_payable"},
            "expense-service",
        ),
        (
            "expense",
            "primary",
            {"creditor_kind": "supplier", "expense_class": "service"},
            "expense-service",
        ),
        (
            "expense",
            "primary",
            {"creditor_kind": "individual", "expense_class": "administration"},
            "expense-administration",
        ),
        (
            "expense",
            "primary",
            {"creditor_kind": "supplier", "expense_class": "tax_late_fee"},
            "expense-tax_late_fee",
        ),
        (
            "expense",
            "primary",
            {"creditor_kind": "supplier", "expense_class": "social_contribution_late_fee"},
            "expense-social_contribution_late_fee",
        ),
    ],
)
def test_adopted_formal_matter_mapping(kind, component, data, key):
    assert obligation_matter(kind, component, data=data).key == key


@pytest.mark.parametrize("nature", ["other_receivable", "other_payable"])
def test_generic_opening_nature_does_not_claim_a_shared_matter(nature):
    assert obligation_matter("opening_obligation", "primary", data={"nature": nature}) is None
    assert activity_matter("opening_obligation", {"nature": nature}) is None


def formal_payment_samples(opening_book, *, separate=False, housing=True):
    """Synthetic precise adopted sources reused by kernel and browser samples."""
    engine, save, publish, package, proof = opening_book
    amounts = {
        "withheld_tax": 140307,
        "employee_social": 300000,
        "employer_social": 319000,
        "employee_housing": 20000,
        "employer_housing": 30000,
    }
    if not housing:
        amounts = {name: amount for name, amount in amounts.items() if "housing" not in name}
    package(
        [
            (
                "opening_bank",
                "bank-start",
                {"bank_account_id": "bank", "balance_fen": sum(amounts.values())},
            ),
            *[
                (
                    "opening_payroll_payable",
                    component,
                    {
                        "employee_id": "employee",
                        "recipient_id": "authority",
                        "payroll_period": "2025-12",
                        "component": component,
                        "outstanding_fen": amount,
                    },
                )
                for component, amount in amounts.items()
            ],
        ]
    )
    parts = (
        {
            "tax-105": ["withheld_tax"],
            "social-108": ["employee_social", "employer_social"],
            **({"housing": ["employee_housing", "employer_housing"]} if housing else {}),
        }
        if separate
        else {"combined": list(amounts)}
    )
    for subject, components in parts.items():
        save(
            "payment",
            subject,
            {
                "period": "2026-01",
                "actual_date": "2026-01-10",
                "direction": "outflow",
                "bank_account_id": "bank",
                "counterparty_id": "authority",
                "amount_fen": sum(amounts[name] for name in components),
                "allocations": [
                    _allocation("opening_payroll_payable", name, amounts[name])
                    for name in components
                ],
            },
        )
        publish(subject)
        profile(engine, "business", subject, purpose="原单事项说明", note="原单保留备注")
    return Dashboard(engine)


def test_same_recipient_tax140307_and_social619000_remain_two_matters(opening_book):
    dashboard = formal_payment_samples(opening_book, separate=True, housing=False)
    before = dashboard.engine.ledger("2026-01")
    data = dashboard.brief("2026-01")["data"]
    payments = data["collections"]["activity"]["items"]
    assert {(row["title"], row["amount_fen"], row["group"]) for row in payments} == {
        ("缴纳个税", 140307, "tax"),
        ("缴纳社保", 619000, "payroll"),
    }
    subjects = set()
    for group in payments:
        response = dashboard.brief_group(
            "2026-01", section="activity", group_key=group["group_key"]
        )
        validate_response("dashboard_brief_group", response)
        subjects.update(
            row["subject_id"] for row in response["data"]["collections"]["members"]["items"]
        )
    assert subjects == {"tax-105", "social-108"}
    assert dashboard.engine.ledger("2026-01") == before


def test_same_voucher_many_matters_conserve_money_and_exact_scope(opening_book):
    dashboard = formal_payment_samples(opening_book)
    Periods(dashboard.engine).management(
        "combined", note="原单管理备注", payment_period=None, payment_category=None,
        expected_revision=0, request_id="combined-management-note",
    )
    before = dashboard.engine.ledger("2026-01")
    response = dashboard.brief("2026-01")
    validate_response("dashboard_brief", response)
    data = response["data"]
    assert data["voucher_count"] == 1 and data["activity_count"] == data["group_count"] == 3
    assert sum(row["amount_fen"] for row in data["collections"]["activity"]["items"]) == 809307
    members, versions = [], set()
    for group in data["collections"]["activity"]["items"]:
        page = dashboard.brief_group("2026-01", section="activity", group_key=group["group_key"])
        member = page["data"]["collections"]["members"]["items"][0]
        members.append(member)
        assert member["key"] == member["detail_scope_key"]
        assert "整单说明：原单事项说明；原单保留备注；原单管理备注" in member["description"]
        vouchers = page["data"]["collections"]["vouchers"]["items"]
        assert len(vouchers) == 1
        versions.add(vouchers[0]["voucher_version_id"])
    for member in members:
        status = dashboard.business_status(
            "2026-01",
            "combined",
            voucher_version_id=member["voucher_version_id"],
            detail_scope_key=member["key"],
            limit=1,
        )
        validate_response("dashboard_business_status", status)
        scope = status["data"]["detail_scope"]
        assert scope["key"] == member["key"] and scope["amount_fen"] == member["amount_fen"]
        assert scope["category"] == member["group"]
        events = status["data"]["collections"]["settlement_events"]
        if member["group"] == "payroll":
            assert events["page"]["total_count"] == 2
            other = next(
                row for row in members if row["group"] == "payroll" and row["key"] != member["key"]
            )
            with pytest.raises(KernelError) as mismatch:
                dashboard.business_status(
                    "2026-01",
                    "combined",
                    voucher_version_id=member["voucher_version_id"],
                    detail_scope_key=other["key"],
                    section="settlement_events",
                    limit=1,
                    cursor=events["page"]["next_cursor"],
                )
            assert mismatch.value.code == "dashboard_snapshot_changed"
    assert len(versions) == 1
    assert dashboard.engine.ledger("2026-01") == before


def test_payment_member_keeps_management_only_note_without_changing_scope(book):
    engine, save, publish, _ = book
    _expense(save, publish, "cost", "alice", 1000, "employee")
    _payment(book, "paid", "alice", [_allocation("expense", "cost", 1000)])
    dashboard = Dashboard(engine)
    group = next(
        row for row in dashboard.brief("2026-09")["data"]["collections"]["activity"]["items"]
        if row["kind"] == "payment"
    )
    before = dashboard.brief_group(
        "2026-09", section="activity", group_key=group["group_key"]
    )["data"]["collections"]["members"]["items"][0]
    ledger = engine.ledger("2026-09")
    Periods(engine).management(
        "paid", note="仅管理资料保留的付款备注", payment_period=None, payment_category=None,
        expected_revision=0, request_id="payment-management-only-note",
    )
    member = dashboard.brief_group(
        "2026-09", section="activity", group_key=group["group_key"]
    )["data"]["collections"]["members"]["items"][0]
    assert "仅管理资料保留的付款备注" in member["description"]
    assert "整单说明：" not in member["description"]
    for field in ("key", "group_key", "amount_fen", "detail_scope_key", "voucher_version_id"):
        assert member[field] == before[field]
    assert member["amount_fen"] == 1000
    status = dashboard.business_status(
        "2026-09", "paid", voucher_version_id=member["voucher_version_id"],
        detail_scope_key=member["key"],
    )["data"]
    assert status["detail_scope"]["key"] == member["key"]
    assert status["detail_scope"]["amount_fen"] == 1000
    assert status["collections"]["settlement_events"]["page"]["total_count"] == 1
    assert engine.ledger("2026-09") == ledger


def test_same_matter_merges_bank_cash_and_platform_and_keeps_purposes(book):
    engine, save, publish, _ = book
    _expense(save, publish, "cost", "alice", 3000, "employee")
    for channel in ("bank", "cash", "platform"):
        _payment(
            book,
            channel + "-paid",
            "alice",
            [_allocation("expense", "cost", 1000)],
            channel=channel,
            date="2026-09-" + {"bank": "27", "cash": "28", "platform": "29"}[channel],
        )
        profile(engine, "business", channel + "-paid", purpose=channel + "用途")
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-09") as snap:
        groups = activity_group_page(snap)["items"]
    group = next(row for row in groups if row["member_count"] == 3)
    assert group["amount_fen"] == 3000 and group["voucher_count"] == 3
    page = dashboard.brief_group("2026-09", section="activity", group_key=group["group_key"])
    members = page["data"]["collections"]["members"]["items"]
    assert {row["subject_id"] for row in members} == {"bank-paid", "cash-paid", "platform-paid"}
    assert all(row["subject_id"].split("-")[0] + "用途" in row["description"] for row in members)
    assert all("整单说明：" not in row["description"] for row in members)


def test_same_agency_distinct_formal_late_fee_matters_remain_separate(book):
    engine, save, publish, _ = book
    for nature in ("tax_late_fee", "social_contribution_late_fee"):
        save(
            "expense",
            nature,
            {
                "period": "2026-09",
                "counterparty_id": "authority",
                "amount_fen": 1000,
                "expense_class": nature,
                "creditor_kind": "supplier",
            },
        )
        publish(nature)
        _payment(book, nature + "-paid", "authority", [_allocation("expense", nature, 500)])
    dashboard = Dashboard(engine)
    data = dashboard.brief("2026-09")["data"]
    assert len(data["collections"]["activity"]["items"]) == 4
    with snapshot(engine, "2026-09") as snap:
        groups = open_group_page(snap)["collection"]["items"]
    assert {(row["description"], row["outstanding_fen"]) for row in groups} == {
        ("税收滞纳金", 500),
        ("社保滞纳金", 500),
    }


def test_known_object_generic_payables_are_independent_in_both_sections(opening_book):
    engine, save, publish, package, _ = opening_book
    package(
        [
            ("opening_bank", "bank-start", {"bank_account_id": "bank", "balance_fen": 2000}),
            *[
                (
                    "opening_obligation",
                    subject,
                    {
                        "counterparty_id": "supplier",
                        "nature": "other_payable",
                        "outstanding_fen": 1000,
                        "business_reference": subject,
                    },
                )
                for subject in ("generic-a", "generic-b")
            ],
        ]
    )
    save(
        "payment",
        "generic-payment",
        {
            "period": "2026-01",
            "actual_date": "2026-01-10",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "supplier",
            "amount_fen": 200,
            "allocations": [
                _allocation("opening_obligation", subject, 100)
                for subject in ("generic-a", "generic-b")
            ],
        },
    )
    publish("generic-payment")
    dashboard = Dashboard(engine)
    data = dashboard.brief("2026-01")["data"]
    assert data["group_count"] == data["activity_count"] == 2
    assert data["voucher_count"] == 1
    assert {row["title"] for row in data["collections"]["activity"]["items"]} == {"付款"}
    assert {row["member_count"] for row in data["collections"]["activity"]["items"]} == {1}
    pending = data["collections"]["open_items"]["items"]
    assert len(pending) == 2 and all(row["member_count"] == 1 for row in pending)


def test_adopted_opening_taxes_have_distinct_matters_and_one_tax_navigation(opening_book):
    engine, _, _, package, _ = opening_book
    kinds = {
        "vat": "增值税",
        "surtax": "附加税",
        "enterprise_income_tax": "企业所得税",
        "individual_income_tax": "个人所得税",
    }
    package(
        [
            ("opening_bank", "bank-start", {"bank_account_id": "bank", "balance_fen": 4000}),
            *[
                (
                    "opening_tax",
                    name,
                    {
                        "authority_id": "authority",
                        "tax_kind": name,
                        "balance_kind": "payable",
                        "outstanding_fen": 1000,
                        "tax_period": "2025-12",
                    },
                )
                for name in kinds
            ],
        ]
    )
    with snapshot(engine, "2026-01") as snap:
        result = open_group_page(snap)
    assert result["total_count"] == result["group_count"] == 4
    assert result["payable_fen"] == 4000
    assert {row["description"] for row in result["collection"]["items"]} == set(kinds.values())
    assert {row["category_key"] for row in result["collection"]["items"]} == {"tax_payables"}
    assert result["categories"] == [
        {
            "key": "tax_payables",
            "label": "待缴税费",
            "direction": "payable",
            "unit": "笔",
            "count": 4,
            "group_count": 4,
            "loaded_count": 4,
            "outstanding_fen": 4000,
        }
    ]
