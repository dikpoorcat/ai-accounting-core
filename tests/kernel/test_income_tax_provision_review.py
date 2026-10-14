"""Independent regressions for recalculation order and frozen quarterly sources."""

import pytest

from test_computed_income_tax_reports import provision
from test_reports import book as book, scenario

from ai_accounting.kernel import close_review
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.tax_filing_data import TaxFilingData


def test_revised_profit_publishes_before_pending_income_tax_is_reprepared(book):
    report = scenario(book, tax=False)
    engine, save, publish, _ = book
    original = {
        "period": "2026-03", "counterparty_id": "supplier", "amount_fen": 500,
        "expense_class": "bank_fee", "creditor_kind": "supplier",
    }
    save("expense", "bank-cost", original)
    publish("bank-cost")
    provision(book)
    save("expense", "bank-cost", original | {"amount_fen": 700}, revision=1)

    with pytest.raises(KernelError) as combined:
        engine.preview(["bank-cost", "computed-tax"])
    assert combined.value.code == "needs_information"
    assert any(issue["field"] == "pending" for issue in combined.value.issues)
    preview = engine.preview(["bank-cost"])
    assert preview["subjects"] == ["bank-cost"]
    publish("bank-cost")
    with engine.store.connection(read_only=True) as connection:
        pending = {row[0] for row in connection.execute("SELECT subject_id FROM pending")}
    assert "bank-cost" not in pending and "computed-tax" in pending
    assert report.report(2026, 1)["status"] == "needs_information"
    tax = TaxFilingData(engine).query(2026, 1)["income_tax_assessments"][0]
    assert tax["status"] == "needs_information" and tax["cumulative_assessed_fen"] is None

    publish("computed-tax")
    current = report.report(2026, 1)
    assert current["status"] == "ready", current["fact_issues"]
    with QueryReads.snapshot(engine) as reads:
        snapshot = Reports(engine)._report(
            2026, 1, source="open", connection=reads.connection, reads=reads,
            _pre_tax_fields_only=True, _income_tax_basis=True,
        )["income_tax_snapshot"]
    assert snapshot["profit_before_tax_fen"] == -10_700
    assert TaxFilingData(engine).query(2026, 1)["income_tax_assessments"][0]["status"] == "ready"


def test_closed_zero_provision_and_later_actual_adjustment_keep_quarter_boundaries(book):
    report = scenario(book, tax=False)
    engine, _, publish, close = book
    proof = provision(book)
    for month in ("2026-01", "2026-02", "2026-03"):
        close(month)
    frozen = report.report(2026, 1, source="closed")
    engine.save_fact("income_tax_declaration_actual", "actual-tax", {
        "period": "2026-04", "assessment_id": "computed-tax", "year": 2026,
        "tax_period_end": "2026-03", "filing_evidence": proof,
        "basis_type": "filed_return", "cumulative_assessed_fen": 125,
        "prepaid_tax_fen": 25, "tax_due_fen": 100,
    }, evidence=(proof,), expected_revision=0, request_id="late-actual-feedback")
    publish("computed-tax", posting_period="2026-04")

    without_june = report.report(2026, 2)
    assert not any(issue.get("period") == "2026-03" for issue in without_june["fact_issues"])
    assert any(issue.get("period") == "2026-06" for issue in without_june["fact_issues"])
    assert report.report(2026, 1, source="closed")["statements"] == frozen["statements"]
    latest_tax = TaxFilingData(engine).query(2026, 1)["income_tax_assessments"][0]
    assert latest_tax["amount_source"] == "actual" and latest_tax["cumulative_assessed_fen"] == 125

    provision(book, period="2026-06", subject="computed-june")
    current = report.report(2026, 2)
    assert current["status"] == "ready", current["fact_issues"]
    assert current["statements"]["profit_statement"]["31"]["year_to_date_fen"] == 0
    assert report.report(2026, 1, source="closed")["statements"] == frozen["statements"]


def test_income_tax_policy_display_keeps_the_exact_adopted_revision(book):
    scenario(book, tax=False)
    engine, _, _, _ = book
    proof = provision(book)
    with QueryReads.snapshot(engine) as reads:
        calculation_id = reads.connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='computed-tax'",
        ).fetchone()[0]
        policy = reads.store.current_fact(reads.connection, "income-policy")
        original_policy = policy.id
        changed = policy.fact.model_dump(mode="json")
    changed["policy"]["version"] = "later-unadopted-policy"
    changed["policy"]["standard_rate_percent"] = "24"
    new_policy = engine.save_fact(
        "income_tax_policy", "income-policy", changed, evidence=(proof,), expected_revision=1,
        request_id="revise-policy-without-adopting",
    )["fact_id"]

    with QueryReads.snapshot(engine) as reads:
        basis = close_review.business_adopted_basis(
            reads.connection, engine, (calculation_id,), reads=reads,
        )
        assert len(basis["policies"]) == 1
        adopted = basis["policies"][0]
        assert adopted["kind"] == "income_tax_policy"
        assert adopted["label"] == "企业所得税政策"
        assert adopted["version"] == "synthetic-2026"
        assert adopted["reference"]["id"] == original_policy != new_policy
        assert adopted["official_urls"] == [changed["policy"]["source_url"]]
        cards = close_review._policy_cards(
            reads.connection, engine, (original_policy,), _source_reads=reads,
        )
        assert cards[0]["title"] == "企业所得税政策"
        assert "synthetic-2026" in cards[0]["subtitle"]
        assert "later-unadopted-policy" not in cards[0]["subtitle"]
