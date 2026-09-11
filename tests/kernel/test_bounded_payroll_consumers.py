"""Proven-zero wage tax remains usable without inventing exact deduction facts."""

import pytest
from test_exports import evidence, inventory, template_bytes
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company
from test_workflow import obligation

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.payroll import PayrollBounded
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.workflow import Workflow


@pytest.fixture
def bounded_company(tmp_path):
    company = Company(tmp_path / "bounded-consumers.sqlite")
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
    ):
        company.save(fact, subject)
    data = payroll(accounting_gross_salary_fen=300_000, tax_reported_salary_fen=300_000).model_dump(
        mode="json"
    )
    data.update(
        dict.fromkeys(
            (
                "tax_exempt_income_fen",
                "special_additional_deduction_fen",
                "other_legal_deduction_fen",
                "tax_relief_fen",
            )
        )
    )
    company.save(PayrollBounded.model_validate(data), "bounded-january")
    return company


def test_unpublished_bounded_wage_is_not_missing_person_or_empty_external_basis(bounded_company):
    company = bounded_company
    company.save(obligation(), "obligation")
    workflow = Workflow(company.engine)
    before = workflow.query("2026-01", as_of="2026-02-25")
    payroll_issues = [
        item
        for step in before["steps"]
        for item in step["fact_issues"]
        if item.get("domain") == "payroll"
    ]
    assert any(item["field"] == "unpublished_payroll" for item in payroll_issues)
    assert not any(item["field"] == "missing_payroll" for item in payroll_issues)
    with pytest.raises(KernelError) as unpublished:
        workflow.obligation_basis("obligation")
    assert unpublished.value.code == "basis_unpublished"
    company.publish("bounded-january")
    basis = workflow.obligation_basis("obligation")
    assert [item["subject_id"] for item in basis["accepted_calculations"]] == ["bounded-january"]
    assert company.current("bounded-january", "payroll_bounded").values["tax_state"] is None
    # Publishing an exact zero-tax outcome never records an actual external filing.
    state = workflow.query("2026-01", as_of="2026-02-25")
    assert state["obligations"][0]["status"] == "due"


def test_exact_net_export_includes_bounded_wage_and_still_requires_payee(bounded_company):
    company = bounded_company
    company.publish("bounded-january")
    template = evidence(company, template_bytes())
    inventory(company)
    exports = Exports(company.engine)
    with pytest.raises(NeedsInformation, match="收款姓名和账号"):
        exports.preview("2026-01", template_evidence_digest=template)
    exports.save_payee(
        "employee",
        name="合成员工",
        account="001234567890",
        evidence_digest=company.owner_confirmation,
        expected_revision=0,
        request_id=company.request(),
    )
    plan = exports.preview("2026-01", template_evidence_digest=template)
    wage = company.current("bounded-january", "payroll_bounded")
    assert wage.values["tax_fen"] == 0 and wage.values["tax_state"] is None
    assert len(plan["rows"]) == 1
    assert len(plan["rows"][0]["sources"]) == 1
    assert plan["rows"][0]["sources"][0]["kind"] == "payroll_bounded"
    assert plan["rows"][0]["amount_fen"] == wage.values["net_fen"]
    assert plan["rows"][0]["category"] == "工资"


def test_published_bounded_wage_satisfies_month_close_population(bounded_company):
    company = bounded_company
    company.publish("bounded-january")
    inventory(company)
    preview = Periods(company.engine).preview_close(
        "2026-01", owner_confirmation=company.owner_confirmation
    )
    assert preview["status"] == "preview"
