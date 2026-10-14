"""A kernel provision breaks the filing cycle without weakening other checks."""

import itertools

import pytest
from material_fixture import supporting_text
from test_reports import book as book, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.reports import Reports, run_report_jobs
from ai_accounting.kernel.tax_filing_data import TaxFilingData


def provision(book, *, period="2026-03", subject="computed-tax", treatment="standard"):
    engine, save, publish, _ = book
    proof = engine.register_evidence(
        b"synthetic confirmed actual-profit treatment; no special adjustments or prior loss",
        "text/plain", "income tax basis", request_id="income-tax-proof",
    )["digest"]
    supporting_text(engine, proof)
    for kind, ident, data in (
        ("income_tax_policy", "income-policy", {"period": "2026-01", "policy": {
            "version": "synthetic-2026", "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "source_url": "https://shanghai.chinatax.gov.cn/zcfw/zcfgk/grsds/202308/t468162.html",
            "standard_rate_percent": "25", "small_profit_rate_percent": "20",
            "small_profit_taxable_fraction": "0.25", "small_profit_limit_fen": 300_000_000,
        }}),
        ("income_tax_prepayment_basis", "income-basis-" + period, {
            "period": period, "year": 2026, "tax_period_end": period,
            "collection_method": "actual_profit", "treatment": treatment,
            "basis_evidence": proof, "eligibility_evidence": proof,
            "loss_deduction_fen": 0, "special_treatments": [],
        }),
    ):
        if ident == "income-policy":
            with engine.store.connection(read_only=True) as connection:
                if connection.execute("SELECT 1 FROM subject WHERE id=?", (ident,)).fetchone():
                    continue
        engine.save_fact(kind, ident, data, evidence=(proof,), expected_revision=0,
                         request_id="tax-input-" + ident)
    save("income_tax_assessment", subject, {
        "period": period, "year": 2026, "assessment_basis": "computed_provision",
        "policy_id": "income-policy", "basis_id": "income-basis-" + period,
    })
    publish(subject)
    return proof


def inventories_without_tax(book, *, tax_expected=None):
    engine = book[0]
    periods, ids = Periods(engine), itertools.count()
    with engine.store.connection(read_only=True) as connection:
        proof = connection.execute("SELECT hex(digest) FROM evidence LIMIT 1").fetchone()[0].lower()
        kinds = {row[0] for row in connection.execute("SELECT DISTINCT kind FROM subject")}
    busy_categories = {engine.store.registry.models[kind].material_category
                       for kind in kinds if kind in engine.store.registry.evaluators}
    for month in ("2026-01", "2026-02", "2026-03"):
        for category in MATERIAL_CATEGORIES:
            if category == "tax":
                if tax_expected is None:
                    continue
                periods.inventory(month, category, evidence=[], expected=tax_expected,
                                  no_business=False, confirmation_evidence=proof,
                                  request_id=f"missing-tax-{month}")
                continue
            busy = category in busy_categories
            periods.inventory(month, category, evidence=[proof] if busy else [],
                              expected=int(busy), no_business=not busy,
                              confirmation_evidence=proof, request_id=f"inventory-{next(ids)}")
    return periods, proof


@pytest.mark.parametrize("tax_expected", [None, 2])
def test_zero_provision_exports_without_duplicate_confirmation_or_tax_inventory(book, tax_expected):
    report = scenario(book, tax=False)
    provision(book)
    periods, proof = inventories_without_tax(book, tax_expected=tax_expected)
    current = report.report(2026, 1)
    assert current["status"] == "ready", current["fact_issues"]
    assert current["income_tax_source"] == "provisional"
    assert current["statements"]["profit_statement"]["31"]["current_fen"] == 0
    plan = report.preview_export(2026, 1)
    queued = report.confirm_export(2026, 1, preview_digest=plan["digest"], epochs=plan["epochs"],
                                   request_id="export-computed")
    jobs = run_report_jobs(book[0])
    assert any(job["job_id"] == queued["job_id"] and job["status"] == "succeeded" for job in jobs)
    with book[0].store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fact_report_income_tax_confirmation").fetchone()[0] == 0
        readiness = periods.collect_current_readiness(connection, "2026-03")
    assert any(item["field"] == "materials.tax" for item in readiness["issues"])


def test_new_profit_source_invalidates_zero_provision_in_reports_and_tax_query(book):
    report = scenario(book, tax=False)
    provision(book)
    engine, save, publish, _ = book
    before = report.report(2026, 1)
    save("expense", "later-cost", {
        "period": "2026-03", "counterparty_id": "supplier", "amount_fen": 500,
        "expense_class": "bank_fee", "creditor_kind": "supplier",
    })
    publish("later-cost")
    changed = report.report(2026, 1)
    assert any(issue.get("reason") == "needs_recalculation" for issue in changed["fact_issues"])
    query = TaxFilingData(engine).query(2026, 1)
    assert query["income_tax_assessments"][0]["cumulative_assessed_fen"] is None
    assert query["income_tax_assessments"][0]["status"] == "needs_information"
    publish("computed-tax")
    assert report.report(2026, 1)["status"] == "ready"
    assert before["digest"] != report.report(2026, 1)["digest"]


def test_actual_feedback_uses_same_tax_and_preserves_export_snapshot(book):
    report = scenario(book, tax=False)
    proof = provision(book)
    inventories_without_tax(book)
    engine, save, publish, _ = book
    plan = report.preview_export(2026, 1)
    job = report.confirm_export(2026, 1, preview_digest=plan["digest"], epochs=plan["epochs"],
                                request_id="original-export")
    with engine.store.connection(read_only=True) as connection:
        frozen = connection.execute("SELECT payload FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0]
    engine.save_fact("income_tax_declaration_actual", "actual-tax", {
        "period": "2026-04", "assessment_id": "computed-tax", "year": 2026,
        "tax_period_end": "2026-03", "filing_evidence": proof,
        "basis_type": "filed_return", "cumulative_assessed_fen": 125,
        "prepaid_tax_fen": 25, "tax_due_fen": 100,
    }, evidence=(proof,), expected_revision=0, request_id="actual-feedback")
    assert report.report(2026, 1)["status"] == "needs_information"
    publish("computed-tax")
    actual = report.report(2026, 1)
    assert actual["status"] == "ready", actual["fact_issues"]
    assert actual["income_tax_source"] == "actual"
    assert actual["statements"]["profit_statement"]["31"]["year_to_date_fen"] == 125
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT payload FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0] == frozen
    with pytest.raises(KernelError) as stale:
        report.confirm_export(2026, 1, preview_digest=plan["digest"], epochs=plan["epochs"],
                              request_id="stale-export")
    assert stale.value.code == "preview_expired"


def test_freezing_unchanged_profit_does_not_invalidate_computed_provision(book):
    report = scenario(book, tax=False)
    provision(book)
    before = report.report(2026, 1)["statements"]
    for period in ("2026-01", "2026-02", "2026-03"):
        book[3](period)
    closed = report.report(2026, 1, source="closed")
    assert closed["status"] == "ready", closed["fact_issues"]
    assert closed["statements"] == before


def test_unproven_zero_assessment_never_falls_back_to_manual_zero(book, monkeypatch):
    from ai_accounting.kernel.business_queries import BusinessQueries

    report = scenario(book)
    provision(book)
    original = BusinessQueries._selected_accounting

    def uncertain_tax(self, *args, **kwargs):
        if kwargs.get("kinds") == {"income_tax_assessment"}:
            return {"through_period": {
                "voucher_events": [], "state_results": [],
                "unestablished_state_selections": [{"reason": "state_selection_unavailable"}],
            }}
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BusinessQueries, "_selected_accounting", uncertain_tax)
    result = report.report(2026, 1)
    assert result["status"] == "needs_information"
    assert any(issue["field"] == "income_tax_assessment"
               and issue.get("reason") == "state_selection_unavailable"
               for issue in result["fact_issues"])
