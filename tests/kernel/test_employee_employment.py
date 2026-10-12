"""Source-backed workforce timelines, using synthetic companies only."""

import json

import pytest

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.employee_employment import monthly_projection, records, verify_history
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities, employee_entities, workforce_timelines
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


@pytest.fixture
def engine(tmp_path):
    return Engine(Store.create(
        tmp_path / "workforce.sqlite", production_bundle(), "company",
        "911100000000000001", "database",
    ))


def register(engine, *, action="hire", date="2025-01-01", request="hire", **kwargs):
    data = {"display_name": "合成人员", "action": action, "effective_date": date}
    for key in ("initial_unpaid_leave", "after_event_id"):
        if key in kwargs:
            data[key] = kwargs.pop(key)
    return Entities(engine).register_worker(
        data, source="合成用工开始确认", request_id=request, **kwargs,
    )


def change(engine, saved, action, date, *, request=None, **links):
    return Entities(engine).change_workforce_relationship(
        saved["entity_id"], {"action": action, "effective_date": date, **links},
        source="合成用工变更确认", expected_revision=saved["revision"],
        request_id=request or action + "-" + date,
    )


def latest_content(engine, ident):
    with engine.store.connection(read_only=True) as connection:
        return json.loads(connection.execute(
            "SELECT content FROM entity_profile_revision WHERE entity_id=? "
            "ORDER BY revision DESC LIMIT 1", (ident,),
        ).fetchone()[0])


def timeline(engine, ident):
    with engine.store.connection(read_only=True) as connection:
        return workforce_timelines(connection, [ident])[ident]


def database_state(engine):
    with engine.store.connection(read_only=True) as connection:
        return {
            name: [tuple(row) for row in connection.execute("SELECT * FROM " + name)]
            for name in (
                "entity", "entity_profile_revision", "request", "audit", "state",
                "dashboard_read_model_commit", "dashboard_read_model_block",
                "dashboard_read_model_current",
            )
        }


def structure(engine):
    with engine.store.connection(read_only=True) as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )]


def test_registration_and_rename_keep_only_event_facts(engine):
    before = structure(engine)
    saved = register(engine)
    ident = saved["entity_id"]
    assert set(saved) == {"status", "entity_id", "profile_id", "revision"}
    current = latest_content(engine, ident)
    assert not any(key.startswith("employment_") for key in current)
    assert current["workforce_event"]["effective_date"] == "2025-01-01"
    original = timeline(engine, ident)
    assert original["employment_state"] == "regular"
    assert monthly_projection(original, "2025-01")["employee_member"] is True
    Entities(engine).update_entity_profile(
        ident, {"display_name": "更新合成姓名"}, source="合成姓名更正",
        expected_revision=saved["revision"], request_id="rename",
    )
    assert latest_content(engine, ident).get("workforce_event") is None
    after = timeline(engine, ident)
    assert after["segments"] == original["segments"]
    assert after["event_version"] == original["event_version"]
    assert structure(engine) == before


@pytest.mark.parametrize(
    "action,collection", [("hire", "employees"), ("start_labor", "labor_workers")],
)
def test_worker_only_registration_produces_its_zero_money_month_page(engine, action, collection):
    from ai_accounting.kernel.dashboard import Dashboard

    saved = register(engine, action=action)
    dashboard = Dashboard(engine)
    response = dashboard.employees("2025-01")
    items = response["data"]["collections"][collection]["items"]
    identity_field = "employee_id" if action == "hire" else "person_id"
    assert [item[identity_field] for item in items] == [saved["entity_id"]]
    assert response["data"]["employees"]["payroll_count"] == 0
    with engine.store.connection(read_only=True) as connection:
        assert [item["key"] for item in dashboard._periods(connection)] == ["2025-01"]
        assert connection.execute("SELECT count(*) FROM fact_revision").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM voucher_version").fetchone()[0] == 0


def test_cross_month_correction_keeps_old_month_readable_and_refreshes_both_models(engine):
    from ai_accounting.kernel.dashboard import Dashboard

    hired = register(engine)
    changed = change(
        engine, hired, "hire", "2025-02-01", replaces_event_id=hired["profile_id"],
        request="correct-hire-month",
    )
    dashboard = Dashboard(engine)
    january = dashboard.employees("2025-01")["data"]
    february = dashboard.employees("2025-02")["data"]
    assert january["collections"]["employees"]["items"] == []
    assert [item["employee_id"] for item in february["collections"]["employees"]["items"]] == [
        changed["entity_id"]
    ]
    with engine.store.connection(read_only=True) as connection:
        assert {item["key"] for item in dashboard._periods(connection)} == {"2025-01", "2025-02"}


def test_existing_person_is_reused_without_name_matching(engine):
    directory = Entities(engine)
    person = directory.register_entity(
        "person", {"display_name": "原合成姓名", "note": "合成备注"},
        source="合成人员资料", request_id="person",
    )
    saved = register(engine, entity_id=person["entity_id"], expected_revision=person["revision"])
    assert saved["entity_id"] == person["entity_id"]
    assert latest_content(engine, saved["entity_id"])["note"] == "合成备注"
    other = register(engine, request="same-name-another-person")
    assert other["entity_id"] != saved["entity_id"]


@pytest.mark.parametrize(
    "action,collection,identity_field,state", [
        ("hire", "employees", "employee_id", "regular"),
        ("start_labor", "labor_workers", "person_id", "labor"),
    ],
)
def test_expense_identity_resolution_cannot_end_the_sources_workforce_relationship(
    engine, action, collection, identity_field, state,
):
    from test_identity_corrections import confirm, expense

    from ai_accounting.kernel.dashboard import Dashboard

    first = register(engine, action=action, date="2026-01-01")
    second = Entities(engine).register_entity(
        "person", {"display_name": "合成费用实际对象"}, source="合成费用对象资料",
        request_id="expense-actual-person",
    )
    proof = engine.register_evidence(
        b"Synthetic confirmation: expense recipient was B rather than A; workforce unchanged.",
        "text/plain", "合成费用对象更正确认", request_id="expense-person-proof",
    )["digest"]
    data = expense(engine, proof, first["entity_id"])
    before_history = Entities(engine).workforce_relationship_history(first["entity_id"])
    before_accounts = engine.overview("2026-01")["accounts"]
    confirm(engine, dict(
        changes=[{
            "subject_id": "expense", "expected_revision": 1, "action": "reassign",
            "data": {**data, "counterparty_id": second["entity_id"]},
        }], evidence=[proof], reason="合成费用对象误填，更正为实际对象，不改变用工关系",
        entity_resolution={
            "source_entity_id": first["entity_id"], "target_entity_id": second["entity_id"],
        },
    ))
    actual = Dashboard(engine).employees("2026-01")["data"]["collections"][collection]["items"]
    assert [item[identity_field] for item in actual] == [first["entity_id"]]
    assert actual[0]["employment_state"] == state
    assert Entities(engine).workforce_relationship_history(first["entity_id"]) == before_history
    assert engine.overview("2026-01")["accounts"] == before_accounts
    with engine.store.connection(read_only=True) as connection:
        corrected = engine.store.current_fact(connection, "expense").fact
        assert corrected.counterparty_id == second["entity_id"]
        assert corrected.amount_fen == data["amount_fen"]
        assert connection.execute("SELECT count(*) FROM entity_resolution").fetchone()[0] == 1
        employees = employee_entities(connection, "2026-01", registry=engine.store.registry)
        assert (first["entity_id"] in employees) is (action == "hire")
        assert second["entity_id"] not in employees
    found = Entities(engine).find_entities(query=first["entity_id"], kind="person")["items"]
    assert found[0]["employment_state"] == state


def test_registration_reuses_labor_person_and_ends_labor_at_hire(engine):
    labor = register(engine, action="start_labor", date="2026-02-01", request="labor-person")
    hired = register(
        engine, date="2026-06-01", request="labor-person-hire",
        entity_id=labor["entity_id"], expected_revision=labor["revision"],
    )
    assert hired["entity_id"] == labor["entity_id"]
    derived = timeline(engine, hired["entity_id"])
    assert monthly_projection(derived, "2026-02")["period_states"] == ["labor"]
    assert monthly_projection(derived, "2026-06")["period_states"] == ["regular"]
    assert derived["segments"][0]["end"] == "2026-06-01"


def test_historical_labor_before_existing_hire_converges_atomically(engine):
    from ai_accounting.kernel.dashboard import Dashboard

    hired = register(engine, date="2026-06-01")
    backfill = change(engine, hired, "start_labor", "2026-02-01", request="historical-labor")
    ident = backfill["entity_id"]
    february = Dashboard(engine).employees("2026-02")["data"]["collections"]
    june = Dashboard(engine).employees("2026-06")["data"]["collections"]
    assert february["employees"]["items"] == []
    assert [item["person_id"] for item in february["labor_workers"]["items"]] == [ident]
    assert [item["employee_id"] for item in june["employees"]["items"]] == [ident]
    assert june["labor_workers"]["items"] == []
    history = Entities(engine).workforce_relationship_history(ident)
    assert history["page"]["total_count"] == 2
    assert all(item["active"] and item["recorded_at"] for item in history["items"])


def test_backfilled_relationship_keeps_frozen_payroll_and_financial_sources_unchanged(tmp_path):
    from test_settlement_late_reviews import prepared

    company = prepared(tmp_path)
    directory = Entities(company.engine)

    def authoritative_financial_state():
        with company.engine.store.connection(read_only=True) as connection:
            return {
                name: [tuple(row) for row in connection.execute("SELECT * FROM " + name)]
                for name, in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
                if name.startswith("fact_") or name in {
                    "calculation", "calculation_publication", "calculation_current",
                    "voucher", "voucher_version", "voucher_current", "voucher_line",
                    "period_close", "close_reference",
                }
            }

    before = authoritative_financial_state()
    hired = directory.register_worker(
        {"action": "hire", "effective_date": "2026-01-01"}, entity_id="employee",
        expected_revision=1, source="合成确认2026年1月1日入职", request_id="closed-hire",
    )
    directory.change_workforce_relationship(
        "employee", {"action": "start_labor", "effective_date": "2025-12-01"},
        expected_revision=hired["revision"], source="合成补录2025年12月1日劳务关系开始",
        request_id="closed-prior-labor",
    )
    assert authoritative_financial_state() == before
    assert before["calculation"] and before["voucher_version"] and before["period_close"]


def test_all_relationship_transitions_form_one_timeline(engine):
    saved = register(engine, action="start_labor")
    ident = saved["entity_id"]
    for action, date, state in (
        ("to_employee", "2025-02-01", "regular"),
        ("unpaid_leave", "2025-03-01", "unpaid_leave"),
        ("resume", "2025-04-01", "regular"),
        ("to_labor", "2025-05-01", "labor"),
        ("end_labor", "2025-06-01", "labor_ended"),
        ("start_labor", "2025-07-01", "labor"),
        ("to_employee", "2025-08-01", "regular"),
        ("depart", "2025-09-01", "departed"),
        ("hire", "2025-10-01", "regular"),
    ):
        saved = change(engine, saved, action, date)
        assert timeline(engine, ident)["employment_state"] == state
    segments = timeline(engine, ident)["segments"]
    assert len(segments) == 10
    assert all(a["end"] == b["start"] for a, b in zip(segments, segments[1:], strict=False))
    assert len(Entities(engine).workforce_relationship_history(ident)["items"]) == 10


@pytest.mark.parametrize("action", ["hire", "to_employee"])
def test_immediate_unpaid_leave_has_no_regular_segment(engine, action):
    if action == "hire":
        saved = register(engine, date="2025-01", initial_unpaid_leave=True)
    else:
        saved = register(engine, action="start_labor", date="2024-12")
        saved = change(engine, saved, action, "2025-01", initial_unpaid_leave=True)
    derived = timeline(engine, saved["entity_id"])
    assert derived["employment_state"] == "unpaid_leave"
    assert all(segment["state"] != "regular" for segment in derived["segments"])
    month = monthly_projection(derived, "2025-01")
    assert "unpaid_leave" in month["period_states"]
    assert "regular" not in month["period_states"]


def test_history_and_month_precision_are_not_synthetic_days(engine):
    saved = register(engine, date="2025-01")
    current = latest_content(engine, saved["entity_id"])
    assert current["workforce_event"]["effective_date"] == "2025-01"
    assert timeline(engine, saved["entity_id"])["segments"][0]["start"] == "2025-01"
    assert monthly_projection(timeline(engine, saved["entity_id"]), "2025-01")[
        "employee_member"
    ] is True


@pytest.mark.parametrize("end,expected", [("2025-02-01", False), ("2025-02-02", True)])
def test_departure_is_exclusive_and_active_means_any_confirmed_day(engine, end, expected):
    saved = register(engine)
    change(engine, saved, "depart", end)
    february = monthly_projection(timeline(engine, saved["entity_id"]), "2025-02")
    assert february["employee_member"] is expected
    assert ("regular" in february["period_states"]) is expected
    assert february["period_end_state"] == "departed"


def test_same_day_transition_has_no_predecessor_occupancy(engine):
    saved = register(engine)
    change(engine, saved, "unpaid_leave", "2025-01-01", after_event_id=saved["profile_id"])
    month = monthly_projection(timeline(engine, saved["entity_id"]), "2025-01")
    assert month["period_states"] == ["unpaid_leave"]
    assert month["possible_period_states"] == []


def test_same_month_and_mixed_precision_need_source_order(engine):
    saved = register(engine, date="2025-01")
    before = database_state(engine)
    with pytest.raises(NeedsInformation) as failure:
        change(engine, saved, "unpaid_leave", "2025-01-15")
    assert failure.value.issues[0]["field"] == "after_event_id"
    assert database_state(engine) == before
    changed = change(
        engine, saved, "unpaid_leave", "2025-01-15",
        after_event_id=saved["profile_id"], request="ordered-leave",
    )
    month = monthly_projection(timeline(engine, changed["entity_id"]), "2025-01")
    assert "regular" in month["possible_period_states"]
    assert "unpaid_leave" in month["period_states"]
    assert month["fact_issues"][0]["allowed_precision"] == ["month", "day"]


def test_month_only_boundary_reports_unknown_prior_role_in_transition_month(engine):
    saved = register(engine, action="start_labor", date="2025-01")
    change(engine, saved, "to_employee", "2025-02")
    derived = timeline(engine, saved["entity_id"])
    month = monthly_projection(derived, "2025-02")
    assert month["employee_member"] is True
    assert month["labor_member"] is None
    assert month["period_states"] == ["regular"]
    assert month["possible_period_states"] == ["labor"]
    assert month["period_end_state"] == "regular"
    assert month["fact_issues"]
    result = Entities(engine).workforce_relationship_history(saved["entity_id"], period="2025-02")
    assert result["period"] == "2025-02"
    assert result["monthly"] == month
    assert set(result["monthly"]["fact_issues"][0]["reusable_sources"]) == {
        segment["event_id"] for segment in derived["segments"]
    }


def test_midmonth_conversion_has_both_rosters_without_overlapping_intervals(engine):
    saved = register(engine, action="start_labor")
    change(engine, saved, "to_employee", "2025-02-15")
    month = monthly_projection(timeline(engine, saved["entity_id"]), "2025-02")
    assert month["employee_member"] is month["labor_member"] is True
    assert month["period_states"] == ["labor", "regular"]
    assert not month["fact_issues"]


def test_backfill_and_correction_preserve_later_state(engine):
    hired = register(engine)
    departed = change(engine, hired, "depart", "2025-06-30")
    leave = change(engine, departed, "unpaid_leave", "2025-02-01")
    resumed = change(engine, leave, "resume", "2025-03-01")
    corrected = change(
        engine, resumed, "unpaid_leave", "2025-02-02",
        replaces_event_id=leave["profile_id"], request="correct-leave",
    )
    assert timeline(engine, hired["entity_id"])["employment_state"] == "departed"
    items = Entities(engine).workforce_relationship_history(hired["entity_id"])["items"]
    by_id = {item["event_id"]: item for item in items}
    assert not by_id[leave["profile_id"]]["active"]
    assert by_id[corrected["profile_id"]]["active"]
    assert by_id[leave["profile_id"]]["effective_date"] == "2025-02-01"


def test_order_reference_follows_corrected_predecessor(engine):
    hired = register(engine, date="2025-01")
    leave = change(engine, hired, "unpaid_leave", "2025-01", after_event_id=hired["profile_id"])
    resumed = change(engine, leave, "resume", "2025-01", after_event_id=leave["profile_id"])
    change(
        engine, resumed, "unpaid_leave", "2025-01", after_event_id=hired["profile_id"],
        replaces_event_id=leave["profile_id"], request="replace-predecessor",
    )
    assert timeline(engine, hired["entity_id"])["employment_state"] == "regular"


@pytest.mark.parametrize("link", ["after_event_id", "replaces_event_id"])
def test_cross_person_references_roll_back(engine, link):
    first = register(engine)
    other = register(engine, request="other")
    before = database_state(engine)
    with pytest.raises(NeedsInformation):
        change(engine, first, "unpaid_leave", "2025-02", **{link: other["profile_id"]})
    assert database_state(engine) == before


@pytest.mark.parametrize("action", ["start_labor", "hire", "resume", "end_labor", "to_employee"])
def test_invalid_transition_is_atomic(engine, action):
    saved = register(engine)
    before = database_state(engine)
    with pytest.raises(NeedsInformation):
        change(engine, saved, action, "2025-02")
    assert database_state(engine) == before


def test_future_stale_version_and_idempotency(engine):
    empty = database_state(engine)
    with pytest.raises((ValueError, KernelError)):
        register(engine, date="9999-12-01", request="future")
    assert database_state(engine) == empty
    saved = register(engine)
    assert register(engine) == saved
    before = database_state(engine)
    with pytest.raises(NeedsInformation):
        change(engine, saved, "depart", "9999-12-01", request="future-change")
    assert database_state(engine) == before
    with pytest.raises(KernelError):
        change(engine, dict(saved, revision=saved["revision"] + 1), "depart", "2025-06")
    assert database_state(engine) == before
    with pytest.raises(KernelError):
        register(engine, date="2025-02-01")
    assert database_state(engine) == before


@pytest.mark.parametrize("failure_stage", ["published", "commit"])
def test_atomic_failure_after_publication_or_read_model_production(engine, failure_stage):
    before = database_state(engine)

    def fail(stage, connection):
        if stage == failure_stage:
            raise RuntimeError("synthetic interruption")

    engine.fault = fail
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        register(engine)
    assert database_state(engine) == before


@pytest.mark.parametrize("source,evidence", [("", None), ("合成未登记证据", "00" * 32)])
def test_missing_source_and_evidence_roll_back_identity(engine, source, evidence):
    before = database_state(engine)
    with pytest.raises((ValueError, KernelError)):
        Entities(engine).register_worker(
            {"action": "hire", "effective_date": "2025-01"},
            source=source, evidence_digest=evidence, request_id="missing-source",
        )
    assert database_state(engine) == before


@pytest.mark.parametrize("data", [
    {"employment_state": "labor"}, {"employment_start": "2024-01"},
    {"workforce_event": {"action": "hire", "effective_date": "2025-01"}},
])
def test_metadata_cannot_change_relationship(engine, data):
    saved = register(engine)
    before = database_state(engine)
    with pytest.raises(ValueError):
        Entities(engine).update_entity_profile(
            saved["entity_id"], data, source="合成旁路", expected_revision=saved["revision"],
            request_id="bypass",
        )
    assert database_state(engine) == before


def test_history_cursor_follows_events_and_survives_rename(engine):
    hired = register(engine)
    leave = change(engine, hired, "unpaid_leave", "2025-02")
    directory = Entities(engine)
    first = directory.workforce_relationship_history(hired["entity_id"], limit=1)
    assert first["period"] is first["monthly"] is None
    assert first["page"]["total_count"] == 2
    assert first["items"][0]["recorded_at"]
    assert first["items"][0]["source"] == "合成用工变更确认"
    renamed = directory.update_entity_profile(
        hired["entity_id"], {"note": "合成管理备注"}, source="合成资料",
        expected_revision=leave["revision"], request_id="history-rename",
    )
    page = directory.workforce_relationship_history(
        hired["entity_id"], cursor=first["page"]["next_cursor"], limit=1,
    )
    assert page["event_version"] == first["event_version"]
    assert page["items"][0]["event_id"] == hired["profile_id"]
    change(engine, renamed, "resume", "2025-03")
    with pytest.raises(KernelError) as failure:
        directory.workforce_relationship_history(
            hired["entity_id"], cursor=first["page"]["next_cursor"], limit=1,
        )
    assert failure.value.code == "workforce_history_cursor_stale"


def test_history_month_scope_cannot_reuse_a_different_month_cursor(engine):
    hired = register(engine)
    change(engine, hired, "unpaid_leave", "2025-02")
    directory = Entities(engine)
    january = directory.workforce_relationship_history(
        hired["entity_id"], limit=1, period="2025-01",
    )
    assert january["monthly"]["period_states"] == ["regular"]
    with pytest.raises(KernelError) as failure:
        directory.workforce_relationship_history(
            hired["entity_id"], cursor=january["page"]["next_cursor"], period="2025-02",
        )
    assert failure.value.code == "workforce_history_cursor_stale"


def test_missing_early_event_is_not_an_unknown_relationship(engine):
    hired = register(engine)
    Entities(engine).update_entity_profile(
        hired["entity_id"], {"note": "合成备注"}, source="合成资料",
        expected_revision=hired["revision"], request_id="metadata-after-event",
    )
    with engine.store.connection(read_only=True) as connection:
        history = records(connection, hired["entity_id"])
    with pytest.raises(KernelError) as failure:
        verify_history(history[1:])
    assert failure.value.code == "entity_profile_corrupt"


def test_unknown_person_does_not_imply_either_relationship(engine):
    saved = Entities(engine).register_entity(
        "person", {"display_name": "未确认关系"}, source="合成人员资料", request_id="unknown",
    )
    month = monthly_projection(timeline(engine, saved["entity_id"]), "2025-01")
    assert month["employee_member"] is month["labor_member"] is None
    assert month["period_states"] == ["unknown"]


def test_monthly_employee_candidates_exclude_future_hires_and_pure_labor(engine):
    labor = register(engine, action="start_labor", date="2025-02-01", request="labor")
    employee = register(engine, date="2025-02-01", request="employee")
    future_hire = register(engine, date="2025-07-01", request="later-hire")
    converted = change(engine, labor, "to_employee", "2025-06-01")
    with engine.store.connection(read_only=True) as connection:
        february = employee_entities(connection, "2025-02", registry=engine.store.registry)
        july = employee_entities(connection, "2025-07", registry=engine.store.registry)
    assert february == [employee["entity_id"]]
    assert set(july) == {employee["entity_id"], future_hire["entity_id"], converted["entity_id"]}
