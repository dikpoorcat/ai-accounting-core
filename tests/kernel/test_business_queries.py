"""Stable business identity, selected accounting and read-only status contracts."""

import hashlib
import json
from functools import partial
from pathlib import Path
from typing import ClassVar

import pytest
from test_deletion_boundaries import book as domain_book_fixture
from test_deletion_boundaries import prepare_payment
from test_engine import Charge, Source, close, evidence, publish, save
from test_engine import engine as engine_fixture

import ai_accounting.kernel.business_queries as business_query_module
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import (
    BalanceEffect,
    Context,
    Fact,
    KernelError,
    Line,
    Outcome,
    Read,
    Registry,
)
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.read_indexes import sync_close, sync_job
from ai_accounting.kernel.service import LocalService, default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth
from ai_accounting.kernel.workflow import Workflow

engine = engine_fixture
domain_book = domain_book_fixture


class ExactReference(Fact):
    kind: ClassVar[str] = "test_exact_reference"
    old_id: str

    def reads(self):
        return (
            Read("calculation", "test_charge", "#" + self.old_id),
            Read("calculation", "test_charge", str(self.period)),
        )


def calculate_reference(version, context: Context):
    old = context.calculations("test_charge", "#" + version.fact.old_id)
    current = context.calculations("test_charge", str(version.fact.period))
    return Outcome((), {"sources": [item.id for item in (*old, *current)]})


def calculate_state_charge(version, _context: Context, *, opening=False):
    obligation = {
        "name": "primary",
        "key": f"test_charge:{version.subject_id}:primary",
        "amount_fen": version.fact.amount,
        "account": "2202",
        "normal": "credit",
        "category": "payable",
        "counterparty_id": "supplier",
        "cashflow": "operating",
    }
    lines = (
        ()
        if version.fact.suppress_posting
        else (
            Line("5602", debit=version.fact.amount),
            Line("2202", credit=version.fact.amount),
        )
    )
    return Outcome(
        lines,
        {"amount": version.fact.amount, "obligations": [obligation]},
        (BalanceEffect(obligation["key"], version.fact.amount, "payable"),),
        opening_lines=(
            (Line("1601", debit=version.fact.amount), Line("2202", credit=version.fact.amount))
            if opening
            else ()
        ),
        opening=opening,
    )


@pytest.fixture
def state_review_engine(tmp_path, request):
    registry = Registry()
    registry.register(Source)
    registry.register(
        Charge, partial(calculate_state_charge, opening=getattr(request, "param", False))
    )
    registry.register(ExactReference, calculate_reference)
    return Engine(
        Store.create(
            tmp_path / "state-review.sqlite",
            registry,
            "company-a",
            "91310000123456789A",
            "db-a",
        )
    )


def test_latest_fact_and_unpublished_state_are_separate(engine):
    saved = save(engine)

    result = BusinessQueries(engine).business_status("charge", "2026-01", as_of="2026-02-01")

    assert result["latest_fact"]["id"] == saved["fact_id"]
    assert result["latest_fact"]["knowledge"] == "current_knowledge"
    assert result["current_publication"] is None
    assert result["review"]["status"] == "unpublished"
    assert result["review"]["latest_matches_publication"] is False
    assert result["review"]["pending_causes"] == [{"cause_fact_id": saved["fact_id"]}]
    assert result["review"]["dispositions"] == []
    assert result["selected_accounting"]["period_events"] == []
    assert result["settlements"]["status"] == "not_established"
    assert result["settlements"]["obligations"] == []
    assert result["selected_accounting"]["through_period"]["status"] == "not_established"
    assert result["external"]["status"] == "unestablished"


def test_published_no_entry_result_is_not_unpublished(engine):
    save(engine, amount=100)
    # The test calculator has an explicit no-journal result branch.
    fact = engine.store.current_fact
    with engine.store.connection(read_only=True) as connection:
        version = fact(connection, "charge")
    engine.amend_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": version.fact.amount, "suppress_posting": True},
        evidence=version.evidence,
        expected_revision=1,
        request_id="suppress",
        recording_error_confirmed=True,
    )
    _, published = publish(engine, request="publish-no-entry")

    result = BusinessQueries(engine).business_status("charge", "2026-01", as_of="2026-02-01")

    assert result["review"]["status"] == "current"
    assert result["current_publication"]["publication"]["has_journal_lines"] is False
    assert (
        result["current_publication"]["calculation"]["id"]
        == published["results"][0]["calculation_id"]
    )
    assert result["selected_accounting"]["period_events"][0]["event_type"] == "state_result"
    assert result["selected_accounting"]["period_events"][0]["vouchers"] == []


def test_no_impact_review_reuses_one_precise_voucher_event(engine):
    save(engine)
    _, original = publish(engine)
    revised = save(engine, revision=1, request="reviewed-fact")
    preview, reviewed = publish(engine, request="review-no-impact")
    assert preview["results"][0]["impact"] == "review_no_impact"

    result = BusinessQueries(engine).business_status("charge", "2026-01", as_of="2026-02-01")
    current_id = reviewed["results"][0]["calculation_id"]
    event = result["selected_accounting"]["period_events"][0]

    assert result["latest_fact"]["id"] == revised["fact_id"]
    assert result["current_publication"]["calculation"]["id"] == current_id
    assert event["calculation_id"] == current_id
    assert event["voucher_calculation_id"] == original["results"][0]["calculation_id"]
    assert len(result["selected_accounting"]["period_events"]) == 1
    assert result["review"]["status"] == "current"
    assert result["trace_targets"] == [
        {
            "calculation_id": current_id,
            "voucher_version_id": event["voucher_version_id"],
        }
    ]


def test_closed_ambiguous_no_entry_states_are_trace_only(state_review_engine):
    proof = evidence(state_review_engine)
    first_fact = state_review_engine.save_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(proof,),
        expected_revision=0,
        request_id="first-fact",
    )
    _, first = publish(state_review_engine, request="first-publication")
    state_review_engine.amend_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(proof,),
        expected_revision=1,
        request_id="second-fact",
        recording_error_confirmed=True,
    )
    second_preview, second = publish(state_review_engine, request="second-publication")
    assert second_preview["results"][0]["impact"] == "review_no_impact"
    state_review_engine.save_fact(
        "test_exact_reference",
        "reference",
        {
            "period": "2026-01",
            "old_id": first["results"][0]["calculation_id"],
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="reference-fact",
    )
    publish(state_review_engine, subjects=["reference"], request="reference-publication")
    close(state_review_engine, "2026-01")

    result = BusinessQueries(state_review_engine).business_status(
        "charge", "2026-01", as_of="2026-02-01"
    )
    accounting = result["selected_accounting"]["through_period"]

    assert first_fact["fact_id"] != result["latest_fact"]["id"]
    assert accounting["state_results"] == []
    assert accounting["status"] == "unestablished"
    selection = accounting["unestablished_state_selections"][0]
    assert selection["status"] == "unestablished"
    assert {item["calculation_id"] for item in selection["candidates"]} == {
        first["results"][0]["calculation_id"],
        second["results"][0]["calculation_id"],
    }
    assert result["selected_accounting"]["period_events"] == []
    assert {item["calculation_id"] for item in result["trace_targets"]} == {
        item["calculation_id"] for item in selection["candidates"]
    }
    assert result["settlements"]["status"] == "not_established"
    assert result["settlements"]["obligations"] == []

    readiness = BusinessQueries(state_review_engine).period_readiness("2026-01", as_of="2026-02-01")
    current = readiness["current_followups"]["settlements"]
    assert current["status"] == "established"
    assert [
        item["calculation_id"] for item in current["business"] if item["subject_id"] == "charge"
    ] == [second["results"][0]["calculation_id"]]
    assert len(current["obligations"]) == 1
    assert current["obligations"][0]["source_amount_fen"] == 100


@pytest.mark.parametrize(
    "move_before_close", [True, False], ids=["old_dependency", "root_and_dependency"]
)
def test_closed_single_no_entry_dependency_cannot_prove_adoption(
    state_review_engine, move_before_close
):
    engine = state_review_engine
    proof = evidence(engine)
    engine.save_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(proof,),
        expected_revision=0,
        request_id="state-in-january",
    )
    _, first = publish(engine, request="publish-january-state")
    first_id = first["results"][0]["calculation_id"]
    if move_before_close:
        engine.amend_fact(
            "test_charge",
            "charge",
            {"period": "2026-02", "amount": 100, "suppress_posting": True},
            evidence=(proof,),
            expected_revision=1,
            request_id="move-state-to-february",
            recording_error_confirmed=True,
        )
        publish(engine, request="publish-february-state")
    engine.save_fact(
        "test_exact_reference",
        "reference",
        {"period": "2026-01", "old_id": first_id},
        evidence=(proof,),
        expected_revision=0,
        request_id="reference-old-state",
    )
    _, reference = publish(engine, subjects=["reference"], request="publish-old-reference")
    reference_id = reference["results"][0]["calculation_id"]
    close(engine, "2026-01")
    with engine.store.connection(read_only=True) as connection:
        manifest = json.loads(
            connection.execute(
                "SELECT manifest FROM period_close WHERE period=?", (YearMonth("2026-01").ordinal,)
            ).fetchone()[0]
        )
        assert set(manifest["calculations"]) == {first_id, reference_id}

    result = BusinessQueries(engine).business_status("charge", "2026-01", as_of="2026-03-01")
    through = result["selected_accounting"]["through_period"]
    assert result["current_publication"]["calculation"]["posting_period"] == (
        "2026-02" if move_before_close else "2026-01"
    )
    assert through["status"] == "unestablished"
    assert through["state_results"] == result["selected_accounting"]["period_events"] == []
    selection = through["unestablished_state_selections"][0]
    assert selection["reason"] == "manifest_state_adoption_not_proven"
    assert [item["calculation_id"] for item in selection["candidates"]] == [first_id]
    assert selection["candidates"][0]["trace_only"] is True
    assert result["trace_targets"] == [
        {
            "calculation_id": first_id,
            "voucher_version_id": None,
            "selection_status": "unestablished",
        }
    ]
    assert result["settlements"]["status"] == "not_established"
    assert result["settlements"]["obligations"] == []


@pytest.mark.parametrize(
    "state_review_engine", [False, True], indirect=True, ids=["state", "opening"]
)
def test_closed_independent_no_entry_root_remains_established(state_review_engine):
    engine = state_review_engine
    proof = evidence(engine)
    engine.save_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(proof,),
        expected_revision=0,
        request_id="independent-state",
    )
    _, published = publish(engine, request="publish-independent-state")
    calculation_id = published["results"][0]["calculation_id"]
    close(engine, "2026-01")

    result = BusinessQueries(engine).business_status("charge", "2026-01", as_of="2026-02-01")
    through = result["selected_accounting"]["through_period"]
    assert through["status"] == "established"
    assert through["unestablished_state_selections"] == []
    assert len(through["state_results"]) == 1
    state = through["state_results"][0]
    assert state["calculation_id"] == calculation_id
    assert state["selection_proof"] == {"basis": "manifest_lineage_root"}
    assert state["opening"] == result["current_publication"]["calculation"]["outcome"]["opening"]
    assert result["selected_accounting"]["period_events"] == [state]
    assert len(result["settlements"]["obligations"]) == 1
    assert result["settlements"]["obligations"][0]["source_amount_fen"] == 100


def test_old_no_entry_dependency_does_not_duplicate_new_voucher_state(state_review_engine):
    proof = evidence(state_review_engine)
    state_review_engine.save_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(proof,),
        expected_revision=0,
        request_id="old-state-fact",
    )
    _, old = publish(state_review_engine, request="old-state-publication")
    state_review_engine.amend_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": False},
        evidence=(proof,),
        expected_revision=1,
        request_id="new-voucher-fact",
        recording_error_confirmed=True,
    )
    _, current = publish(state_review_engine, request="new-voucher-publication")
    state_review_engine.save_fact(
        "test_exact_reference",
        "reference",
        {
            "period": "2026-01",
            "old_id": old["results"][0]["calculation_id"],
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="mixed-reference-fact",
    )
    publish(state_review_engine, subjects=["reference"], request="mixed-reference-publication")
    close(state_review_engine, "2026-01")

    result = BusinessQueries(state_review_engine).business_status(
        "charge", "2026-01", as_of="2026-02-01"
    )
    accounting = result["selected_accounting"]

    assert [item["calculation_id"] for item in accounting["period_events"]] == [
        current["results"][0]["calculation_id"]
    ]
    assert accounting["through_period"]["state_results"] == []
    assert accounting["through_period"]["status"] == "partially_established"
    assert len(accounting["through_period"]["unestablished_state_selections"]) == 1


def test_period_cutoff_keeps_frozen_original_and_later_correction_events(engine):
    save(engine)
    _, original = publish(engine)
    close(engine, "2026-01")
    save(engine, amount=150, revision=1, request="changed-after-close")
    _, corrected = publish(engine, request="correct-in-march", correction_period="2026-03")

    january = BusinessQueries(engine).business_status("charge", "2026-01", as_of="2026-03-31")
    march = BusinessQueries(engine).business_status("charge", "2026-03", as_of="2026-03-31")

    assert january["latest_fact"]["id"] == march["latest_fact"]["id"]
    assert (
        january["current_publication"]["calculation"]["id"]
        == corrected["results"][0]["calculation_id"]
    )
    assert [item["calculation_id"] for item in january["selected_accounting"]["period_events"]] == [
        original["results"][0]["calculation_id"]
    ]
    assert [item["role"] for item in march["selected_accounting"]["period_events"]] == [
        "reversal",
        "replacement",
    ]
    through_roles = [
        item["role"] for item in march["selected_accounting"]["through_period"]["voucher_events"]
    ]
    assert through_roles == [
        "original",
        "reversal",
        "replacement",
    ]


@pytest.mark.parametrize("close_correction", [False, True], ids=["open", "frozen"])
def test_closed_correction_to_no_entry_has_original_reversal_and_new_state(
    engine, close_correction
):
    save(engine)
    _, original = publish(engine)
    close(engine, "2026-01")
    with engine.store.connection(read_only=True) as connection:
        evidence = engine.store.current_fact(connection, "charge").evidence
    engine.amend_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=evidence,
        expected_revision=1,
        request_id="no-entry-correction-fact",
        recording_error_confirmed=True,
    )
    _, corrected = publish(
        engine,
        request="no-entry-correction",
        correction_period="2026-03",
    )
    if close_correction:
        periods = Periods(engine)
        for category in MATERIAL_CATEGORIES:
            periods.inventory(
                "2026-03",
                category,
                evidence=[],
                expected=0,
                no_business=True,
                confirmation_evidence=evidence[0],
                request_id="march-inventory-" + category,
            )
        preview = periods.preview_close("2026-03", owner_confirmation=evidence[0])
        periods.close(
            "2026-03",
            owner_confirmation=evidence[0],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="close-march",
        )

    result = BusinessQueries(engine).business_status("charge", "2026-03", as_of="2026-03-31")
    events = result["selected_accounting"]["period_events"]
    reversal = next(item for item in events if item["event_type"] == "voucher")
    state = next(item for item in events if item["event_type"] == "state_result")

    assert reversal["role"] == "reversal"
    assert reversal["calculation_id"] == original["results"][0]["calculation_id"]
    assert reversal["voucher_calculation_id"] == corrected["results"][0]["calculation_id"]
    assert state["calculation_id"] == corrected["results"][0]["calculation_id"]
    assert state["selection_proof"]["basis"] == (
        "manifest_voucher_root" if close_correction else "calculation_current"
    )


def test_payment_job_uses_only_frozen_direct_sources_and_does_not_read_files(
    domain_book, monkeypatch
):
    engine, save_fact, publish_businesses, *_ = domain_book
    save_fact(
        "expense",
        "expense",
        {
            "period": "2026-01",
            "amount_fen": 100,
            "counterparty_id": "supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    published = publish_businesses("expense")
    source = {
        "subject_id": "expense",
        "calculation_id": published["results"][0]["calculation_id"],
        "obligation": "expense:expense:primary",
    }
    plan = {"period": "2026-01", "rows": [{"sources": [source]}]}
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO jobs(id,kind,payload,status) VALUES(?,?,?,'pending')",
            ("job", "payment_export", json.dumps({"plan": plan})),
        )
        sync_job(connection, "job")
        connection.commit()

    def unexpected_file_read(*_args, **_kwargs):
        raise AssertionError("business status must not inspect generated files")

    monkeypatch.setattr(Path, "read_bytes", unexpected_file_read)
    result = BusinessQueries(engine).business_status("expense", "2026-01", as_of="2026-02-01")

    assert result["file_jobs"] == [
        {
            "job_id": "job",
            "kind": "payment_export",
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "result": None,
            "association": "direct_source",
            "references": [source],
            "period": "2026-01",
            "verified_when_succeeded": False,
            "current_file_availability": "not_checked",
        }
    ]


def test_settlement_balance_uses_exact_payment_relation_at_cutoff(domain_book):
    engine, _save, publish_businesses, *_ = domain_book
    prepare_payment(domain_book)
    publish_businesses("payment")

    result = BusinessQueries(engine).business_status("expense", "2026-01", as_of="2026-02-01")

    primary = next(
        item for item in result["settlements"]["obligations"] if item["name"] == "primary"
    )
    assert primary["source_amount_fen"] == 100
    assert primary["paid_fen"] == 100
    assert primary["remaining_fen"] == 0
    assert result["settlements"]["movements"][0]["source_business"] == {
        "kind": "expense",
        "subject_id": "expense",
    }


def test_unresolved_settlement_does_not_claim_a_paid_balance(domain_book, monkeypatch):
    engine, _save, publish_businesses, *_ = domain_book
    prepare_payment(domain_book)
    publish_businesses("payment")
    actual_resolver = business_query_module.resolve_calculation_relations

    def unresolved_payment(calculation, **loaders):
        resolved = actual_resolver(calculation, **loaders)
        if calculation["kind"] == "cash_payment":
            return {
                **resolved,
                "settlements": [
                    {**item, "state": "unresolved"} for item in resolved["settlements"]
                ],
            }
        return resolved

    monkeypatch.setattr(
        business_query_module,
        "resolve_calculation_relations",
        unresolved_payment,
    )

    result = BusinessQueries(engine).business_status("expense", "2026-01", as_of="2026-02-01")
    obligation = result["settlements"]["obligations"][0]

    assert result["settlements"]["status"] == "partially_established"
    assert obligation["paid_fen"] is None
    assert obligation["remaining_fen"] is None


def test_repeated_manifest_reference_does_not_repeat_voucher_effect(engine):
    save(engine)
    publish(engine)
    close(engine, "2026-01")
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        january = json.loads(
            connection.execute(
                "SELECT manifest FROM period_close WHERE period=?",
                (YearMonth("2026-01").ordinal,),
            ).fetchone()[0]
        )
        duplicate = {**january, "period": "2026-02"}
        connection.execute(
            "INSERT INTO period_close(period,manifest,digest) VALUES(?,?,?)",
            (YearMonth("2026-02").ordinal, json.dumps(duplicate),
             hashlib.sha256(json.dumps(duplicate).encode()).digest()),
        )
        sync_close(connection, YearMonth("2026-02").ordinal)
        connection.commit()

    result = BusinessQueries(engine).business_status("charge", "2026-02", as_of="2026-03-01")

    assert len(result["selected_accounting"]["through_period"]["voucher_events"]) == 1


def test_withdrawn_open_business_keeps_history_but_clears_current_accounting(domain_book):
    engine, save_fact, publish_businesses, _close, withdraw, _proof = domain_book
    saved = save_fact(
        "expense",
        "expense",
        {
            "period": "2026-01",
            "amount_fen": 100,
            "counterparty_id": "supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish_businesses("expense")
    withdraw("expense")

    result = BusinessQueries(engine).business_status("expense", "2026-01", as_of="2026-02-01")

    assert result["latest_fact"]["id"] == saved["fact_id"]
    assert result["latest_fact"]["deleted"] is True
    assert result["review"]["status"] == "deleted"
    assert result["current_publication"] is None
    assert result["selected_accounting"]["through_period"]["status"] == "not_established"


def test_tax_and_report_jobs_use_only_database_plan_references(engine):
    saved = save(engine)
    publish(engine)
    plans = (
        ("tax-direct", "tax_import", {"source_versions": [saved["fact_id"]]}),
        ("tax-unrelated", "tax_import", {"source_versions": ["not-a-source"]}),
        (
            "report-quarter",
            "report_export",
            {
                "period": {
                    "year": 2026,
                    "quarter": 1,
                    "quarter_start": "2026-01-01",
                    "quarter_end": "2026-03-31",
                },
                "report_fact_ids": [],
                "source_closes": [],
            },
        ),
        (
            "report-direct",
            "report_export",
            {"report_fact_ids": [saved["fact_id"]], "source_closes": []},
        ),
        (
            "report-unrelated-bad",
            "report_export",
            {"report_fact_ids": [{}]},
        ),
    )
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        connection.executemany(
            "INSERT INTO jobs(id,kind,payload,status) VALUES(?,?,?,'pending')",
            [(ident, kind, json.dumps({"plan": plan})) for ident, kind, plan in plans],
        )
        for ident, _kind, _plan in plans:
            sync_job(connection, ident)
        connection.commit()

    result = BusinessQueries(engine).business_status("charge", "2026-01", as_of="2026-02-01")
    jobs = {item["job_id"]: item for item in result["file_jobs"]}

    assert jobs["tax-direct"]["association"] == "direct_source"
    assert "tax-unrelated" not in jobs
    assert jobs["report-quarter"]["association"] == "period_scope"
    assert "contract_issues" not in jobs["report-quarter"]
    assert jobs["report-direct"]["association"] == "direct_source"
    assert "report-unrelated-bad" not in jobs


def test_malformed_job_collections_are_isolated_to_the_associated_job(engine):
    plans = (
        ("payment-null-rows", "payment_export", {"period": "2026-01", "rows": None}),
        (
            "payment-null-sources",
            "payment_export",
            {"period": "2026-01", "rows": [{"sources": None}]},
        ),
        (
            "payment-missing-sources",
            "payment_export",
            {"period": "2026-01", "rows": [{}]},
        ),
        (
            "payment-bad-elements",
            "payment_export",
            {"period": "2026-01", "rows": [{"sources": [None, {"subject_id": 1}]}]},
        ),
        ("tax-null-sources", "tax_import", {"period": "2026-01", "source_versions": None}),
        ("tax-bad-elements", "tax_import", {"period": "2026-01", "source_versions": [{}]}),
        (
            "report-null-fields",
            "report_export",
            {
                "period": {
                    "quarter_start": "2026-01-01",
                    "quarter_end": "2026-03-31",
                },
                "report_fact_ids": [{}],
                "source_closes": [None],
            },
        ),
    )
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        connection.executemany(
            "INSERT INTO jobs(id,kind,payload,status,result) VALUES(?,?,?,'succeeded','not-json')",
            [(ident, kind, json.dumps({"plan": plan})) for ident, kind, plan in plans],
        )
        for ident, _kind, _plan in plans:
            sync_job(connection, ident)
        connection.commit()

    result = BusinessQueries(engine).period_readiness("2026-01", as_of="2026-02-01")
    jobs = {item["job_id"]: item for item in result["current_followups"]["file_jobs"]}

    assert set(jobs) == {ident for ident, _kind, _plan in plans}
    assert all(item["association"] == "period_scope" for item in jobs.values())
    assert all(item["result"] is None and item["result_issue"] for item in jobs.values())
    assert all(item["contract_issues"] for item in jobs.values())
    assert all(item["verified_when_succeeded"] is False for item in jobs.values())


def test_as_of_changes_no_selected_accounting_or_settlement_amount(domain_book):
    engine, _save, publish_businesses, *_ = domain_book
    prepare_payment(domain_book)
    publish_businesses("payment")

    earlier = BusinessQueries(engine).business_status("expense", "2026-01", as_of="2026-01-15")
    later = BusinessQueries(engine).business_status("expense", "2026-01", as_of="2026-12-31")

    assert earlier["selected_accounting"] == later["selected_accounting"]
    assert earlier["settlements"] == later["settlements"]


def test_external_obligation_is_related_before_any_completion(domain_book):
    engine, save_fact, publish_businesses, *_ = domain_book
    save_fact(
        "expense",
        "expense",
        {
            "period": "2026-01",
            "amount_fen": 100,
            "counterparty_id": "supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish_businesses("expense")
    save_fact(
        "external_obligation",
        "quarter-obligation",
        {
            "period": "2026-01",
            "obligation_kind": "quarterly_tax_and_reports",
            "start_period": "2026-01",
            "end_period": "2026-01",
            "due_date": "2026-02-20",
            "applicability_confirmed": True,
            "applicability": "required",
        },
    )

    result = BusinessQueries(engine).business_status("expense", "2026-01", as_of="2026-02-25")

    assert result["external"]["status"] == "followup_required"
    assert [item["id"] for item in result["external"]["obligations"]] == ["quarter-obligation"]
    assert result["external"]["completions"] == []


def test_external_completion_is_not_an_accounting_state_event(domain_book):
    engine, save_fact, publish_businesses, close_period, *_ = domain_book
    save_fact(
        "expense",
        "expense",
        {
            "period": "2026-01",
            "amount_fen": 100,
            "counterparty_id": "supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish_businesses("expense")
    saved_obligation = save_fact(
        "external_obligation",
        "quarter-obligation",
        {
            "period": "2026-01",
            "obligation_kind": "quarterly_tax_and_reports",
            "start_period": "2026-01",
            "end_period": "2026-01",
            "due_date": "2026-02-20",
            "applicability_confirmed": True,
            "applicability": "required",
        },
    )
    close_period("2026-01")
    basis = Workflow(engine).obligation_basis("quarter-obligation")
    save_fact(
        "external_completion",
        "completion",
        {
            **basis,
            "period": "2026-02",
            "obligation_fact_id": saved_obligation["fact_id"],
            "completion_status": "confirmed_complete",
            "date_status": "known",
            "completion_date": "2026-02-10",
        },
    )
    publish_businesses("completion")

    result = BusinessQueries(engine).business_status("completion", "2026-02", as_of="2026-02-28")

    assert result["current_publication"]["calculation"]["kind"] == "external_completion"
    assert result["selected_accounting"]["period_events"] == []
    assert result["selected_accounting"]["through_period"]["status"] == "not_established"


def test_commands_are_discoverable_and_period_is_required():
    models = command_models(default_registry())

    assert {"business_status", "period_readiness"} <= models.keys()
    valid = validate_command(
        models,
        "business_status",
        {"company_id": "company", "subject_id": "business", "period": "2026-01"},
    )
    assert valid["as_of"] is None
    with pytest.raises(KernelError) as error:
        validate_command(
            models,
            "business_status",
            {"company_id": "company", "subject_id": "business"},
        )
    assert error.value.code == "invalid_command"


def test_period_readiness_uses_exact_close_without_erasing_current_followups(engine):
    save(engine)
    publish(engine)
    close(engine, "2026-01")

    result = BusinessQueries(engine).period_readiness("2026-01", as_of="2026-02-01")

    assert result["closure"]["state"] == "exact_close"
    assert result["frozen_readiness"]["status"] == "ready"
    assert result["frozen_readiness"]["readiness"]["status"] == "recorded"
    assert result["readiness"] is None
    assert result["current_followups"]["knowledge"] == "current_knowledge"
    assert result["current_followups"]["affects_frozen_readiness"] is False


def test_period_readiness_marks_legacy_manifest_fields_not_recorded(engine):
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO period_close(period,manifest,digest) VALUES(?,?,?)",
            (YearMonth("2026-01").ordinal, json.dumps({"period": "2026-01"}),
             hashlib.sha256(json.dumps({"period": "2026-01"}).encode()).digest()),
        )
        sync_close(connection, YearMonth("2026-01").ordinal)
        connection.commit()

    result = BusinessQueries(engine).period_readiness("2026-01", as_of="2026-02-01")

    assert result["closure"]["state"] == "exact_close"
    for field in ("readiness", "inventories", "material_coverage", "previous_close_digest"):
        assert result["frozen_readiness"][field] == {"status": "not_recorded"}


def test_period_readiness_does_not_borrow_later_close_manifest(engine):
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO period_close(period,manifest,digest) VALUES(?,?,?)",
            (YearMonth("2026-02").ordinal, json.dumps({"period": "2026-02"}),
             hashlib.sha256(json.dumps({"period": "2026-02"}).encode()).digest()),
        )
        sync_close(connection, YearMonth("2026-02").ordinal)
        connection.commit()

    result = BusinessQueries(engine).period_readiness("2026-01", as_of="2026-02-01")

    assert result["closure"]["state"] == "sealed_by_later_close"
    assert result["closure"]["sealing_boundary"] == "2026-02"
    assert result["frozen_readiness"] == {
        "status": "unavailable",
        "reason": "no_exact_period_manifest",
    }
    assert result["readiness"] is None


def test_closed_period_current_followups_include_only_related_later_settlement(domain_book):
    engine, save_fact, publish_businesses, close_period, *_ = domain_book
    save_fact(
        "expense",
        "expense",
        {
            "period": "2026-01",
            "amount_fen": 100,
            "counterparty_id": "supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish_businesses("expense")
    close_period("2026-01")
    save_fact(
        "cash_funding",
        "funding",
        {
            "period": "2026-02",
            "actual_date": "2026-02-01",
            "cash_account_id": "cash",
            "owner_id": "owner",
            "amount_fen": 100,
            "funding_kind": "capital",
        },
    )
    publish_businesses("funding")
    save_fact(
        "expense",
        "unrelated-expense",
        {
            "period": "2026-02",
            "amount_fen": 25,
            "counterparty_id": "other-supplier",
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    publish_businesses("unrelated-expense")
    save_fact(
        "cash_payment",
        "payment",
        {
            "period": "2026-02",
            "actual_date": "2026-02-10",
            "cash_account_id": "cash",
            "counterparty_id": "supplier",
            "direction": "outflow",
            "amount_fen": 100,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": "expense",
                    "obligation": "primary",
                    "amount_fen": 100,
                }
            ],
        },
    )
    publish_businesses("payment")

    result = BusinessQueries(engine).period_readiness("2026-01", as_of="2026-02-01")
    settlements = result["current_followups"]["settlements"]
    obligations = settlements["obligations"]

    assert result["closure"]["state"] == "exact_close"
    assert settlements["scope_period"] == "2026-01"
    assert settlements["current_cutoff_period"] == "2026-02"
    assert settlements["cutoff_semantics"] == ("current_published_relations_independent_of_as_of")
    assert len(obligations) == 1
    assert obligations[0]["source_amount_fen"] == 100
    assert obligations[0]["paid_fen"] == 100
    assert obligations[0]["remaining_fen"] == 0
    assert {item["subject_id"] for item in settlements["business"]} == {
        "expense",
        "payment",
    }


def test_service_routes_read_only_business_status_with_company_isolation(tmp_path):
    service = LocalService(tmp_path)
    service.security.provision("owner", "test-password-123")
    token = service.security.login("owner", "test-password-123").session_token
    dispatch = partial(service.dispatch, session_token=token)
    first = dispatch(
        "create_company",
        {"taxpayer_id": "91310000123456789A", "name": "甲公司"},
    )["id"]
    second = dispatch(
        "create_company",
        {"taxpayer_id": "91310000123456789B", "name": "乙公司"},
    )["id"]
    item = records(service.engine(first))[0]
    dispatch("save_fact", {**item, "company_id": first, "request_id": "save"})
    with service.engine(first).store.connection(read_only=True) as connection:
        audit_before = connection.execute("SELECT count(*) FROM audit").fetchone()[0]

    result = dispatch(
        "business_status",
        {"company_id": first, "subject_id": "expense-0", "period": "2026-01"},
    )

    assert result["identity"]["company_id"] == first
    with service.engine(first).store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM audit").fetchone()[0] == audit_before
    with pytest.raises(KernelError) as error:
        dispatch(
            "business_status",
            {"company_id": second, "subject_id": "expense-0", "period": "2026-01"},
        )
    assert error.value.code == "unknown_subject"


def records(engine):
    evidence = engine.register_evidence(b"business query", "text/plain", "query", request_id="e")
    return [
        {
            "kind": "expense",
            "subject_id": "expense-0",
            "data": {
                "period": "2026-01",
                "amount_fen": 1200,
                "counterparty_id": "supplier",
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            "evidence": [evidence["digest"]],
            "expected_revision": 0,
        }
    ]
