"""Ordinary changes touch their own projection, with a cold-equivalent content version."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import MAX_FEN, ContractError, PageQuery, loads_json
from ai_accounting_modular.reading import (
    _brief_open,
    _brief_open_buckets,
    _brief_open_index,
    _brief_open_inputs,
    _brief_open_sources,
    _brief_open_totals,
    _brief_open_update,
    _monthly,
)
from ai_accounting_modular.reading._collections import CollectionFilter, collection_page
from ai_accounting_modular.reading._repository import ReadingRepository
from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import case as case
from tests.modular.domains.people.test_read_remuneration import bank_object

from .test_brief_open import apply, members, page, payment, publish


def scope(case):
    return _monthly.normalize(
        {
            "company_id": case.company,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "view": "as_posted",
        }
    )


def snapshot(case):
    with case.kernel.storage.read_unit(case.company) as read:
        return _monthly.ready_snapshot(
            case.kernel.reading, scope(case), (_monthly.BRIEF_OPEN_ITEMS,), read
        )


def matches_cold(case):
    with case.kernel.storage.read_unit(case.company) as read:
        fresh = _brief_open.prepare(case.kernel.reading, scope(case), read)
        cached = read.repository("reading").payload(
            _monthly.key(scope(case)), _monthly.BRIEF_OPEN_ITEMS
        )
    expected, actual = loads_json(fresh.payload_json), loads_json(cached["payload_json"])
    assert expected["_semantic_digest"] == actual["_semantic_digest"]
    for key in ("_item_epoch", "_semantic_digest"):
        expected.pop(key)
        actual.pop(key)
    assert expected == actual
    return fresh


def change(case, fact, amount, seq=1, **options):
    fact = deepcopy(fact)
    fact["data"]["amount_fen"] = amount
    return apply(case, mutations=[{"op": "replace", "expected_seq": seq, "fact": fact}], **options)


def test_hot_change_reads_only_changed_business_and_matches_a_cold_model(case, monkeypatch):
    first = case.seed()
    second = case.expense("200000")
    apply(case, second)
    publish(case)
    initial = snapshot(case)
    change(case, first, "90000")
    capture, seen = _brief_open_sources.capture, []

    def bounded(reader, selected, read, *, business_ids=None, **kwargs):
        assert business_ids is not None, "A hot business change must not capture every obligation"
        seen.extend(business_ids)
        return capture(reader, selected, read, business_ids=business_ids, **kwargs)

    published = []
    original_publish = ReadingRepository.publish
    original_items = ReadingRepository.item_page

    def no_unrelated_members(repository, key, component, collection, *args):
        assert collection != "open_members", (
            "an unchanged group representative needs no member body"
        )
        return original_items(repository, key, component, collection, *args)

    def publish_patch(repository, key, component, wanted, format_version, model):
        assert isinstance(model, _brief_open_index.PreparedOpenPatch)
        published.append(model)
        return original_publish(repository, key, component, wanted, format_version, model)

    with monkeypatch.context() as patch:
        patch.setattr(_brief_open_sources, "capture", bounded)
        patch.setattr(ReadingRepository, "publish", publish_patch)
        patch.setattr(ReadingRepository, "item_page", no_unrelated_members)
        assert case.kernel.reading.run_pending(case.company)
    after = snapshot(case)
    assert after is not None and after.version != initial.version
    assert set(seen) == {first["business_id"]}
    unchanged = second["business_id"] + ":primary"
    assert all(("open_members", unchanged) not in model.deletes for model in published)
    assert all(
        not (row[0] == "open_members" and row[1] == unchanged)
        for model in published
        for row in model.model.rows
    )
    assert after.components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]["payable_fen"] == "290000"
    matches_cold(case)


def test_two_commits_coalesce_and_removing_last_group_member_matches_cold(case):
    first = case.seed()
    second = case.expense("200000")
    apply(case, second)
    publish(case)
    for fact in (first, second):
        apply(
            case,
            mutations=[{"op": "withdraw", "expected_seq": 1, "business_id": fact["business_id"]}],
        )
    assert case.kernel.reading.run_pending(case.company)
    selected = snapshot(case)
    assert selected is not None
    summary = selected.components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]
    assert summary["group_count"] == summary["total_count"] == 0
    assert summary["categories"] == []
    matches_cold(case)


def test_full_rebuild_changes_storage_epoch_but_keeps_public_content_version(case):
    first = case.seed()
    publish(case)
    change(case, first, "90000")
    assert case.kernel.reading.run_pending(case.company)
    before = snapshot(case)
    with case.kernel.storage.read_unit(case.company) as read:
        previous = read.repository("reading").payload(
            _monthly.key(scope(case)), _monthly.BRIEF_OPEN_ITEMS
        )
    after = publish(case)
    with case.kernel.storage.read_unit(case.company) as read:
        current = read.repository("reading").payload(
            _monthly.key(scope(case)), _monthly.BRIEF_OPEN_ITEMS
        )
    assert (
        loads_json(previous["payload_json"])["_item_epoch"]
        != loads_json(current["payload_json"])["_item_epoch"]
    )
    assert before.version == after.version and before.components == after.components


def test_stale_patch_cannot_publish_or_clear_a_newer_committed_business(case):
    first = case.seed()
    second = case.expense("200000")
    apply(case, second)
    publish(case)
    change(case, first, "90000")
    key = _monthly.key(scope(case))
    with case.kernel.storage.read_unit(case.company) as read:
        state = read.repository("reading").state(key, _monthly.BRIEF_OPEN_ITEMS)
        prepared = _brief_open_update.prepare(case.kernel.reading, scope(case), read, state)
    change(case, second, "180000")
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        assert not repository.publish(
            key,
            _monthly.BRIEF_OPEN_ITEMS,
            state["wanted_head"],
            _monthly.version(_monthly.BRIEF_OPEN_ITEMS),
            prepared,
        )
        full, pending = _brief_open_inputs.pending(repository, key)
        assert not full and pending == {first["business_id"], second["business_id"]}
    assert case.kernel.reading.run_pending(case.company)
    assert (
        snapshot(case).components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]["payable_fen"]
        == "270000"
    )
    matches_cold(case)


def test_corrupt_compact_bucket_fails_and_explicit_full_retry_includes_later_changes(case):
    first = case.seed()
    publish(case)
    key = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_item SET payload_json='{}' WHERE scope_key=? AND collection_key=?",
            (key, _brief_open_index.BUCKETS),
        )
    change(case, first, "90000")
    assert case.kernel.reading.run_pending(case.company) == ()
    change(case, first, "80000", seq=2)
    assert case.kernel.reading.run_pending(case.company) == ()
    _monthly.retry(case.kernel.reading, scope(case), (_monthly.BRIEF_OPEN_ITEMS,))
    assert case.kernel.reading.run_pending(case.company)
    assert (
        snapshot(case).components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]["payable_fen"] == "80000"
    )
    matches_cold(case)


def test_closed_january_progress_updates_from_a_february_payment_without_full_capture(
    case, monkeypatch
):
    first = case.seed()
    assert case.close(case.check())["state"] == "closed"
    later = case.expense("1000", "2026-02")
    bank = bank_object()
    apply(case, later, objects=[bank])
    publish(case)
    actual_payment = payment(case, first["business_id"], bank["object_id"], "60000", "2026-02")
    apply(case, actual_payment)
    capture = case.kernel.ledger.capture_obligation_directory

    def known_directory(query, read):
        assert query.business_ids is not None, "future follow-up uses known January producers"
        return capture(query, read)

    with monkeypatch.context() as patch:
        patch.setattr(case.kernel.ledger, "capture_obligation_directory", known_directory)
        assert case.kernel.reading.run_pending(case.company)
    selected = snapshot(case).components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]
    assert selected["payable_fen"] == "100000"
    with case.kernel.storage.read_unit(case.company) as read:
        member = read.repository("reading").item_get(
            _monthly.key(scope(case)),
            _monthly.BRIEF_OPEN_ITEMS,
            "open_members",
            [first["business_id"] + ":primary"],
        )[0]
    value = loads_json(member["payload_json"])["value"]
    assert value["outstanding_fen"] == "100000" and value["current_outstanding_fen"] == "40000"
    matches_cold(case)


def test_unknown_group_preserves_large_known_subtotals_until_they_become_known():
    totals = _brief_open_totals
    fold = totals.empty()
    first = {field: "0" for field in totals.FIELDS} | {
        "group_key": "g",
        "category_key": "other_payables",
        "party": "合成对象",
        "_matter_title": "合成事项",
        "status": "open",
        "current_status": "open",
        "source_amount_fen": str(MAX_FEN),
    }
    unknown = first | {"source_amount_fen": None}
    totals.adjust(fold, first, 1)
    totals.adjust(fold, first, 1)
    totals.adjust(fold, unknown, 1)
    assert totals.group(first, fold)["source_amount_fen"] is None
    totals.adjust(fold, unknown, -1)
    with pytest.raises(ValueError):
        totals.group(first, fold)
    totals.adjust(fold, first, -1)
    assert totals.group(first, fold)["source_amount_fen"] == str(MAX_FEN)


def test_group_move_and_earliest_member_removal_keep_unaffected_rows(case):
    case.seed(expense=False)
    facts = [case.expense(str(amount)) for amount in (100, 200, 300)]
    for number, fact in enumerate(facts):
        fact["business_id"] = f"10000000-0000-4000-8000-{number + 1:012d}"
    apply(case, *facts)
    publish(case)
    changed = deepcopy(facts[0])
    changed["data"]["expense_class"] = "sales"
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}])
    assert case.kernel.reading.run_pending(case.company)
    matches_cold(case)
    summary = snapshot(case).components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]
    assert summary["group_count"] == 2 and summary["total_count"] == 3
    apply(
        case,
        mutations=[{"op": "withdraw", "expected_seq": 2, "business_id": facts[0]["business_id"]}],
    )
    assert case.kernel.reading.run_pending(case.company)
    summary = snapshot(case).components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]
    assert summary["group_count"] == 1 and summary["payable_fen"] == "500"
    matches_cold(case)


def test_referenced_object_rename_uses_full_fallback_and_changes_display_version(case, monkeypatch):
    case.seed()
    before = publish(case)
    with case.kernel.storage.read_unit(case.company) as read:
        party = case.kernel.identity.get_batch([case.party], read)[0]
    apply(
        case,
        objects=[
            {
                **{field: party[field] for field in ("object_id", "kind", "data")},
                "expected_seq": party["seq"],
                "name": "核实后的供应商名称",
            }
        ],
    )
    capture, kinds = _brief_open_sources.capture, []

    def observed(*args, **kwargs):
        kinds.append(kwargs.get("business_ids"))
        return capture(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(_brief_open_sources, "capture", observed)
        assert case.kernel.reading.run_pending(case.company)
    assert kinds == [None]
    assert snapshot(case).version != before.version
    matches_cold(case)


def test_failed_atomic_patch_rolls_back_rows_and_keeps_work_for_full_retry(case, monkeypatch):
    first = case.seed()
    publish(case)
    key = _monthly.key(scope(case))
    change(case, first, "90000")
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        old_payload = repository.payload(key, _monthly.BRIEF_OPEN_ITEMS)
        session_type = type(repository._session)
        original = session_type.executemany

    def interrupted(session, sql, args):
        if "INSERT INTO read_item" in sql and "ON CONFLICT" not in sql:
            raise RuntimeError("synthetic publication interrupted after deletes")
        return original(session, sql, args)

    with monkeypatch.context() as patch:
        patch.setattr(session_type, "executemany", interrupted)
        assert case.kernel.reading.run_pending(case.company) == ()
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        assert repository.payload(key, _monthly.BRIEF_OPEN_ITEMS) == old_payload
        full, pending = _brief_open_inputs.pending(repository, key)
        assert not full and pending == {first["business_id"]}
        assert repository.item_get(
            key, _monthly.BRIEF_OPEN_ITEMS, "open_members", [first["business_id"] + ":primary"]
        )
    _monthly.retry(case.kernel.reading, scope(case), (_monthly.BRIEF_OPEN_ITEMS,))
    assert case.kernel.reading.run_pending(case.company)
    matches_cold(case)


@pytest.mark.parametrize("collection", [_brief_open_index.BUSINESSES, _brief_open_index.OBJECTS])
def test_missing_dependency_index_cannot_silently_ignore_a_later_notification(case, collection):
    first = case.seed()
    formal = collection == _brief_open_index.BUSINESSES
    if formal:
        assert case.close(case.check())["state"] == "closed"
        bank = bank_object()
        apply(case, case.expense("1000", "2026-02"), objects=[bank])
    publish(case)
    key = _monthly.key(scope(case))
    identity = first["business_id"] if formal else case.party
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        assert (
            unit.repository("reading")._session.execute(
                "DELETE FROM read_item WHERE scope_key=? AND collection_key=? AND item_id=?",
                (key, _brief_open_index.BUCKETS, _brief_open_buckets.key(collection, identity)),
            )
            == 1
        )
    if formal:
        apply(case, payment(case, first["business_id"], bank["object_id"], "60000", "2026-02"))
    else:
        with case.kernel.storage.read_unit(case.company) as read:
            party = case.kernel.identity.get_batch([case.party], read)[0]
        apply(
            case,
            objects=[
                {
                    **{field: party[field] for field in ("object_id", "kind", "data")},
                    "expected_seq": party["seq"],
                    "name": "通知后的实际名称",
                }
            ],
        )
    with case.kernel.storage.read_unit(case.company) as read:
        state = read.repository("reading").state(key, _monthly.BRIEF_OPEN_ITEMS)
    assert (state["state"], state["error_code"]) == ("failed", "storage_corrupt")
    assert case.kernel.reading.run_pending(case.company) == ()
    _monthly.retry(case.kernel.reading, scope(case), (_monthly.BRIEF_OPEN_ITEMS,))
    assert case.kernel.reading.run_pending(case.company)
    matches_cold(case)


@pytest.mark.parametrize("collection", [_brief_open_index.BUSINESSES, _brief_open_index.OBJECTS])
@pytest.mark.parametrize(
    "corruption", ["identity", "sort", "epoch", "body", "collection", "metadata"]
)
def test_same_count_dependency_damage_cannot_hide_a_real_notification(
    case, monkeypatch, collection, corruption
):
    first = case.seed()
    formal = collection == _brief_open_index.BUSINESSES
    if formal:
        assert case.close(case.check())["state"] == "closed"
        bank = bank_object()
        apply(case, case.expense("1000", "2026-02"), objects=[bank])
    publish(case)
    key = _monthly.key(scope(case))
    identity = first["business_id"] if formal else case.party
    row_id = _brief_open_buckets.key(collection, identity)
    edits = {
        "identity": ("item_id=?", str(uuid4())),
        "sort": ("sort_key=?", "damaged order"),
        "epoch": ("head=?", 0),
        "body": ("payload_json=json_set(payload_json,'$.values',json(?))", "[]"),
        "collection": ("payload_json=json_set(payload_json,'$.collection',?)", "wrong"),
        "metadata": ("group_key=?", "not a logical group"),
    }
    expression, replacement = edits[corruption]
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        assert (
            unit.repository("reading")._session.execute(
                f"UPDATE read_item SET {expression} WHERE scope_key=? AND component_key=? "
                "AND collection_key=? AND item_id=?",
                (replacement, key, _monthly.BRIEF_OPEN_ITEMS, _brief_open_index.BUCKETS, row_id),
            )
            == 1
        )

    notification = (
        payment(case, first["business_id"], bank["object_id"], "60000", "2026-02")
        if formal
        else None
    )
    allowed = {
        _brief_open_buckets.key(collection, identity),
        *([_brief_open_buckets.key(collection, notification["business_id"])] if formal else []),
    }
    original = ReadingRepository.open_bucket_rows

    def bounded(repository, selected_scope, keys):
        assert set(keys) <= allowed, "a notification reads only involved identity buckets"
        return original(repository, selected_scope, keys)

    with monkeypatch.context() as patch:
        patch.setattr(ReadingRepository, "open_bucket_rows", bounded)
        if formal:
            apply(case, notification)
        else:
            with case.kernel.storage.read_unit(case.company) as read:
                party = case.kernel.identity.get_batch([case.party], read)[0]
            apply(
                case,
                objects=[
                    {
                        **{field: party[field] for field in ("object_id", "kind", "data")},
                        "expected_seq": party["seq"],
                        "name": "通知后的实际名称",
                    }
                ],
            )
    with case.kernel.storage.read_unit(case.company) as read:
        state = read.repository("reading").state(key, _monthly.BRIEF_OPEN_ITEMS)
    assert (state["state"], state["error_code"]) == ("failed", "storage_corrupt")
    assert case.kernel.reading.run_pending(case.company) == ()
    _monthly.retry(case.kernel.reading, scope(case), (_monthly.BRIEF_OPEN_ITEMS,))
    assert case.kernel.reading.run_pending(case.company)
    matches_cold(case)


def test_closed_correction_uses_each_partys_actual_formal_identity_without_full_scan(
    case, monkeypatch
):
    first = case.seed()
    assert case.close(case.check())["state"] == "closed"
    apply(case, case.expense("1000", "2026-02"))
    publish(case)
    revised = deepcopy(first)
    party = str(uuid4())
    revised["data"].update(creditor_id=party, amount_fen="80000")
    apply(
        case,
        mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}],
        objects=[
            {
                "object_id": party,
                "kind": "organization",
                "name": "新的实际债权方",
                "expected_seq": 0,
                "data": {},
            }
        ],
        correction_period="2026-02",
    )
    capture = case.kernel.ledger.capture_obligation_directory

    def known_directory(query, read):
        assert query.business_ids is not None, "correction uses exact historical/current businesses"
        return capture(query, read)

    with monkeypatch.context() as patch:
        patch.setattr(case.kernel.ledger, "capture_obligation_directory", known_directory)
        assert case.kernel.reading.run_pending(case.company)
    with case.kernel.storage.read_unit(case.company) as read:
        selected = collection_page(
            _monthly.key(scope(case)),
            _monthly.BRIEF_OPEN_ITEMS,
            "open_items",
            CollectionFilter(),
            PageQuery(),
            read,
        )
    value = selected.items[0]
    assert value["party"] == "合成供应商" and value["outstanding_fen"] == "100000"
    assert value["current_status"] == "reversed" and value["current_outstanding_fen"] == "0"
    matches_cold(case)


def test_member_epoch_must_equal_the_full_generation_not_merely_be_older(case):
    case.seed()
    publish(case)
    key = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        epoch = loads_json(repository.payload(key, _monthly.BRIEF_OPEN_ITEMS)["payload_json"])[
            "_item_epoch"
        ]
        assert epoch > 0
        repository._session.execute(
            "UPDATE read_item SET head=? WHERE scope_key=? AND collection_key='open_members'",
            (epoch - 1, key),
        )
    with case.kernel.storage.read_unit(case.company) as read, pytest.raises(ContractError) as error:
        collection_page(
            key, _monthly.BRIEF_OPEN_ITEMS, "open_members", CollectionFilter(), PageQuery(), read
        )
    assert error.value.code == "storage_corrupt"


def test_explicit_full_retry_does_not_depend_on_a_corrupt_old_header_or_pending_row(case):
    first = case.seed()
    publish(case)
    change(case, first, "90000")
    key = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        repository._session.execute(
            "UPDATE read_component SET payload_json='{}' WHERE scope_key=? AND component_key=?",
            (key, _monthly.BRIEF_OPEN_ITEMS),
        )
        repository._session.execute(
            "UPDATE read_item SET payload_json='{}' WHERE scope_key=? AND collection_key=?",
            (key, _brief_open_index.DIRTY),
        )
    assert case.kernel.reading.run_pending(case.company) == ()
    _monthly.retry(case.kernel.reading, scope(case), (_monthly.BRIEF_OPEN_ITEMS,))
    assert case.kernel.reading.run_pending(case.company)
    assert (
        snapshot(case).components[_monthly.BRIEF_OPEN_ITEMS]["open_items"]["payable_fen"] == "90000"
    )
    matches_cold(case)


@pytest.mark.parametrize("mode", MODES)
def test_both_real_factories_keep_private_bucket_delta_and_public_pages_cold_equal(tmp_path, mode):
    with install_ledger_implementation(mode):
        fixture = case.__wrapped__(tmp_path)
        book = next(fixture)
        try:
            first = book.seed()
            second = book.expense("200000")
            apply(book, second)
            publish(book)
            revised, party = deepcopy(first), str(uuid4())
            revised["data"].update(creditor_id=party, amount_fen="80000")
            apply(
                book,
                mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}],
                objects=[
                    {
                        "object_id": party,
                        "kind": "organization",
                        "name": "新实际供应商",
                        "expected_seq": 0,
                        "data": {},
                    }
                ],
            )
            assert book.kernel.reading.run_pending(book.company)
            matches_cold(book)
            apply(
                book,
                mutations=[
                    {
                        "op": "withdraw",
                        "expected_seq": 1,
                        "business_id": second["business_id"],
                    }
                ],
            )
            assert book.kernel.reading.run_pending(book.company)
            hot = snapshot(book)
            overview = page(book, hot)
            assert overview["open_items"]["payable_fen"] == "80000"
            group = overview["collections"]["open_items"]["items"][0]["group_key"]
            detail = members(book, hot, group, limit=500)
            matches_cold(book)
            cold = publish(book)
            assert page(book, cold) == overview
            assert members(book, cold, group, limit=500) == detail
        finally:
            fixture.close()
