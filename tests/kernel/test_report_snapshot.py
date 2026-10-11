"""Full quarterly input changes and actual-file submission bindings."""

import copy
import hashlib
from types import SimpleNamespace

import pytest
from test_reports import book as report_book_fixture
from test_reports import close_quarter, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_snapshot import (
    BASIS_CONTRACT,
    _completion_binding,
    _prepare_one,
    report_dependency_reads,
    report_signature,
    review_snapshot_state,
    snapshot_basis,
)
from ai_accounting.kernel.reports import Reports, render_quarterly_template
from ai_accounting.kernel.types import YearMonth, canonical, digest
from ai_accounting.kernel.workflow import Workflow

report_book = report_book_fixture


def report_plan(engine, reads, *, source="open"):
    plan = Reports(engine)._report(2026, 1, source=source, reads=reads, connection=reads.connection)
    plan["report_basis"] = snapshot_basis(engine, reads, plan)
    plan["digest"] = digest(
        {key: value for key, value in plan.items() if key not in {"digest", "epochs"}}
    ).hex()
    return plan


def obligation():
    return SimpleNamespace(
        obligation_kind="quarterly_financial_report",
        start_period=YearMonth("2026-01"),
        end_period=YearMonth("2026-03"),
    )


def retain_export(engine, plan, suffix):
    workbook = render_quarterly_template(plan)
    checksum = hashlib.sha256(workbook).hexdigest()
    job_id = "report-" + suffix
    manifest = {"job_id": job_id, "report_digest": plan["digest"], "sha256": checksum, "plan": plan}
    with engine.store.connection() as connection:
        connection.execute(
            "INSERT INTO jobs(id,kind,payload,status,result) "
            "VALUES(?,'report_export',?,'succeeded',?)",
            (
                job_id,
                canonical({"plan": plan}),
                canonical(
                    {
                        "sha256": checksum,
                        "report_digest": plan["digest"],
                    }
                ),
            ),
        )
    manifest_digest = engine.register_evidence(
        canonical(manifest).encode(),
        "application/json",
        "manifest.json",
        request_id="manifest-" + suffix,
    )["digest"]
    workbook_digest = engine.register_evidence(
        workbook,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "report.xlsx",
        request_id="workbook-" + suffix,
    )["digest"]
    receipt_digest = engine.register_evidence(
        ("received " + suffix).encode(),
        "text/plain",
        "receipt.txt",
        request_id="receipt-" + suffix,
    )["digest"]
    return manifest_digest, workbook_digest, receipt_digest


def completion(evidence):
    return SimpleNamespace(
        id="completed-fact",
        subject_id="completed",
        evidence=evidence,
        fact=SimpleNamespace(
            obligation_id="quarter",
            obligation_fact_id="quarter-fact",
            obligation_kind="quarterly_financial_report",
            start_period=YearMonth("2026-01"),
            end_period=YearMonth("2026-03"),
            adopted_evidence_digests=evidence[:2],
        ),
    )


def review(evidence):
    return SimpleNamespace(
        id="review-fact",
        evidence=evidence,
        fact=SimpleNamespace(
            completion_id="completed",
            completion_fact_id="completed-fact",
            obligation_id="quarter",
            obligation_fact_id="quarter-fact",
        ),
    )


def attach_versions(monkeypatch, reads, completed):
    versions = {
        "completed-fact": completed,
        "quarter-fact": SimpleNamespace(fact=obligation()),
    }
    monkeypatch.setattr(reads, "fact_version", versions.__getitem__)


def test_actual_sources_and_signature_survive_the_final_close(report_book):
    engine = report_book[0]
    scenario(report_book)
    with QueryReads.snapshot(engine) as reads:
        before = report_plan(engine, reads)
    close_quarter(report_book)
    with QueryReads.snapshot(engine) as reads:
        after = report_plan(engine, reads, source="closed")
    assert before["source"] != after["source"]
    assert before["source_closes"] != after["source_closes"]
    assert before["report_basis"] == after["report_basis"]
    assert report_signature(before) == report_signature(after)
    assert {item["kind"] for item in before["report_basis"]["accounting_sources"]} >= {
        "cash_funding",
        "expense",
        "cash_payment",
    }


@pytest.mark.parametrize(
    "field",
    [
        "organization",
        "opening_source",
        "template",
        "rule",
        "report_basis",
        "statements",
    ],
)
def test_complete_content_or_input_changes_invalidate_signature(report_book, field):
    engine = report_book[0]
    scenario(report_book)
    with QueryReads.snapshot(engine) as reads:
        plan = report_plan(engine, reads)
    changed = copy.deepcopy(plan)
    changed[field] = {
        **(changed[field] or {}),
        "changed": "same displayed amounts can have different inputs",
    }
    assert report_signature(changed) != report_signature(plan)


def test_missing_complete_basis_cannot_be_treated_as_a_match():
    with pytest.raises(KernelError, match="完整内容"):
        report_signature({"statements": {}})
    with pytest.raises(KernelError):
        report_signature({"report_basis": {"contract": BASIS_CONTRACT}})


def test_dependencies_cover_history_and_management_and_exclude_external(report_book):
    registry = report_book[0].store.registry
    reads = report_dependency_reads(registry, "2026-09")
    assert all(read.before_period == YearMonth("2026-10") for read in reads)
    assert all(read.kind != "*" for read in reads)
    kinds = {read.kind for read in reads}
    assert {
        "opening_package",
        "report_profile",
        "report_classification",
        "report_income_tax_confirmation",
    } <= kinds
    assert (
        not {"external_completion", "external_basis_review", "payroll_disbursement_basis"} & kinds
    )
    assert any(read.source == "fact" and read.kind == "expense" for read in reads)
    assert any(read.source == "calculation" and read.kind == "expense" for read in reads)


def test_original_workbook_and_job_are_required_for_proven_review(report_book, monkeypatch):
    engine = report_book[0]
    scenario(report_book)
    with QueryReads.snapshot(engine) as reads:
        plan = report_plan(engine, reads)
    original = retain_export(engine, plan, "submitted")
    reviewed = retain_export(engine, plan, "reviewed")
    completed = completion(original)
    monkeypatch.setattr(Reports, "_export_plan", lambda *args, **kwargs: plan)
    with QueryReads.snapshot(engine) as reads:
        attach_versions(monkeypatch, reads, completed)
        prepared = _prepare_one(engine, reads, review(reviewed))
        assert prepared["comparison_result"] == "matched"
        assert prepared["submitted_report_binding"]["workbook_evidence_digest"] == original[1]
        assert prepared["current_calculations"]
        calc = SimpleNamespace(values={**prepared, "review_result": "matched"})
        assert (
            review_snapshot_state(engine, reads, obligation(), completed, calc)["review_status"]
            == "reviewed"
        )
    # Retaining a manifest and receipt without the separately adopted workbook
    # does not establish the contents actually submitted.
    missing = completion((original[0], original[2]))
    missing.fact.adopted_evidence_digests = (original[0],)
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            _completion_binding(engine, reads, obligation(), missing)


@pytest.mark.parametrize(
    "damage",
    ["wrong_company", "wrong_quarter", "pending_job", "missing_basis", "workbook_mismatch"],
)
def test_manifest_authentication_rejects_unproven_sources(report_book, damage):
    engine = report_book[0]
    scenario(report_book)
    with QueryReads.snapshot(engine) as reads:
        plan = report_plan(engine, reads)
    if damage == "wrong_company":
        plan["company_id"] = "some-other-company"
    elif damage == "wrong_quarter":
        plan["period"]["quarter"] = 2
    elif damage == "missing_basis":
        plan.pop("report_basis")
    plan["digest"] = digest(
        {key: value for key, value in plan.items() if key not in {"digest", "epochs"}}
    ).hex()
    evidence = retain_export(engine, plan, damage)
    if damage == "pending_job":
        with engine.store.connection() as connection:
            connection.execute("UPDATE jobs SET status='pending' WHERE id=?", ("report-" + damage,))
    elif damage == "workbook_mismatch":
        wrong = engine.register_evidence(
            b"PK fabricated different workbook",
            "application/octet-stream",
            "wrong",
            request_id="wrong",
        )["digest"]
        evidence = evidence[0], wrong, evidence[2]
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError):
            _completion_binding(engine, reads, obligation(), completion(evidence))


def test_management_change_with_same_amounts_requires_another_review(report_book, monkeypatch):
    engine = report_book[0]
    scenario(report_book)
    with QueryReads.snapshot(engine) as reads:
        plan = report_plan(engine, reads)
    original = retain_export(engine, plan, "original")
    reviewed = retain_export(engine, plan, "review")
    completed = completion(original)
    current = copy.deepcopy(plan)
    current["rule"]["adapter_version"] = "changed-report-rule"
    monkeypatch.setattr(Reports, "_export_plan", lambda *args, **kwargs: current)
    with QueryReads.snapshot(engine) as reads:
        attach_versions(monkeypatch, reads, completed)
        assert _prepare_one(engine, reads, review(reviewed))["comparison_result"] == "outdated"
        binding = _completion_binding(engine, reads, obligation(), completed)
        calc = SimpleNamespace(
            values={
                "submitted_report_binding": binding,
                "reviewed_report_signature": report_signature(plan),
                "review_result": "matched",
            }
        )
        assert (
            review_snapshot_state(engine, reads, obligation(), completed, calc)["review_status"]
            == "outdated"
        )


def test_new_complete_review_establishes_real_report_difference(report_book, monkeypatch):
    engine = report_book[0]
    scenario(report_book)
    with QueryReads.snapshot(engine) as reads:
        plan = report_plan(engine, reads)
    original = retain_export(engine, plan, "old")
    current = copy.deepcopy(plan)
    current["organization"]["name"] = "Corrected company name"
    current["digest"] = digest(
        {key: value for key, value in current.items() if key not in {"digest", "epochs"}}
    ).hex()
    reviewed = retain_export(engine, current, "new")
    monkeypatch.setattr(Reports, "_export_plan", lambda *args, **kwargs: current)
    with QueryReads.snapshot(engine) as reads:
        attach_versions(monkeypatch, reads, completion(original))
        assert _prepare_one(engine, reads, review(reviewed))["comparison_result"] == "different"


def test_new_manifest_can_prove_unchanged_content_after_basis_changes(report_book, monkeypatch):
    engine = report_book[0]
    scenario(report_book)
    with QueryReads.snapshot(engine) as reads:
        plan = report_plan(engine, reads)
    original = retain_export(engine, plan, "same-content-original")
    current = copy.deepcopy(plan)
    current["report_basis"]["report_facts"][0]["fact_id"] = "replacement-input-version"
    current["digest"] = digest(
        {key: value for key, value in current.items() if key not in {"digest", "epochs"}}
    ).hex()
    reviewed = retain_export(engine, current, "same-content-reviewed")
    assert report_signature(current) != report_signature(plan)
    monkeypatch.setattr(Reports, "_export_plan", lambda *args, **kwargs: current)
    with QueryReads.snapshot(engine) as reads:
        completed = completion(original)
        attach_versions(monkeypatch, reads, completed)
        old_review = review((original[0], original[1], original[2]))
        assert _prepare_one(engine, reads, old_review)["comparison_result"] == "outdated"
        assert _prepare_one(engine, reads, review(reviewed))["comparison_result"] == "matched"


def test_current_report_missing_information_invalidates_established_review(
    report_book, monkeypatch
):
    engine = report_book[0]
    scenario(report_book)
    with QueryReads.snapshot(engine) as reads:
        plan = report_plan(engine, reads)
    original = retain_export(engine, plan, "blocked-original")
    current = copy.deepcopy(plan)
    current.pop("report_basis")
    current["status"] = "needs_information"
    current["fact_issues"] = [
        {"field": "classification", "message": "classification requires correction"}
    ]
    monkeypatch.setattr(Reports, "_export_plan", lambda *args, **kwargs: current)
    with QueryReads.snapshot(engine) as reads:
        completed = completion(original)
        attach_versions(monkeypatch, reads, completed)
        prepared = _prepare_one(engine, reads, review(original))
        assert prepared["comparison_result"] == "outdated"
        assert prepared["basis_issues"] == current["fact_issues"]
        calc = SimpleNamespace(
            values={
                "submitted_report_binding": _completion_binding(
                    engine, reads, obligation(), completed
                ),
                "reviewed_report_signature": report_signature(plan),
                "review_result": "matched",
            }
        )
        state = review_snapshot_state(engine, reads, obligation(), completed, calc)
        assert state["review_status"] == "outdated"
        assert state["basis_issues"] == current["fact_issues"]


@pytest.mark.parametrize("component", ["evidence", "job_payload"])
def test_formal_source_damage_is_an_integrity_error_not_missing_proof(
    report_book, monkeypatch, component
):
    from test_integrity_content import damage

    engine = report_book[0]
    scenario(report_book)
    with QueryReads.snapshot(engine) as reads:
        plan = report_plan(engine, reads)
    original = retain_export(engine, plan, "damaged-source")
    if component == "evidence":
        damage(
            engine,
            "evidence",
            "UPDATE evidence SET content=? WHERE digest=unhex(?)",
            (b"broken bytes", original[1]),
        )
    else:
        damage(
            engine,
            "jobs",
            "UPDATE jobs SET payload=json_set(payload,'$.plan.unhashed_change',1) WHERE id=?",
            ("report-damaged-source",),
        )
    monkeypatch.setattr(Reports, "_export_plan", lambda *args, **kwargs: plan)
    with QueryReads.snapshot(engine) as reads:
        completed = completion(original)
        attach_versions(monkeypatch, reads, completed)
        with pytest.raises(KernelError) as failure:
            _prepare_one(engine, reads, review(original))
        assert failure.value.code == "content_integrity_failed"
        calc = SimpleNamespace(values={"reviewed_report_signature": report_signature(plan)})
        with pytest.raises(KernelError) as failure:
            review_snapshot_state(engine, reads, obligation(), completed, calc)
        assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("followup", ["final-close", "q2-correction", "open-batch"])
def test_real_worker_evidence_engine_publication_and_query_lifecycle(
    report_book, tmp_path, followup
):
    """Submit and prove an open report, then close without rewriting that version."""
    import json
    from pathlib import Path

    from test_report_open_export import prepare_materials

    from ai_accounting.kernel.reports import run_report_jobs

    engine, save, publish, _ = report_book
    reports = scenario(report_book)
    prepare_materials(report_book)
    plan = reports.preview_export(2026, 1)
    assert plan["source"] == "open"
    receipt = reports.confirm_export(
        2026,
        1,
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        output_directory=str(tmp_path / "original-export"),
        request_id="real-export-job",
    )
    results = run_report_jobs(engine)
    job = next(row for row in results if row["job_id"] == receipt["job_id"])
    assert job["status"] == "succeeded"
    result = job["result"]
    originals = []
    for path in (Path(result["directory"]) / "manifest.json", Path(result["path"])):
        originals.append(
            engine.register_evidence(
                path.read_bytes(),
                "application/octet-stream",
                path.name,
                request_id="retain-" + path.name,
            )["digest"]
        )
    external_receipt = engine.register_evidence(
        b"Fictional authority accepted this exact original workbook",
        "text/plain",
        "authority-receipt",
        request_id="authority-receipt",
    )["digest"]
    obligation_saved = save(
        "external_obligation",
        "quarter",
        {
            "period": "2026-04",
            "obligation_kind": "quarterly_financial_report",
            "start_period": "2026-01",
            "end_period": "2026-03",
            "applicability_confirmed": True,
        },
    )
    basis = Workflow(engine).obligation_basis("quarter")
    completed_saved = engine.save_fact(
        "external_completion",
        "completed",
        {
            "period": "2026-04",
            "obligation_id": "quarter",
            "obligation_fact_id": obligation_saved["fact_id"],
            "obligation_kind": "quarterly_financial_report",
            "start_period": "2026-01",
            "end_period": "2026-03",
            "accepted_calculations": basis["candidate_calculations"],
            "adopted_evidence_digests": originals,
            "completion_status": "submitted",
            "date_status": "known",
            "completion_date": "2026-04-05",
        },
        evidence=tuple([*originals, external_receipt]),
        expected_revision=0,
        request_id="real-completion",
    )
    publish("completed")
    review_saved = engine.save_fact(
        "external_basis_review",
        "reviewed",
        {
            "period": "2026-04",
            "completion_id": "completed",
            "completion_fact_id": completed_saved["fact_id"],
            "obligation_id": "quarter",
            "obligation_fact_id": obligation_saved["fact_id"],
            "obligation_kind": "quarterly_financial_report",
            "start_period": "2026-01",
            "end_period": "2026-03",
            "adopted_calculations": basis["candidate_calculations"],
            "reviewed_calculations": basis["candidate_calculations"],
            "review_result": "matched",
        },
        evidence=tuple(originals),
        expected_revision=0,
        request_id="real-review",
    )
    publish("reviewed")
    queried = Workflow(engine).query(as_of="2026-04-10")
    item = next(
        row for row in queried["sections"]["external"]["obligations"] if row["id"] == "quarter"
    )
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "reviewed"
    original_workbook = Path(result["path"]).read_bytes()
    with engine.store.connection(read_only=True) as connection:
        job_payload = connection.execute(
            "SELECT payload FROM jobs WHERE id=?", (receipt["job_id"],)
        ).fetchone()[0]
        original_evidence = {
            row["digest"].hex(): bytes(row["content"])
            for row in connection.execute(
                "SELECT digest,content FROM evidence "
                "WHERE digest IN (SELECT unhex(value) FROM json_each(?))",
                (canonical([*originals, external_receipt]),),
            )
        }
    if followup == "open-batch":
        save(
            "expense",
            "cost",
            {
                "period": "2026-02",
                "counterparty_id": "supplier",
                "amount_fen": 15000,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            revision=1,
        )
        batch = engine.preview(["cost", "reviewed"])
        reviewed_result = next(row for row in batch["results"] if row["subject_id"] == "reviewed")
        assert reviewed_result["values"]["review_result"] == "outdated"
        assert any(
            issue.get("field") == "report_basis"
            and issue.get("message") == "报表核对须在上游正式发布后重新准备"
            for issue in reviewed_result["values"]["basis_issues"]
        )
        engine.confirm(
            ["cost", "reviewed"],
            preview_digest=batch["digest"],
            epochs=batch["epochs"],
            request_id="open-upstream-review-batch",
        )
        with engine.store.connection(read_only=True) as connection:
            outcome = connection.execute(
                "SELECT c.outcome FROM calculation_current a "
                "JOIN calculation c ON c.id=a.calculation_id WHERE a.subject_id='reviewed'",
            ).fetchone()[0]
        assert json.loads(outcome)["values"]["review_result"] == "outdated"
        queried = Workflow(engine).query(as_of="2026-04-10")
        item = next(
            row for row in queried["sections"]["external"]["obligations"] if row["id"] == "quarter"
        )
        assert item["actual_completion_status"] == "completed"
        assert item["basis_review_status"] == "outdated"
        assert Path(result["path"]).read_bytes() == original_workbook
        with QueryReads.snapshot(engine) as reads:
            saved_review = reads.fact_version(review_saved["fact_id"])
            assert saved_review.fact.review_result == "matched"
            assert saved_review.evidence == tuple(originals)
            for subject_id, fact_id in (
                ("completed", completed_saved["fact_id"]),
                ("reviewed", review_saved["fact_id"]),
            ):
                current_fact = reads.connection.execute(
                    "SELECT fact_id FROM fact_current WHERE subject_id=?", (subject_id,)
                ).fetchone()[0]
                assert current_fact == fact_id
            assert reads.connection.execute(
                "SELECT payload FROM jobs WHERE id=?", (receipt["job_id"],)
            ).fetchone()[0] == job_payload
            for ident, content in original_evidence.items():
                assert bytes(reads.connection.execute(
                    "SELECT content FROM evidence WHERE digest=unhex(?)", (ident,)
                ).fetchone()[0]) == content
        return
    close_quarter(report_book)
    queried = Workflow(engine).query(as_of="2026-04-10")
    item = next(
        row for row in queried["sections"]["external"]["obligations"] if row["id"] == "quarter"
    )
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "reviewed"
    assert Path(result["path"]).read_bytes() == original_workbook
    with engine.store.connection(read_only=True) as connection:
        unchanged_job = connection.execute(
            "SELECT payload FROM jobs WHERE id=?", (receipt["job_id"],)
        ).fetchone()[0]
        assert unchanged_job == job_payload
        assert json.loads(unchanged_job)["plan"]["source"] == "open"
        assert (
            connection.execute(
                "SELECT fact_id FROM fact_current WHERE subject_id='completed'",
            ).fetchone()[0]
            == completed_saved["fact_id"]
        )
        for ident, content in original_evidence.items():
            assert (
                bytes(
                    connection.execute(
                        "SELECT content FROM evidence WHERE digest=unhex(?)",
                        (ident,),
                    ).fetchone()[0]
                )
                == content
            )
    if followup == "q2-correction":
        original_review_calculation = item["basis_review_calculation_id"]
        original_q1 = reports.preview_export(2026, 1)
        save(
            "expense",
            "cost",
            {
                "period": "2026-02",
                "counterparty_id": "supplier",
                "amount_fen": 15000,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            revision=1,
        )
        publish("cost", posting_period="2026-04")
        preserved_q1 = reports.preview_export(2026, 1)
        assert preserved_q1["statements"] == original_q1["statements"]
        assert report_signature(preserved_q1) == report_signature(original_q1)
        april = engine.ledger("2026-04")
        expense_vouchers = [row for row in april if row["kind"] == "expense"]
        assert len(expense_vouchers) == 2
        assert sum(row["reverses_id"] is not None for row in expense_vouchers) == 1
        with engine.store.connection(read_only=True) as connection:
            amount = connection.execute(
                "SELECT sum(l.debit-l.credit) FROM voucher_version v "
                "JOIN voucher_line l ON l.version_id=v.id WHERE v.period=? AND l.account='5602'",
                (YearMonth("2026-04").ordinal,),
            ).fetchone()[0]
        assert amount == 5000
        queried = Workflow(engine).query(as_of="2026-04-10")
        item = next(
            row for row in queried["sections"]["external"]["obligations"] if row["id"] == "quarter"
        )
        assert item["actual_completion_status"] == "completed"
        with engine.store.connection(read_only=True) as connection:
            recalculated_review = connection.execute(
                "SELECT c.id,c.outcome FROM calculation_current a "
                "JOIN calculation c ON c.id=a.calculation_id WHERE a.subject_id='reviewed'",
            ).fetchone()
        assert recalculated_review["id"] != original_review_calculation
        assert json.loads(recalculated_review["outcome"])["values"]["review_result"] == "matched"
        assert item["basis_review_status"] == "reviewed", {
            "actual_status": item["basis_review_status"],
            "review_recalculated": recalculated_review["id"] != original_review_calculation,
            "review_calculated_result": json.loads(recalculated_review["outcome"])["values"][
                "review_result"
            ],
            "basis_issues": item.get("basis_issues", []),
        }
