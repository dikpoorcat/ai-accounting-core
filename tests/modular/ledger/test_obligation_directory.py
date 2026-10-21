"""A light directory preserves exact posted/current resources without wide-page statistics."""

from dataclasses import replace

import pytest
from test_adoption import (
    BUSINESS,
    COMPANY,
    _payment_at,
    adopt_declared,
    expense,
    seed_payment,
    withdrawal,
)
from test_adoption import store as store
from test_resource_indexes import split_resources

from ai_accounting_modular.contracts.public import ContractError, QuerySelection
from ai_accounting_modular.ledger._read_repository import ReadRepository
from ai_accounting_modular.ledger.public import (
    ObligationDirectoryQuery,
    ObligationPositionQuery,
    create_ledger,
)


def query(month="2026-01", current=None, **fields):
    return ObligationDirectoryQuery(month, current or month, False, **fields)


def by_ref(items):
    return {(item.reference["business_id"], item.reference["slot"]): item for item in items}


def assert_same(actual, expected):
    assert actual.reference == expected.definition.reference
    assert actual.source == expected.definition.source
    assert actual.amounts == expected.amounts
    for name in ("direction", "category", "business_period", "party_id"):
        assert getattr(actual, name) == expected.definition.value.get(name)


def test_directory_captures_501_resources_once_and_reuses_one_checked_source(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [split_resources(501)[0]], unit)
    calls = []
    method = ReadRepository.read_obligation_directory_rows

    def observed(*args, **kwargs):
        calls.append((args[2], kwargs))
        return method(*args, **kwargs)

    monkeypatch.setattr(ReadRepository, "read_obligation_directory_rows", observed)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        monkeypatch.setattr(
            repo, "read_origin_rows", lambda *a: pytest.fail("未请求来源却读了正文")
        )
        actual = ledger.capture_obligation_directory(query(), read)
        expected = ledger.get_obligation_positions("2026-01", read, business_ids=(BUSINESS,))
    assert len(calls) == 1 and calls[0][0] == QuerySelection("current")
    assert len(actual.selected) == 501 and len(actual.sources) == 1
    assert actual.current is actual.selected and actual.origins is None
    assert all(item.source is actual.sources[0].source for item in actual.selected)
    expected = {
        (item.definition.reference["business_id"], item.definition.reference["slot"]): item
        for item in expected
    }
    for identity, item in by_ref(actual.selected).items():
        assert_same(item, expected[identity])


def test_current_is_exact_selected_resource_even_after_category_change(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    revised = expense("80000", seq=1)
    revised["plans"][0]["obligations"][0]["category"] = "reimbursement"
    revised["plans"][0]["obligations"][0]["direction"] = "receivable"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [revised], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        actual = ledger.capture_obligation_directory(
            query(current="2026-02", categories=("trade",), direction="payable"), read
        )
        forced = ledger.capture_obligation_directory(query(categories=("trade",)), read)
        assert len(actual.selected) == len(actual.current) == 1
        assert actual.selected[0].category == "trade"
        assert actual.current[0].category == "reimbursement"
        assert actual.current[0].direction == "receivable"
        assert actual.selected[0].amounts.remaining_fen == 100000
        assert actual.current[0].amounts.remaining_fen == 80000
        assert actual.selected[0].source != actual.current[0].source
        # A real later adoption prevents merging even when both economic cutoffs equal January.
        assert forced.selected[0].amounts.remaining_fen == 100000
        assert forced.current[0].amounts.remaining_fen == 80000


def test_scope_filters_exclude_unrelated_resources_and_current_siblings(store):
    ledger = create_ledger()
    changes = split_resources(3)[0]
    changes["plans"][0]["obligations"][0]["category"] = "reimbursement"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [changes], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    revised = split_resources(3)[0]
    revised["businesses"][0].update(expected_seq=1, op="replace")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [revised], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        actual = ledger.capture_obligation_directory(
            query(current="2026-02", categories=("reimbursement",), direction="payable"), read
        )
        none = ledger.capture_obligation_directory(query(direction="receivable"), read)
    assert len(actual.selected) == len(actual.current) == 1
    assert actual.selected[0].reference["slot"] == "payable:0"
    assert actual.current[0].category == "trade"
    assert none.selected == none.current == none.sources == ()


def test_current_history_discovers_new_resources_instead_of_matching_only_posted_ones(
    store, monkeypatch
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [split_resources(1)[0]], unit)
        unit.repository("ledger").freeze_heads("2026-01")
    revised = split_resources(2)[0]
    revised["businesses"][0].update(expected_seq=1, op="replace")
    revised["plans"][0]["obligations"][0]["category"] = "reimbursement"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [revised], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        posted = ledger.capture_obligation_directory(query(categories=("trade",)), read)
        expected = ledger.capture_obligation_positions(
            ObligationPositionQuery(
                "2026-01", categories=("trade",), direction="payable", include_settled=False
            ),
            read,
            selection=QuerySelection("current"),
        ).items
        repo = read.repository("ledger")
        method, calls = repo.read_obligation_directory_rows, []

        def observed(*args, **kwargs):
            calls.append((args, kwargs))
            return method(*args, **kwargs)

        monkeypatch.setattr(repo, "read_obligation_directory_rows", observed)
        monkeypatch.setattr(
            repo, "has_adoptions_after", lambda *a: pytest.fail("当前同截止无需frontier证明")
        )
        current = ledger.capture_obligation_directory(
            query(selected_view="current", categories=("trade",), direction="payable"), read
        )
    assert len(calls) == 1 and calls[0][0][1] == QuerySelection("current")
    assert current.current is current.selected
    assert [item.reference["slot"] for item in posted.selected] == ["payable:0"]
    assert [item.reference["slot"] for item in posted.current] == ["payable:0"]
    assert [item.reference["slot"] for item in current.selected] == ["payable:1"]
    assert current.sources[0].reference["posting_period"] == "2026-02"
    assert current.sources[0].reference["business_period"] == "2026-01"
    assert len(current.selected) == len(expected) == 1
    assert_same(current.selected[0], expected[0])


@pytest.mark.parametrize("selected_view", ["as_posted", "current"])
def test_future_actual_payment_preserves_selected_due_and_current_fully_settled_zero(
    store, selected_view
):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(), _payment_at(period="2026-02")], unit)
    with store.read_unit(COMPANY) as read:
        result = ledger.capture_obligation_directory(
            query(current="2026-02", categories=("trade",), selected_view=selected_view), read
        )
    assert len(result.selected) == len(result.current) == len(result.sources) == 1
    assert result.selected[0].amounts.remaining_fen == 100000
    assert result.current[0].amounts.money_settled_fen == 100000
    assert result.current[0].amounts.remaining_fen == 0


def test_withdrawal_is_absent_current_not_a_fake_zero_resource(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
        old = ledger.get_result_source(BUSINESS, "main", unit)
        unit.repository("ledger").freeze_heads("2026-01")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [withdrawal(old)], unit, correction_period="2026-02")
    with store.read_unit(COMPANY) as read:
        old = ledger.capture_obligation_directory(query(current="2026-02"), read)
        current = ledger.capture_obligation_directory(query("2026-02"), read)
        revised_january = ledger.capture_obligation_directory(query(selected_view="current"), read)
    assert len(old.selected) == 1 and old.current == ()
    assert current.selected == current.current == current.sources == ()
    assert revised_january.selected == revised_january.current == revised_january.sources == ()


def test_empty_scope_and_requested_empty_origins_do_not_query_or_make_zero(store, monkeypatch):
    ledger = create_ledger()
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        monkeypatch.setattr(
            repo, "read_obligation_directory_rows", lambda *a, **k: pytest.fail("空范围")
        )
        empty = ledger.capture_obligation_directory(query(business_ids=()), read)
        wanted = ledger.capture_obligation_directory(
            replace(query(business_ids=()), include_origins=True), read
        )
    assert empty.selected == empty.current == empty.sources == ()
    assert empty.origins is None and wanted.origins == {}


@pytest.mark.parametrize(
    "fields",
    [
        {"include_origins": None},
        {"categories": ("invalid",)},
        {"direction": "invalid"},
        {"business_ids": (BUSINESS, BUSINESS)},
        {"business_ids": []},
        {"categories": ("trade", "trade")},
        {"cutoff_period": "2026-13"},
        {"selected_view": "invalid"},
        {"selected_view": None},
    ],
)
def test_invalid_query_does_not_enter_resource_sql(store, monkeypatch, fields):
    ledger = create_ledger()
    with store.read_unit(COMPANY) as read:
        monkeypatch.setattr(
            read.repository("ledger"),
            "read_obligation_directory_rows",
            lambda *a, **k: pytest.fail("坏条件"),
        )
        with pytest.raises(ContractError) as error:
            ledger.capture_obligation_directory(replace(query(), **fields), read)
    assert error.value.code == "invalid_input"


def test_light_query_omits_unpromised_resource_fields_and_sql_order(store, monkeypatch):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        fetch, statements = repo._session.fetchall, []

        def observed(sql, args=()):
            statements.append(sql)
            return fetch(sql, args)

        monkeypatch.setattr(repo._session, "fetchall", observed)
        ledger.capture_obligation_directory(query(), read)
    assert len(statements) == 1
    assert "ORDER BY" not in statements[0]
    for field in (
        "allowed_settlements_json",
        "cashflow_weights_json",
        "target_json",
        "basis_json",
        "due_on",
        "account_code",
    ):
        assert field not in statements[0]
