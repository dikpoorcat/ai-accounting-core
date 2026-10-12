"""Typed employment events, using isolated synthetic company databases."""

import json

import pytest
from entity_fixture import save_entity_display_profile
from material_fixture import supporting_text

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


@pytest.fixture
def engine(tmp_path):
    return Engine(
        Store.create(
            tmp_path / "employment.sqlite",
            production_bundle(),
            "company",
            "911100000000000001",
            "database",
        )
    )


def hire(engine, *, date="2025-01-01", request="hire", **kwargs):
    return Entities(engine).register_employee(
        {"display_name": "合成员工", "effective_date": date},
        source="合成入职确认",
        request_id=request,
        **kwargs,
    )


def change(engine, employee, revision, action, date, *, request=None, **links):
    return Entities(engine).change_employee_employment(
        employee,
        {"action": action, "effective_date": date, **links},
        source="合成任职事项确认",
        expected_revision=revision,
        request_id=request or action + "-" + date,
    )


def profile(engine, employee):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM entity_profile_revision WHERE entity_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (employee,),
        ).fetchone()
        return dict(row), json.loads(row["content"])


def database_state(engine):
    with engine.store.connection(read_only=True) as connection:
        return {
            name: [tuple(row) for row in connection.execute("SELECT * FROM " + name)]
            for name in ("entity", "entity_profile_revision", "request", "audit", "state")
        }


def structure(engine):
    with engine.store.connection(read_only=True) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
        ]


def establish_dashboard_period(engine, period="2025-01"):
    proof = engine.register_evidence(
        ("Synthetic employment dashboard month " + period).encode(),
        "text/plain",
        "合成月份支持资料",
        request_id="dashboard-month-proof-" + period,
    )["digest"]
    supporting_text(engine, proof, period=period)


def test_registration_without_wages_and_metadata_patch_preserve_employment(engine):
    establish_dashboard_period(engine)
    before = structure(engine)
    saved = hire(engine)
    employee = saved["entity_id"]
    _, current = profile(engine, employee)
    assert current["employment_state"] == "regular"
    assert current["employment_start"] == "2025-01-01"
    result = Dashboard(engine).employees("2025-01", employee_filter="employment_active")
    items = result["data"]["collections"]["employees"]["items"]
    item = next(item for item in items if item["employee_id"] == employee)
    assert item["employment_state"] == "regular"
    assert item["gross_salary_fen"] == 0
    Entities(engine).update_entity_profile(
        employee,
        {"display_name": "更新后的合成姓名"},
        source="合成姓名更正",
        expected_revision=saved["revision"],
        request_id="rename",
    )
    _, updated = profile(engine, employee)
    assert updated["employment_state"] == "regular"
    assert updated["employment_start"] == current["employment_start"]
    assert not updated.get("employment_event")
    assert structure(engine) == before


def test_hire_reuses_person_identity_and_preserves_metadata(engine):
    directory = Entities(engine)
    person = directory.register_entity(
        "person",
        {"display_name": "原合成姓名", "note": "合成备注"},
        source="合成人员资料",
        request_id="person",
    )
    saved = hire(engine, entity_id=person["entity_id"], expected_revision=person["revision"])
    assert saved["entity_id"] == person["entity_id"]
    assert profile(engine, person["entity_id"])[1]["note"] == "合成备注"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM entity WHERE kind='person'").fetchone()[0] == 1
        )


def test_leave_resume_depart_and_rehire_do_not_rewrite_history(engine):
    saved = hire(engine)
    employee = saved["entity_id"]
    with engine.store.connection(read_only=True) as connection:
        original = tuple(
            connection.execute(
                "SELECT * FROM entity_profile_revision WHERE id=?", (saved["profile_id"],)
            ).fetchone()
        )
    for action, date, expected in (
        ("unpaid_leave", "2025-02", "unpaid_leave"),
        ("resume", "2025-03", "regular"),
        ("depart", "2025-04", "departed"),
        ("hire", "2025-05", "regular"),
    ):
        saved = change(engine, employee, saved["revision"], action, date)
        assert profile(engine, employee)[1]["employment_state"] == expected
    with engine.store.connection(read_only=True) as connection:
        assert (
            tuple(
                connection.execute(
                "SELECT * FROM entity_profile_revision WHERE entity_id=? "
                "ORDER BY revision LIMIT 1",
                    (employee,),
                ).fetchone()
            )
            == original
        )


def test_future_and_stale_version_fail_atomically_and_retry_is_idempotent(engine):
    empty = database_state(engine)
    with pytest.raises((ValueError, KernelError, NeedsInformation)):
        hire(engine, date="9999-12-01", request="future-hire")
    assert database_state(engine) == empty
    saved = hire(engine)
    assert hire(engine) == saved
    employee = saved["entity_id"]
    before = database_state(engine)
    with pytest.raises((ValueError, KernelError, NeedsInformation)):
        change(engine, employee, saved["revision"], "depart", "9999-12")
    assert database_state(engine) == before
    with pytest.raises(KernelError):
        change(engine, employee, saved["revision"] + 1, "depart", "2025-06")
    assert database_state(engine) == before
    with pytest.raises(KernelError):
        hire(engine, date="2025-02-01")
    assert database_state(engine) == before


def test_metadata_cannot_bypass_program_owned_employment_fields(engine):
    saved = hire(engine)
    for data in ({"employment_state": "departed"}, {"employment_start": "2024-01"}):
        before = database_state(engine)
        with pytest.raises((ValueError, KernelError, NeedsInformation)):
            Entities(engine).update_entity_profile(
                saved["entity_id"],
                data,
                source="合成旁路输入",
                expected_revision=saved["revision"],
                request_id="bypass-" + next(iter(data)),
            )
        assert database_state(engine) == before


def test_failure_after_profile_append_rolls_back_identity_history_and_request(engine):
    before = database_state(engine)

    def fail(stage, connection):
        if stage == "published":
            raise RuntimeError("synthetic interruption after employment append")

    engine.fault = fail
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        hire(engine)
    assert database_state(engine) == before
    engine.fault = lambda stage, connection: None
    assert hire(engine)["revision"] == 1


def test_backfill_and_correction_preserve_later_current_state(engine):
    saved = hire(engine)
    employee = saved["entity_id"]
    departed = change(engine, employee, saved["revision"], "depart", "2025-06-30")
    leave = change(engine, employee, departed["revision"], "unpaid_leave", "2025-02-01")
    resume = change(engine, employee, leave["revision"], "resume", "2025-03-01")
    assert profile(engine, employee)[1]["employment_state"] == "departed"
    corrected = change(
        engine,
        employee,
        resume["revision"],
        "unpaid_leave",
        "2025-02-02",
        replaces_profile_id=leave["profile_id"],
        request="correct-leave",
    )
    assert corrected["revision"] > resume["revision"]
    assert profile(engine, employee)[1]["employment_state"] == "departed"


def test_same_month_sequence_requires_explicit_relationship(engine):
    saved = hire(engine, date="2025-01")
    employee = saved["entity_id"]
    before = database_state(engine)
    with pytest.raises((ValueError, KernelError, NeedsInformation)):
        change(engine, employee, saved["revision"], "unpaid_leave", "2025-01")
    assert database_state(engine) == before
    leave = change(
        engine,
        employee,
        saved["revision"],
        "unpaid_leave",
        "2025-01",
        after_profile_id=saved["profile_id"],
        request="ordered-leave",
    )
    assert profile(engine, employee)[1]["employment_state"] == "unpaid_leave"
    assert leave["revision"] > saved["revision"]


def test_legacy_explicit_baseline_survives_metadata_update_without_fabricated_event(engine):
    saved = save_entity_display_profile(
        engine,
        {
            "kind": "employee",
            "entity_id": "historical-person",
            "display_name": "合成旧档案员工",
            "employment_start": "2024-01",
            "employment_status": "active",
            "employment_state": "unpaid_leave",
            "source": "明确合成历史基线，非真实任职事件",
        },
        expected_revision=0,
        request_id="legacy-baseline",
    )
    Entities(engine).update_entity_profile(
        "historical-person",
        {"display_name": "合成新姓名"},
        source="合成姓名更正",
        expected_revision=saved["revision"],
        request_id="legacy-rename",
    )
    _, current = profile(engine, "historical-person")
    assert current["employment_state"] == "unpaid_leave"
    assert current["employment_start"] == "2024-01"
    assert not current.get("employment_event")


def test_event_links_cannot_reference_another_person(engine):
    first = hire(engine)
    other = hire(engine, request="other-hire")
    for link in ("after_profile_id", "replaces_profile_id"):
        before = database_state(engine)
        with pytest.raises((ValueError, KernelError, NeedsInformation)):
            change(
                engine,
                first["entity_id"],
                first["revision"],
                "unpaid_leave",
                "2025-02",
                request="cross-person-" + link,
                **{link: other["profile_id"]},
            )
        assert database_state(engine) == before


def test_history_pagination_retains_original_events_and_rejects_changed_revision(engine):
    saved = hire(engine)
    employee = saved["entity_id"]
    leave = change(engine, employee, saved["revision"], "unpaid_leave", "2025-02")
    resumed = change(engine, employee, leave["revision"], "resume", "2025-03")
    corrected = change(
        engine,
        employee,
        resumed["revision"],
        "unpaid_leave",
        "2025-02-02",
        replaces_profile_id=leave["profile_id"],
        request="history-correction",
    )
    directory = Entities(engine)
    first = directory.employee_employment_history(employee, limit=1)
    assert first["entity_id"] == employee
    assert first["revision"] == corrected["revision"]
    assert first["page"]["total_count"] == 4
    assert first["page"]["returned_count"] == 1
    assert first["page"]["has_more"]
    cursor = first["page"]["next_cursor"]
    found, page = list(first["items"]), first
    while page["page"]["has_more"]:
        page = directory.employee_employment_history(
            employee,
            cursor=page["page"]["next_cursor"],
            limit=1,
            expected_revision=first["revision"],
        )
        found.extend(page["items"])
    by_id = {item["profile_id"]: item for item in found}
    assert len(by_id) == 4
    assert by_id[leave["profile_id"]]["active"] is False
    assert by_id[leave["profile_id"]]["effective_date"] == "2025-02"
    assert by_id[corrected["profile_id"]]["replaces_profile_id"] == leave["profile_id"]
    assert by_id[corrected["profile_id"]]["source"] == "合成任职事项确认"
    assert all(item["recorded_at"] for item in found)
    Entities(engine).update_entity_profile(
        employee,
        {"note": "合成新增管理备注"},
        source="合成管理资料",
        expected_revision=corrected["revision"],
        request_id="history-rename",
    )
    with pytest.raises(KernelError):
        directory.employee_employment_history(
            employee,
            cursor=cursor,
            limit=1,
            expected_revision=first["revision"],
        )
    updated = directory.employee_employment_history(employee)
    assert updated["page"]["total_count"] == 4


def test_dashboard_refresh_and_cursor_follow_program_generated_current_status(engine):
    establish_dashboard_period(engine)
    first_employee = hire(engine)
    hire(engine, request="second-dashboard-employee")
    dashboard = Dashboard(engine)
    first = dashboard.employees("2025-01", employee_filter="employment_active", limit=1)
    cursor = first["data"]["collections"]["employees"]["page"]["next_cursor"]
    assert cursor
    leave = change(
        engine,
        first_employee["entity_id"],
        first_employee["revision"],
        "unpaid_leave",
        "2025-02",
    )
    with pytest.raises(KernelError) as failure:
        dashboard.employees(
            "2025-01",
            section="employees",
            cursor=cursor,
            limit=1,
            employee_filter="employment_active",
            expected_version=first["snapshot_version"],
        )
    assert failure.value.code == "dashboard_snapshot_changed"
    for _ in range(2):
        active = dashboard.employees("2025-01", employee_filter="employment_active")
        assert all(
            item["employee_id"] != first_employee["entity_id"]
            for item in active["data"]["collections"]["employees"]["items"]
        )
        unpaid = dashboard.employees("2025-01", employee_filter="employment_unpaid_leave")
        item = unpaid["data"]["collections"]["employees"]["items"][0]
        assert item["employee_id"] == first_employee["entity_id"]
        assert item["employment_state"] == "unpaid_leave"
        assert item["in_period"] is True
    change(engine, first_employee["entity_id"], leave["revision"], "resume", "2025-03")
    resumed = dashboard.employees("2025-01", employee_filter="employment_active")
    assert first_employee["entity_id"] in {
        item["employee_id"] for item in resumed["data"]["collections"]["employees"]["items"]
    }


def test_successor_order_relation_follows_replaced_predecessor(engine):
    saved = hire(engine, date="2025-01")
    employee = saved["entity_id"]
    leave = change(
        engine,
        employee,
        saved["revision"],
        "unpaid_leave",
        "2025-01",
        after_profile_id=saved["profile_id"],
        request="linked-leave",
    )
    resumed = change(
        engine,
        employee,
        leave["revision"],
        "resume",
        "2025-01",
        after_profile_id=leave["profile_id"],
        request="linked-resume",
    )
    corrected = change(
        engine,
        employee,
        resumed["revision"],
        "unpaid_leave",
        "2025-01",
        after_profile_id=saved["profile_id"],
        replaces_profile_id=leave["profile_id"],
        request="linked-correction",
    )
    assert profile(engine, employee)[1]["employment_state"] == "regular"
    events = Entities(engine).employee_employment_history(employee)["items"]
    by_id = {item["profile_id"]: item for item in events}
    assert by_id[resumed["profile_id"]]["after_profile_id"] == leave["profile_id"]
    assert by_id[leave["profile_id"]]["active"] is False
    assert by_id[corrected["profile_id"]]["active"] is True


@pytest.mark.parametrize("state,end", [("unpaid_leave", None), ("departed", "2025-05")])
def test_backfilled_hire_cannot_override_explicit_legacy_special_state(engine, state, end):
    saved = save_entity_display_profile(
        engine,
        {
            "kind": "employee",
            "entity_id": "legacy-special",
            "display_name": "合成旧任职员工",
            "employment_start": "2025-01",
            "employment_end": end,
            "employment_state": state,
            "employment_status": "inactive" if end else "active",
            "source": "明确合成旧任职基线",
        },
        expected_revision=0,
        request_id="special-baseline",
    )
    before = database_state(engine)
    with pytest.raises(NeedsInformation):
        hire(
            engine,
            date="2025-01",
            entity_id="legacy-special",
            expected_revision=saved["revision"],
            request="conflicting-backfill",
        )
    assert database_state(engine) == before
    assert profile(engine, "legacy-special")[1]["employment_state"] == state


@pytest.mark.parametrize("source,evidence", [("", None), ("合成未登记证据", "00" * 32)])
def test_missing_source_or_evidence_rolls_back_new_person(engine, source, evidence):
    before = database_state(engine)
    with pytest.raises((ValueError, KernelError, NeedsInformation)):
        Entities(engine).register_employee(
            {"display_name": "合成缺依据员工", "effective_date": "2025-01"},
            source=source,
            evidence_digest=evidence,
            request_id="missing-source",
        )
    assert database_state(engine) == before


def test_mixed_day_and_month_precision_needs_explicit_order(engine):
    saved = hire(engine, date="2025-01")
    before = database_state(engine)
    with pytest.raises(NeedsInformation) as failure:
        change(engine, saved["entity_id"], saved["revision"], "unpaid_leave", "2025-01-15")
    assert any(issue["field"] == "after_profile_id" for issue in failure.value.issues)
    assert database_state(engine) == before
    change(
        engine,
        saved["entity_id"],
        saved["revision"],
        "unpaid_leave",
        "2025-01-15",
        after_profile_id=saved["profile_id"],
        request="mixed-ordered",
    )
    assert profile(engine, saved["entity_id"])[1]["employment_state"] == "unpaid_leave"


def test_monthly_roster_preserves_each_employment_interval_and_excludes_gap(engine):
    for period in ("2025-01", "2025-02", "2025-03", "2025-04", "2025-05"):
        establish_dashboard_period(engine, period)
    saved = hire(engine)
    employee = saved["entity_id"]
    leave = change(engine, employee, saved["revision"], "unpaid_leave", "2025-02")
    unpaid_item = Dashboard(engine).employees("2025-02", employee_filter="employment_unpaid_leave")[
        "data"
    ]["collections"]["employees"]["items"][0]
    assert unpaid_item["in_period"] is True
    departed = change(engine, employee, leave["revision"], "depart", "2025-03")
    change(engine, employee, departed["revision"], "hire", "2025-05")
    dashboard = Dashboard(engine)
    for period, expected in (
        ("2025-01", True),
        ("2025-02", True),
        ("2025-03", True),
        ("2025-04", False),
        ("2025-05", True),
    ):
        items = dashboard.employees(period, employee_filter="all")["data"]["collections"][
            "employees"
        ]["items"]
        assert (
            next(item for item in items if item["employee_id"] == employee)["in_period"] is expected
        ), period


def test_legacy_departed_missing_end_requires_explicit_departure_before_rehire(engine):
    saved = save_entity_display_profile(
        engine,
        {
            "kind": "employee",
            "entity_id": "legacy-no-end",
            "display_name": "合成缺离职日期员工",
            "employment_start": "2025-06",
            "employment_end": None,
            "employment_state": "departed",
            "employment_status": "inactive",
            "source": "合成历史档案明确离职但未提供日期",
        },
        expected_revision=0,
        request_id="missing-end-baseline",
    )
    directory = Entities(engine)
    before = database_state(engine)
    with pytest.raises(NeedsInformation) as failure:
        directory.register_employee(
            {
                "effective_date": "2025-01",
                "after_profile_id": saved["profile_id"],
            },
            entity_id="legacy-no-end",
            expected_revision=saved["revision"],
            source="合成不可成立的再入职确认",
            request_id="impossible-rehire",
        )
    assert any(issue["field"] == "employment_end" for issue in failure.value.issues)
    assert database_state(engine) == before
    departed = change(engine, "legacy-no-end", saved["revision"], "depart", "2025-09")
    assert profile(engine, "legacy-no-end")[1]["employment_end"] == "2025-09"
    change(engine, "legacy-no-end", departed["revision"], "hire", "2025-10")
    _, current = profile(engine, "legacy-no-end")
    assert current["employment_state"] == "regular"
    assert current["employment_intervals"] == [
        {"start": "2025-06", "end": "2025-09"},
        {"start": "2025-10", "end": None},
    ]


def test_legacy_unknown_start_closed_interval_keeps_prior_month_membership_unknown(engine):
    establish_dashboard_period(engine, "2025-05")
    saved = save_entity_display_profile(
        engine,
        {
            "kind": "employee",
            "entity_id": "legacy-no-start",
            "display_name": "合成缺入职日期员工",
            "employment_start": None,
            "employment_end": "2025-09",
            "employment_state": "departed",
            "employment_status": "inactive",
            "source": "合成历史档案仅明确离职月份",
        },
        expected_revision=0,
        request_id="missing-start-baseline",
    )
    change(engine, "legacy-no-start", saved["revision"], "hire", "2025-10")
    _, current = profile(engine, "legacy-no-start")
    assert current["employment_intervals"] == [
        {"start": None, "end": "2025-09"},
        {"start": "2025-10", "end": None},
    ]
    item = Dashboard(engine).employees("2025-05", employee_filter="all")["data"]["collections"][
        "employees"
    ]["items"][0]
    assert item["employee_id"] == "legacy-no-start"
    assert item["in_period"] is None
    assert item["period_state"] == "unknown"
