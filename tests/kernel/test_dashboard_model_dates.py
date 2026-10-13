"""Certified selected-month relationships do not change with the reading day."""

import pytest
from entity_fixture import save_entity_display_profile
from pydantic import ValidationError
from test_banking import book as book
from test_banking import opening
from test_dashboard_model_transactions import comparable, raw_read, state

from ai_accounting.kernel import business_queries
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_async import _produce
from ai_accounting.kernel.dashboard_model_dates import (
    EmployeeDateBasis,
    ModelDateBasis,
    date_verification_days,
    employee_states,
    make_date_basis,
    original_date_states_valid,
)
from ai_accounting.kernel.dashboard_model_runtime import authenticated_model
from ai_accounting.kernel.dashboard_read_model import load_model
from ai_accounting.kernel.maintenance import Maintenance


def basis(employee="person", *, states=("unknown",), possible=None, end="unknown"):
    return EmployeeDateBasis(
        employee_id=employee,
        period_states=states,
        possible_period_states=states if possible is None else possible,
        period_end_state=end,
        event_version="1" * 64,
    )


@pytest.mark.parametrize(
    "states,end",
    [
        (("regular",), "regular"),
        (("unpaid_leave",), "unpaid_leave"),
        (("regular", "departed"), "departed"),
        (("labor", "regular"), "regular"),
        (("labor", "labor_ended"), "labor_ended"),
        (("unknown",), "unknown"),
    ],
)
def test_monthly_certificate_uses_confirmed_timeline_states_without_calendar_substitution(
    states, end
):
    saved = make_date_basis((basis(states=states, end=end),), "2026-02-01", "2026-01")
    for day in ("2026-02-01", "2026-02-15", "2026-03-01"):
        assert employee_states(saved, day) == {"person": end}
    assert saved.employees[0].period_states == states


def test_monthly_certificate_has_business_month_and_event_version_without_calendar_boundaries():
    saved = make_date_basis(
        (
            basis("regular", states=("regular",), end="regular"),
            basis("transition", states=("labor", "regular"), end="regular"),
            basis("unknown", states=(), possible=("regular", "departed")),
        ),
        "2026-02-01",
        "2026-01",
    )
    assert saved.rule == "effective-workforce-month/1"
    assert saved.period == "2026-01"
    assert all(item.event_version == "1" * 64 for item in saved.employees)
    assert "next_change" not in saved.model_dump()
    assert date_verification_days(saved) == ("2026-02-01",)
    assert employee_states(saved, "2026-02-15")["unknown"] == "unknown"
    assert employee_states(saved, "2026-03-01")["regular"] == "regular"
    assert original_date_states_valid(saved, "2026-02-01")
    assert original_date_states_valid(saved, "2026-02-14")
    assert original_date_states_valid(saved, "2026-02-15")
    assert not original_date_states_valid(saved, "2026-01-31")


def test_monthly_certificate_rejects_old_current_state_inputs_and_duplicate_people():
    saved = make_date_basis((basis(),), "2026-02-01", "2026-01")
    value = saved.model_dump(mode="python")
    for changed in (
        {"next_change": None},
        {"next_change": "2026-02-28"},
        {"employees": saved.employees * 2},
    ):
        with pytest.raises(ValidationError):
            ModelDateBasis.model_validate({**value, **changed})
    with pytest.raises(ValidationError):
        EmployeeDateBasis(employee_id="person", explicit_state="unknown", employment_end="2026-02")
    with pytest.raises(ValidationError):
        ModelDateBasis.model_validate({**value, "rule": "explicit-current-employment-state/1"})
    with pytest.raises(ValidationError):
        ModelDateBasis.model_validate({key: item for key, item in value.items() if key != "period"})


def test_all_five_pages_cross_day_without_writes_or_native_fallback(book, monkeypatch):
    engine, save, publish, _ = book
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-10")
    opening(save, publish)
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="date-model")
    dashboard = Dashboard(engine)
    expected = {}
    with raw_read():
        for page in ("brief", "funds", "employees", "assets"):
            expected[page] = comparable(getattr(dashboard, page)(
                "2026-09", **({"movement_account_selection": "first"} if page == "funds" else {})
            ))
        expected["quarterly_report"] = dashboard.quarterly_report(2026, 3)
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-11")
    _produce(engine, "2026-09")
    before = state(engine)
    from ai_accounting.kernel import dashboard_model_reads

    observed = []
    original_read = dashboard_model_reads.read_page
    original_quarter = dashboard_model_reads.read_quarterly_report

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        assert result is not None
        observed.append(args[2])
        return result

    def quarter(*args, **kwargs):
        result = original_quarter(*args, **kwargs)
        assert result is not None
        observed.append("quarterly_report")
        return result

    monkeypatch.setattr(dashboard_model_reads, "read_page", read)
    monkeypatch.setattr(dashboard_model_reads, "read_quarterly_report", quarter)
    for page in ("brief", "funds", "employees", "assets"):
        result = comparable(getattr(dashboard, page)(
            "2026-09", **({"movement_account_selection": "first"} if page == "funds" else {})
        ))
        assert result["read_context"]["as_of"] == "2026-10-11"
        result["read_context"] = expected[page]["read_context"]
        assert len(result["snapshot_version"]) == 64
        result["snapshot_version"] = expected[page]["snapshot_version"]
        # A real reading-day cursor is deliberately rebound, never replayed.
        for collection in result["data"]["collections"].values():
            collection["page"]["next_cursor"] = None
        for collection in expected[page]["data"]["collections"].values():
            collection["page"]["next_cursor"] = None
        assert result == expected[page]
    result = dashboard.quarterly_report(2026, 3)
    assert result["read_context"]["as_of"] == "2026-10-11"
    result["read_context"] = expected["quarterly_report"]["read_context"]
    result["checked_at"] = expected["quarterly_report"]["checked_at"]
    assert result == expected["quarterly_report"]
    assert observed == ["brief", "funds", "employees", "assets", "quarterly_report"]
    assert state(engine) == before


def test_business_epoch_staleness_never_acquires_calendar_only_freshness(book, monkeypatch):
    engine, save, publish, _ = book
    opening(save, publish)
    _produce(engine, "2026-09")
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-09") as snapshot:
        model = authenticated_model(dashboard, snapshot)
        assert model is not None
        scope = model.descriptor.scope
        changed = scope.model_copy(
            update={
                "as_of": "2099-01-01",
                "epochs": scope.epochs.model_copy(
                    update={"management": scope.epochs.management + 1}
                ),
            }
        )
        assert load_model(model.connection, changed) is None


def test_real_day_and_month_transitions_keep_bounded_filters_and_complete_amounts(
    book, monkeypatch
):
    engine, save, publish, _ = book
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-10")
    opening(save, publish)
    for employee_id, fields in (
        ("day-person", {"workforce_events": [
            {"action": "hire", "effective_date": "2026-01-01"},
            {"action": "depart", "effective_date": "2026-10-01"},
        ]}),
        ("month-person", {"workforce_events": [
            {"action": "hire", "effective_date": "2026-01-01"},
            {"action": "depart", "effective_date": "2026-09"},
        ]}),
        ("unpaid-person", {"workforce_events": [
            {"action": "hire", "effective_date": "2026-01-01"},
            {"action": "unpaid_leave", "effective_date": "2026-08-01"},
        ]}),
        ("unknown-person", {"note": "No confirmed workforce relationship"}),
    ):
        save_entity_display_profile(
            engine,
            {
                "kind": "employee",
                "entity_id": employee_id,
                "display_name": employee_id,
                "source": "synthetic personnel confirmation",
                **fields,
            },
            expected_revision=0,
            request_id=f"profile-{employee_id}",
        )
    dashboard = Dashboard(engine)
    before = state(engine)
    from ai_accounting.kernel import dashboard_model_reads

    original = dashboard_model_reads.read_page
    filters = (
        "all",
        "in_period",
        "ended",
        "unknown",
        "employment_active",
        "employment_departed",
        "employment_unknown",
        "employment_unpaid_leave",
    )
    for day in ("2026-10-11", "2026-11-01"):
        monkeypatch.setattr(business_queries, "_today_china", lambda day=day: day)
        _produce(engine, "2026-09")
        before = state(engine)
        with raw_read():
            expected = {
                selection: dashboard.employees("2026-09", employee_filter=selection)
                for selection in filters
            }

        def require_bounded(*args, **kwargs):
            result = original(*args, **kwargs)
            selection = kwargs.get("filters", {}).get("employee_filter")
            if selection == "employment_active":
                assert result is not None  # Current default page has a real model hit.
            else:
                assert result is None  # Non-default selections use bounded native readers.
            return result

        with monkeypatch.context() as reads:
            reads.setattr(dashboard_model_reads, "read_page", require_bounded)
            for selection in filters:
                actual = dashboard.employees("2026-09", employee_filter=selection)
                assert len(actual["snapshot_version"]) == 64
                # A produced certificate and a raw view have distinct opaque
                # paging identities; all source context and business fields agree.
                actual["snapshot_version"] = expected[selection]["snapshot_version"]
                assert actual == expected[selection]
            all_people = dashboard.employees("2026-09", employee_filter="all")["data"]
        states = {
            item["employee_id"]: item["employment_state"]
            for item in all_people["collections"]["employees"]["items"]
        }
        assert states["day-person"] == "regular"
        assert states["month-person"] == "departed"
        assert states["unpaid-person"] == "unpaid_leave"
        assert "unknown-person" not in states
        assert state(engine) == before


def test_complete_verification_retains_the_business_month_across_observation_days(
    book, monkeypatch
):
    engine, save, publish, _ = book
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-10")
    opening(save, publish)
    save_entity_display_profile(
        engine,
        {
            "kind": "employee",
            "entity_id": "person",
            "workforce_events": [
                {"action": "hire", "effective_date": "2026-01-01"},
                {"action": "depart", "effective_date": "2026-09-15"},
            ],
            "source": "synthetic personnel confirmation",
        },
        expected_revision=0,
        request_id="profile",
    )
    _produce(engine, "2026-09")
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-12")
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-09") as snapshot:
        assert authenticated_model(dashboard, snapshot) is None  # Old reading day is stale.
    _produce(engine, "2026-09")
    before = state(engine)
    result = Maintenance(engine).verify_integrity()
    assert result["status"] == "verified"
    with dashboard._snapshot("2026-09") as snapshot:
        model = authenticated_model(dashboard, snapshot)
        assert model is not None
        assert model.descriptor.scope.period == "2026-09"
        assert model.descriptor.scope.as_of == "2026-10-12"
        model.read_page_block("employees/default/overview/0")
    assert state(engine) == before
