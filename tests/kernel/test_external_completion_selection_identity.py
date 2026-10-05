"""Actual completion scopes reject a result classified outside its own fact."""

import pytest
from test_engine import engine as engine_fixture
from test_engine import publish, save
from test_integrity_content import damage
from test_workflow import (
    completion_from_basis,
    contribution_policy,
    income_tax_policy,
    obligation,
    opening,
    payroll,
    profile,
    save_completion,
    setup_company,
)

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError, Read
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.workflow import Workflow

engine = engine_fixture


@pytest.fixture
def completed_company(tmp_path):
    company = setup_company(tmp_path)
    for fact, subject in (
        (profile(), "profile"),
        (contribution_policy(), "contributions"),
        (income_tax_policy(), "income-tax"),
        (opening(), "opening"),
        (payroll(), "january"),
    ):
        company.save(fact, subject)
    company.confirm_payroll("january")
    company.publish("january")
    company.save(obligation(), "obligation")
    basis = Workflow(company.engine).obligation_basis("obligation")
    save_completion(company, completion_from_basis(basis), adopted_basis=True)
    company.publish("completion")
    return company


def external(company, entry):
    if entry == "workflow":
        return Workflow(company.engine).query("2026-01", as_of="2026-02-28")["sections"]["external"]
    return BusinessQueries(company.engine).business_status(
        "january", "2026-01", as_of="2026-02-28"
    )["external"]


@pytest.mark.parametrize("entry", ["workflow", "business_status"])
def test_actual_completion_preserves_receipt_and_accounting_basis(completed_company, entry):
    result = external(completed_company, entry)
    item = next(item for item in result["obligations"] if item["id"] == "obligation")
    assert item["actual_completion_status"] == "completed"
    assert len(item["recorded_completions"]) == 1
    if entry == "business_status":
        assert len(result["completions"]) == 1


@pytest.mark.parametrize("entry", ["workflow", "business_status"])
def test_actual_completion_cannot_disappear_after_mutable_kind_changes(completed_company, entry):
    company = completed_company
    calculation = company.current("completion", "external_completion")
    damage(
        company.engine,
        "calculation",
        "UPDATE calculation SET kind='expense' WHERE id=?",
        (calculation.id,),
    )
    with pytest.raises(KernelError) as failure:
        external(company, entry)
    assert failure.value.code == "content_integrity_failed"


def test_prime_select_failed_batch_does_not_publish_selections_or_typed_results(engine):
    for subject in ("cached", "valid", "damaged"):
        save(engine, subject=subject, request=subject)
    _, result = publish(engine, ["cached", "valid", "damaged"])
    ids = {item["subject_id"]: item["calculation_id"] for item in result["results"]}
    damage(
        engine,
        "calculation",
        "UPDATE calculation SET kind='test_source' WHERE id=?",
        (ids["damaged"],),
    )
    with QueryReads.snapshot(engine) as reads:
        cached = Read("calculation", "*", "#" + ids["cached"])
        reads.prime_select((cached,))
        before_selections = dict(reads._selections)
        before_typed = dict(reads._typed_calculations)
        before_metadata = dict(reads._metadata)
        batch = [Read("calculation", "*", "#" + ids[key]) for key in ("valid", "damaged")]
        for _ in range(2):
            with pytest.raises(KernelError) as failure:
                reads.prime_select(batch)
            assert failure.value.code == "content_integrity_failed"
            assert reads._selections == before_selections
            assert reads._typed_calculations == before_typed
            assert reads._metadata == before_metadata


def test_prime_select_authenticates_one_exact_batch_and_reuses_success(engine, monkeypatch):
    for index in range(8):
        save(engine, subject=f"selected-{index}", request=f"selected-{index}")
    _, result = publish(engine, [f"selected-{index}" for index in range(8)])
    ids = {item["calculation_id"] for item in result["results"]}
    with QueryReads.snapshot(engine) as reads:
        decoded = []
        original = reads.store.calculation

        def calculation(row):
            decoded.append(row["id"])
            return original(row)

        monkeypatch.setattr(reads.store, "calculation", calculation)
        statements = []
        reads.connection.set_trace_callback(statements.append)
        selection = Read("calculation", "test_charge", "*")
        reads.prime_select((selection,))
        selected = reads.select(selection)
        assert {item.id for item in selected} == ids
        assert all(item.values["amount"] == 100 for item in selected)
        assert set(decoded) == ids and len(decoded) == len(ids)
        # Candidate identity, selected results, and supersession are batched;
        # an additional per-result metadata/publication query is unnecessary.
        assert len(statements) == 3
        before = len(statements)
        reads.prime_select((selection,))
        assert len(statements) == before
        assert len(decoded) == len(ids)
        reads.connection.set_trace_callback(None)
