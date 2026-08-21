from __future__ import annotations

import json
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.models import (
    Account,
    BusinessEvent,
    Counterparty,
    Evidence,
    OpenItem,
    Organization,
    Settlement,
    Voucher,
    VoucherLine,
)
from ai_accounting.overview import build_overview_payload
from ai_accounting.overview_server import render_overview_document


def _seed_overview_month(session: Session) -> Organization:
    organization = seed_organization(
        session,
        name="经营概览测试公司",
        accounting_period_control_enabled=True,
    )
    evidence = Evidence(
        org_id=organization.id,
        sha256="d" * 64,
        original_name="股东投入确认.txt",
        source="test",
        size_bytes=10,
        storage_path="overview/owner.txt",
    )
    session.add(evidence)
    session.flush()
    generated = AccountingPeriodService(
        session,
        current_date=date(2026, 8, 17),
    ).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-02",
            idempotency_key="overview-period-202602",
            confirmation_note="经营概览单元测试生成二月期间",
            evidence_references=[evidence.id],
        )
    )
    assert generated.period_id is not None

    owner = Counterparty(
        org_id=organization.id,
        kind="owner",
        name="测试负责人",
    )
    session.add(owner)
    session.flush()
    bank = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "bank",
        )
    )
    capital = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "paid_in_capital",
        )
    )
    assert bank is not None and capital is not None
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="overview-owner-contribution",
        event_type="owner_contribution_received",
        status="posted",
        description="测试负责人投入启动资金",
        facts={},
        business_date=date(2026, 2, 9),
        posting_date=date(2026, 2, 9),
        rule_trace=[],
        evidence=[evidence],
    )
    session.add(event)
    session.flush()
    voucher = Voucher(
        org_id=organization.id,
        event_id=event.id,
        voucher_number="202602-0001",
        posting_date=date(2026, 2, 9),
        description=event.description,
        status="posted",
    )
    session.add(voucher)
    session.flush()
    session.add_all(
        [
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=1,
                account_id=bank.id,
                counterparty_id=owner.id,
                debit_fen=10_000,
                credit_fen=0,
                memo=event.description,
            ),
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=2,
                account_id=capital.id,
                counterparty_id=owner.id,
                debit_fen=0,
                credit_fen=10_000,
                memo=event.description,
            ),
        ]
    )
    session.flush()
    return organization


def _add_test_voucher(
    session: Session,
    *,
    organization: Organization,
    number: str,
    event_type: str,
    description: str,
    facts: dict,
    lines: list[tuple[Account, Counterparty | None, int, int]],
    posting_date: date | None = None,
) -> tuple[BusinessEvent, Voucher]:
    posting_date = posting_date or date(2026, 2, int(number[-2:]))
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="overview-" + number,
        event_type=event_type,
        status="posted",
        description=description,
        facts=facts,
        business_date=posting_date,
        posting_date=posting_date,
        rule_trace=[],
    )
    session.add(event)
    session.flush()
    voucher = Voucher(
        org_id=organization.id,
        event_id=event.id,
        voucher_number=number,
        posting_date=posting_date,
        description=description,
        status="posted",
    )
    session.add(voucher)
    session.flush()
    session.add_all(
        [
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=index,
                account_id=account.id,
                counterparty_id=counterparty.id if counterparty else None,
                debit_fen=debit_fen,
                credit_fen=credit_fen,
                memo=description,
            )
            for index, (account, counterparty, debit_fen, credit_fen) in enumerate(
                lines,
                start=1,
            )
        ]
    )
    session.flush()
    return event, voucher


def test_overview_builds_balanced_month_without_internal_ids(session: Session) -> None:
    _seed_overview_month(session)

    payload = build_overview_payload(session)

    assert payload["company"] == "经营概览测试公司"
    assert payload["default_period"] == "2026-02"
    assert len(payload["months"]) == 1
    month = payload["months"][0]
    assert month["voucher_count"] == 1
    assert month["line_count"] == 2
    assert month["total_debit_fen"] == 10_000
    assert month["total_credit_fen"] == 10_000
    assert month["position"]["assets_fen"] == 10_000
    assert month["position"]["capital_fen"] == 10_000
    assert month["position"]["equation_valid"] is True
    assert month["checks"]["balanced"] is True
    assert month["activity"]["owner_contribution"]["count"] == 1
    assert month["vouchers"][0]["evidence"] == ["股东投入确认.txt"]
    assert month["vouchers"][0]["summary"] == "测试负责人投入启动资金"
    assert month["vouchers"][0]["list_summary"] == "测试负责人投入实收资本"
    assert month["activity"]["owner_contribution"]["rows"][0]["description"] == (
        "测试负责人投入实收资本"
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    assert re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        serialized,
    ) is None


def test_overview_subtracts_accumulated_depreciation_from_assets(
    session: Session,
) -> None:
    organization = _seed_overview_month(session)
    accounts = {
        role: session.scalar(
            select(Account).where(
                Account.org_id == organization.id,
                Account.system_role == role,
            )
        )
        for role in (
            "fixed_asset_cost",
            "accumulated_depreciation",
            "management_depreciation_expense",
            "paid_in_capital",
        )
    }
    assert all(accounts.values())
    fixed_asset = accounts["fixed_asset_cost"]
    accumulated_depreciation = accounts["accumulated_depreciation"]
    depreciation_expense = accounts["management_depreciation_expense"]
    capital = accounts["paid_in_capital"]
    assert fixed_asset and accumulated_depreciation and depreciation_expense and capital

    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0002",
        event_type="fixed_asset_depreciation",
        description="固定资产成本及本月折旧测试",
        facts={},
        lines=[
            (fixed_asset, None, 500_000, 0),
            (capital, None, 0, 500_000),
            (depreciation_expense, None, 111_897, 0),
            (accumulated_depreciation, None, 0, 111_897),
        ],
    )

    month = build_overview_payload(session)["months"][0]

    assert month["position"]["fixed_asset_fen"] == 500_000
    assert month["position"]["assets_fen"] == 398_103
    assert month["position"]["cumulative_result_fen"] == -111_897
    assert month["position"]["equation_valid"] is True


def test_overview_separates_employee_payables_and_refundable_deposits(
    session: Session,
) -> None:
    organization = _seed_overview_month(session)
    accounts = {
        role: session.scalar(
            select(Account).where(
                Account.org_id == organization.id,
                Account.system_role == role,
            )
        )
        for role in (
            "bank",
            "paid_in_capital",
            "general_expense",
            "employee_payable",
            "employee_receivable",
        )
    }
    assert all(accounts.values())
    bank = accounts["bank"]
    capital = accounts["paid_in_capital"]
    expense = accounts["general_expense"]
    employee_payable = accounts["employee_payable"]
    employee_receivable = accounts["employee_receivable"]
    assert bank and capital and expense and employee_payable and employee_receivable

    employee = Counterparty(org_id=organization.id, kind="employee", name="测试员工")
    deposit_holder = Counterparty(org_id=organization.id, kind="other", name="出租方")
    deposit_supplier = Counterparty(
        org_id=organization.id,
        kind="supplier",
        name="保证金对方",
    )
    settled_supplier = Counterparty(
        org_id=organization.id,
        kind="supplier",
        name="已退回保证金对方",
    )
    other_receivable_party = Counterparty(
        org_id=organization.id,
        kind="other",
        name="其他应收对方",
    )
    other_payable_party = Counterparty(
        org_id=organization.id,
        kind="supplier",
        name="其他应付对方",
    )
    session.add_all(
        [
            employee,
            deposit_holder,
            deposit_supplier,
            settled_supplier,
            other_receivable_party,
            other_payable_party,
        ]
    )
    session.flush()

    employee_event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0002",
        event_type="employee_reimbursement",
        description="员工垫付费用",
        facts={"derived": {"reimbursement_kind": "expense"}},
        lines=[
            (expense, employee, 10_000, 0),
            (employee_payable, employee, 0, 10_000),
        ],
    )
    employee_item = OpenItem(
        org_id=organization.id,
        counterparty_id=employee.id,
        source_event_id=employee_event.id,
        item_type="payable",
        original_amount_fen=10_000,
        settled_amount_fen=10_000,
        status="settled",
    )
    session.add(employee_item)
    session.flush()
    february_payment, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0010",
        event_type="employee_reimbursement_payment",
        description="2 月部分支付员工报销款",
        facts={},
        lines=[
            (employee_payable, employee, 2_500, 0),
            (bank, employee, 0, 2_500),
        ],
    )
    march_payment, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202603-0001",
        event_type="employee_reimbursement_payment",
        description="3 月支付剩余员工报销款",
        facts={},
        posting_date=date(2026, 3, 1),
        lines=[
            (employee_payable, employee, 7_500, 0),
            (bank, employee, 0, 7_500),
        ],
    )
    session.add_all(
        [
            Settlement(
                org_id=organization.id,
                open_item_id=employee_item.id,
                payment_event_id=february_payment.id,
                amount_fen=2_500,
            ),
            Settlement(
                org_id=organization.id,
                open_item_id=employee_item.id,
                payment_event_id=march_payment.id,
                amount_fen=7_500,
            ),
        ]
    )

    deposit_event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0003",
        event_type="refundable_deposit_paid",
        description="公对公支付可退保证金",
        facts={"derived": {"refundable_deposit_paid_fen": 20_000}},
        lines=[
            (employee_receivable, deposit_supplier, 20_000, 0),
            (bank, deposit_supplier, 0, 20_000),
        ],
    )
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=deposit_supplier.id,
            source_event_id=deposit_event.id,
            item_type="receivable",
            original_amount_fen=20_000,
            settled_amount_fen=5_000,
            status="partial",
        )
    )
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0004",
        event_type="refundable_deposit_return_received",
        description="部分收回可退保证金",
        facts={"derived": {"refundable_deposit_return_fen": 5_000}},
        lines=[
            (bank, deposit_supplier, 5_000, 0),
            (employee_receivable, deposit_supplier, 0, 5_000),
        ],
    )
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0005",
        event_type="employee_reimbursement",
        description="员工垫付可退租赁押金",
        facts={"derived": {"reimbursement_kind": "refundable_deposit"}},
        lines=[
            (employee_receivable, deposit_holder, 3_000, 0),
            (employee_payable, employee, 0, 3_000),
        ],
    )

    other_receivable_event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0006",
        event_type="unclassified_receivable",
        description="其他应收事项",
        facts={},
        lines=[
            (employee_receivable, other_receivable_party, 4_000, 0),
            (capital, other_receivable_party, 0, 4_000),
        ],
    )
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=other_receivable_party.id,
            source_event_id=other_receivable_event.id,
            item_type="receivable",
            original_amount_fen=4_000,
            settled_amount_fen=0,
            status="open",
        )
    )
    other_payable_event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0007",
        event_type="unclassified_payable",
        description="其他应付事项",
        facts={},
        lines=[
            (expense, other_payable_party, 5_000, 0),
            (employee_payable, other_payable_party, 0, 5_000),
        ],
    )
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=other_payable_party.id,
            source_event_id=other_payable_event.id,
            item_type="payable",
            original_amount_fen=5_000,
            settled_amount_fen=0,
            status="open",
        )
    )

    settled_event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0008",
        event_type="refundable_deposit_paid",
        description="支付后已全额收回的保证金",
        facts={"derived": {"refundable_deposit_paid_fen": 7_000}},
        lines=[
            (employee_receivable, settled_supplier, 7_000, 0),
            (bank, settled_supplier, 0, 7_000),
        ],
    )
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=settled_supplier.id,
            source_event_id=settled_event.id,
            item_type="receivable",
            original_amount_fen=7_000,
            settled_amount_fen=7_000,
            status="settled",
        )
    )
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0009",
        event_type="refundable_deposit_return_received",
        description="全额收回可退保证金",
        facts={"derived": {"refundable_deposit_return_fen": 7_000}},
        lines=[
            (bank, settled_supplier, 7_000, 0),
            (employee_receivable, settled_supplier, 0, 7_000),
        ],
    )
    session.flush()

    month = build_overview_payload(session)["months"][0]
    employee_items = month["open_items"]["employee_payables"]
    deposits = month["open_items"]["refundable_deposit_receivables"]
    other_receivables = month["open_items"]["other_receivables"]
    other_payables = month["open_items"]["other_payables"]
    deposit_activity = month["activity"]["refundable_deposit"]

    assert (employee_items["count"], employee_items["outstanding_fen"]) == (1, 7_500)
    assert month["activity"]["employee_advance"]["paid_fen"] == 2_500
    assert month["activity"]["employee_advance"]["outstanding_fen"] == 7_500
    assert {item["party"] for item in month["activity"]["employee_advance"]["rows"]} == {
        "测试员工"
    }
    assert (deposits["count"], deposits["outstanding_fen"]) == (2, 18_000)
    assert {group["party"] for group in deposits["groups"]} == {
        "出租方",
        "保证金对方",
    }
    assert (other_receivables["count"], other_receivables["outstanding_fen"]) == (
        1,
        4_000,
    )
    assert (other_payables["count"], other_payables["outstanding_fen"]) == (1, 5_000)
    assert deposit_activity["added_count"] == 3
    assert deposit_activity["added_fen"] == 30_000
    assert deposit_activity["returned_count"] == 2
    assert deposit_activity["returned_fen"] == 12_000
    assert deposit_activity["outstanding_fen"] == 18_000
    assert "已退回保证金对方" not in {
        group["party"] for group in deposits["groups"]
    }


def test_overview_document_embeds_data_without_script_breakout(session: Session) -> None:
    _seed_overview_month(session)
    payload = build_overview_payload(session)
    payload["company"] = "</script><script>alert('x')</script>"

    document = render_overview_document(payload)

    assert document.startswith("<!doctype html>")
    assert "__OVERVIEW_DATA__" not in document
    assert "</script><script>alert" not in document
    assert "\\u003c/script>\\u003cscript>alert" in document
    assert "本月发生了什么" in document
    assert "本月一句话" in document
    assert "滚动鼠标滚轮切换月份" in document
    assert "借方合计" in document
    assert "voucher-ledger" in document
    assert "可退保证金" in document
    assert "期末往来事项" in document
    assert 'id="fixed-assets-card"' in document
    assert 'aria-label="查看期末待付员工款明细"' in document
    assert "open-item-category-employee_payables" in document
    assert 'state: month.checks.bank_unmatched === 0 ? "pass" : "pending"' in document
    assert 'text: "本月无银行流水", state: "neutral"' in document
