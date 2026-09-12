"""Shared period checks keep close and workflow meanings aligned."""

import pytest
from test_close_range import ready
from test_payroll import payroll, profile
from test_workflow import obligation, setup_company

from ai_accounting.kernel import workflow
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.types import YearMonth, digest


def test_missing_required_payroll_is_accounting_incomplete_once(tmp_path):
    company = setup_company(tmp_path)
    company.save(profile(), "active-employee")
    ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-01")
    periods = Periods(company.engine)

    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        checked = periods.check_readiness(connection, "2026-01")

    payroll = [issue for issue in checked["issues"] if issue["field"] == "missing_payroll"]
    assert checked["materials"]["status"] == "ready"
    assert checked["accounting"]["status"] == "needs_information"
    assert checked["close_requirements"]["status"] == "needs_information"
    assert len(payroll) == 1
    assert payroll[0] in checked["accounting"]["issues"]
    assert payroll[0] in checked["close_requirements"]["issues"]

    with pytest.raises(KernelError) as failed:
        periods.preview_close("2026-01", owner_confirmation=company.owner_confirmation)
    assert failed.value.code == "period_not_ready"
    assert [
        issue
        for issue in failed.value.details["fact_issues"]
        if issue["field"] == "missing_payroll"
    ] == payroll


def test_range_uses_virtual_previous_close_without_changing_manifest_sources(tmp_path, monkeypatch):
    company = setup_company(tmp_path)
    ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-03")
    periods = Periods(company.engine)
    seen = []
    original = periods.check_readiness

    def capture(connection, period, previous_close):
        seen.append(previous_close)
        return original(connection, period, previous_close)

    monkeypatch.setattr(periods, "check_readiness", capture)
    preview = periods.preview_close_range(
        "2026-01", "2026-03", owner_confirmation=company.owner_confirmation
    )

    assert seen[0] is None
    for index, previous in enumerate(seen[1:], 1):
        assert previous["period"] == YearMonth(preview["manifests"][index - 1]["period"]).ordinal
        assert previous["digest"] == digest(preview["manifests"][index - 1])
        assert preview["manifests"][index]["previous_close_digest"] == previous["digest"].hex()

    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        current = original(connection, "2026-01", None)
    first = preview["manifests"][0]
    assert current["close_requirements"]["readiness"] == first["readiness"]
    assert {
        key: value for key, value in current["materials"]["coverage"].items() if key != "issues"
    } == first["material_coverage"]


def test_workflow_same_snapshot_separates_close_from_external_completion(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "unfiled")
    ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-01")
    periods = Periods(company.engine)
    service = workflow.Workflow(company.engine)

    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        checked = periods.check_readiness(connection, "2026-01")
        before = service._query(
            connection, "2026-01", as_of="2026-02-25", period_readiness=checked
        )
    assert before["accounting_closed"] is False
    assert before["obligations"][0]["status"] == "due"
    assert before["obligations"][0]["completion_status"] == "due"

    company.close("2026-01")
    after = service.query("2026-01", as_of="2026-02-25")
    assert after["accounting_closed"] is True
    assert after["obligations"][0]["status"] == "closed"
    assert after["obligations"][0]["completion_status"] == "due"


def test_workflow_order_failure_keeps_current_month_followups(tmp_path):
    company = setup_company(tmp_path)
    company.save(profile(), "profile")
    service = workflow.Workflow(company.engine)

    baseline = service.query("2026-02", as_of="2026-03-01")
    company.save(payroll(), "january")
    current = service.query("2026-02", as_of="2026-03-01")

    for index in (0, 1, 4):
        assert current["steps"][index] == baseline["steps"][index]
        assert current["steps"][index]["status"] == "needs_information"
    assert any(
        issue["field"] == "missing_payroll"
        for issue in current["steps"][1]["fact_issues"]
    )
    order_issues = [
        issue
        for issue in current["steps"][5]["fact_issues"]
        if issue.get("code") == "earlier_period_open"
    ]
    assert order_issues == [
        {
            "field": "period",
            "code": "earlier_period_open",
            "message": "须先处理并关闭前面有业务的月份",
            "period": "2026-01",
        }
    ]
    assert order_issues[0] in current["fact_issues"]

    with pytest.raises(KernelError) as failed:
        Periods(company.engine).preview_close(
            "2026-02", owner_confirmation=company.owner_confirmation
        )
    assert failed.value.code == "earlier_period_open"
    assert failed.value.details == {"period": "2026-01"}


def test_owner_evidence_still_precedes_closed_period_check(tmp_path):
    company = setup_company(tmp_path)
    company.close("2026-01")

    with pytest.raises(NeedsInformation) as failed:
        Periods(company.engine).preview_close("2026-01", owner_confirmation="00" * 32)
    assert failed.value.issues[0]["field"] == "owner_confirmation"
