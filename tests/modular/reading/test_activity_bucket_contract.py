"""Actual Activity changes preserve complete results while touching only current local indexes."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    loads_json,
)
from ai_accounting_modular.reading import _brief_activity as activity
from ai_accounting_modular.reading import _brief_activity_index as index
from ai_accounting_modular.reading import _brief_activity_totals as totals
from ai_accounting_modular.reading import _brief_activity_update as update
from ai_accounting_modular.reading import _brief_open_index as open_index
from ai_accounting_modular.reading import _brief_open_inputs as open_inputs
from ai_accounting_modular.reading import _brief_open_update as open_update
from ai_accounting_modular.reading import _funds_items, _item_buckets, _monthly

from . import _activity_full_oracle as oracle
from ._publication import CapturedState
from .test_brief_activity import capture as publish_full
from .test_brief_activity_incremental import book as book
from .test_brief_activity_incremental import change, matches_complete, state, watch_contents
from .test_brief_open import publish as publish_open
from .test_brief_open_incremental import matches_cold
from .test_funds import BANK, apply, initialize, selection


def directory(book, read):
    return index.base(read.repository("reading"), state(book))


def group_orders(book, business):
    with book.kernel.storage.read_unit(book.company) as read:
        saved = directory(book, read)
        root = saved.get(index.ROOTS, (business,), required=True)[business]
        member = saved.get("activity_members", root["members"], required=True)[root["members"][0]]
        group = member["row"]["group_key"]
        return group, saved.get(totals.FOLDS, (group,), required=True)[group]


def test_nine_hundred_same_group_members_update_without_whole_directory_or_group_scan(
    book, monkeypatch
):
    book.seed(expense=False)
    facts = [book.expense("1") for _ in range(900)]
    apply(book, *facts[:500])
    apply(book, *facts[500:])
    publish_full(book)
    key, fold = group_orders(book, facts[0]["business_id"])
    assert fold["first"][1] == totals.candidate_id(facts[0]["business_id"], key)
    with book.kernel.storage.read_unit(book.company) as read:
        complete = activity.capture(book.kernel.reading, selection(book), read)[0]
        assert fold["order"] == list(
            min((totals.member_item(member).sort_key, member["row"]["key"]) for member in complete)
        )
        repository_type = type(read.repository("reading"))
    fetched = []
    get, page = repository_type.item_get, repository_type.item_page

    def exact(repository, scope, component, collection, identities):
        if component == index.COMPONENT:
            fetched.extend((collection, identity) for identity in identities)
            assert len(identities) < 16
        return get(repository, scope, component, collection, identities)

    def bounded(repository, scope, component, collection, query, after, limit):
        if component == index.COMPONENT:
            assert limit <= 3, "同组更新不得重新读取900条成员"
        return page(repository, scope, component, collection, query, after, limit)

    def forbidden(*args):
        pytest.fail("局部更新不得读取整份Activity目录")

    with monkeypatch.context() as patch:
        patch.setattr(repository_type, "activity_rows", forbidden)
        patch.setattr(repository_type, "item_get", exact)
        patch.setattr(repository_type, "item_page", bounded)
        seen, filters = watch_contents(book, patch)
        facts[-1]["data"]["amount_fen"] = "2"
        change(book, facts[-1])
        assert book.kernel.reading.run_pending(book.company)
        assert seen == [facts[-1]["business_id"]]
        assert filters == [(facts[-1]["business_id"],)]
    assert len(fetched) < 80
    assert matches_complete(book)["review_businesses"][0]["business_amount_fen"] == "901"
    with monkeypatch.context() as patch:
        patch.setattr(repository_type, "activity_rows", forbidden)
        patch.setattr(repository_type, "item_get", exact)
        patch.setattr(repository_type, "item_page", bounded)
        facts[0]["data"]["expense_class"] = "sales"
        change(book, facts[0])
        assert book.kernel.reading.run_pending(book.company)
    _, fold = group_orders(book, facts[1]["business_id"])
    assert fold["first"][1] == totals.candidate_id(facts[1]["business_id"], key)
    assert matches_complete(book)["group_count"] == 2
    withdrawn = book.app.accounting_apply(
        book.request(
            mutations=[
                {
                    "op": "withdraw",
                    "expected_seq": 1,
                    "business_id": facts[1]["business_id"],
                }
            ]
        )
    )
    assert withdrawn["completion"] == "complete", withdrawn
    assert book.kernel.reading.run_pending(book.company)
    _, fold = group_orders(book, facts[2]["business_id"])
    assert fold["first"][1] == totals.candidate_id(facts[2]["business_id"], key)
    assert matches_complete(book)["activity_count"] == 899


def test_group_and_review_folds_preserve_independent_full_formulas_unknowns_and_negatives(book):
    book.seed()
    with book.kernel.storage.read_unit(book.company) as read:
        member = activity.capture(book.kernel.reading, selection(book), read)[0][0]
    members = [deepcopy(member) for _ in range(3)]
    for number, (item, amount, day) in enumerate(
        zip(members, (None, "17", "-9"), ("28", "02", "15"), strict=True)
    ):
        item["row"].update(key=content_digest(number), amount_fen=amount, date="2026-01-" + day)
        item["row"]["recognition"].update(date=item["row"]["date"], precision="day")
        item["scope"]["part"] = number
    ordered = sorted(members, key=totals.member_order)
    expected, categories = oracle.indexes(ordered)
    actual, folds, review = totals.complete(members)
    assert {
        item.item_id: item for item in actual if item.collection in {"activity", "activity_members"}
    } == {item.item_id: item for item in expected}
    assert totals.summaries(folds, review) == (categories, oracle.review_businesses(ordered))
    value = totals.empty()
    totals.adjust(value, members, 1)
    assert value["unknown"] == 1 and value["sum"] == "8" and value["vouchers"] == 1
    totals.adjust(value, members, -1)
    totals.adjust(value, members[1:], 1)
    assert value["unknown"] == 0 and value["sum"] == "8" and value["vouchers"] == 1
    for role in ("original", "reversal", "replacement", "reclassification", "closing"):
        for item in members:
            item["scope"]["voucher_role"] = role
        review = {}
        totals.review_adjust(review, members, 1)
        assert totals.summaries(folds, review)[1] == oracle.review_businesses(members)


def test_actual_day_order_and_voucher_representative_change_independently(book):
    initialize(book)
    facts = [
        {
            "business_id": str(uuid4()),
            "kind": "operations.other_income",
            "period": "2026-01",
            "evidence_ids": [book.source_id],
            "effective_on": "2026-01-" + day,
            "data": {
                "income_kind": "bank_interest",
                "amount_fen": "1",
                "funds_account_id": BANK,
                "counterparty_id": book.party,
            },
        }
        for day in ("28", "02", "15")
    ]
    apply(book, *facts)
    publish_full(book)
    group_id, fold = group_orders(book, facts[0]["business_id"])
    assert fold["first"][1] == totals.candidate_id(facts[0]["business_id"], group_id)
    with book.kernel.storage.read_unit(book.company) as read:
        second = directory(book, read).get(index.ROOTS, (facts[1]["business_id"],), required=True)[
            facts[1]["business_id"]
        ]
    assert fold["order"][1] in second["members"]
    facts[0]["effective_on"] = "2026-01-01"
    change(book, facts[0])
    assert book.kernel.reading.run_pending(book.company)
    _, revised = group_orders(book, facts[0]["business_id"])
    assert revised["first"] == fold["first"]
    assert revised["order"] != fold["order"]
    assert matches_complete(book)["group_count"] == 1


def test_complete_constructs_each_member_sort_key_once_and_preserves_both_orders(book, monkeypatch):
    initialize(book)
    facts = [
        {
            "business_id": str(uuid4()),
            "kind": "operations.other_income",
            "period": "2026-01",
            "evidence_ids": [book.source_id],
            "effective_on": "2026-01-" + day,
            "data": {
                "income_kind": "bank_interest",
                "amount_fen": "1",
                "funds_account_id": BANK,
                "counterparty_id": book.party,
            },
        }
        for day in ("28", "02", "15")
    ]
    apply(book, *facts)
    with book.kernel.storage.read_unit(book.company) as read:
        members, vouchers, roots = activity.capture(book.kernel.reading, selection(book), read)
        expected, _ = oracle.indexes(sorted(members, key=totals.member_order))
        expected_items = {(item.collection, item.item_id): item for item in expected}
        original, pinyin = totals.member_item, _funds_items.lazy_pinyin
        constructed, names = [], []

        def build(member):
            constructed.append(member["row"]["key"])
            return original(member)

        def transliterate(name, **kwargs):
            names.append(name)
            return pinyin(name, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(totals, "member_item", build)
            patch.setattr(_funds_items, "lazy_pinyin", transliterate)
            model = activity.assemble(
                members, vouchers, roots, scope_key=_monthly.key(selection(book)), epoch=read.head
            )
    assert constructed == [member["row"]["key"] for member in members]
    assert names == [member["row"]["party"] for member in members]
    actual = {
        (row[0], row[1]): (row[2], loads_json(row[-1])["value"])
        for row in model.rows
        if row[0] in {"activity", "activity_members"}
    }
    assert actual == {key: (item.sort_key, item.value) for key, item in expected_items.items()}
    folds = {
        identity: value
        for row in model.rows
        if row[0] == index.BUCKETS and loads_json(row[-1])["collection"] == totals.FOLDS
        for identity, value in loads_json(row[-1])["values"]
    }
    assert len(folds) == 1
    (fold,) = folds.values()
    first = min(members, key=totals.member_order)
    assert first["row"]["date"] == "2026-01-28"
    assert fold["first"][1] == totals.candidate_id(
        first["row"]["subject_id"], first["row"]["group_key"]
    )
    assert (
        next(member for member in members if member["row"]["key"] == fold["order"][1])["row"][
            "date"
        ]
        == "2026-01-02"
    )


@pytest.mark.parametrize("damage", ["note_reverse", "business_reverse"])
def test_mark_checks_actual_reverse_and_input_key_proof_before_deciding_not_affected(book, damage):
    first = book.seed()
    publish_full(book)
    scope_key = _monthly.key(selection(book))
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        collection, identity = (
            (index.BUSINESSES, first["business_id"])
            if damage == "business_reverse"
            else (index.NOTES, "business:" + first["business_id"])
        )
        repository._session.execute(
            "UPDATE read_item SET payload_json='{}' WHERE scope_key=? AND component_key=? "
            "AND collection_key=? AND item_id=?",
            (
                scope_key,
                index.COMPONENT,
                index.BUCKETS,
                _item_buckets.key(collection, identity),
            ),
        )
    first["data"]["amount_fen"] = "8"
    change(book, first)
    assert state(book)["state"] == "failed"
    assert book.get(first["business_id"])["businesses"][0]["fact"]["data"]["amount_fen"] == "8"
    _monthly.retry(book.kernel.reading, selection(book), (index.COMPONENT,))
    assert book.kernel.reading.run_pending(book.company)
    assert matches_complete(book)["activity_count"] == 1


def test_unread_unrelated_bucket_is_deferred_but_selected_root_and_explicit_retry_stay_strict(
    book, monkeypatch
):
    first = book.seed()
    second = book.expense("2")
    while _item_buckets.key(index.ROOTS, first["business_id"]) == _item_buckets.key(
        index.ROOTS, second["business_id"]
    ):
        second = book.expense("2")
    second["data"]["expense_class"] = "sales"
    apply(book, second)
    publish_full(book)
    bad = _item_buckets.key(index.ROOTS, second["business_id"])
    scope_key = _monthly.key(selection(book))
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_item SET payload_json='{}' WHERE scope_key=? AND component_key=? "
            "AND collection_key=? AND item_id=?",
            (scope_key, index.COMPONENT, index.BUCKETS, bad),
        )
    with book.kernel.storage.read_unit(book.company) as read:
        repository_type = type(read.repository("reading"))
    bucket_rows, loaded = repository_type.activity_bucket_rows, []

    def selected(repository, scope, keys):
        loaded.extend(keys)
        return bucket_rows(repository, scope, keys)

    with monkeypatch.context() as patch:
        patch.setattr(repository_type, "activity_bucket_rows", selected)
        first["data"]["amount_fen"] = "9"
        change(book, first)
        assert book.kernel.reading.run_pending(book.company)
    assert bad not in loaded
    assert state(book)["state"] == "ready"
    with book.kernel.storage.read_unit(book.company) as read:
        checked = directory(book, read)
        assert checked.get(index.ROOTS, (first["business_id"],), required=True)
        with pytest.raises(ContractError) as error:
            checked.get(index.ROOTS, (second["business_id"],), required=True)
        assert error.value.code == "storage_corrupt"
    second["data"]["amount_fen"] = "3"
    change(book, second)
    assert state(book)["state"] == "failed"
    _monthly.retry(book.kernel.reading, selection(book), (index.COMPONENT,))
    assert book.kernel.reading.run_pending(book.company)
    assert matches_complete(book)["activity_count"] == 2


def _pending_plan(book):
    with book.kernel.storage.read_unit(book.company) as read:
        current = CapturedState(state(book), read.head)
        model = update.prepare(book.kernel.reading, selection(book), read, current)
    assert isinstance(model, index.PreparedActivityPatch)
    return current, model


def _saved_rows(book, component=index.COMPONENT):
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        scope_key = _monthly.key(selection(book))
        return (
            repository.state(scope_key, component),
            repository.payload(scope_key, component),
            tuple(
                sorted(
                    (row["collection_key"], row["item_id"], row["head"], row["payload_json"])
                    for row in repository._session.fetchall(
                        "SELECT * FROM read_item WHERE scope_key=? AND component_key=?",
                        (scope_key, component),
                    )
                )
            ),
        )


def test_same_wanted_repair_rejects_a_patch_from_the_old_base_without_deleting_any_rows(book):
    first = book.seed()
    publish_full(book)
    first["data"]["amount_fen"] = "9"
    change(book, first)
    previous, prepared = _pending_plan(book)
    publish_full(book)
    before = _saved_rows(book)
    assert before[0]["wanted_head"] == previous["wanted_head"]
    assert before[0]["content_digest"] != previous["content_digest"]
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        assert not unit.repository("reading").publish(
            previous["scope_key"],
            index.COMPONENT,
            previous["wanted_head"],
            index.FORMAT_VERSION,
            prepared,
            guard=previous.guard,
            current_head=unit.head,
        )
    assert _saved_rows(book) == before
    matches_complete(book)


def test_previous_private_format_is_rebuilt_from_current_facts_after_explicit_retry(
    book, monkeypatch
):
    first = book.seed()
    publish_full(book)
    scope_key = _monthly.key(selection(book))
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_scope_head SET format_version=8 WHERE scope_key=? AND component_key=?",
            (scope_key, index.COMPONENT),
        )
    first["data"]["amount_fen"] = "9"
    change(book, first)
    assert state(book)["state"] == "failed"
    _monthly.retry(book.kernel.reading, selection(book), (index.COMPONENT,))
    with monkeypatch.context() as patch:
        seen, filters = watch_contents(book, patch)
        assert book.kernel.reading.run_pending(book.company)
    assert filters == [()] and seen == [first["business_id"]]
    assert state(book)["format_version"] == index.FORMAT_VERSION
    assert matches_complete(book)["review_businesses"][0]["business_amount_fen"] == "9"


def test_patch_failure_after_real_deletion_rolls_back_members_buckets_pending_and_header(
    book, monkeypatch
):
    first = book.seed()
    publish_full(book)
    first["data"]["amount_fen"] = "9"
    change(book, first)
    previous, prepared = _pending_plan(book)
    before, deleted = _saved_rows(book), []
    with pytest.raises(RuntimeError, match="after exact derived deletes"):
        with book.kernel.storage.maintenance_unit(book.company) as unit:
            repository = unit.repository("reading")
            execute_many = repository._session.executemany

            def fail_after_delete(sql, parameters):
                result = execute_many(sql, parameters)
                if sql.startswith("DELETE FROM read_item"):
                    deleted.append(sql)
                    raise RuntimeError("after exact derived deletes")
                return result

            monkeypatch.setattr(repository._session, "executemany", fail_after_delete)
            repository.publish(
                previous["scope_key"],
                index.COMPONENT,
                previous["wanted_head"],
                index.FORMAT_VERSION,
                prepared,
                guard=previous.guard,
                current_head=unit.head,
            )
    assert deleted
    assert _saved_rows(book) == before
    assert book.kernel.reading.run_pending(book.company)
    assert matches_complete(book)["review_businesses"][0]["business_amount_fen"] == "9"


@pytest.mark.parametrize("component", [index.COMPONENT, open_index.COMPONENT])
@pytest.mark.parametrize("phase", ["failed", "retry"])
def test_old_partial_cannot_publish_over_failure_or_same_head_full_retry(book, component, phase):
    first = book.seed()
    (publish_full if component == index.COMPONENT else publish_open)(book)
    first["data"]["amount_fen"] = "9"
    change(book, first)
    scope_key = _monthly.key(selection(book))
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        previous = CapturedState(repository.state(scope_key, component), read.head)
        model = (update if component == index.COMPONENT else open_update).prepare(
            book.kernel.reading, selection(book), read, previous
        )
    assert isinstance(model, (index.PreparedActivityPatch, open_index.PreparedOpenPatch))
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        unit.repository("reading").fail(
            scope_key,
            component,
            previous["wanted_head"],
            "production_failed",
            expected_revision=previous["revision"],
        )
    if phase == "retry":
        _monthly.retry(book.kernel.reading, selection(book), (component,))
        with book.kernel.storage.read_unit(book.company) as read:
            repository = read.repository("reading")
            full = (
                index.pending(repository, scope_key)["full"]
                if component == index.COMPONENT
                else open_inputs.pending(repository, scope_key)[0]
            )
            assert full
    before = _saved_rows(book, component)
    assert before[0]["wanted_head"] == previous["wanted_head"]
    assert before[0]["revision"] > previous["revision"]
    if phase == "retry" and component == open_index.COMPONENT:
        assert before[0]["ready_head"] == 0 and before[0]["content_digest"] is None
        assert before[1] is None
    else:
        assert before[0]["ready_head"] == previous["ready_head"]
        assert before[0]["content_digest"] == previous["content_digest"]
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        assert not unit.repository("reading").publish(
            scope_key,
            component,
            previous["wanted_head"],
            _monthly.version(component),
            model,
            guard=previous.guard,
            current_head=unit.head,
        )
    assert _saved_rows(book, component) == before
    if phase == "failed":
        _monthly.retry(book.kernel.reading, selection(book), (component,))
    assert book.kernel.reading.run_pending(book.company)
    (matches_complete if component == index.COMPONENT else matches_cold)(book)


@pytest.mark.parametrize("component", [index.COMPONENT, open_index.COMPONENT])
def test_a_notification_failure_uses_its_own_transaction_updated_revision(
    book, monkeypatch, component
):
    first = book.seed()
    (publish_full if component == index.COMPONENT else publish_open)(book)
    key = _monthly.key(selection(book))
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        before = repository.state(key, component)
        repository_type, original = type(repository), type(repository).mark
        old_head = read.head
    advanced = []

    def interrupted(repository, selected, name, head):
        result = original(repository, selected, name, head)
        if (selected, name) == (key, component):
            current = repository.state(selected, name)
            assert current["revision"] > before["revision"]
            advanced.append(current)
            raise ContractError(
                "storage_corrupt", "synthetic failure after this transaction's mark"
            )
        return result

    with monkeypatch.context() as patch:
        patch.setattr(repository_type, "mark", interrupted)
        first["data"]["amount_fen"] = "9"
        change(book, first)
    assert len(advanced) == 1
    with book.kernel.storage.read_unit(book.company) as read:
        failed = read.repository("reading").state(key, component)
        assert read.head == old_head + 1
        assert (failed["state"], failed["error_code"]) == ("failed", "storage_corrupt")
        assert failed["wanted_head"] == read.head
        assert failed["revision"] > advanced[0]["revision"]
    _monthly.retry(book.kernel.reading, selection(book), (component,))
    assert book.kernel.reading.run_pending(book.company)
    (matches_complete if component == index.COMPONENT else matches_cold)(book)
