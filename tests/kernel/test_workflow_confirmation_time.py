"""Unknown filing dates use immutable knowledge time, never a fabricated business day."""

from contextlib import contextmanager

import pytest
from test_payroll import payroll
from test_workflow import (
    completion_from_basis,
    confirmation_clock,
    obligation,
    setup_company,
)

from ai_accounting.kernel import workflow
from ai_accounting.kernel.contracts import FactVersion
from ai_accounting.kernel.types import digest


def prepare(tmp_path, *, obligation_kind="individual_income_tax", count=1):
    company = setup_company(tmp_path)
    service = workflow.Workflow(company.engine)
    facts = []
    for index in range(count):
        subject = f"obligation-{index}"
        company.save(obligation(obligation_kind), subject)
        facts.append(
            completion_from_basis(
                service.obligation_basis(subject),
                period="2026-09",
                date_status="not_established",
                completion_date=None,
                no_reportable_activity_confirmed=True,
            )
        )
    return company, service, facts


def command(company, fact, subject="completion"):
    return {
        "kind": fact.kind,
        "subject_id": subject,
        "data": fact.model_dump(mode="json"),
        "evidence": (company.owner_confirmation,),
        "expected_revision": 0,
    }


def item(service, day):
    return service.query("2026-09", as_of=day)["obligations"][0]


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("actor", [False, True])
def test_same_month_unknown_completion_uses_single_or_batch_confirmation(
    tmp_path, monkeypatch, batch, actor
):
    company, service, facts = prepare(tmp_path)
    engine = company.engine
    if actor:
        engine.audit_actor = {"kind": "test", "id": "synthetic-owner"}
    confirmation_clock(monkeypatch, engine, "2026-09-11T10:00:00.000Z")
    entry = command(company, facts[0])
    if batch:
        engine.save_facts([entry], request_id=company.request())
    else:
        engine.save_fact(**entry, request_id=company.request())
    company.publish("completion")
    original = company.current("completion", "external_completion")
    before = item(service, "2026-09-10")
    assert before["status"] == "due"
    recorded = before["recorded_completions"][0]
    assert recorded["basis_current"] and not recorded["known_as_of"]
    assert not before["basis_review_required"]
    now = item(service, "2026-09-11")
    assert now["status"] == "completed"
    assert now["recorded_completions"][0] == {
        **recorded,
        "known_as_of": True,
        "completion_date": None,
        "confirmation_recorded_at": "2026-09-11T10:00:00.000Z",
    }
    assert company.current("completion", "external_completion") == original


def test_old_recording_month_cannot_leak_future_confirmation(tmp_path, monkeypatch):
    company, service, facts = prepare(tmp_path)
    confirmation_clock(monkeypatch, company.engine, "2026-09-11T10:00:00.000Z")
    company.save(facts[0].model_copy(update={"period": "2026-02"}), "completion")
    company.publish("completion")
    assert item(service, "2026-03-01")["status"] == "due"
    assert item(service, "2026-09-11")["status"] == "completed"


def test_unknown_completion_uses_fixed_china_day_boundary(tmp_path, monkeypatch):
    company, service, facts = prepare(tmp_path)
    confirmation_clock(monkeypatch, company.engine, "2026-09-11T16:30:00.000Z")
    company.save(facts[0], "completion")
    company.publish("completion")
    assert item(service, "2026-09-11")["status"] == "due"
    assert item(service, "2026-09-12")["status"] == "completed"


def test_known_business_day_is_not_replaced_by_later_confirmation_time(tmp_path, monkeypatch):
    company, service, facts = prepare(tmp_path)
    confirmation_clock(monkeypatch, company.engine, "2026-09-11T10:00:00.000Z")
    company.save(
        facts[0].model_copy(update={"date_status": "known", "completion_date": "2026-02-25"}),
        "completion",
    )
    company.publish("completion")
    assert item(service, "2026-02-24")["status"] == "due"
    assert item(service, "2026-02-25")["status"] == "completed"


def test_missing_confirmation_audit_keeps_unknown_day_unestablished(tmp_path):
    company, service, facts = prepare(tmp_path)
    fact = facts[0]
    # A retained source lacking the normal confirmation audit must not acquire a
    # fake date from its recording month or its later calculation publication.
    version = FactVersion("retained-fact", "completion", 1, fact, (company.owner_confirmation,))
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        company.engine.store.write_fact(connection, version, digest(fact.model_dump(mode="json")))
        connection.commit()
    company.publish("completion")
    result = item(service, "2099-01-01")
    assert result["status"] == "due"
    recorded = result["recorded_completions"][0]
    assert recorded["basis_current"]
    assert not recorded["known_as_of"]
    assert recorded["confirmation_recorded_at"] is None


def test_idempotent_replay_and_recalculation_keep_original_knowledge_time(tmp_path, monkeypatch):
    company, service, facts = prepare(tmp_path)
    confirmation_clock(monkeypatch, company.engine, "2026-09-11T10:00:00.000Z")
    entry = command(company, facts[0])
    request = company.request()
    saved = company.engine.save_fact(**entry, request_id=request)
    company.publish("completion")
    confirmation_clock(monkeypatch, company.engine, "2026-09-20T10:00:00.000Z")
    assert company.engine.save_fact(**entry, request_id=request) == saved
    company.publish("completion")
    company.save(obligation("annual_business_report"), "unrelated")
    assert item(service, "2026-09-11")["status"] == "completed"
    with company.engine.store.connection(read_only=True) as connection:
        assert len(workflow._completion_confirmation_times(connection, [saved["fact_id"]])) == 1


@pytest.mark.parametrize("actor", [False, True])
def test_recording_correction_uses_exact_new_fact_id_and_keeps_old_record_visible(
    tmp_path, monkeypatch, actor
):
    company, service, facts = prepare(tmp_path)
    engine = company.engine
    if actor:
        engine.audit_actor = {"kind": "test", "id": "synthetic-owner"}
    confirmation_clock(monkeypatch, engine, "2026-09-11T10:00:00.000Z")
    old = company.save(facts[0], "completion")
    company.publish("completion")
    confirmation_clock(monkeypatch, engine, "2026-09-20T10:00:00.000Z")
    corrected = facts[0].model_copy(update={"completion_status": "submitted"})
    saved = engine.amend_fact(
        **(command(company, corrected) | {"expected_revision": 1}),
        recording_error_confirmed=True,
        request_id=company.request(),
    )
    assert saved["fact_id"] != old["fact_id"]
    pending = item(service, "2026-09-11")
    assert pending["basis_review_required"]
    assert pending["recorded_completions"][0]["known_as_of"]
    assert not pending["recorded_completions"][0]["basis_current"]
    company.publish("completion")
    assert item(service, "2026-09-11")["status"] == "due"
    assert item(service, "2026-09-20")["status"] == "completed"
    assert item(service, "2026-09-20")["recorded_completions"][0]["completion_date"] is None


def test_changed_basis_keeps_actual_completion_and_requests_review(tmp_path, monkeypatch):
    company, service, facts = prepare(tmp_path)
    confirmation_clock(monkeypatch, company.engine, "2026-09-11T10:00:00.000Z")
    company.save(facts[0], "completion")
    company.publish("completion")
    company.save(payroll(), "new-unpublished-payroll")
    result = item(service, "2026-09-11")
    assert result["status"] == "due" and result["basis_review_required"]
    recorded = result["recorded_completions"][0]
    assert recorded["known_as_of"] and not recorded["basis_current"]
    assert recorded["status"] == "confirmed_complete"
    assert recorded["completion_date"] is None


def test_all_obligations_share_one_audit_read(tmp_path, monkeypatch):
    company, service, facts = prepare(tmp_path, count=3)
    confirmation_clock(monkeypatch, company.engine, "2026-09-11T10:00:00.000Z")
    company.engine.save_facts(
        [command(company, fact, f"completion-{index}") for index, fact in enumerate(facts)],
        request_id=company.request(),
    )
    company.publish(*(f"completion-{index}" for index in range(3)))
    queries = []
    connection_factory = company.engine.store.connection

    @contextmanager
    def traced_connection(*args, **kwargs):
        with connection_factory(*args, **kwargs) as connection:
            connection.set_trace_callback(queries.append)
            yield connection

    monkeypatch.setattr(company.engine.store, "connection", traced_connection)
    result = service.query("2026-09", as_of="2026-09-11")
    assert len(result["obligations"]) == 3
    assert all(value["status"] == "completed" for value in result["obligations"])
    audit_queries = [
        query for query in queries if "SELECT action,payload,created_at FROM audit" in query
    ]
    assert len(audit_queries) == 1


@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-09-11T10:00:00"])
def test_untrusted_missing_or_unzoned_metadata_cannot_establish_known_day(timestamp):
    assert not workflow._completion_known_as_of(None, timestamp, "2026-09-11")
