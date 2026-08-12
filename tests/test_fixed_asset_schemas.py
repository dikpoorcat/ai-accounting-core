from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from pydantic import ValidationError

from ai_accounting.schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    ConfirmFixedAssetDepreciationRequest,
    DisposeFixedAssetRequest,
    FixedAssetAcquisitionSettlementKind,
    FixedAssetBenefitArea,
    FixedAssetCategory,
    FixedAssetDisposalKind,
    FixedAssetDisposalSettlementKind,
    PreviewFixedAssetDepreciationRequest,
)


def _id() -> str:
    return str(uuid.uuid4())


def _acquisition_payload() -> dict[str, object]:
    return {
        "org_id": _id(),
        "idempotency_key": "fixed-asset-acquisition",
        "asset_code": "LAPTOP-001",
        "asset_name": "Laptop",
        "category": "electronic",
        "expected_use_over_one_year": True,
        "purchase_date": "2026-01-10",
        "posting_date": "2026-01-10",
        "cost_components": {
            "purchase_price_fen": 100_000,
            "noncreditable_tax_fen": 3_000,
            "transport_and_handling_fen": 0,
            "installation_and_direct_cost_fen": 0,
        },
        "supplier": {"kind": "supplier", "name": "Supplier"},
        "settlement_method": "bank",
        "bank_account_code": "1002",
        "payment_date": "2026-01-10",
        "evidence_references": [_id()],
        "bank_transaction_references": [{"id": _id()}],
        "claims_creditable_input_vat": False,
    }


def test_fixed_asset_enums_are_finite_and_match_frozen_public_contract() -> None:
    assert {item.value for item in FixedAssetCategory} == {
        "production_equipment",
        "tools_furniture",
        "transport",
        "electronic",
        "other_movable_tangible",
    }
    assert {item.value for item in FixedAssetBenefitArea} == {
        "management",
        "sales",
        "service_delivery",
    }
    assert {item.value for item in FixedAssetAcquisitionSettlementKind} == {"bank", "payable"}
    assert {item.value for item in FixedAssetDisposalKind} == {"sale", "retirement"}
    assert {item.value for item in FixedAssetDisposalSettlementKind} == {
        "bank",
        "receivable",
        "none",
    }


def test_acquisition_schema_requires_explicit_facts_without_defaulting_treatment() -> None:
    request = AcquireFixedAssetRequest(org_id=_id(), idempotency_key="missing-facts")

    requirements = {item.code: set(item.fields) for item in request.missing_information()}

    assert requirements["FIXED_ASSET_IDENTITY_REQUIRED"] == {"asset_code", "asset_name", "category"}
    assert requirements["FIXED_ASSET_COST_COMPONENTS_REQUIRED"] == {
        "cost_components.purchase_price_fen",
        "cost_components.noncreditable_tax_fen",
        "cost_components.transport_and_handling_fen",
        "cost_components.installation_and_direct_cost_fen",
    }
    assert requirements["FIXED_ASSET_INPUT_VAT_TREATMENT_REQUIRED"] == {
        "claims_creditable_input_vat"
    }


def test_bank_acquisition_requires_payment_and_account_but_payable_requires_due_date() -> None:
    bank_payload = _acquisition_payload()
    bank_payload.pop("payment_date")
    bank_payload["bank_transaction_references"] = []
    bank = AcquireFixedAssetRequest.model_validate(bank_payload)
    bank_codes = {item.code for item in bank.missing_information()}
    assert "FIXED_ASSET_PAYMENT_DATE_REQUIRED" in bank_codes
    assert "FIXED_ASSET_BANK_TRANSACTIONS_REQUIRED" not in bank_codes

    payable_payload = _acquisition_payload()
    payable_payload["settlement_method"] = "payable"
    payable_payload.pop("payment_date")
    payable_payload.pop("bank_account_code")
    payable_payload["bank_transaction_references"] = []
    payable = AcquireFixedAssetRequest.model_validate(payable_payload)
    assert "FIXED_ASSET_DUE_DATE_REQUIRED" in {item.code for item in payable.missing_information()}


def test_fixed_asset_schemas_forbid_extra_fields_and_float_money() -> None:
    payload = _acquisition_payload()
    payload["cost_components"] = {**payload["cost_components"], "purchase_price_fen": 1.5}
    with pytest.raises(ValidationError):
        AcquireFixedAssetRequest.model_validate(payload)

    for field_name in ("expected_use_over_one_year", "claims_creditable_input_vat"):
        payload = _acquisition_payload()
        payload[field_name] = "false"
        with pytest.raises(ValidationError):
            AcquireFixedAssetRequest.model_validate(payload)

    payload = _acquisition_payload()
    payload["arbitrary_credit_line"] = {"credit_fen": 100}
    with pytest.raises(ValidationError):
        AcquireFixedAssetRequest.model_validate(payload)


def test_activation_requires_at_least_thirteen_month_life_and_explicit_residual() -> None:
    with pytest.raises(ValidationError):
        ActivateFixedAssetRequest.model_validate(
            {
                "org_id": _id(),
                "asset_id": _id(),
                "idempotency_key": "activation",
                "activation_date": "2026-01-31",
                "posting_date": "2026-01-31",
                "useful_life_months": 12,
                "residual_value_fen": 0,
                "benefit_area": "management",
                "evidence_references": [_id()],
            }
        )

    request = ActivateFixedAssetRequest(org_id=_id(), idempotency_key="missing-activation")
    assert request.missing_information()[0].code == "FIXED_ASSET_ACTIVATION_FACTS_REQUIRED"


def test_preview_is_read_only_and_confirmation_adds_its_own_idempotency_key() -> None:
    preview = PreviewFixedAssetDepreciationRequest.model_validate(
        {
            "org_id": _id(),
            "asset_id": _id(),
            "depreciation_period": "2026-02",
            "posting_date": "2026-02-28",
        }
    )
    assert preview.missing_information() == []
    preview_properties = PreviewFixedAssetDepreciationRequest.model_json_schema()["properties"]
    assert "idempotency_key" not in preview_properties

    confirmation = ConfirmFixedAssetDepreciationRequest.model_validate(
        {
            "org_id": _id(),
            "asset_id": _id(),
            "depreciation_period": "2026-02",
            "posting_date": "2026-02-28",
            "idempotency_key": "confirm-february",
        }
    )
    assert confirmation.missing_information()[-1].code == "FIXED_ASSET_CONFIRMATION_REQUIRED"
    with pytest.raises(ValidationError):
        PreviewFixedAssetDepreciationRequest(
            org_id=uuid.uuid4(), asset_id=uuid.uuid4(), depreciation_period="2026-13"
        )


def test_disposal_schema_enforces_sale_and_retirement_boundaries() -> None:
    base = {
        "org_id": _id(),
        "asset_id": _id(),
        "idempotency_key": "dispose",
        "disposal_date": "2026-03-31",
        "posting_date": "2026-03-31",
        "clearance_cost_fen": 0,
        "evidence_references": [_id()],
    }
    retirement = DisposeFixedAssetRequest.model_validate(
        {**base, "disposal_kind": "retirement", "settlement_method": "none"}
    )
    assert retirement.missing_information() == []

    with pytest.raises(ValidationError):
        DisposeFixedAssetRequest.model_validate(
            {**base, "disposal_kind": "retirement", "settlement_method": "bank"}
        )
    with pytest.raises(ValidationError):
        DisposeFixedAssetRequest.model_validate(
            {
                **base,
                "disposal_kind": "retirement",
                "settlement_method": "none",
                "invoice_type": "none",
            }
        )
    with pytest.raises(ValidationError):
        DisposeFixedAssetRequest.model_validate(
            {**base, "disposal_kind": "sale", "settlement_method": "none"}
        )
    with pytest.raises(ValidationError):
        DisposeFixedAssetRequest.model_validate(
            {
                **base,
                "disposal_kind": "sale",
                "gross_proceeds_fen": 103,
                "invoice_type": "ordinary",
                "waive_exemption": "false",
                "settlement_method": "bank",
                "customer": {"kind": "customer", "name": "Buyer"},
                "tax_obligation_date": "2026-03-31",
            }
        )


def test_bank_receipt_or_bank_clearance_requires_bank_account() -> None:
    sale = DisposeFixedAssetRequest.model_validate(
        {
            "org_id": _id(),
            "asset_id": _id(),
            "idempotency_key": "bank-sale",
            "disposal_date": "2026-03-31",
            "posting_date": "2026-03-31",
            "disposal_kind": "sale",
            "gross_proceeds_fen": 10_300,
            "invoice_type": "ordinary",
            "waive_exemption": False,
            "settlement_method": "bank",
            "customer": {"kind": "customer", "name": "Buyer"},
            "tax_obligation_date": "2026-03-31",
            "clearance_cost_fen": 1,
            "evidence_references": [_id()],
        }
    )
    requirements = sale.missing_information()
    assert [item.code for item in requirements].count("FIXED_ASSET_BANK_ACCOUNT_REQUIRED") == 1


def test_retirement_settlement_requirement_is_not_hidden_by_clearance_cost() -> None:
    retirement = DisposeFixedAssetRequest.model_validate(
        {
            "org_id": _id(),
            "asset_id": _id(),
            "idempotency_key": "retirement-clearance",
            "disposal_date": "2026-03-31",
            "posting_date": "2026-03-31",
            "disposal_kind": "retirement",
            "clearance_cost_fen": 100,
            "bank_account_code": "1002",
            "evidence_references": [_id()],
        }
    )

    assert [item.code for item in retirement.missing_information()] == [
        "FIXED_ASSET_RETIREMENT_SETTLEMENT_REQUIRED",
    ]


def test_public_fixed_asset_schema_has_no_free_entry_fields() -> None:
    schema_text = json.dumps(
        {
            "acquisition": AcquireFixedAssetRequest.model_json_schema(),
            "activation": ActivateFixedAssetRequest.model_json_schema(),
            "preview": PreviewFixedAssetDepreciationRequest.model_json_schema(),
            "confirmation": ConfirmFixedAssetDepreciationRequest.model_json_schema(),
            "disposal": DisposeFixedAssetRequest.model_json_schema(),
        },
        ensure_ascii=False,
    )
    assert "debit_fen" not in schema_text
    assert "credit_fen" not in schema_text
    assert '"account_code":' not in schema_text
    assert date.today().isoformat() not in schema_text
