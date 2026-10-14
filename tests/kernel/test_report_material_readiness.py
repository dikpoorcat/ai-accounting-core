"""Financial reports relax missing tax coverage, retaining received-source checks."""

import pytest
from test_materials import Company

from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods


def inventories(company, *, omit=None, tax_evidence=(), tax_expected=None):
    periods = Periods(company.engine)
    for category in MATERIAL_CATEGORIES:
        if category == omit:
            continue
        evidence = list(tax_evidence) if category == "tax" else []
        expected = (
            tax_expected if category == "tax" and tax_expected is not None else len(evidence)
        )
        periods.inventory(
            "2026-01", category, evidence=evidence, expected=expected,
            no_business=not evidence and expected == 0,
            confirmation_evidence=company.proof, request_id=company.request(),
        )


def checks(company, *, summary=False):
    periods = Periods(company.engine)
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        report = periods.collect_current_readiness(
            connection, "2026-01", purpose="financial_report",
            _allow_frozen_materials=summary,
        )
        close = periods.collect_current_readiness(
            connection, "2026-01", _allow_frozen_materials=summary,
        )
    return report, close


@pytest.mark.parametrize("missing", MATERIAL_CATEGORIES)
def test_report_only_omits_missing_tax_inventory(tmp_path, missing):
    company = Company(tmp_path)
    inventories(company, omit=missing)
    report, close = checks(company)
    field = "materials." + missing
    assert (field in {item["field"] for item in report["materials"]["issues"]}) == (
        missing != "tax"
    )
    assert field in {item["field"] for item in close["materials"]["issues"]}
    assert report["materials"]["status"] == ("ready" if missing == "tax" else "needs_information")
    assert close["materials"]["status"] == "needs_information"


@pytest.mark.parametrize("summary", [False, True])
def test_report_does_not_require_unreceived_tax_materials(tmp_path, summary):
    company = Company(tmp_path)
    inventories(company, tax_expected=2)
    report, close = checks(company, summary=summary)
    assert report["materials"]["issues"] == []
    assert report["materials"]["status"] == "ready"
    assert any(
        item.get("code") == "material_expected_missing" and item["category"] == "tax"
        for item in close["materials"]["issues"]
    )
    assert close["materials"]["status"] == "needs_information"


@pytest.mark.parametrize("summary", [False, True])
def test_report_still_checks_received_tax_original_without_registered_source(tmp_path, summary):
    company = Company(tmp_path)
    evidence = company.evidence(b"name,amount,period\ntax,10.00,2026-01\n")
    inventories(company, tax_evidence=[evidence])
    report, close = checks(company, summary=summary)
    for checked in (report, close):
        assert any(
            item.get("code") == "material_source_not_registered"
            and item["evidence_digest"] == evidence
            for item in checked["materials"]["issues"]
        )
        assert checked["materials"]["status"] == "needs_information"


@pytest.mark.parametrize("summary", [False, True])
def test_report_still_requires_processing_received_tax_rows(tmp_path, summary):
    company = Company(tmp_path)
    source, evidence = company.source(
        b"name,amount,period\ntax,10.00,2026-01\n", category="tax",
    )
    inventories(company, tax_evidence=[evidence], tax_expected=2)
    report, close = checks(company, summary=summary)
    for checked in (report, close):
        assert any(
            item.get("code") == "material_item_unresolved"
            and item["source_id"] == source["subject_id"]
            and item["field"] == "materials.tax"
            for item in checked["materials"]["issues"]
        )
        assert checked["materials"]["status"] == "needs_information"
    assert not any(
        item.get("code") == "material_expected_missing"
        for item in report["materials"]["issues"]
    )


def test_report_keeps_tax_no_business_conflicts(tmp_path):
    company = Company(tmp_path)
    engine = company.engine
    engine.save_fact(
        "income_tax_assessment", "confirmed-tax",
        {
            "period": "2026-01", "year": 2026,
            "assessment_basis": "confirmed_provision", "cumulative_assessed_fen": 1000,
        },
        evidence=(company.proof,), expected_revision=0, request_id=company.request(),
    )
    preview = engine.preview(["confirmed-tax"])
    engine.confirm(
        ["confirmed-tax"], preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id=company.request(),
    )
    inventories(company)
    report, close = checks(company)
    for checked in (report, close):
        assert {
            "field": "materials.tax", "message": "无业务确认与已确认业务冲突",
        } in checked["materials"]["issues"]
