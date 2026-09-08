import uuid
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_accounting.coa import get_account_for_business_class, seed_organization
from ai_accounting.database import Base, make_engine
from ai_accounting.ledger import (
    ComponentPostingPlan,
    Entry,
    OpenItemPlan,
    SettlementPlan,
    commit_posting_plan,
)
from ai_accounting.models import (
    Account,
    BusinessEvent,
    BusinessEventComponent,
    Counterparty,
    OpenItem,
    Settlement,
    Voucher,
    VoucherLine,
)


@pytest.fixture(autouse=True)
def deterministic_business_date(monkeypatch):
    # These are internal compiler tests, independent of MCP's public schemas.
    monkeypatch.setattr("ai_accounting.ledger.china_current_date", lambda: date(2026, 9, 8))


@pytest.fixture
def ledger_session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        org = seed_organization(
            session,
            name="组件测试",
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
        )
        party = Counterparty(org_id=org.id, kind="supplier", name="测试供应商")
        session.add(party)
        session.commit()
        yield session, org, party
    engine.dispose()


def event_for(org):
    return BusinessEvent(
        org_id=org.id,
        idempotency_key=str(uuid.uuid4()),
        event_type="composite",
        status="draft",
        facts={},
        business_date=date(2026, 9, 1),
        posting_date=date(2026, 9, 1),
        description="组件测试",
    )


def expense_plan(party):
    return ComponentPostingPlan(
        key="expense",
        kind="expense",
        facts={"amount_fen": 200},
        entries=[
            Entry(account_role="general_expense", debit_fen=200),
            Entry(account_role="accounts_payable", credit_fen=200, counterparty_id=party.id),
        ],
        open_items=[
            OpenItemPlan(
                counterparty_id=party.id,
                item_type="payable",
                original_amount_fen=200,
                account_role="accounts_payable",
            )
        ],
    )


def test_component_settlement_resolves_same_posting_source_and_preserves_ownership(ledger_session):
    session, org, party = ledger_session
    event = event_for(org)
    payment = ComponentPostingPlan(
        key="payment",
        kind="payable_settlement",
        facts={},
        entries=[
            Entry(account_role="accounts_payable", debit_fen=75, counterparty_id=party.id),
        ],
        settlements=[
            SettlementPlan(
                amount_fen=75,
                purpose="payable_settlement",
                expected_item_type="payable",
                counterparty_id=party.id,
                source_component_key="expense",
            )
        ],
    )
    funds = ComponentPostingPlan(
        key="cash",
        kind="funds",
        facts={},
        entries=[
            Entry(account_role="cash", credit_fen=75),
        ],
    )
    voucher = commit_posting_plan(
        session,
        event=event,
        components=[expense_plan(party), payment, funds],
        posting_date=event.posting_date,
        description="一笔费用部分付款",
    )
    session.commit()
    assert event.status == "posted"
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == event.id))
    assert (item.original_amount_fen, item.settled_amount_fen, item.status) == (200, 75, "partial")
    assert item.account_id is not None
    assert session.scalar(select(Settlement.payment_component_id)) is not None
    assert session.scalar(select(func.count()).select_from(BusinessEventComponent)) == 3
    assert session.scalar(select(func.count()).select_from(Voucher)) == 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(VoucherLine)
            .where(
                VoucherLine.voucher_id == voucher.id,
                VoucherLine.component_id.is_(None),
            )
        )
        == 0
    )


def test_effect_failure_rolls_back_every_component_and_balance(ledger_session):
    session, org, party = ledger_session
    event = event_for(org)

    def fail(_session, _event, _component):
        raise ValueError("DOMAIN_SOURCE_INVALID")

    failed = ComponentPostingPlan(key="invalid", kind="test", facts={}, entries=[], effects=[fail])
    with pytest.raises(ValueError, match="DOMAIN_SOURCE_INVALID"):
        commit_posting_plan(
            session,
            event=event,
            components=[expense_plan(party), failed],
            posting_date=event.posting_date,
            description="失败应全撤",
        )
    assert session.scalar(select(func.count()).select_from(BusinessEventComponent)) == 0
    assert session.scalar(select(func.count()).select_from(OpenItem)) == 0
    assert session.scalar(select(func.count()).select_from(Voucher)) == 0


def test_settlement_cannot_take_another_counterpartys_source(ledger_session):
    session, org, party = ledger_session
    event = event_for(org)
    wrong = ComponentPostingPlan(
        key="wrong",
        kind="payable_settlement",
        facts={},
        entries=[],
        settlements=[
            SettlementPlan(
                amount_fen=10,
                purpose="payable_settlement",
                expected_item_type="payable",
                counterparty_id=uuid.uuid4(),
                source_component_key="expense",
            )
        ],
    )
    with pytest.raises(ValueError, match="COUNTERPARTY_MISMATCH"):
        commit_posting_plan(
            session,
            event=event,
            components=[expense_plan(party), wrong],
            posting_date=event.posting_date,
            description="拒绝串户",
        )
    assert session.scalar(select(func.count()).select_from(OpenItem)) == 0


def test_multiple_details_share_business_class_without_changing_default(ledger_session):
    session, org, _party = ledger_session
    account = Account(
        org_id=org.id,
        code="560299",
        name="管理费用明细",
        category="expense",
        normal_side="debit",
        business_class="general_expense",
    )
    session.add(account)
    session.flush()
    assert get_account_for_business_class(session, org.id, "general_expense").code == "5602"
    assert (
        get_account_for_business_class(session, org.id, "general_expense", account_code="560299")
        == account
    )
    with pytest.raises(ValueError, match="BUSINESS_CLASS_MISMATCH"):
        get_account_for_business_class(session, org.id, "sales_expense", account_code="560299")


@pytest.mark.parametrize("amount", [True, 1.0, 1.5])
def test_internal_entries_also_reject_non_integer_fen(amount):
    with pytest.raises(ValueError, match="integer fen"):
        Entry(account_role="cash", debit_fen=amount).validate()
