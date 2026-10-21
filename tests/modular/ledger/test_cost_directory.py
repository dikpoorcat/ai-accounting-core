"""Cost owners read their actual sources without requesting global statistics."""

import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest
import test_adoption
from test_adoption import BANK, BUSINESS, COMPANY, SUPPLIER, adopt_declared, seed_payment
from test_aggregations import at_period
from test_resource_indexes import split_resources

from ai_accounting_modular.contracts.public import ContractError, QuerySelection
from ai_accounting_modular.ledger import _read_positions
from ai_accounting_modular.ledger.public import CostPositionQuery, create_ledger
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory


@pytest.fixture(params=MODES)
def implementation(request, monkeypatch):
    factory = repository_factory(request.param)
    monkeypatch.setattr(test_adoption, "repository_factory", lambda: factory)


@pytest.fixture
def store(implementation, request, tmp_path):
    return test_adoption.store.__wrapped__(tmp_path, request)


def mixed(store):
    ledger = create_ledger()
    seed_payment(store)
    source, consumer = split_resources(33)
    for position, lot in enumerate(source["plans"][0]["cost_lots"]):
        lot["category"] = "project" if position < 10 else "money_fund"
        lot["object_id"] = BANK if position == 9 else SUPPLIER
    consumer["plans"][0]["cost_consumptions"][0]["amount_fen"] = "100"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [source, consumer], unit)
    return ledger, source


@pytest.mark.parametrize(
    "changes,count,remaining",
    [
        ({"categories": ("project",)}, 10, 540),
        ({"categories": ("project",), "include_consumed": False}, 9, 540),
        ({"categories": ("project",), "object_ids": (BANK,)}, 1, 60),
        ({"object_ids": (SUPPLIER,)}, 32, 1860),
        ({"categories": ("money_fund",), "business_ids": (BUSINESS,)}, 23, 1380),
        ({"categories": ("not_recorded",)}, 0, 0),
        ({}, 33, 1920),
    ],
)
def test_directory_equals_complete_selected_rows_without_changing_wide_totals(
    store, changes, count, remaining
):
    ledger, _ = mixed(store)
    query = replace(CostPositionQuery("2026-01"), **changes)
    with store.read_unit(COMPANY) as read:
        wide = ledger.capture_cost_positions(query, read)
        actual = ledger.capture_cost_directory(query, read)
        assert type(actual) is tuple and actual == wide.items
        assert len(actual) == wide.filtered_count == count
        assert wide.total_count == 33
        assert sum(item.amounts.remaining_fen for item in actual) == remaining
        assert sum(item.amounts.amount_fen for item in wide.full_totals) == 3300
        if actual:
            actual[0].definition.value["amount_fen"] = "1"
            assert (
                ledger.capture_cost_directory(query, read)[0].definition.value["amount_fen"]
                == "100"
            )


def test_directory_filters_actual_adoption_before_selecting_costs_and_uses(store):
    ledger, source = mixed(store)
    with store.write_unit(COMPANY) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    changed = deepcopy(source)
    changed["businesses"][0].update(expected_seq=1, op="replace")
    for lot in changed["plans"][0]["cost_lots"]:
        lot["category"] = "project"
        lot["object_id"] = SUPPLIER
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [changed], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        query = CostPositionQuery("2026-02", categories=("money_fund",))
        earlier = QuerySelection("as_posted", "2026-01")
        frozen = ledger.capture_cost_directory(query, read, selection=earlier)
        assert len(frozen) == 23
        assert all(item.amounts.remaining_fen == 60 for item in frozen)
        assert ledger.capture_cost_directory(query, read, selection=QuerySelection("current")) == ()
        project = replace(query, categories=("project",))
        for selection, count in ((earlier, 10), (QuerySelection("as_posted", "2026-02"), 33)):
            actual = ledger.capture_cost_directory(project, read, selection=selection)
            assert actual == ledger.capture_cost_positions(project, read, selection=selection).items
            assert len(actual) == count
        current = ledger.capture_cost_directory(project, read)
        assert current[0].definition.source != frozen[0].definition.source
        assert sum(item.amounts.consumed_fen for item in current) == 1380
        old_object = replace(project, object_ids=(BANK,))
        assert len(ledger.capture_cost_directory(old_object, read, selection=earlier)) == 1
        assert ledger.capture_cost_directory(old_object, read) == ()


def test_open_replacement_and_physical_withdrawal_leave_no_old_category_or_use(store):
    ledger, source = mixed(store)
    changed = deepcopy(source)
    changed["businesses"][0].update(expected_seq=1, op="replace")
    for lot in changed["plans"][0]["cost_lots"]:
        lot["category"] = "project"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [changed], unit)
    with store.read_unit(COMPANY) as read:
        assert (
            ledger.capture_cost_directory(
                CostPositionQuery("2026-01", categories=("money_fund",)), read
            )
            == ()
        )
        current = ledger.capture_cost_directory(CostPositionQuery("2026-01"), read)
        assert len(current) == 33
        assert sum(item.amounts.remaining_fen for item in current) == 1920
    _, consumer = split_resources(33)

    def retire(change, seq):
        change = deepcopy(change)
        change["publications"] = change["plans"] = []
        change["businesses"][0].pop("fact")
        change["businesses"][0].update(expected_seq=seq, op="withdraw", complete_result_slots=[])
        return change

    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [retire(consumer, 1)], unit)
    with store.read_unit(COMPANY) as read:
        remaining = ledger.capture_cost_directory(CostPositionQuery("2026-01"), read)
        assert len(remaining) == 33
        assert all(item.amounts.remaining_fen == 100 for item in remaining)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [retire(changed, 2)], unit)
    with store.read_unit(COMPANY) as read:
        assert ledger.capture_cost_directory(CostPositionQuery("2026-01"), read) == ()


def test_directory_only_hydrates_matching_rows_and_never_reads_formal_bodies(store, monkeypatch):
    ledger, _ = mixed(store)
    query = CostPositionQuery("2026-01", categories=("project",), object_ids=(BANK,))
    counted, amounts = [], _read_positions._amounts

    def count(row, kind):
        counted.append((row["category"], row["object_id"]))
        return amounts(row, kind)

    monkeypatch.setattr(_read_positions, "_amounts", count)
    with store.read_unit(COMPANY) as read:
        repository = read.repository("ledger")
        connection, fetch, statements = read._connection, repository._session.fetchall, []

        class ObservedConnection:
            def __getattr__(self, name):
                return getattr(connection, name)

            def set_authorizer(self, callback):
                def authorize(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_READ and column.endswith("_json"):
                        return sqlite3.SQLITE_DENY
                    return callback(action, table, column, database, trigger)

                connection.set_authorizer(authorize if callback is not None else None)

        def inspect(sql, args=()):
            result = fetch(sql, args)
            statements.append((sql, len(result)))
            return result

        monkeypatch.setattr(read, "_connection", ObservedConnection())
        monkeypatch.setattr(repository._session, "fetchall", inspect)
        actual = ledger.capture_cost_directory(query, read)
        assert len(actual) == 1 and actual[0].amounts.remaining_fen == 60
        assert counted == [("project", BANK)]
        assert len(statements) == 1 and statements[0][1] == 1
        assert "FROM candidates" in statements[0][0]
        assert "c.source_business_id,c.source_public_slot" in statements[0][0]


@pytest.mark.parametrize(
    "query",
    [
        None,
        CostPositionQuery("2026-13"),
        CostPositionQuery("2026-01", business_ids=("not-an-id",)),
        CostPositionQuery("2026-01", object_ids=("not-an-id",)),
        CostPositionQuery("2026-01", categories=(True,)),
        CostPositionQuery("2026-01", include_consumed=1),
    ],
)
def test_directory_rejects_invalid_queries_without_replacing_them_with_defaults(store, query):
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError) as error:
            create_ledger().capture_cost_directory(query, read)
        assert error.value.code == "invalid_input"


def test_directory_does_not_invent_empty_or_future_costs(store):
    ledger = create_ledger()
    with store.read_unit(COMPANY) as read:
        assert ledger.capture_cost_directory(CostPositionQuery("2026-01"), read) == ()
    source, _ = split_resources(1)
    at_period(source, "2026-02")
    source["plans"][0]["cost_lots"][0]["business_period"] = "2026-02"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [source], unit)
    with store.read_unit(COMPANY) as read:
        assert ledger.capture_cost_directory(CostPositionQuery("2026-01"), read) == ()
        actual = ledger.capture_cost_directory(CostPositionQuery("2026-02"), read)
        assert len(actual) == 1
