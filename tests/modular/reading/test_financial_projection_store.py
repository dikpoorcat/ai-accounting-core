"""Opaque plans and exact work/descriptor CAS use real SQLite transactions."""

import sqlite3

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, content_digest
from ai_accounting_modular.ledger.public import FinancialProjectionPlan
from ai_accounting_modular.reading import _financial_projection as projection
from ai_accounting_modular.reading._financial_store import OWNER_PREFIX

from ._financial_projection_case import (
    captured,
    drain,
    page,
    publish,
    scope,
)
from ._financial_projection_case import (
    financial_case as financial_case,
)


def pending_plan(case):
    with case.kernel.storage.read_unit(case.company) as read:
        target = projection.store(read)
        work, token = target.work(), target.token()
        requests = projection._requests(case.kernel.reading, work, read)
        plan = case.kernel.ledger.prepare_financial_projection(
            tuple(requests.values()), (), (), read, base=None, rebuild=True
        )
        revision = target.state()["revision"]
    return token, plan, revision


def test_same_head_new_request_cannot_be_cleared_by_an_old_producer(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    token, plan, revision = pending_plan(case)
    page(case, "2026-02")
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        target = projection.store(unit)
        assert not target.publish(token, plan, rebuild=True)
        assert len(target.work()["pending"]) == 2
        assert target.header_json is None
        assert not target.fail(token, "production_failed", expected_revision=revision)
    drain(case)
    assert page(case).state == page(case, "2026-02").state == "ready"


def test_same_head_rebuild_does_not_let_an_old_plan_replace_the_new_base(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    old, plan, _ = pending_plan(case)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        assert projection.store(unit).publish(old, plan, rebuild=True)
        selected = projection.request(case.kernel.reading, scope(case), "summary_values", unit)
        projection.queue(case.kernel.reading, selected, unit, retry=True)
        assert not projection.store(unit).publish(old, plan, rebuild=True)


def test_opaque_batch_is_ordered_with_duplicates_missing_and_strict_metadata(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    drain(case)
    with case.kernel.storage.read_unit(case.company) as read:
        selected = projection.request(case.kernel.reading, scope(case), "summary_values", read)
        target = projection.store(read)
        found = target.get_blocks((selected.descriptor_key,))[0]
        assert target.get_blocks(
            (selected.descriptor_key, ("missing", "key"), selected.descriptor_key)
        ) == (found, None, found)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        collection, identity = selected.descriptor_key
        unit.repository("reading")._session.execute(
            "UPDATE read_item SET category='not-opaque' WHERE scope_key=? "
            "AND collection_key=? AND item_id=?",
            (projection.key(unit), OWNER_PREFIX + collection, identity),
        )
    with case.kernel.storage.read_unit(case.company) as read:
        with pytest.raises(ContractError) as error:
            projection.store(read).get_blocks((selected.descriptor_key,))
        assert error.value.code == "storage_corrupt"


def test_rebuild_replaces_all_owner_garbage_only_after_successful_cas(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    drain(case)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        target = projection.store(unit)
        target._session.execute(
            "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,component_key,head,"
            "payload_json) VALUES(?,?,?,?,'projection',?,'{}')",
            (projection.key(unit), OWNER_PREFIX + "unrooted", "orphan", "orphan", unit.head),
        )
        selected = projection.request(case.kernel.reading, scope(case), "summary_values", unit)
        projection.queue(case.kernel.reading, selected, unit, retry=True, rebuild=True)
    token, plan, _ = pending_plan(case)
    bad = FinancialProjectionPlan(
        plan.header_json, plan.header_digest, (*plan.writes, ("bad", "json", "not-json")), ()
    )
    with pytest.raises(sqlite3.IntegrityError):
        with case.kernel.storage.maintenance_unit(case.company) as unit:
            projection.store(unit).publish(token, bad, rebuild=True)
    with case.kernel.storage.read_unit(case.company) as read:
        target = projection.store(read)
        assert target.get_blocks((("unrooted", "orphan"),)) == ("{}",)
        assert target.token() == token
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        assert projection.store(unit).publish(token, plan, rebuild=True)
        assert projection.store(unit).get_blocks((("unrooted", "orphan"),)) == (None,)
        assert projection.store(unit).work()["registered"]


def test_unrelated_successful_producer_updates_do_not_starve_a_consumer(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    drain(case)
    prepared = captured(case)
    for month in ("2026-02", "2026-03"):
        page(case, month)
        assert projection.produce(case.kernel.reading, case.company)
    assert not publish(case, prepared, "2026-02")
    assert publish(case, prepared)


def test_changed_actual_request_descriptor_rejects_old_consumer(financial_case):
    case, _ = financial_case
    fact = case.seed()
    page(case)
    drain(case)
    prepared = captured(case)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        target = projection.store(unit)
        raw = canonical_json({"actual": "different request root"})
        target._session.execute(
            "UPDATE read_item SET payload_json=? WHERE scope_key=? "
            "AND collection_key=? AND item_id=?",
            (
                raw,
                projection.key(unit),
                OWNER_PREFIX + prepared.guard.descriptor_key[0],
                prepared.guard.descriptor_key[1],
            ),
        )
        assert not target.matches_consumer(prepared.guard, validate=False)
    assert not publish(case, prepared)
    assert fact["data"]["amount_fen"] == "100000"


def test_an_open_read_keeps_its_old_blocks_through_atomic_replacement(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    drain(case)
    with case.kernel.storage.read_unit(case.company) as old:
        target = projection.store(old)
        selected = projection.request(case.kernel.reading, scope(case), "summary_values", old)
        old_header = target.header_json
        old_body = target.get_blocks((selected.descriptor_key,))[0]
        with case.kernel.storage.maintenance_unit(case.company) as unit:
            current = projection.store(unit)
            projection.queue(case.kernel.reading, selected, unit, retry=True, rebuild=True)
            header = {"replacement": True}
            body = canonical_json({"different": "opaque block"})
            plan = FinancialProjectionPlan(
                canonical_json(header),
                content_digest(header),
                ((*selected.descriptor_key, body),),
                (),
            )
            assert current.publish(current.token(), plan, rebuild=True)
        assert target.header_json == old_header
        assert target.get_blocks((selected.descriptor_key,))[0] == old_body
    with case.kernel.storage.read_unit(case.company) as read:
        assert projection.store(read).get_blocks((selected.descriptor_key,)) == (body,)


def test_batched_block_read_does_not_query_each_block_again(financial_case, monkeypatch):
    case, _ = financial_case
    case.seed()
    page(case)
    page(case, "2026-02")
    drain(case)
    with case.kernel.storage.read_unit(case.company) as read:
        target = projection.store(read)
        keys = tuple(
            projection.request(
                case.kernel.reading, scope(case, month), "summary_values", read
            ).descriptor_key
            for month in ("2026-01", "2026-02")
        )
        fetchall, calls = target._session.fetchall, []

        def observed(sql, parameters=()):
            calls.append(sql)
            return fetchall(sql, parameters)

        monkeypatch.setattr(target._session, "fetchall", observed)
        assert all(target.get_blocks(keys))
        assert len(calls) == 1
