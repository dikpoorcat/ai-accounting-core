from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from ai_accounting.borrowing_schemas import (
    BorrowingTermFacts,
    DrawBorrowingRequest,
)
from ai_accounting.domain_action_schemas import (
    BorrowingDrawdownFacts,
    FixedAssetAcquisitionFacts,
    IntangibleAssetAcquisitionFacts,
)
from ai_accounting.fixed_assets import calculate_acquisition_cost as fixed_asset_cost
from ai_accounting.intangible_asset_schemas import AcquireIntangibleAssetRequest
from ai_accounting.intangible_assets import (
    calculate_acquisition_cost as intangible_asset_cost,
)
from ai_accounting.schemas import AcquireFixedAssetRequest


def test_fixed_asset_accepts_total_cost_without_management_or_breakdown() -> None:
    request = AcquireFixedAssetRequest.model_validate(
        {
            "org_id": str(uuid.uuid4()),
            "idempotency_key": "fixed-minimal",
            "category": "electronic",
            "expected_use_over_one_year": True,
            "purchase_date": "2026-09-01",
            "posting_date": "2026-09-01",
            "cost_fen": 120_000,
            "settlement_method": "payable",
            "claims_creditable_input_vat": False,
            "evidence_references": [str(uuid.uuid4())],
        }
    )

    assert request.missing_information() == []
    assert request.asset_code is None
    assert request.asset_name is None
    assert request.supplier is None
    assert request.due_date is None
    assert fixed_asset_cost(cost_fen=120_000).purchase_price_fen is None


def test_fixed_asset_rejects_inconsistent_optional_breakdown() -> None:
    with pytest.raises(ValidationError, match="sum exactly to cost_fen"):
        AcquireFixedAssetRequest.model_validate(
            {
                "org_id": str(uuid.uuid4()),
                "idempotency_key": "fixed-breakdown",
                "cost_fen": 100,
                "cost_components": {"purchase_price_fen": 99},
            }
        )


def test_intangible_asset_accepts_total_cost_without_management_or_breakdown() -> None:
    request = AcquireIntangibleAssetRequest.model_validate(
        {
            "org_id": str(uuid.uuid4()),
            "idempotency_key": "intangible-minimal",
            "category": "software",
            "acquisition_date": "2026-09-01",
            "available_for_use_date": "2026-09-01",
            "posting_date": "2026-09-01",
            "cost_fen": 240_000,
            "settlement_method": "payable",
            "benefit_area": "management",
            "life_basis": "reliably_estimated",
            "useful_life_months": 24,
            "is_available_for_use": True,
            "claims_creditable_input_vat": False,
            "evidence_references": [str(uuid.uuid4())],
        }
    )

    assert request.missing_information() == []
    assert request.asset_code is None
    assert request.asset_name is None
    assert request.rights_description is None
    assert request.supplier is None
    assert request.due_date is None
    assert intangible_asset_cost(cost_fen=240_000).purchase_price_fen is None


def test_borrowing_accepts_current_calculation_facts_without_management_schedule() -> None:
    request = DrawBorrowingRequest.model_validate(
        {
            "org_id": str(uuid.uuid4()),
            "idempotency_key": "borrowing-minimal",
            "lender": {"name": "持证银行"},
            "lender_is_licensed_financial_institution": True,
            "currency": "CNY",
            "principal_fen": 10_000_000,
            "drawdown_date": "2026-09-01",
            "due_date": "2027-09-01",
            "posting_date": "2026-09-01",
            "annual_rate_percent": "3.5",
            "day_count_basis": "actual_365",
            "capitalization_applicable": False,
            "term_facts": {
                "single_drawdown": True,
                "fixed_rate": True,
                "simple_interest": True,
                "bullet_principal_at_maturity": True,
            },
            "bank_account_code": "1002-01",
            "evidence_references": [str(uuid.uuid4())],
        }
    )

    assert request.missing_information() == []
    assert request.borrowing_code is None
    assert request.contract_name is None
    assert request.purpose_description is None
    assert request.interest_due_dates is None


def test_future_borrowing_clauses_do_not_block_current_fixed_simple_draw() -> None:
    terms = BorrowingTermFacts(
        single_drawdown=True,
        fixed_rate=True,
        simple_interest=True,
        bullet_principal_at_maturity=True,
        allows_prepayment=True,
        allows_extension=True,
        has_penalty_interest=True,
        has_financing_fees=True,
    )

    assert terms.is_phase_one_supported() is True


def test_public_domain_facts_exclude_management_fields() -> None:
    assert "asset_code" not in FixedAssetAcquisitionFacts.model_fields
    assert "asset_name" not in FixedAssetAcquisitionFacts.model_fields
    assert "supplier" not in FixedAssetAcquisitionFacts.model_fields
    assert "asset_code" not in IntangibleAssetAcquisitionFacts.model_fields
    assert "rights_description" not in IntangibleAssetAcquisitionFacts.model_fields
    assert "life_basis_explanation" not in IntangibleAssetAcquisitionFacts.model_fields
    assert "borrowing_code" not in BorrowingDrawdownFacts.model_fields
    assert "contract_name" not in BorrowingDrawdownFacts.model_fields
    assert "purpose_description" not in BorrowingDrawdownFacts.model_fields
    assert "interest_due_dates" not in BorrowingDrawdownFacts.model_fields
    source_schema = FixedAssetAcquisitionFacts.model_json_schema()["$defs"][
        "FixedAssetEmployeeCostSourceFacts"
    ]["properties"]
    assert "due_date" not in source_schema
    assert "description" not in source_schema
