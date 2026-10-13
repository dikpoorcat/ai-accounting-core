"""New bank instructions adopt current opening identities and preserve old files."""

import json
from pathlib import Path

import pytest
from test_exports import template_bytes
from test_identity_corrections import confirm, opening_package
from test_identity_corrections import identity_engine as identity_engine  # noqa: F401
from test_integrity_content import damage
from test_opening_continuation import _close_without_current_business

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.exports import MANIFEST_NAME, Exports, run_export_jobs
from ai_accounting.kernel.periods import Periods


def make_opening_payment_export(identity_engine, kind="opening_obligation"):
    engine, proof, first, second = identity_engine
    fields = (
        {"counterparty_id": first, "nature": "employee_reimbursement",
         "outstanding_fen": 12000, "business_reference": "prior-reimbursement"}
        if kind == "opening_obligation" else
        {"employee_id": first, "recipient_id": first, "payroll_period": "2025-12",
         "component": "net", "outstanding_fen": 12000}
    )
    opening_package(engine, proof, [
        (kind, "opening-payment", fields),
        ("opening_equity", "opening-loss", {
            "equity_kind": "retained_earnings", "balance_fen": -12000,
            "holder_or_basis_id": first,
        }),
    ])
    export = Exports(engine)
    for index, party in enumerate((first, second)):
        export.save_payee(
            party, name=f"合成收款人{index}", account=f"00012345678{index}",
            evidence_digest=proof, expected_revision=0, request_id=f"payee-{index}",
        )
    template = engine.register_evidence(
        template_bytes(), "application/octet-stream", "bank template", request_id="template",
    )["digest"]
    _close_without_current_business(engine, "2026-01", proof)
    corrected = (
        fields | {"counterparty_id": second} if kind == "opening_obligation"
        else fields | {"employee_id": second, "recipient_id": second}
    )
    options = {
        "changes": [{"subject_id": "opening-payment", "expected_revision": 1,
                     "action": "reassign", "data": {
                         "period": "2026-01", "package_id": "opening", **corrected,
                     }}],
        "evidence": [proof], "reason": "合成确认原期初债务应付给另一具名对象",
        "posting_period": "2026-02",
    }
    return engine, export, template, options, first, second


def preview(export, template):
    return export.preview("2026-01", template_evidence_digest=template,
                          source_ids=["opening-payment"])


def freeze(export, template, plan, directory, request):
    return export.confirm(
        "2026-01", template_evidence_digest=template, source_ids=["opening-payment"],
        preview_digest=plan["digest"], epochs=plan["epochs"],
        output_directory=str(directory), request_id=request,
    )


@pytest.mark.parametrize("kind", ["opening_obligation", "opening_payroll_payable"])
def test_corrected_opening_recipient_feeds_new_file_and_keeps_original_frozen_plan(
    identity_engine, tmp_path, kind,
):
    engine, export, template, options, first, second = make_opening_payment_export(
        identity_engine, kind,
    )
    original = preview(export, template)
    old_job = freeze(export, template, original, tmp_path / "old-file", "old-export")
    closed = Periods(engine).closed_report("2026-01")
    with engine.store.connection(read_only=True) as connection:
        original_fact = engine.store.current_fact(connection, "opening-payment")
    correction, _ = confirm(engine, options)
    binding_id = next(item["calculation_id"] for item in correction["results"]
                      if item["kind"] == "opening_identity_binding")
    updated = preview(export, template)
    source = updated["rows"][0]["sources"][0]
    assert updated["total_fen"] == original["total_fen"] == 12000
    assert original["rows"][0]["party_id"] == first
    assert updated["rows"][0]["party_id"] == second
    assert updated["rows"][0]["account"] == "000123456781"
    assert source["opening_binding_calculation_id"] == binding_id
    assert "opening_binding_calculation_id" not in original["rows"][0]["sources"][0]
    new_job = freeze(export, template, updated, tmp_path / "new-file", "new-export")
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.current_fact(connection, "opening-payment") == original_fact
    assert Periods(engine).closed_report("2026-01") == closed
    results = run_export_jobs(engine)
    plans = {
        item["job_id"]: json.loads(
            (Path(item["result"]["directory"]) / MANIFEST_NAME).read_text(encoding="utf-8")
        )["plan"]
        for item in results
        if item["status"] == "succeeded"
    }
    assert len(plans) == 2
    assert plans[old_job["job_id"]] == original
    assert plans[new_job["job_id"]] == updated


@pytest.mark.parametrize("state", ["unpublished", "pending"])
def test_opening_identity_binding_must_be_published_and_current_before_export(
    identity_engine, state,
):
    engine, export, template, options, _, _ = make_opening_payment_export(identity_engine)
    correction, _ = confirm(engine, options)
    binding = next(item for item in correction["results"]
                   if item["kind"] == "opening_identity_binding")
    subject = binding["subject_id"]
    if state == "unpublished":
        damage(engine, "calculation_current", "DELETE FROM calculation_current WHERE subject_id=?",
               (subject,))
    else:
        with engine.store.connection() as connection:
            fact = engine.store.current_fact(connection, subject)
            connection.execute("INSERT INTO pending VALUES(?,?)", (subject, fact.id))
    with pytest.raises(KernelError) as blocked:
        preview(export, template)
    assert blocked.value.code == "materials_incomplete"
    assert any(issue["field"] == subject for issue in blocked.value.details["fact_issues"])
