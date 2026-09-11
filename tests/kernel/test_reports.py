"""Three statements retain closed sources, classified detail and retryable XLSX output."""

import itertools
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from material_fixture import supporting_text
from openpyxl import load_workbook

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.reports import ReportClassification, Reports, _statements, run_report_jobs
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


@pytest.fixture
def book(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite", default_registry(), "co", "911100000000000001", "db"
        )
    )
    proof = engine.register_evidence(
        b"fictional report evidence", "text/plain", "proof", request_id="proof"
    )["digest"]
    supporting_text(engine, proof)
    counter = itertools.count()

    def save(kind, subject, data, revision=0):
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=revision,
            request_id=f"save-{next(counter)}",
        )

    def publish(*subjects, correction_period=None):
        preview = engine.preview(list(subjects), correction_period=correction_period)
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"publish-{next(counter)}",
            correction_period=correction_period,
        )

    def close(period):
        periods = Periods(engine)
        with engine.store.connection(read_only=True) as connection:
            kinds = {
                r[0]
                for r in connection.execute(
                    "SELECT s.kind FROM subject s JOIN fact_current a ON a.subject_id=s.id "
                    "JOIN fact_revision f ON f.id=a.fact_id WHERE f.period=?",
                    ((int(period[:4]) - 1) * 12 + int(period[5:]) - 1,),
                )
            }
        categories = {
            engine.store.registry.models[k].material_category
            for k in kinds
            if k in engine.store.registry.evaluators
        }
        for category in MATERIAL_CATEGORIES:
            busy = category in categories
            periods.inventory(
                period,
                category,
                evidence=[proof] if busy else [],
                expected=int(busy),
                no_business=not busy,
                confirmation_evidence=proof,
                request_id=f"inventory-{next(counter)}",
            )
        preview = periods.preview_close(period, owner_confirmation=proof)
        return periods.close(
            period,
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"close-{next(counter)}",
        )

    return engine, save, publish, close


def profile(save, publish, period="2026-01", name="测试企业", subject="profile"):
    save(
        "report_profile",
        subject,
        {
            "period": period,
            "company_name": name,
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": "2026-01",
            "newly_established_zero_opening_confirmed": True,
        },
    )


def cit(save, publish, period="2026-03", amount=0, calculation_id=None):
    subject = "cit-" + period
    save(
        "report_income_tax_confirmation",
        subject,
        {
            "period": period,
            "treatment": "assessed" if calculation_id else "zero",
            "cumulative_assessed_fen": amount,
            "calculation_id": calculation_id,
            "explanation": "已核对本期所得税依据",
        },
    )


def classify(engine, save, publish, subject="cost", amount=10000):
    with engine.store.connection(read_only=True) as connection:
        version = connection.execute(
            "SELECT a.version_id FROM voucher_current a JOIN voucher_version v ON "
            "v.id=a.version_id JOIN calculation c ON c.id=v.calculation_id WHERE c.subject_id=?",
            (subject,),
        ).fetchone()[0]
    save(
        "report_classification",
        "class-" + version,
        {
            "period": "2026-02",
            "voucher_version_id": version,
            "profit_details": [
                {"line_no": 1, "detail_code": "management_entertainment", "amount_fen": amount}
            ],
        },
    )
    return version


def scenario(book, classification=True, tax=True):
    engine, save, publish, close = book
    profile(save, publish)
    save(
        "cash_funding",
        "capital",
        {
            "period": "2026-01",
            "actual_date": "2026-01-03",
            "owner_id": "owner",
            "funding_kind": "capital",
            "amount_fen": 50000,
            "cash_account_id": "cash",
        },
    )
    save(
        "expense",
        "cost",
        {
            "period": "2026-02",
            "counterparty_id": "supplier",
            "amount_fen": 10000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish("capital", "cost")
    if classification:
        classify(engine, save, publish)
    save(
        "cash_payment",
        "payment",
        {
            "period": "2026-03",
            "actual_date": "2026-03-09",
            "direction": "outflow",
            "cash_account_id": "cash",
            "counterparty_id": "supplier",
            "amount_fen": 10000,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "cost",
                    "obligation": "primary",
                    "amount_fen": 10000,
                }
            ],
        },
    )
    publish("payment")
    if tax:
        cit(save, publish)
    return Reports(engine)


def close_quarter(book):
    for month in ("2026-01", "2026-02", "2026-03"):
        book[3](month)


def test_real_business_three_statements_and_template(book, tmp_path):
    report = scenario(book)
    open_plan = report.report(2026, 1)
    assert open_plan["status"] == "ready", open_plan["fact_issues"]
    close_quarter(book)
    plan = report.preview_export(2026, 1)
    assert plan["statements"] == open_plan["statements"]
    balance, profit, cash = (
        plan["statements"][k] for k in ("balance_sheet", "profit_statement", "cash_flow_statement")
    )
    assert balance["1"]["ending_fen"] == 40000
    assert balance["48"]["ending_fen"] == 50000
    assert balance["51"]["ending_fen"] == -10000
    assert profit["14"]["current_fen"] == profit["16"]["current_fen"] == 10000
    assert cash["15"]["current_fen"] == 50000
    assert cash["6"]["current_fen"] == 10000
    assert len(plan["source_closes"]) == 3 and all(x["passed"] for x in plan["checks"])
    queued = report.confirm_export(
        2026,
        1,
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        output_directory=str(tmp_path / "report"),
        request_id="export",
    )
    first = run_report_jobs(book[0])[0]
    assert first["status"] == "succeeded", first
    assert first["job_id"] == queued["job_id"]
    workbook = load_workbook(first["result"]["path"], data_only=True)
    assert len(workbook.worksheets) == 3
    assert workbook.worksheets[0]["D7"].value == 400
    assert workbook.worksheets[1]["D21"].value == 100
    assert workbook.worksheets[2]["D23"].value == 500
    workbook.close()


@pytest.mark.parametrize(
    "classification,tax,field",
    [
        (False, True, "report_classification.profit_details"),
        (True, False, "report_income_tax_confirmation"),
    ],
)
def test_report_facts_required_before_close_without_filing_cycle(book, classification, tax, field):
    report = scenario(book, classification, tax)
    assert field in {x["field"] for x in report.report(2026, 1)["fact_issues"]}
    book[3]("2026-01")
    if classification:
        book[3]("2026-02")
    blocked = "2026-03" if classification else "2026-02"
    with pytest.raises(KernelError) as failure:
        book[3](blocked)
    assert failure.value.code == "period_not_ready"
    assert field in {item["field"] for item in failure.value.details["fact_issues"]}
    if not classification:
        classify(book[0], book[1], book[2])
        book[3]("2026-02")
    if not tax:
        cit(book[1], book[2])
    book[3]("2026-03")
    assert report.preview_export(2026, 1)["status"] == "ready"


def test_frozen_report_ignores_future_profile_and_live_projection(book):
    report = scenario(book)
    close_quarter(book)
    before = report.report(2026, 1, source="closed")
    engine, save, publish, _ = book
    profile(save, publish, "2026-04", name="后续名称", subject="new-profile")
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute('UPDATE monthly_account SET debit=999999 WHERE account="1001"')
        connection.commit()
    after = report.report(2026, 1, source="closed")
    for key in ("statements", "organization", "source_closes", "report_fact_ids", "digest"):
        assert after[key] == before[key]


def test_report_job_crash_retry_and_tamper_rejection(book, tmp_path):
    report = scenario(book)
    close_quarter(book)
    plan = report.preview_export(2026, 1)
    report.confirm_export(
        2026,
        1,
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        output_directory=str(tmp_path / "export"),
        request_id="export",
    )

    def fault(stage, ident):
        if stage == "files_published":
            raise RuntimeError("simulated crash after durable files")

    assert run_report_jobs(book[0], fault=fault)[0]["status"] == "failed"
    manifest_before = (tmp_path / "export" / "manifest.json").read_bytes()
    assert run_report_jobs(book[0])[0]["status"] == "succeeded"
    assert (tmp_path / "export" / "manifest.json").read_bytes() == manifest_before
    assert run_report_jobs(book[0]) == []
    with book[0].store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('UPDATE jobs SET payload="{}"')
        connection.execute('UPDATE jobs SET status="failed"')
        connection.commit()
    next((tmp_path / "export").glob("*.xlsx")).write_bytes(b"corruption")
    result = run_report_jobs(book[0])[0]
    assert result["status"] == "failed" and "KernelError" in result["error"]


def test_export_preview_epoch_expiration_is_atomic(book, tmp_path):
    report = scenario(book)
    close_quarter(book)
    plan = report.preview_export(2026, 1)
    profile(book[1], book[2], "2026-04", subject="changed")
    with pytest.raises(KernelError, match="过期"):
        report.confirm_export(
            2026,
            1,
            preview_digest=plan["digest"],
            epochs=plan["epochs"],
            output_directory=str(tmp_path / "export"),
            request_id="expired",
        )
    with book[0].store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_assessed_income_tax_requires_real_calculation(book):
    engine, save, publish, _ = book
    profile(save, publish)
    save(
        "income_tax_assessment",
        "assessment",
        {
            "period": "2026-03",
            "year": 2026,
            "assessment_basis": "confirmed_provision",
            "cumulative_assessed_fen": 700,
        },
    )
    publish("assessment")
    with engine.store.connection(read_only=True) as connection:
        calculation = connection.execute(
            'SELECT calculation_id FROM calculation_current WHERE subject_id="assessment"'
        ).fetchone()[0]
    cit(save, publish, amount=700, calculation_id=calculation)
    close_quarter(book)
    report = Reports(engine).preview_export(2026, 1)
    assert report["statements"]["profit_statement"]["31"]["current_fen"] == 700


def test_template_module_does_not_import_orm():
    import ast

    import ai_accounting.financial_statement_template as template

    tree = ast.parse(Path(template.__file__).read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module
        and ("sqlalchemy" in node.module or "service" in node.module or "models" in node.module)
        for node in ast.walk(tree)
    )


def test_closed_correction_uses_original_classification_in_next_quarter(book):
    report = scenario(book)
    close_quarter(book)
    original = report.preview_export(2026, 1)
    engine, save, publish, close = book
    save(
        "expense",
        "cost",
        {
            "period": "2026-02",
            "counterparty_id": "supplier",
            "amount_fen": 15000,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        revision=1,
    )
    publish("cost", correction_period="2026-04")
    with engine.store.connection(read_only=True) as connection:
        version = connection.execute(
            "SELECT v.id FROM voucher_current a JOIN voucher_version v ON v.id=a.version_id "
            'JOIN calculation c ON c.id=v.calculation_id WHERE c.subject_id="cost" '
            "AND v.reverses_id IS NULL ORDER BY v.period DESC LIMIT 1"
        ).fetchone()[0]
    save(
        "report_classification",
        "new-detail",
        {
            "period": "2026-04",
            "voucher_version_id": version,
            "profit_details": [
                {"line_no": 1, "detail_code": "management_entertainment", "amount_fen": 15000}
            ],
        },
    )
    cit(save, publish, "2026-06")
    for period in ("2026-04", "2026-05", "2026-06"):
        close(period)
    corrected = report.preview_export(2026, 2)
    profit = corrected["statements"]["profit_statement"]
    assert profit["16"]["current_fen"] == profit["14"]["current_fen"] == 5000
    assert profit["16"]["year_to_date_fen"] == 15000
    assert report.preview_export(2026, 1)["digest"] == original["digest"]


def test_unconfigured_company_can_close_but_cannot_export(book):
    close_quarter(book)
    with pytest.raises(KernelError) as failure:
        Reports(book[0]).preview_export(2026, 1)
    assert "report_profile" in {i["field"] for i in failure.value.details["fact_issues"]}


def report_row(
    account,
    amount,
    *,
    party=None,
    classification=None,
    kind="example",
    values=None,
    cashflow=None,
    version="example",
):
    return {
        "period": 24302,
        "account": account,
        "amount": amount,
        "party": party,
        "classification": classification,
        "kind": kind,
        "values": values or {},
        "cash_source": SimpleNamespace(),
        "fact": SimpleNamespace(),
        "cashflow": cashflow,
        "version_id": version,
        "reverses_id": None,
        "line_no": 1,
    }


def test_counterparties_are_reclassified_separately_and_split_cash_is_exact():
    rows = [
        report_row("2202", -100, party="vendor-a"),
        report_row("2202", 80, party="vendor-b"),
        report_row("1001", 20, cashflow="other_operating_receipts"),
    ]
    issues = []
    statements = _statements(rows, 24300, 24300, 24302, issues)
    assert not issues
    assert statements["balance_sheet"]["33"]["ending_fen"] == 100
    assert statements["balance_sheet"]["5"]["ending_fen"] == 80
    classification = ReportClassification.model_validate_json(
        json.dumps(
            {
                "period": "2026-03",
                "voucher_version_id": "paid",
                "cash_details": [
                    {"line_no": 1, "category": 3, "amount_fen": 100},
                    {"line_no": 1, "category": 12, "amount_fen": 200},
                ],
            }
        )
    )
    rows = [
        report_row("1001", -300, classification=classification, version="paid"),
        report_row("2202", 300, party="vendor"),
    ]
    issues = []
    statements = _statements(rows, 24300, 24300, 24302, issues)
    assert not issues
    assert statements["cash_flow_statement"]["3"]["current_fen"] == 100
    assert statements["cash_flow_statement"]["12"]["current_fen"] == 200
    assert statements["cash_flow_statement"]["20"]["current_fen"] == -300


def test_tax_surtax_credit_needs_detail_and_does_not_repeat_original_assessment():
    values = {
        "surtax_fen": 720,
        "urban_tax_fen": 420,
        "education_tax_fen": 180,
        "local_education_tax_fen": 120,
    }
    rows = [
        report_row("5403", 720, kind="tax_assessment", values=values),
        report_row("222102", -720),
        report_row("5403", -120, kind="tax_assessment", values=values, version="credit"),
        report_row("222102", 120),
    ]
    issues = []
    _statements(rows, 24300, 24300, 24302, issues)
    assert "report_classification.profit_details" in {i["field"] for i in issues}
    rows[2]["classification"] = ReportClassification.model_validate_json(
        json.dumps(
            {
                "period": "2026-03",
                "voucher_version_id": "credit",
                "profit_details": [
                    {"line_no": 1, "detail_code": "tax_urban", "amount_fen": 70},
                    {"line_no": 1, "detail_code": "tax_education", "amount_fen": 30},
                    {"line_no": 1, "detail_code": "tax_local_education", "amount_fen": 20},
                ],
            }
        )
    )
    issues = []
    result = _statements(rows, 24300, 24300, 24302, issues)["profit_statement"]
    assert not issues
    assert result["3"]["current_fen"] == 600
    assert result["6"]["current_fen"] == 350
    assert result["10"]["current_fen"] == 250


def test_prior_year_income_tax_adjustment_is_current_expense_not_current_year_assessment(book):
    engine, save, publish, close = book
    save(
        "report_profile",
        "profile",
        {
            "period": "2025-12",
            "company_name": "测试企业",
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": "2025-12",
            "newly_established_zero_opening_confirmed": True,
        },
    )
    save(
        "income_tax_assessment",
        "old-year",
        {
            "period": "2025-12",
            "year": 2025,
            "assessment_basis": "confirmed_provision",
            "cumulative_assessed_fen": 100,
        },
    )
    publish("old-year")
    with engine.store.connection(read_only=True) as connection:
        old_calculation = connection.execute(
            'SELECT calculation_id FROM calculation_current WHERE subject_id="old-year"'
        ).fetchone()[0]
    cit(save, publish, "2025-12", amount=100, calculation_id=old_calculation)
    close("2025-12")
    save(
        "income_tax_assessment",
        "annual",
        {
            "period": "2026-03",
            "year": 2025,
            "assessment_basis": "annual_settlement",
            "cumulative_assessed_fen": 60,
        },
    )
    publish("annual")
    cit(save, publish, "2026-03")
    close_quarter(book)
    plan = Reports(engine).preview_export(2026, 1)
    assert plan["statements"]["profit_statement"]["31"]["current_fen"] == -40
