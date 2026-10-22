"""Directory selection stays narrow without changing formal history or large repairs."""

import json
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

import pytest
from test_adoption import BUSINESS, COMPANY, adopt_declared, expense, withdrawal
from test_adoption import store as store
from test_financial_core import origin, save
from test_read_basics import scope

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger._read_repository import HEAD_FIELDS
from ai_accounting_modular.ledger.public import ObligationDirectoryQuery, create_ledger


def identity(index):
    return str(uuid5(NAMESPACE_URL, f"directory-selection/{index}"))


def expense_at(index, month):
    change = expense("100", business_id=identity(index))
    change["businesses"][0]["fact"]["period"] = month
    plan = change["plans"][0]
    plan["accounting_period"] = plan["basis"]["period"] = month
    plan["obligations"][0]["business_period"] = month
    for publication in change["publications"]:
        publication["period_from"] = publication["period_to"] = month
    return change


def page_all(query, requested_scope, kinds, read, **filters):
    items, page = [], PageQuery(3)
    while True:
        result = query(requested_scope, kinds, page, read, **filters)
        items.extend(result.items)
        assert result.total_count == result.filtered_count
        if result.next_cursor is None:
            assert len(items) == result.total_count
            return tuple(items)
        page = PageQuery(3, result.next_cursor, result.version)


def plans(read, monkeypatch, query, requested_scope, kinds, **filters):
    repo, statements = read.repository("ledger"), []
    fetch = repo._session.fetchall

    def observe(sql, args=()):
        if sql.startswith((f"SELECT {HEAD_FIELDS} FROM ", f"SELECT {HEAD_FIELDS},CASE WHEN ")) or (
            " FROM voucher v JOIN " in sql
        ):
            statements.append((sql, args))
        return fetch(sql, args)

    with monkeypatch.context() as patched:
        patched.setattr(repo._session, "fetchall", observe)
        result = query(requested_scope, kinds, PageQuery(3), read, **filters)
    assert len(statements) == 2  # Real identity and page queries, not a hand-written stand-in.
    return result, [
        [row[3] for row in read._connection.execute("EXPLAIN QUERY PLAN " + sql, args)]
        for sql, args in statements
    ]


@pytest.mark.parametrize("analyzed", [False, True])
def test_actual_directory_plans_use_kind_period_or_exact_identity(store, monkeypatch, analyzed):
    ledger = create_ledger()
    save(
        store,
        ledger,
        [origin("0")]
        + [expense_at(index, "2026-01" if index < 12 else "2026-02") for index in range(96)],
    )
    if analyzed:
        # Statistics are a test condition, not a product maintenance dependency.
        with store.write_unit(COMPANY) as unit:
            unit._connection.execute("ANALYZE")
    cases = (
        (ledger.query_adopted_directory, scope(), ("investment.cooperation_position",), {}, 0),
        (ledger.query_adopted_directory, scope(view="current"), ("opening.package",), {}, 1),
        (ledger.query_adopted_values, scope(), ("operations.expense",), {}, 12),
        (
            ledger.query_adopted_directory,
            scope(view="current"),
            ("operations.expense",),
            {"business_ids": (identity(0),), "result_slots": ("main",)},
            1,
        ),
        (ledger.query_voucher_directory, scope(), ("operations.expense",), {}, 12),
        (
            ledger.query_voucher_directory,
            scope(view="current"),
            ("operations.expense",),
            {"business_ids": (identity(0),), "roles": ("original",)},
            1,
        ),
    )
    with store.read_unit(COMPANY) as read:
        for query, requested_scope, kinds, filters, count in cases:
            result, actual_plans = plans(
                read, monkeypatch, query, requested_scope, kinds, **filters
            )
            assert result.total_count == count
            for plan in actual_plans:
                assert not any(step.startswith("SCAN r") for step in plan), plan
                assert any("SEARCH r USING INDEX " in step for step in plan), plan
                if query == ledger.query_voucher_directory:
                    # SQLite may enter through either table. Both are narrow:
                    # the exact business, or a real posting-period range.
                    assert any(
                        "adopted_result_business_slot_period" in step
                        or (
                            "adopted_result_kind_posting_period" in step
                            and "posting_period>?" in step
                        )
                        or ("SEARCH v USING INDEX " in step and "period>?" in step)
                        for step in plan
                    ), plan
                elif filters.get("business_ids"):
                    assert any("adopted_result_business_slot_period" in step for step in plan), plan
        # Empty filter tuples mean all kinds; they must not become an empty IN set.
        all_results = page_all(ledger.query_adopted_directory, scope(), (), read)
        assert len(all_results) == 13


def test_directory_history_withdraw_restore_and_voucher_periods_remain_formal(store):
    ledger = create_ledger()
    save(store, ledger, [expense()], freeze="2026-01")
    save(store, ledger, [expense("80000", seq=1)], correction="2026-02", freeze="2026-02")
    with store.write_unit(COMPANY) as unit:
        baseline = ledger.get_result_source(BUSINESS, "main", unit)
        adopt_declared(ledger, [withdrawal(baseline, seq=2)], unit, correction_period="2026-03")
        unit.repository("ledger").freeze_heads("2026-03")
    restored = expense("70000", seq=3)
    restored["businesses"][0]["op"] = "restore"
    save(store, ledger, [restored], correction="2026-04")
    with store.read_unit(COMPANY) as read:
        for month, amount in (("2026-01", "100000"), ("2026-02", "80000"), ("2026-04", "70000")):
            (item,) = page_all(
                ledger.query_adopted_targets, scope(month), ("operations.expense",), read
            )
            assert item.target["values"]["amount_fen"] == amount
            assert item.reference["business_period"] == "2026-01"
            assert item.reference["posting_period"] == month
        (retired,) = page_all(
            ledger.query_adopted_targets, scope("2026-03"), ("operations.expense",), read
        )
        assert retired.target["kind"] == "system.withdrawal"
        (current,) = page_all(
            ledger.query_adopted_targets, scope(view="current"), ("operations.expense",), read
        )
        assert current.target["values"]["amount_fen"] == "70000"
        assert current.reference["posting_period"] == "2026-04"
        (current_flow,) = page_all(
            ledger.query_adopted_values, scope(view="current"), ("operations.expense",), read
        )
        assert current_flow.target["values"]["amount_fen"] == "70000"
        for view in ("current", "as_posted"):
            requested = scope("2026-04", view=view) | {"period_from": "2026-01"}
            vouchers = page_all(
                ledger.query_voucher_directory, requested, ("operations.expense",), read
            )
            assert [item.period for item in vouchers] == [
                "2026-01",
                "2026-02",
                "2026-02",
                "2026-03",
                "2026-04",
            ]
            assert [item.role for item in vouchers] == [
                "original",
                "reversal",
                "replacement",
                "reversal",
                "replacement",
            ]
            assert all(item.period == item.result["posting_period"] for item in vouchers)
        # The explicit cutoff may precede the requested voucher range's end.
        vouchers = page_all(
            ledger.query_voucher_directory,
            scope("2026-04") | {"period_from": "2026-01", "as_of": "2026-02"},
            ("operations.expense",),
            read,
        )
        assert [item.period for item in vouchers] == ["2026-01", "2026-02", "2026-02"]


def test_positive_pages_and_old_directory_versions_are_not_reused(store):
    ledger = create_ledger()
    save(store, ledger, [expense()] + [expense_at(index, "2026-01") for index in range(7)])
    with store.read_unit(COMPANY) as read:
        for query in (ledger.query_adopted_directory, ledger.query_voucher_directory):
            items = page_all(query, scope(), ("operations.expense",), read)
            assert len(items) == 8
            keys = [
                item.reference["result_id"] if hasattr(item, "reference") else item.voucher_id
                for item in items
            ]
            assert len(set(keys)) == 8
        old = [
            query(scope(), ("operations.expense",), PageQuery(3), read)
            for query in (ledger.query_adopted_directory, ledger.query_voucher_directory)
        ]
    save(store, ledger, [expense("90000", seq=1)])
    with store.read_unit(COMPANY) as read:
        for query, prior in zip(
            (ledger.query_adopted_directory, ledger.query_voucher_directory), old, strict=True
        ):
            with pytest.raises(ContractError) as error:
                query(
                    scope(),
                    ("operations.expense",),
                    PageQuery(3, prior.next_cursor, prior.version),
                    read,
                )
            assert error.value.code == "snapshot_changed"


def test_all_three_bounded_filters_accept_500_values_and_keep_known_result(store):
    ledger = create_ledger()
    save(store, ledger, [expense()])
    kinds = ("operations.expense",) + tuple(f"unused.kind{index}" for index in range(499))
    businesses = (BUSINESS,) + tuple(identity(index) for index in range(499))
    slots = ("main",) + tuple(f"unused_{index}" for index in range(499))
    with store.read_unit(COMPANY) as read:
        page = ledger.query_adopted_directory(
            scope(), kinds, PageQuery(), read, business_ids=businesses, result_slots=slots
        )
        assert page.total_count == 1 and page.items[0].reference["business_id"] == BUSINESS
        # Voucher roles have five allowed values, not 500 invented roles.
        vouchers = ledger.query_voucher_directory(
            scope(),
            kinds,
            PageQuery(),
            read,
            business_ids=businesses,
            roles=("original", "reversal", "replacement", "reclassification", "closing"),
        )
        assert vouchers.total_count == 1 and vouchers.items[0].role == "original"


def test_large_repair_business_selection_retains_json_parameters(store, monkeypatch):
    ledger = create_ledger()
    save(store, ledger, [expense()])
    query = ObligationDirectoryQuery("2026-01", "2026-01", False)
    with store.read_unit(COMPANY) as read:
        expected = ledger.capture_obligation_directory(query, read)
        repo, statements = read.repository("ledger"), []
        fetch = repo._session.fetchall

        def observe(sql, args=()):
            statements.append((sql, args))
            return fetch(sql, args)

        monkeypatch.setattr(repo._session, "fetchall", observe)
        selected = (BUSINESS,) + tuple(identity(index) for index in range(10999))
        actual = ledger.capture_obligation_directory(replace(query, business_ids=selected), read)
    assert actual == expected and len(actual.selected) == 1
    assert len(statements) == 1
    sql, args = statements[0]
    assert sql.count("json_each(?)") >= 3
    assert len(args) < 20
    assert (
        sum(
            isinstance(arg, str) and arg.startswith("[") and json.loads(arg) == list(selected)
            for arg in args
        )
        == 3
    )
