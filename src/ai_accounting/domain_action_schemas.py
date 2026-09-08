"""Business-only asset and borrowing facts, derived from their domain contracts.

The parent component supplies company, dates, evidence and execution identity.
Field definitions and validators are shared with the internal domain requests;
no second copy of monetary, enum or textual validation is maintained here.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model, field_validator

from .borrowing_schemas import ConfirmBorrowingInterestRequest, DrawBorrowingRequest
from .intangible_asset_schemas import (
    AcquireIntangibleAssetRequest,
    ConfirmIntangibleAssetAmortizationRequest,
    RetireIntangibleAssetRequest,
)
from .schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    ConfirmFixedAssetDepreciationBatchRequest,
    ConfirmFixedAssetDepreciationRequest,
    DisposeFixedAssetRequest,
)

_PARENT_FIELDS = {
    "org_id",
    "idempotency_key",
    "posting_date",
    "business_date",
    "payment_date",
    "description",
    "evidence_references",
    "bank_account_code",
    "bank_transaction_references",
    "purchase_date",
    "acquisition_date",
    "activation_date",
    "drawdown_date",
    "disposal_date",
    "retirement_date",
}


def _facts_model(name: str, request_type: type[BaseModel]) -> type[BaseModel]:
    fields = {
        key: (field.annotation, deepcopy(field))
        for key, field in request_type.model_fields.items()
        if key not in _PARENT_FIELDS
    }
    validators = {}
    for key, decorator in request_type.__pydantic_decorators__.field_validators.items():
        names = tuple(name for name in decorator.info.fields if name in fields)
        if names:
            function = getattr(decorator.func, "__func__", decorator.func)
            validators[key] = field_validator(*names, mode=decorator.info.mode)(function)
    return create_model(
        name,
        __config__=ConfigDict(extra="forbid"),
        __module__=__name__,
        __validators__=validators,
        **fields,
    )


FixedAssetAcquisitionFacts = _facts_model("FixedAssetAcquisitionFacts", AcquireFixedAssetRequest)
FixedAssetActivationFacts = _facts_model("FixedAssetActivationFacts", ActivateFixedAssetRequest)
FixedAssetDepreciationFacts = _facts_model(
    "FixedAssetDepreciationFacts", ConfirmFixedAssetDepreciationRequest
)
FixedAssetDepreciationBatchFacts = _facts_model(
    "FixedAssetDepreciationBatchFacts", ConfirmFixedAssetDepreciationBatchRequest
)
FixedAssetDisposalFacts = _facts_model("FixedAssetDisposalFacts", DisposeFixedAssetRequest)
IntangibleAssetAcquisitionFacts = _facts_model(
    "IntangibleAssetAcquisitionFacts", AcquireIntangibleAssetRequest
)
IntangibleAssetAmortizationFacts = _facts_model(
    "IntangibleAssetAmortizationFacts", ConfirmIntangibleAssetAmortizationRequest
)
IntangibleAssetRetirementFacts = _facts_model(
    "IntangibleAssetRetirementFacts", RetireIntangibleAssetRequest
)
BorrowingDrawdownFacts = _facts_model("BorrowingDrawdownFacts", DrawBorrowingRequest)
BorrowingInterestAccrualFacts = _facts_model(
    "BorrowingInterestAccrualFacts", ConfirmBorrowingInterestRequest
)

DOMAIN_REQUEST_TYPES = {
    "fixed_asset_acquisition": AcquireFixedAssetRequest,
    "fixed_asset_activation": ActivateFixedAssetRequest,
    "fixed_asset_depreciation": ConfirmFixedAssetDepreciationRequest,
    "fixed_asset_depreciation_batch": ConfirmFixedAssetDepreciationBatchRequest,
    "fixed_asset_disposal": DisposeFixedAssetRequest,
    "intangible_asset_acquisition": AcquireIntangibleAssetRequest,
    "intangible_asset_amortization": ConfirmIntangibleAssetAmortizationRequest,
    "intangible_asset_retirement": RetireIntangibleAssetRequest,
    "borrowing_drawdown": DrawBorrowingRequest,
    "borrowing_interest_accrual": ConfirmBorrowingInterestRequest,
}


class DomainFactsRequired(ValueError):
    def __init__(self, fields: list[str]):
        self.fields = fields
        super().__init__("DOMAIN_FACTS_REQUIRED")


def domain_request(
    kind: str,
    facts: BaseModel,
    *,
    org_id: uuid.UUID,
    key: str,
    posting_date: date,
    business_date: date,
    payment_date: date | None,
    evidence_references: list[uuid.UUID],
    description: str = "",
    ignored_missing_fields: set[str] | None = None,
) -> BaseModel:
    """Bind authoritative parent scope, then run the domain's cross-field rules."""
    request_type = DOMAIN_REQUEST_TYPES[kind]
    supplied: dict[str, Any] = facts.model_dump(mode="python")
    inherited = {
        "org_id": org_id,
        "idempotency_key": key,
        "posting_date": posting_date,
        "business_date": business_date,
        "payment_date": payment_date,
        "description": description,
        "evidence_references": evidence_references,
        "purchase_date": business_date,
        "acquisition_date": business_date,
        "activation_date": business_date,
        "drawdown_date": business_date,
        "disposal_date": business_date,
        "retirement_date": business_date,
    }
    supplied.update(
        {key: value for key, value in inherited.items() if key in request_type.model_fields}
    )
    # Domain Decimal validators require the caller's decimal string. A validated
    # fact model retains Decimal exactly; serialize that field without rounding.
    if kind == "borrowing_drawdown" and supplied.get("annual_rate_percent") is not None:
        supplied["annual_rate_percent"] = str(supplied["annual_rate_percent"])
    request = request_type.model_validate(supplied)
    requirements = request.missing_information()
    ignored = {
        "bank_account_code",
        "bank_transaction_references",
        *(ignored_missing_fields or set()),
    }
    missing = sorted(
        {
            field
            for requirement in requirements
            for field in requirement.fields
            if field not in ignored
        }
    )
    if (
        kind == "borrowing_interest_accrual"
        and getattr(request, "period_end", None) != posting_date
    ):
        raise ValueError("BORROWING_ACCRUAL_POSTING_DATE_MUST_EQUAL_PERIOD_END")
    if missing:
        raise DomainFactsRequired(missing)
    return request
