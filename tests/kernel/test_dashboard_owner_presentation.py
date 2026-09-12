"""Owner-facing summaries and money roles retain their native SQLite evidence."""

import pytest
import test_banking as banking
import test_payroll_corrections as payroll_corrections
import test_platforms as platforms
import test_reimbursement_assets as reimbursement_assets
from test_opening_continuation import book as _opening_book
from test_payroll import payroll
from test_payroll_corrections import payment
from test_platforms import funding_data, payment_data, transfer_data
from test_reimbursement_assets import accepted_batch, batch_card

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display

bank_book = banking.book
payroll_book = payroll_corrections.company
platform_book = platforms.book
asset_book = reimbursement_assets.book
opening_book = _opening_book


def display_profile(engine, kind, entity_id, **fields):
    return Display(engine).save_display_profile(
        {"kind": kind, "entity_id": entity_id, "source": "合成展示资料", **fields},
        expected_revision=0,
        request_id=f"display-{kind}-{entity_id}",
    )


def voucher(engine, period, subject):
    return next(
        row
        for row in Dashboard(engine).brief(period)["data"]["vouchers"]
        if row["components"][0]["id"] == subject and not row["reverses_version_id"]
    )


def expense_fields(party, *, amount=1000, period="2026-09"):
    return {
        "period": period,
        "counterparty_id": party,
        "amount_fen": amount,
        "expense_class": "administration",
        "creditor_kind": "supplier",
    }


def test_business_short_title_keeps_supplied_purpose_and_party_in_full_summary(bank_book):
    engine, save, publish, _ = bank_book
    save("expense", "office", expense_fields("supplier"))
    publish("office")
    display_profile(engine, "counterparty", "supplier", display_name="甲办公用品店")
    display_profile(
        engine,
        "business",
        "office",
        display_name="办公用品采购",
        purpose="供研发办公室日常使用",
        note="已核对本次采购清单",
    )
    before = engine.ledger("2026-09")
    data = Dashboard(engine).brief("2026-09")["data"]
    row = data["vouchers"][0]
    assert row["list_summary"] == "办公用品采购"
    assert row["display_summary"] == (
        "办公用品采购（2026-09） · 甲办公用品店；供研发办公室日常使用；已核对本次采购清单"
    )
    activity = data["activity_groups"][0]["rows"][0]
    assert activity["title"] == row["list_summary"]
    assert activity["display_description"] == row["display_summary"]
    assert row["business_amount_fen"] == 1000
    assert engine.ledger("2026-09") == before


def test_next_month_wage_payment_summary_names_employee_and_source_month(payroll_book):
    company = payroll_book
    company.publish("january", "february")
    display_profile(company.engine, "employee", "employee", display_name="甲员工")
    company.save(payment(), "salary-paid")
    company.publish("salary-paid")
    display_profile(company.engine, "business", "salary-paid", purpose="补发一月份工资")
    row = voucher(company.engine, "2026-02", "salary-paid")
    assert row["list_summary"] == "支付工资奖金"
    assert row["display_summary"] == "支付工资奖金（2026-01） · 甲员工；补发一月份工资"
    assert row["date"] == "2026-02-10"
    assert row["fund_outflow_fen"] == 907400
    assert row["lines"][0]["source_label"] == "2026-01 · 工资计提 · 甲员工"
    assert row["lines"][0]["party"] == "甲员工"


@pytest.mark.parametrize(
    "components, title",
    [
        (("net",), "支付工资奖金"),
        (("employee_social", "employer_social"), "支付社保"),
        (("employee_housing", "employer_housing"), "支付公积金"),
        (("withheld_tax",), "付款"),
    ],
)
def test_opening_payroll_payment_names_original_wage_month_and_component(
    opening_book, components, title
):
    engine, save, publish, package, _ = opening_book
    recipient = "employee" if components == ("net",) else "authority"
    package(
        [
            (
                "opening_bank",
                "bank-opening",
                {"bank_account_id": "bank", "balance_fen": 50000 * len(components)},
            ),
            *[
                (
                    "opening_payroll_payable",
                    "prior-" + component,
                    {
                        "employee_id": "employee",
                        "recipient_id": recipient,
                        "payroll_period": "2025-12",
                        "component": component,
                        "outstanding_fen": 50000,
                    },
                )
                for component in components
            ],
        ],
        period="2026-01",
    )
    display_profile(engine, "employee", "employee", display_name="甲员工")
    display_profile(engine, "counterparty", "authority", display_name="实际收款机构")
    save(
        "payment",
        "prior-payroll-paid",
        {
            "period": "2026-01",
            "actual_date": "2026-01-15",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": recipient,
            "amount_fen": 15000 * len(components),
            "allocations": [
                {
                    "source_kind": "opening_payroll_payable",
                    "source_id": "prior-" + component,
                    "obligation": "primary",
                    "amount_fen": 15000,
                }
                for component in components
            ],
        },
    )
    publish("prior-payroll-paid")
    before = engine.ledger("2026-01")
    row = voucher(engine, "2026-01", "prior-payroll-paid")
    assert row["list_summary"] == title
    assert row["display_summary"].startswith(f"{title}（2025-12）")
    assert row["recognition"]["period"] == "2026-01"
    assert row["date"] == "2026-01-15"
    assert row["business_amount_fen"] == row["fund_outflow_fen"] == 15000 * len(components)
    for relation in row["settlements"]:
        assert relation["source_period"] == "2025-12"
        assert relation["source_label"] == "2025-12 · 薪酬未付明细期初 · 甲员工"
        assert relation["obligation_name"] == "primary"
        assert relation["party"] == ("甲员工" if recipient == "employee" else "实际收款机构")
        assert relation["source_period"] != row["components"][0]["recognition"]["period"]
    assert engine.ledger("2026-01") == before


def test_reversal_summary_identifies_original_payroll_without_hiding_its_sign(payroll_book):
    company = payroll_book
    display_profile(company.engine, "employee", "employee", display_name="甲员工")
    display_profile(company.engine, "business", "january", purpose="一月份员工工资")
    company.publish("january", "february")
    company.close("2026-01")
    original = voucher(company.engine, "2026-01", "january")
    company.save(payroll(accounting_gross_salary_fen=1100000), "january", revision=1)
    company.publish("january", correction_period="2026-02")
    rows = Dashboard(company.engine).brief("2026-02")["data"]["vouchers"]
    reversal = next(
        row for row in rows if row["reverses_version_id"] == original["voucher_version_id"]
    )
    replacement = voucher(company.engine, "2026-02", "january")
    assert reversal["list_summary"] == "冲正·计提工资"
    assert reversal["display_summary"] == "冲销原业务：计提工资（2026-01） · 甲员工；一月份员工工资"
    assert reversal["business_amount_fen"] == -1000000
    assert replacement["list_summary"] == "计提工资"
    assert replacement["business_amount_fen"] == 1100000
    assert "2026-01" in replacement["display_summary"]
    assert reversal["funds"] == replacement["funds"] == []


@pytest.mark.parametrize("order", [("first", "second"), ("second", "first")])
def test_equal_supplier_amounts_follow_explicit_advance_source_order(bank_book, order):
    engine, save, publish, _ = bank_book
    names = {"first": "甲供应商", "second": "乙供应商"}
    for source, name in names.items():
        save("expense", source, expense_fields(source, amount=12500))
        display_profile(engine, "counterparty", source, display_name=name)
    display_profile(engine, "counterparty", "owner", display_name="丙股东")
    publish("first", "second")
    save(
        "employee_advance",
        "paid-on-behalf",
        {
            "period": "2026-09",
            "payer_id": "owner",
            "payer_kind": "owner",
            "payment_on_behalf_confirmed": True,
            "actual_creditor_payment_date": "2026-09-10",
            "sources": [
                {
                    "source_kind": "expense",
                    "source_id": source,
                    "obligation": "primary",
                    "amount_fen": 12500,
                }
                for source in order
            ],
        },
    )
    publish("paid-on-behalf")
    row = voucher(engine, "2026-09", "paid-on-behalf")
    assert [(line["code"], line["debit_fen"], line["credit_fen"]) for line in row["lines"]] == [
        ("2202", 12500, 0),
        ("2202", 12500, 0),
        ("2241", 0, 25000),
    ]
    assert [line["party"] for line in row["lines"]] == [
        *(names[source] for source in order),
        "丙股东",
    ]
    for line, source in zip(row["lines"][:2], order, strict=True):
        assert line["parties"] == [{"id": source, "name": names[source], "amount_fen": 12500}]
        assert line["party_state"] == "known"
    assert row["funds"] == []
    assert row["fund_inflow_fen"] == row["fund_outflow_fen"] == 0


def test_aggregate_batch_credit_keeps_each_creditor_and_accepted_amount(asset_book):
    engine, save, publish = asset_book
    for ident, name in (("alice", "甲垫付人"), ("bob", "乙垫付人")):
        display_profile(engine, "employee", ident, display_name=name)
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    save("reimbursed_asset", "chair", batch_card(30000))
    publish("batch", "computer", "chair")
    row = voucher(engine, "2026-02", "batch")
    credit = next(line for line in row["lines"] if line["code"] == "224101")
    assert credit["credit_fen"] == 150000
    assert credit["party_state"] == "multiple"
    assert credit["parties"] == [
        {"id": "alice", "name": "甲垫付人", "amount_fen": 90000},
        {"id": "bob", "name": "乙垫付人", "amount_fen": 60000},
    ]
    assert credit["party"] == "甲垫付人、乙垫付人"
    assert row["funds"] == []
    assert all(line["parties"] == [] for line in row["lines"] if line["code"] != "224101")


def test_offset_changes_obligations_without_presenting_company_money(bank_book):
    engine, save, publish, _ = bank_book
    save("expense", "office", expense_fields("supplier", amount=15000))
    save(
        "advance",
        "prepayment",
        {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": 15000,
            "side": "supplier",
            "contractual_obligation_established": True,
        },
    )
    save(
        "settlement",
        "offset",
        {
            "period": "2026-09",
            "settlement_kind": "advance_application",
            "offset_right_confirmed": True,
            "first": {
                "source_kind": "advance",
                "source_id": "prepayment",
                "obligation": "advance",
                "amount_fen": 15000,
            },
            "second": {
                "source_kind": "expense",
                "source_id": "office",
                "obligation": "primary",
                "amount_fen": 15000,
            },
        },
    )
    publish("office", "prepayment", "offset")
    data = Dashboard(engine).brief("2026-09")["data"]
    row = next(row for row in data["vouchers"] if row["kind"] == "settlement")
    assert row["list_summary"] == "款项抵销"
    assert row["business_amount_fen"] == 15000
    assert {line["code"] for line in row["lines"]} == {"2202", "1123"}
    assert row["funds"] == []
    assert row["fund_inflow_fen"] == row["fund_outflow_fen"] == 0
    assert data["funds_overview"]["inflow_fen"] == data["funds_overview"]["outflow_fen"] == 0


def test_brief_funds_includes_cash_platform_and_excludes_internal_transfer(platform_book):
    engine, save, publish, _ = platform_book
    funding = funding_data(amount=120000)
    save(
        "funding",
        "bank-capital",
        {k: v for k, v in funding.items() if k != "platform_account_id"}
        | {"bank_account_id": "bank-a"},
    )
    save(
        "cash_funding",
        "cash-capital",
        {k: v for k, v in funding.items() if k != "platform_account_id"}
        | {"cash_account_id": "cashbox", "amount_fen": 70000},
    )
    save("platform_funding", "platform-capital", funding_data(amount=30000))
    save("bank_platform_transfer", "internal", transfer_data(amount=10000))
    save("expense", "expense", expense_fields("supplier", amount=11000))
    save("cash_payment", "cash-paid", payment_data(kind="cash_payment", amount=5000))
    save("platform_payment", "platform-paid", payment_data(amount=6000))
    publish(
        "bank-capital",
        "cash-capital",
        "platform-capital",
        "internal",
        "expense",
        "cash-paid",
        "platform-paid",
    )
    before = engine.ledger("2026-09")
    data = Dashboard(engine).brief("2026-09")["data"]
    funds = data["funds_overview"]
    assert funds == {
        "total_fen": 209000,
        "bank_fen": 130000,
        "cash_fen": 65000,
        "payment_platform_fen": 14000,
        "inflow_fen": 220000,
        "outflow_fen": 11000,
        "net_change_fen": 209000,
        "internal_transfer_fen": 10000,
    }
    # No bank statement has been provided; that does not erase confirmed cash or platform money.
    assert data["cash"]["inflow_fen"] is None
    assert data["cash"]["outflow_fen"] is None
    assert engine.ledger("2026-09") == before
