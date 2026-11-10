"""Fixed-contract holding selection precedes paging and validated target loading."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    canonical_json,
    content_digest,
    loads_json,
)
from ai_accounting_modular.ledger.public import AdoptedTarget
from tests.modular.domains.financing._investment_support import acquire, dispose, terms
from tests.modular.domains.financing._support import assemble, change, request


@pytest.fixture
def holdings(tmp_path):
    app, company = assemble(tmp_path)
    active, closed, zero_book = terms(), terms(), terms()
    partial_exit = dispose(zero_book, units="2", price="1")
    actual = app.accounting_apply(
        request(
            company,
            active,
            acquire(active),
            closed,
            acquire(closed),
            dispose(closed, units="3", price="100"),
            zero_book,
            acquire(zero_book, value="1"),
            partial_exit,
        )
    )
    assert actual["completion"] == "complete", actual
    return app, company, active, closed, zero_book, partial_exit


def scope(company, *, view="current", **fields):
    return {
        "company_id": company,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": view,
        **fields,
    }


def test_only_selected_holdings_are_counted_paged_and_loaded_as_complete_targets(
    holdings, monkeypatch
):
    app, company, active, _, zero_book, _ = holdings
    with app.storage.read_unit(company) as read:
        repo = read.repository("ledger")
        fetch, loaded = type(repo).financial_results, []
        identity_rows, identity_counts = type(repo).read_identity_rows, []

        def identities(owner, *args):
            rows = identity_rows(owner, *args)
            identity_counts.append(len(rows))
            return rows

        def targets(owner, ids):
            loaded.append(set(ids))
            rows = fetch(owner, ids)
            assert all("basis_json" not in row for row in rows)
            return rows

        monkeypatch.setattr(type(repo), "financial_results", targets)
        monkeypatch.setattr(type(repo), "read_identity_rows", identities)
        for method in ("results", "vouchers", "children", "current_heads", "heads_through"):
            monkeypatch.setattr(type(repo), method, lambda *a, **kw: pytest.fail("读取无关正文"))
        first = app.ledger.query_investment_holdings(scope(company), PageQuery(1), read)
        assert first.total_count == first.filtered_count == 2 and first.next_cursor
        assert isinstance(first.items[0], AdoptedTarget)
        assert loaded == [{first.items[0].reference["result_id"]}]
        assert identity_counts == [2]
        second = app.ledger.query_investment_holdings(
            scope(company), PageQuery(1, first.next_cursor, first.version), read
        )
        assert second.next_cursor is None and second.total_count == 2
        items = (*first.items, *second.items)
        assert {item.reference["business_id"] for item in items} == {
            active["business_id"],
            zero_book["business_id"],
        }
        assert all(item.business_kind == "investment.position" for item in items)
        assert all(item.reference["result_slot"] == "position:2026-01" for item in items)
        assert all(item.source["result_id"] == item.reference["result_id"] for item in items)
        zero = next(
            item for item in items if item.reference["business_id"] == zero_book["business_id"]
        )
        assert zero.target["values"]["closing_cost_fen"] == "0"
        assert zero.target["values"]["quantity"] == "1"
        assert len(loaded) == 2 and loaded[-1] == {second.items[0].reference["result_id"]}


def test_holdings_cursor_is_separate_from_full_targets_and_checks_expected_head(holdings):
    app, company, *_ = holdings
    with app.storage.read_unit(company) as read:
        first = app.ledger.query_investment_holdings(scope(company), PageQuery(1), read)
        full = app.ledger.query_adopted_targets(
            scope(company),
            ("investment.position",),
            PageQuery(1),
            read,
            result_slots=("position:2026-01",),
        )
        assert full.total_count == 3 and first.total_count == 2
        for query, page in (
            (scope(company), PageQuery(1, full.next_cursor, full.version)),
            (scope(company, expected_head="0" * 64), PageQuery()),
            (scope(company, view="as_posted"), PageQuery(1, first.next_cursor, first.version)),
        ):
            with pytest.raises(ContractError) as error:
                app.ledger.query_investment_holdings(query, page, read)
            assert error.value.code == "snapshot_changed"


def test_holdings_preserve_original_frozen_positions_after_later_zero_replacement(holdings):
    app, company, active, _, zero_book, partial_exit = holdings
    with app.storage.write_unit(company) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    changed = deepcopy(partial_exit)
    changed["data"]["quantity"] = "3"
    actual = app.accounting_apply(change(company, changed, correction_period="2026-02"))
    assert actual["completion"] == "complete", actual
    with app.storage.read_unit(company) as read:
        frozen = app.ledger.query_investment_holdings(
            scope(company, view="as_posted"), PageQuery(), read
        )
        current = app.ledger.query_investment_holdings(scope(company), PageQuery(), read)
        later = app.ledger.query_investment_holdings(
            scope(company, view="as_posted", as_of="2026-02"), PageQuery(), read
        )
        assert {item.reference["business_id"] for item in frozen.items} == {
            active["business_id"],
            zero_book["business_id"],
        }
        assert {item.reference["business_id"] for item in current.items} == {active["business_id"]}
        assert current.items == later.items and frozen.version != current.version


def _change_position_value(app, company, agreement, field, value):
    with app.storage.write_unit(company) as unit:
        repo = unit.repository("ledger")
        row = repo._session.fetchone(
            "SELECT result_id,target_json FROM adopted_result "
            "WHERE business_id=? AND result_slot=?",
            (agreement["business_id"], "position:2026-01"),
        )
        target = loads_json(row["target_json"])
        if value == "missing":
            target["values"].pop(field)
        else:
            target["values"][field] = value
        repo._session.execute(
            "UPDATE adopted_result SET target_json=?,target_digest=? WHERE result_id=?",
            (canonical_json(target), content_digest(target), row["result_id"]),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", None),
        ("quantity", "missing"),
        ("quantity", "0e0"),
        ("closing_cost_fen", None),
        ("closing_cost_fen", 0),
        ("opening_cost_fen", "9223372036854775808"),
    ],
)
def test_missing_unknown_or_damaged_values_are_not_swallowed_by_zero_filter(holdings, field, value):
    app, company, _, closed, *_ = holdings
    _change_position_value(app, company, closed, field, value)
    with app.storage.read_unit(company) as read:
        with pytest.raises(ContractError) as error:
            app.ledger.query_investment_holdings(scope(company), PageQuery(), read)
        assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("quantity", ["0.000000000000000001", "0.000"])
def test_quantity_uses_no_float_conversion_and_noncanonical_zero_is_conservative(
    holdings, quantity
):
    app, company, _, closed, *_ = holdings
    _change_position_value(app, company, closed, "quantity", quantity)
    with app.storage.read_unit(company) as read:
        actual = app.ledger.query_investment_holdings(scope(company), PageQuery(), read)
        item = next(
            item for item in actual.items if item.reference["business_id"] == closed["business_id"]
        )
        assert item.target["values"]["quantity"] == quantity
