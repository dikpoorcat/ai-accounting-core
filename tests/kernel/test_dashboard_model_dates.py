"""Calendar changes adapt certified personnel, without publishing new books."""

import pytest
from entity_fixture import save_entity_display_profile
from pydantic import ValidationError
from test_banking import book as book
from test_banking import opening
from test_dashboard_model_transactions import comparable, raw_read, state

from ai_accounting.kernel import business_queries
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_dates import (
    EmployeeDateBasis,
    ModelDateBasis,
    current_employment_state,
    date_verification_days,
    employee_states,
    make_date_basis,
    original_date_states_valid,
)
from ai_accounting.kernel.dashboard_model_runtime import model_scope
from ai_accounting.kernel.dashboard_read_model import load_model
from ai_accounting.kernel.maintenance import Maintenance


def basis(employee="person", **fields):
    return EmployeeDateBasis(
        employee_id=employee,
        explicit_state=fields.get("explicit_state"),
        employment_start=fields.get("employment_start"),
        employment_end=fields.get("employment_end"),
        employment_status=fields.get("employment_status"),
    )


@pytest.mark.parametrize(
    "source,day,expected",
    [
        ({"employment_status": "active", "employment_end": "2026-02-15"}, "2026-02-14", "regular"),
        ({"employment_status": "active", "employment_end": "2026-02-15"}, "2026-02-15", "unknown"),
        (
            {"employment_status": "inactive", "employment_end": "2026-02-15"},
            "2026-02-14",
            "unknown",
        ),
        (
            {"employment_status": "inactive", "employment_end": "2026-02-15"},
            "2026-02-15",
            "departed",
        ),
        ({"employment_status": "active", "employment_end": "2026-02"}, "2026-02-28", "regular"),
        ({"employment_status": "active", "employment_end": "2026-02"}, "2026-03-01", "unknown"),
        ({"employment_status": "inactive", "employment_end": "2026-02"}, "2026-03-01", "departed"),
        ({"explicit_state": "unknown", "employment_status": "active"}, "2026-02-01", "unknown"),
        ({"explicit_state": "regular", "employment_end": "2026-02-01"}, "2026-03-01", "regular"),
        ({"explicit_state": "unpaid_leave"}, "2026-03-01", "unpaid_leave"),
        (
            {
                "employment_status": "active",
                "employment_start": "2026-03",
                "employment_end": "2026-02",
            },
            "2026-03-01",
            "unknown",
        ),
        (
            {
                "employment_status": "active",
                "employment_start": "2026-02-16",
                "employment_end": "2026-02-15",
            },
            "2026-02-14",
            "unknown",
        ),
        (
            {
                "employment_status": "active",
                "employment_start": "2026-02-16",
                "employment_end": "2026-02",
            },
            "2026-02-14",
            "regular",
        ),
        ({"employment_status": "active"}, "2026-03-01", "regular"),
        ({}, "2026-03-01", "unknown"),
    ],
)
def test_current_personnel_rule_preserves_precision_and_explicit_unknown(source, day, expected):
    saved = basis(**source)
    profile = {**source, "employment_state": source.get("explicit_state")}
    assert current_employment_state(saved, day) == expected
    assert current_employment_state(profile, day) == expected


def test_calendar_certificate_has_only_actual_state_change_boundaries():
    saved = make_date_basis(
        (
            basis("explicit", explicit_state="regular", employment_end="2026-02-01"),
            basis("conflict", employment_start="2026-03", employment_end="2026-02"),
            basis("day", employment_status="active", employment_end="2026-02-15"),
            basis("month", employment_end="2026-02"),
            basis("forever", employment_status="active", employment_end="9999-12"),
        ),
        "2026-02-01",
    )
    assert saved.next_change == "2026-02-15"
    assert date_verification_days(saved) == (
        "2026-02-01",
        "2026-02-14",
        "2026-02-15",
        "2026-02-28",
        "2026-03-01",
    )
    assert employee_states(saved, "2026-02-15")["day"] == "unknown"
    assert employee_states(saved, "2026-03-01")["month"] == "departed"
    assert employee_states(saved, "2026-03-01")["explicit"] == "regular"
    assert original_date_states_valid(saved, "2026-02-01")
    assert original_date_states_valid(saved, "2026-02-14")
    assert not original_date_states_valid(saved, "2026-02-15")
    assert not original_date_states_valid(saved, "2026-01-31")


def test_complete_calendar_inputs_reject_missing_or_false_interval_claims():
    saved = make_date_basis((basis(employment_end="2026-02"),), "2026-02-01")
    value = saved.model_dump(mode="python")
    for changed in (
        {"next_change": None},
        {"next_change": "2026-02-28"},
        {"employees": saved.employees * 2},
    ):
        with pytest.raises(ValidationError):
            ModelDateBasis.model_validate({**value, **changed})
    with pytest.raises(ValidationError):
        basis(employment_end="2026-02-30")


def test_all_five_pages_cross_day_without_writes_or_native_fallback(book, monkeypatch):
    engine, save, publish, _ = book
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-10")
    opening(save, publish)
    Maintenance(engine).rebuild_dashboard_read_model("2026-09", request_id="date-model")
    dashboard = Dashboard(engine)
    expected = {}
    with raw_read():
        for page in ("brief", "funds", "employees", "assets"):
            expected[page] = comparable(getattr(dashboard, page)("2026-09"))
        expected["quarterly_report"] = dashboard.quarterly_report(2026, 3)
    before = state(engine)
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-11")
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
        result = comparable(getattr(dashboard, page)("2026-09"))
        assert result["read_context"]["as_of"] == "2026-10-11"
        result["read_context"] = expected[page]["read_context"]
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
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        scope = model_scope(engine, connection, "2026-09")
        assert load_model(connection, scope) is not None
        changed = scope.model_copy(
            update={
                "as_of": "2099-01-01",
                "epochs": scope.epochs.model_copy(
                    update={"management": scope.epochs.management + 1}
                ),
            }
        )
        assert load_model(connection, changed) is None


def test_real_day_and_month_transitions_keep_bounded_filters_and_complete_amounts(
    book, monkeypatch
):
    engine, save, publish, _ = book
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-10")
    opening(save, publish)
    for employee_id, fields in (
        ("day-person", {"employment_status": "active", "employment_end": "2026-10-11"}),
        ("month-person", {"employment_status": "inactive", "employment_end": "2026-10"}),
        ("explicit-person", {"employment_state": "regular", "employment_end": "2026-10-11"}),
        ("unknown-person", {"employment_state": "unknown", "employment_status": "active"}),
    ):
        save_entity_display_profile(
            engine,
            {
                "kind": "employee",
                "entity_id": employee_id,
                "display_name": employee_id,
                "employment_start": "2026-01",
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
        with raw_read():
            expected = {
                selection: dashboard.employees("2026-09", employee_filter=selection)
                for selection in filters
            }

        def require_bounded(*args, **kwargs):
            result = original(*args, **kwargs)
            assert result is not None
            return result

        with monkeypatch.context() as reads:
            reads.setattr(dashboard_model_reads, "read_page", require_bounded)
            for selection in filters:
                actual = dashboard.employees("2026-09", employee_filter=selection)
                assert actual == expected[selection]
            all_people = dashboard.employees("2026-09", employee_filter="all")["data"]
        states = {
            item["employee_id"]: item["employment_state"]
            for item in all_people["collections"]["employees"]["items"]
        }
        assert states["day-person"] == "unknown"
        assert states["month-person"] == ("unknown" if day == "2026-10-11" else "departed")
        assert states["explicit-person"] == "regular"
        assert states["unknown-person"] == "unknown"
    assert state(engine) == before


def test_complete_verification_compares_original_day_and_real_calendar_boundaries(
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
            "employment_start": "2026-01",
            "employment_end": "2026-10-11",
            "employment_status": "active",
            "source": "synthetic personnel confirmation",
        },
        expected_revision=0,
        request_id="profile",
    )
    before = state(engine)
    monkeypatch.setattr(business_queries, "_today_china", lambda: "2026-10-12")
    result = Maintenance(engine).verify_integrity()
    assert result["coverage"]["dashboard_read_models"]["current_compared"] > 0
    assert state(engine) == before
