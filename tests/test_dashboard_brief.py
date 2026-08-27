from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.dashboard_brief import _compact_voucher_summary, load_brief_dashboard
from ai_accounting.database import Base, make_engine
from ai_accounting.models import (
    Account,
    AccountingPeriod,
    AccountingPeriodAction,
    AccountingPeriodClose,
    AccountingPeriodCloseCommentary,
    BusinessEvent,
    Counterparty,
    Evidence,
    Organization,
    Voucher,
    VoucherLine,
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


@pytest.mark.parametrize(
    ("event_type", "description", "parties", "expected"),
    [
        ("payroll_accrual", "一段很长的工资计提原始说明", [], "一段很长的工资计提原始说明"),
        ("labor_remuneration_accrual", "一段很长的劳务原始说明", [], "2026年3月个人劳务"),
        (
            "fixed_asset_depreciation",
            "计提固定资产折旧 2026-03（月度汇总）",
            [],
            "2026年3月月度汇总",
        ),
        (
            "service_credit_sale",
            "确认当月服务收入并形成应收；次月到账。",
            ["测试客户"],
            "测试客户服务收入",
        ),
        (
            "refundable_deposit_paid",
            "支付可退保证金并等待后续收回。",
            ["测试供应商"],
            "测试供应商保证金",
        ),
        (
            "inventory",
            "这是无法按类型提炼但必须完整保留的第一段摘要；第二段说明。",
            [],
            "这是无法按类型提炼但必须完整保留的第一段摘要",
        ),
    ],
)
def test_compact_voucher_summary_is_short_and_never_hard_truncated(
    event_type: str,
    description: str,
    parties: list[str],
    expected: str,
) -> None:
    event = BusinessEvent(event_type=event_type, business_date=date(2026, 3, 31), facts={})

    result = _compact_voucher_summary(event=event, description=description, parties=parties)

    assert result == expected
    assert "…" not in result


def _add_owner_contribution(
    session: Session,
    *,
    organization: Organization,
    evidence: Evidence,
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
    assert voucher["list_summary"] == "测试负责人投入实收资本"
    assert voucher["evidence"] == ["股东投入确认.txt"]
    assert voucher["balanced"] is True
    assert [line["debit_fen"] for line in voucher["lines"]] == [10_000, 0]
    assert [line["credit_fen"] for line in voucher["lines"]] == [0, 10_000]

    assert len(month["activity_groups"]) == 1
    activity = month["activity_groups"][0]
    assert activity["key"] == "financing_owner"
    assert activity["event_count"] == 1
    assert activity["type_counts"] == [{"label": "股东投入", "count": 1}]
    assert activity["rows"][0]["subject"] == "测试负责人投入实收资本"
    assert activity["rows"][0]["description"] == "测试负责人投入启动资金"

    validation = month["validation"]
    assert validation["state"] == "attention"
    assert validation["integrity_valid"] is True
    assert validation["attention_count"] == 1
    validation_items = {item["key"]: item for item in validation["items"]}
    assert validation_items["voucher_balance"]["state"] == "pass"
    assert validation_items["accounting_equation"]["state"] == "pass"
    assert validation_items["bank_match"]["state"] == "neutral"
    assert validation_items["period_status"]["state"] == "pending"

    serialized = json.dumps(result, ensure_ascii=False)
    assert re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        serialized,
    ) is None


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
