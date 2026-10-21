"""Financial resources are exactly the inputs used by loan/social classification."""

from dataclasses import asdict
from unittest.mock import patch

import _financial_snapshot_review_support as support
import pytest
import test_financial_snapshot_review as review

from ai_accounting_modular.ledger import _financial
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_positions import _resource_positions
from ai_accounting_modular.ledger._financial_selection import Selection
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory


@pytest.fixture(params=MODES)
def implementation(request, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(support, "ledger_repository", lambda: factory)


def complete_inputs(selection, heads, accounts):
    """The previous full input selection, using the same unchanged amount formula."""
    return {
        table: selection.children(table, heads)
        for table in ("obligation", "settlement", "obligation_reclassification")
    }


def original_outputs(book, scope, read):
    with patch.object(Selection, "position_resources", complete_inputs):
        return (
            book.app.ledger.financial_snapshot(scope, read),
            asdict(book.app.ledger.financial_summary(scope, read)),
        )


@pytest.mark.parametrize(
    "case",
    [
        review.test_review_R01_full_fixed_numbers_through_real_sale_money_expense_and_income_tax,
        review.test_review_R18_distinct_loans_classify_at_exact_twelve_month_boundary,
        review.test_review_R19_beginning_and_ending_classify_same_opening_loan_separately,
        review.test_review_as_posted_loan_terms_come_from_old_adoption_not_current_contract,
        review.test_review_payroll_known_categories_do_not_need_fictitious_institution_parties,
    ],
)
def test_complete_reports_and_exact_source_digest_match_previous_inputs(
    implementation, tmp_path, monkeypatch, case
):
    actual = _financial._snapshot_v1
    compared = []

    def compare(scope, read):
        with patch.object(Selection, "position_resources", complete_inputs):
            expected = actual(scope, read)
        observed = actual(scope, read)
        assert observed == expected
        compared.append(observed)
        return observed

    monkeypatch.setattr(_financial, "_snapshot_v1", compare)
    case(tmp_path)
    assert compared


def test_trade_resources_are_not_loaded_or_calculated_for_financial_reports(
    implementation, tmp_path, monkeypatch
):
    book = support.assemble(tmp_path)
    bill = support.expense("ordinary", "500")
    book.submit(support.opening(cash="1000"), bill, support.payment("paid", bill, "200"))
    scope = {
        "company_id": book.company,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "current",
    }
    with book.app.storage.read_unit(book.company) as read:
        expected = original_outputs(book, scope, read)
        repo = read.repository("ledger")
        children, narrow = repo.children, repo.financial_resource_rows
        calls = []

        def other_children(table, ids):
            assert table not in ("obligation", "settlement", "obligation_reclassification")
            return children(table, ids)

        def actual_rows(ids, accounts):
            records = narrow(ids, accounts)
            assert records == {
                "obligation": [],
                "settlement": [],
                "obligation_reclassification": [],
            }
            calls.append(records)
            return records

        monkeypatch.setattr(repo, "children", other_children)
        monkeypatch.setattr(repo, "financial_resource_rows", actual_rows)
        assert book.app.ledger.financial_snapshot(scope, read) == expected[0]
        assert asdict(book.app.ledger.financial_summary(scope, read)) == expected[1]
        assert calls
        assert expected[0]["balance_sheet"]["33"]["ending"]["amount_fen"] == "300"


@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_controlled_loan_uses_both_selected_producer_and_selected_consumer(
    implementation, tmp_path, monkeypatch, view
):
    book = support.assemble(tmp_path)
    contract = support.loan_contract("loan", "2027-01-31")
    drawdown = support.drawdown("loan-cash", contract, "300")
    bill = support.expense("ordinary", "100")
    repayment = support.payment("repay", drawdown, "100", slot="principal", on="2026-02-01")
    book.submit(
        support.opening(cash="1000"),
        contract,
        drawdown,
        bill,
        support.payment("trade-paid", bill, "40"),
        repayment,
    )
    for month, outstanding in (("2026-01", "300"), ("2026-02", "200")):
        scope = {"company_id": book.company, "period_from": month, "period_to": month, "view": view}
        with book.app.storage.read_unit(book.company) as read:
            expected = original_outputs(book, scope, read)
            repo, observed = read.repository("ledger"), []
            narrow = repo.financial_resource_rows

            def actual_rows(ids, accounts, *, _narrow=narrow, _observed=observed):
                records = _narrow(ids, accounts)
                assert all(row["account_code"] == "2501" for row in records["obligation"])
                assert all(
                    row["source_business_id"] == drawdown["business_id"]
                    for row in records["settlement"]
                )
                assert all(row["result_id"] in ids for rows in records.values() for row in rows)
                _observed.append(records)
                return records

            with monkeypatch.context() as local:
                local.setattr(repo, "financial_resource_rows", actual_rows)
                snapshot = book.app.ledger.financial_snapshot(scope, read)
                summary = asdict(book.app.ledger.financial_summary(scope, read))
            assert snapshot == expected[0] and summary == expected[1]
            assert snapshot["balance_sheet"]["40"]["ending"]["amount_fen"] == outstanding
            assert any(records["obligation"] for records in observed)
            assert any(records["settlement"] for records in observed) == month.endswith("02")


def test_fully_repaid_loan_keeps_zero_remaining_identity_and_same_source_selection(
    implementation, tmp_path
):
    book = support.assemble(tmp_path)
    contract = support.loan_contract("loan", "2027-01-31")
    drawn = support.drawdown("cash", contract, "300")
    book.submit(
        support.opening(),
        contract,
        drawn,
        support.payment("all-repaid", drawn, "300", slot="principal"),
    )
    scope = {
        "company_id": book.company,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "current",
    }
    with book.app.storage.read_unit(book.company) as read:
        expected = original_outputs(book, scope, read)
        selection = Selection(
            read.repository("ledger"), "current", "2026-01", read.start_period, Issues()
        )

        rows = _resource_positions(
            selection, [*selection.stock("2026-01").values(), selection.opening]
        )
        assert len(rows) == 1 and rows[0][2]["amount_fen"] == 300 and rows[0][3] == 0
        assert not selection.used  # Merely loading remaining=0 does not add a Source.
        assert book.app.ledger.financial_snapshot(scope, read) == expected[0]
        assert asdict(book.app.ledger.financial_summary(scope, read)) == expected[1]
