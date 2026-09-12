"""Evidence reviews preserve a closed wage/payment chain and its accepted filing."""

import pytest
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import payment
from test_payroll_withholding_actual import actual
from test_workflow import completion_from_basis, obligation, setup_company

from ai_accounting.kernel import workflow
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains.banking import (
    BankEntry,
    BankOpening,
    BankReconciliation,
    BankStatement,
    Match,
)
from ai_accounting.kernel.domains.transactions import Funding
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.types import canonical

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


def historical_chain_view(company, original_vouchers, current, *, basis_current):
    """Read the existing closed chain through shared queries and real page adapters."""
    queries, dashboard = BusinessQueries(company.engine), Dashboard(company.engine)
    status = queries.business_status("january", "2026-01", as_of="2026-03-31")
    assert status["current_publication"]["calculation"]["id"] == current["january"].id
    events = status["selected_accounting"]["period_events"]
    assert len(events) == 1
    wage_voucher = next(item for item in original_vouchers.values() if item["kind"] == "payroll")
    assert events[0]["voucher_version_id"] == wage_voucher["id"]
    net = next(item for item in status["settlements"]["obligations"] if item["name"] == "net")
    assert (net["source_amount_fen"], net["paid_fen"], net["remaining_fen"]) == (
        900_000,
        100_000,
        800_000,
    )
    completion = next(
        item for item in status["external"]["completions"] if item["subject_id"] == "completion"
    )
    assert completion["basis_current"] is basis_current
    assert canonical(completion["accepted_calculations"]) == canonical(
        current["completion"].values["accepted_calculations"]
    )
    assert status["external"]["as_of_semantics"] == "current_knowledge"
    readiness = queries.period_readiness("2026-01", as_of="2026-03-31")
    assert readiness["closure"]["state"] == "exact_close"
    assert readiness["frozen_readiness"]["status"] == "ready"
    assert readiness["readiness"] is None
    followups = readiness["current_followups"]
    assert followups["knowledge"] == "current_knowledge"
    assert followups["affects_frozen_readiness"] is False
    current_net = next(
        item
        for item in followups["settlements"]["obligations"]
        if item["key"] == "payroll:january:net"
    )
    assert current_net["source_amount_fen"] == current["january"].values["net_fen"]
    assert current_net["paid_fen"] == 100_000
    assert current_net["remaining_fen"] == current["january"].values["net_fen"] - 100_000
    external = next(
        item for item in followups["external"]["obligations"] if item["id"] == "obligation"
    )
    assert external["completion_status"] == ("completed" if basis_current else "due")

    brief_response = dashboard.brief("2026-01", voucher_number=wage_voucher["number"])
    brief = brief_response["data"]
    preparation = brief["period_preparation"]
    assert preparation["closure"] == readiness["closure"]
    assert preparation["frozen_readiness"]["status"] == "ready"
    assert preparation["current_followups"]["affects_frozen_readiness"] is False
    focused = brief["focused_voucher"]
    assert focused["voucher_version_id"] == wage_voucher["id"]
    traced = company.engine.trace(voucher_version_id=focused["voucher_version_id"])
    assert traced["voucher"]["id"] == wage_voucher["id"]
    assert traced["calculation"]["id"] == wage_voucher["calculation_id"]
    assert traced["calculation"]["outcome"]["values"]["net_fen"] == 900_000

    employee_response = dashboard.employees("2026-01")
    employee = next(
        item
        for item in employee_response["data"]["employees"]["items"]
        if item["employee_id"] == "employee"
    )
    source = next(item for item in employee["payroll_sources"] if item["source_id"] == "january")
    assert source["calculation_id"] == events[0]["calculation_id"]
    employee_net = next(item for item in source["obligations"] if item["name"] == "net")
    assert employee_net["remaining_fen"] == net["remaining_fen"]
    assert len(source["movements"]) == 1
    movement = source["movements"][0]
    assert movement["source_id"] == "payment" and movement["amount_fen"] == 100_000
    assert movement["source_calculation_id"] == source["calculation_id"]
    funds_response = dashboard.funds("2026-01")
    funds = funds_response["data"]
    assert funds["outflow_fen"] == brief["funds_overview"]["outflow_fen"] == 100_000
    payment_voucher = next(item for item in original_vouchers.values() if item["kind"] == "payment")
    payments = [
        item for item in funds["movements"] if item["reference"] == str(payment_voucher["number"])
    ]
    assert len(payments) == 1 and payments[0]["signed_amount_fen"] == -100_000
    for response in (brief_response, employee_response, funds_response):
        assert response["read_semantics"]["accounting"] == "frozen_close"
        assert response["read_semantics"]["knowledge"] == "current_knowledge"
    return {
        "accounting": status["selected_accounting"],
        "net": net,
        "employee_source": source["calculation_id"],
        "employee_movement": movement["calculation_id"],
        "funds_calculation": payments[0]["calculation_id"],
        "trace_calculation": traced["calculation"]["id"],
    }


def test_withholding_evidence_review_then_real_closed_correction(review_chain):
    company, service, withholding, initial = review_chain
    original = current_chain(company)
    january_ledger = company.engine.ledger("2026-01")
    original_vouchers = {row["calculation_id"]: row for row in january_ledger}
    original_journal = journal_snapshot(company)
    assert original["january"].values["tax_fen"] == 20_000
    assert original["january"].values["net_fen"] == 900_000
    assert original["february"].values["prior_tax_state"]["cumulative_withheld_tax_fen"] == 20_000
    assert original["february"].values["tax_fen"] == 5_200
    assert original["completion"].values["basis_current"]
    assert all(initial[subject]["impact"] == "initial" for subject in KINDS)
    frozen = None

    for revision in (1, 2):
        # Company.save registers a distinct confirmation document for each revision
        # while keeping every field of the actual withholding fact unchanged.
        saved = company.save(withholding, "actual-withholding", revision=revision)
        assert set(KINDS) <= company.pending()
        before = current_chain(company)
        preview, confirmed = company.publish("actual-withholding")
        results = {item["subject_id"]: item for item in preview["results"]}
        reviewed = current_chain(company)
        assert not company.pending()
        for subject in ("january", "payment", "february"):
            assert results[subject]["impact"] == "review_no_impact"
            assert results[subject]["accounting"] == initial[subject]["accounting"]
            assert results[subject]["accounting"]["contract"]
            assert len(results[subject]["accounting"]["digest"]) == 64
            assert reviewed[subject].id != before[subject].id
        for subject, result in results.items():
            assert confirmed[subject]["impact"] == result["impact"]
            assert confirmed[subject]["accounting"] == result["accounting"]
        for subject in ("january", "payment"):
            assert reviewed[subject].result_digest != before[subject].result_digest
        assert reviewed["february"].values == original["february"].values
        assert reviewed["payment"].fact_id == original["payment"].fact_id
        assert (
            reviewed["payment"].values["settlements"][0]["source_calculation"]
            == reviewed["january"].id
        )
        completion = reviewed["completion"]
        assert completion.fact_id == original["completion"].fact_id
        for field in ("accepted_calculations", "completion_date", "completion_evidence"):
            assert completion.values[field] == original["completion"].values[field]
        assert completion.values["basis_current"]
        assert (
            completion.values["reviewed_calculations"][0]["calculation_id"]
            == reviewed["january"].id
        )
        assert (
            completion.values["reviewed_calculations"][0]["result_digest"]
            == reviewed["january"].result_digest
        )
        with company.engine.store.connection(read_only=True) as connection:
            assert connection.execute(
                "SELECT 1 FROM dependency_fact WHERE calculation_id=? AND fact_id=?",
                (reviewed["january"].id, saved["fact_id"]),
            ).fetchone()
            for subject in ("payment", "february", "completion"):
                assert connection.execute(
                    "SELECT 1 FROM dependency_calculation WHERE calculation_id=? AND upstream_id=?",
                    (reviewed[subject].id, reviewed["january"].id),
                ).fetchone()
        assert journal_snapshot(company) == original_journal
        assert company.engine.ledger("2026-01") == january_ledger
        if revision == 1:
            assert service.query("2026-01", as_of="2026-03-01")["obligations"][0]["status"] == (
                "completed"
            )
            frozen = company.close("2026-01")
            frozen_view = historical_chain_view(
                company, original_vouchers, reviewed, basis_current=True
            )
        else:
            assert Periods(company.engine).closed_report("2026-01") == frozen
            assert (
                historical_chain_view(company, original_vouchers, reviewed, basis_current=True)
                == frozen_view
            )

    # The immutable payment is only partial, so a real tax change can be tested
    # without inventing an unrelated overpayment/recovery fact.
    company.save(
        withholding.model_copy(update={"withheld_tax_fen": 21_000}),
        "actual-withholding",
        revision=3,
    )
    before_failed_publish = current_chain(company)
    count = company.count("calculation")
    pending = company.pending()
    with pytest.raises(KernelError) as failure:
        company.publish("actual-withholding")
    assert failure.value.code == "closed_correction_required"
    assert company.count("calculation") == count
    assert current_chain(company) == before_failed_publish
    assert journal_snapshot(company) == original_journal
    assert company.pending() == pending

    preview, confirmed = company.publish("actual-withholding", correction_period="2026-03")
    results = {item["subject_id"]: item for item in preview["results"]}
    corrected = current_chain(company)
    for subject in ("january", "payment", "february"):
        assert results[subject]["impact"] == confirmed[subject]["impact"] == "accounting_changed"
        assert results[subject]["accounting"] != initial[subject]["accounting"]
    assert corrected["january"].values["tax_fen"] == 21_000
    assert corrected["february"].values["prior_tax_state"]["cumulative_withheld_tax_fen"] == 21_000
    assert corrected["february"].values["tax_fen"] == 4_200
    assert corrected["payment"].fact_id == original["payment"].fact_id
    assert corrected["payment"].values["amount_fen"] == 100_000
    assert not corrected["completion"].values["basis_current"]
    assert (
        corrected["completion"].values["accepted_calculations"]
        == original["completion"].values["accepted_calculations"]
    )
    march = company.engine.ledger("2026-03")
    for subject in ("january", "payment"):
        original_voucher = original_vouchers[original[subject].id]
        reversed_rows = [item for item in march if item["reverses_id"] == original_voucher["id"]]
        assert len(reversed_rows) == 1
        assert any(
            item["reverses_id"] is None
            and item["calculation_id"] == corrected[subject].id
            and item["number"] == confirmed[subject]["voucher_number"]
            for item in march
        )
    assert not company.pending()
    assert company.engine.ledger("2026-01") == january_ledger
    assert Periods(company.engine).closed_report("2026-01") == frozen
    assert (
        historical_chain_view(company, original_vouchers, corrected, basis_current=False)
        == frozen_view
    )
