from __future__ import annotations

import uuid
from dataclasses import asdict

import pytest
from pydantic import ValidationError

from ai_accounting.intangible_asset_schemas import (
    MAX_FEN,
    AcquireIntangibleAssetRequest,
    PreviewIntangibleAssetAmortizationRequest,
    RetireIntangibleAssetRequest,
)
from ai_accounting.intangible_assets import (
    IntangibleAssetCalculationError,
    calculate_acquisition_cost,
    calculate_straight_line_amortization,
    intangible_asset_calculation_hash,
)


def test_acquisition_cost_is_strict_integer_fen() -> None:
    result = calculate_acquisition_cost(
        purchase_price_fen=12_000,
        noncreditable_tax_fen=360,
        directly_attributable_cost_fen=640,
    )
    assert result.cost_fen == 13_000

    for bad in (True, 1.0, "1", -1):
        with pytest.raises(IntangibleAssetCalculationError) as exc:
            calculate_acquisition_cost(
                purchase_price_fen=bad,  # type: ignore[arg-type]
                noncreditable_tax_fen=0,
                directly_attributable_cost_fen=0,
            )
        assert exc.value.code == "INVALID_FEN"

    with pytest.raises(IntangibleAssetCalculationError) as exc:
        calculate_acquisition_cost(
            purchase_price_fen=MAX_FEN,
            noncreditable_tax_fen=1,
            directly_attributable_cost_fen=0,
        )
    assert exc.value.code == "INTANGIBLE_ASSET_COST_OUT_OF_RANGE"


def test_amortization_remainder_closes_exactly_in_final_month() -> None:
    cost = 1_205
    life = 12
    results = [
        calculate_straight_line_amortization(
            cost_fen=cost,
            useful_life_months=life,
            completed_months=index,
            opening_accumulated_amortization_fen=(cost // life) * index,
        )
        for index in range(life)
    ]
    assert [item.amortization_fen for item in results[:-1]] == [100] * 11
    assert results[-1].amortization_fen == 105
    assert sum(item.amortization_fen for item in results) == cost
    assert results[-1].closing_accumulated_amortization_fen == cost
    assert results[-1].residual_value_fen == 0


def test_amortization_rejects_zero_month_policy_and_noncontinuous_opening() -> None:
    with pytest.raises(IntangibleAssetCalculationError) as exc:
        calculate_straight_line_amortization(
            cost_fen=11,
            useful_life_months=12,
            completed_months=0,
            opening_accumulated_amortization_fen=0,
        )
    assert exc.value.code == "INTANGIBLE_ASSET_INVALID_AMORTIZATION_POLICY"

    with pytest.raises(IntangibleAssetCalculationError) as exc:
        calculate_straight_line_amortization(
            cost_fen=120,
            useful_life_months=12,
            completed_months=2,
            opening_accumulated_amortization_fen=19,
        )
    assert exc.value.code == "INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE"


def test_calculation_hash_covers_command_request_and_result_canonically() -> None:
    calculation = calculate_straight_line_amortization(
        cost_fen=1_205,
        useful_life_months=12,
        completed_months=0,
        opening_accumulated_amortization_fen=0,
    )
    request = {
        "org_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "period": "2026-01",
    }
    first = intangible_asset_calculation_hash(
        command="finance_preview_intangible_asset_amortization",
        request=request,
        calculation=calculation,
    )
    second = intangible_asset_calculation_hash(
        command="finance_preview_intangible_asset_amortization",
        request={"period": "2026-01", "org_id": request["org_id"]},
        calculation=asdict(calculation),
    )
    changed = intangible_asset_calculation_hash(
        command="finance_preview_intangible_asset_amortization",
        request={**request, "period": "2026-02"},
        calculation=calculation,
    )
    assert first == second
    assert first != changed
    assert len(first) == 64


def test_acquisition_schema_keeps_missing_treatment_facts_explicit() -> None:
    request = AcquireIntangibleAssetRequest(
        org_id=uuid.uuid4(), idempotency_key="intangible-missing"
    )
    requirements = {item.code: item.fields for item in request.missing_information()}
    assert "INTANGIBLE_ASSET_COST_COMPONENTS_REQUIRED" in requirements
    assert "is_available_for_use" in requirements["INTANGIBLE_ASSET_POLICY_FACTS_REQUIRED"]
    assert (
        "claims_creditable_input_vat"
        in requirements["INTANGIBLE_ASSET_POLICY_FACTS_REQUIRED"]
    )

    with pytest.raises(ValidationError):
        AcquireIntangibleAssetRequest.model_validate(
            {
                "org_id": uuid.uuid4(),
                "idempotency_key": "strict-fen",
                "cost_components": {
                    "purchase_price_fen": True,
                    "noncreditable_tax_fen": 0,
                    "directly_attributable_cost_fen": 0,
                },
            }
        )


def test_retirement_requires_each_zero_income_fact_without_defaults() -> None:
    request = RetireIntangibleAssetRequest(
        org_id=uuid.uuid4(), idempotency_key="retirement-missing"
    )
    requirement = next(
        item
        for item in request.missing_information()
        if item.code == "INTANGIBLE_ASSET_RETIREMENT_FACTS_REQUIRED"
    )
    assert {
        "gross_proceeds_fen",
        "compensation_fen",
        "taxes_and_fees_fen",
        "residual_proceeds_fen",
    } <= set(requirement.fields)


def test_schema_strips_required_text_and_enforces_storage_bounds() -> None:
    request = AcquireIntangibleAssetRequest.model_validate(
        {
            "org_id": uuid.uuid4(),
            "idempotency_key": "strict-text",
            "asset_code": "  IA-STRIP  ",
            "asset_name": "  软件许可  ",
            "rights_description": "  可辨认许可权  ",
            "life_basis_explanation": "  合同十二个月  ",
            "supplier": {
                "kind": " supplier ",
                "name": "  供应商  ",
                "external_ref": " EXT-1 ",
            },
        }
    )
    assert request.asset_code == "IA-STRIP"
    assert request.asset_name == "软件许可"
    assert request.supplier.kind == "supplier"
    assert request.supplier.name == "供应商"
    assert request.supplier.external_ref == "EXT-1"

    for field_name in (
        "asset_code",
        "asset_name",
        "rights_description",
        "life_basis_explanation",
        "other_right_type_description",
        "identifiability_basis",
    ):
        with pytest.raises(ValidationError):
            AcquireIntangibleAssetRequest.model_validate(
                {
                    "org_id": uuid.uuid4(),
                    "idempotency_key": f"blank-{field_name}",
                    field_name: "   ",
                }
            )
    for supplier in (
        {"kind": "supplier", "name": "x" * 201},
        {"kind": "supplier", "name": "supplier", "external_ref": "x" * 101},
    ):
        with pytest.raises(ValidationError):
            AcquireIntangibleAssetRequest.model_validate(
                {
                    "org_id": uuid.uuid4(),
                    "idempotency_key": "oversized-supplier",
                    "supplier": supplier,
                }
            )
    with pytest.raises(ValidationError):
        AcquireIntangibleAssetRequest.model_validate(
            {
                "org_id": uuid.uuid4(),
                "idempotency_key": "oversized-fen",
                "cost_components": {
                    "purchase_price_fen": MAX_FEN + 1,
                    "noncreditable_tax_fen": 0,
                    "directly_attributable_cost_fen": 0,
                },
            }
        )
    with pytest.raises(ValidationError):
        AcquireIntangibleAssetRequest.model_validate(
            {
                "org_id": uuid.uuid4(),
                "idempotency_key": "oversized-life",
                "useful_life_months": 119_989,
            }
        )
    with pytest.raises(ValidationError):
        PreviewIntangibleAssetAmortizationRequest(
            org_id=uuid.uuid4(),
            amortization_period="0000-01",
        )

    schema = AcquireIntangibleAssetRequest.model_json_schema()
    supplier_schema = schema["$defs"]["IntangibleAssetSupplierReference"]
    assert supplier_schema["additionalProperties"] is False
    assert supplier_schema["properties"]["name"]["anyOf"][0]["maxLength"] == 200
    assert supplier_schema["properties"]["external_ref"]["anyOf"][0]["maxLength"] == 100
