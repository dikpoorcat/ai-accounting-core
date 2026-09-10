from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.dashboard_brief import load_brief_dashboard
from ai_accounting.database import Base, make_engine
from ai_accounting.ledger import (
    CashFlowPlan,
    ComponentPostingPlan,
    Entry,
    OpenItemPlan,
    commit_posting_plan,
)
from ai_accounting.models import (
    Account,
    AccountingPeriod,
    AccountingPeriodAction,
    AccountingPeriodClose,
    AccountingPeriodCloseCommentary,
    BusinessEvent,
    BusinessMetadataVersion,
    Counterparty,
    Evidence,
    Organization,
    Voucher,
)


@pytest.fixture
def brief_engine(tmp_path: Path) -> Iterator[Engine]:
    database_path = tmp_path / "dashboard-brief.sqlite3"
    engine = make_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_organization(session: Session) -> tuple[Organization, Evidence]:
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="经营简报测试公司",
        accounting_period_control_enabled=True,
    )
    evidence = Evidence(
        org_id=organization.id,
        sha256="d" * 64,
        original_name="股东投入确认.txt",
        source="test",
        size_bytes=10,
        storage_path="dashboard/brief-owner.txt",
    )
    session.add(evidence)
    session.flush()
    return organization, evidence


def _generate_period(
    session: Session,
    *,
    organization: Organization,
    evidence: Evidence,
    period_key: str,
) -> None:
    result = AccountingPeriodService(
        session,
        current_date=date(2026, 8, 17),
    ).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month=period_key,
            idempotency_key=f"dashboard-brief-period-{period_key}",
            confirmation_note=f"经营简报测试生成 {period_key} 期间",
            evidence_references=[evidence.id],
        )
    )
    assert result.period_id is not None


def _add_owner_contribution(
    session: Session,
    *,
    organization: Organization,
    evidence: Evidence,
    description: str = "测试负责人投入启动资金",
) -> None:
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
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="dashboard-brief-owner-contribution",
        event_type="composite",
        status="draft",
        description=description,
        facts={"components": ["capital", "funds.receipt"]},
        business_date=date(2026, 2, 9),
        posting_date=date(2026, 2, 9),
        rule_trace=[],
        evidence=[evidence],
    )
    commit_posting_plan(
        session,
        event=event,
        posting_date=date(2026, 2, 9),
        description=event.description,
        components=[
            ComponentPostingPlan(
                key="capital",
                kind="owner_funding",
                facts={
                    "key": "capital",
                    "kind": "owner_funding",
                    "amount_fen": 10_000,
                    "funding_kind": "capital",
                    "counterparty": {"kind": "owner", "name": owner.name},
                },
                derived={},
                entries=[
                    Entry(account_code=capital.code, counterparty_id=owner.id, credit_fen=10_000),
                ],
                cash_flows=[CashFlowPlan(bank.code, "cash_flow_19", 10_000)],
                rule_version="test-components",
            ),
            ComponentPostingPlan(
                key="funds.receipt",
                kind="funds",
                facts={
                    "key": "funds.receipt",
                    "kind": "funds",
                    "allocations": [{"component_key": "capital", "amount_fen": 10_000}],
                },
                derived={},
                entries=[
                    Entry(account_code=bank.code, counterparty_id=owner.id, debit_fen=10_000),
                ],
                cash_flows=[],
                rule_version="test-components",
            ),
        ],
    )
    session.flush()


def test_brief_returns_empty_state_when_organization_has_no_periods(
    brief_engine: Engine,
) -> None:
    with Session(brief_engine) as session, session.begin():
        organization, _evidence = _seed_organization(session)
        organization_id = organization.id

    result = load_brief_dashboard(brief_engine, org_id=organization_id)

    assert result == {"schema_version": 1, "selected_period": None, "data": None}


def test_brief_projects_balanced_month_and_owner_activity(brief_engine: Engine) -> None:
    with Session(brief_engine) as session, session.begin():
        organization, evidence = _seed_organization(session)
        _generate_period(
            session,
            organization=organization,
            evidence=evidence,
            period_key="2026-02",
        )
        _add_owner_contribution(
            session,
            organization=organization,
            evidence=evidence,
        )
        organization_id = organization.id

    result = load_brief_dashboard(
        brief_engine,
        period_key="2026-02",
        org_id=organization_id,
    )

    assert result["schema_version"] == 1
    assert result["selected_period"]["key"] == "2026-02"
    month = result["data"]
    assert month["voucher_count"] == 1
    assert month["line_count"] == 2
    assert month["total_debit_fen"] == 10_000
    assert month["total_credit_fen"] == 10_000
    assert month["position"]["assets_fen"] == 10_000
    assert month["position"]["capital_fen"] == 10_000
    assert month["position"]["month_result_fen"] == 0
    assert month["position"]["equation_valid"] is True
    assert month["workforce_cost"]["has_activity"] is False
    assert month["workforce_cost"]["total_fen"] == 0

    voucher = month["vouchers"][0]
    assert voucher["summary"] == "测试负责人投入启动资金"
    assert voucher["list_summary"] == "测试负责人投入启动资金"
    assert voucher["evidence"] == ["股东投入确认.txt"]
    assert voucher["balanced"] is True
    assert [(item["key"], item["kind"]) for item in voucher["components"]] == [
        ("capital", "owner_funding")
    ]
    assert [(item["key"], item["kind"]) for item in voucher["funds"]] == [
        ("funds.receipt", "funds")
    ]
    assert sorted((line["debit_fen"], line["credit_fen"]) for line in voucher["lines"]) == [
        (0, 10_000),
        (10_000, 0),
    ]

    assert len(month["activity_groups"]) == 1
    activity = month["activity_groups"][0]
    assert activity["key"] == "financing_owner"
    assert activity["event_count"] == 1
    assert activity["type_counts"] == [{"label": "股东投入或借款", "count": 1}]
    assert activity["rows"][0]["subject"] == "测试负责人投入启动资金"
    assert activity["rows"][0]["description"] == "测试负责人投入启动资金"

    validation = month["validation"]
    assert validation["state"] == "attention"
    assert validation["integrity_valid"] is True
    assert validation["attention_count"] == 1 + len(month["material_completeness"]["issues"])
    validation_items = {item["key"]: item for item in validation["items"]}
    assert validation_items["voucher_balance"]["state"] == "pass"
    assert validation_items["accounting_equation"]["state"] == "pass"
    assert validation_items["bank_match"]["state"] == "neutral"
    assert validation_items["period_status"]["state"] == "pending"


def test_foreign_summary_has_chinese_display_without_rewriting_voucher(
    brief_engine: Engine,
) -> None:
    from ai_accounting.dashboard_funds import load_funds_dashboard

    original = "Owner contributed RMB 100.00 as startup capital."
    with Session(brief_engine) as session, session.begin():
        organization, evidence = _seed_organization(session)
        _generate_period(
            session, organization=organization, evidence=evidence, period_key="2026-02"
        )
        _add_owner_contribution(
            session, organization=organization, evidence=evidence, description=original
        )
        org_id = organization.id

    data = load_brief_dashboard(brief_engine, period_key="2026-02", org_id=org_id)["data"]
    expected = "2026-02-09，股东投入或借款，金额100.00元。"
    voucher = data["vouchers"][0]
    assert voucher["summary"] == original
    assert voucher["display_summary"] == expected
    assert voucher["list_summary"] == expected.rstrip("。")
    activity = data["activity_groups"][0]["rows"][0]
    assert activity["description"] == original
    assert activity["display_description"] == expected
    assert voucher["amount_fen"] == 10_000
    assert voucher["balanced"] is True

    funds = load_funds_dashboard(brief_engine, period_key="2026-02", org_id=org_id)["data"]
    movement = funds["movements"][0]
    assert movement["summary"] == original
    assert movement["display_summary"] == expected
    with Session(brief_engine) as session:
        assert session.scalar(select(Voucher.description)) == original
        assert session.scalar(select(BusinessEvent.description)) == original


def test_brief_returns_stored_close_management_commentary(brief_engine: Engine) -> None:
    expected = "公司完成启动投入，但尚未形成经营造血能力；下一阶段应关注稳定收入。"
    with Session(brief_engine) as session, session.begin():
        organization, evidence = _seed_organization(session)
        _generate_period(
            session,
            organization=organization,
            evidence=evidence,
            period_key="2026-02",
        )
        period = session.scalar(
            select(AccountingPeriod).where(
                AccountingPeriod.org_id == organization.id,
                AccountingPeriod.calendar_year == 2026,
                AccountingPeriod.calendar_month == 2,
            )
        )
        assert period is not None
        action = AccountingPeriodAction(
            org_id=organization.id,
            action_type="period_close",
            idempotency_key="dashboard-brief-commentary-close",
            request_payload_hash="a" * 64,
            status="posted",
            input_facts={},
            missing_information=[],
            errors=[],
            confirmation_note="测试经营解读展示",
        )
        session.add(action)
        session.flush()
        confirmed_at = datetime(2026, 2, 28, tzinfo=UTC)
        close = AccountingPeriodClose(
            org_id=organization.id,
            period_id=period.id,
            action_id=action.id,
            calculation={},
            calculation_payload="{}",
            calculation_hash="b" * 64,
            rule_version="test",
            rule_effective_from=date(2026, 1, 1),
            source_urls=[],
            previous_close_hash=None,
            checker_version="test",
            confirmed_at=confirmed_at,
            voucher_count=0,
            line_count=0,
            total_debit_fen=0,
            total_credit_fen=0,
        )
        session.add(close)
        session.flush()
        session.add(
            AccountingPeriodCloseCommentary(
                org_id=organization.id,
                close_id=close.id,
                commentary=expected,
                prompt_version="period_close_management_commentary_v1",
                context_payload={"version": "test"},
                context_hash="c" * 64,
                generation_method="historical_ai_backfill",
            )
        )
        period.status = "closed"
        period.closed_at = confirmed_at
        period.close_id = close.id
        organization_id = organization.id

    result = load_brief_dashboard(
        brief_engine,
        period_key="2026-02",
        org_id=organization_id,
    )

    assert result["data"]["management_commentary"] == expected


def test_brief_defaults_to_latest_generated_period_even_when_empty(
    brief_engine: Engine,
) -> None:
    with Session(brief_engine) as session, session.begin():
        organization, evidence = _seed_organization(session)
        _generate_period(
            session,
            organization=organization,
            evidence=evidence,
            period_key="2026-02",
        )
        _add_owner_contribution(
            session,
            organization=organization,
            evidence=evidence,
        )
        _generate_period(
            session,
            organization=organization,
            evidence=evidence,
            period_key="2026-03",
        )
        organization_id = organization.id

    result = load_brief_dashboard(brief_engine, org_id=organization_id)

    assert result["selected_period"]["key"] == "2026-03"
    assert result["data"]["voucher_count"] == 0
    assert result["data"]["activity_groups"] == []
    assert result["data"]["position"]["assets_fen"] == 10_000
    assert result["data"]["position"]["capital_fen"] == 10_000


@pytest.mark.parametrize("party_source", ["ledger", "metadata_name", "metadata_id", "missing"])
def test_open_items_use_readable_names_without_merging_unnamed_items(
    brief_engine: Engine, party_source: str
) -> None:
    with Session(brief_engine) as session, session.begin():
        organization, evidence = _seed_organization(session)
        _generate_period(
            session, organization=organization, evidence=evidence, period_key="2026-02"
        )
        party = Counterparty(org_id=organization.id, kind="customer", name="测试客户")
        session.add(party)
        session.flush()
        party_id = party.id if party_source == "ledger" else None
        event = BusinessEvent(
            org_id=organization.id,
            idempotency_key="semantic-replay:" + "f" * 40,
            event_type="composite",
            status="draft",
            description="测试客户服务收入（摘要不能用来推断往来对象）",
            facts={"components": ["sale1", "sale2"]},
            business_date=date(2026, 2, 9),
            posting_date=date(2026, 2, 9),
            rule_trace=[],
            evidence=[evidence],
        )
        voucher = commit_posting_plan(
            session,
            event=event,
            posting_date=event.posting_date,
            description=event.description,
            components=[
                ComponentPostingPlan(
                    key=key,
                    kind="service_sale",
                    facts={"key": key, "kind": "service_sale", "amount_fen": amount},
                    derived={},
                    entries=[
                        Entry(
                            account_role="accounts_receivable",
                            counterparty_id=party_id,
                            debit_fen=amount,
                        ),
                        Entry(account_role="service_revenue", credit_fen=amount),
                    ],
                    cash_flows=[],
                    open_items=[
                        OpenItemPlan(
                            counterparty_id=party_id,
                            item_type="receivable",
                            original_amount_fen=amount,
                            account_role="accounts_receivable",
                        )
                    ],
                    rule_version="test-components",
                )
                for key, amount in [("sale1", 10_000), ("sale2", 20_000)]
            ],
        )
        if party_source != "missing":
            for key in ("sale1", "sale2"):
                for version in (1, 2):
                    reference = (
                        {"id": str(party.id)}
                        if party_source == "metadata_id"
                        else {"kind": "customer", "name": party.name}
                    )
                    if version == 1 or party_source == "ledger":
                        reference = {"kind": "customer", "name": "其他管理名称"}
                    session.add(
                        BusinessMetadataVersion(
                            org_id=organization.id,
                            event_id=event.id,
                            component_key=key,
                            version=version,
                            metadata_values={"counterparty": reference},
                            idempotency_key=f"metadata-{key}-{version}",
                            request_hash="a" * 64,
                        )
                    )
        organization_id = organization.id
        voucher_number = voucher.voucher_number
        original_facts = event.facts

    result = load_brief_dashboard(brief_engine, period_key="2026-02", org_id=organization_id)
    open_items = result["data"]["open_items"]
    category = next(
        item for item in open_items["categories"] if item["key"] == "customer_receivables"
    )
    expected_name = (
        f"未填写往来对象（{voucher_number}）" if party_source == "missing" else "测试客户"
    )
    assert {item["party"] for item in category["items"]} == {expected_name}
    assert {group["party"] for group in category["groups"]} == {expected_name}
    assert len({item["id"] for item in category["items"]}) == 2
    assert len(category["groups"]) == (2 if party_source == "missing" else 1)
    assert len({group["key"] for group in category["groups"]}) == len(category["groups"])
    assert sum(group["count"] for group in category["groups"]) == 2
    assert sum(group["outstanding_fen"] for group in category["groups"]) == 30_000
    assert open_items["receivable_fen"] == 30_000
    assert open_items["receivable_count"] == 2
    assert open_items["current_outstanding"]["receivable_fen"] == 30_000
    with Session(brief_engine) as session:
        stored = session.scalar(
            select(BusinessEvent).where(BusinessEvent.org_id == organization_id)
        )
        assert stored.facts == original_facts
        assert stored.description == "测试客户服务收入（摘要不能用来推断往来对象）"
