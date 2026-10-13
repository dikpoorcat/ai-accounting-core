"""Labor presentation reads current tax sources and exact historical contracts."""

from copy import deepcopy

import pytest
from test_confirmed_remuneration import context, tax_basis, version
from test_labor_accrual import accrual
from test_labor_assets import cost
from test_payroll import labor
from test_payroll_corrections import Company
from test_workforce_monthly_roster import worker

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_async import _produce
from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
from ai_accounting.kernel.domains.payroll import LaborRemuneration, calculate_labor
from ai_accounting.kernel.labor_result_presentation import labor_withholding_presentation
from ai_accounting.kernel.payroll_history import HistoricalLaborRemuneration
from ai_accounting.kernel.response_contracts import http_response, validate_response


def current_values():
    current = version(labor(), "labor")
    basis = tax_basis(current, 0)
    return calculate_labor(current, context(current, [basis])).values


def present(values, model=LaborRemuneration):
    return labor_withholding_presentation(
        values, model, fact_id="labor:v1", subject_id="labor", period="2026-01",
    )


@pytest.mark.parametrize("basis_type", ["final_statement", "actual_withholding"])
def test_current_saved_zero_tax_does_not_imply_payment_or_filing(basis_type):
    values = current_values()
    values["tax_basis"]["basis_type"] = basis_type
    before = deepcopy(values)
    result = present(values)
    assert result["withholding_method"] == "confirmed_tax_basis"
    assert result["withholding_label"] == {
        "final_statement": "按最终报酬表确认扣税金额",
        "actual_withholding": "按实际扣税记录确认金额",
    }[basis_type]
    assert values == before
    assert "withholding_method" not in values
    assert values["tax_basis"]["cash_payment_recorded"] is False


@pytest.mark.parametrize("field,value", [
    ("confirmed", False), ("basis_type", "filed"), ("tax_amount_fen", 1),
    ("source_fact_id", "other:v1"), ("source_id", "other"),
    ("period", "2026-02"),
    ("cash_payment_recorded", True), ("evidence", []),
])
def test_malformed_current_basis_is_never_a_historical_fallback(field, value):
    values = current_values()
    values["tax_basis"][field] = value
    with pytest.raises(KernelError) as failed:
        present(values)
    assert failed.value.code == "content_integrity_failed"


@pytest.mark.parametrize("shape", ["missing", "hybrid", "unexpected"])
def test_current_body_requires_only_its_explicit_saved_tax_grammar(shape):
    values = current_values()
    if shape == "missing":
        values.pop("tax_basis")
    elif shape == "hybrid":
        values["withholding_method"] = "net_after_withholding"
    else:
        values["tax_basis"]["made_up_field"] = True
    with pytest.raises(KernelError) as failed:
        present(values)
    assert failed.value.code == "content_integrity_failed"


@pytest.mark.parametrize("method,label", [
    ("gross_paid_without_withholding", "已按毛额付款、未扣税"),
    ("net_after_withholding", "已确认净额及扣税义务"),
])
def test_explicit_historical_body_keeps_its_original_method(method, label):
    assert present({"withholding_method": method}, HistoricalLaborRemuneration) == {
        "withholding_method": method, "withholding_label": label,
    }
    with pytest.raises(KernelError):
        present(current_values(), HistoricalLaborRemuneration)


@pytest.mark.parametrize("closed", [False, True])
def test_nonempty_confirmed_labor_native_details_and_default_production(tmp_path, closed):
    company = Company(tmp_path / "labor-current.sqlite")
    first = worker(company.engine, "甲劳务", "start_labor", "2026-01-01")["entity_id"]
    second = worker(company.engine, "乙劳务", "start_labor", "2026-01-01")["entity_id"]
    third = worker(company.engine, "丙劳务", "start_labor", "2026-01-01")["entity_id"]
    for subject, person, gross, tax, basis_type in (
        ("labor-final", first, 1000, 0, "final_statement"),
        ("labor-actual", second, 2000, 101, "actual_withholding"),
    ):
        company.save(labor(person_id=person, fixed_fee_fen=gross), subject)
        company.tax_basis(subject, tax, basis_type=basis_type)
        company.publish(subject)
    company.save(accrual(person_id=third, gross_fee_fen=303), "labor-accrual")
    company.publish("labor-accrual")
    company.save(LaborProjectCost(**cost(period="2026-01", person_id=third, gross_fee_fen=404)),
                 "labor-capital")
    company.publish("labor-capital")
    if closed:
        company.close("2026-01")
    dashboard = Dashboard(company.engine)
    native = dashboard.brief("2026-01")
    validate_response("dashboard_brief", native)
    assert native["data"]["workforce_cost"]["personal_labor"]["total_fen"] == 3303
    note = native["data"]["workforce_cost"]["personal_labor"]["withholding_note"]
    assert "部分劳务" in note and "付款及申报状态分别核对" in note
    details = dashboard.employees("2026-01", section="labor_sources", employee_filter="all")
    validate_response("dashboard_employees", details)
    items = {item["source_id"]: item
             for item in details["data"]["collections"]["labor_sources"]["items"]}
    assert (items["labor-final"]["net_fen"], items["labor-final"]["booked_tax_fen"]) == (1000, 0)
    assert tuple(items["labor-actual"][field] for field in ("net_fen", "booked_tax_fen")) == (
        1899, 101,
    )
    for subject in ("labor-final", "labor-actual"):
        assert items[subject]["withholding_method"] == "confirmed_tax_basis"
        assert items[subject]["obligations"][0]["remaining_fen"] > 0
        assert "付款" not in items[subject]["withholding_label"]
        assert "申报" not in items[subject]["withholding_label"]
        assert "withholding_method" not in company.current(subject, "labor").values
    assert items["labor-accrual"]["withholding_method"] == "not_withheld_not_filed"
    assert items["labor-capital"]["withholding_method"] == "not_withheld_not_filed"
    assert native["data"]["workforce_cost"]["capitalized_labor_fen"] == 404
    page = dashboard.employees(
        "2026-01", section="labor_sources", employee_id=first, employee_filter="all", limit=1,
    )["data"]["collections"]["labor_sources"]["page"]
    assert tuple(page[field] for field in (
        "total_count", "filtered_count", "returned_count", "has_more", "next_cursor",
    )) == (4, 1, 1, False, None)
    first_page = dashboard.employees(
        "2026-01", section="labor_sources", employee_id=third, employee_filter="all", limit=1,
    )
    continuation = first_page["data"]["collections"]["labor_sources"]["page"]
    assert (continuation["total_count"], continuation["filtered_count"],
            continuation["returned_count"], continuation["has_more"]) == (4, 2, 1, True)
    last_page = dashboard.employees(
        "2026-01", section="labor_sources", employee_id=third, employee_filter="all", limit=1,
        cursor=continuation["next_cursor"], expected_version=first_page["snapshot_version"],
    )["data"]["collections"]["labor_sources"]
    assert (last_page["page"]["total_count"], last_page["page"]["filtered_count"],
            last_page["page"]["returned_count"], last_page["page"]["has_more"],
            last_page["page"]["next_cursor"]) == (4, 2, 1, False, None)
    assert first_page["data"]["collections"]["labor_sources"]["items"] != last_page["items"]
    wire = http_response("dashboard_employees", details)
    assert wire["schema_version"] == 13
    assert _produce(company.engine, "2026-01")["status"] == "ready"
    produced = dashboard.brief("2026-01")
    assert produced["data"]["workforce_cost"] == native["data"]["workforce_cost"]
    assert dashboard.employees("2026-01")["data"]["employees"]["labor_worker_count"] == 3
    if closed:
        company.save(labor(person_id=first, fixed_fee_fen=1100), "labor-final", revision=1)
        company.tax_basis("labor-final", 0, revision=1)
        company.publish("labor-final", posting_period="2026-02")
        assert dashboard.brief("2026-01")["data"]["workforce_cost"] == native["data"][
            "workforce_cost"
        ]
        assert dashboard.brief("2026-02")["data"]["workforce_cost"]["personal_labor"][
            "total_fen"
        ] == 100
