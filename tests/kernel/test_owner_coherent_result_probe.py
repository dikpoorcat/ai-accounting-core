"""Narrow default-owner probes for coherent result and domain-filter corruption."""

import json
import traceback
from functools import partial

import pytest
from stage9_metrics import measure_work
from test_integrity_content import damage
from test_labor_assets import book as _labor_book
from test_labor_assets import cost
from test_payroll_corrections import company as _company
from test_reimbursement_assets import asset
from test_reimbursement_assets import book as _asset_book
from test_reports import book as _report_book
from test_reports import scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_reads import scalar_facts, verified_scalar_facts
from ai_accounting.kernel.integrity import verify_sources
from ai_accounting.kernel.types import canonical, digest

company, labor_book, asset_book, report_book = _company, _labor_book, _asset_book, _report_book


def change_result(engine, subject, mutate=None, *, kind=None):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT c.id,c.outcome FROM calculation_current h JOIN calculation c "
            "ON c.id=h.calculation_id WHERE h.subject_id=?",
            (subject,),
        ).fetchone()
    if kind is not None:
        damage(engine, "calculation", "UPDATE calculation SET kind=? WHERE id=?", (kind, row["id"]))
    else:
        outcome = json.loads(row["outcome"])
        mutate(outcome)
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
            (canonical(outcome), digest(outcome), row["id"]),
        )
    return row["id"]


def require_rejection(name, ident, before, request, receipt_dir):
    record = {"case": name, "calculation_id": ident, "before": before}
    try:
        after = request()
    except KernelError as error:
        record.update(
            rejected=True,
            code=error.code,
            details=error.details,
            protector=[
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(error.__traceback__)[-8:]
            ],
        )
        assert error.code == "content_integrity_failed"
    except Exception as error:
        record.update(
            rejected=False,
            exception_type=type(error).__name__,
            exception_message=str(error),
            protector=[
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(error.__traceback__)[-8:]
            ],
        )
    else:
        record.update(rejected=False, after=after)
    target = receipt_dir / f"owner-coherent-{name}.json"
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2), "utf-8")
    assert record["rejected"], (
        f"default owner did not reject with content_integrity_failed "
        f"({record.get('exception_type', 'accepted response')}); evidence: {target}"
    )


@pytest.mark.parametrize("change", ["gross", "outside_kind"])
def test_default_employee_current_result_coherent_probe(company, change, tmp_path):
    company.publish("january")
    dashboard = Dashboard(company.engine)
    request = partial(dashboard.employees, "2026-01")
    before = request()
    assert before["data"]["employees"]["gross_salary_fen"] == 1_000_000
    if change == "gross":
        ident = change_result(
            company.engine,
            "january",
            lambda body: body["values"].__setitem__("gross_fen", 1_000_001),
        )
    else:
        ident = change_result(company.engine, "january", kind="expense")
    require_rejection("employees-" + change, ident, before, request, tmp_path)


def test_default_assets_project_value_coherent_probe(labor_book, tmp_path):
    engine, save, publish = labor_book
    save("labor_project_cost", "labor-cost", cost())
    publish("labor-cost")
    dashboard = Dashboard(engine)
    request = partial(dashboard.assets, "2026-11")
    before = request()
    assert before["data"]["collections"]["projects"]["items"][0]["cost_fen"] == 1_600_000
    ident = change_result(
        engine, "labor-cost", lambda body: body["values"].__setitem__("capitalized_fen", 1_600_001)
    )
    require_rejection("assets-project-value", ident, before, request, tmp_path)


def test_default_assets_outside_domain_kind_probe(asset_book, tmp_path):
    engine, save, publish = asset_book
    save("reimbursed_asset", "asset", asset())
    publish("asset")
    dashboard = Dashboard(engine)
    request = partial(dashboard.assets, "2026-02")
    before = request()
    assert before["data"]["collections"]["assets"]["page"]["total_count"] == 1
    ident = change_result(engine, "asset", kind="expense")
    require_rejection("assets-outside-kind", ident, before, request, tmp_path)


def test_default_brief_activity_value_coherent_probe(company, tmp_path):
    company.publish("january")
    dashboard = Dashboard(company.engine)
    request = partial(dashboard.brief, "2026-01")
    before = request()
    assert before["data"]["collections"]["activity"]["items"][0]["amount_fen"] == 1_000_000
    ident = change_result(
        company.engine, "january", lambda body: body["values"].__setitem__("gross_fen", 1_000_001)
    )
    require_rejection("brief-activity-value", ident, before, request, tmp_path)


def test_default_quarter_amount_coherent_probe(report_book, tmp_path):
    scenario(report_book)
    engine = report_book[0]
    dashboard = Dashboard(engine)
    request = partial(dashboard.quarterly_report, 2026, 1)
    before = request()
    assert before["schema_version"] == 5 and before["close_state"] == "open"

    def mutate(body):
        for line in body["lines"]:
            if line["debit"]:
                line["debit"] += 1
            if line["credit"]:
                line["credit"] += 1

    ident = change_result(engine, "cost", mutate)
    require_rejection("quarter-amount", ident, before, request, tmp_path)


def test_default_brief_cross_month_payment_source_outside_kind_probe(report_book, tmp_path):
    scenario(report_book)
    engine = report_book[0]
    dashboard = Dashboard(engine)
    request = partial(dashboard.brief, "2026-03")
    before = request()
    assert any(
        item["subject_id"] == "payment" and item["amount_fen"] == 10000
        for item in before["data"]["collections"]["activity"]["items"]
    )
    ident = change_result(engine, "cost", kind="report_profile")
    require_rejection("brief-cross-month-source-kind", ident, before, request, tmp_path)


def test_asset_scalar_header_validation_reads_only_exact_headers(asset_book, tmp_path):
    engine, save, publish = asset_book
    save("reimbursed_asset", "asset", asset())
    publish("asset")

    def read(*, authenticate_header):
        with Dashboard(engine)._snapshot("2026-02") as snap:
            calculations = list(snap.calculations_of_kind("reimbursed_asset"))
            if authenticate_header:
                return verified_scalar_facts(snap, calculations)
            # The earlier path's fact/evidence guard is retained unchanged.
            verify_sources(
                engine, snap.connection, fact_ids={calc["fact_id"] for calc in calculations}
            )
            return scalar_facts(snap, calculations)

    before, facts = measure_work(engine, partial(read, authenticate_header=False))
    after, verified = measure_work(engine, partial(read, authenticate_header=True))
    assert verified == facts
    old, new = before["counters"], after["counters"]
    assert new["sql_calls"] == old["sql_calls"] + 1
    assert new["returned_rows"] == old["returned_rows"] + 1
    assert new["calculation_result_json_decodes"] == old["calculation_result_json_decodes"]
    assert new["stdlib_json_loads"] == old["stdlib_json_loads"]
    receipt = {"before": old, "after": new}
    (tmp_path / "asset-header-work.json").write_text(json.dumps(receipt, indent=2), "utf-8")
    print("asset_header_counter_receipt=" + json.dumps(receipt))
