"""Obsolete derived formats rebuild on demand without changing formal accounting data."""

from contextlib import contextmanager

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.reading import _financial_projection, _monthly
from ai_accounting_modular.reading._publication_guard import CaptureGuard
from tests.modular.application._working_case import case as case


def scope(case, **fields):
    return {
        "company_id": case.company,
        "period_from": "2026-01",
        "period_to": "2026-01",
        "view": "as_posted",
        **fields,
    }


def delivery(case, component, **fields):
    return _monthly.deliver(
        case.kernel.reading,
        scope(case, **fields),
        (component,),
        lambda snapshot, read: {"component": snapshot.components[component]},
    )


def ready(case, component, **fields):
    assert delivery(case, component, **fields).state == "preparing"
    assert case.kernel.reading.run_pending(case.company)
    result = delivery(case, component, **fields)
    assert result.state == "ready", result
    return result


def state(case, component):
    with case.kernel.storage.read_unit(case.company) as read:
        return read.repository("reading").state(
            _monthly.key(_monthly.normalize(scope(case))), component
        )


def obsolete(case, component, status="ready", *, damaged_payload=False):
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        session = unit.repository("reading")._session
        key = _monthly.key(_monthly.normalize(scope(case)))
        session.execute(
            "UPDATE read_scope_head SET format_version=?,state=?,error_code=? "
            "WHERE scope_key=? AND component_key=?",
            (
                _monthly.version(component) - 1,
                status,
                "storage_corrupt" if status == "failed" else None,
                key,
                component,
            ),
        )
        if damaged_payload:
            session.execute(
                "UPDATE read_component SET payload_json='{}' WHERE scope_key=? AND component_key=?",
                (key, component),
            )


@pytest.mark.parametrize(
    "component",
    [_monthly.BRIEF_ACTIVITY, _monthly.BRIEF_OPEN_ITEMS, _monthly.FUNDS, _monthly.FINANCIAL],
)
@pytest.mark.parametrize("status", ["ready", "failed", "preparing"])
def test_obsolete_requested_component_rebuilds_from_formal_data(case, component, status):
    fact = case.seed()
    original = ready(case, component)
    other = _monthly.EMPLOYEES
    ready(case, other)
    unrelated = state(case, other)
    facts = case.get(fact["business_id"])
    with case.kernel.storage.read_unit(case.company) as read:
        formal_head = read.head
    obsolete(case, component, status, damaged_payload=True)

    pending = delivery(case, component)
    assert pending.state == "preparing" and pending.reason is None
    refreshed = state(case, component)
    assert refreshed["format_version"] == _monthly.version(component)
    assert refreshed["state"] == "preparing" and refreshed["error_code"] is None
    assert case.kernel.reading.run_pending(case.company)
    rebuilt = delivery(case, component)
    assert rebuilt.state == "ready", rebuilt
    assert rebuilt.payload == original.payload
    assert case.get(fact["business_id"]) == facts
    assert state(case, other) == unrelated
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == formal_head


def test_ready_snapshot_does_not_read_or_repair_an_obsolete_payload(case):
    case.seed()
    component = _monthly.BRIEF_ACTIVITY
    ready(case, component)
    obsolete(case, component, damaged_payload=True)
    before = state(case, component)
    with case.kernel.storage.read_unit(case.company) as read:
        assert (
            _monthly.ready_snapshot(
                case.kernel.reading, _monthly.normalize(scope(case)), (component,), read
            )
            is None
        )
    assert state(case, component) == before


def test_old_expected_snapshot_is_rejected_while_the_requested_upgrade_is_queued(case):
    case.seed()
    component = _monthly.BRIEF_ACTIVITY
    previous = ready(case, component)
    obsolete(case, component)
    with pytest.raises(ContractError) as error:
        delivery(case, component, expected_head=previous.version)
    assert error.value.code == "snapshot_changed"
    assert state(case, component)["state"] == "preparing"
    assert case.kernel.reading.run_pending(case.company)
    assert delivery(case, component).state == "ready"


def test_concurrent_current_publication_is_not_reset_by_an_obsolete_read(case, monkeypatch):
    case.seed()
    component = _monthly.BRIEF_ACTIVITY
    previous = ready(case, component)
    obsolete(case, component)
    maintenance = case.kernel.storage.maintenance_unit
    published = []

    @contextmanager
    def intervening(company):
        if not published:
            normalized = _monthly.normalize(scope(case))
            key = _monthly.key(normalized)
            with case.kernel.storage.read_unit(company) as read:
                model = _monthly.capture(case.kernel.reading, component, normalized, read)
            with maintenance(company) as unit:
                repository = unit.repository("reading")
                repository.retry(key, component, format_version=_monthly.version(component))
                assert repository.publish(
                    key,
                    component,
                    unit.head,
                    _monthly.version(component),
                    model,
                    guard=CaptureGuard.capture(repository.state(key, component), unit.head),
                    current_head=unit.head,
                )
                published.append(repository.state(key, component))
        with maintenance(company) as unit:
            yield unit

    monkeypatch.setattr(case.kernel.storage, "maintenance_unit", intervening)
    assert delivery(case, component).state == "preparing"
    assert state(case, component) == published[0]
    assert case.kernel.reading.run_pending(case.company) == ()
    assert delivery(case, component).payload == previous.payload


@pytest.mark.parametrize("producer_corrupt", [False, True])
def test_old_failed_financial_consumer_does_not_rebuild_unrelated_months_unless_producer_is_corrupt(
    case, monkeypatch, producer_corrupt
):
    case.seed()
    changed = case.app.accounting_apply(case.request(case.expense("200", "2026-02")))
    assert changed["completion"] == "complete", changed
    component = _monthly.FINANCIAL
    january = ready(case, component)
    ready(case, component, period_from="2026-02", period_to="2026-02")
    with case.kernel.storage.read_unit(case.company) as read:
        formal_head = read.head
    obsolete(case, component, "failed", damaged_payload=True)
    if producer_corrupt:
        with case.kernel.storage.maintenance_unit(case.company) as unit:
            target = _financial_projection.store(unit)
            previous = target.state()
            assert target.repository.fail(
                target.scope_key,
                "projection",
                previous["wanted_head"],
                "storage_corrupt",
                expected_revision=previous["revision"],
                expected_state=previous["state"],
                expected_format=previous["format_version"],
            )
    owner = case.kernel.ledger.prepare_financial_projection
    prepared = []

    def capture(requests, changes, closures, read, *, base, rebuild=False):
        prepared.append(
            (sorted(item.scope["period_to"] for item in requests), rebuild, base is None)
        )
        return owner(requests, changes, closures, read, base=base, rebuild=rebuild)

    monkeypatch.setattr(case.kernel.ledger, "prepare_financial_projection", capture)
    assert delivery(case, component).state == "preparing"
    assert case.kernel.reading.run_pending(case.company)
    assert prepared == [
        (
            ["2026-01", "2026-02"] if producer_corrupt else ["2026-01"],
            producer_corrupt,
            producer_corrupt,
        )
    ]
    assert delivery(case, component).payload == january.payload
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == formal_head


@pytest.mark.parametrize("future_format", [False, True])
def test_current_corruption_and_unavailable_future_format_still_need_explicit_retry(
    case, future_format
):
    case.seed()
    component = _monthly.BRIEF_ACTIVITY
    ready(case, component)
    key = _monthly.key(_monthly.normalize(scope(case)))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        session = unit.repository("reading")._session
        if future_format:
            session.execute(
                "UPDATE read_scope_head SET format_version=999 "
                "WHERE scope_key=? AND component_key=?",
                (key, component),
            )
        else:
            session.execute(
                "UPDATE read_component SET payload_json='{}' WHERE scope_key=? AND component_key=?",
                (key, component),
            )
    failed = delivery(case, component)
    assert (failed.state, failed.reason) == ("failed", "corrupt")
    assert state(case, component)["state"] == "failed"
    assert delivery(case, component).state == "failed"
    assert case.kernel.reading.run_pending(case.company) == ()
    _monthly.retry(case.kernel.reading, scope(case), (component,))
    assert case.kernel.reading.run_pending(case.company)
    assert delivery(case, component).state == "ready"
