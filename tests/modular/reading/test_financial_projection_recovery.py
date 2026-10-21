"""Corrupt derived caches cannot turn a valid formal submission into a failure."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import canonical_json, content_digest
from ai_accounting_modular.ledger.public import FINANCIAL_PROJECTION_FORMAT
from ai_accounting_modular.reading import _financial_projection as projection
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading._financial_store import CONTROL, OWNER_PREFIX
from ai_accounting_modular.reading.public import MonthScope

from ._financial_projection_case import (
    drain,
    page,
    quarter,
    scope,
)
from ._financial_projection_case import (
    financial_case as financial_case,
)
from .test_financial_projection_integration import live_case as live_case


def reading_rows(case, *, healthy_only=False):
    with case.kernel.storage.read_unit(case.company) as read:
        session = read.repository("reading")._session
        where = " WHERE component_key NOT IN ('financial','projection')" if healthy_only else ""
        return {
            table: sorted(
                canonical_json(dict(row))
                for row in session.fetchall("SELECT * FROM " + table + where)
            )
            for table in ("read_scope_head", "read_component", "read_item")
        }


def formal_documents(case):
    with case.kernel.storage.read_unit(case.company) as read:
        root = case.kernel.storage.company_metadata(read).company_root / "standard"
        return (
            read.head,
            case.kernel.records.document_status(read),
            {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            },
        )


def ready_views(case):
    reader, selected = case.kernel.reading, MonthScope(case.company, "2026-01")
    reader.page("brief", selected, {})
    quarter(case)
    drain(case)
    brief, report = reader.page("brief", selected, {}), quarter(case)
    assert brief.state == report.state == "ready"
    return page(case), report


def retry_view(case, entry):
    if entry == "monthly":
        return case.kernel.reading.retry("brief", MonthScope(case.company, "2026-01"), {})
    return case.kernel.reading.retry(
        "quarterly_report",
        {
            "company_id": case.company,
            "period_from": "2026-01",
            "period_to": "2026-03",
            "view": "as_posted",
        },
    )


@pytest.mark.parametrize("entry", ("monthly", "quarterly"))
@pytest.mark.parametrize("damage", ("old_format", "storage_corrupt"))
def test_ready_consumer_explicit_retry_rebuilds_obsolete_or_corrupt_real_producer(
    live_case, entry, damage
):
    case = live_case
    case.seed()
    expected = ready_views(case)
    unchanged, documents = reading_rows(case, healthy_only=True), formal_documents(case)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        target = projection.store(unit)
        work = target.work()
        assert not work["pending"]
        if damage == "old_format":
            # The former owner format also used different request identities.
            work["registered"] = {
                content_digest(
                    {
                        "format": 1,
                        "company_id": unit.company_id,
                        "database_id": unit.database_id,
                        **value,
                    }
                ): value
                for value in work["registered"].values()
            }
            target.save_work(work, unit.head)
            target._session.execute(
                "UPDATE read_scope_head SET format_version=1,state='ready' WHERE scope_key=?",
                (target.scope_key,),
            )
        else:
            target._session.execute(
                "UPDATE read_scope_head SET state='failed',error_code='storage_corrupt' "
                "WHERE scope_key=?",
                (target.scope_key,),
            )
        assert all(
            state["state"] == "ready"
            for state in unit.repository("reading").scopes()
            if state["component_key"] == "financial"
        )
    retry_view(case, entry)
    with case.kernel.storage.read_unit(case.company) as read:
        target = projection.store(read)
        assert target.state()["state"] == "preparing"
        assert target.state()["format_version"] == FINANCIAL_PROJECTION_FORMAT
        assert target.work()["rebuild"] is True
        consumers = [
            state
            for state in read.repository("reading").scopes()
            if state["component_key"] == "financial" and state["state"] == "preparing"
        ]
        assert len(consumers) == 1
        assert ('"page":"monthly"' in consumers[0]["scope_key"]) == (entry == "monthly")
    assert reading_rows(case, healthy_only=True) == unchanged
    completed = drain(case)
    assert consumers[0]["scope_key"] in completed
    assert ready_views(case) == expected
    assert reading_rows(case, healthy_only=True) == unchanged
    assert formal_documents(case) == documents


def test_ready_real_producer_and_consumers_explicit_retry_is_a_noop(live_case):
    case = live_case
    case.seed()
    expected = ready_views(case)
    before, documents = reading_rows(case), formal_documents(case)
    retry_view(case, "monthly")
    retry_view(case, "quarterly")
    assert reading_rows(case) == before
    assert case.kernel.reading.run_pending(case.company) == ()
    assert ready_views(case) == expected
    assert reading_rows(case) == before
    assert formal_documents(case) == documents


def test_retry_does_not_register_an_unvisited_scope_even_with_an_obsolete_producer(live_case):
    case = live_case
    case.seed()
    quarter(case)
    drain(case)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        target = projection.store(unit)
        target._session.execute(
            "UPDATE read_scope_head SET format_version=1 WHERE scope_key=?", (target.scope_key,)
        )
    before = reading_rows(case)
    retry_view(case, "monthly")
    assert reading_rows(case) == before
    assert case.kernel.reading.run_pending(case.company) == ()


def descriptor(case):
    with case.kernel.storage.read_unit(case.company) as read:
        request = projection.request(case.kernel.reading, scope(case), "summary_values", read)
        return request, projection.store(read).get_blocks((request.descriptor_key,))[0]


def remove_descriptor(case, selected):
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        target = projection.store(unit)
        target._session.execute(
            "DELETE FROM read_item WHERE scope_key=? AND collection_key=? AND item_id=?",
            (
                projection.key(unit),
                OWNER_PREFIX + selected.descriptor_key[0],
                selected.descriptor_key[1],
            ),
        )
        target.mark_consumer(_monthly.key(scope(case)), unit.head)


def test_declared_missing_descriptor_fails_as_corrupt_then_explicit_retry_rebuilds(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    drain(case)
    selected, _ = descriptor(case)
    remove_descriptor(case, selected)
    assert case.kernel.reading.run_pending(case.company) == ()
    assert page(case).state == "failed" and page(case).reason == "corrupt"
    _monthly.retry(case.kernel.reading, scope(case), ("financial",))
    drain(case)
    assert page(case).state == "ready"


def test_old_missing_descriptor_failure_cannot_fail_a_newly_repaired_descriptor(
    financial_case, monkeypatch
):
    case, owner = financial_case
    case.seed()
    page(case)
    drain(case)
    selected, raw = descriptor(case)
    remove_descriptor(case, selected)

    def repaired(request, read, base):
        with case.kernel.storage.maintenance_unit(case.company) as unit:
            target = projection.store(unit)
            collection, identity = request.descriptor_key
            target._session.execute(
                "INSERT INTO read_item(scope_key,collection_key,item_id,sort_key,component_key,"
                "head,"
                "payload_json) VALUES(?,?,?,?,'projection',?,?)",
                (
                    projection.key(unit),
                    OWNER_PREFIX + collection,
                    identity,
                    identity,
                    unit.head,
                    raw,
                ),
            )
        projection.corrupt()

    monkeypatch.setattr(case.kernel.ledger, "read_financial_projection", repaired)
    assert case.kernel.reading.run_pending(case.company) == ()
    assert page(case).state == "preparing"
    monkeypatch.setattr(case.kernel.ledger, "read_financial_projection", owner.capture)
    drain(case)
    assert page(case).state == "ready"


def test_control_repair_that_drops_a_registration_requeues_its_uncovered_consumer(financial_case):
    case, _ = financial_case
    case.seed()
    page(case)
    drain(case)
    page(case, "2026-02")
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        target = projection.store(unit)
        target._session.execute(
            "UPDATE read_item SET payload_json='{}' WHERE scope_key=? AND collection_key=?",
            (projection.key(unit), CONTROL),
        )
        selected = projection.request(
            case.kernel.reading, scope(case, "2026-02"), "summary_values", unit
        )
        projection.queue(case.kernel.reading, selected, unit, retry=True, rebuild=True)
    assert projection.produce(case.kernel.reading, case.company)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        projection.store(unit).mark_consumer(_monthly.key(scope(case)), unit.head)
    case.kernel.reading.run_pending(case.company)
    with case.kernel.storage.read_unit(case.company) as read:
        selected = projection.request(case.kernel.reading, scope(case), "summary_values", read)
        assert selected.key in projection.store(read).work()["pending"]
    drain(case)
    assert page(case).state == "ready"


def bad_targets(case):
    return (
        "not JSON",
        "[]",
        "null",
        "{}",
        '{"page":"monthly"}',
        '{"page":"unknown","scope":{}}',
        canonical_json({"page": "monthly", "scope": scope(case) | {"company_id": str(uuid4())}}),
        canonical_json({"page": "monthly", "scope": scope(case) | {"period_to": "bad"}}),
        canonical_json({"model": "ledger_financial_projection"}),
    )


def insert_bad_targets(case, unit):
    rows = bad_targets(case)
    unit.repository("reading")._session.executemany(
        "INSERT INTO read_scope_head(scope_key,component_key,wanted_head,format_version,state) "
        "VALUES(?,'funds',?,7,'preparing')",
        ((key, unit.head) for key in rows),
    )
    return rows


def test_bad_scope_header_and_sparse_inputs_are_isolated_during_a_real_apply(financial_case):
    case, _ = financial_case
    case.seed()
    reader = case.kernel.reading
    reader.page("brief", MonthScope(case.company, "2026-01"), {})
    reader.page("funds", MonthScope(case.company, "2026-01"), {})
    drain(case)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        bad = insert_bad_targets(case, unit)
        repository = unit.repository("reading")
        repository._session.execute(
            "UPDATE read_component SET payload_json='{}' "
            "WHERE component_key IN ('projection','brief_open_items','funds')"
        )
    fact = case.expense("40000")
    actual = case.app.accounting_apply(case.request(fact))
    assert actual["completion"] == "complete", actual
    assert case.get(fact["business_id"])["businesses"][0]["fact"] == fact
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        assert all(repository.state(key, "funds")["error_code"] == "storage_corrupt" for key in bad)
    assert page(case).state == "failed" and page(case).reason == "corrupt"
    assert reader.page("funds", MonthScope(case.company, "2026-01"), {}).state == "failed"
    assert reader.page("brief", MonthScope(case.company, "2026-01"), {}).state == "failed"


def test_bad_target_does_not_abort_the_worker_before_independent_work(financial_case):
    case, _ = financial_case
    case.seed()
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        bad = insert_bad_targets(case, unit)
    reader = case.kernel.reading
    reader.page("funds", MonthScope(case.company, "2026-01"), {})
    assert reader.run_pending(case.company)
    assert reader.page("funds", MonthScope(case.company, "2026-01"), {}).state == "ready"
    with case.kernel.storage.read_unit(case.company) as read:
        assert all(
            read.repository("reading").state(key, "funds")["state"] == "failed" for key in bad
        )
