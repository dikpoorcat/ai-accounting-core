"""Actual tax provenance reviews preserve vouchers and verified cash settlements."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import create_model
from test_actual_tax_priority import (
    ASSESSMENT,
    PAYABLE_VAT,
    income_actual,
    publish,
    snapshot,
    vat_actual,
)
from test_actual_tax_priority import income_company as income_company
from test_actual_tax_priority import quarter_company as quarter_company

from ai_accounting.kernel.accounting import AccountingBook
from ai_accounting.kernel.contracts import Calculation, KernelError, NeedsInformation
from ai_accounting.kernel.domains import taxes, transactions
from ai_accounting.kernel.types import YearMonth, canonical, digest


@pytest.fixture(params=["vat", "income"])
def actual_case(request):
    if request.param == "vat":
        company = request.getfixturevalue("quarter_company")
        source = vat_actual(
            company,
            assessed_vat_fen=PAYABLE_VAT,
            vat_due_fen=PAYABLE_VAT,
            row_15_fen=PAYABLE_VAT,
        )
        kind, subject = "tax_assessment", ASSESSMENT
    else:
        company = request.getfixturevalue("income_company")
        source = income_actual(company)
        kind, subject = "income_tax_assessment", "current-tax"
    publish(company, subject, "initial-actual-tax")
    return company, kind, subject, source


def rows_for(result):
    return {item["subject_id"]: item for item in result["results"]}


def journal_snapshot(company):
    with company.engine.store.connection(read_only=True) as connection:
        return {
            table: tuple(
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            )
            for table in (
                "voucher",
                "voucher_version",
                "voucher_line",
                "voucher_current",
                "balance",
            )
        }


def pay_tax(company, kind, subject, *, direction="outflow"):
    obligation = company.current(kind, subject).values["obligations"][0]
    company.save(
        transactions.Payment,
        "tax-payment",
        period="2026-10",
        actual_date="2026-10-08",
        bank_account_id="bank",
        counterparty_id="authority",
        amount_fen=obligation["amount_fen"],
        direction=direction,
        allocations=[
            dict(
                source_kind=kind,
                source_id=subject,
                obligation=obligation["name"],
                amount_fen=obligation["amount_fen"],
            )
        ],
    )
    publish(company, "tax-payment", "publish-tax-payment")
    return company.current("payment", "tax-payment")


def load_book(company, kind, subject):
    current = company.current(kind, subject)
    book = AccountingBook(company.engine.store.registry)
    with company.engine.store.connection(read_only=True) as connection:
        book.load(company.engine.store, connection, [current.id])
    return book, book.records[current.id]


def add_candidate(book, record, outcome, pointer="detached-candidate", *, version=None):
    version = version or record.version
    calculation = Calculation(
        pointer,
        version.subject_id,
        version.fact.kind,
        version.fact.period,
        outcome["values"],
        version.id,
        digest(outcome).hex(),
    )
    book.add(
        calculation,
        version,
        outcome,
        fact_ids=record.fact_ids,
        calculation_ids=record.calculation_ids,
    )
    return calculation.id


def revise_actual(company, kind, **changes):
    if kind == "tax_assessment":
        return vat_actual(
            company,
            **dict(
                assessed_vat_fen=PAYABLE_VAT,
                vat_due_fen=PAYABLE_VAT,
                row_15_fen=PAYABLE_VAT,
            )
            | changes,
        )
    return income_actual(company, **changes)


def test_changed_reference_policy_does_not_rewrite_actual_tax_or_paid_voucher(quarter_company):
    company = quarter_company
    vat_actual(
        company,
        assessed_vat_fen=PAYABLE_VAT,
        vat_due_fen=PAYABLE_VAT,
        row_15_fen=PAYABLE_VAT,
    )
    first_preview, _ = publish(company, ASSESSMENT, "initial-actual-tax")
    old_tax = company.current("tax_assessment", ASSESSMENT)
    old_payment = pay_tax(company, "tax_assessment", ASSESSMENT)
    original_trace = deepcopy(company.engine.trace(old_tax.id)["calculation"])
    journal = journal_snapshot(company)
    with company.engine.store.connection(read_only=True) as connection:
        policy = company.engine.store.current_fact(connection, "surtax").fact.policy
    company.save(
        taxes.SurtaxPolicyFact,
        "surtax",
        period="2026-07",
        policy=policy.model_dump(mode="json")
        | {"version": "synthetic-reference-policy-revised", "urban_rate_percent": "9"},
    )
    preview, result = publish(company, ASSESSMENT, "review-reference-policy")
    for subject in (ASSESSMENT, "tax-payment"):
        assert rows_for(preview)[subject]["impact"] == "review_no_impact"
        assert rows_for(result)[subject]["mode"] == "review_no_impact"
    current = company.current("tax_assessment", ASSESSMENT)
    assert current.id != old_tax.id
    assert current.values["payable_vat_fen"] == PAYABLE_VAT
    assert current.values["surtax_fen"] == 0
    assert (
        current.values["calculated_assessment"]["surtax_fen"]
        != (old_tax.values["calculated_assessment"]["surtax_fen"])
    )
    assert current.values["calculated_assessment"] != old_tax.values["calculated_assessment"]
    assert (
        rows_for(preview)[ASSESSMENT]["accounting"]
        == rows_for(first_preview)[ASSESSMENT]["accounting"]
    )
    payment = company.current("payment", "tax-payment")
    assert payment.id != old_payment.id and payment.fact_id == old_payment.fact_id
    assert payment.values["settlements"][0]["source_calculation"] == current.id
    assert company.balance(f"tax_assessment:{ASSESSMENT}:vat") == 0
    assert journal_snapshot(company) == journal
    assert company.engine.trace(old_tax.id)["calculation"] == original_trace
    assert digest(original_trace["outcome"]).hex() == original_trace["digest"]


def test_new_actual_proof_revision_is_adopted_without_rewriting_tax_or_payment(actual_case):
    company, kind, subject, old_source = actual_case
    original = company.current(kind, subject)
    pay_tax(company, kind, subject)
    original_trace = deepcopy(company.engine.trace(original.id)["calculation"])
    journal = journal_snapshot(company)
    revised = revise_actual(company, kind, basis_type="tax_authority_assessment")
    assert revised["fact_id"] != old_source["fact_id"]
    preview, result = publish(company, subject, "review-actual-proof")
    for target in (subject, "tax-payment"):
        assert rows_for(preview)[target]["impact"] == "review_no_impact"
        assert rows_for(result)[target]["mode"] == "review_no_impact"
    current = company.current(kind, subject)
    assert current.values["actual_declaration"]["fact_id"] == revised["fact_id"]
    assert current.values["actual_declaration"]["basis_type"] == "tax_authority_assessment"
    assert journal_snapshot(company) == journal
    assert company.engine.trace(original.id)["calculation"] == original_trace


def test_genuine_actual_tax_increase_changes_accounting_with_paid_source_preserved(actual_case):
    company, kind, subject, _ = actual_case
    original = company.current(kind, subject)
    payment = pay_tax(company, kind, subject)
    if kind == "tax_assessment":
        revise_actual(
            company,
            kind,
            assessed_vat_fen=PAYABLE_VAT + 17,
            vat_due_fen=PAYABLE_VAT + 17,
            row_15_fen=PAYABLE_VAT + 17,
        )
    else:
        revise_actual(
            company,
            kind,
            cumulative_assessed_fen=original.values["cumulative_assessed_fen"] + 17,
            tax_due_fen=original.values["tax_due_fen"] + 17,
        )
    preview, result = publish(company, subject, "increase-actual-tax")
    assert rows_for(preview)[subject]["impact"] == "accounting_changed"
    assert rows_for(result)[subject]["impact"] == "accounting_changed"
    current = company.current(kind, subject)
    assert current.values["obligations"][0]["amount_fen"] == (
        original.values["obligations"][0]["amount_fen"] + 17
    )
    assert company.balance(current.values["obligations"][0]["key"]) == 17
    assert company.current("payment", "tax-payment").fact_id == payment.fact_id


def test_downward_actual_after_payment_keeps_overpayment_block_and_original_vouchers(actual_case):
    company, kind, subject, _ = actual_case
    original = company.current(kind, subject)
    pay_tax(company, kind, subject)
    if kind == "tax_assessment":
        revise_actual(
            company,
            kind,
            assessed_vat_fen=PAYABLE_VAT - 17,
            vat_due_fen=PAYABLE_VAT - 17,
            row_15_fen=PAYABLE_VAT - 17,
        )
    else:
        revise_actual(
            company,
            kind,
            cumulative_assessed_fen=original.values["cumulative_assessed_fen"] - 17,
            tax_due_fen=original.values["tax_due_fen"] - 17,
        )
    before = snapshot(company)
    with pytest.raises(NeedsInformation) as failure:
        company.engine.preview([subject])
    assert any(issue["field"] == "overpayment" for issue in failure.value.issues)
    assert snapshot(company) == before
    assert company.current(kind, subject).id == original.id


@pytest.mark.parametrize("path", ["values", "explanation", "outcome", "actual_declaration"])
def test_unknown_outcome_fields_remain_significant_and_original_digest_stays_immutable(
    actual_case, path
):
    company, kind, subject, _ = actual_case
    book, record = load_book(company, kind, subject)
    original_trace = deepcopy(company.engine.trace(record.calculation.id)["calculation"])
    before = canonical(record.outcome)
    outcome = json.loads(before)
    if path == "values":
        outcome["values"]["future_accounting_meaning"] = 7
    elif path == "explanation":
        outcome["explanation"].append({"future_accounting_meaning": 7})
    elif path == "actual_declaration":
        outcome["values"]["actual_declaration"]["future_accounting_meaning"] = 7
    else:
        outcome["future_accounting_meaning"] = 7
    candidate = add_candidate(book, record, outcome)
    assert book.signature(record.calculation.id) != book.signature(candidate)
    assert canonical(record.outcome) == before
    assert company.engine.trace(record.calculation.id)["calculation"] == original_trace
    assert digest(original_trace["outcome"]).hex() == original_trace["digest"]


@pytest.mark.parametrize("component", ["lines", "obligations", "balances"])
def test_genuine_journal_or_obligation_changes_are_not_normalized(actual_case, component):
    company, kind, subject, _ = actual_case
    book, record = load_book(company, kind, subject)
    outcome = json.loads(canonical(record.outcome))
    if component == "lines":
        outcome["lines"][0]["account"] = "571101"
    elif component == "obligations":
        outcome["values"]["obligations"][0]["amount_fen"] += 1
    else:
        outcome["balances"][0]["amount"] += 1
    candidate = add_candidate(book, record, outcome)
    assert book.signature(record.calculation.id) != book.signature(candidate)


@pytest.mark.parametrize(
    "fault",
    ["missing", "unrecorded", "wrong_assessment", "wrong_period", "wrong_evidence", "wrong_amount"],
)
def test_missing_or_mismatched_exact_actual_source_requires_compatibility(actual_case, fault):
    company, kind, subject, _ = actual_case
    book, record = load_book(company, kind, subject)
    pointer = record.outcome["values"]["actual_declaration"]["fact_id"]
    source = book.facts[pointer]
    if fault == "missing":
        del book.facts[pointer]
    elif fault == "unrecorded":
        book.records[record.calculation.id] = replace(record, fact_ids=record.fact_ids - {pointer})
    elif fault == "wrong_assessment":
        book.facts[pointer] = replace(
            source, fact=source.fact.model_copy(update={"assessment_id": "unrelated-tax"})
        )
    elif fault == "wrong_period":
        changed = (
            {"period_end": "2026-08-31"}
            if kind == "tax_assessment"
            else {"tax_period_end": "2026-08"}
        )
        book.facts[pointer] = replace(source, fact=source.fact.model_copy(update=changed))
    elif fault == "wrong_evidence":
        book.facts[pointer] = replace(source, evidence=())
    else:
        field = "assessed_vat_fen" if kind == "tax_assessment" else "cumulative_assessed_fen"
        book.facts[pointer] = replace(
            source, fact=source.fact.model_copy(update={field: getattr(source.fact, field) + 1})
        )
    with pytest.raises(KernelError) as failure:
        book.signature(record.calculation.id)
    assert failure.value.code == "accounting_compatibility_required"


def test_income_tax_refund_review_preserves_inflow_direction_and_payment_voucher(income_company):
    company = income_company
    income_actual(company, cumulative_assessed_fen=0, tax_due_fen=-9_629)
    publish(company, "current-tax", "actual-refund-tax")
    tax = company.current("income_tax_assessment", "current-tax")
    assert tax.values["obligations"][0]["normal"] == "debit"
    assert tax.values["obligations"][0]["category"] == "receivable"
    payment = pay_tax(company, "income_tax_assessment", "current-tax", direction="inflow")
    journal = journal_snapshot(company)
    income_actual(
        company,
        cumulative_assessed_fen=0,
        tax_due_fen=-9_629,
        basis_type="tax_authority_assessment",
    )
    preview, result = publish(company, "current-tax", "review-refund-proof")
    assert rows_for(preview)["tax-payment"]["impact"] == "review_no_impact"
    assert rows_for(result)["tax-payment"]["mode"] == "review_no_impact"
    assert journal_snapshot(company) == journal
    with company.engine.store.connection(read_only=True) as connection:
        source = company.engine.store.fact(connection, payment.fact_id)
    assert source.fact.direction == "inflow"
    book, record = load_book(company, "payment", "tax-payment")
    book.records[record.calculation.id] = replace(
        record,
        version=replace(source, fact=source.fact.model_copy(update={"direction": "outflow"})),
    )
    with pytest.raises(KernelError) as failure:
        book.signature(record.calculation.id)
    assert failure.value.code == "accounting_compatibility_required"
    assert failure.value.details["reason"] == "tax_settlement_treatment_mismatch"


def test_changed_original_income_reference_amount_preserves_actual_tax_and_paid_voucher(
    income_company,
):
    company = income_company
    actual = income_actual(company)
    publish(company, "current-tax", "initial-actual-income")
    original = company.current("income_tax_assessment", "current-tax")
    old_payment = pay_tax(company, "income_tax_assessment", "current-tax")
    original_trace = deepcopy(company.engine.trace(original.id)["calculation"])
    journal = journal_snapshot(company)
    reference_amount = original.values["cumulative_assessed_fen"] + 5_000
    with company.engine.store.connection(read_only=True) as connection:
        original_fact = company.engine.store.fact(connection, original.fact_id)
    saved = company.save(
        taxes.IncomeTaxAssessment,
        "current-tax",
        **original_fact.fact.model_dump(mode="json")
        | {"cumulative_assessed_fen": reference_amount},
    )
    preview, result = publish(company, "current-tax", "review-original-income-reference")
    for subject in ("current-tax", "tax-payment"):
        assert rows_for(preview)[subject]["impact"] == "review_no_impact"
        assert rows_for(result)[subject]["mode"] == "review_no_impact"
    current = company.current("income_tax_assessment", "current-tax")
    assert current.fact_id == saved["fact_id"] and current.fact_id != original.fact_id
    assert current.values["cumulative_assessed_fen"] == original.values["cumulative_assessed_fen"]
    assert current.values["calculated_assessment"]["cumulative_assessed_fen"] == reference_amount
    assert current.values["actual_declaration"]["fact_id"] == actual["fact_id"]
    assert company.current("payment", "tax-payment").fact_id == old_payment.fact_id
    assert journal_snapshot(company) == journal
    assert company.engine.trace(original.id)["calculation"] == original_trace
    with company.engine.store.connection(read_only=True) as connection:
        frozen = company.engine.store.fact(connection, original_fact.id)
    assert frozen.fact.cumulative_assessed_fen == original_fact.fact.cumulative_assessed_fen


def test_switched_reference_policy_ids_preserve_actual_tax_business_sources_and_paid_voucher(
    quarter_company,
):
    company = quarter_company
    actual = vat_actual(
        company,
        assessed_vat_fen=PAYABLE_VAT,
        vat_due_fen=PAYABLE_VAT,
        row_15_fen=PAYABLE_VAT,
    )
    publish(company, ASSESSMENT, "initial-actual-vat")
    original = company.current("tax_assessment", ASSESSMENT)
    old_payment = pay_tax(company, "tax_assessment", ASSESSMENT)
    original_trace = deepcopy(company.engine.trace(original.id)["calculation"])
    journal = journal_snapshot(company)
    with company.engine.store.connection(read_only=True) as connection:
        assessment = company.engine.store.fact(connection, original.fact_id)
        vat_policy = company.engine.store.current_fact(connection, "vat").fact.policy
        surtax_policy = company.engine.store.current_fact(connection, "surtax").fact.policy
    company.save(
        taxes.VatPolicyFact,
        "other-reference-vat",
        period="2026-07",
        policy=vat_policy.model_dump(mode="json")
        | {"version": "synthetic-other-reference-vat", "threshold_fen": 300_000_000},
    )
    company.save(
        taxes.SurtaxPolicyFact,
        "other-reference-surtax",
        period="2026-07",
        policy=surtax_policy.model_dump(mode="json")
        | {"version": "synthetic-other-reference-surtax", "urban_rate_percent": "9"},
    )
    saved = company.save(
        taxes.TaxAssessment,
        ASSESSMENT,
        **assessment.fact.model_dump(mode="json")
        | {"vat_policy_id": "other-reference-vat", "surtax_policy_id": "other-reference-surtax"},
    )
    preview, result = publish(company, ASSESSMENT, "review-switched-reference-policies")
    for subject in (ASSESSMENT, "tax-payment"):
        assert rows_for(preview)[subject]["impact"] == "review_no_impact"
        assert rows_for(result)[subject]["mode"] == "review_no_impact"
    current = company.current("tax_assessment", ASSESSMENT)
    assert current.fact_id == saved["fact_id"]
    assert current.values["payable_vat_fen"] == PAYABLE_VAT
    assert current.values["calculated_assessment"]["payable_vat_fen"] == 0
    assert current.values["actual_declaration"]["fact_id"] == actual["fact_id"]
    assert (
        current.values["vat_source_calculation_ids"]
        == original.values["vat_source_calculation_ids"]
    )
    assert company.current("payment", "tax-payment").fact_id == old_payment.fact_id
    assert journal_snapshot(company) == journal
    assert company.engine.trace(original.id)["calculation"] == original_trace
    with company.engine.store.connection(read_only=True) as connection:
        frozen = company.engine.store.fact(connection, assessment.id)
    assert frozen.fact.vat_policy_id == "vat" and frozen.fact.surtax_policy_id == "surtax"


def test_changed_accounting_month_remains_significant_in_projected_fact(actual_case):
    company, kind, subject, _ = actual_case
    book, record = load_book(company, kind, subject)
    changed = replace(
        record.version,
        fact=record.version.fact.model_copy(update={"period": YearMonth("2026-10")}),
    )
    candidate = add_candidate(book, record, json.loads(canonical(record.outcome)), version=changed)
    assert book.signature(record.calculation.id) != book.signature(candidate)


def test_tax_credit_scope_and_income_year_are_not_treated_as_reference_input(actual_case):
    company, kind, subject, _ = actual_case
    book, record = load_book(company, kind, subject)
    changed = replace(
        record.version,
        fact=record.version.fact.model_copy(
            update={"tax_credit_ids": ("additional-synthetic-credit",)}
            if kind == "tax_assessment"
            else {"year": 2025}
        ),
    )
    candidate = add_candidate(book, record, json.loads(canonical(record.outcome)), version=changed)
    if kind == "tax_assessment":
        assert book.signature(record.calculation.id) != book.signature(candidate)
    else:
        with pytest.raises(KernelError) as failure:
            book.signature(candidate)
        assert failure.value.code == "accounting_compatibility_required"
        assert failure.value.details["reason"] == "actual_tax_source_period_mismatch"


def test_unknown_owning_fact_fields_remain_significant_after_reference_projection(actual_case):
    company, kind, subject, _ = actual_case
    book, record = load_book(company, kind, subject)
    future_model = create_model(
        "FutureTaxAssessment",
        __base__=type(record.version.fact),
        future_accounting_meaning=(int, 7),
    )
    changed = replace(
        record.version,
        fact=future_model.model_validate_json(record.version.fact.model_dump_json()),
    )
    candidate = add_candidate(book, record, json.loads(canonical(record.outcome)), version=changed)
    assert book.signature(record.calculation.id) != book.signature(candidate)
