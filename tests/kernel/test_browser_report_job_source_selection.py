"""Browser job titles select only adopted carry sources; delivery keeps its own proof."""

import json
from contextlib import contextmanager

import pytest
import test_opening_continuation as opening_cases
import test_reports as report_cases
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.maintenance import Maintenance
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


def test_panel_and_single_job_poll_keep_status_and_do_not_decode_unrelated_facts(
    book, monkeypatch
):
    report = report_cases.scenario(book)
    report_cases.close_quarter(book)
    engine = book[0]
    plan, job_id = _queue(report)
    assert plan["report_fact_ids"]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("non-carry report facts must not be decoded for a job title")

    monkeypatch.setattr(Store, "fact", forbidden)
    for status in ("pending", "running", "failed"):
        with engine.store.connection() as connection:
            connection.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        panel = report.browser_job_results(engine.jobs(limit=20))[0]
        single = report.browser_job_results(engine.jobs(job_id=job_id, limit=1))[0]
        assert panel["report_source"] == single["report_source"] == {
            "year": 2026,
            "quarter": 1,
            "carry_forward_fact_id": None,
        }
        assert panel["status"] == single["status"] == status
        assert not panel["download_available"] and not single["download_available"]
        assert "report_source" not in engine.jobs(job_id=job_id)[0]

    with engine.store.connection() as connection:
        connection.execute("UPDATE jobs SET status='pending' WHERE id=?", (job_id,))
    assert run_report_jobs(engine)[0]["status"] == "succeeded"
    item = report.browser_job_results(engine.jobs(job_id=job_id))[0]
    assert item["report_source"]["carry_forward_fact_id"] is None
    assert item["delivery_status"] == "verified" and item["download_available"]
    name, content = report.download_browser_report(job_id)
    assert item["download_file_name"] == name and content


def test_browser_job_rejects_changed_plan_identity_shape_and_missing_sources(book):
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
        item = report.browser_job_results(engine.jobs(job_id=job_id))[0]
        assert item["delivery_status"] == "invalid"
        assert not item["download_available"] and "report_source" not in item
        _replace_payload(engine, job_id, original)

    invalid(lambda plan: plan.update(company_id="other-company"))
    invalid(lambda plan: plan.update(database_id="other-database"))
    invalid(lambda plan: plan["report_fact_ids"].append(plan["report_fact_ids"][0]))
    invalid(lambda plan: plan["report_fact_ids"].append("missing-fact"))
    invalid(lambda plan: plan["report_fact_ids"].append(7))
    invalid(lambda plan: plan.update(digest="bad"), resign=False)
    assert report.browser_job_results(engine.jobs(job_id=job_id))[0]["delivery_status"] == "pending"


def test_selected_carry_is_authenticated_and_damage_is_rejected(opening_book):
    opening_cases._midyear_report_supplement_scenario(opening_book)
    engine = opening_book[0]
    report = Reports(engine)
    options = opening_cases.Dashboard(engine).quarterly_report(2026, 3)["carry_forward"]["options"]
    chosen = options[0]["fact_id"]
    _, job_id = _queue(report, year=2026, quarter=3, carry=chosen)
    item = report.browser_job_results(engine.jobs(job_id=job_id))[0]
    assert item["report_source"]["carry_forward_fact_id"] == chosen
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            "SELECT digest FROM fact_revision WHERE id=?", (chosen,)
        ).fetchone()[0]
    _damage_digest(engine, chosen, b"\0" * len(original))
    damaged = report.browser_job_results(engine.jobs(job_id=job_id))[0]
    assert damaged["delivery_status"] == "invalid" and not damaged["download_available"]
    _damage_digest(engine, chosen, original)
    restored = report.browser_job_results(engine.jobs(job_id=job_id))[0]
    assert restored["report_source"]["carry_forward_fact_id"] == chosen


@pytest.mark.parametrize("case", ["carry_kind", "carry_typed", "other_as_carry"])
def test_browser_job_rejects_inconsistent_carry_kind_and_typed_heads(opening_book, case):
    opening_cases._midyear_report_supplement_scenario(opening_book)
    engine = opening_book[0]
    report = Reports(engine)
    options = opening_cases.Dashboard(engine).quarterly_report(2026, 3)[
        "carry_forward"
    ]["options"]
    chosen = options[0]["fact_id"]
    plan, job_id = _queue(report, year=2026, quarter=3, carry=chosen)
    assert (
        report.browser_job_results(engine.jobs(job_id=job_id))[0]["report_source"][
            "carry_forward_fact_id"
        ]
        == chosen
    )
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
    item = report.browser_job_results(engine.jobs(job_id=job_id))[0]
    assert item["delivery_status"] == "invalid"
    assert not item["download_available"] and "report_source" not in item


def test_unrelated_source_damage_belongs_to_full_verifier_not_job_title(book):
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
    item = report.browser_job_results(engine.jobs(job_id=job_id))[0]
    assert item["delivery_status"] == "pending"
    with pytest.raises(KernelError) as error:
        Maintenance(engine).verify_integrity()
    assert error.value.code == "content_integrity_failed"
    _damage_digest(engine, non_carry_id, original)


def test_real_browser_projection_batches_multiple_plans_and_unrelated_sources(
    book, monkeypatch
):
    report = report_cases.scenario(book)
    engine = book[0]
    subjects = []
    for number in range(24):
        subject = f"adopted-cost-{number}"
        book[1](
            "expense",
            subject,
            {
                "period": "2026-02",
                "counterparty_id": "supplier",
                "amount_fen": 101 + number,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
        )
        subjects.append(subject)
    book[2](*subjects)
    for number, subject in enumerate(subjects):
        report_cases.classify(engine, book[1], book[2], subject=subject, amount=101 + number)
    report_cases.close_quarter(book)
    plan, first = _queue(report, request_id="browser-job-one")
    _, second = _queue(report, request_id="browser-job-two")
    assert len(plan["report_fact_ids"]) >= 24
    native = engine.jobs(limit=20)
    assert {row["id"] for row in native} == {first, second}

    def observed():
        stats = {"connections": 0, "selects": 0, "vm": 0}
        original = engine.store.connection

        @contextmanager
        def traced(*args, **kwargs):
            with original(*args, **kwargs) as connection:
                stats["connections"] += 1

                def sql(statement):
                    if statement.startswith("SELECT"):
                        stats["selects"] += 1

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
            rows = report.browser_job_results(native)
        finally:
            monkeypatch.setattr(engine.store, "connection", original)
        return rows, stats

    # A later, unrelated open-period profile must not enter either frozen plan.
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

    def forbidden(*_args, **_kwargs):
        raise AssertionError("adopted non-carry source body was decoded for a job title")

    monkeypatch.setattr(Store, "fact", forbidden)
    projected, work = observed()
    assert len(projected) == 2
    assert all(row["report_source"]["carry_forward_fact_id"] is None for row in projected)
    assert work["connections"] == 1
    assert work["selects"] <= 8, work
    assert work["vm"] < 300 * len(plan["report_fact_ids"]), work
