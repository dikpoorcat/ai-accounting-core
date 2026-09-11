"""Alternative representations of one amount cannot create extra original-row capacity."""

from typing import ClassVar

import pytest
from test_materials import Company, codes

from ai_accounting.kernel.contracts import Fact, Line, Outcome
from ai_accounting.kernel.domains.transactions import BankIncome, ProjectCost
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.materials import Materials
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import PositiveFen


class TwoAmounts(Fact):
    kind: ClassVar[str] = "test_two_amounts"
    first_fen: PositiveFen
    second_fen: PositiveFen


def company_with_evaluator(tmp_path, evaluator):
    registry = default_registry()
    registry.register(TwoAmounts, evaluator)
    company = object.__new__(Company)
    company.engine = Engine(
        Store.create(tmp_path / "company.sqlite", registry, "company", "tax", "db")
    )
    company.materials = Materials(company.engine)
    company.sequence = 0
    company.proof = company.evidence(b"Synthetic declared original facts")
    return company


def publish(company, fact):
    saved = company.engine.save_fact(
        fact.kind,
        "business",
        fact.model_dump(mode="json"),
        evidence=(company.proof,),
        expected_revision=0,
        request_id=company.request(),
    )
    preview = company.engine.preview(["business"])
    result = company.engine.confirm(
        ["business"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )["results"][0]
    return dict(
        subject_id="business",
        fact_kind=fact.kind,
        fact_id=saved["fact_id"],
        calculation_id=result["calculation_id"],
        amount_fen=1000,
        recognition_period="2026-01",
    )


@pytest.mark.parametrize(
    "fact,second_field",
    [
        (
            BankIncome(
                period="2026-01",
                actual_date="2026-01-01",
                bank_account_id="bank",
                amount_fen=1000,
                income_kind="bank_interest",
                counterparty_id="bank",
                entitlement_confirmed=True,
            ),
            "result.amount_fen",
        ),
        (
            ProjectCost(
                period="2026-01",
                amount_fen=1000,
                supplier_id="vendor",
                project_id="project",
                project_nature="internal_development",
                capitalization_conditions_confirmed=True,
            ),
            "result.capitalized_fen",
        ),
    ],
)
def test_same_amount_fact_and_result_aliases_share_one_capacity(tmp_path, fact, second_field):
    company = Company(tmp_path)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\nb,10,2026-01\n")
    link = publish(company, fact)
    company.resolve(source, "CSV!B2", [{**link, "amount_field": "fact.amount_fen"}])
    company.resolve(source, "CSV!B3", [{**link, "amount_field": second_field}])
    result = company.materials.check("2026-01")
    assert "material_business_overallocated" in codes(result)
    assert result["file_status"] == "needs_information"


def test_equal_but_independent_amount_dimensions_are_not_merged(tmp_path):
    def calculate(version, context):
        fact = version.fact
        amount = fact.first_fen + fact.second_fen
        return Outcome(
            (Line("5602", debit=amount), Line("2202", credit=amount)),
            {"first_fen": fact.first_fen, "second_fen": fact.second_fen},
        )

    company = company_with_evaluator(tmp_path, calculate)
    source, _ = company.source(b"name,amount,period\na,10,2026-01\nb,10,2026-01\n")
    link = publish(company, TwoAmounts(period="2026-01", first_fen=1000, second_fen=1000))
    company.resolve(source, "CSV!B2", [{**link, "amount_field": "fact.first_fen"}])
    company.resolve(source, "CSV!B3", [{**link, "amount_field": "result.second_fen"}])
    assert company.materials.check("2026-01")["status"] == "complete"


def test_same_named_fact_result_disagreement_cannot_select_a_convenient_value(tmp_path):
    company = company_with_evaluator(
        tmp_path,
        lambda version, context: Outcome(
            (Line("5602", debit=1000), Line("2202", credit=1000)), {"first_fen": 999}
        ),
    )
    source, _ = company.source(b"name,amount,period\na,10,2026-01\n")
    link = publish(company, TwoAmounts(period="2026-01", first_fen=1000, second_fen=1000))
    company.resolve(source, "CSV!B2", [{**link, "amount_field": "fact.first_fen"}])
    assert "material_amount_basis_conflict" in codes(company.materials.check("2026-01"))


def test_explicit_cross_named_disagreement_is_detected(tmp_path):
    registry = default_registry()
    from ai_accounting.kernel.contracts import Calculation
    from ai_accounting.kernel.materials import _amount_basis

    model = registry.models["project_cost"](
        period="2026-01",
        amount_fen=1000,
        supplier_id="vendor",
        project_id="project",
        project_nature="internal_development",
        capitalization_conditions_confirmed=True,
    )
    calculation = Calculation(
        "calculation",
        "project",
        "project_cost",
        model.period,
        {"capitalized_fen": 2000},
        "fact",
        "00" * 32,
    )
    with pytest.raises(ValueError, match="同一业务金额"):
        _amount_basis(model, calculation, "fact.amount_fen")
