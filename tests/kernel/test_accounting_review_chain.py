"""Evidence reviews preserve a closed wage/payment chain and its accepted filing."""

import pytest
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import payment
from test_payroll_withholding_actual import actual
from test_workflow import completion_from_basis, obligation, review_from_completion, setup_company

from ai_accounting.kernel import workflow
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.banking import (
    BankEntry,
    BankOpening,
    BankReconciliation,
    BankStatement,
    Match,
)
from ai_accounting.kernel.domains.transactions import Funding
from ai_accounting.kernel.periods import Periods

KINDS = {
    "january": "payroll",
    "payment": "payment",
    "february": "payroll",
    "completion": "external_completion",
}


@pytest.fixture
def review_chain(tmp_path):
    company = setup_company(tmp_path)
    withholding = actual(
        period="2026-01",
        amount=20_000,
        reported_cumulative_standard_deduction_fen=None,
    )
    for fact, subject in (
        (profile(effective_to="2026-02"), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (withholding, "actual-withholding"),
        (payroll(), "january"),
        (payroll(period="2026-02"), "february"),
        (obligation(), "obligation"),
        (
            BankOpening(
                period="2026-01", bank_account_id="bank", opening_fen=0, basis="new_account"
            ),
            "bank-opening",
        ),
        (
            Funding(
                period="2026-01",
                actual_date="2026-01-02",
                bank_account_id="bank",
                owner_id="owner",
                amount_fen=1_000_000,
                funding_kind="capital",
            ),
            "funding",
        ),
        (
            payment(100_000).model_copy(update={"period": "2026-01", "actual_date": "2026-01-31"}),
            "payment",
        ),
        (
            BankStatement(
                period="2026-01",
                bank_account_id="bank",
                opening_fen=0,
                closing_fen=900_000,
                entries=(
                    BankEntry(reference="capital", actual_date="2026-01-02", signed_fen=1_000_000),
                    BankEntry(reference="wage", actual_date="2026-01-31", signed_fen=-100_000),
                ),
            ),
            "statement",
        ),
        (
            BankReconciliation(
                period="2026-01",
                bank_account_id="bank",
                statement_id="statement",
                matches=(
                    Match(reference="capital", source_kind="funding", source_id="funding"),
                    Match(reference="wage", source_kind="payment", source_id="payment"),
                ),
            ),
            "reconciliation",
        ),
    ):
        company.save(fact, subject)
    company.confirm_payroll("january", "february")
    initial, _ = company.publish(
        "january", "february", "bank-opening", "funding", "payment", "statement", "reconciliation"
    )
    service = workflow.Workflow(company.engine)
    company.save(completion_from_basis(service.obligation_basis("obligation")), "completion")
    completion_preview, _ = company.publish("completion")
    previews = {
        item["subject_id"]: item for item in (*initial["results"], *completion_preview["results"])
    }
    return company, service, withholding, previews


def current_chain(company):
    return {subject: company.current(subject, kind) for subject, kind in KINDS.items()}


def journal_snapshot(company):
    with company.engine.store.connection(read_only=True) as connection:
        return {
            table: [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in (
                "voucher",
                "voucher_version",
                "voucher_line",
                "voucher_current",
                "balance",
            )
        }


def external_item(service):
    result = service.query("2026-01", as_of="2026-03-31")
    return next(
        item for item in result["sections"]["external"]["obligations"] if item["id"] == "obligation"
    )


def test_withholding_evidence_review_then_real_closed_correction(review_chain):
    company, service, withholding, initial = review_chain
    original = current_chain(company)
    original_journal = journal_snapshot(company)
    january_ledger = company.engine.ledger("2026-01")
    original_vouchers = {row["calculation_id"]: row for row in january_ledger}
    assert original["january"].values["tax_fen"] == 20_000
    assert original["february"].values["tax_fen"] == 5_200

    with company.engine.store.connection(read_only=True) as connection:
        completion_fact = company.engine.store.current_fact(connection, "completion")
    basis = service.obligation_basis("obligation")
    completion = completion_fact.fact
    review = review_from_completion(
        completion, completion_fact.id, basis["candidate_calculations"], "matched"
    )
    company.save(review, "review")
    company.publish("review")
    assert external_item(service)["actual_completion_status"] == "completed"
    assert external_item(service)["basis_review_status"] == "reviewed"

    for revision in (1, 2):
        company.save(withholding, "actual-withholding", revision=revision)
        assert "review" in company.pending()
        preview, confirmed = company.publish("actual-withholding")
        results = {row["subject_id"]: row for row in preview["results"]}
        for subject in ("january", "payment", "february"):
            assert results[subject]["impact"] == "review_no_impact"
            assert confirmed[subject]["accounting"] == initial[subject]["accounting"]
        assert (
            company.current("completion", "external_completion").fact_id
            == original["completion"].fact_id
        )
        assert (
            company.current("review", "external_basis_review").values["review_result"] == "outdated"
        )
        assert external_item(service)["actual_completion_status"] == "completed"
        assert external_item(service)["basis_review_status"] == "outdated"
        assert journal_snapshot(company) == original_journal
        assert company.engine.ledger("2026-01") == january_ledger
        if revision == 1:
            frozen = company.close("2026-01")
        else:
            assert Periods(company.engine).closed_report("2026-01") == frozen

    company.save(
        withholding.model_copy(update={"withheld_tax_fen": 21_000}),
        "actual-withholding",
        revision=3,
    )
    before = current_chain(company)
    count = company.count("calculation")
    with pytest.raises(KernelError) as failure:
        company.publish("actual-withholding")
    assert failure.value.code == "posting_period_required"
    assert current_chain(company) == before
    assert company.count("calculation") == count

    preview, confirmed = company.publish("actual-withholding", posting_period="2026-03")
    results = {row["subject_id"]: row for row in preview["results"]}
    corrected = current_chain(company)
    assert corrected["january"].values["tax_fen"] == 21_000
    assert corrected["february"].values["tax_fen"] == 4_200
    assert corrected["payment"].fact_id == original["payment"].fact_id
    for subject in ("january", "payment", "february"):
        assert results[subject]["impact"] == confirmed[subject]["impact"] == "accounting_changed"
    assert corrected["completion"].fact_id == original["completion"].fact_id
    assert external_item(service)["actual_completion_status"] == "completed"
    assert external_item(service)["basis_review_status"] == "outdated"
    assert company.engine.ledger("2026-01") == january_ledger
    assert Periods(company.engine).closed_report("2026-01") == frozen
    march = company.engine.ledger("2026-03")
    for subject in ("january", "payment"):
        original_voucher = original_vouchers[original[subject].id]
        assert len([item for item in march if item["reverses_id"] == original_voucher["id"]]) == 1
        assert any(
            item["reverses_id"] is None and item["calculation_id"] == corrected[subject].id
            for item in march
        )
