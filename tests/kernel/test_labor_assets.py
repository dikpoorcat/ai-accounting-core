"""Real publication: an advance and earned labor create one asset, one debt, two payments."""

import json

import pytest
from pydantic import ValidationError
from test_payroll_corrections import Company
from test_reimbursement_assets import book as asset_book
from test_reimbursement_assets import result
from test_reports import cit, profile

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.labor_assets import AssetAdvance, LaborProjectCost
from ai_accounting.kernel.exports import DEFAULT_CATEGORIES, EXPORT_KINDS, _export_obligations
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.workflow import SOURCES, ExternalObligation, Workflow


@pytest.fixture
def book(tmp_path):
    return asset_book.__wrapped__(tmp_path)


def advance(**changes):
    return {
        "period": "2026-09",
        "counterparty_id": "designer",
        "asset_type": "intangible",
        "amount_fen": 800_000,
        "contractual_obligation_established": True,
        **changes,
    }


def cost(**changes):
    return {
        "period": "2026-11",
        "person_id": "designer",
        "project_id": "ui-design",
        "gross_fee_fen": 1_600_000,
        "tax_treatment": "not_withheld_not_filed",
        "capitalization_conditions_confirmed": True,
        **changes,
    }


def allocation(kind, subject, name, amount):
    return {
        "source_kind": kind,
        "source_id": subject,
        "obligation": name,
        "amount_fen": amount,
    }


def payment(day, allocation_value, **changes):
    return {
        "period": day[:7],
        "actual_date": day,
        "bank_account_id": "bank",
        "counterparty_id": "designer",
        "direction": "outflow",
        "amount_fen": allocation_value["amount_fen"],
        "allocations": [allocation_value],
        **changes,
    }


def asset(cost_fen=1_600_000, **changes):
    return {
        "period": "2026-11",
        "asset_type": "intangible",
        "acquisition_date": "2026-11-30",
        "cost_fen": cost_fen,
        "acquisition_basis": "project_completion",
        "project_sources": [{"source_id": "labor-cost", "amount_fen": cost_fen}],
        **changes,
    }


def application(amount=800_000, **changes):
    return {
        "period": "2026-11",
        "settlement_kind": "advance_application",
        "first": allocation("asset_advance", "advance", "advance", amount),
        "second": allocation("labor_project_cost", "labor-cost", "net", amount),
        "offset_right_confirmed": True,
        **changes,
    }


def chain(book, *, activated=True):
    engine, save, publish = book
    save("asset_advance", "advance", advance())
    save(
        "payment",
        "deposit-paid",
        payment("2026-09-21", allocation("asset_advance", "advance", "primary", 800_000)),
    )
    publish("advance", "deposit-paid")
    save("labor_project_cost", "labor-cost", cost())
    save("asset", "asset", asset())
    save("settlement", "apply", application())
    save(
        "payment",
        "balance-paid",
        payment("2026-11-30", allocation("labor_project_cost", "labor-cost", "net", 800_000)),
    )
    published = publish("labor-cost", "asset", "apply", "balance-paid")
    if activated:
        save(
            "asset_activation",
            "activation",
            {
                "period": "2026-11",
                "asset_id": "asset",
                "in_use_date": "2026-11-30",
                "useful_life_months": 60,
                "residual_fen": 0,
                "benefit_area": "administration",
                "rounding_policy": "floor_final_remainder",
            },
        )
        publish("activation")
    return published


def test_complete_labor_asset_chain_has_one_cost_no_tax_debt_and_exact_cash(book):
    engine, _, _ = book
    chain(book)
    values = result(engine, "labor-cost")["values"]
    assert values["gross_fen"] == values["capitalized_fen"] == 1_600_000
    assert values["tax_assessed"] is False
    assert values["tax_review_required"] is True
    assert values["withholding_method"] == "not_withheld_not_filed"
    assert values["tax_fen"] == 0 and values["theoretical_tax_fen"] is None
    assert len(values["obligations"]) == 1
    assert values["obligations"][0]["cashflow"] == "asset_acquisition"
    assert result(engine, "asset")["values"]["obligations"] == []
    with engine.store.connection(read_only=True) as connection:
        balance = dict(connection.execute("SELECT balance_key,amount FROM balance"))
        assert (
            balance.get("asset_advance:advance:primary", 0)
            == balance.get("asset_advance:advance:advance", 0)
            == 0
        )
        assert balance.get("labor_project_cost:labor-cost:net", 0) == 0
        assert balance.get("project-cost:labor-cost", 0) == 0
        assert balance["asset:asset:carrying"] == 1_600_000
        assert (
            connection.execute(
                "SELECT sum(debit-credit) FROM voucher_line WHERE account='1701'"
            ).fetchone()[0]
            == 1_600_000
        )
        assert (
            connection.execute(
                "SELECT sum(debit-credit) FROM voucher_line WHERE account='189901'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT sum(credit) FROM voucher_line WHERE account='1002'"
            ).fetchone()[0]
            == 1_600_000
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM voucher_line WHERE account LIKE '5%' OR account='222103'"
            ).fetchone()[0]
            == 0
        )
        dates = {
            subject: str(engine.store.current_fact(connection, subject).fact.actual_date)
            for subject in ["deposit-paid", "balance-paid"]
        }
        assert dates == {"deposit-paid": "2026-09-21", "balance-paid": "2026-11-30"}
        assert connection.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    assert DEFAULT_CATEGORIES["labor_project_cost"] == "劳务"
    assert "labor_project_cost" in EXPORT_KINDS
    assert _export_obligations("labor_project_cost", values)[0]["amount_fen"] == 1_600_000


@pytest.mark.parametrize(
    "kind,data,field",
    [
        (
            "asset_advance",
            advance(contractual_obligation_established=None),
            "contractual_obligation_established",
        ),
        (
            "asset_advance",
            advance(contractual_obligation_established=False),
            "contractual_obligation_established",
        ),
        (
            "labor_project_cost",
            cost(capitalization_conditions_confirmed=None),
            "capitalization_conditions_confirmed",
        ),
        (
            "labor_project_cost",
            cost(capitalization_conditions_confirmed=False),
            "capitalization_conditions_confirmed",
        ),
        ("labor_project_cost", cost(gross_fee_fen=None), "gross_fee_fen"),
        ("labor_project_cost", cost(tax_treatment=None), "tax_treatment"),
    ],
)
def test_missing_authoritative_facts_block_without_default_or_partial_entries(
    book, kind, data, field
):
    engine, save, publish = book
    save(kind, "incomplete", data)
    with pytest.raises(NeedsInformation) as error:
        publish("incomplete")
    assert error.value.issues[0]["field"] == field
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM calculation").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM voucher").fetchone()[0] == 0


@pytest.mark.parametrize("model,data", [(AssetAdvance, advance()), (LaborProjectCost, cost())])
def test_public_types_do_not_accept_free_journals_or_implicit_business_dates(model, data):
    assert "actual_date" not in model.model_fields
    assert "expense_class" not in model.model_fields
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(data | {"account": "5602"}))
    amount_field = "amount_fen" if model is AssetAdvance else "gross_fee_fen"
    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(data | {amount_field: True}))


def test_same_labor_cost_cannot_create_another_asset_or_be_released_twice(book):
    engine, save, publish = book
    chain(book)
    save("asset", "duplicate", asset())
    before = engine.ledger("2026-11")
    with pytest.raises(KernelError) as error:
        publish("duplicate")
    assert error.value.code == "project_cost_overallocated"
    assert engine.ledger("2026-11") == before


def test_labor_source_reduction_marks_dependents_and_fails_atomically(book):
    engine, save, publish = book
    chain(book)
    before = engine.ledger("2026-11")
    save("labor_project_cost", "labor-cost", cost(gross_fee_fen=1_500_000), revision=1)
    with engine.store.connection(read_only=True) as connection:
        pending = {row[0] for row in connection.execute("SELECT subject_id FROM pending")}
    assert {"labor-cost", "asset", "apply", "balance-paid", "activation"} <= pending
    with pytest.raises(KernelError):
        publish("labor-cost", "asset", "apply", "balance-paid", "activation")
    assert engine.ledger("2026-11") == before


def test_prepaid_right_and_labor_debt_must_have_same_person(book):
    engine, save, publish = book
    save("asset_advance", "advance", advance(counterparty_id="another-person"))
    save("labor_project_cost", "labor-cost", cost())
    publish("advance", "labor-cost")
    save("settlement", "application", application())
    with pytest.raises(KernelError) as error:
        publish("application")
    assert error.value.code == "settlement_party"


def test_prepaid_right_cannot_clear_a_future_labor_debt_early(book):
    engine, save, publish = book
    save("asset_advance", "advance", advance())
    save("labor_project_cost", "labor-cost", cost())
    publish("advance", "labor-cost")
    save("settlement", "too-early", application(period="2026-09"))
    september = engine.ledger("2026-09")
    with pytest.raises(KernelError) as error:
        publish("too-early")
    assert error.value.code == "settlement_before_obligation"
    assert engine.ledger("2026-09") == september


def test_supplier_and_labor_costs_combine_once_for_the_same_project(book):
    engine, save, publish = book
    save("labor_project_cost", "labor-cost", cost(gross_fee_fen=1_000_000))
    save(
        "project_cost",
        "supplier-cost",
        {
            "period": "2026-11",
            "project_id": "ui-design",
            "supplier_id": "supplier",
            "amount_fen": 600_000,
            "project_nature": "purchased_intangible",
            "capitalization_conditions_confirmed": True,
        },
    )
    save(
        "asset",
        "asset",
        asset(
            project_sources=[
                {"source_id": "labor-cost", "amount_fen": 1_000_000},
                {"source_id": "supplier-cost", "amount_fen": 600_000},
            ]
        ),
    )
    publish("labor-cost", "supplier-cost", "asset")
    assert result(engine, "asset")["values"]["cost_fen"] == 1_600_000
    save(
        "project_release",
        "again",
        {
            "period": "2026-11",
            "project_id": "ui-design",
            "project_sources": [{"source_id": "labor-cost", "amount_fen": 1}],
            "expense_class": "administration",
        },
    )
    with pytest.raises(KernelError) as error:
        publish("again")
    assert error.value.code == "project_cost_overallocated"


@pytest.mark.parametrize(
    "cost_kind,cost_data",
    [
        (
            "expense",
            {
                "period": "2026-11",
                "counterparty_id": "designer",
                "amount_fen": 800_000,
                "expense_class": "administration",
                "creditor_kind": "individual",
            },
        ),
        (
            "asset",
            {
                "period": "2026-11",
                "asset_type": "fixed",
                "supplier_id": "designer",
                "acquisition_date": "2026-11-30",
                "cost_fen": 800_000,
                "acquisition_basis": "direct_purchase",
            },
        ),
    ],
)
def test_asset_advance_cannot_reclassify_an_operating_cost_or_another_asset_type(
    book, cost_kind, cost_data
):
    _, save, publish = book
    save("asset_advance", "advance", advance())
    save(cost_kind, "other-cost", cost_data)
    publish("advance", "other-cost")
    save(
        "settlement",
        "misuse",
        application(second=allocation(cost_kind, "other-cost", "primary", 800_000)),
    )
    with pytest.raises(KernelError) as error:
        publish("misuse")
    assert error.value.code == "asset_advance_purpose_conflict"


def test_closed_capitalized_labor_changes_only_through_open_compensation(tmp_path):
    company = Company(tmp_path / "closed.sqlite")
    company.save(LaborProjectCost(**cost(period="2026-01")), "labor-cost")
    company.publish("labor-cost")
    frozen = company.close("2026-01")
    ledger = company.engine.ledger("2026-01")
    company.save(
        LaborProjectCost(**cost(period="2026-01", gross_fee_fen=1_700_000)),
        "labor-cost",
        revision=1,
    )
    with pytest.raises(KernelError) as error:
        company.publish("labor-cost")
    assert error.value.code == "closed_correction_required"
    company.publish("labor-cost", correction_period="2026-02")
    assert company.engine.ledger("2026-01") == ledger
    assert Periods(company.engine).closed_report("2026-01") == frozen
    assert len(company.engine.ledger("2026-02")) == 2


def test_labor_filing_basis_includes_capitalized_cost_without_social_or_fake_completion(tmp_path):
    company = Company(tmp_path / "filing.sqlite")
    company.save(LaborProjectCost(**cost()), "labor-cost")
    company.publish("labor-cost")
    company.save(
        ExternalObligation(
            period="2026-11",
            obligation_kind="individual_income_tax",
            start_period="2026-11",
            end_period="2026-11",
            applicability_confirmed=True,
            due_date=None,
        ),
        "filing",
    )
    basis = Workflow(company.engine).obligation_basis("filing")
    assert basis["accepted_calculations"] == [
        {
            "subject_id": "labor-cost",
            "calculation_id": company.current("labor-cost", "labor_project_cost").id,
        }
    ]
    assert "labor_project_cost" not in SOURCES["contribution_declaration"]


def test_real_reports_show_both_cash_payments_as_investing_without_expense(book):
    engine, save, publish = book
    profile(save, publish)
    cit(save, publish, "2026-03")
    cit(save, publish, "2026-06")
    cit(save, publish, "2026-09")
    cit(save, publish, "2026-12")
    save(
        "funding",
        "capital",
        {
            "period": "2026-09",
            "owner_id": "owner",
            "amount_fen": 1_600_000,
            "funding_kind": "capital",
            "actual_date": "2026-09-01",
            "bank_account_id": "bank",
        },
    )
    publish("capital")
    chain(book, activated=False)
    september = Reports(engine).report(2026, 3)
    november = Reports(engine).report(2026, 4)
    assert september["status"] == november["status"] == "ready", (
        september["fact_issues"],
        november["fact_issues"],
    )
    assert september["statements"]["cash_flow_statement"]["12"]["current_fen"] == 800_000
    assert november["statements"]["cash_flow_statement"]["12"]["current_fen"] == 800_000
    assert november["statements"]["balance_sheet"]["28"]["ending_fen"] == 1_600_000
    assert november["statements"]["profit_statement"]["14"]["current_fen"] == 0
    assert november["statements"]["cash_flow_statement"]["6"]["current_fen"] == 0
