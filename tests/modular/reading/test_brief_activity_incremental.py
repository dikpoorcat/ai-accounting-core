"""Actual changed roots reuse display rows and remain equal to complete preparation."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery, loads_json
from ai_accounting_modular.money.public import stable_occurrence_id
from ai_accounting_modular.reading import (
    _brief_activity as activity,
)
from ai_accounting_modular.reading import (
    _brief_activity_index as index,
)
from ai_accounting_modular.reading import (
    _brief_activity_update as update,
)
from ai_accounting_modular.reading import (
    _monthly,
)
from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import case as working_case
from tests.modular.domains.financing._cooperation_support import (
    PROJECT,
)
from tests.modular.domains.financing._cooperation_support import (
    contract as cooperation_contract,
)
from tests.modular.domains.financing._cooperation_support import (
    expense as project_expense,
)
from tests.modular.domains.financing._support import BANK, COUNTERPARTY, payment, six
from tests.modular.money.test_reconciliation import pair as statement_pair

from .test_brief_activity import activity as activity_page
from .test_brief_activity import capture as publish_full
from .test_funds import apply, initialize, selection


@pytest.fixture(params=MODES)
def book(tmp_path, request):
    with install_ledger_implementation(request.param):
        fixture = working_case.__wrapped__(tmp_path)
        case = next(fixture)
        try:
            with case.kernel.storage.read_unit(case.company) as read:
                assert (
                    type(read.repository("ledger")).__name__ == "AggregateJoinLedgerRepository"
                ) == (request.param == "aggregate_join")
            yield case
        finally:
            fixture.close()


def change(book, fact, *, seq=1, **fields):
    actual = book.app.accounting_apply(
        book.request(mutations=[{"op": "replace", "expected_seq": seq, "fact": fact}], **fields)
    )
    assert actual["completion"] == "complete", actual
    return actual


def state(book, month="2026-01"):
    with book.kernel.storage.read_unit(book.company) as read:
        return read.repository("reading").state(
            _monthly.key(selection(book, month)), index.COMPONENT
        )


def matches_complete(book, month="2026-01"):
    scope = selection(book, month)
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        current = repository.state(_monthly.key(scope), index.COMPONENT)
        assert current["state"] == "ready", current
        fresh = activity.prepare(book.kernel.reading, scope, read)
        saved = loads_json(repository.payload(_monthly.key(scope), index.COMPONENT)["payload_json"])
        assert index.semantic(saved) == index.semantic(loads_json(fresh.payload_json))
        assert index.base(repository, current) is not None
        assert index.pending(repository, _monthly.key(scope)) is None
        rows = repository.activity_rows(_monthly.key(scope))
        actual = {(row["collection_key"], row["item_id"]): row["payload_json"] for row in rows}
        assert actual == {(row[0], row[1]): row[-1] for row in fresh.rows}
    return {
        key: value
        for key, value in loads_json(fresh.payload_json).items()
        if key not in {"_item_epoch", "_semantic_digest"}
    }


def watch_contents(book, monkeypatch):
    seen = []
    contents, values = (
        book.kernel.ledger.get_adopted_contents,
        book.kernel.ledger.query_adopted_values,
    )
    filters = []

    def exact(refs, read):
        seen.extend(ref["business_id"] for ref in refs)
        return contents(refs, read)

    def selected(*args, business_ids=()):
        filters.append(tuple(business_ids))
        return values(*args, business_ids=business_ids)

    monkeypatch.setattr(book.kernel.ledger, "get_adopted_contents", exact)
    monkeypatch.setattr(book.kernel.ledger, "query_adopted_values", selected)
    return seen, filters


def test_root_dependencies_do_not_borrow_unselected_business_bodies(book):
    initialize(book)
    _, contract, drawdown, interest, *_ = six()
    paid = payment(interest, "1000", slot="interest", day="2026-01-31")
    apply(book, contract, drawdown, interest, paid)
    scope = selection(book)
    with book.kernel.storage.read_unit(book.company) as read:
        complete = activity.capture(book.kernel.reading, scope, read)[2]
        selected = activity.capture(
            book.kernel.reading, scope, read, business_ids=(paid["business_id"],)
        )[2]
    assert len(selected) == 1
    assert selected[0] == next(item for item in complete if item.business_id == paid["business_id"])


def test_current_root_ignores_retained_older_generation_in_shared_body_cache(book, monkeypatch):
    initialize(book)
    _, contract, drawdown, interest, *_ = six()
    paid = payment(interest, "1000", slot="interest", day="2026-01-31")
    _, _, interest, _ = apply(book, contract, drawdown, interest, paid)
    ids = {"bank": BANK, "source": book.source_id}
    statement, reconciled = statement_pair(ids, values=(1000000, 1000), day="2026-01-01")
    statement["data"]["closing_fen"] = "999000"
    statement["data"]["rows"][1].update(actual_on="2026-01-31", direction="outflow")
    reconciled["data"]["matches"] = [
        {
            "statement_row_ids": [row["row_id"]],
            "fund_movement_ids": [stable_occurrence_id(item["business_id"], "funds")],
        }
        for row, item in zip(statement["data"]["rows"], (drawdown, paid), strict=True)
    ]
    apply(book, statement, reconciled)
    checked = book.check()
    assert checked["ready"], checked["blocking_issues"]
    assert book.close(checked)["state"] == "closed"
    original = book.get(interest["business_id"])["businesses"][0]["results"][0]["reference"]
    interest["data"]["end_before"] = "2026-01-31"
    change(book, interest, correction_period="2026-02")
    apply(book, *statement_pair(ids, values=(), opening="999000", day="2026-02-01"))
    assert book.close(book.check("2026-02"))["state"] == "closed"
    interest["data"]["end_before"] = "2026-01-30"
    change(book, interest, seq=2, correction_period="2026-03")
    decoded = []
    actual = book.kernel.ledger.get_adopted_contents

    def exact(refs, read):
        decoded.extend(refs)
        return actual(refs, read)

    monkeypatch.setattr(book.kernel.ledger, "get_adopted_contents", exact)
    scope = selection(book, "2026-03")
    with book.kernel.storage.read_unit(book.company) as read:
        selected = activity.capture(
            book.kernel.reading, scope, read, business_ids=(interest["business_id"],)
        )[2]
        direct = list(decoded)
        decoded.clear()
        load = activity.Contents.load

        def warmed(cache, refs):
            load(cache, refs)
            load(cache, (original,))

        # A legitimate retained frozen body can be present for another root.
        monkeypatch.setattr(activity.Contents, "load", warmed)
        complete = activity.capture(book.kernel.reading, scope, read)[2]
    old = original["result_id"], original["generation"]
    assert old not in {(ref["result_id"], ref["generation"]) for ref in direct}
    assert old in {(ref["result_id"], ref["generation"]) for ref in decoded}
    assert selected[0] == next(
        item for item in complete if item.business_id == interest["business_id"]
    )


def test_only_one_changed_business_body_is_read_among_many_unchanged_roots(book, monkeypatch):
    first = book.seed()
    others = [book.expense(str(number)) for number in range(1, 33)]
    apply(book, *others)
    publish_full(book)
    revised = deepcopy(first)
    revised["data"]["amount_fen"] = "90000"
    change(book, revised)
    with book.kernel.storage.read_unit(book.company) as read:
        assert index.pending(read.repository("reading"), _monthly.key(selection(book))) == {
            "full": False,
            "business_ids": [first["business_id"]],
        }
    with monkeypatch.context() as patch:
        seen, filters = watch_contents(book, patch)
        assert book.kernel.reading.run_pending(book.company)
        assert seen == [first["business_id"]]
        assert filters == [(first["business_id"],)]
    payload = matches_complete(book)
    assert payload["activity_count"] == payload["voucher_count"] == 33
    assert payload["review_businesses"][0]["business_amount_fen"] == "90528"


def test_new_business_and_new_party_do_not_force_unrelated_roots_to_be_read(book, monkeypatch):
    first = book.seed()
    publish_full(book)
    party = str(uuid4())
    second = book.expense("200")
    second["data"]["creditor_id"] = party
    apply(
        book,
        second,
        objects=[
            {
                "object_id": party,
                "kind": "organization",
                "name": "新供应商",
                "expected_seq": 0,
                "data": {},
            }
        ],
    )
    with monkeypatch.context() as patch:
        seen, _ = watch_contents(book, patch)
        assert book.kernel.reading.run_pending(book.company)
        assert seen == [second["business_id"]]
        assert first["business_id"] not in seen
    matches_complete(book)
    assert {row["party"] for row in activity_page(book)["items"]} == {"合成供应商", "新供应商"}


@pytest.mark.parametrize("view", ["as_posted", "current"])
def test_adopted_value_filter_and_cursor_are_bound_to_both_actual_sql_scopes(book, view):
    first = book.seed()
    second, excluded = book.expense("20"), book.expense("30")
    apply(book, second, excluded)
    chosen = tuple(sorted((first["business_id"], second["business_id"])))
    scope = {key: value for key, value in selection(book).items() if key != "as_of"} | {
        "view": view
    }
    with book.kernel.storage.read_unit(book.company) as read:
        initial = book.kernel.ledger.query_adopted_values(
            scope, (), PageQuery(1), read, business_ids=chosen
        )
        assert initial.total_count == initial.filtered_count == 2
        assert initial.next_cursor is not None
        tail = book.kernel.ledger.query_adopted_values(
            scope, (), PageQuery(1, initial.next_cursor), read, business_ids=chosen
        )
        assert {item.reference["business_id"] for item in (*initial.items, *tail.items)} == set(
            chosen
        )
        assert tail.next_cursor is None
        with pytest.raises(ContractError) as error:
            book.kernel.ledger.query_adopted_values(
                scope,
                (),
                PageQuery(1, initial.next_cursor),
                read,
                business_ids=(excluded["business_id"],),
            )
        assert error.value.code == "snapshot_changed"


def test_changed_dependency_is_replaced_and_unused_dependency_does_not_accumulate(book):
    initialize(book)
    first, second = book.expense("100"), book.expense("200")
    first["data"]["creditor_id"] = second["data"]["creditor_id"] = COUNTERPARTY
    second["data"]["expense_class"] = "sales"
    paid = payment(first, 100, party=COUNTERPARTY, slot="primary")
    first, second, paid = apply(book, first, second, paid)
    publish_full(book)
    paid["data"]["allocations"][0]["source"]["business_id"] = second["business_id"]
    change(book, paid)
    assert book.kernel.reading.run_pending(book.company)
    matches_complete(book)
    with book.kernel.storage.read_unit(book.company) as read:
        directory = index.base(read.repository("reading"), state(book))
        roots = directory.get(index.ROOTS, directory.payload["_inputs"]["business_ids"])
    dependencies = {source["business_id"] for source in roots[paid["business_id"]]["sources"]}
    assert second["business_id"] in dependencies and first["business_id"] not in dependencies
    saved = book.app.accounting_apply(
        book.request(
            mutations=[{"op": "withdraw", "expected_seq": 1, "business_id": first["business_id"]}]
        )
    )
    assert saved["completion"] == "complete", saved
    assert book.kernel.reading.run_pending(book.company)
    payload = matches_complete(book)
    assert first["business_id"] not in payload["_inputs"]["business_ids"]
    assert "支付销售费用" in {row["title"] for row in activity_page(book)["items"]}


def test_move_month_and_withdraw_remove_old_display_and_root_dependencies(book):
    first = book.seed()
    publish_full(book)
    publish_full(book, "2026-02")
    moved = deepcopy(first)
    moved["period"] = "2026-02"
    change(book, moved)
    assert book.kernel.reading.run_pending(book.company)
    assert matches_complete(book)["activity_count"] == 0
    assert matches_complete(book, "2026-02")["activity_count"] == 1
    saved = book.app.accounting_apply(
        book.request(
            mutations=[{"op": "withdraw", "expected_seq": 2, "business_id": first["business_id"]}]
        )
    )
    assert saved["completion"] == "complete", saved
    assert book.kernel.reading.run_pending(book.company)
    payload = matches_complete(book, "2026-02")
    assert payload["activity_count"] == payload["voucher_count"] == 0
    assert first["business_id"] not in payload["_inputs"]["business_ids"]


def test_management_rename_uses_full_capture_and_unrelated_object_does_not_invalidate(
    book, monkeypatch
):
    book.seed()
    second = book.expense("200")
    apply(book, second)
    publish_full(book)
    before = state(book)
    apply(
        book,
        objects=[
            {
                "object_id": str(uuid4()),
                "kind": "organization",
                "name": "无关对象",
                "expected_seq": 0,
                "data": {},
            }
        ],
    )
    assert state(book) == before
    apply(
        book,
        objects=[
            {
                "object_id": book.party,
                "kind": "organization",
                "name": "更名供应商",
                "expected_seq": 1,
                "data": {},
            }
        ],
    )
    with book.kernel.storage.read_unit(book.company) as read:
        assert index.pending(read.repository("reading"), _monthly.key(selection(book)))["full"]
    with monkeypatch.context() as patch:
        seen, filters = watch_contents(book, patch)
        assert book.kernel.reading.run_pending(book.company)
        assert len(seen) == 2 and filters == [()]
    matches_complete(book)
    assert {row["party"] for row in activity_page(book)["items"]} == {"更名供应商"}


def test_later_changes_survive_rejected_whole_index_publication(book):
    first = book.seed()
    second = book.expense("200")
    apply(book, second)
    publish_full(book)
    first["data"]["amount_fen"] = "90000"
    change(book, first)
    scope = selection(book)
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        previous = repository.state(_monthly.key(scope), index.COMPONENT)
        prepared = update.prepare(book.kernel.reading, scope, read, previous)
    second["data"]["amount_fen"] = "300"
    change(book, second)
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        assert not repository.publish(
            _monthly.key(scope),
            index.COMPONENT,
            previous["wanted_head"],
            index.FORMAT_VERSION,
            prepared,
        )
        assert set(index.pending(repository, _monthly.key(scope))["business_ids"]) == {
            first["business_id"],
            second["business_id"],
        }
        assert (
            repository.state(_monthly.key(scope), index.COMPONENT)["ready_head"]
            == previous["ready_head"]
        )
    assert book.kernel.reading.run_pending(book.company)
    assert matches_complete(book)["review_businesses"][0]["business_amount_fen"] == "90300"


def test_full_management_work_subsumes_both_earlier_and_later_dirty_roots(book):
    first = book.seed()
    second = book.expense("200")
    apply(book, second)
    publish_full(book)
    first["data"]["amount_fen"] = "90000"
    change(book, first)
    apply(
        book,
        objects=[
            {
                "object_id": book.party,
                "kind": "organization",
                "name": "更名供应商",
                "expected_seq": 1,
                "data": {},
            }
        ],
    )
    second["data"]["amount_fen"] = "300"
    change(book, second)
    with book.kernel.storage.read_unit(book.company) as read:
        assert index.pending(read.repository("reading"), _monthly.key(selection(book))) == {
            "full": True,
            "business_ids": [],
        }
    assert book.kernel.reading.run_pending(book.company)
    assert matches_complete(book)["review_businesses"][0]["business_amount_fen"] == "90300"
    assert {row["party"] for row in activity_page(book)["items"]} == {"更名供应商"}


def test_failure_keeps_later_work_and_explicit_retry_rebuilds_current_facts(book, monkeypatch):
    first = book.seed()
    second = book.expense("200")
    apply(book, second)
    publish_full(book)
    first["data"]["amount_fen"] = "90000"
    change(book, first)

    def fail_capture(*args, **kwargs):
        raise RuntimeError("capture failed")

    with monkeypatch.context() as patch:
        patch.setattr(activity, "capture", fail_capture)
        assert book.kernel.reading.run_pending(book.company) == ()
    assert state(book)["state"] == "failed"
    second["data"]["amount_fen"] = "300"
    change(book, second)
    assert state(book)["state"] == "failed"
    _monthly.retry(book.kernel.reading, selection(book), (index.COMPONENT,))
    assert book.kernel.reading.run_pending(book.company)
    assert matches_complete(book)["review_businesses"][0]["business_amount_fen"] == "90300"


def test_closed_correction_and_withdrawal_keep_the_frozen_month(book):
    first = book.seed()
    publish_full(book)
    assert book.close(book.check())["state"] == "closed"
    assert book.kernel.reading.run_pending(book.company)
    january = matches_complete(book)
    publish_full(book, "2026-02")
    first["data"].update(amount_fen="80000", expense_class="sales")
    change(book, first, correction_period="2026-02")
    assert book.kernel.reading.run_pending(book.company)
    assert matches_complete(book) == january
    february = matches_complete(book, "2026-02")
    assert february["activity_count"] == february["voucher_count"] == 2
    assert {row["title"] for row in activity_page(book, "2026-02")["items"]} == {
        "冲正原业务 · 管理费用",
        "更正原业务 · 销售费用",
    }
    withdrawn = book.app.accounting_apply(
        book.request(
            mutations=[{"op": "withdraw", "expected_seq": 2, "business_id": first["business_id"]}],
            correction_period="2026-02",
            explanation="已核实该笔费用没有对应现实业务，撤去误记。",
        )
    )
    assert withdrawn["completion"] == "complete", withdrawn
    assert book.kernel.reading.run_pending(book.company)
    assert matches_complete(book) == january
    assert matches_complete(book, "2026-02")["activity_count"] == 1


def test_no_voucher_business_legs_remain_incremental_after_closing(book):
    initialize(book)
    agreement = cooperation_contract()
    agreement["data"]["contract_source"]["source_id"] = book.source_id
    agreement["data"]["company_revenue_expense_share"] = "0.1"
    charged = project_expense(agreement, project="1", company="0")
    _, charged = apply(
        book,
        agreement,
        charged,
        objects=[
            {
                "object_id": PROJECT,
                "kind": "investment_product",
                "name": "合成合作项目",
                "expected_seq": 0,
                "data": {"category": "cooperation"},
            }
        ],
    )
    publish_full(book)
    charged["data"]["project_total_fen"] = "2"
    change(book, charged)
    assert book.kernel.reading.run_pending(book.company)
    january = matches_complete(book)
    assert january["activity_count"] == 1 and january["voucher_count"] == 0
    assert book.close(book.check())["state"] == "closed"
    assert book.kernel.reading.run_pending(book.company)
    january = matches_complete(book)
    publish_full(book, "2026-02")
    charged["data"]["project_total_fen"] = "3"
    change(book, charged, seq=2, correction_period="2026-02")
    assert book.kernel.reading.run_pending(book.company)
    changed = matches_complete(book, "2026-02")
    assert changed["activity_count"] == 2 and changed["voucher_count"] == 0
    charged["data"]["project_total_fen"] = "4"
    change(book, charged, seq=3, correction_period="2026-02")
    assert book.kernel.reading.run_pending(book.company)
    changed = matches_complete(book, "2026-02")
    assert changed["activity_count"] == 2 and changed["voucher_count"] == 0
    assert matches_complete(book) == january


@pytest.mark.parametrize("damaged", ["activity_members", index.ROOTS, index.DIRTY])
def test_damaged_reuse_or_pending_rows_require_explicit_full_retry(book, damaged):
    first = book.seed()
    publish_full(book)
    seq = 1
    if damaged == index.DIRTY:
        first["data"]["amount_fen"] = "90000"
        change(book, first)
        seq = 2
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        from ai_accounting_modular.reading import _item_buckets

        target = index.BUCKETS if damaged == index.ROOTS else damaged
        args = (_monthly.key(selection(book)), index.COMPONENT, target)
        if damaged == index.ROOTS:
            args += (_item_buckets.key(index.ROOTS, first["business_id"]),)
        unit.repository("reading")._session.execute(
            "UPDATE read_item SET payload_json='{}' WHERE scope_key=? AND component_key=? "
            "AND collection_key=?" + (" AND item_id=?" if damaged == index.ROOTS else ""),
            args,
        )
    first["data"]["amount_fen"] = "80000"
    change(book, first, seq=seq)
    assert book.get(first["business_id"])["businesses"][0]["fact"]["data"]["amount_fen"] == "80000"
    assert book.kernel.reading.run_pending(book.company) == ()
    assert (state(book)["state"], state(book)["error_code"]) == ("failed", "storage_corrupt")
    _monthly.retry(book.kernel.reading, selection(book), (index.COMPONENT,))
    assert book.kernel.reading.run_pending(book.company)
    assert matches_complete(book)["review_businesses"][0]["business_amount_fen"] == "80000"
