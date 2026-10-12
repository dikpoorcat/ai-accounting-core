"""Native date boundaries; no model scope relaxation is implemented here."""

from entity_fixture import save_entity_display_profile
from monthly_close_fixture import close_months, ready
from test_banking import book as book  # noqa: F401
from test_banking import opening
from test_dashboard_model_transactions import raw_read, state

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.workflow import Workflow


def business(value):
    if isinstance(value, list):
        return [business(item) for item in value]
    if not isinstance(value, dict):
        return value
    metadata = {"read_context", "snapshot_version", "generated_at", "checked_at", "as_of",
                "next_cursor", "read_version"}
    return {key: business(item) for key, item in value.items() if key not in metadata}


def pages(engine, monkeypatch, day, *, period="2026-09", quarter=3):
    monkeypatch.setattr("ai_accounting.kernel.business_queries._today_china", lambda: day)
    dashboard = Dashboard(engine)
    with raw_read():
        return {"brief": business(dashboard.brief(period)),
                "funds": business(dashboard.funds(period)),
                "employees": business(dashboard.employees(period)),
                "assets": business(dashboard.assets(period)),
                "quarterly": business(dashboard.quarterly_report(2026, quarter))}


def test_five_native_pages_have_no_date_business_diff_without_date_sensitive_people(
    book, monkeypatch,
):
    engine, save, publish, proof = book
    ready(engine, proof, first="2026-01", last="2026-03")
    before = state(engine)
    opened = pages(engine, monkeypatch, "2026-10-10", period="2026-03", quarter=1)
    assert opened["quarterly"]["close_state"] == "open"
    assert opened == pages(engine, monkeypatch, "2026-11-01", period="2026-03", quarter=1)
    assert state(engine) == before
    close_months(Periods(engine), proof, first="2026-01", last="2026-03")
    before = state(engine)
    closed = pages(engine, monkeypatch, "2026-10-10", period="2026-03", quarter=1)
    assert closed["brief"]["data"]["month_state"] == "closed"
    assert closed["quarterly"]["close_state"] == "closed"
    assert closed == pages(engine, monkeypatch, "2026-11-01", period="2026-03", quarter=1)
    assert state(engine) == before


def test_hidden_employee_day_and_month_events_use_selected_month_states(book, monkeypatch):
    engine, save, publish, _ = book
    for index in range(22):
        save_entity_display_profile(engine, {
            "kind": "employee", "entity_id": f"shown-{index:02}",
            "display_name": f"a-person-{index:02}",
            "workforce_event": {"action": "hire", "effective_date": "2026-01-01"},
            "source": "synthetic explicit hire",
        }, expected_revision=0, request_id=f"shown-{index}")
    for ident, action, effective in (("hidden-day", "depart", "2026-08-15"),
                                   ("hidden-month", "depart", "2026-08"),
                                   ("hidden-active", "depart", "2026-10-01"),
                                   ("explicit", "unpaid_leave", "2026-08-01")):
        data = {"kind": "employee", "entity_id": ident,
                "display_name": "z-person-" + ident,
                "workforce_events": [
                    {"action": "hire", "effective_date": "2026-01-01"},
                    {"action": action, "effective_date": effective},
                ],
                "source": "synthetic explicit dated relationship"}
        save_entity_display_profile(engine, data, expected_revision=0, request_id=ident)
    opening(save, publish)
    before = state(engine)
    observed = {}
    for day in ("2026-10-10", "2026-10-11", "2026-10-31", "2026-11-01"):
        observed[day] = pages(engine, monkeypatch, day)
        with raw_read():
            response = Dashboard(engine).employees("2026-09", employee_filter="all", limit=100)
        selected = {item["employee_id"]: item["employment_state"]
                    for item in response["data"]["collections"]["employees"]["items"]}
        assert selected["hidden-day"] == "departed"
        assert selected["hidden-month"] == "departed"
        assert selected["hidden-active"] == "regular"
        assert selected["explicit"] == "unpaid_leave"
    first = observed["2026-10-10"]
    last = observed["2026-10-11"]
    assert {key: value for key, value in first.items() if key != "employees"} == {
        key: value for key, value in last.items() if key != "employees"}
    first_people = first["employees"]["data"]["collections"]["employees"]
    last_people = last["employees"]["data"]["collections"]["employees"]
    assert first_people["items"] == last_people["items"]
    assert all(item["employee_id"] != "hidden-active" for item in first_people["items"])
    assert first_people["page"]["filtered_count"] == 23
    assert last_people["page"]["filtered_count"] == 23
    assert observed["2026-10-11"] == observed["2026-11-01"]
    assert state(engine) == before


def test_deadline_changes_external_status_but_not_current_five_owner_pages(book, monkeypatch):
    engine, save, publish, _ = book
    opening(save, publish)
    save("external_obligation", "filing", {
        "period": "2026-09", "obligation_kind": "individual_income_tax",
        "start_period": "2026-09", "end_period": "2026-09",
        "due_date": "2026-10-11", "applicability_confirmed": True,
    })
    before = state(engine)
    earlier = pages(engine, monkeypatch, "2026-10-10")
    later = pages(engine, monkeypatch, "2026-10-11")
    assert earlier == later
    assert all(task["deadline"] is None for task in earlier["brief"]["data"]["owner_tasks"])
    service = Workflow(engine)
    statuses = []
    for day in ("2026-10-10", "2026-10-11"):
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            rows = service._external_obligations(connection, "2026-09", day)
            statuses.append(next(item["actual_completion_status"] for item in rows
                                 if item["id"] == "filing"))
    assert statuses == ["pending", "due"]
    assert state(engine) == before
