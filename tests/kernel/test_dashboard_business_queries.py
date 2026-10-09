"""Shared business meaning reaches the existing brief and employee consumers."""

import pytest
from test_payroll import profile
from test_payroll_reserve_payment import company as _wage_company
from test_payroll_reserve_payment import prepare
from test_tax_credits import company as _tax_company
from test_workflow import setup_company

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains import taxes, transactions
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.reports import ReportProfile, Reports

wage_company, tax_company = _wage_company, _tax_company


def test_refundable_tax_position_agrees_with_report_and_refund(tax_company):
    company = tax_company
    company.save(taxes.TaxCreditConfirmation, "credit", **company.credit_fields())
    company.assess("february", "2026-02", ["credit"])
    company.publish("credit", "february")
    # The brief must establish its position even without a report profile.
    dashboard = Dashboard(company.engine)
    before = dashboard.brief("2026-02")["data"]
    assert before["position"]["complete"]
    assert not any(item["key"] == "amounts" for item in before["risks"])
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
    # The returned sales consideration remains a payable; the refundable tax is an asset.
    assert balance["47"]["ending_fen"] == 101000
    assert balance["30"]["ending_fen"] == balance["53"]["ending_fen"]
    assets_before = balance["30"]["ending_fen"]
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
    after = dashboard.brief("2026-02")["data"]
    balance = Reports(company.engine).report(2026, 1)["statements"]["balance_sheet"]
    assert balance["14"]["ending_fen"] == 0
    assert assets_before == balance["30"]["ending_fen"]
    assert balance["30"]["ending_fen"] == balance["53"]["ending_fen"]
    assert after["funds_overview"]["bank_fen"] == before["funds_overview"]["bank_fen"] + 1060


@pytest.mark.parametrize("reserve", [False, True])
def test_equal_wage_batch_keeps_each_recipient_and_exact_source(wage_company, reserve):
    company = wage_company
    for person in ("one", "two"):
        Entities(company.engine).update_entity_profile(
            person,
            {"display_name": "合成人员" + person},
            source="合成人员身份资料",
            expected_revision=1,
            request_id=company.request(),
        )
    if reserve:
        prepare(company)
        company.publish("gross-batch")
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
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-02", employee_filter="all")
    employees = response["data"]["collections"]["employees"]["items"]
    for employee in employees:
        person = employee["employee_id"]
        focused = dashboard.employees(
            "2026-02",
            section="employees",
            employee_id=person,
            expected_version=response["snapshot_version"],
            employee_filter="all",
        )["data"]["collections"]["employees"]["items"]
        assert focused == [employee]
        collection = dashboard.business_status(
            "2026-02",
            "wage-" + person,
            section="settlement_events",
            expected_version=response["snapshot_version"],
        )["data"]["collections"]["settlement_events"]
        movement = next(item for item in collection["items"] if item["subject_id"] == payment_id)
        assert movement["source_subject_id"] == "wage-" + person
        assert movement["signed_amount_fen"] == company.current("wage-" + person).values["net_fen"]
        assert (
            employee["direct_net_payments_fen"]
            == company.current("wage-" + person).values["net_fen"]
        )
        assert employee["outstanding_net_fen"] == 0
        core = BusinessQueries(company.engine).business_status("wage-" + person, "2026-02")
        exact = next(
            item
            for item in core["settlements"]["movements"]
            if item["settlement_business"]["subject_id"] == payment_id
        )
        assert exact["recipient_id"] == person
        assert exact["source_business"]["subject_id"] == "wage-" + person
        assert (
            exact["source_calculation_id"] == core["current_business_result"]["calculation"]["id"]
        )
        assert exact["obligation_key"] == "payroll:wage-" + person + ":net"
        assert employee["name"] == "合成人员" + person
        identity = next(
            item
            for item in Entities(company.engine).find_entities(query=person, kind="person")["items"]
            if item["entity_id"] == person
        )
        assert identity["profile"]["display_name"] == employee["name"]
        assert identity["profile"]["source"] == "合成人员身份资料"
    earlier_response = dashboard.employees("2026-01", employee_filter="all")
    earlier = earlier_response["data"]["collections"]["employees"]["items"]
    for employee in earlier:
        focused = dashboard.employees(
            "2026-01",
            section="employees",
            employee_id=employee["employee_id"],
            expected_version=earlier_response["snapshot_version"],
            employee_filter="all",
        )["data"]["collections"]["employees"]["items"]
        assert focused == [employee]
        assert employee["direct_net_payments_fen"] == 0
        movements = dashboard.business_status(
            "2026-01",
            "wage-" + employee["employee_id"],
            section="settlement_events",
            expected_version=earlier_response["snapshot_version"],
        )["data"]["collections"]["settlement_events"]["items"]
        assert {(item["subject_id"], item["posting_period"]) for item in movements} == {
            (payment_id, "2026-02")
        }


def test_complete_materials_do_not_hide_missing_payroll_in_brief(tmp_path):
    company = setup_company(tmp_path)
    company.save(profile(effective_to="2026-01"), "profile")
    # The helper establishes material inventories before asking for a close preview.
    with pytest.raises(KernelError) as error:
        company.close("2026-01")
    assert any(issue["field"] == "missing_payroll" for issue in error.value.details["fact_issues"])
    dashboard = Dashboard(company.engine)
    response = dashboard.brief("2026-01")
    context = response["read_context"]
    assert {"material_completeness", "validation", "period_preparation"}.isdisjoint(
        response["data"]
    )
    checks = dashboard.period_preparation(
        "2026-01", expected_read_version=context["read_version"], as_of=context["as_of"]
    )["data"]["brief_checks"]
    assert checks["material_completeness"]["satisfied"]
    assert any(issue["field"] == "missing_payroll" for issue in checks["issues"])
    assert any(
        item["key"] == "accounting" and item["state"] == "pending" for item in checks["items"]
    )
