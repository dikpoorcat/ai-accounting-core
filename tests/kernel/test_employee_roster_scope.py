"""Employee discovery witnesses and full wage money have separate proof scopes."""

import json
import sqlite3

import pytest
from entity_fixture import save_entity_display_profile
from stage9_book import MixedBook
from test_identity_corrections import identity_engine as identity_engine_fixture
from test_integrity_content import damage, verify
from test_payroll import contribution_policy, income_tax_policy, opening, payroll, profile
from test_payroll_corrections import Company
from test_settlement_late_reviews import prepared

from ai_accounting.kernel import dashboard as dashboard_module
from ai_accounting.kernel import dashboard_reads
from ai_accounting.kernel.backup import BackupError, verify_file
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.types import canonical, digest

identity_engine = identity_engine_fixture


def result(engine, period, **kwargs):
    response = Dashboard(engine).employees(period, preparation="deferred", **kwargs, employee_filter="all")
    response.pop("generated_at", None)
    response["data"].pop("generated_at", None)
    return response


def rejected_read(engine, period, *, warm, expected_code="content_integrity_failed"):
    with pytest.raises(KernelError) as failure:
        if warm:
            # Reuse a real successful immutable root proof in this owned read;
            # it is not a proof of the mutable calculation or publication.
            with Dashboard(engine)._snapshot(period) as snapshot:
                closed_row = snapshot.connection.execute(
                    "SELECT period FROM period_close WHERE period<=? ORDER BY period DESC LIMIT 1",
                    (snapshot.month,),
                ).fetchone()
                if closed_row is None:
                    snapshot.month_journal.sql()
                else:
                    closed = snapshot.reads.authoritative_close_rows(
                        periods=[closed_row[0]], through_period=snapshot.month
                    )[0]
                    snapshot.reads.close_header(closed)
                dashboard_module._employees(snapshot)
        else:
            result(engine, period)
    assert failure.value.code == expected_code


def february(company, *, employee="employee"):
    if employee != "employee":
        company.save(profile(employee_id=employee, effective_to="2026-02"), "profile-" + employee)
        company.save(
            opening(
                period="2026-02", employee_id=employee, through_period="2026-01",
                cumulative_standard_deduction_fen=500000,
            ),
            "opening-" + employee,
        )
    company.save(payroll(period="2026-02", employee_id=employee,
                         profile_id="profile" if employee == "employee" else "profile-" + employee),
                 "february")
    company.confirm_payroll("february")
    company.publish("february")


def current_zero_company(tmp_path):
    company = Company(tmp_path / "current-zero.sqlite")
    for fact, subject in (
        (profile(social_insurance_participating=False, social_insurance_base_fen=None), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(accounting_gross_salary_fen=0, tax_reported_salary_fen=0), "january"),
    ):
        company.save(fact, subject)
    company.confirm_payroll("january")
    company.publish("january")
    return company


def test_default_roster_authenticates_witnesses_and_month_money_without_all_history(
    tmp_path, monkeypatch
):
    book = MixedBook(tmp_path / "mixed", employees=2, businesses=28)
    for index in range(3):
        book.add_month(index)
    original = dashboard_module.verified_payroll_heads
    scopes = []

    def observed(snapshot, heads):
        scopes.append({head["subject_id"] for head in heads})
        return original(snapshot, heads)

    monkeypatch.setattr(dashboard_module, "verified_payroll_heads", observed)
    actual = result(book.engine, "2016-03", limit=1)
    assert len(scopes) == 1 and len(scopes[0]) == 2
    assert actual["data"]["employees"]["registered_count"] == 2
    assert actual["data"]["collections"]["employees"]["page"]["returned_count"] == 1
    monkeypatch.setattr(dashboard_module, "payroll_list_head_metadata", lambda *_a, **_k: None)
    expected = result(book.engine, "2016-03", limit=1)
    assert len(scopes[-1]) == 6
    assert actual == expected


def test_old_zero_wage_and_retired_employee_remain_in_complete_roster(tmp_path, monkeypatch):
    company = prepared(tmp_path, zero=True)
    save_entity_display_profile(company.engine, {
        "kind": "employee", "entity_id": "employee", "employment_status": "inactive",
        "employment_start": "2025-12", "employment_end": "2026-01", "source": "明确的合成离职资料",
    }, expected_revision=0, request_id="retired")
    february(company, employee="employee-2")
    actual = result(company.engine, "2026-02")
    employees = {
        item["employee_id"]: item
        for item in actual["data"]["collections"]["employees"]["items"]
    }
    assert set(employees) == {"employee", "employee-2"}
    assert employees["employee"]["period_state"] == "ended"
    assert employees["employee"]["gross_salary_fen"] == 0
    assert actual["data"]["employees"]["registered_count"] == 2
    monkeypatch.setattr(dashboard_module, "payroll_list_head_metadata", lambda *_a, **_k: None)
    assert actual == result(company.engine, "2026-02")


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("missing", ["publication", "calculation", "redirected_fact", "subject",
                                     "fact_revision"])
@pytest.mark.parametrize("period", ["2026-01", "2026-02"])
def test_zero_line_frozen_witness_cannot_disappear_with_missing_mutable_source(
    tmp_path, missing, period, warm
):
    company = prepared(tmp_path, zero=True)
    if period == "2026-02":
        company.save(
            profile(period="2026-02", effective_from="2026-02", effective_to="2026-02"),
            "february-profile",
        )
    assert result(company.engine, period)["data"]["employees"]["registered_count"] == 1
    with company.engine.store.connection(read_only=True) as connection:
        profile_content = connection.execute(
            "SELECT content FROM entity_profile_revision WHERE entity_id='employee'"
        ).fetchone()[0]
        assert json.loads(profile_content)["display_name"] is None
    if missing == "redirected_fact":
        damage(
            company.engine, "calculation",
            "UPDATE calculation SET fact_id=(SELECT id FROM fact_revision "
            "WHERE subject_id='income-tax') WHERE subject_id='january'",
        )
    else:
        table = "calculation_publication" if missing == "publication" else missing
        where = "id='january'" if table == "subject" else "subject_id='january'"
        damage(
            company.engine, table, f"DELETE FROM {table} WHERE {where}",
            foreign_keys=False,
        )
    rejected_read(company.engine, period, warm=warm)


def test_future_posting_does_not_become_a_frozen_roster_witness(tmp_path):
    company = prepared(tmp_path, zero=True)
    february(company, employee="employee-2")
    assert result(company.engine, "2026-01")["data"]["employees"]["registered_count"] == 1
    assert result(company.engine, "2026-02")["data"]["employees"]["registered_count"] == 2


def test_unpublished_later_wage_keeps_earlier_adopted_roster_and_money(tmp_path, monkeypatch):
    company = prepared(tmp_path, zero=True)
    company.save(payroll(
        period="2026-02", accounting_gross_salary_fen=0, tax_reported_salary_fen=0,
    ), "unpublished-february")
    actual = result(company.engine, "2026-02")
    assert actual["data"]["employees"]["registered_count"] == 1
    assert actual["data"]["employees"]["payroll_count"] == 0
    monkeypatch.setattr(dashboard_module, "payroll_list_head_metadata", lambda *_a, **_k: None)
    assert actual == result(company.engine, "2026-02")


def test_withdrawn_later_wage_keeps_the_earlier_person_witness(tmp_path, monkeypatch):
    company = prepared(tmp_path, zero=True)
    february(company)
    proof = company.owner_confirmation
    preview = company.engine.preview_delete("february", recording_error_evidence=proof)
    company.engine.delete(
        "february", preview_digest=preview["digest"], epochs=preview["epochs"],
        recording_error_evidence=proof, request_id=company.request(),
    )
    actual = result(company.engine, "2026-02")
    assert actual["data"]["employees"]["registered_count"] == 1
    assert actual["data"]["employees"]["payroll_count"] == 0
    monkeypatch.setattr(dashboard_module, "payroll_list_head_metadata", lambda *_a, **_k: None)
    assert actual == result(company.engine, "2026-02")


def test_same_names_and_registered_person_without_wages_preserve_full_roster(tmp_path, monkeypatch):
    company = prepared(tmp_path, zero=True)
    february(company, employee="employee-2")
    for employee in ("employee", "employee-2", "registered-no-wages"):
        save_entity_display_profile(company.engine, {
            "kind": "employee", "entity_id": employee, "display_name": "同名员工",
            "employment_status": "active", "source": "明确的合成对象档案",
        }, expected_revision=0, request_id="same-name-" + employee)
    actual = result(company.engine, "2026-02", limit=1)
    assert actual["data"]["employees"]["registered_count"] == 3
    assert actual["data"]["collections"]["employees"]["page"]["returned_count"] == 1
    monkeypatch.setattr(dashboard_module, "payroll_list_head_metadata", lambda *_a, **_k: None)
    assert actual == result(company.engine, "2026-02", limit=1)


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("changed", ["missing_role", "kind", "period"])
def test_required_zero_wage_role_damage_cannot_hide_its_month_head(tmp_path, warm, changed):
    company = prepared(tmp_path, zero=True)
    assert result(company.engine, "2026-01")["data"]["employees"]["registered_count"] == 1
    if changed == "missing_role":
        statement = (
            "DELETE FROM entity_reference_recorded WHERE role='employee' "
            "AND fact_id IN(SELECT id FROM fact_revision WHERE subject_id='january')"
        )
    else:
        assignment = "kind='asset'" if changed == "kind" else "period=period+1"
        statement = (
            f"UPDATE entity_reference_recorded SET {assignment} WHERE role='employee' "
            "AND fact_id IN(SELECT id FROM fact_revision WHERE subject_id='january')"
        )
    damage(company.engine, "entity_reference_recorded", statement)
    rejected_read(company.engine, "2026-01", warm=warm, expected_code="entity_reference_corrupt")


@pytest.mark.parametrize("warm", [False, True])
def test_open_zero_wage_without_current_pointer_is_not_employee_absence(tmp_path, warm):
    company = prepared(tmp_path, zero=True)
    company.save(payroll(
        period="2026-02", accounting_gross_salary_fen=0, tax_reported_salary_fen=0,
    ), "february")
    company.confirm_payroll("february")
    company.publish("february")
    assert result(company.engine, "2026-02")["data"]["employees"]["payroll_count"] == 1
    damage(company.engine, "calculation_current",
           "DELETE FROM calculation_current WHERE subject_id='february'")
    rejected_read(company.engine, "2026-02", warm=warm)


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("changed", ["missing_role", "source_digest"])
def test_open_zero_wage_role_damage_is_proved_from_the_required_month(tmp_path, warm, changed):
    company = prepared(tmp_path, zero=True)
    company.save(payroll(
        period="2026-02", accounting_gross_salary_fen=0, tax_reported_salary_fen=0,
    ), "february")
    company.confirm_payroll("february")
    company.publish("february")
    assert result(company.engine, "2026-02")["data"]["employees"]["payroll_count"] == 1
    where = (
        "WHERE role='employee' AND fact_id IN"
        "(SELECT id FROM fact_revision WHERE subject_id='february')"
    )
    statement = (
        "DELETE FROM entity_reference_current " + where
        if changed == "missing_role" else
        "UPDATE entity_reference_current SET source_digest=zeroblob(32) " + where
    )
    damage(company.engine, "entity_reference_current", statement)
    rejected_read(company.engine, "2026-02", warm=warm, expected_code="entity_reference_corrupt")


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("missing", ["publication", "calculation"])
def test_current_zero_wage_requires_its_exact_publication_and_calculation(tmp_path, missing, warm):
    company = prepared(tmp_path, zero=True)
    company.save(
        payroll(period="2026-02", accounting_gross_salary_fen=0, tax_reported_salary_fen=0),
        "february",
    )
    company.confirm_payroll("february")
    company.publish("february")
    assert result(company.engine, "2026-02")["data"]["employees"]["payroll_count"] == 1
    table = "calculation_publication" if missing == "publication" else "calculation"
    damage(
        company.engine, table, f"DELETE FROM {table} WHERE subject_id='february'",
        foreign_keys=False,
    )
    rejected_read(company.engine, "2026-02", warm=warm)


@pytest.mark.parametrize("content_version", [1, 2])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("missing", ["publication", "calculation"])
def test_current_zero_wage_without_frozen_anchor_requires_exact_source(
    tmp_path, missing, warm, content_version
):
    company = current_zero_company(tmp_path)
    with historical_content(content_version):
        assert result(company.engine, "2026-01")["data"]["employees"]["payroll_count"] == 1
        table = "calculation_publication" if missing == "publication" else "calculation"
        damage(
            company.engine, table, f"DELETE FROM {table} WHERE subject_id='january'",
            foreign_keys=False,
        )
        rejected_read(company.engine, "2026-01", warm=warm)


@pytest.mark.parametrize("warm", [False, True])
def test_current_zero_wage_without_frozen_anchor_missing_subject_is_not_absence(tmp_path, warm):
    company = current_zero_company(tmp_path)
    assert result(company.engine, "2026-01")["data"]["employees"]["payroll_count"] == 1
    damage(company.engine, "subject", "DELETE FROM subject WHERE id='january'", foreign_keys=False)
    rejected_read(company.engine, "2026-01", warm=warm)


@pytest.mark.parametrize("warm", [False, True])
def test_selected_frozen_wage_rejects_a_damaged_reference_type(tmp_path, warm):
    company = prepared(tmp_path, zero=True)
    assert result(company.engine, "2026-01")["data"]["employees"]["registered_count"] == 1
    damage(
        company.engine, "close_reference",
        "UPDATE close_reference SET reference_type='damaged-calculation' "
        "WHERE path='adopted_results[*].calculation_id' AND reference_id IN "
        "(SELECT id FROM calculation WHERE subject_id='january')",
    )
    rejected_read(company.engine, "2026-01", warm=warm,
                  expected_code="read_index_integrity_failed")


def _sql_vm_work(connection, query, parameters):
    # Prepare this same statement after fixture schema changes before counting
    # its execution, so schema loading cannot masquerade as scoped query work.
    list(connection.execute(query, parameters))
    progress = [0]

    def visited():
        progress[0] += 100
        return 0

    connection.set_progress_handler(visited, 100)
    try:
        rows = list(connection.execute(query, parameters))
    finally:
        connection.set_progress_handler(None, 0)
    return rows, progress[0]


def test_head_candidate_and_exact_reference_work_ignore_unrelated_sql_rows(tmp_path):
    company = prepared(tmp_path, zero=True)
    copy = sqlite3.connect(":memory:")
    copy.row_factory = sqlite3.Row
    with Dashboard(company.engine)._snapshot("2026-01") as snapshot:
        query, parameters = dashboard_reads._adopted_heads_sql(
            snapshot, dashboard_module.PAYROLL_KINDS | {"opening_payroll_payable"}
        )
        query += "SELECT subject_id,calculation_id,posting_period,fact_id FROM heads WHERE rn=1"
        identifiers = [row[0] for row in snapshot.connection.execute(
            "SELECT id FROM calculation WHERE subject_id='january'"
        )]
        reference_parameters = canonical([*identifiers, "unrelated-absent"]), snapshot.month
        snapshot.connection.backup(copy)
    reference_query = dashboard_reads._head_close_locator_sql()
    before_heads, before_head_vm = _sql_vm_work(copy, query, parameters)
    before_refs, before_ref_vm = _sql_vm_work(copy, reference_query, reference_parameters)
    assert len(before_heads) == len(before_refs) == 1

    # This isolated SQLite copy is only a SQL work fixture. These extra rows
    # are deliberately unconsumed noise, not asserted as valid kernel facts
    # or a complete frozen directory. All business proof tests use real writes.
    copy.execute("PRAGMA foreign_keys=OFF")
    for row in copy.execute("SELECT name FROM sqlite_schema WHERE type='trigger'").fetchall():
        copy.execute('DROP TRIGGER "' + row[0].replace('"', '""') + '"')
    highwater = copy.execute("SELECT max(sequence) FROM calculation_publication").fetchone()[0]
    close_period = copy.execute("SELECT period FROM period_close").fetchone()[0]
    for index in range(1000):
        subject, fact, calculation, publication = (
            f"work-noise-{prefix}-{index}" for prefix in ("subject", "fact", "calc", "pub")
        )
        copy.execute("INSERT INTO subject VALUES(?,?)", (subject, "reimbursed_asset"))
        copy.execute("INSERT INTO fact_revision VALUES(?,?,1,?,?)",
                     (fact, subject, close_period, bytes(32)))
        copy.execute("INSERT INTO calculation VALUES(?,?,?,?,?,?,?,?)", (
            calculation, subject, fact, "reimbursed_asset", close_period, "{}", bytes(32), "work",
        ))
        copy.execute("INSERT INTO calculation_current VALUES(?,?)", (subject, calculation))
        copy.execute(
            "INSERT INTO calculation_publication VALUES(?,?,?,NULL,?,'initial',?,NULL,NULL)",
            (publication, highwater + index + 1, subject, calculation, close_period),
        )
        for path, typ, ident in (
            ("adopted_results[*].calculation_id", "calculation", calculation),
            ("adopted_results[*].fact_id", "fact", fact),
        ):
            copy.execute(
                "INSERT INTO close_reference(close_period,path,position,reference_type,"
                "reference_id,related_id) VALUES(?,?,?,?,?,NULL)",
                (close_period, path, str(100000 + index), typ, ident),
            )
    after_heads, after_head_vm = _sql_vm_work(copy, query, parameters)
    after_refs, after_ref_vm = _sql_vm_work(copy, reference_query, reference_parameters)
    assert [tuple(row) for row in after_heads] == [tuple(row) for row in before_heads]
    assert [tuple(row) for row in after_refs] == [tuple(row) for row in before_refs]
    print(json.dumps({"head_vm": [before_head_vm, after_head_vm],
                      "reference_vm": [before_ref_vm, after_ref_vm]}))
    assert after_head_vm <= before_head_vm + 1000
    assert after_ref_vm <= before_ref_vm + 1000
    copy.close()


def test_real_month_and_employee_growth_keeps_roster_locator_work_linear(tmp_path):
    company = Company(tmp_path / "growing-roster.sqlite")
    company.save(contribution_policy(), "contributions")
    company.save(income_tax_policy(), "income-tax")
    employees = []
    observations = []
    kinds = dashboard_module.PAYROLL_KINDS | {"opening_payroll_payable"}
    for month in range(1, 13):
        period = f"2026-{month:02d}"
        if month in (1, 3):
            for index in range(len(employees), len(employees) + 2):
                employee = f"employee-{index}"
                company.save(profile(
                    period=period, employee_id=employee, effective_from=period,
                    effective_to="2026-12", social_insurance_participating=False,
                    social_insurance_base_fen=None,
                ), f"profile-{employee}")
                company.save(opening(
                    period=period, employee_id=employee,
                    through_period=None if month == 1 else "2026-02",
                    cumulative_standard_deduction_fen=(month - 1) * 500000,
                ), f"opening-{employee}")
                employees.append(employee)
        subjects = []
        for employee in employees:
            subject = f"payroll-{month}-{employee}"
            company.save(payroll(
                period=period, employee_id=employee, profile_id=f"profile-{employee}",
                accounting_gross_salary_fen=0, tax_reported_salary_fen=0,
            ), subject)
            subjects.append(subject)
        company.confirm_payroll(*subjects)
        company.publish(*subjects)
        if month in (2, 12):
            response = result(company.engine, period)
            assert response["data"]["employees"]["registered_count"] == len(employees)
            assert response["data"]["employees"]["payroll_count"] == len(employees)
            assert response["data"]["employees"]["gross_salary_fen"] == 0
            with Dashboard(company.engine)._snapshot(period) as snapshot:
                # Count all locator phases, not merely the few returned heads.
                progress = [0]

                def visited(progress=progress):
                    progress[0] += 100
                    return 0

                snapshot.connection.set_progress_handler(visited, 100)
                try:
                    candidates = dashboard_reads._payroll_roster_candidates(snapshot, kinds, set())
                    assert candidates is not None
                    query, parameters = dashboard_reads._payroll_roster_sql(
                        snapshot, kinds, set(), head_subjects=candidates[1],
                    )
                    rows = list(snapshot.connection.execute(query, parameters))
                finally:
                    snapshot.connection.set_progress_handler(None, 0)
                work = progress[0]
                query, parameters = dashboard_reads._payroll_roster_sql(snapshot, kinds, set())
                full_rows, full_work = _sql_vm_work(snapshot.connection, query, parameters)
                assert {tuple(row) for row in rows} == {tuple(row) for row in full_rows}
                assert len({row["id"] for row in rows}) == len(employees)
                assert {row["roster_employee_id"] for row in rows
                        if row["roster_employee_id"] is not None} == set(employees)
                required_heads = snapshot.connection.execute(
                    "SELECT count(*) FROM calculation WHERE kind='payroll'"
                ).fetchone()[0]
                observations.append({"month": month, "employees": len(employees),
                                     "required_heads": required_heads, "vm": work,
                                     "full_locator_vm": full_work})
        if month < 12:
            company.close(period)
    before, after = observations
    assert (before["required_heads"], after["required_heads"]) == (4, 44)
    print(json.dumps({"real_roster_growth": observations}))
    assert after["vm"] <= before["vm"] * 5
    assert after["vm"] < after["full_locator_vm"]


@pytest.mark.parametrize("damage_scope", ["missing_item", "redirected_item"])
def test_actual_correction_scope_is_authenticated_before_narrowing_roster(
    identity_engine, damage_scope, monkeypatch
):
    from test_identity_corrections import (
        test_payroll_conflict_resolution_recomputes_later_cumulative_state as scenario,
    )

    scenario(identity_engine, True)
    engine, _evidence, _first, _second = identity_engine
    current = result(engine, "2026-03")
    original_frozen = result(engine, "2026-01")
    assert current["data"]["employees"]["registered_count"] >= 2
    with monkeypatch.context() as full:
        full.setattr(dashboard_module, "payroll_list_head_metadata", lambda *_a, **_k: None)
        assert current == result(engine, "2026-03")
        assert original_frozen == result(engine, "2026-01")
    statement = (
        "DELETE FROM identity_correction_item WHERE subject_id='jan-a'"
        if damage_scope == "missing_item" else
        "UPDATE identity_correction_item SET replacement_subject_id='feb-b' "
        "WHERE subject_id='jan-a'"
    )
    damage(engine, "identity_correction_item", statement)
    with pytest.raises(KernelError) as failure:
        result(engine, "2026-03")
    assert failure.value.code == "content_integrity_failed"
    assert failure.value.details["component"] == "identity_correction"


def test_unused_old_body_stays_outside_roster_but_history_core_and_backup_reject_it(tmp_path):
    company = prepared(tmp_path)
    february(company)
    company.close("2026-02")
    expected = result(company.engine, "2026-02")
    with company.engine.store.connection(read_only=True) as connection:
        saved = connection.execute(
            "SELECT id,outcome FROM calculation WHERE subject_id='january'"
        ).fetchone()
    outcome = json.loads(saved["outcome"])
    outcome["synthetic_changed_result"] = True
    damage(company.engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
           (canonical(outcome), digest(outcome), saved["id"]))
    assert result(company.engine, "2026-02") == expected
    with pytest.raises(KernelError):
        from ai_accounting.kernel.business_queries import BusinessQueries

        BusinessQueries(company.engine).business_status("january", "2026-02")
    with Dashboard(company.engine)._snapshot("2026-02") as snapshot:
        with pytest.raises(KernelError):
            snapshot.calculations.selected(kinds={"payroll"}, subjects={"january"})
    with pytest.raises(KernelError):
        verify(company.engine)
    with pytest.raises(BackupError):
        verify_file(company.engine.store.path)
