from collections import defaultdict

import pytest
from sqlalchemy import select
from test_business_components import expense, request
from test_business_components import sample_evidence as _sample_evidence_fixture

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import ComponentCashFlowAllocation, OpenItem, VoucherLine

sample_evidence = _sample_evidence_fixture


def test_asset_balance_detail_accounts_follow_the_card_through_later_calculations(
    session, organization, sample_evidence
):
    import uuid
    from datetime import date

    from test_fixed_asset_service import _acquisition_request

    from ai_accounting.component_schemas import ConfigureAccountRequest
    from ai_accounting.domain_action_schemas import FixedAssetAcquisitionFacts
    from ai_accounting.fixed_asset_service import FixedAssetService
    from ai_accounting.schemas import (
        ConfirmFixedAssetDepreciationRequest,
        PreviewFixedAssetDepreciationRequest,
    )

    service = ComponentService(session)
    for role, code in [
        ("fixed_asset_pending", "160499"),
        ("fixed_asset_cost", "160199"),
        ("accumulated_depreciation", "160299"),
    ]:
        service.configure_account(
            ConfigureAccountRequest(
                org_id=organization.id,
                idempotency_key=code,
                business_class=role,
                code=code,
                name=role + "明细",
            )
        )
    acquisition = _acquisition_request(organization, sample_evidence).model_dump(mode="json")
    facts = {
        key: value
        for key, value in acquisition.items()
        if key in FixedAssetAcquisitionFacts.model_fields
    }
    acquired = service.record(
        RecordEventRequest(
            org_id=organization.id,
            posting_date="2026-01-02",
            idempotency_key="detail-asset",
            evidence_references=[sample_evidence.id],
            components=[
                {
                    "key": "asset",
                    "kind": "fixed_asset_acquisition",
                    "business_date": "2026-01-02",
                    "facts": facts,
                    "account_selections": {"fixed_asset_pending": "160499"},
                }
            ],
        )
    )
    assert acquired.status == "posted", acquired
    asset_id = uuid.UUID(acquired.data["components"][0]["derived"]["asset_id"])
    activated = service.record(
        RecordEventRequest(
            org_id=organization.id,
            posting_date="2026-01-10",
            idempotency_key="detail-activation",
            evidence_references=[sample_evidence.id],
            components=[
                {
                    "key": "activate",
                    "kind": "fixed_asset_activation",
                    "business_date": "2026-01-10",
                    "facts": {
                        "asset_id": asset_id,
                        "useful_life_months": 13,
                        "residual_value_fen": 10000,
                        "benefit_area": "management",
                    },
                    "account_selections": {"fixed_asset_cost": "160199"},
                }
            ],
        )
    )
    assert activated.status == "posted", activated
    domain = FixedAssetService(session)
    preview_request = PreviewFixedAssetDepreciationRequest(
        org_id=organization.id,
        asset_id=asset_id,
        depreciation_period="2026-02",
        posting_date=date(2026, 2, 28),
    )
    preview = domain.preview_fixed_asset_depreciation(preview_request)
    assert preview.status == "calculated", preview
    depreciated = service.record(
        RecordEventRequest(
            org_id=organization.id,
            posting_date="2026-02-28",
            idempotency_key="detail-depreciation",
            evidence_references=[sample_evidence.id],
            components=[
                {
                    "key": "depreciate",
                    "kind": "fixed_asset_depreciation",
                    "business_date": "2026-02-28",
                    "facts": {
                        "asset_id": asset_id,
                        "depreciation_period": "2026-02",
                        "calculation_hash": preview.calculation_hash,
                    },
                    "account_selections": {"accumulated_depreciation": "160299"},
                }
            ],
        )
    )
    assert depreciated.status == "posted", depreciated
    next_request = preview_request.model_copy(
        update={"depreciation_period": "2026-03", "posting_date": date(2026, 3, 31)}
    )
    next_preview = domain.preview_fixed_asset_depreciation(next_request)
    confirmed = domain.confirm_fixed_asset_depreciation(
        ConfirmFixedAssetDepreciationRequest(
            **next_request.model_dump(),
            idempotency_key="later-detail-depreciation",
            calculation_hash=next_preview.calculation_hash,
        )
    )
    assert confirmed.status == "posted", confirmed
    balances = defaultdict(int)
    for line in session.scalars(select(VoucherLine)):
        balances[line.account.code] += line.debit_fen - line.credit_fen
    assert balances["160499"] == 0
    assert balances["160199"] == 1050000
    assert balances["160299"] == -160000
    assert balances["1602"] == balances["1601"] == balances["1604"] == 0


def test_salary_labor_and_both_withholding_taxes_share_projected_state(
    session, organization, sample_evidence
):
    from test_labor_remuneration_service import _labor_accrual, _labor_component
    from test_payroll_service import preview_and_confirm

    from ai_accounting.models import (
        Counterparty,
        LaborWithholdingTaxPaymentAllocation,
        PayrollEventLink,
    )

    _, payroll = preview_and_confirm(session, organization)
    salary_item = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == payroll.event_id, OpenItem.payable_category == "salary"
        )
    )
    labor_item = _labor_accrual(session, organization, sample_evidence, key="local-withholding")
    # Both domains must share the authoritative agency identity.
    tax_agency = Counterparty(
        org_id=organization.id,
        kind="other",
        name="法定缴费机构 税务局 [TAX-01]",
        external_ref="TAX-01",
    )
    existing_agency = session.scalar(
        select(Counterparty).where(
            Counterparty.org_id == organization.id, Counterparty.external_ref == "TAX-01"
        )
    )
    if existing_agency is None:
        session.add(tax_agency)
        session.flush()
    else:
        tax_agency = existing_agency
    common = {"business_date": "2026-03-05", "payment_date": "2026-03-05"}
    from ai_accounting.component_schemas import ConfigureAccountRequest

    configured_tax = ComponentService(session).configure_account(
        ConfigureAccountRequest(
            org_id=organization.id,
            idempotency_key="withholding-detail",
            code="222199",
            name="本次工资劳务代扣个税",
            business_class="individual_income_tax_payable",
        )
    )
    salary = {
        "key": "salary",
        "kind": "salary_settlement",
        "amount_fen": 839500,
        "account_selections": {"individual_income_tax_payable": "222199"},
        "allocations": [{"open_item_id": salary_item.id, "amount_fen": 1000000}],
        "withholding_allocations": [
            {
                "open_item_id": salary_item.id,
                "employee_social_insurance_items": {"pension": 80000},
                "employee_housing_fund_items": {"housing_fund": 70000},
                "individual_income_tax_fen": 10500,
            }
        ],
        **common,
    }
    labor = _labor_component(labor_item)
    labor.update(
        withholding_agency_code="TAX-01",
        withholding_agency_name="税务局",
        account_selections={"individual_income_tax_payable": "222199"},
    )
    salary_tax = {
        "key": "salary-tax",
        "kind": "payable_settlement",
        "counterparty": {"id": tax_agency.id},
        "allocations": [
            {
                "source_component_key": "salary",
                "source_open_item_key": "individual_income_tax.tax",
                "amount_fen": 10500,
            }
        ],
        **common,
    }
    labor_tax = {
        "key": "labor-tax",
        "kind": "labor_tax_settlement",
        "source_component_key": "labor",
        "amount_fen": 80000,
        **common,
    }
    result = ComponentService(session).record(
        request(
            organization,
            sample_evidence,
            [labor_tax, salary_tax, salary, labor],
            key="pay-withholding-together",
            amounts=[
                ("salary", 839500),
                ("labor", 420000),
                ("salary-tax", 10500),
                ("labor-tax", 80000),
            ],
        )
    )
    assert result.status == "posted", result
    tax_items = list(
        session.scalars(
            select(OpenItem).where(
                OpenItem.source_event_id == result.event_id,
                OpenItem.payable_category.in_(
                    ["individual_income_tax", "labor_individual_income_tax"]
                ),
            )
        )
    )
    assert len(tax_items) == 2 and all(item.status == "settled" for item in tax_items)
    assert {str(item.account_id) for item in tax_items} == {configured_tax["account_id"]}
    assert {item.counterparty_id for item in tax_items} == {tax_agency.id}
    assert session.scalar(select(LaborWithholdingTaxPaymentAllocation)).amount_fen == 80000
    assert set(
        session.scalars(
            select(PayrollEventLink.link_kind).where(PayrollEventLink.event_id == result.event_id)
        )
    ) == {"salary_payment", "statutory_payment"}
    from ai_accounting.schemas import ReverseEventRequest
    from ai_accounting.service import FinanceService

    reversed_result = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=result.event_id,
            posting_date="2026-03-06",
            idempotency_key="reverse-withholding-together",
            reason="整笔核对更正",
        )
    )
    assert reversed_result.status == "posted", reversed_result
    assert salary_item.status == labor_item.status == "open"
    assert all(item.status == "reversed" for item in tax_items)
    assert session.scalar(select(LaborWithholdingTaxPaymentAllocation)).reversed is True


def test_multiple_expenses_obligation_and_fee_share_one_commit(
    session, organization, sample_evidence
):
    supplier = {"kind": "supplier", "name": "组合供应商"}
    components = [
        expense("old-purchase", 700, payment_basis="supplier_credit", counterparty=supplier),
        expense("travel", 110),
        expense("marketing", 230, "sales_expense"),
        expense("fee", 9, "finance_expense", expense_nature="bank_service_fee"),
        {
            "key": "pay-supplier",
            "kind": "payable_settlement",
            "business_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "counterparty": supplier,
            "allocations": [{"source_component_key": "old-purchase", "amount_fen": 700}],
        },
    ]
    result = ComponentService(session).record(
        request(
            organization,
            sample_evidence,
            components,
            amounts=[("travel", 110), ("marketing", 230), ("fee", 9), ("pay-supplier", 700)],
        )
    )
    assert result.status == "posted", result
    lines = list(
        session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == result.voucher_id))
    )
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines) == 1749
    assert session.scalar(select(OpenItem)).status == "settled"


def test_receivable_advance_and_multiple_creditors_keep_separate_origins(
    session, organization, sample_evidence
):
    day = "2026-03-05"
    customer = {"kind": "customer", "name": "客户"}
    creditor_a, creditor_b = (
        {"kind": "supplier", "name": name} for name in ["债权人甲", "债权人乙"]
    )
    common = {"business_date": day, "payment_date": day}
    payload = RecordEventRequest(
        org_id=organization.id,
        idempotency_key="collect-many-origins",
        posting_date=day,
        evidence_references=[sample_evidence.id],
        components=[
            {
                "key": "sale",
                "kind": "service_sale",
                "amount_fen": 1000,
                "counterparty": customer,
                "recognition_basis": "credit",
                "fulfillment_date": day,
                "tax_facts": {
                    "taxable": False,
                    "invoice_type": "none",
                    "waive_exemption": False,
                    "tax_due_on_event": False,
                },
                **common,
            },
            {
                "key": "ar",
                "kind": "receivable_settlement",
                "counterparty": customer,
                "allocations": [{"source_component_key": "sale", "amount_fen": 1000}],
                **common,
            },
            {
                "key": "advance",
                "kind": "customer_advance",
                "amount_fen": 500,
                "counterparty": customer,
                "tax_facts": {"tax_due_on_event": False},
                **common,
            },
            *[
                {
                    "key": key,
                    "kind": "pass_through",
                    "amount_fen": amount,
                    "beneficiary": party,
                    "creditor": party,
                    "creditor_basis": "beneficiary",
                    "purpose": "客户委托代收",
                    **common,
                }
                for key, amount, party in [
                    ("agency-a", 200, creditor_a),
                    ("agency-b", 300, creditor_b),
                ]
            ],
        ],
        funds=[
            {
                "key": "collection",
                "account_code": "1001",
                "direction": "receipt",
                "payment_date": day,
                "amount_fen": 2000,
                "allocations": [
                    {"component_key": key, "amount_fen": amount}
                    for key, amount in [
                        ("ar", 1000),
                        ("advance", 500),
                        ("agency-a", 200),
                        ("agency-b", 300),
                    ]
                ],
            }
        ],
    )
    result = ComponentService(session).record(payload)
    assert result.status == "posted", result
    items = list(session.scalars(select(OpenItem)))
    creditors = [item for item in items if item.payable_category == "pass_through"]
    assert len({item.counterparty_id for item in creditors}) == 2
    assert sorted(item.original_amount_fen for item in creditors) == [200, 300]
    flows = defaultdict(int)
    for row in session.scalars(select(ComponentCashFlowAllocation)):
        flows[row.category] += row.amount_fen
    assert dict(flows) == {"cash_flow_1": 1500, "cash_flow_2": 500}


def test_debt_transfer_preserves_each_cash_flow_origin_for_later_reimbursement(
    session, organization, sample_evidence
):
    day = "2026-03-05"
    supplier = {"kind": "supplier", "name": "垫付供应商"}
    owner = {"kind": "owner", "name": "垫付负责人"}
    result = ComponentService(session).record(
        RecordEventRequest(
            org_id=organization.id,
            idempotency_key="transfer",
            posting_date=day,
            evidence_references=[sample_evidence.id],
            components=[
                expense(
                    "supplies",
                    100,
                    "service_cost",
                    payment_basis="supplier_credit",
                    counterparty=supplier,
                ),
                expense("office", 200, payment_basis="supplier_credit", counterparty=supplier),
                {
                    "key": "person",
                    "kind": "debt_transfer",
                    "business_date": day,
                    "payment_date": day,
                    "payer": owner,
                    "allocations": [
                        {"source_component_key": key, "amount_fen": amount}
                        for key, amount in [("supplies", 100), ("office", 200)]
                    ],
                },
            ],
        )
    )
    assert result.status == "posted", result
    items = list(session.scalars(select(OpenItem).where(OpenItem.status == "open")))
    assert {item.component_key for item in items} == {"cash_flow_3", "cash_flow_6"}
    assert session.scalar(select(ComponentCashFlowAllocation)) is None
    repayment = {
        "key": "reimburse",
        "kind": "payable_settlement",
        "business_date": day,
        "payment_date": day,
        "counterparty": owner,
        "allocations": [
            {"open_item_id": item.id, "amount_fen": item.original_amount_fen} for item in items
        ],
    }
    result = ComponentService(session).record(
        request(
            organization,
            sample_evidence,
            [repayment],
            key="reimburse",
            amounts=[("reimburse", 300)],
        )
    )
    assert result.status == "posted", result
    flows = {
        row.category: row.amount_fen for row in session.scalars(select(ComponentCashFlowAllocation))
    }
    assert flows == {"cash_flow_3": -100, "cash_flow_6": -200}


@pytest.mark.parametrize("cash_amount", [50, 51])
def test_reserve_offset_and_cash_can_share_one_obligation_with_atomic_total_cap(
    session, organization, sample_evidence, cash_amount
):
    from ai_accounting.models import BusinessEvent, Settlement

    supplier = {"kind": "supplier", "name": "Offset supplier"}
    day = {"business_date": "2026-03-05", "payment_date": "2026-03-05"}
    components = [
        expense("paid-cost", 50),
        expense("reserve", 100, payment_basis="supplier_credit", counterparty=supplier),
        {
            "key": "offset",
            "kind": "expense_reserve_settlement",
            "amount_fen": 50,
            "source": {"component_key": "paid-cost"},
            "counterparty": supplier,
            "allocations": [{"source_component_key": "reserve", "amount_fen": 50}],
            **day,
        },
        {
            "key": "cash",
            "kind": "payable_settlement",
            "counterparty": supplier,
            "allocations": [{"source_component_key": "reserve", "amount_fen": cash_amount}],
            **day,
        },
    ]
    result = ComponentService(session).record(
        request(
            organization,
            sample_evidence,
            components,
            amounts=[("paid-cost", 50), ("cash", cash_amount)],
        )
    )
    if cash_amount == 50:
        assert result.status == "posted", result
        item = session.scalar(select(OpenItem))
        assert item.status == "settled" and item.settled_amount_fen == 100
        settlements = list(session.scalars(select(Settlement)))
        assert len(settlements) == 2
        assert {s.purpose for s in settlements} == {
            "expense_reserve_settlement",
            "payable_settlement",
        }
    else:
        assert result.status == "rejected", result
        assert not list(session.scalars(select(BusinessEvent)))
        assert not list(session.scalars(select(Settlement)))
