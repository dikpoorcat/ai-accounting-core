"""Owner summaries separate standard payroll matters from retained source profiles."""

import json

import pytest
from test_dashboard_matters import formal_payment_samples
from test_dashboard_open_groups import opening_book as opening_book
from test_dashboard_owner_presentation import display_profile
from test_dashboard_provenance import frozen_rows
from test_dashboard_voucher_adapter import activity_members
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_bounded import bounded
from test_payroll_corrections import Company, payment

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.response_contracts import validate_response

CUSTOM_NAME = "合成资料自定义名称"
PURPOSE = "合成工资核算复核说明"
NOTE = "合成原件位置与采用依据"


def add_source_text(engine, subject):
    display_profile(
        engine,
        "business",
        subject,
        display_name=CUSTOM_NAME,
        purpose=PURPOSE,
        note=NOTE,
    )
    Periods(engine).management(
        subject,
        payment_period=None,
        payment_category=None,
        expected_revision=0,
        request_id=f"management:{subject}",
    )


def assert_no_source_text(value):
    encoded = json.dumps(value, ensure_ascii=False)
    for text in (CUSTOM_NAME, PURPOSE, NOTE):
        assert text not in encoded


def assert_source_profile_retained(engine, period, subject):
    with Dashboard(engine)._snapshot(period) as snapshot:
        selected = snapshot.profile("business", subject)
        management = snapshot.management.get(subject)
    assert selected["display_name"] == CUSTOM_NAME
    assert selected["purpose"] == PURPOSE
    assert selected["note"] == NOTE
    assert "note" not in management
    stored = Display(engine).display_profiles(period=period)["profiles"]["business"][subject]
    assert stored["purpose"] == PURPOSE and stored["note"] == NOTE


@pytest.mark.parametrize("kind", ["payroll", "payroll_bounded"])
@pytest.mark.parametrize("closed", [False, True])
def test_payroll_owner_entries_use_standard_text_without_mutating_sources(tmp_path, kind, closed):
    company = Company(tmp_path / f"{kind}-{closed}.sqlite")
    for fact, subject in (
        (profile(effective_to="2026-02"), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll() if kind == "payroll" else bounded(), "wage"),
    ):
        company.save(fact, subject)
    company.confirm_payroll("wage")
    company.publish("wage")
    add_source_text(company.engine, "wage")
    if closed:
        company.close("2026-01")
    before = frozen_rows(company.engine)
    dashboard = Dashboard(company.engine)
    members = activity_members(company.engine, "2026-01")
    assert len(members) == 1
    assert members[0]["description"] == "计提工资（2026-01）"
    assert_no_source_text(members)
    vouchers = dashboard.brief("2026-01", section="vouchers")
    validate_response("dashboard_brief", vouchers)
    assert_no_source_text(vouchers)
    card = vouchers["data"]["collections"]["vouchers"]["items"][0]
    assert card["list_summary"] == "计提工资"
    assert "2026-01" in card["summary"]
    for group in dashboard.brief("2026-01")["data"]["collections"]["open_items"]["items"]:
        result = dashboard.brief_group(
            "2026-01", section="open_items", group_key=group["group_key"]
        )
        assert_no_source_text(result)
    status = dashboard.business_status("2026-01", "wage")
    validate_response("dashboard_business_status", status)
    assert_no_source_text(status["data"]["display_profiles"]["business"])
    assert_source_profile_retained(company.engine, "2026-01", "wage")
    assert frozen_rows(company.engine) == before


def test_opening_cross_month_social_and_housing_payments_standardize_all_owner_entries(
    opening_book,
):
    dashboard = formal_payment_samples(opening_book, separate=True)
    engine = dashboard.engine
    # The fixture already retained ordinary purpose/note; add deliberately long
    # adopted-source explanations in each of the two contribution payments.
    for subject in ("social-108", "housing"):
        Display(engine).save_display_profile(
            {
                "kind": "business",
                "entity_id": subject,
                "source": "合成核算资料",
                "display_name": CUSTOM_NAME,
                "purpose": PURPOSE,
                "note": NOTE,
            },
            expected_revision=1,
            request_id=f"replace-profile:{subject}",
        )
        Periods(engine).management(
            subject,
            payment_period=None,
            payment_category=None,
            expected_revision=0,
            request_id=f"management:{subject}",
        )
    before = frozen_rows(engine)
    members = activity_members(engine, "2026-01")
    funds = dashboard.funds("2026-01")["data"]["collections"]["movements"]["items"]
    vouchers = dashboard.brief("2026-01", section="vouchers")["data"]["collections"]["vouchers"][
        "items"
    ]
    for subject, title in (("social-108", "缴纳社保"), ("housing", "缴纳公积金")):
        member = next(row for row in members if row["subject_id"] == subject)
        assert member["title"] == title
        assert member["description"] == f"{title}（2025-12）"
        assert member["date"] == "2026-01-10"
        assert member["amount_fen"] > 0
        assert_no_source_text(member)
        movement = next(row for row in funds if row["subject_id"] == subject)
        assert_no_source_text(movement)
        card = next(row for row in vouchers if row["subject_id"] == subject)
        assert card["list_summary"] == title
        assert_no_source_text(card)
        status = dashboard.business_status("2026-01", subject)
        validate_response("dashboard_business_status", status)
        assert_no_source_text(status["data"]["display_profiles"]["business"])
        assert_source_profile_retained(engine, "2026-01", subject)
    assert frozen_rows(engine) == before


def test_actual_net_salary_payment_keeps_original_business_notes_across_owner_entries(payroll_book):
    company = payroll_book
    company.publish("january", "february")
    company.save(payment(), "net-paid")
    company.publish("net-paid")
    add_source_text(company.engine, "net-paid")
    before = frozen_rows(company.engine)
    dashboard = Dashboard(company.engine)
    member = next(
        row
        for row in activity_members(company.engine, "2026-02")
        if row["subject_id"] == "net-paid"
    )
    movement = next(
        row
        for row in dashboard.funds("2026-02")["data"]["collections"]["movements"]["items"]
        if row["subject_id"] == "net-paid"
    )
    card = next(
        row
        for row in dashboard.brief("2026-02", section="vouchers")["data"]["collections"][
            "vouchers"
        ]["items"]
        if row["subject_id"] == "net-paid"
    )
    for row in (member, movement, card):
        encoded = json.dumps(row, ensure_ascii=False)
        assert PURPOSE in encoded and NOTE in encoded
    status = dashboard.business_status("2026-02", "net-paid")
    values = status["data"]["display_profiles"]["business"]["values"]
    assert values["purpose"] == PURPOSE and values["note"] == NOTE
    assert member["amount_fen"] == 907400
    assert_source_profile_retained(company.engine, "2026-02", "net-paid")
    assert frozen_rows(company.engine) == before


@pytest.mark.parametrize(
    "component",
    ["employee_social", "employer_social", "employee_housing", "employer_housing", "net"],
)
def test_opening_contribution_details_are_standard_but_opening_net_notes_remain(
    opening_book, component
):
    engine, _, _, package, _ = opening_book
    package(
        [
            ("opening_bank", "bank-start", {"bank_account_id": "bank", "balance_fen": 50000}),
            (
                "opening_payroll_payable",
                "prior-payable",
                {
                    "employee_id": "employee",
                    "recipient_id": "employee" if component == "net" else "authority",
                    "payroll_period": "2025-12",
                    "component": component,
                    "outstanding_fen": 50000,
                },
            ),
        ]
    )
    add_source_text(engine, "prior-payable")
    before = frozen_rows(engine)
    dashboard = Dashboard(engine)
    response = dashboard.business_status("2026-01", "prior-payable")
    validate_response("dashboard_business_status", response)
    profile_values = response["data"]["display_profiles"]["business"]["values"]
    if component == "net":
        assert profile_values["purpose"] == PURPOSE and profile_values["note"] == NOTE
    else:
        assert_no_source_text(profile_values)
    group = dashboard.brief("2026-01")["data"]["collections"]["open_items"]["items"][0]
    members = dashboard.brief_group("2026-01", section="open_items", group_key=group["group_key"])[
        "data"
    ]["collections"]["members"]["items"]
    if component == "net":
        assert PURPOSE in members[0]["purpose"]
    else:
        assert_no_source_text(members)
    assert members[0]["source_amount_fen"] == 50000
    assert_source_profile_retained(engine, "2026-01", "prior-payable")
    assert frozen_rows(engine) == before


@pytest.mark.parametrize("matter", ["social", "housing"])
def test_contribution_bank_batch_keeps_actual_recipients_allocations_and_original_memo(
    opening_book, matter
):
    engine, save, publish, package, _ = opening_book
    parts = (("first", "authority-a", 100), ("second", "authority-b", 200))
    package(
        [
            ("opening_bank", "bank-start", {"bank_account_id": "bank", "balance_fen": 300}),
            *[
                (
                    "opening_payroll_payable",
                    subject,
                    {
                        "employee_id": "employee",
                        "recipient_id": recipient,
                        "payroll_period": "2025-12",
                        "component": f"employee_{matter}",
                        "outstanding_fen": amount,
                    },
                )
                for subject, recipient, amount in parts
            ],
        ]
    )
    for _, recipient, _ in parts:
        display_profile(
            engine, "counterparty", recipient, display_name="合成机构" + recipient[-1].upper()
        )
    save(
        "payment",
        "contribution-batch",
        {
            "period": "2026-01",
            "actual_date": "2026-01-10",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": None,
            "payment_method": "bank_batch",
            "amount_fen": 300,
            "allocations": [
                {
                    "source_kind": "opening_payroll_payable",
                    "source_id": subject,
                    "obligation": "primary",
                    "recipient_id": recipient,
                    "amount_fen": amount,
                }
                for subject, recipient, amount in parts
            ],
        },
    )
    publish("contribution-batch")
    add_source_text(engine, "contribution-batch")
    memo = "原始银行摘要：合成缴费批次，保持原文"
    save(
        "bank_statement",
        "statement",
        {
            "period": "2026-01",
            "bank_account_id": "bank",
            "opening_fen": 300,
            "closing_fen": 0,
            "entries": [
                {
                    "reference": "observed-batch",
                    "actual_date": "2026-01-10",
                    "signed_fen": -300,
                    "description": memo,
                }
            ],
        },
    )
    publish("statement")
    save(
        "bank_reconciliation",
        "reconciliation",
        {
            "period": "2026-01",
            "bank_account_id": "bank",
            "statement_id": "statement",
            "matches": [
                {
                    "reference": "observed-batch",
                    "source_kind": "payment",
                    "source_id": "contribution-batch",
                }
            ],
        },
    )
    publish("reconciliation")
    before = frozen_rows(engine)
    response = Dashboard(engine).funds("2026-01", section="statements", statement_account_id="bank")
    validate_response("dashboard_funds", response)
    row = response["data"]["collections"]["statements"]["items"][0]
    assert row["party"] == (
        "社保批量支付 · 2 项" if matter == "social" else "公积金批量支付 · 2 项"
    )
    assert row["memo"] == memo and row["signed_amount_fen"] == -300
    assert row["batch_payment"] == {
        "bank_row_count": 1,
        "total_fen": 300,
        "items": [
            {"party": "合成机构A", "amount_fen": 100},
            {"party": "合成机构B", "amount_fen": 200},
        ],
    }
    assert_no_source_text(row)
    assert frozen_rows(engine) == before
