"""Profit selectors narrow formal identities without changing complete tax or baseline semantics."""

import pytest
from test_adoption import BUSINESS, COMPANY, adopt_declared, withdrawal
from test_adoption import store as store
from test_aggregations import journal
from test_financial_core import save
from test_read_basics import scope

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger.public import create_ledger
from tests.modular.domains.financing._support import assemble, request
from tests.modular.domains.taxes._fixtures import evidence
from tests.modular.domains.taxes.test_investment_tax_application import inputs


@pytest.fixture
def taxed_profit(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    body = request(company, *inputs())
    body["sources"].append(evidence())
    actual = app.accounting_apply(body)
    assert actual["completion"] == "complete", actual
    return app, {
        "company_id": company,
        "period_from": "2026-07",
        "period_to": "2026-07",
        "view": "as_posted",
    }


def test_default_profit_stays_complete_while_selected_ids_pages_and_journals_exclude_tax(
    taxed_profit, monkeypatch
):
    app, selected_scope = taxed_profit
    with app.storage.read_unit(selected_scope["company_id"]) as read:
        repo = read.repository("ledger")
        full = app.ledger.profit_contributions(selected_scope, PageQuery(), read)
        assert full == app.ledger.profit_contributions(
            selected_scope, PageQuery(), read, business_kinds=()
        )
        assert full.total_count == full.filtered_count == 2
        assert {item.business_kind for item in full.items} == {
            "investment.dispose",
            "taxes.financial_transfer_basis",
        }
        assert sum(item.investment_income_fen for item in full.items) == 100000
        load, loaded = type(repo).read_profit_lines, []

        def lines(owner, ids, accounts):
            values = load(owner, ids, accounts)
            loaded.extend(values)
            return values

        monkeypatch.setattr(type(repo), "read_profit_lines", lines)
        monkeypatch.setattr(
            type(repo), "results", lambda *a: pytest.fail("投资损益读取了完整正式正文")
        )
        narrow = app.ledger.profit_contributions(
            selected_scope, PageQuery(), read, business_kinds=("investment.dispose",)
        )
        assert narrow.total_count == narrow.filtered_count == len(narrow.items) == 1
        assert narrow.items[0].business_kind == "investment.dispose"
        assert narrow.items[0].investment_income_fen == 102000
        assert [(row["debit_fen"], row["credit_fen"]) for row in loaded] == [(0, 102000)]
        assert (
            app.ledger.profit_contributions(
                selected_scope, PageQuery(), read, business_kinds=("not_present",)
            ).items
            == ()
        )


def test_selector_is_bound_to_cursor_expected_version_and_expected_head(taxed_profit):
    app, selected_scope = taxed_profit
    with app.storage.read_unit(selected_scope["company_id"]) as read:
        first = app.ledger.profit_contributions(selected_scope, PageQuery(1), read)
        assert first.next_cursor
        both_kinds = ("investment.dispose", "taxes.financial_transfer_basis")
        for query, page, kinds in (
            (selected_scope, PageQuery(1, first.next_cursor), ("investment.dispose",)),
            (selected_scope, PageQuery(1, expected_version=first.version), both_kinds),
            (selected_scope | {"expected_head": first.version}, PageQuery(), both_kinds),
        ):
            with pytest.raises(ContractError) as error:
                app.ledger.profit_contributions(query, page, read, business_kinds=kinds)
            assert error.value.code == "snapshot_changed"


@pytest.mark.parametrize("kinds", [None, "investment.dispose", (True,), ("",), ("x", "x")])
def test_profit_selector_uses_existing_bounded_validation(store, kinds):
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            create_ledger().profit_contributions(scope(), PageQuery(), read, business_kinds=kinds)
        assert error.value.code == "invalid_input"


def test_withdrawal_keeps_original_kind_and_frozen_baseline_under_selector(store):
    ledger = create_ledger()
    save(store, ledger, [journal(BUSINESS, [("1002", 30), ("5111", -30)])], freeze="2026-01")
    with store.write_unit(COMPANY) as unit:
        original = ledger.get_result_source(BUSINESS, "main", unit)
        adopt_declared(ledger, [withdrawal(original)], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        full = ledger.profit_contributions(scope("2026-02"), PageQuery(), read)
        selected = ledger.profit_contributions(
            scope("2026-02"), PageQuery(), read, business_kinds=("operations.expense",)
        )
        assert selected.items == full.items and len(selected.items) == 1
        item = selected.items[0]
        assert item.business_kind == "operations.expense"
        assert (item.investment_income_fen, item.baseline_income_fen) == (0, 30)
        assert item.reference["baseline_result_id"] == item.baseline_reference["result_id"]
        assert item.baseline_source == original
