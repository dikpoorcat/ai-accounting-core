"""Export status is a primary-key read; full verification authenticates saved sources."""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_opening_continuation as opening_cases
import test_reports as report_cases
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import Reports, run_report_jobs
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import canonical, digest

book = report_cases.book
opening_book = opening_cases.book


def _queue(report, *, year=2026, quarter=1, carry=None, request_id="browser-job-source"):
    plan = report.preview_export(year, quarter, carry_forward_fact_id=carry)
    task = report.confirm_browser_export(
        year,
        quarter,
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        request_id=request_id,
        carry_forward_fact_id=carry,
    )
    return plan, task["job_id"]


def _payload(engine, job_id):
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
    return json.loads(row[0])


def _replace_payload(engine, job_id, payload):
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        triggers = list(
            connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='jobs'"
            )
        )
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            "UPDATE jobs SET payload=? WHERE id=?", (canonical(payload), job_id)
        )
        for _, definition in triggers:
            connection.execute(definition)
        connection.commit()


def _resign(plan):
    plan["digest"] = digest(
        {key: value for key, value in plan.items() if key not in {"digest", "epochs"}}
    ).hex()


def _damage_digest(engine, fact_id, replacement):
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        triggers = list(
            connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
                "AND tbl_name='fact_revision'"
            )
        )
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            "UPDATE fact_revision SET digest=? WHERE id=?", (replacement, fact_id)
        )
        for _, definition in triggers:
            connection.execute(definition)
        connection.commit()


def test_single_export_status_does_not_decode_plans_sources_or_read_files(book, monkeypatch):
    report = report_cases.scenario(book)
    report_cases.close_quarter(book)
    engine = book[0]
    _, job_id = _queue(report)
    assert run_report_jobs(engine)[0]["status"] == "succeeded"
    with engine.store.connection() as connection:
        connection.execute("UPDATE jobs SET result='invalid stored result' WHERE id=?", (job_id,))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("status must not decode or verify export data")

    monkeypatch.setattr(engine, "jobs", forbidden)
    monkeypatch.setattr(Store, "fact", forbidden)
    monkeypatch.setattr(QueryReads, "snapshot", forbidden)
    monkeypatch.setattr(report, "download_browser_report", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr("ai_accounting.kernel.reports.json", SimpleNamespace(loads=forbidden))
    for status in ("pending", "running", "failed", "succeeded"):
        with engine.store.connection() as connection:
            connection.execute(
                "UPDATE jobs SET status=?,error_code='private diagnostic text' WHERE id=?",
                (status, job_id),
            )
        item = report.browser_export_status(job_id)
        assert item == {
            "schema_version": 1,
            "company_id": engine.store.company_id,
            "database_id": engine.store.database_id,
            "job_id": job_id,
            "status": status,
            "attempts": 1,
            "error_code": "job_failed" if status == "failed" else None,
            "error_message": (
                "后台任务未完成，请检查任务设置或本机运行状态" if status == "failed" else None
            ),
        }


def test_full_verifier_rejects_changed_plan_identity_shape_and_missing_sources(book):
    report = report_cases.scenario(book)
    report_cases.close_quarter(book)
    engine = book[0]
    _, job_id = _queue(report)
    original = _payload(engine, job_id)
    assert original["plan"]["report_fact_ids"]

    def invalid(mutator, *, resign=True):
        changed = json.loads(json.dumps(original))
        mutator(changed["plan"])
        if resign:
            _resign(changed["plan"])
        _replace_payload(engine, job_id, changed)
        assert report.browser_export_status(job_id)["status"] == "pending"
        with pytest.raises(KernelError) as error:
            Maintenance(engine).verify_integrity()
        assert error.value.code == "content_integrity_failed"
        _replace_payload(engine, job_id, original)

    invalid(lambda plan: plan.update(company_id="other-company"))
    invalid(lambda plan: plan.update(database_id="other-database"))
    invalid(lambda plan: plan["report_fact_ids"].append(plan["report_fact_ids"][0]))
    invalid(lambda plan: plan["report_fact_ids"].append("missing-fact"))
    invalid(lambda plan: plan["report_fact_ids"].append(7))
    invalid(lambda plan: plan.update(digest="bad"), resign=False)
    invalid(lambda plan: plan.update(report_fact_ids="not-a-list"))
    invalid(lambda plan: plan["report_fact_ids"].append(""))
    for year in (True, 0, 10000, "2026"):
        invalid(lambda plan, value=year: plan["period"].update(year=value))
    for quarter in (True, 0, 5, "1"):
        invalid(lambda plan, value=quarter: plan["period"].update(quarter=value))
    invalid(lambda plan: plan.update(period=None))
    assert report.browser_export_status(job_id)["status"] == "pending"
    Maintenance(engine).verify_integrity()


def test_selected_carry_is_authenticated_and_damage_is_rejected(opening_book):
    chosen = opening_cases._midyear_report_supplement_scenario(opening_book)
    engine = opening_book[0]
    report = Reports(engine)
    plan, job_id = _queue(report, year=2026, quarter=3, carry=chosen)
    assert chosen in plan["report_fact_ids"]
    assert report.browser_export_status(job_id)["status"] == "pending"
    Maintenance(engine).verify_integrity()
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            "SELECT digest FROM fact_revision WHERE id=?", (chosen,)
        ).fetchone()[0]
    _damage_digest(engine, chosen, b"\0" * len(original))
    with pytest.raises(KernelError) as error:
        Maintenance(engine).verify_integrity()
    assert error.value.code == "content_integrity_failed"
    _damage_digest(engine, chosen, original)
    Maintenance(engine).verify_integrity()


@pytest.mark.parametrize("case", ["carry_kind", "carry_typed", "other_as_carry"])
def test_full_verifier_rejects_inconsistent_carry_kind_and_typed_heads(opening_book, case):
    chosen = opening_cases._midyear_report_supplement_scenario(opening_book)
    engine = opening_book[0]
    report = Reports(engine)
    plan, job_id = _queue(report, year=2026, quarter=3, carry=chosen)
    assert chosen in plan["report_fact_ids"]
    Maintenance(engine).verify_integrity()
    if case == "carry_kind":
        damage(
            engine,
            "subject",
            "UPDATE subject SET kind='report_profile' WHERE id="
            "(SELECT subject_id FROM fact_revision WHERE id=?)",
            (chosen,),
        )
    elif case == "carry_typed":
        damage(
            engine,
            "fact_report_carry_forward",
            "DELETE FROM fact_report_carry_forward WHERE revision_id=?",
            (chosen,),
            foreign_keys=False,
        )
    else:
        with engine.store.connection(read_only=True) as connection:
            other = next(
                ident
                for ident in plan["report_fact_ids"]
                if ident != chosen
                and connection.execute(
                    "SELECT s.kind FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
                    "WHERE f.id=?",
                    (ident,),
                ).fetchone()[0]
                != "report_carry_forward"
            )
        damage(
            engine,
            "subject",
            "UPDATE subject SET kind='report_carry_forward' WHERE id="
            "(SELECT subject_id FROM fact_revision WHERE id=?)",
            (other,),
        )
    assert report.browser_export_status(job_id)["status"] == "pending"
    with pytest.raises(KernelError) as error:
        Maintenance(engine).verify_integrity()
    assert error.value.code == "content_integrity_failed"


def test_source_damage_belongs_to_full_verifier_not_export_status(book):
    report = report_cases.scenario(book)
    report_cases.close_quarter(book)
    engine = book[0]
    plan, job_id = _queue(report)
    non_carry_id = plan["report_fact_ids"][0]
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            "SELECT digest FROM fact_revision WHERE id=?", (non_carry_id,)
        ).fetchone()[0]
    _damage_digest(engine, non_carry_id, b"\0" * len(original))
    assert report.browser_export_status(job_id)["status"] == "pending"
    with pytest.raises(KernelError) as error:
        Maintenance(engine).verify_integrity()
    assert error.value.code == "content_integrity_failed"
    _damage_digest(engine, non_carry_id, original)


def test_single_export_status_work_is_constant_with_unrelated_jobs_and_history(book, monkeypatch):
    report = report_cases.scenario(book)
    report_cases.close_quarter(book)
    engine = book[0]
    _, job_id = _queue(report)

    def observed():
        stats = {"connections": 0, "selects": [], "vm": 0}
        original = engine.store.connection

        @contextmanager
        def traced(*args, **kwargs):
            with original(*args, **kwargs) as connection:
                stats["connections"] += 1

                def sql(statement):
                    if statement.startswith("SELECT"):
                        stats["selects"].append(statement)

                def tick():
                    stats["vm"] += 1
                    return 0

                connection.set_trace_callback(sql)
                connection.set_progress_handler(tick, 1)
                try:
                    yield connection
                finally:
                    connection.set_trace_callback(None)
                    connection.set_progress_handler(None, 0)

        monkeypatch.setattr(engine.store, "connection", traced)
        try:
            item = report.browser_export_status(job_id)
        finally:
            monkeypatch.setattr(engine.store, "connection", original)
        return item, stats

    baseline, work = observed()
    for number in range(24):
        book[1](
            "report_profile",
            f"future-profile-{number}",
            {
                "period": "2026-04",
                "company_name": f"其他期间资料 {number}",
                "accounting_standard": "small_enterprise",
                "bookkeeping_start": "2026-01",
                "newly_established_zero_opening_confirmed": True,
            },
        )
    with engine.store.connection() as connection:
        connection.executemany(
            "INSERT INTO jobs(id,kind,payload,status,result) "
            "VALUES(?,'backup','{}','succeeded','{}')",
            [(f"unrelated-{number}",) for number in range(400)],
        )
        detail = connection.execute(
            "EXPLAIN QUERY PLAN SELECT kind,status,attempts,error_code FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()[3]
    padded, padded_work = observed()
    assert baseline == padded
    assert "SEARCH jobs USING INDEX" in detail and "id=?" in detail
    assert work == padded_work
    assert work["connections"] == 1
    assert len(work["selects"]) == 1
    assert work["selects"][0].startswith(
        "SELECT kind,status,attempts,error_code FROM jobs WHERE id="
    )
    assert work["vm"] < 50, work
