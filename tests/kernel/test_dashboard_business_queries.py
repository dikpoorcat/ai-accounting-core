"""Shared business meaning reaches the existing brief and employee consumers."""

import pytest
from test_payroll import profile
from test_payroll_reserve_payment import company as _wage_company
from test_payroll_reserve_payment import prepare
from test_tax_credits import company as _tax_company
from test_workflow import setup_company

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.domains import taxes, transactions
from ai_accounting.kernel.reports import ReportProfile, Reports

wage_company, tax_company = _wage_company, _tax_company


def test_refundable_tax_position_agrees_with_report_and_refund(tax_company):
    company = tax_company
    company.save(taxes.TaxCreditConfirmation, "credit", **company.credit_fields())
    company.assess("february", "2026-02", ["credit"])
    company.publish("credit", "february")
    # The brief must establish its position even without a report profile.
    dashboard = Dashboard(company.engine)
    position = dashboard.brief("2026-02")["data"]["position"]
    assert position["complete"] and position["equation_valid"] is True
    # The returned sales consideration remains a payable; the refundable tax is an asset.
    assert position["liabilities_fen"] == 101000
    company.save(
        ReportProfile,
        "report-profile",
        period="2026-01",
        company_name="合成企业",
        accounting_standard="small_enterprise",
        bookkeeping_start="2026-01",
        newly_established_zero_opening_confirmed=True,
    )
    balance = Reports(company.engine).report(2026, 1)["statements"]["balance_sheet"]
    assert balance["14"]["ending_fen"] == 1060
    assert position["assets_fen"] == balance["30"]["ending_fen"]
    assert position["liabilities_fen"] == balance["47"]["ending_fen"]
    company.save(
        transactions.Payment,
        "refund",
        period="2026-02",
        actual_date="2026-02-25",
        direction="inflow",
        bank_account_id="bank",
        counterparty_id="authority",
        amount_fen=1060,
        allocations=[
            dict(
                source_kind="tax_assessment",
                source_id="february",
                obligation="vat_credit_credit",
                amount_fen=1000,
            ),
            dict(
                source_kind="tax_assessment",
                source_id="february",
                obligation="surtax_credit_credit",
                amount_fen=60,
            ),
        ],
    )
    company.publish("refund")
    after = dashboard.brief("2026-02")["data"]["position"]
    balance = Reports(company.engine).report(2026, 1)["statements"]["balance_sheet"]
    assert balance["14"]["ending_fen"] == 0
    assert after["assets_fen"] == position["assets_fen"] == balance["30"]["ending_fen"]
    assert after["bank_fen"] == position["bank_fen"] + 1060


@pytest.mark.parametrize("reserve", [False, True])
def test_equal_wage_batch_keeps_each_recipient_and_exact_source(wage_company, reserve):
    company = wage_company
    for person in ("one", "two"):
        Display(company.engine).save_display_profile(
            {
                "kind": "employee",
                "entity_id": person,
                "display_name": "合成人员" + person,
                "source": "合成人员身份资料",
            },
            expected_revision=0,
            request_id=company.request(),
        )
    if reserve:
        prepare(company)
        company.publish("scope", "gross-batch")
        payment_id = "gross-batch"
    else:
        allocations = [
            transactions.Allocation(
                source_kind="payroll",
                source_id="wage-" + person,
                obligation="net",
                recipient_id=person,
                amount_fen=company.current("wage-" + person).values["net_fen"],
            )
            for person in ("one", "two")
        ]
        company.save(
            transactions.Payment(
                period="2026-02",
                actual_date="2026-02-10",
                direction="outflow",
                bank_account_id="bank",
                counterparty_id="batch-provider",
                payment_method="bank_batch",
                amount_fen=sum(item.amount_fen for item in allocations),
                allocations=tuple(allocations),
            ),
            "batch",
        )
        company.publish("batch")
        payment_id = "batch"
    employees = Dashboard(company.engine).employees("2026-02")["data"]["employees"]["items"]
    for employee in employees:
        person = employee["employee_id"]
        source = next(s for s in employee["payroll_sources"] if s["source_id"] == "wage-" + person)
        movement = next(m for m in source["movements"] if m["source_id"] == payment_id)
        assert movement["party_id"] == person
        assert movement["party"] == "合成人员" + person
        assert movement["source_calculation_id"] == source["calculation_id"]
        assert movement["obligation_key"] == "payroll:wage-" + person + ":net"
        assert movement["field_sources"]["party"]
    earlier = Dashboard(company.engine).employees("2026-01")["data"]["employees"]["items"]
    assert all(not s["movements"] for e in earlier for s in e["payroll_sources"])


def test_complete_materials_do_not_hide_missing_payroll_in_brief(tmp_path):
    company = setup_company(tmp_path)
    company.save(profile(effective_to="2026-01"), "profile")
    # The helper establishes material inventories before asking for a close preview.
    with pytest.raises(KernelError) as error:
        company.close("2026-01")
    assert any(issue["field"] == "missing_payroll" for issue in error.value.details["fact_issues"])
    data = Dashboard(company.engine).brief("2026-01")["data"]
    assert data["material_completeness"]["satisfied"]
    assert data["validation"]["state"] == "attention"
    assert any(issue["field"] == "missing_payroll" for issue in data["validation"]["issues"])
    assert any(
        item["key"] == "accounting" and item["state"] == "pending"
        for item in data["validation"]["items"]
    )
