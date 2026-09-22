"""Business details expose the exact wage confirmation retained by publication."""

import pytest
from test_payroll_preparation import company as payroll_company
from test_payroll_preparation import confirm_no_change

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard


def test_dashboard_current_and_frozen_wage_details_retain_monthly_plan_evidence(tmp_path):
    company = payroll_company(tmp_path)
    dashboard = Dashboard(company.engine)

    current_response = dashboard.business_status("2026-01", "january", as_of="2026-02-01")
    assert current_response["schema_version"] == 3
    current = current_response["data"]
    source = current["current_business_result"]["payroll_confirmation"]

    assert source["mode"] == "monthly_plan"
    assert source["confirmation_kind"] in {"payroll_plan_v2", "payroll_plan_bounded"}
    assert source["confirmation_fact_id"]
    assert source["confirmation_subject_id"]
    assert source["confirmation_revision"] == 1
    assert len(source["evidence"]) == 1

    company.close("2026-01")
    closed = dashboard.business_status("2026-01", "january", as_of="2026-02-01")["data"]

    assert closed["frozen_adoption"]["payroll_confirmation"] == source
    assert (
        closed["frozen_adoption"]["calculation_id"]
        == closed["current_business_result"]["calculation"]["id"]
    )


def test_current_wage_details_retain_explicit_no_change_evidence(tmp_path):
    company = payroll_company(tmp_path)
    service = confirm_no_change(company)
    plan = service.prepare("2026-02")
    saved = service.confirm("2026-02", preview_digest=plan["digest"], request_id=company.request())
    subject_id = saved["results"][0]["subject_id"]
    company.publish(subject_id)

    result = BusinessQueries(company.engine).business_status(
        subject_id, "2026-02", as_of="2026-03-01"
    )
    source = result["current_business_result"]["payroll_confirmation"]

    assert source["mode"] == "explicit_no_change"
    assert source["confirmation_kind"] == "payroll_no_change_v2"
    assert source["confirmation_revision"] == 1
    assert len(source["evidence"]) == 1


def test_wage_details_do_not_replace_confirmation_with_current_fact_revision(tmp_path):
    company = payroll_company(tmp_path)
    queries = BusinessQueries(company.engine)
    original = queries.business_status("january", "2026-01", as_of="2026-02-01")[
        "current_business_result"
    ]["payroll_confirmation"]
    with company.engine.store.connection(read_only=True) as connection:
        exact = company.engine.store.fact(connection, original["confirmation_fact_id"])

    revised = company.save(exact.fact, exact.subject_id, revision=exact.revision)
    assert revised["fact_id"] != original["confirmation_fact_id"]

    current = queries.business_status("january", "2026-01", as_of="2026-02-01")
    assert current["current_business_result"]["payroll_confirmation"] == original


def test_missing_published_wage_confirmation_is_content_integrity_failure(tmp_path):
    queries = BusinessQueries(payroll_company(tmp_path).engine)

    with pytest.raises(KernelError) as failure:
        queries._payroll_confirmation_source(
            None,
            {
                "id": "calculation-without-confirmation",
                "kind": "payroll",
                "outcome": {"values": {}},
            },
        )

    assert failure.value.code == "content_integrity_failed"
    assert failure.value.details["component"] == "business_status"
