"""Shared assembly accepts owner calculations without forcing complete Source enumeration."""

from dataclasses import asdict
from types import SimpleNamespace

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.ledger._financial import normalize
from ai_accounting_modular.ledger._financial_calculation import (
    ClassicCalculation,
    calculate_snapshot,
    summary_values,
)
from ai_accounting_modular.ledger._financial_cells import Issues
from ai_accounting_modular.ledger._financial_selection import Selection


class BoundaryOnly:
    def __init__(self, selected):
        self._selected = selected
        self.issues = selected.issues

    def __getattr__(self, name):
        if name not in {"prior", "opening", "continuation", "view"}:
            pytest.fail(f"the amount assembler attempted to enumerate selection.{name}")
        return getattr(self._selected, name)


def context(read):
    # No repository/SQL handle is available to the shared assembler.
    return SimpleNamespace(
        company_id=read.company_id,
        database_id=read.database_id,
        head=read.head,
        start_period=read.start_period,
    )


def selection(read, scope):
    return Selection(
        read.repository("ledger"), scope["view"], scope["period_to"], read.start_period, Issues()
    )


@pytest.mark.parametrize("view", ["current", "as_posted"])
@pytest.mark.parametrize("opening_known", [False, True])
@pytest.mark.parametrize("quarter", [False, True])
def test_complete_public_values_and_shared_assembly_agree_without_sources_or_repository(
    tmp_path, view, opening_known, quarter
):
    book = support.assemble(tmp_path)
    cost = support.expense("assembly-cost", "500", details=opening_known)
    book.submit(*([support.opening(cash="1000")] if opening_known else []), cost)
    scope = {
        "company_id": book.company,
        "period_from": "2026-01",
        "period_to": "2026-03" if quarter else "2026-01",
        "view": view,
    }
    with book.app.storage.read_unit(book.company) as read:
        normalized = normalize(scope, read)
        expected = book.app.ledger.financial_snapshot(scope, read)
        selected = selection(read, normalized)
        actual = calculate_snapshot(
            normalized,
            context(read),
            BoundaryOnly(selected),
            calculation=ClassicCalculation(selected),
        )
        assert actual == {key: value for key, value in expected.items() if key != "selection"}
        assert "selection" not in actual and "sources" not in actual
        summary = asdict(book.app.ledger.financial_summary(scope, read))
        selected = selection(read, normalized)
        actual = summary_values(
            normalized, BoundaryOnly(selected), calculation=ClassicCalculation(selected)
        )
        assert actual == {
            key: value
            for key, value in summary.items()
            if key not in {"version", "scope", "issues", "sources"}
        }
        assert selected.issues.values == list(summary["issues"])


def test_saved_summary_tables_are_authoritative_and_only_live_account_fields_are_computed(tmp_path):
    book = support.assemble(tmp_path)
    book.submit(support.opening(), support.expense("closed-cost", "500"))
    with book.app.storage.write_unit(book.company) as unit:
        checked = book.app.ledger.check_period("2026-01", unit)
        book.app.ledger.close_period("2026-01", checked.head, unit)
    scope = {
        "company_id": book.company,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "as_posted",
    }
    with book.app.storage.read_unit(book.company) as read:
        normalized = normalize(scope, read)
        saved = book.app.ledger.financial_snapshot(scope, read)
        expected = asdict(book.app.ledger.financial_summary(scope, read))
        selected = selection(read, normalized)
        classic = ClassicCalculation(selected)

        class SavedAccountFields:
            amount = classic.amount

            def __getattr__(self, name):
                pytest.fail(f"saved tables must not be recomputed through {name}")

        actual = summary_values(
            normalized, BoundaryOnly(selected), saved=saved, calculation=SavedAccountFields()
        )
        assert actual == {
            key: value
            for key, value in expected.items()
            if key not in {"version", "scope", "issues", "sources"}
        }
        assert selected.issues.values == list(expected["issues"])
