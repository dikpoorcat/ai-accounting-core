"""Company work list chooses only established months and keeps source lanes distinct."""

import calendar
import json

import pytest
from monthly_close_fixture import ready
from test_materials import Company as MaterialCompany
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_reports import book as book  # noqa: F401
from test_reports import scenario
from test_workflow import obligation, setup_company

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.assets import LoanAgreement
from ai_accounting.kernel.domains.payroll import PayrollFirstWageTreatment
from ai_accounting.kernel.domains.transactions import Expense
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.read_indexes import sync_job
from ai_accounting.kernel.reports import ReportProfile
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.workflow import ExternalObligation
from ai_accounting.kernel.worklist import Worklist


def view(company, *, as_of="2026-03-01", period=None):
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        result = Worklist(company.engine).query(connection, as_of=as_of, period=period)
        assert validate_response("workflow", result) == result
        return result


def test_empty_company_does_not_invent_a_month(tmp_path):
    company = setup_company(tmp_path)
    result = view(company)

    assert result["period"] is None
    assert result["period_selection"] == "empty"
    assert [area["id"] for area in result["sections"]["materials_and_accounting"]] == [
        "bank",
        "payroll",
        "transactions",
        "tax",
        "assets",
        "financing",
    ]
    assert all(
        area["accounting"]["status"] == "unestablished"
        for area in result["sections"]["materials_and_accounting"]
    )


def test_obligation_establishes_a_month_but_not_an_accounting_payment(tmp_path):
    company = setup_company(tmp_path)
    company.save(obligation(), "tax-filing")

    result = view(company, as_of="2026-02-25")
    assert result["period"] == "2026-01"
    assert result["period_selection"] == "earliest_open_source"
    obligations = result["sections"]["external"]["obligations"]
    assert len(obligations) == 1
    assert obligations[0]["status"] == "due"
    assert all(
        area["accounting"]["fact_count"] == 0
        for area in result["sections"]["materials_and_accounting"]
    )


def test_explicit_month_does_not_require_any_source(tmp_path):
    company = setup_company(tmp_path)
    result = view(company, period="2027-05")
    assert result["period"] == "2027-05"
    assert result["period_selection"] == "explicit"
    assert result["sections"]["close"]["closure"]["state"] == "open"
    assert result["sections"]["close"]["status"] == "needs_information"
    assert {item["field"] for item in result["sections"]["close"]["issues"]} >= {
        "materials." + category for category in MATERIAL_CATEGORIES
    }
    readiness = BusinessQueries(company.engine).period_readiness("2027-05", as_of="2026-03-01")
    assert validate_response("period_readiness", readiness) == readiness


def test_future_obligation_stays_visible_without_selecting_future_month(tmp_path):
    company = setup_company(tmp_path)
    company.save(
        ExternalObligation(
            period="2026-01",
            obligation_kind="quarterly_tax",
            start_period="2026-01",
            end_period="2026-03",
            due_date="2026-04-20",
            applicability_confirmed=True,
        ),
        "quarter",
    )

    before = view(company, as_of="2026-02-15")
    after = view(company, as_of="2026-03-15")
    assert before["period"] is None
    assert before["sections"]["external"]["obligations"][0]["status"] == "pending"
    assert after["period"] == "2026-03"


def test_latest_close_is_background_when_no_open_source_exists(tmp_path):
    company = setup_company(tmp_path)
    ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-01")
    company.close("2026-01")

    result = view(company, as_of="2026-03-01")
    assert result["period"] == "2026-01"
    assert result["period_selection"] == "latest_processed_background"
    assert result["sections"]["close"]["status"] == "closed"
    readiness = BusinessQueries(company.engine).period_readiness("2026-01", as_of="2026-03-01")
    assert validate_response("period_readiness", readiness) == readiness


def test_loan_business_anchors_financing_but_report_policy_does_not(tmp_path):
    company = setup_company(tmp_path)
    company.save(
        ReportProfile(
            period="2026-01",
            company_name="Synthetic Company",
            accounting_standard="small_enterprise",
            bookkeeping_start="2026-01",
            newly_established_zero_opening_confirmed=True,
        ),
        "profile",
    )
    assert view(company, as_of="2026-02-01")["period"] is None

    company.save(
        LoanAgreement(
            period="2026-01",
            lender_id="lender",
            lender_is_licensed=True,
            currency="CNY",
            annual_rate_percent="6",
            day_count_basis="actual_365",
            maturity_date="2027-01-01",
            loan_term="short_term",
        ),
        "loan",
    )
    result = view(company, as_of="2026-02-01")
    assert result["period"] == "2026-01"
    financing = next(
        area for area in result["sections"]["materials_and_accounting"] if area["id"] == "financing"
    )
    assert financing["accounting"]["fact_count"] == 1
    assert financing["sources"][0]["kind"] == "loan_agreement"
    assert financing["accounting"] == {
        "status": "ready", "fact_count": 1, "calculation_count": 0,
        "pending_count": 0, "issues": [],
    }


def test_payroll_basis_is_not_unpublished_but_wage_confirmation_and_pending_still_block(tmp_path):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(), "profile"), (opening(), "opening"),
        (PayrollFirstWageTreatment(period="2026-01", employee_id="employee",
                                   standard_deduction_start_month=1), "first-wage"),
        (contribution_policy(), "contributions"), (income_tax_policy(), "income-tax"),
    ):
        company.save(fact, subject)

    def wage_area():
        return next(item for item in view(company, period="2026-01")["sections"][
            "materials_and_accounting"] if item["id"] == "payroll")

    basis = wage_area()
    # An active employee still requires this month's explicit wage inputs.
    # The basis itself requires no calculation; keep the real roster gate.
    assert basis["accounting"]["status"] == "needs_information"
    assert (basis["accounting"]["fact_count"], basis["accounting"]["calculation_count"],
            basis["accounting"]["pending_count"]) == (5, 0, 0)
    assert {issue["field"] for issue in basis["accounting"]["issues"]} == {"missing_payroll"}
    assert {item["kind"] for item in basis["sources"] if item["lane"] == "accounting"} == {
        "payroll_profile", "payroll_opening_state", "payroll_first_wage_treatment",
        "payroll_contribution_policy", "payroll_income_tax_policy",
    }
    company.save(payroll(), "wage")
    unconfirmed = wage_area()["accounting"]
    assert unconfirmed["status"] == "needs_information"
    assert unconfirmed["pending_count"] == 1
    assert unconfirmed["issues"]
    company.confirm_payroll("wage")
    company.publish("wage")
    published = wage_area()["accounting"]
    assert published["status"] == "ready"
    assert (published["fact_count"], published["calculation_count"],
            published["pending_count"]) == (6, 1, 0)
    company.save(profile(social_insurance_base_fen=1_100_000), "profile", revision=1)
    revised = wage_area()["accounting"]
    assert revised["status"] == "needs_information"
    assert revised["pending_count"] == 1


def test_report_classification_and_tax_confirmation_remain_sources_without_calculations(book):
    from types import SimpleNamespace

    scenario(book)
    company = SimpleNamespace(engine=book[0])
    for month, area, kind in (
        ("2026-02", "transactions", "report_classification"),
        ("2026-03", "tax", "report_income_tax_confirmation"),
    ):
        item = next(item for item in view(company, period=month)["sections"][
            "materials_and_accounting"] if item["id"] == area)
        source = next(source for source in item["sources"] if source["kind"] == kind)
        assert source["calculation_id"] is None and source["pending"] is False
        assert item["accounting"]["status"] == "ready"
        assert item["accounting"]["pending_count"] == 0
        assert item["accounting"]["fact_count"] > item["accounting"]["calculation_count"]


def test_real_unpublished_and_revised_business_still_needs_processing(tmp_path):
    company = setup_company(tmp_path)
    expense = Expense(period="2026-01", counterparty_id="supplier", amount_fen=1000,
                      expense_class="administration", creditor_kind="supplier")

    def transactions():
        return next(item for item in view(company, period="2026-01")["sections"][
            "materials_and_accounting"] if item["id"] == "transactions")["accounting"]

    company.save(expense, "expense")
    initial = transactions()
    assert initial["status"] == "needs_information"
    assert (initial["calculation_count"], initial["pending_count"]) == (0, 1)
    assert any(issue["field"] == "expense" for issue in initial["issues"])
    company.publish("expense")
    assert transactions()["pending_count"] == 0
    company.save(expense.model_copy(update={"amount_fen": 2000}), "expense", revision=1)
    amended = transactions()
    assert amended["status"] == "needs_information"
    assert (amended["calculation_count"], amended["pending_count"]) == (0, 1)


def test_material_lane_source_anchors_its_declared_business_area(tmp_path):
    company = MaterialCompany(tmp_path)
    company.source(period="2026-01", category="assets")

    result = view(company, as_of="2026-02-01")
    assert result["period"] == "2026-01"
    assets = next(
        area for area in result["sections"]["materials_and_accounting"] if area["id"] == "assets"
    )
    assert assets["materials"]["source_count"] == len(assets["sources"])
    assert assets["accounting"]["fact_count"] == 0
    assert {item["kind"] for item in assets["sources"]} >= {"material_source_v2"}
    assert all(item["lane"] == "material" for item in assets["sources"])


def test_close_backup_job_keeps_its_actual_month(tmp_path):
    company = setup_company(tmp_path)
    ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-01")
    periods = Periods(company.engine)
    preview = periods.preview_close("2026-01", owner_confirmation=company.owner_confirmation)
    periods.close(
        "2026-01",
        owner_confirmation=company.owner_confirmation,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        backup_directory=str(tmp_path / "backup"),
        request_id=company.request(),
    )

    result = view(company, as_of="2026-03-01")
    jobs = result["sections"]["files"]["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "portable_backup"
    assert jobs[0]["status"] == "pending"
    assert jobs[0]["period"] == "2026-01"

    with company.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE jobs SET status='failed',error_code='private stack trace' WHERE id=?",
            (jobs[0]["job_id"],),
        )
        connection.commit()
    failed = view(company, as_of="2026-03-01")["sections"]["files"]["jobs"][0]
    assert failed["error_code"] == "job_failed"
    assert "private stack trace" not in str(failed)


def test_manual_backup_is_company_work_without_inferred_month(tmp_path):
    company = setup_company(tmp_path)
    company.engine.queue_backup(str(tmp_path / "backup"), request_id=company.request())
    jobs = view(company, period="2026-03")["sections"]["files"]["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "portable_backup"
    assert jobs[0]["association"] == "company" and jobs[0]["period"] is None
    assert jobs[0]["status"] == "pending"


@pytest.mark.parametrize("quarter", [1, 2, 3, 4])
def test_all_file_job_states_project_months_and_complete_quarter_periods(tmp_path, quarter):
    company = setup_company(tmp_path)
    month = f"2026-{quarter * 3:02d}"
    saved_period = {
        "year": 2026, "quarter": quarter,
        "quarter_start": f"2026-{quarter * 3 - 2:02d}-01",
        "quarter_end": f"{month}-{calendar.monthrange(2026, quarter * 3)[1]}",
    }
    expected_period = {**saved_period, "label": f"2026 年第 {quarter} 季度"}
    plans = {}
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN")
        for kind in ("payment_export", "tax_import", "report_export"):
            for status in ("pending", "running", "failed", "succeeded"):
                ident = kind + "-" + status
                plan = (
                    {"period": saved_period, "report_fact_ids": [], "source_closes": []}
                    if kind == "report_export" else
                    {"period": month, "rows": []} if kind == "payment_export" else
                    {"period": month, "source_versions": []}
                )
                plans[ident] = json.dumps({"plan": plan})
                connection.execute(
                    "INSERT INTO jobs(id,kind,payload,status,error_code,result) "
                    "VALUES(?,?,?,?,?,?)",
                    (ident, kind, plans[ident], status,
                     "private failure" if status == "failed" else None,
                     json.dumps({"path": "synthetic.xlsx", "sha256": "a" * 64})
                     if status == "succeeded" else None),
                )
                sync_job(connection, ident)
        connection.commit()

    result = view(company, period=month)
    workflow_jobs = {job["job_id"]: job for job in result["sections"]["files"]["jobs"]}
    readiness = BusinessQueries(company.engine).period_readiness(month, as_of="2026-12-31")
    assert validate_response("period_readiness", readiness) == readiness
    readiness_jobs = {job["job_id"]: job for job in readiness["current_followups"]["file_jobs"]}
    assert set(workflow_jobs) == set(readiness_jobs) == set(plans)
    for jobs in (workflow_jobs, readiness_jobs):
        for ident, job in jobs.items():
            assert job["period"] == (expected_period if job["kind"] == "report_export" else month)
            assert job["association"] == "period_scope"
            assert job["status"] == ident.rsplit("-", 1)[1]
            assert job["current_file_availability"] == "not_checked"
            assert job["verified_when_succeeded"] is (job["status"] == "succeeded")
    with company.engine.store.connection(read_only=True) as connection:
        assert {
            row[0]: row[1] for row in connection.execute("SELECT id,payload FROM jobs")
        } == plans
        company_jobs = Worklist._company_jobs(connection)
        assert all(job["period"] == expected_period for job in company_jobs
                   if job["kind"] == "report_export")
    assert len(company.engine.jobs()) == len(plans)



@pytest.mark.parametrize(
    ("payload", "result"),
    [
        ('{"private":"do-not-leak"', None),
        ('["do-not-leak"]', None),
        ('{"plan":["do-not-leak"]}', None),
        ('{}', '{"private":"do-not-leak"'),
        ('{}', '["do-not-leak"]'),
    ],
)
def test_company_job_rejects_malformed_content_without_disclosure(payload, result):
    class DamagedJobConnection:
        def execute(self, statement, parameters=()):
            if statement.startswith("SELECT * FROM jobs"):
                return [
                    {
                        "id": "synthetic-job",
                        "kind": "portable_backup",
                        "status": "failed",
                        "attempts": 1,
                        "error_code": None,
                        "payload": payload,
                        "result": result,
                    }
                ]
            return []

    with pytest.raises(KernelError) as exc:
        Worklist._company_jobs(DamagedJobConnection())
    assert exc.value.code == "content_integrity_failed"
    assert "do-not-leak" not in str(exc.value.response())


def test_closed_month_new_business_is_carried_to_first_open_work_month(tmp_path):
    company = setup_company(tmp_path)
    ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-01")
    company.close("2026-01")
    company.save(
        Expense(
            period="2026-01",
            counterparty_id="supplier",
            amount_fen=1000,
            expense_class="administration",
            creditor_kind="supplier",
        ),
        "late-expense",
    )
    result = view(company, as_of="2026-03-01")
    assert result["period"] == "2026-02"
    assert result["period_selection"] == "closed_issue_carry"
    assert result["sections"]["close"]["status"] == "needs_information"


def _closed_material(company, raw, subject):
    evidence = company.engine.register_evidence(
        raw, "text/csv", subject + ".csv", request_id=company.request()
    )["digest"]
    return Materials(company.engine).receive(
        subject,
        {
            "period": "2026-01",
            "evidence_digest": evidence,
            "category": "transactions",
            "purpose": "business",
            "specification": {
                "format": "csv",
                "columns": [
                    {"column": "A", "role": "context"},
                    {"column": "B", "role": "amount"},
                    {"column": "C", "role": "recognition_period"},
                ],
            },
        },
        evidence=(evidence, company.owner_confirmation),
        expected_revision=0,
        request_id=company.request(),
    )


def test_closed_published_material_problem_is_carried_until_resolved(tmp_path, monkeypatch):
    company = setup_company(tmp_path)
    ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-01")
    company.close("2026-01")
    source = _closed_material(company, b"name,amount,period\na,10.00,2026-01\n", "late-file")

    from ai_accounting.kernel import materials

    inspections = []
    inspect = materials.inspect_bytes

    def count_inspection(raw, specification):
        inspections.append(raw)
        return inspect(raw, specification)

    monkeypatch.setattr(materials, "inspect_bytes", count_inspection)
    pending = view(company, as_of="2026-03-01")
    assert inspections.count(b"name,amount,period\na,10.00,2026-01\n") == 1
    assert pending["period"] == "2026-02"
    assert pending["period_selection"] == "closed_issue_carry"
    assert any(
        issue.get("responsibility") == "closed_followup"
        for issue in pending["sections"]["close"]["issues"]
    )

    Materials(company.engine).resolve(
        "late-file-resolution",
        {
            "period": "2026-01",
            "source_id": source["subject_id"],
            "source_fact_id": source["fact_id"],
            "location": "CSV!B2",
            "treatment": "no_accounting",
            "non_accounting_reason": "not_company_business",
            "reason": "负责人确认该笔非本公司业务",
        },
        evidence=(company.owner_confirmation,),
        expected_revision=0,
        request_id=company.request(),
    )
    settled = view(company, as_of="2026-03-01")
    assert settled["period"] == "2026-01"
    assert settled["period_selection"] == "latest_processed_background"


def test_future_material_row_does_not_carry_closed_month(tmp_path):
    company = setup_company(tmp_path)
    ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-01")
    company.close("2026-01")
    _closed_material(company, b"name,amount,period\na,10.00,2026-04\n", "future-file")

    result = view(company, as_of="2026-03-01")
    assert result["period"] == "2026-01"
    assert result["period_selection"] == "latest_processed_background"


def test_closed_material_unknown_period_is_carried_for_assignment(tmp_path):
    company = setup_company(tmp_path)
    ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-01")
    company.close("2026-01")
    _closed_material(company, b"name,amount,period\na,10.00,\n", "unassigned-file")

    result = view(company, as_of="2026-03-01")
    assert result["period"] == "2026-02"
    assert result["period_selection"] == "closed_issue_carry"
    assert any(
        issue.get("responsibility") == "unassigned"
        for issue in result["sections"]["close"]["issues"]
    )
