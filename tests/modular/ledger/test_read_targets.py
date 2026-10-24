"""Selected target pages expose roster facts without loading historical bases."""

from uuid import uuid4

import pytest
from test_adoption import BUSINESS, COMPANY, adopt_declared, expense, withdrawal
from test_adoption import store as store
from test_financial_core import origin, save
from test_read_basics import scope

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger.public import AdoptedTarget, create_ledger


def test_target_page_reads_only_its_targets_and_never_calculation_bases(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), expense(str(2**53 + 1))])
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        original, loaded = type(repo).financial_results, []

        def targets(owner, ids):
            loaded.append(set(ids))
            rows = original(owner, ids)
            assert all("basis_json" not in row for row in rows)
            return rows

        monkeypatch.setattr(type(repo), "financial_results", targets)
        for method in ("results", "vouchers", "children", "current_heads", "heads_through"):
            monkeypatch.setattr(type(repo), method, lambda *a, **kw: pytest.fail("读取了无关正文"))
        first = ledger.query_adopted_targets(scope(), (), PageQuery(1), read)
        assert first.total_count == 2 and len(first.items) == 1 and first.next_cursor
        assert isinstance(first.items[0], AdoptedTarget)
        assert loaded == [{first.items[0].reference["result_id"]}]
        second = ledger.query_adopted_targets(
            scope(), (), PageQuery(1, first.next_cursor, first.version), read
        )
        assert second.total_count == 2 and second.next_cursor is None
        items = (*first.items, *second.items)
        actual = next(item for item in items if item.business_kind == "operations.expense")
        assert actual.target["values"]["amount_fen"] == "9007199254740993"
        assert loaded[-1] == {second.items[0].reference["result_id"]}


def test_target_directory_selects_one_adoption_at_cutoff_and_preserves_withdrawal(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense("80000", seq=1)], unit, correction_period="2026-02")
        unit.repository("ledger").freeze_heads("2026-02")
    with store.write_unit(COMPANY) as unit:
        baseline = ledger.get_result_source(BUSINESS, "main", unit)
        adopt_declared(ledger, [withdrawal(baseline)], unit, correction_period="2026-03")
    with store.read_unit(COMPANY) as read:
        jan = ledger.query_adopted_targets(scope(), (), PageQuery(), read)
        feb = ledger.query_adopted_targets(scope("2026-02"), (), PageQuery(), read)
        mar = ledger.query_adopted_targets(scope("2026-03"), (), PageQuery(), read)
    assert jan.total_count == feb.total_count == mar.total_count == 1
    assert jan.items[0].target["values"]["amount_fen"] == "100000"
    assert feb.items[0].target["values"]["amount_fen"] == "80000"
    assert mar.items[0].business_kind == "operations.expense"
    assert mar.items[0].target["kind"] == "system.withdrawal"


def test_target_cursor_is_not_a_directory_cursor_and_scope_version_is_checked(store):
    ledger = create_ledger()
    save(store, ledger, [origin("0"), expense()])
    with store.read_unit(COMPANY) as read:
        directory = ledger.query_adopted_directory(scope(), (), PageQuery(1), read)
        with pytest.raises(ContractError) as error:
            ledger.query_adopted_targets(
                scope(), (), PageQuery(1, directory.next_cursor, directory.version), read
            )
        assert error.value.code == "snapshot_changed"

        with pytest.raises(ContractError) as error:
            ledger.query_adopted_targets(
                scope() | {"expected_head": "0" * 64}, (), PageQuery(), read
            )
        assert error.value.code == "snapshot_changed"


def test_target_source_filter_selects_only_actual_formal_consumers_and_binds_cursor(store):
    ledger = create_ledger()
    consumers = [str(uuid4()), str(uuid4())]
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        source = ledger.get_result_source(BUSINESS, "main", unit)
        for identity in consumers:
            unit.repository("application").business(identity)
            linked = expense("123", business_id=identity)
            linked["plans"][0]["basis"]["source_results"] = [source]
            adopt_declared(ledger, [linked], unit)
    ref = {"business_id": BUSINESS, "slot": "main"}
    with store.read_unit(COMPANY) as read:
        first = ledger.query_adopted_targets(
            scope(), ("operations.expense",), PageQuery(1), read, source_refs=(ref,)
        )
        assert first.total_count == 2 and first.next_cursor
        last = ledger.query_adopted_targets(
            scope(),
            ("operations.expense",),
            PageQuery(1, first.next_cursor, first.version),
            read,
            source_refs=(ref,),
        )
        assert {item.reference["business_id"] for item in (*first.items, *last.items)} == set(
            consumers
        )
        assert last.next_cursor is None
        assert (
            ledger.query_adopted_targets(
                scope(), (), PageQuery(), read, source_refs=({**ref, "slot": "another"},)
            ).total_count
            == 0
        )
        with pytest.raises(ContractError) as error:
            ledger.query_adopted_targets(
                scope(),
                ("operations.expense",),
                PageQuery(1, first.next_cursor, first.version),
                read,
            )
        assert error.value.code == "snapshot_changed"
        with pytest.raises(ContractError) as error:
            ledger.query_adopted_targets(scope(), (), PageQuery(), read, source_refs=(ref, ref))
        assert error.value.code == "invalid_input"


def test_target_source_filter_keeps_frozen_basis_after_current_consumer_withdrawal(store):
    ledger = create_ledger()
    consumer = str(uuid4())
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        source = ledger.get_result_source(BUSINESS, "main", unit)
        unit.repository("application").business(consumer)
        linked = expense("123", business_id=consumer)
        linked["plans"][0]["basis"]["source_results"] = [source]
        adopt_declared(ledger, [linked], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        baseline = ledger.get_result_source(consumer, "main", unit)
        withdrawn = withdrawal(baseline)
        for item in (*withdrawn["businesses"], *withdrawn["plans"], *withdrawn["publications"]):
            item["business_id"] = consumer
        adopt_declared(ledger, [withdrawn], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        selected = ledger.query_adopted_targets(
            scope(),
            ("operations.expense",),
            PageQuery(),
            read,
            source_refs=({"business_id": BUSINESS, "slot": "main"},),
        )
    assert selected.total_count == 1
    assert selected.items[0].reference["business_id"] == consumer
    assert selected.items[0].target["values"]["amount_fen"] == "123"
