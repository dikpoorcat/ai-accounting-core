"""Owner-facing summaries and money roles retain their native SQLite evidence."""

import pytest
import test_banking as banking
import test_payroll_corrections as payroll_corrections
import test_platforms as platforms
import test_reimbursement_assets as reimbursement_assets
from entity_fixture import seed_entities
from test_dashboard_voucher_adapter import activity_members
from test_opening_continuation import book as _opening_book
from test_payroll import payroll
from test_payroll_corrections import payment
from test_platforms import funding_data, payment_data, transfer_data
from test_reimbursement_assets import accepted_batch, batch_card

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.entities import Entities

bank_book = banking.book
payroll_book = payroll_corrections.company
platform_book = platforms.book
asset_book = reimbursement_assets.book
opening_book = _opening_book


def display_profile(engine, kind, entity_id, **fields):
    if kind == "business":
        return Display(engine).save_display_profile(
            {"kind": kind, "entity_id": entity_id, "source": "合成展示资料", **fields},
            expected_revision=0,
            request_id=f"display-{kind}-{entity_id}",
        )
    entity_kind, account_type = {
        "employee": ("person", None),
        "counterparty": ("organization", None),
        "asset": ("asset", None),
    }[kind]
    with engine.store.connection(read_only=True) as connection:
        exists = connection.execute("SELECT 1 FROM entity WHERE id=?", (entity_id,)).fetchone()
    if exists is None:
        seed_entities(engine, ((entity_id, entity_kind, account_type),))
    evidence_digest = fields.pop("evidence_digest", None)
    return Entities(engine).update_entity_profile(
        entity_id,
        fields,
        source="合成展示资料",
        expected_revision=1,
        request_id=f"entity-profile:{kind}:{entity_id}",
        evidence_digest=evidence_digest,
    )


def voucher(engine, period, subject):
    return next(
        row
        for row in activity_members(engine, period)
        if row["subject_id"] == subject and row["state"] == "已入账"
    )


def accountant_status(engine, period, subject):
    return BusinessQueries(engine).business_status(subject, period)


def accounting_event(status, version_id):
    return next(
        item
        for item in status["as_posted"]["voucher_events"]
        if item["voucher_version_id"] == version_id
    )


def assert_company_money(status, version_id, *, inflow=0, outflow=0):
    lines = accounting_event(status, version_id)["lines"]
    money = [line for line in lines if line["account"] in {"1001", "1002", "1012"}]
    assert sum(line["debit"] for line in money) == inflow
    assert sum(line["credit"] for line in money) == outflow


def expense_fields(party, *, amount=1000, period="2026-09"):
    return {
        "period": period,
        "counterparty_id": party,
        "amount_fen": amount,
        "expense_class": "administration",
        "creditor_kind": "supplier",
    }


def test_activity_omits_repeated_party_and_keeps_purpose_and_full_voucher_summary(bank_book):
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
    row = activity_members(engine, "2026-09")[0]
    assert "vouchers" not in data["collections"]
    assert row["title"] == "管理费用确认"
    assert row["description"] == (
        "办公用品采购（2026-09）；供研发办公室日常使用；已核对本次采购清单"
    )
    assert row["party"] == "甲办公用品店"
    full = Dashboard(engine).brief("2026-09", section="vouchers")
    assert full["data"]["collections"]["vouchers"]["items"][0]["summary"] == (
        "办公用品采购（2026-09） · 甲办公用品店；供研发办公室日常使用；已核对本次采购清单"
    )
    assert row["amount_fen"] == 1000
    assert engine.ledger("2026-09") == before


def test_next_month_wage_payment_summary_names_employee_and_source_month(payroll_book):
    company = payroll_book
    company.publish("january", "february")
    display_profile(company.engine, "employee", "employee", display_name="甲员工")
    company.save(payment(), "salary-paid")
    company.publish("salary-paid")
    display_profile(company.engine, "business", "salary-paid", purpose="补发一月份工资")
    row = voucher(company.engine, "2026-02", "salary-paid")
    assert row["title"] == "支付工资"
    assert row["description"] == "支付工资（2026-01）；补发一月份工资"
    assert row["date"] == "2026-02-10"
    assert row["amount_fen"] == 907400
    assert row["party"] == "甲员工"
    status = accountant_status(company.engine, "2026-02", "salary-paid")
    assert_company_money(status, row["voucher_version_id"], outflow=907400)
    movement = status["settlements"]["movements"][0]
    assert movement["recipient_id"] == "employee"
    source = accountant_status(company.engine, "2026-02", movement["source_business"]["subject_id"])
    assert source["current_business_result"]["calculation"]["period"] == "2026-01"


@pytest.mark.parametrize(
    "components, title",
    [
        (("net",), "支付工资"),
        (("employee_social", "employer_social"), "缴纳社保"),
        (("employee_housing", "employer_housing"), "缴纳公积金"),
        (("withheld_tax",), "缴纳个税"),
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
    assert row["title"] == title
    assert row["description"].startswith(f"{title}（2025-12）")
    assert row["recognition"]["period"] == "2026-01"
    assert row["date"] == "2026-01-15"
    assert row["amount_fen"] == 15000 * len(components)
    assert row["party"] == ("甲员工" if recipient == "employee" else "实际收款机构")
    status = accountant_status(engine, "2026-01", "prior-payroll-paid")
    assert_company_money(status, row["voucher_version_id"], outflow=15000 * len(components))
    assert len(status["settlements"]["movements"]) == len(components)
    for relation in status["settlements"]["movements"]:
        assert relation["obligation_name"] == "primary"
        assert relation["recipient_id"] == recipient
        source = accountant_status(engine, "2026-01", relation["source_business"]["subject_id"])
        data = source["current_business_result"]["calculation"]["fact_data"]
        assert data["employee_id"] == "employee"
        assert data["payroll_period"] == "2025-12" != row["recognition"]["period"]
    assert engine.ledger("2026-01") == before


def test_reversal_summary_identifies_original_payroll_without_hiding_its_sign(payroll_book):
    company = payroll_book
    display_profile(company.engine, "employee", "employee", display_name="甲员工")
    display_profile(company.engine, "business", "january", purpose="一月份员工工资")
    company.publish("january", "february")
    company.close("2026-01")
    original = voucher(company.engine, "2026-01", "january")
    company.save(payroll(accounting_gross_salary_fen=1100000), "january", revision=1)
    company.confirm_payroll("january")
    company.publish("january", posting_period="2026-02")
    rows = activity_members(company.engine, "2026-02")
    reversal = next(
        row for row in rows if row["subject_id"] == "january" and row["state"] == "更正原业务"
    )
    replacement = voucher(company.engine, "2026-02", "january")
    assert reversal["title"] == "冲正·工资计提"
    assert reversal["description"] == "冲销原业务：计提工资（2026-01）；一月份员工工资"
    assert reversal["party"] == replacement["party"] == "甲员工"
    assert reversal["amount_fen"] == -1000000
    assert replacement["title"] == "工资计提"
    assert replacement["amount_fen"] == 1100000
    assert "2026-01" in replacement["description"]
    status = accountant_status(company.engine, "2026-02", "january")
    assert (
        accounting_event(status, reversal["voucher_version_id"])["reverses_voucher_version_id"]
        == original["voucher_version_id"]
    )
    assert_company_money(status, reversal["voucher_version_id"])
    assert_company_money(status, replacement["voucher_version_id"])


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
    status = accountant_status(engine, "2026-09", "paid-on-behalf")
    lines = accounting_event(status, row["voucher_version_id"])["lines"]
    assert [(line["account"], line["debit"], line["credit"]) for line in lines] == [
        ("2202", 12500, 0),
        ("2202", 12500, 0),
        ("2241", 0, 25000),
    ]
    relations = sorted(
        (item for item in status["settlements"]["line_relations"] if item["role"] == "advance"),
        key=lambda item: item["line_no"],
    )
    for line, source in zip(relations, order, strict=True):
        assert line["creditor_id"] == source
        assert line["source_business"]["subject_id"] == source
        assert line["amount_fen"] == 12500 and line["state"] == "resolved"
    assert all(name in row["party"] for name in [*names.values(), "丙股东"])
    assert row["description"] == "个人代付（2026-09）"
    assert status["current_business_result"]["calculation"]["fact_data"]["payer_id"] == "owner"
    assert_company_money(status, row["voucher_version_id"])


def test_aggregate_batch_credit_keeps_each_creditor_and_accepted_amount(asset_book):
    engine, save, publish = asset_book
    for ident, name in (("alice", "甲垫付人"), ("bob", "乙垫付人")):
        display_profile(engine, "employee", ident, display_name=name)
    for ident, name, code in (
        ("computer", "办公电脑", "FA-001"),
        ("chair", "办公椅", "FA-002"),
    ):
        display_profile(engine, "asset", ident, display_name=name, display_number=code)
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    save("reimbursed_asset", "chair", batch_card(30000, asset_id="chair"))
    publish("batch", "computer", "chair")
    row = voucher(engine, "2026-02", "batch")
    status = accountant_status(engine, "2026-02", "batch")
    lines = accounting_event(status, row["voucher_version_id"])["lines"]
    credit = next(line for line in lines if line["account"] == "224101")
    assert credit["credit"] == 150000
    relations = [
        item
        for item in status["settlements"]["line_relations"]
        if item["line_no"] == credit["line_no"]
    ]
    assert [(item["creditor_id"], item["amount_fen"], item["state"]) for item in relations] == [
        ("alice", -90000, "resolved"),
        ("bob", -60000, "resolved"),
    ]
    assert row["party"] == "甲垫付人、乙垫付人"
    assert "甲垫付人" not in row["description"] and "乙垫付人" not in row["description"]
    assert all(
        item["creditor_id"] is None
        for item in status["settlements"]["line_relations"]
        if item["line_no"] != credit["line_no"]
    )
    assert_company_money(status, row["voucher_version_id"])
    cards = Dashboard(engine).assets("2026-02")["data"]["collections"]["assets"]["items"]
    assert [
        (item["asset_id"], item["name"], item["code"], item["cost_fen"])
        for item in sorted(cards, key=lambda item: -item["cost_fen"])
    ] == [
        ("computer", "办公电脑", "FA-001", 120000),
        ("chair", "办公椅", "FA-002", 30000),
    ]
    assert "2 张资产卡片：办公电脑（FA-001）、办公椅（FA-002）" in row["description"]


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
    row = next(row for row in activity_members(engine, "2026-09") if row["subject_id"] == "offset")
    assert row["title"] == "款项抵销"
    assert row["amount_fen"] == 15000
    status = accountant_status(engine, "2026-09", "offset")
    assert {
        line["account"] for line in accounting_event(status, row["voucher_version_id"])["lines"]
    } == {"2202", "1123"}
    assert_company_money(status, row["voucher_version_id"])
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
    bank = Dashboard(engine).funds("2026-09")["data"]["bank_statement"]
    assert bank["inflow_fen"] is None
    assert bank["outflow_fen"] is None
    assert engine.ledger("2026-09") == before
