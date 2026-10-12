"""Workforce membership follows dated relationships, independently of money."""

import pytest
from stage9_employee_profiles import accounting_source_fingerprint
from test_dashboard_model_integrity import raw_read
from test_payroll_corrections import company as company  # noqa: F401

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.maintenance import Maintenance


def worker(engine, name, action, effective_date, **facts):
    return Entities(engine).register_worker(
        {"display_name": name, "action": action, "effective_date": effective_date, **facts},
        source="明确的合成用工时间线",
        request_id="start-" + name,
    )


def change(engine, saved, action, effective_date, key, **facts):
    return Entities(engine).change_workforce_relationship(
        saved["entity_id"],
        {"action": action, "effective_date": effective_date, **facts},
        source="明确的合成用工变更",
        expected_revision=saved["revision"],
        request_id=key,
    )


def ids(response, section):
    key = "employee_id" if section == "employees" else "person_id"
    return {item[key] for item in response["data"]["collections"][section]["items"]}


def sources(engine):
    with engine.store.connection(read_only=True) as connection:
        return accounting_source_fingerprint(connection)


@pytest.mark.parametrize("closed", [False, True])
def test_february_roster_excludes_june_and_july_hires_and_preserves_labor(company, closed):
    from test_labor_people_dashboard import ensure_labor_opening

    ensure_labor_opening(company)
    company.publish("january", "february")
    if closed:
        company.close("2026-01")
        company.close("2026-02")
    before = sources(company.engine)
    regular = [worker(company.engine, name, "hire", "2026-02-01") for name in ("杜", "姜", "罗")]
    contractors = [worker(company.engine, name, "start_labor", "2026-02") for name in ("蔡", "杨")]
    for saved in contractors:
        change(company.engine, saved, "to_employee", "2026-06-01", "convert-" + saved["entity_id"])
    july = worker(company.engine, "王", "hire", "2026-07-01")
    dashboard = Dashboard(company.engine)
    february = dashboard.employees("2026-02")
    assert ids(february, "employees") == {item["entity_id"] for item in regular}
    assert ids(february, "labor_workers") == {item["entity_id"] for item in contractors}
    assert "labor_sources" not in february["data"]["collections"]
    assert all(
        not item["has_labor_activity"]
        for item in february["data"]["collections"]["labor_workers"]["items"]
    )
    assert all(
        not item["has_payroll_activity"]
        for item in february["data"]["collections"]["employees"]["items"]
    )
    june = dashboard.employees("2026-06")
    assert ids(june, "employees") == {item["entity_id"] for item in regular + contractors}
    assert ids(june, "labor_workers") == set()
    july_page = dashboard.employees("2026-07")
    assert ids(july_page, "employees") == {
        item["entity_id"] for item in regular + contractors + [july]
    }
    assert sources(company.engine) == before
    with raw_read():
        native = dashboard.employees("2026-02")
    assert native == february
    assert Maintenance(company.engine).verify_integrity()["status"] == "verified"


def test_monthly_active_filter_keeps_departure_month_and_skips_initial_unpaid_leave(company):
    active = worker(company.engine, "月内离职", "hire", "2026-02-01")
    change(company.engine, active, "depart", "2026-02-15", "mid-month-depart")
    paused = worker(company.engine, "入职即停薪", "hire", "2026-02", initial_unpaid_leave=True)
    dashboard = Dashboard(company.engine)
    page = dashboard.employees("2026-02")
    assert active["entity_id"] in ids(page, "employees")
    assert paused["entity_id"] not in ids(page, "employees")
    record = next(
        item
        for item in page["data"]["collections"]["employees"]["items"]
        if item["employee_id"] == active["entity_id"]
    )
    assert record["employment_state"] == "departed"
    assert record["period_states"] == ["regular", "departed"]
    paused_page = dashboard.employees("2026-02", employee_filter="employment_unpaid_leave")
    assert ids(paused_page, "employees") == {paused["entity_id"]}


def test_same_month_conversion_has_two_rosters_with_distinct_nonoverlapping_segments(company):
    saved = worker(company.engine, "月内转员工", "start_labor", "2026-02-01")
    change(company.engine, saved, "to_employee", "2026-02-15", "mid-month-convert")
    response = Dashboard(company.engine).employees("2026-02")
    assert ids(response, "employees") == ids(response, "labor_workers") == {saved["entity_id"]}
    employee = response["data"]["collections"]["employees"]["items"][0]
    assert employee["relationship_segments"] == [
        {"state": "labor", "start": "2026-02-01", "end": "2026-02-15"},
        {"state": "regular", "start": "2026-02-15", "end": None},
    ]
    assert employee["period_states"] == ["labor", "regular"]


def test_labor_people_cursor_rejects_filter_and_relationship_changes(company):
    members = [
        worker(company.engine, name, "start_labor", "2026-02-01") for name in ("阿", "波", "蔡")
    ]
    dashboard = Dashboard(company.engine)
    first = dashboard.employees("2026-02", section="labor_workers", limit=1)
    cursor = first["data"]["collections"]["labor_workers"]["page"]["next_cursor"]
    with pytest.raises(KernelError) as changed_filter:
        dashboard.employees(
            "2026-02", section="labor_workers", limit=1, cursor=cursor,
            employee_filter="all", expected_version=first["snapshot_version"],
        )
    assert changed_filter.value.code == "dashboard_snapshot_changed"
    following = dashboard.employees(
        "2026-02", section="labor_workers", limit=1, cursor=cursor,
        expected_version=first["snapshot_version"],
    )
    assert ids(first, "labor_workers").isdisjoint(ids(following, "labor_workers"))
    assert first["data"]["employees"]["labor_worker_count"] == 3
    change(company.engine, members[0], "end_labor", "2026-02-20", "labor-ended")
    with pytest.raises(KernelError) as failure:
        dashboard.employees(
            "2026-02",
            section="labor_workers",
            limit=1,
            cursor=cursor,
            employee_filter="employment_unpaid_leave",
            expected_version=first["snapshot_version"],
        )
    assert failure.value.code == "dashboard_snapshot_changed"


def test_known_month_boundary_is_visible_as_unconfirmed_and_never_fabricates_day(company):
    saved = worker(company.engine, "按月转换", "start_labor", "2026-01")
    change(company.engine, saved, "to_employee", "2026-02", "month-convert")
    dashboard = Dashboard(company.engine)
    assert saved["entity_id"] not in ids(dashboard.employees("2026-02"), "labor_workers")
    result = dashboard.employees("2026-02", employee_filter="employment_unknown")
    labor = next(
        item
        for item in result["data"]["collections"]["labor_workers"]["items"]
        if item["person_id"] == saved["entity_id"]
    )
    assert labor["in_period"] is None
    assert labor["possible_period_states"] == ["labor"]
    assert labor["relationship_segments"] == [
        {"state": "labor", "start": "2026-01", "end": "2026-02"},
    ]
    assert saved["entity_id"] in ids(dashboard.employees("2026-02"), "employees")
