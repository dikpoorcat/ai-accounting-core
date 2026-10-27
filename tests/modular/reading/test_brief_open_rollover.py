"""A new month may inherit only a checked, current previous-month Open projection."""

from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import loads_json
from ai_accounting_modular.reading import (
    _brief_open as brief,
)
from ai_accounting_modular.reading import (
    _brief_open_index as index,
)
from ai_accounting_modular.reading import (
    _brief_open_inputs as inputs,
)
from ai_accounting_modular.reading import (
    _brief_open_sources as sources,
)
from ai_accounting_modular.reading import (
    _brief_open_update as update,
)
from ai_accounting_modular.reading import (
    _monthly,
    _open_store,
)
from ai_accounting_modular.reading._collections import CollectionFilter
from ai_accounting_modular.reading.public import MonthScope
from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import case as working_case

from ._publication import CapturedState, assert_subscriptions, body
from .test_brief_open import apply, payment, publish


@pytest.fixture(params=MODES)
def book(tmp_path, request):
    with install_ledger_implementation(request.param):
        fixture = working_case.__wrapped__(tmp_path)
        case = next(fixture)
        try:
            yield case
        finally:
            fixture.close()


def scope(book, month):
    return _monthly.normalize(
        {"company_id": book.company, "period_from": month, "period_to": month, "view": "as_posted"}
    )


def state(book, month):
    with book.kernel.storage.read_unit(book.company) as read:
        return read.repository("reading").state(_monthly.key(scope(book, month)), index.COMPONENT)


def ready(book, month):
    reader, selected = book.kernel.reading, MonthScope(book.company, month)
    for _ in range(6):
        result = reader.page("brief", selected, {"section": "open_items"})
        if result.state == "ready":
            return result
        assert result.state == "preparing", result
        reader.run_pending(book.company)
    pytest.fail("the actual public page did not become ready")


def cold_equal(book, month):
    selected = scope(book, month)
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        saved = body(repository, _monthly.key(selected), index.COMPONENT)
        expected = brief.prepare(book.kernel.reading, selected, read)
        assert_subscriptions(
            repository, _monthly.key(selected), index.COMPONENT, expected.model.inputs
        )
        actual, fresh = loads_json(saved["payload_json"]), loads_json(expected.payload_json)
        assert actual["_semantic_digest"] == fresh["_semantic_digest"]
        assert {key: value for key, value in actual.items() if key != "_item_epoch"} == {
            key: value for key, value in fresh.items() if key != "_item_epoch"
        }
        directory = index.Directory(repository, _monthly.key(selected), actual)
        rows = repository._session.fetchall(
            "SELECT * FROM read_item WHERE scope_key=? AND component_key=?",
            (_monthly.key(selected), index.COMPONENT),
        )
        assert {
            (row["collection_key"], row["item_id"]): row["payload_json"]
            for row in rows
            if row["collection_key"] != "_component_body"
        } == {(row[0], row[1]): row[-1] for row in expected.rows}
        for collection in index.PUBLIC:
            known = directory.item_page(collection, CollectionFilter(), None, 500)
            directory.check_many(known)
    return fresh["open_items"]


def advanced_source(book, *, count=8):
    first = book.seed()
    old = [first, *(book.expense(str(number)) for number in range(1, count))]
    apply(book, *old[1:])
    ready(book, "2026-01")
    added = book.expense("300", "2026-02")
    apply(book, added)
    assert book.kernel.reading.run_pending(book.company)
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        current = repository.state(_monthly.key(scope(book, "2026-01")), index.COMPONENT)
        payload = update.base(repository, current).payload
        assert current["state"] == "ready" and current["ready_head"] == current["wanted_head"]
        assert not repository.open_pending_rows(current["scope_key"])
        assert payload["open_items"]["current_cutoff_period"] == "2026-02"
    return old, added


def captures(monkeypatch):
    original, seen = sources.capture, []

    def observed(reader, selected, read, *, business_ids=None, **options):
        if selected["period_to"] == "2026-02":
            seen.append(None if business_ids is None else set(business_ids))
        return original(reader, selected, read, business_ids=business_ids, **options)

    monkeypatch.setattr(sources, "capture", observed)
    return seen


def register(book, month="2026-02"):
    selected = scope(book, month)
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        unit.repository("reading").register(
            _monthly.key(selected), index.COMPONENT, unit.head, _monthly.version(index.COMPONENT)
        )


def fork_plan(book, month="2026-02"):
    register(book, month)
    selected = scope(book, month)
    with book.kernel.storage.read_unit(book.company) as read:
        current = CapturedState(
            read.repository("reading").state(_monthly.key(selected), index.COMPONENT), read.head
        )
        model = update.prepare(book.kernel.reading, selected, read, current)
    assert isinstance(model, index.PreparedOpenPatch), "a checked previous month must yield a fork"
    assert model.base_scope == _monthly.key(scope(book, "2026-01"))
    return current, model


def publish_plan(book, current, model):
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        assert unit.repository("reading").publish(
            current["scope_key"],
            index.COMPONENT,
            current["wanted_head"],
            _monthly.version(index.COMPONENT),
            model,
            guard=current.guard,
            current_head=unit.head,
        )


def test_first_new_month_reads_only_new_claims_and_matches_the_actual_public_page(
    book, monkeypatch
):
    old, added = advanced_source(book)
    with monkeypatch.context() as hook:
        seen = captures(hook)
        result = ready(book, "2026-02")
    assert seen == [{added["business_id"]}], (
        "new-month Open must project only the real endpoint candidates, not old claim bodies",
        seen,
    )
    summary = cold_equal(book, "2026-02")
    assert summary["total_count"] == len(old) + 1
    assert result.payload["data"]["collections"]["open_items"]["page"]["total_count"] >= 1


def test_registered_unpublished_month_can_fork_without_a_duplicate_dirty_manifest(
    book, monkeypatch
):
    book.seed()
    apply(book, *(book.expense(str(number)) for number in range(1, 8)))
    ready(book, "2026-01")
    queued = book.kernel.reading.page(
        "brief", MonthScope(book.company, "2026-02"), {"section": "open_items"}
    )
    assert queued.state == "preparing"
    added = book.expense("300", "2026-02")
    apply(book, added)
    for _ in range(20):
        if state(book, "2026-01")["state"] == "ready":
            break
        book.kernel.reading.run_pending(book.company, limit=1)
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        previous = repository.state(_monthly.key(scope(book, "2026-01")), index.COMPONENT)
        assert previous["state"] == "ready" and previous["ready_head"] == previous["wanted_head"]
        assert (
            update.base(repository, previous).payload["open_items"]["current_cutoff_period"]
            == "2026-02"
        )
        target = repository.state(_monthly.key(scope(book, "2026-02")), index.COMPONENT)
        assert target["ready_head"] == 0 and target["content_digest"] is None
        assert repository.payload(target["scope_key"], index.COMPONENT) is None
        assert inputs.pending(repository, target["scope_key"]) == (False, set())
    with monkeypatch.context() as hook:
        seen = captures(hook)
        ready(book, "2026-02")
    assert seen == [{added["business_id"]}], "initial selection must reuse a checked prior month"
    assert cold_equal(book, "2026-02")["payable_fen"] == "100328"


@pytest.mark.parametrize(
    "unusable", ["missing", "pending", "failed", "old_horizon", "old_format", "bad_digest"]
)
def test_missing_unfinished_failed_or_old_horizon_sources_use_a_real_cold_capture(
    book, monkeypatch, unusable
):
    advanced_source(book)
    stale = None
    if unusable == "old_horizon":
        with book.kernel.storage.read_unit(book.company) as read:
            full = brief.prepare(book.kernel.reading, scope(book, "2026-01"), read)
        payload = loads_json(full.payload_json)
        payload["open_items"]["current_cutoff_period"] = "2026-01"
        # Isolate the horizon guard from pending/state guards with a sealed header.
        stale = replace(full, model=index.seal(payload, (), payload["_item_epoch"]))
    key = _monthly.key(scope(book, "2026-01"))
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        if unusable == "missing":
            repository._session.execute(
                "DELETE FROM read_component WHERE scope_key=? AND component_key=?",
                (key, index.COMPONENT),
            )
        elif unusable == "pending":
            repository.open_mark(key, unit.head, full=True)
        elif unusable == "failed":
            source = repository.state(key, index.COMPONENT)
            repository.fail(
                key,
                index.COMPONENT,
                source["wanted_head"],
                "production_failed",
                expected_revision=source["revision"],
                expected_state="ready",
            )
        elif unusable == "old_format":
            repository._session.execute(
                "UPDATE read_scope_head SET format_version=7 WHERE scope_key=? AND component_key=?",
                (key, index.COMPONENT),
            )
        elif unusable == "bad_digest":
            repository._session.execute(
                "UPDATE read_component SET payload_json='{}' WHERE scope_key=? AND component_key=?",
                (key, index.COMPONENT),
            )
        elif stale is not None:
            repository.open_mark(key, unit.head, full=True)
            repository._session.execute(
                "UPDATE read_scope_head SET state='preparing' "
                "WHERE scope_key=? AND component_key=?",
                (key, index.COMPONENT),
            )
            assert repository.publish(
                key,
                index.COMPONENT,
                unit.head,
                _monthly.version(index.COMPONENT),
                stale,
                guard=CapturedState(repository.state(key, index.COMPONENT), unit.head).guard,
                current_head=unit.head,
            )
    # Prepare Q directly so the background worker cannot repair P before selection.
    register(book)
    with monkeypatch.context() as hook:
        seen = captures(hook)
        with book.kernel.storage.read_unit(book.company) as read:
            selected = scope(book, "2026-02")
            source = CapturedState(
                read.repository("reading").state(_monthly.key(selected), index.COMPONENT), read.head
            )
            model = update.prepare(book.kernel.reading, selected, read, source)
    assert seen == [None]
    assert not isinstance(model, index.PreparedOpenPatch)
    publish_plan(book, source, model)
    cold_equal(book, "2026-02")


def test_source_same_head_full_repair_rejects_the_old_fork_without_touching_target(book):
    old, _ = advanced_source(book)
    replacement = deepcopy(old[0])
    replacement["data"]["amount_fen"] = "90000"
    apply(book, mutations=[{"op": "replace", "expected_seq": 1, "fact": replacement}])
    assert book.kernel.reading.run_pending(book.company)
    current, model = fork_plan(book)
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        source = repository.state(model.base_scope, index.COMPONENT)
        previous = loads_json(body(repository, model.base_scope, index.COMPONENT)["payload_json"])
        assert previous["_item_epoch"] < source["ready_head"]
    publish(book, "2026-01")
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        assert not repository.publish(
            current["scope_key"],
            index.COMPONENT,
            current["wanted_head"],
            _monthly.version(index.COMPONENT),
            model,
            guard=current.guard,
            current_head=unit.head,
        )
        assert repository.payload(current["scope_key"], index.COMPONENT) is None
        assert repository.state(current["scope_key"], index.COMPONENT) == current


def test_a_target_already_ready_at_the_same_wanted_head_rejects_the_old_fork(book):
    advanced_source(book)
    current, model = fork_plan(book)
    publish(book, "2026-02")
    before = state(book, "2026-02")
    assert before["ready_head"] == current["wanted_head"]
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        assert not unit.repository("reading").publish(
            current["scope_key"],
            index.COMPONENT,
            current["wanted_head"],
            _monthly.version(index.COMPONENT),
            model,
            guard=current.guard,
            current_head=unit.head,
        )
    assert state(book, "2026-02") == before
    cold_equal(book, "2026-02")


def test_a_late_target_payload_cannot_masquerade_as_an_initial_scope(book):
    advanced_source(book)
    current, model = fork_plan(book)
    with book.kernel.storage.read_unit(book.company) as read:
        cold = brief.prepare(book.kernel.reading, scope(book, "2026-02"), read)
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        repository._session.execute(
            "INSERT INTO read_component VALUES(?,?,?,?)",
            (current["scope_key"], index.COMPONENT, current["wanted_head"], cold.payload_json),
        )
        assert not repository.publish(
            current["scope_key"],
            index.COMPONENT,
            current["wanted_head"],
            _monthly.version(index.COMPONENT),
            model,
            guard=current.guard,
            current_head=unit.head,
        )
        assert repository.state(current["scope_key"], index.COMPONENT) == current
        assert (
            repository.payload(current["scope_key"], index.COMPONENT)["payload_json"]
            == cold.payload_json
        )


def test_a_fork_is_bound_to_its_actual_target_cutoff(book):
    advanced_source(book)
    current, model = fork_plan(book)
    register(book, "2026-03")
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        target = repository.state(_monthly.key(scope(book, "2026-03")), index.COMPONENT)
        assert not repository.publish(
            target["scope_key"],
            index.COMPONENT,
            target["wanted_head"],
            _monthly.version(index.COMPONENT),
            model,
            guard=current.guard,
            current_head=unit.head,
        )
        assert repository.state(target["scope_key"], index.COMPONENT) == target
        assert repository.payload(target["scope_key"], index.COMPONENT) is None


def test_initial_orphan_rows_are_cleared_without_becoming_inherited_history(book):
    advanced_source(book)
    current, model = fork_plan(book)
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        repository._session.execute(
            "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,payload_json,"
            "component_key,head) VALUES(?,?,?,?,?,?,?)",
            (current["scope_key"], "orphan", "orphan", "orphan", "{}", index.COMPONENT, 0),
        )
    publish_plan(book, current, model)
    cold_equal(book, "2026-02")


@pytest.mark.parametrize("changed", ["pending", "failed", "format", "payload"])
def test_source_changes_with_unchanged_target_head_reject_the_old_fork(book, changed):
    advanced_source(book)
    current, model = fork_plan(book)
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        source = repository.state(model.base_scope, index.COMPONENT)
        if changed == "pending":
            repository.open_mark(model.base_scope, unit.head, full=True)
        elif changed == "failed":
            repository.fail(
                model.base_scope,
                index.COMPONENT,
                source["wanted_head"],
                "production_failed",
                expected_revision=source["revision"],
                expected_state="ready",
            )
        elif changed == "format":
            repository._session.execute(
                "UPDATE read_scope_head SET format_version=7 WHERE scope_key=? AND component_key=?",
                (model.base_scope, index.COMPONENT),
            )
        else:
            repository._session.execute(
                "UPDATE read_component SET payload_json='{}' WHERE scope_key=? AND component_key=?",
                (model.base_scope, index.COMPONENT),
            )
        assert repository.state(current["scope_key"], index.COMPONENT) == current
        assert not repository.publish(
            current["scope_key"],
            index.COMPONENT,
            current["wanted_head"],
            _monthly.version(index.COMPONENT),
            model,
            guard=current.guard,
            current_head=unit.head,
        )
        assert repository.payload(current["scope_key"], index.COMPONENT) is None
        assert repository.state(current["scope_key"], index.COMPONENT) == current


@pytest.mark.parametrize("change", ["forward", "backward", "withdraw"])
def test_consumed_open_month_moves_and_physical_withdrawals_never_leave_inherited_members(
    book, monkeypatch, change
):
    old, added = advanced_source(book)
    if change == "withdraw":
        mutations = [{"op": "withdraw", "expected_seq": 1, "business_id": old[0]["business_id"]}]
        extra = []
    else:
        selected = deepcopy(old[0] if change == "forward" else added)
        selected["period"] = "2026-02" if change == "forward" else "2026-01"
        mutations = [{"op": "replace", "expected_seq": 1, "fact": selected}]
        extra = [book.expense("1", "2026-02")] if change == "backward" else []
    apply(
        book,
        mutations=[
            *mutations,
            *({"op": "create", "expected_seq": 0, "fact": fact} for fact in extra),
        ],
    )
    assert book.kernel.reading.run_pending(book.company)
    with monkeypatch.context() as hook:
        seen = captures(hook)
        current, model = fork_plan(book)
    assert all(value is not None for value in seen)
    assert not set().union(*seen).intersection(item["business_id"] for item in old[1:])
    publish_plan(book, current, model)
    summary = cold_equal(book, "2026-02")
    assert summary["payable_fen"] == (
        "328" if change == "withdraw" else "100329" if change == "backward" else "100328"
    )


def test_closed_retirement_of_a_fully_paid_claim_rediscovers_its_missing_member(book):
    expense = book.seed()
    cash = str(uuid4())
    paid = payment(book, expense["business_id"], cash, "100000")
    apply(
        book,
        paid,
        objects=[
            {
                "object_id": cash,
                "kind": "cash_account",
                "name": "合成现金",
                "expected_seq": 0,
                "data": {"currency": "CNY"},
            }
        ],
    )
    ready(book, "2026-01")
    assert book.close(book.check())["state"] == "closed"
    apply(
        book,
        mutations=[{"op": "withdraw", "expected_seq": 1, "business_id": paid["business_id"]}],
        correction_period="2026-02",
        explanation="已核实原现金付款误记，实际没有付出。",
    )
    assert book.kernel.reading.run_pending(book.company)
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        source = repository.state(_monthly.key(scope(book, "2026-01")), index.COMPONENT)
        assert update.base(repository, source).payload["open_items"]["total_count"] == 0
    current, model = fork_plan(book)
    publish_plan(book, current, model)
    assert cold_equal(book, "2026-02")["payable_fen"] == "100000"


def test_opening_completeness_is_read_for_q_instead_of_inherited_from_a_checked_header(
    book, monkeypatch
):
    _, added = advanced_source(book)
    selected = scope(book, "2026-01")
    with book.kernel.storage.read_unit(book.company) as read:
        model = brief.prepare(book.kernel.reading, selected, read)
    payload = loads_json(model.payload_json)
    payload["open_items"].update(complete=False, payable_fen=None, receivable_fen=None)
    # Interface-level fixture: a sealed, internally valid derived header is not
    # proof of the new cutoff's completeness. This is not a natural Apply sequence.
    incomplete = replace(model, model=index.seal(payload, (), payload["_item_epoch"]))
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        repository.open_mark(_monthly.key(selected), unit.head, full=True)
        repository._session.execute(
            "UPDATE read_scope_head SET state='preparing' WHERE scope_key=? AND component_key=?",
            (_monthly.key(selected), index.COMPONENT),
        )
        assert repository.publish(
            _monthly.key(selected),
            index.COMPONENT,
            unit.head,
            _monthly.version(index.COMPONENT),
            incomplete,
            guard=CapturedState(
                repository.state(_monthly.key(selected), index.COMPONENT), unit.head
            ).guard,
            current_head=unit.head,
        )
    with monkeypatch.context() as hook:
        seen = captures(hook)
        current, fork = fork_plan(book)
    assert seen == [{added["business_id"]}], "unknown completeness must not force old-body replay"
    publish_plan(book, current, fork)
    assert cold_equal(book, "2026-02")["complete"] is True


def test_repeated_closed_transfer_corrections_reproject_the_original_and_final_owners(
    book, monkeypatch
):
    first = book.seed()
    second, third = book.expense(), book.expense()
    owner = str(uuid4())
    accepted = {
        "business_id": str(uuid4()),
        "kind": "operations.reimbursement",
        "period": "2026-01",
        "evidence_ids": [book.source_id],
        "data": {
            "type": "acceptance",
            "payer_id": owner,
            "payer_kind": "owner",
            "sources": [
                {
                    "source": {"business_id": first["business_id"], "slot": "primary"},
                    "amount_fen": "40000",
                    "recipient_id": book.party,
                }
            ],
        },
    }
    apply(
        book,
        second,
        third,
        accepted,
        objects=[
            {
                "object_id": owner,
                "kind": "person",
                "name": "实际垫付人",
                "expected_seq": 0,
                "data": {},
            }
        ],
    )
    ready(book, "2026-01")
    assert book.close(book.check())["state"] == "closed"
    for seq, target in enumerate((second, third), 1):
        accepted["data"]["sources"][0]["source"]["business_id"] = target["business_id"]
        apply(
            book,
            mutations=[
                {
                    "op": "replace",
                    "expected_seq": seq,
                    "fact": deepcopy(accepted),
                }
            ],
            correction_period="2026-02",
        )
        assert book.kernel.reading.run_pending(book.company)
    with monkeypatch.context() as hook:
        seen = captures(hook)
        current, model = fork_plan(book)
    assert None not in seen
    assert set().union(*seen) >= {
        first["business_id"],
        third["business_id"],
        accepted["business_id"],
    }
    publish_plan(book, current, model)
    assert cold_equal(book, "2026-02")["payable_fen"] == "300000"


def test_later_formal_work_rejects_a_first_fork_even_without_existing_subscriptions(book):
    advanced_source(book)
    current, model = fork_plan(book)
    apply(book, book.expense("7", "2026-02"))
    after = state(book, "2026-02")
    assert after["wanted_head"] == current["wanted_head"]
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        assert not repository.publish(
            current["scope_key"],
            index.COMPONENT,
            current["wanted_head"],
            _monthly.version(index.COMPONENT),
            model,
            guard=current.guard,
            current_head=unit.head,
        )
        assert repository.payload(current["scope_key"], index.COMPONENT) is None
        assert repository.state(current["scope_key"], index.COMPONENT) == after
    ready(book, "2026-02")
    assert cold_equal(book, "2026-02")["payable_fen"] == "100335"


def test_node_publication_failure_rolls_back_shared_nodes_and_target_header(book, monkeypatch):
    advanced_source(book)
    current, model = fork_plan(book)
    with book.kernel.storage.read_unit(book.company) as read:
        before = tuple(
            dict(row)
            for row in read.repository("reading")._session.fetchall(
                "SELECT * FROM read_item WHERE scope_key=? ORDER BY collection_key,item_id",
                (_open_store.SCOPE,),
            )
        )
    published = []
    original = _open_store.publish

    def fail_after_nodes(repository, token, plan):
        result = original(repository, token, plan)
        assert result
        published.append(plan)
        raise RuntimeError("shared node boundary failed")

    with pytest.raises(RuntimeError, match="shared node boundary failed"):
        with book.kernel.storage.maintenance_unit(book.company) as unit:
            repository = unit.repository("reading")
            monkeypatch.setattr(_open_store, "publish", fail_after_nodes)
            repository.publish(
                current["scope_key"],
                index.COMPONENT,
                current["wanted_head"],
                _monthly.version(index.COMPONENT),
                model,
                guard=current.guard,
                current_head=unit.head,
            )
    assert published, "the fault must happen after actual shared-node publication"
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        assert repository.state(current["scope_key"], index.COMPONENT) == current
        assert repository.payload(current["scope_key"], index.COMPONENT) is None
        assert repository.item_counts(
            current["scope_key"], index.COMPONENT, "open_members", CollectionFilter()
        ) == (0, 0)
        assert (
            tuple(
                dict(row)
                for row in repository._session.fetchall(
                    "SELECT * FROM read_item WHERE scope_key=? ORDER BY collection_key,item_id",
                    (_open_store.SCOPE,),
                )
            )
            == before
        )
    cold_equal(book, "2026-01")
