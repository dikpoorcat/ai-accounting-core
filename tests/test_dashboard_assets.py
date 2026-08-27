from __future__ import annotations

from datetime import date

from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.dashboard_assets import build_assets_data, load_assets_dashboard
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.models import (
    Account,
    AccountingPeriod,
    BusinessEvent,
    Counterparty,
    Evidence,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDepreciation,
    IntangibleAsset,
    IntangibleAssetAmortization,
    Organization,
    Voucher,
    VoucherLine,
)


def _seed_period(session: Session) -> tuple[Organization, AccountingPeriod]:
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="资产看板测试公司",
        accounting_period_control_enabled=True,
    )
    evidence = Evidence(
        org_id=organization.id,
        sha256="d" * 64,
        original_name="资产看板期间确认.txt",
        source="test",
        size_bytes=10,
        storage_path="dashboard/assets-period.txt",
    )
    session.add(evidence)
    session.flush()
    service = AccountingPeriodService(session, current_date=date(2026, 8, 17))
    result = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-02",
            idempotency_key="dashboard-assets-period-202602",
            confirmation_note="资产看板测试生成二月期间",
            evidence_references=[evidence.id],
        )
    )
    assert result.period_id is not None
    period = session.get(AccountingPeriod, result.period_id)
    assert period is not None
    return organization, period


def _account(session: Session, organization: Organization, role: str) -> Account:
    account = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == role,
        )
    )
    assert account is not None
    return account


def _add_voucher(
    session: Session,
    *,
    organization: Organization,
    number: str,
    event_type: str,
    posting_date: date,
    lines: list[tuple[Account, Counterparty | None, int, int]],
) -> tuple[BusinessEvent, Voucher]:
    business_event = BusinessEvent(
        org_id=organization.id,
        idempotency_key=f"dashboard-assets-{number}",
        event_type=event_type,
        status="posted",
        description=f"资产看板测试 {number}",
        facts={},
        business_date=posting_date,
        posting_date=posting_date,
        rule_trace=[],
    )
    session.add(business_event)
    session.flush()
    voucher = Voucher(
        org_id=organization.id,
        event_id=business_event.id,
        voucher_number=number,
        posting_date=posting_date,
        description=business_event.description,
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
                memo=business_event.description,
            )
            for index, (account, counterparty, debit_fen, credit_fen) in enumerate(
                lines, start=1
            )
        ]
    )
    session.flush()
    return business_event, voucher


def _seed_asset_portfolio(
    session: Session,
) -> tuple[Organization, AccountingPeriod, BusinessEvent]:
    organization, period = _seed_period(session)
    supplier = Counterparty(
        org_id=organization.id,
        kind="supplier",
        name="测试资产供应商",
    )
    session.add(supplier)
    session.flush()
    capital = _account(session, organization, "paid_in_capital")
    fixed_cost = _account(session, organization, "fixed_asset_cost")
    fixed_pending = _account(session, organization, "fixed_asset_pending")
    depreciation_expense = _account(
        session, organization, "management_depreciation_expense"
    )
    accumulated_depreciation = _account(
        session, organization, "accumulated_depreciation"
    )
    intangible_cost = _account(session, organization, "intangible_asset_cost")
    amortization_expense = _account(
        session, organization, "management_amortization_expense"
    )
    accumulated_amortization = _account(
        session, organization, "accumulated_amortization"
    )

    fixed_event, _ = _add_voucher(
        session,
        organization=organization,
        number="202602-0001",
        event_type="fixed_asset_acquisition",
        posting_date=date(2026, 2, 2),
        lines=[
            (fixed_cost, supplier, 1_200_000, 0),
            (capital, None, 0, 1_200_000),
        ],
    )
    fixed_asset = FixedAsset(
        org_id=organization.id,
        asset_code="FA-DASHBOARD-001",
        name="测试电脑",
        category="electronic",
        expected_use_over_one_year=True,
        acquisition_date=date(2026, 2, 2),
        posting_date=date(2026, 2, 2),
        purchase_price_fen=1_150_000,
        noncreditable_tax_fen=20_000,
        transport_and_handling_fen=10_000,
        installation_and_direct_cost_fen=20_000,
        cost_fen=1_200_000,
        supplier_id=supplier.id,
        settlement_method="payable",
        due_date=date(2026, 3, 2),
        acquisition_event_id=fixed_event.id,
        accounting_rule_version="dashboard-fixed-v1",
        accounting_rule_source_url="https://www.gov.cn/",
    )
    session.add(fixed_asset)
    session.flush()
    activation = FixedAssetActivation(
        org_id=organization.id,
        asset_id=fixed_asset.id,
        event_id=fixed_event.id,
        in_service_date=date(2026, 2, 2),
        posting_date=date(2026, 2, 2),
        useful_life_months=36,
        residual_value_fen=0,
        benefit_area="management",
        accounting_rule_version="dashboard-fixed-v1",
        accounting_rule_source_url="https://www.gov.cn/",
    )
    session.add(activation)
    session.flush()
    depreciation_event, _ = _add_voucher(
        session,
        organization=organization,
        number="202602-0002",
        event_type="fixed_asset_depreciation",
        posting_date=date(2026, 2, 28),
        lines=[
            (depreciation_expense, None, 100_000, 0),
            (accumulated_depreciation, None, 0, 100_000),
        ],
    )
    session.add(
        FixedAssetDepreciation(
            org_id=organization.id,
            asset_id=fixed_asset.id,
            activation_id=activation.id,
            event_id=depreciation_event.id,
            period_start=date(2026, 2, 1),
            posting_date=date(2026, 2, 28),
            sequence_no=1,
            amount_fen=100_000,
            accumulated_after_fen=100_000,
            calculation_hash="a" * 64,
            accounting_rule_version="dashboard-fixed-v1",
            accounting_rule_source_url="https://www.gov.cn/",
        )
    )

    intangible_event, _ = _add_voucher(
        session,
        organization=organization,
        number="202602-0003",
        event_type="intangible_asset_acquisition",
        posting_date=date(2026, 2, 3),
        lines=[
            (intangible_cost, supplier, 600_000, 0),
            (capital, None, 0, 600_000),
        ],
    )
    intangible_asset = IntangibleAsset(
        org_id=organization.id,
        asset_code="IA-DASHBOARD-001",
        name="测试软件许可",
        category="software",
        rights_description="十二个月软件使用权",
        supplier_id=supplier.id,
        acquisition_date=date(2026, 2, 3),
        available_for_use_date=date(2026, 2, 3),
        posting_date=date(2026, 2, 3),
        purchase_price_fen=550_000,
        noncreditable_tax_fen=20_000,
        directly_attributable_cost_fen=30_000,
        cost_fen=600_000,
        settlement_method="payable",
        due_date=date(2026, 3, 3),
        benefit_area="management",
        life_basis="legal_or_contractual",
        useful_life_months=12,
        life_basis_explanation="合同约定十二个月许可期",
        is_available_for_use=True,
        claims_creditable_input_vat=False,
        acquisition_event_id=intangible_event.id,
        accounting_rule_version="dashboard-intangible-v1",
        accounting_rule_source_url="https://www.gov.cn/",
    )
    session.add(intangible_asset)
    session.flush()
    amortization_event, _ = _add_voucher(
        session,
        organization=organization,
        number="202602-0004",
        event_type="intangible_asset_amortization",
        posting_date=date(2026, 2, 28),
        lines=[
            (amortization_expense, None, 50_000, 0),
            (accumulated_amortization, None, 0, 50_000),
        ],
    )
    session.add(
        IntangibleAssetAmortization(
            org_id=organization.id,
            asset_id=intangible_asset.id,
            event_id=amortization_event.id,
            period_start=date(2026, 2, 1),
            posting_date=date(2026, 2, 28),
            sequence_no=1,
            amount_fen=50_000,
            accumulated_after_fen=50_000,
            calculation_hash="b" * 64,
            accounting_rule_version="dashboard-intangible-v1",
            accounting_rule_source_url="https://www.gov.cn/",
        )
    )

    pending_event, _ = _add_voucher(
        session,
        organization=organization,
        number="202602-0005",
        event_type="fixed_asset_acquisition",
        posting_date=date(2026, 2, 20),
        lines=[
            (fixed_pending, supplier, 300_000, 0),
            (capital, None, 0, 300_000),
        ],
    )
    session.add(
        FixedAsset(
            org_id=organization.id,
            asset_code="FA-DASHBOARD-PENDING",
            name="待安装测试设备",
            category="production_equipment",
            expected_use_over_one_year=True,
            acquisition_date=date(2026, 2, 20),
            posting_date=date(2026, 2, 20),
            purchase_price_fen=300_000,
            noncreditable_tax_fen=0,
            transport_and_handling_fen=0,
            installation_and_direct_cost_fen=0,
            cost_fen=300_000,
            supplier_id=supplier.id,
            settlement_method="payable",
            due_date=date(2026, 3, 20),
            acquisition_event_id=pending_event.id,
            accounting_rule_version="dashboard-fixed-v1",
            accounting_rule_source_url="https://www.gov.cn/",
        )
    )
    session.flush()
    return organization, period, fixed_event


def test_assets_dashboard_returns_empty_without_accounting_periods() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session, session.begin():
        seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            name="无期间资产看板测试公司",
            accounting_period_control_enabled=False,
        )

    result = load_assets_dashboard(engine)

    assert result == {"schema_version": 1, "selected_period": None, "data": None}
    engine.dispose()


def test_assets_data_builds_reconciled_fixed_and_intangible_portfolio(
    session: Session,
) -> None:
    organization, period, fixed_event = _seed_asset_portfolio(session)
    later_reversal = BusinessEvent(
        org_id=organization.id,
        idempotency_key="dashboard-assets-later-reversal",
        event_type="reversal",
        status="posted",
        description="三月冲正固定资产取得",
        facts={},
        business_date=date(2026, 3, 2),
        posting_date=date(2026, 3, 2),
        rule_trace=[],
    )
    session.add(later_reversal)
    session.flush()
    fixed_event.status = "reversed"
    fixed_event.reversed_by_event_id = later_reversal.id
    fixed_voucher = session.scalar(
        select(Voucher).where(Voucher.event_id == fixed_event.id)
    )
    assert fixed_voucher is not None
    fixed_voucher.status = "reversed"
    session.flush()

    query_count = 0

    def count_query(*_args: object) -> None:
        nonlocal query_count
        query_count += 1

    bind = session.get_bind()
    assert isinstance(bind, Engine)
    event.listen(bind, "before_cursor_execute", count_query)
    try:
        assets = build_assets_data(
            session,
            organization=organization,
            period=period,
        )
    finally:
        event.remove(bind, "before_cursor_execute", count_query)

    assert query_count == 12
    assert assets["active_count"] == 2
    assert assets["registered_count"] == 3
    assert assets["ledger_cost_fen"] == 1_800_000
    assert assets["ledger_accumulated_fen"] == 150_000
    assert assets["ledger_net_fen"] == 1_650_000
    assert assets["fixed_asset_net_fen"] == 1_100_000
    assert assets["intangible_asset_net_fen"] == 550_000
    assert assets["month_charge_fen"] == 150_000
    assert assets["month_acquired_count"] == 3
    assert assets["month_acquired_fen"] == 2_100_000
    assert assets["month_activated_count"] == 1
    assert assets["pending_fixed_count"] == 1
    assert assets["pending_fixed_cost_fen"] == 300_000
    assert assets["reconciled"] is True
    fixed_items = {item["code"]: item for item in assets["fixed"]["items"]}
    assert fixed_items["FA-DASHBOARD-001"]["category_label"] == "电子设备"
    assert fixed_items["FA-DASHBOARD-001"]["month_charge_fen"] == 100_000
    assert fixed_items["FA-DASHBOARD-001"]["acquisition_reference"] == "202602-0001"
    assert fixed_items["FA-DASHBOARD-PENDING"]["status"] == "pending_activation"
    assert fixed_items["FA-DASHBOARD-PENDING"]["book_value_fen"] == 300_000
    intangible = assets["intangible"]["items"][0]
    assert intangible["rights_description"] == "十二个月软件使用权"
    assert intangible["month_charge_fen"] == 50_000


def test_assets_data_excludes_acquisition_reversed_within_selected_period(
    session: Session,
) -> None:
    organization, period, _fixed_event = _seed_asset_portfolio(session)
    supplier = session.scalar(
        select(Counterparty).where(
            Counterparty.org_id == organization.id,
            Counterparty.kind == "supplier",
        )
    )
    assert supplier is not None
    pending = _account(session, organization, "fixed_asset_pending")
    capital = _account(session, organization, "paid_in_capital")
    acquisition, voucher = _add_voucher(
        session,
        organization=organization,
        number="202602-0006",
        event_type="fixed_asset_acquisition",
        posting_date=date(2026, 2, 21),
        lines=[(pending, supplier, 90_000, 0), (capital, None, 0, 90_000)],
    )
    session.add(
        FixedAsset(
            org_id=organization.id,
            asset_code="FA-DASHBOARD-REVERSED",
            name="月内已冲正设备",
            category="electronic",
            expected_use_over_one_year=True,
            acquisition_date=date(2026, 2, 21),
            posting_date=date(2026, 2, 21),
            purchase_price_fen=90_000,
            noncreditable_tax_fen=0,
            transport_and_handling_fen=0,
            installation_and_direct_cost_fen=0,
            cost_fen=90_000,
            supplier_id=supplier.id,
            settlement_method="payable",
            due_date=date(2026, 3, 21),
            acquisition_event_id=acquisition.id,
            accounting_rule_version="dashboard-fixed-v1",
            accounting_rule_source_url="https://www.gov.cn/",
        )
    )
    reversal = BusinessEvent(
        org_id=organization.id,
        idempotency_key="dashboard-assets-same-month-reversal",
        event_type="reversal",
        status="posted",
        description="月内冲正待启用资产",
        facts={},
        business_date=date(2026, 2, 25),
        posting_date=date(2026, 2, 25),
        rule_trace=[],
    )
    session.add(reversal)
    session.flush()
    acquisition.status = "reversed"
    acquisition.reversed_by_event_id = reversal.id
    voucher.status = "reversed"
    session.flush()

    assets = build_assets_data(session, organization=organization, period=period)

    assert assets["registered_count"] == 3
    assert {item["code"] for item in assets["fixed"]["items"]} == {
        "FA-DASHBOARD-001",
        "FA-DASHBOARD-PENDING",
    }
