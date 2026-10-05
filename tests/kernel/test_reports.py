"""Three statements retain closed sources, classified detail and retryable XLSX output."""

import itertools
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from entity_fixture import seed_entities
from material_fixture import supporting_text
from openpyxl import load_workbook

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _position
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.reports import ReportClassification, Reports, _statements, run_report_jobs
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


def test_open_publication_probe_checks_exact_months_and_scales_with_month_count():
    from ai_accounting.kernel.reports import _has_open_publication

    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE calculation_publication(posting_period INTEGER);"
            "CREATE INDEX publication_posting ON calculation_publication(posting_period);"
            "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
            "CREATE TABLE voucher_version(id TEXT PRIMARY KEY,period INTEGER);"
            "CREATE INDEX voucher_period ON voucher_version(period,id);"
            "CREATE TABLE voucher_current(version_id TEXT UNIQUE);"
        )
        assert not _has_open_publication(connection, 10)
        connection.executemany(
            "INSERT INTO calculation_publication VALUES(?)", [(1,), (3,), (8,)]
        )
        connection.executemany("INSERT INTO period_close VALUES(?)", [(1,), (8,)])
        assert not _has_open_publication(connection, 2)
        assert _has_open_publication(connection, 3)
        # Month 8 cannot pretend that month 3 has its own close.
        assert _has_open_publication(connection, 8)
        connection.execute("INSERT INTO period_close VALUES(3)")

        def measured():
            steps = 0

            def tick():
                nonlocal steps
                steps += 1
                return 0

            connection.set_progress_handler(tick, 1)
            try:
                assert not _has_open_publication(connection, 8)
            finally:
                connection.set_progress_handler(None, 0)
            return steps

        before = measured()
        connection.executemany(
            "INSERT INTO calculation_publication VALUES(?)",
            [(month,) for month in (1, 3, 8)] * 1000,
        )
        after = measured()
        assert after < before + 100, (before, after)
        connection.execute("INSERT INTO calculation_publication VALUES(10)")
        assert not _has_open_publication(connection, 9)
        assert _has_open_publication(connection, 10)


@pytest.fixture
def book(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite", production_bundle(), "co", "911100000000000001", "db"
        )
    )
    seed_entities(
        engine,
        (
            ("owner", "person", None),
            ("supplier", "organization", None),
            ("cash", "fund_account", "cash"),
        ),
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

    def publish(*subjects, posting_period=None):
        preview = engine.preview(list(subjects), posting_period=posting_period)
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"publish-{next(counter)}",
            posting_period=posting_period,
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


@pytest.mark.parametrize("classification", [True, False])
def test_open_cash_profit_and_party_report_matches_full_source_resolution(book, classification):
    from ai_accounting.kernel.query_reads import QueryReads

    engine = book[0]
    report = scenario(book, classification=classification)
    with QueryReads.snapshot(engine) as reads:
        bounded = report._report(2026, 1, source="open", connection=reads.connection, reads=reads)
    with QueryReads.snapshot(engine) as reads:
        reads.report_line_relations_many = lambda requests: reads.relations_many(requests)
        full = report._report(2026, 1, source="open", connection=reads.connection, reads=reads)
    assert bounded == full
    assert bounded["statements"]["cash_flow_statement"]["6"]["current_fen"] == 10000
    assert bool(bounded["fact_issues"]) is not classification


@pytest.mark.parametrize("source,issues_only", [("open", False), ("open", True), ("closed", False)])
def test_report_reuses_frozen_end_amounts_without_changing_complete_plan(book, source, issues_only):
    from ai_accounting.kernel.query_reads import QueryReads
    from ai_accounting.kernel.types import YearMonth

    engine = book[0]
    report = scenario(book)
    for month in ("2026-01", "2026-02", *(() if source == "open" else ("2026-03",))):
        book[3](month)
    cutoff = YearMonth("2026-03").ordinal

    def read(connection, reads):
        statements = []
        connection.set_trace_callback(statements.append)
        try:
            plan = report._report(
                2026, 1, source=source, connection=connection, reads=reads,
                _issues_only=issues_only,
            )
        finally:
            connection.set_trace_callback(None)
        # This is the shared totals reader's actual frozen-baseline SQL, not
        # a mock of the conditional being optimized. Other cutoffs still read.
        end_reads = [
            sql for sql in statements
            if sql.startswith("SELECT period,manifest,digest FROM period_close ")
            and f"period<={cutoff} " in sql
        ]
        return plan, len(end_reads)

    with QueryReads.snapshot(engine) as reads:
        reused, owned_end_reads = read(reads.connection, reads)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        independent, unowned_end_reads = read(connection, QueryReads(engine, connection))
    assert reused == independent
    assert reused["status"] == "ready", reused["fact_issues"]
    assert reused["statements"]["cash_flow_statement"]["22"]["current_fen"] == 40000
    assert (owned_end_reads, unowned_end_reads) == (1, 2)


def test_report_without_frozen_baseline_retains_both_end_amount_reads(book):
    from ai_accounting.kernel.query_reads import QueryReads
    from ai_accounting.kernel.types import YearMonth

    engine = book[0]
    report = scenario(book)
    cutoff = YearMonth("2026-03").ordinal
    statements = []
    with QueryReads.snapshot(engine) as reads:
        reads.connection.set_trace_callback(statements.append)
        try:
            owned = report._report(
                2026, 1, source="open", connection=reads.connection, reads=reads,
            )
        finally:
            reads.connection.set_trace_callback(None)
    independent = report.report(2026, 1)
    assert owned == independent
    assert len([
        sql for sql in statements
        if sql.startswith("SELECT account,sum(debit-credit) amount FROM monthly_account ")
        and f"period<={cutoff} " in sql
    ]) == 2


def test_readiness_issues_match_full_report_without_full_history_id_output(book):
    from ai_accounting.kernel.query_reads import QueryReads
    from ai_accounting.kernel.reports import check_report_readiness
    from ai_accounting.kernel.types import YearMonth

    engine = book[0]
    report = scenario(book, classification=True)
    book[3]("2026-01")
    book[3]("2026-02")
    period = YearMonth("2026-03")
    with QueryReads.snapshot(engine) as reads:
        full = report._report(
            2026, 1, source="open", connection=reads.connection, reads=reads,
            through_period=period,
        )
    with QueryReads.snapshot(engine) as reads:
        issues_only = report._report(
            2026, 1, source="open", connection=reads.connection, reads=reads,
            through_period=period, _issues_only=True,
        )
        readiness = check_report_readiness(
            engine.store, reads.connection, period, reads=reads,
        )
    assert issues_only["fact_issues"] == full["fact_issues"] == readiness
    assert set(issues_only["report_fact_ids"]) < set(full["report_fact_ids"])


def test_readiness_issue_selection_falls_back_to_full_refs_without_directory(book, monkeypatch):
    import ai_accounting.kernel.report_classification_directory as directory
    from ai_accounting.kernel.query_reads import QueryReads
    from ai_accounting.kernel.reports import check_report_readiness
    from ai_accounting.kernel.types import YearMonth

    engine = book[0]
    report = scenario(book)
    book[3]("2026-01")
    book[3]("2026-02")
    monkeypatch.setattr(directory, "classification_directory_scope", lambda *_a, **_k: None)
    period = YearMonth("2026-03")
    with QueryReads.snapshot(engine) as reads:
        full = report._report(
            2026, 1, source="open", connection=reads.connection, reads=reads,
            through_period=period,
        )
    with QueryReads.snapshot(engine) as reads:
        narrow = check_report_readiness(engine.store, reads.connection, period, reads=reads)
        assert ("closed_report_fact_sources", period.ordinal) in reads._report_snapshot_cache
    assert narrow == full["fact_issues"]


def test_open_report_reuses_only_successfully_selected_month_lines(book, monkeypatch):
    import ai_accounting.kernel.report_projection as projection
    from ai_accounting.kernel.query_reads import QueryReads
    from ai_accounting.kernel.types import YearMonth

    engine = book[0]
    report = scenario(book)
    with QueryReads.snapshot(engine) as reads:
        reused = report._report(2026, 1, source="open", connection=reads.connection, reads=reads)
        assert ("report_open_source_rows", YearMonth("2026-03").ordinal) in (
            reads._report_snapshot_cache
        )
    authoritative_rows = projection._authoritative_rows

    def without_absence(*args, **kwargs):
        result = authoritative_rows(*args, **kwargs)
        if kwargs.get("with_absence"):
            return result[0], False
        return result

    monkeypatch.setattr(projection, "_authoritative_rows", without_absence)
    with QueryReads.snapshot(engine) as reads:
        selected = report._report(2026, 1, source="open", connection=reads.connection, reads=reads)
        assert ("report_open_source_rows", YearMonth("2026-03").ordinal) not in (
            reads._report_snapshot_cache
        )
    assert reused == selected


def test_report_pending_check_keeps_period_scope_when_pending_drives_lookup(book):
    engine = book[0]
    report = scenario(book)
    before = report.report(2026, 1)
    with engine.store.connection() as connection:
        subject, fact = connection.execute(
            "SELECT f.subject_id,f.id FROM fact_current c JOIN fact_revision f "
            "ON f.id=c.fact_id WHERE f.period<=? LIMIT 1",
            ((2026 - 1) * 12 + 3 - 1,),
        ).fetchone()
        connection.execute("INSERT INTO pending VALUES(?,?)", (subject, fact))
        connection.commit()
    blocked = report.report(2026, 1)
    assert any(item["field"] == "pending" for item in blocked["fact_issues"])
    with engine.store.connection() as connection:
        connection.execute("DELETE FROM pending WHERE subject_id=? AND cause_id=?", (subject, fact))
        connection.commit()
    restored = report.report(2026, 1)
    assert restored["fact_issues"] == before["fact_issues"]


def test_profit_only_classification_preserves_position_without_history_hydration(book):
    engine, save, publish, _ = book
    scenario(book, classification=False, tax=False)
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-02") as snap:
        before = _position(snap)
    version = classify(engine, save, publish)
    with engine.store.connection(read_only=True) as connection:
        classification_id = connection.execute(
            "SELECT c.fact_id FROM fact_current c JOIN fact_report_classification r "
            "ON r.revision_id=c.fact_id WHERE r.voucher_version_id=?",
            (version,),
        ).fetchone()[0]
    with dashboard._snapshot("2026-02") as snap:
        original_fact = snap.fact

        def checked_fact(ident):
            assert ident != classification_id
            return original_fact(ident)

        snap.fact = checked_fact
        snap.journal.select = lambda **_: pytest.fail(
            "profit-only classification must not hydrate historical journal lines"
        )
        after = _position(snap)
    assert after == before
    assert after["assets_fen"] == 50000
    assert after["liabilities_fen"] == 10000
    assert after["issues"] == before["issues"]


def test_duplicate_profit_classification_keeps_position_issue(book):
    engine, save, publish, _ = book
    scenario(book, classification=False, tax=False)
    version = classify(engine, save, publish)
    save(
        "report_classification",
        "class-duplicate",
        {
            "period": "2026-02",
            "voucher_version_id": version,
            "profit_details": [
                {"line_no": 1, "detail_code": "management_entertainment", "amount_fen": 10000}
            ],
        },
    )
    with Dashboard(engine)._snapshot("2026-02") as snap:
        position = _position(snap)
    assert position["assets_fen"] == 50000
    assert position["liabilities_fen"] == 10000
    assert {
        "field": "report_classification",
        "message": "同一凭证版本存在多个分类来源",
        "voucher_version_id": version,
    } in position["issues"]


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
    import hashlib

    from ai_accounting.kernel.business_queries import BusinessQueries
    from ai_accounting.kernel.response_contracts import validate_response
    from ai_accounting.kernel.workflow import Workflow

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

    def projected(status):
        workflow = Workflow(book[0]).query("2026-03", as_of="2026-04-01")
        assert validate_response("workflow", workflow) == workflow
        readiness = BusinessQueries(book[0]).period_readiness("2026-03", as_of="2026-04-01")
        assert validate_response("period_readiness", readiness) == readiness
        for jobs in (workflow["sections"]["files"]["jobs"],
                     readiness["current_followups"]["file_jobs"]):
            job = next(job for job in jobs if job["kind"] == "report_export")
            assert job["status"] == status
            assert job["period"] == {
                "year": 2026, "quarter": 1, "quarter_start": "2026-01-01",
                "quarter_end": "2026-03-31", "label": "2026 年第 1 季度",
            }
            assert job["verified_when_succeeded"] is (status == "succeeded")

    projected("pending")

    def fault(stage, ident):
        if stage == "files_published":
            raise RuntimeError("simulated crash after durable files")

    failed = run_report_jobs(book[0], fault=fault)[0]
    assert failed["status"] == "failed" and failed["error_code"] == "job_failed"
    projected("failed")
    assert book[0].jobs(job_id=failed["job_id"])[0]["error_code"] == "job_failed"
    manifest_before = (tmp_path / "export" / "manifest.json").read_bytes()
    assert run_report_jobs(book[0])[0]["status"] == "succeeded"
    projected("succeeded")
    core_job = book[0].jobs(job_id=failed["job_id"])[0]
    assert hashlib.sha256(Path(core_job["result"]["path"]).read_bytes()).hexdigest() == (
        core_job["result"]["sha256"]
    )
    workbook = load_workbook(core_job["result"]["path"])
    assert len(workbook.worksheets) == 3
    workbook.close()
    assert report.browser_export_status(core_job["id"])["status"] == "succeeded"
    assert (tmp_path / "export" / "manifest.json").read_bytes() == manifest_before
    assert run_report_jobs(book[0]) == []
    with book[0].store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('UPDATE jobs SET payload="{}"')
        connection.execute('UPDATE jobs SET status="failed"')
        connection.commit()
    next((tmp_path / "export").glob("*.xlsx")).write_bytes(b"corruption")
    result = run_report_jobs(book[0])[0]
    assert result["status"] == "failed" and result["error_code"] == "job_failed"


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
    publish("cost", posting_period="2026-04")
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
    from ai_accounting.kernel.report_flow import require_report_flow

    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        assert require_report_flow(engine, connection)["rows"] == 6
        connection.commit()


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
