from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.dashboard_funds import (
    build_bank_activity,
    build_funds_data,
    load_funds_dashboard,
)
from ai_accounting.database import Base, make_engine
from ai_accounting.models import (
    Account,
    AccountingPeriod,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Counterparty,
    Evidence,
    Organization,
    Voucher,
    VoucherLine,
)


def _seed_funds_month(session: Session) -> tuple[Organization, AccountingPeriod]:
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="资金看板测试公司",
        accounting_period_control_enabled=True,
    )
    evidence = Evidence(
        org_id=organization.id,
        sha256="d" * 64,
        original_name="资金看板测试.txt",
        source="test",
        size_bytes=10,
        storage_path="dashboard/funds.txt",
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
            idempotency_key="dashboard-funds-period-202602",
            confirmation_note="资金看板测试生成二月期间",
            evidence_references=[evidence.id],
        )
    )
    assert generated.period_id is not None
    period = session.get(AccountingPeriod, generated.period_id)
    assert period is not None

    owner = Counterparty(org_id=organization.id, kind="owner", name="测试负责人")
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
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0001",
        event_type="owner_contribution_received",
        description="测试负责人投入启动资金",
        lines=[(bank, owner, 10_000, 0), (capital, owner, 0, 10_000)],
        posting_date=date(2026, 2, 9),
    )
    return organization, period


def _add_test_voucher(
    session: Session,
    *,
    organization: Organization,
    number: str,
    event_type: str,
    description: str,
    lines: list[tuple[Account, Counterparty | None, int, int]],
    posting_date: date,
) -> tuple[BusinessEvent, Voucher]:
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="dashboard-funds-" + number,
        event_type=event_type,
        status="posted",
        description=description,
        facts={},
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


def test_build_funds_data_separates_accounts_cash_and_internal_transfers(
    session: Session,
) -> None:
    organization, period = _seed_funds_month(session)
    cash = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "cash",
        )
    )
    payment_platform = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "payment_platform_funds",
        )
    )
    capital = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "paid_in_capital",
        )
    )
    expense = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "general_expense",
        )
    )
    owner = session.scalar(
        select(Counterparty).where(
            Counterparty.org_id == organization.id,
            Counterparty.kind == "owner",
        )
    )
    assert (
        cash is not None
        and payment_platform is not None
        and capital is not None
        and expense is not None
        and owner is not None
    )
    second_bank = Account(
        org_id=organization.id,
        code="100201",
        name="工商银行基本户",
        category="asset",
        normal_side="debit",
        requires_bank_reconciliation=True,
        bank_reconciliation_start_date=date(2026, 2, 1),
        bank_reconciliation_configured_at=datetime.now(UTC),
    )
    session.add(second_bank)
    session.flush()
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0002",
        event_type="owner_contribution_received",
        description="投入工商银行基本户",
        lines=[(second_bank, owner, 50_000, 0), (capital, owner, 0, 50_000)],
        posting_date=date(2026, 2, 10),
    )
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0003",
        event_type="cash_bank_transfer",
        description="从工商银行提取备用金",
        lines=[(cash, None, 10_000, 0), (second_bank, None, 0, 10_000)],
        posting_date=date(2026, 2, 11),
    )
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0005",
        event_type="payment_platform_transfer",
        description="从工商银行转入公司支付宝",
        lines=[
            (payment_platform, None, 5_000, 0),
            (second_bank, None, 0, 5_000),
        ],
        posting_date=date(2026, 2, 11),
    )
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0004",
        event_type="expense_cash",
        description="现金支付办公用品",
        lines=[(expense, None, 3_000, 0), (cash, None, 0, 3_000)],
        posting_date=date(2026, 2, 12),
    )

    funds = build_funds_data(session, organization=organization, period=period)
    accounts = {item["code"]: item for item in funds["accounts"]}

    assert funds["account_count"] == 4
    assert funds["bank_account_count"] == 2
    assert funds["cash_account_count"] == 1
    assert funds["payment_platform_account_count"] == 1
    assert funds["total_fen"] == 57_000
    assert funds["bank_fen"] == 45_000
    assert funds["cash_fen"] == 7_000
    assert funds["payment_platform_fen"] == 5_000
    assert funds["inflow_fen"] == 60_000
    assert funds["outflow_fen"] == 3_000
    assert funds["net_change_fen"] == 57_000
    assert funds["internal_transfer_fen"] == 15_000
    assert funds["movement_count"] == 7
    assert accounts["100201"]["closing_fen"] == 35_000
    assert accounts["100201"]["reconciliation"]["state"] == "pending"
    assert accounts["1001"]["closing_fen"] == 7_000
    assert accounts["1001"]["reconciliation"]["state"] == "not_applicable"
    assert accounts["1012"]["closing_fen"] == 5_000
    assert accounts["1012"]["reconciliation"]["state"] == "not_applicable"
    transfer_rows = [item for item in funds["movements"] if item["internal_transfer"]]
    assert len(transfer_rows) == 4
    assert {item["direction"] for item in transfer_rows} == {"inflow", "outflow"}


def test_build_bank_activity_uses_only_current_valid_matches(session: Session) -> None:
    organization, period = _seed_funds_month(session)
    event = session.scalar(
        select(BusinessEvent).where(
            BusinessEvent.org_id == organization.id,
            BusinessEvent.event_type == "owner_contribution_received",
        )
    )
    assert event is not None
    matched = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="a" * 64,
        booking_date=date(2026, 2, 9),
        amount_fen=10_000,
        source_sha256="b" * 64,
        matched_event_id=event.id,
    )
    stale = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="c" * 64,
        booking_date=date(2026, 2, 10),
        amount_fen=5_000,
        source_sha256="e" * 64,
        matched_event_id=event.id,
    )
    invalidator = BusinessEvent(
        org_id=organization.id,
        idempotency_key="dashboard-funds-bank-match-invalidator",
        event_type="reversal",
        status="posted",
        description="使旧银行匹配失效",
        facts={},
        business_date=date(2026, 2, 10),
        posting_date=date(2026, 2, 10),
        rule_trace=[],
    )
    session.add_all([matched, stale, invalidator])
    session.flush()
    session.add_all(
        [
            BankTransactionMatch(
                org_id=organization.id,
                bank_transaction_id=matched.id,
                event_id=event.id,
            ),
            BankTransactionMatch(
                org_id=organization.id,
                bank_transaction_id=stale.id,
                event_id=event.id,
                invalidated_by_event_id=invalidator.id,
                invalidated_at=datetime.now(UTC),
            ),
        ]
    )
    session.flush()

    activity = build_bank_activity(session, org_id=organization.id, period=period)

    assert activity["ordinary_count"] == 2
    assert activity["matched_count"] == 1
    assert activity["unmatched_count"] == 1
    assert {item["state"] for item in activity["rows"]} == {"matched", "unmatched"}
    assert activity["attention_rows"][0]["amount_fen"] == 5_000

    funds = build_funds_data(session, organization=organization, period=period)
    assert funds["total_fen"] == 10_000
    assert funds["movement_count"] == 1
    assert funds["bank_statement"]["transaction_count"] == 2
    assert funds["bank_statement"]["inflow_fen"] == 15_000


@pytest.mark.parametrize(
    ("event_type", "description", "offset_role"),
    [
        (
            "payment_platform_transfer",
            "企业支付宝余额转入网商银行",
            "payment_platform_funds",
        ),
        (
            "owner_contribution_received",
            "股东杜颖成通过企业支付宝投入的投资资金转入浙江网商银行",
            "paid_in_capital",
        ),
    ],
)
def test_bank_activity_identifies_alipay_balance_transfer_without_changing_raw_memo(
    session: Session,
    event_type: str,
    description: str,
    offset_role: str,
) -> None:
    organization, period = _seed_funds_month(session)
    bank = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "bank",
        )
    )
    offset_account = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == offset_role,
        )
    )
    assert bank is not None and offset_account is not None
    event, _voucher = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0002",
        event_type=event_type,
        description=description,
        lines=[(bank, None, 600_000, 0), (offset_account, None, 0, 600_000)],
        posting_date=date(2026, 2, 13),
    )
    transaction = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="7" * 64,
        external_id="2026021311120510400400059356651",
        booking_date=date(2026, 2, 13),
        amount_fen=600_000,
        counterparty_name=organization.name,
        memo="网商银行转入；（转入）网商银行转入",
        source_sha256="8" * 64,
    )
    session.add(transaction)
    session.flush()
    transaction.matched_event_id = event.id
    session.add(
        BankTransactionMatch(
            org_id=organization.id,
            bank_transaction_id=transaction.id,
            event_id=event.id,
        )
    )
    session.flush()

    activity = build_bank_activity(session, org_id=organization.id, period=period)
    row = next(item for item in activity["rows"] if item["amount_fen"] == 600_000)

    assert row["party"] == (
        "企业支付宝余额转入（原对方户名：资金看板测试公司）"
    )
    assert row["memo"] == "网商银行转入；（转入）网商银行转入"


def test_load_funds_dashboard_returns_selected_period_and_empty_period_state(tmp_path) -> None:
    database_path = tmp_path / "dashboard-funds.sqlite3"
    engine = make_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session, session.begin():
            organization, _period = _seed_funds_month(session)
            organization_id = organization.id

        result = load_funds_dashboard(engine, org_id=organization_id)

        assert result["schema_version"] == 1
        assert result["selected_period"]["key"] == "2026-02"
        assert result["data"]["total_fen"] == 10_000

        with Session(engine) as session, session.begin():
            empty_organization = seed_organization(
                session,
                taxpayer_identification_number="91330106MA7654321P",
                name="无期间资金看板公司",
                accounting_period_control_enabled=True,
            )
            empty_organization_id = empty_organization.id

        empty = load_funds_dashboard(engine, org_id=empty_organization_id)
        assert empty == {"schema_version": 1, "selected_period": None, "data": None}
    finally:
        engine.dispose()
