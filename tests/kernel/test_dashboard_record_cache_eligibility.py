"""Record cache eligibility inspects only values outside JSON1 base scalars."""

from types import SimpleNamespace

import pytest
from test_banking import book as book
from test_banking import close_month, inventories
from test_dashboard_open_groups import expenses, snapshot

from ai_accounting.kernel import settlement_freeze as freeze


def _state(key, **updates):
    value = freeze._empty_state(key)
    value.update(source_kind="expense", component="primary", category="payable",
                 account="2202", source_subject_id=key, source_amount=100,
                 source_event_count=1)
    value.update(updates)
    return freeze._DashboardStateRecord(*(
        value[name] for name in freeze._DashboardStateRecord._fields
    ))


def test_actual_frozen_base_records_do_not_repeat_eighteen_field_scan(
    book, monkeypatch, record_property,
):
    engine, _, _, proof = book
    expenses(book, 40)
    assert close_month(inventories(engine, proof, "2026-09", {"transactions"}), proof, "2026-09")[
        "status"
    ] == "closed"
    original, visited = freeze._DashboardSourceRecord, []

    class ObservedRecord(original):
        def __iter__(self):
            visited.append(self.key)
            return tuple.__iter__(self)

    with snapshot(engine, "2026-09") as snap:
        scope = freeze._scope(snap.connection, snap.period, current=False, reads=snap.reads)
        assert scope.overrides == {}
        with monkeypatch.context() as patch:
            patch.setattr(freeze, "_DashboardSourceRecord", ObservedRecord)
            first = freeze.frozen_dashboard_group_sources(
                snap.connection, snap.period, reads=snap.reads,
            )
        assert len(first["obligations"]) == len(snap.reads._frozen_settlement_scalars) == 40
        assert visited == []
        stored = snap.reads._report_snapshot_cache["dashboard_complete_frozen_records", snap.period]
        assert stored[2]["obligations"] is first["obligations"]
        current = freeze.frozen_dashboard_group_sources(
            snap.connection, snap.period, current=True, reads=snap.reads,
            exact_sources=True, page_keys={row.key for row in first["obligations"]},
        )
        assert current["obligations"] is first["obligations"]
        record_property(
            "actual_authenticated_base_states", len(snap.reads._frozen_settlement_scalars),
        )
        record_property("repeated_record_field_scans", len(visited))


@pytest.mark.parametrize("updates,period,overlay", [
    ({}, (0, 0), False),
    ({}, ([1], 0), False),
    ({}, (0, {"unusual": 1}), False),
    ({"bad_paid": True}, ([1], 0), False),
    ({"bad_other": True}, (0, {"unusual": 1}), False),
    ({"component": ["unusual"]}, (0, 0), True),
    ({"counterparty_id": {"unusual": 1}}, (0, 0), True),
    ({"source_event_count": [1]}, (0, 0), True),
    ({"source_amount": [1], "bad_source": True}, (0, 0), True),
    ({"paid": {"unusual": 1}, "bad_paid": True}, ([1], 0), True),
    ({"source_digest": [1]}, (0, 0), True),
])
def test_eligibility_keeps_old_actual_output_composite_and_unknown_rules(updates, period, overlay):
    key = "expense:subject:primary"
    state = _state(key, **updates)
    scope = SimpleNamespace(period_amounts={key: period}, overrides={key: state} if overlay else {})
    legacy = freeze._dashboard_source_records(None, scope, {key: state})
    actual, eligible = freeze._dashboard_source_records(
        None, scope, {key: state}, _cache_eligibility=True,
    )
    assert actual == legacy
    assert eligible == all(not isinstance(value, (dict, list)) for row in legacy for value in row)


def test_only_overlay_records_are_iterated_for_output_qualification(monkeypatch):
    original, visited = freeze._DashboardSourceRecord, []

    class ObservedRecord(original):
        def __iter__(self):
            visited.append(self.key)
            return tuple.__iter__(self)

    states = {f"expense:subject-{index}:primary": _state(f"expense:subject-{index}:primary")
              for index in range(128)}
    overlay = next(reversed(states))
    scope = SimpleNamespace(overrides={overlay: states[overlay]},
                            period_amounts={next(iter(states)): (11, 13)})
    monkeypatch.setattr(freeze, "_DashboardSourceRecord", ObservedRecord)
    actual, eligible = freeze._dashboard_source_records(
        None, scope, states, _cache_eligibility=True,
    )
    assert len(actual) == 128 and eligible
    assert visited == [overlay]


@pytest.mark.parametrize("updates,period", [
    ({"source_amount": "not-an-amount"}, (0, 0)),
    ({"source_amount": 1 << 63}, (0, 0)),
    ({}, (1,)),
])
def test_eligibility_never_replaces_existing_amount_or_reducer_exceptions(updates, period):
    key = "expense:subject:primary"
    state = _state(key, **updates)
    scope = SimpleNamespace(overrides={key: state}, period_amounts={key: period})
    outcomes = []
    for qualify in (False, True):
        try:
            freeze._dashboard_source_records(None, scope, {key: state}, _cache_eligibility=qualify)
        except Exception as error:
            outcomes.append((type(error), str(error)))
    assert len(outcomes) == 2 and outcomes[0] == outcomes[1]
