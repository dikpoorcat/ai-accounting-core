"""Financial calculation loads exact headers, necessary Targets and integer journal columns."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest
from _financial_snapshot_review_support import BANK, SOURCE, assemble, expense, fact, opening

from ai_accounting_modular.contracts.public import MAX_FEN, MIN_FEN, ContractError, format_fen
from ai_accounting_modular.ledger._balance_cube import balance_cube
from ai_accounting_modular.ledger._journal_amounts import journal_amount


@pytest.mark.parametrize("value", [MIN_FEN, -1, 0, 1, MAX_FEN])
def test_journal_integer_and_typed_text_amounts_keep_the_same_signed_int64(value):
    assert journal_amount(value) == journal_amount(str(value)) == value


@pytest.mark.parametrize("value", [True, False, 1.0, None, MAX_FEN + 1, MIN_FEN - 1, "01", "1.0"])
def test_journal_projection_does_not_accept_bool_float_noncanonical_or_out_of_range(value):
    with pytest.raises(ValueError):
        journal_amount(value)


def test_cube_still_checks_each_ordered_dimension_addition_before_later_cancellation():
    rows = [
        {"account_code": "1002", "debit_fen": debit, "credit_fen": credit}
        for debit, credit in ((MAX_FEN, 0), (1, 0), (0, 1))
    ]
    selection = SimpleNamespace(
        _balance_cubes={},
        start="2026-01",
        opening_rows=lambda: [],
        stock=lambda cutoff: {},
        lines=lambda heads: {"journal": rows},
    )
    with pytest.raises(ContractError, match="资源累计金额超出"):
        balance_cube(selection, "2026-01")


@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_public_financial_projection_batches_only_used_targets_and_uses_integer_lines(
    tmp_path, monkeypatch, view
):
    book = assemble(tmp_path)
    cost = expense("projection-cost", "200")
    interest = fact(
        "projection-bank-interest",
        "operations.other_income",
        {
            "income_kind": "bank_interest",
            "amount_fen": "5",
            "funds_account_id": BANK,
        },
        on="2026-01-15",
    )
    tax = fact(
        "projection-income-tax",
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": "confirmed_provision",
            "tax_year": 2026,
            "tax_period_end": "2026-03",
            "cumulative_tax_fen": "4",
            "source": {"source_id": SOURCE},
        },
        "2026-03",
    )
    book.submit(opening(cash="1000"), cost, interest, tax)
    scope = {
        "company_id": book.company,
        "period_from": "2026-01",
        "period_to": "2026-03",
        "view": view,
    }
    with book.app.storage.read_unit(book.company) as read:
        ledger, repo = book.app.ledger, read.repository("ledger")
        expected = ledger.financial_snapshot(scope, read)
        expected_summary = ledger.financial_summary(scope, read)
        expected_sources = {cost["business_id"], interest["business_id"], tax["business_id"]}
        assert expected_sources <= {value["business_id"] for value in expected_summary.sources}
        assert expected_summary.expense == {"state": "known", "amount_fen": "199"}
        assert expected_summary.net_profit == {"state": "known", "amount_fen": "-199"}
        targets, actual_rows, stock_requests = [], [], []
        read_targets, read_lines, read_heads = (
            repo.financial_targets,
            repo.financial_lines,
            repo.financial_heads,
        )

        def target_bodies(ids):
            values = read_targets(ids)
            assert {value["result_id"] for value in values} == set(ids)
            targets.append(set(ids))
            return values

        def integer_lines(ids):
            values = read_lines(ids)
            assert all(
                type(row[field]) is int for row in values for field in ("debit_fen", "credit_fen")
            )
            assert all("summary" not in row for row in values)
            assert [(row["voucher_id"], row["line_no"]) for row in values] == sorted(
                (row["voucher_id"], row["line_no"]) for row in values
            )
            actual_rows.extend(values)
            return values

        def headers(cutoff, *, view, metadata_only=False):
            assert metadata_only
            values = read_heads(cutoff, view=view, metadata_only=True)
            assert all("target_json" not in value and "basis_json" not in value for value in values)
            stock_requests.append(cutoff)
            return values

        def forbidden(*args, **kwargs):
            pytest.fail("金融投影展开了完整凭证或不必要的正式Basis正文")

        monkeypatch.setattr(repo, "financial_targets", target_bodies)
        monkeypatch.setattr(repo, "financial_lines", integer_lines)
        monkeypatch.setattr(repo, "financial_heads", headers)
        monkeypatch.setattr(repo, "vouchers", forbidden)
        monkeypatch.setattr(repo, "results", forbidden)
        actual = ledger.financial_snapshot(scope, read)
        actual_summary = ledger.financial_summary(scope, read)
        needed = {
            row["result_id"]
            for row in repo.financial_heads("2026-03", view=view, metadata_only=True)
            if row["business_id"] in (interest["business_id"], tax["business_id"])
            and row["target_kind"] in ("operations.other_income", "taxes.assessment")
        }
        assert targets == [needed, needed]
        assert stock_requests and actual_rows
        assert actual == expected
        assert asdict(actual_summary) == asdict(expected_summary)

        def typed_text_lines(ids):
            return [
                {
                    **row,
                    "debit_fen": format_fen(row["debit_fen"]),
                    "credit_fen": format_fen(row["credit_fen"]),
                }
                for row in read_lines(ids)
            ]

        monkeypatch.setattr(repo, "financial_lines", typed_text_lines)
        assert ledger.financial_snapshot(scope, read) == expected
        assert asdict(ledger.financial_summary(scope, read)) == asdict(expected_summary)
