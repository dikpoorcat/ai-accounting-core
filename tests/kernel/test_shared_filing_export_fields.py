"""Report, filing preparation and delivered files retain one adopted field version."""

import json
from pathlib import Path

from test_report_open_export import prepare_materials
from test_reports import book as book
from test_reports import scenario

from ai_accounting.kernel.reports import run_report_jobs
from ai_accounting.kernel.tax_filing_data import TaxFilingData
from ai_accounting.kernel.types import digest


def test_report_filing_and_delivered_manifest_share_amounts_and_exact_basis(book, tmp_path):
    report = scenario(book)
    prepare_materials(book)
    ordinary = report.report(2026, 1)
    plan = report.preview_export(2026, 1)
    filing = TaxFilingData(book[0]).query(2026, 1)
    basis_digest = digest(plan["report_basis"]).hex()
    rows = {"operating_revenue": 1, "operating_cost": 2, "profit_before_tax": 30}
    for field in filing["fields"]:
        if field["section"] not in {"cit", "financial_statement"}:
            continue
        assert field["status"] == "ready", field["fact_issues"]
        name = field["field_id"].split(".", 1)[1]
        line, column = str(rows[name]), field["period_basis"] + "_fen"
        assert field["value_fen"] == ordinary["statements"]["profit_statement"][line][column]
        assert field["value_fen"] == plan["statements"]["profit_statement"][line][column]
        assert {"type": "report_basis_digest", "value": basis_digest} in field["source_basis"]
    queued = report.confirm_export(
        2026, 1, preview_digest=plan["digest"], epochs=plan["epochs"],
        output_directory=str(tmp_path / "shared-field-export"), request_id="shared-field-export",
    )
    job = run_report_jobs(book[0])[0]
    assert job["status"] == "succeeded" and job["job_id"] == queued["job_id"]
    manifest = json.loads(
        (Path(job["result"]["directory"]) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["plan"] == plan
    assert digest(manifest["plan"]["report_basis"]).hex() == basis_digest
