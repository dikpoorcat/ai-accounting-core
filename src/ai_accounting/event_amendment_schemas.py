"""Typed replacement facts for the existing posting workflows."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .borrowing_schemas import (
    DrawBorrowingRequest,
    PreviewBorrowingInterestRequest,
)
from .component_schemas import RecordEventRequest
from .enterprise_income_tax_schemas import PreviewEnterpriseIncomeTaxResultRequest
from .financial_statement_schemas import ConfirmEnterpriseIncomeTaxQuarterRequest
from .intangible_asset_schemas import (
    AcquireIntangibleAssetRequest,
    PreviewIntangibleAssetAmortizationRequest,
    RetireIntangibleAssetRequest,
)
from .labor_remuneration_schemas import (
    PreviewLaborRemunerationBatchRequest,
)
from .schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    DisposeFixedAssetRequest,
    PreviewFixedAssetDepreciationBatchRequest,
    PreviewFixedAssetDepreciationRequest,
    PreviewPayrollRequest,
    RecordPayrollContributionSupplementRequest,
    TaxPeriodPreviewRequest,
)

ReplacementFacts = (
    RecordEventRequest
    | PreviewPayrollRequest
    | RecordPayrollContributionSupplementRequest
    | AcquireFixedAssetRequest
    | ActivateFixedAssetRequest
    | PreviewFixedAssetDepreciationRequest
    | PreviewFixedAssetDepreciationBatchRequest
    | DisposeFixedAssetRequest
    | AcquireIntangibleAssetRequest
    | PreviewIntangibleAssetAmortizationRequest
    | RetireIntangibleAssetRequest
    | DrawBorrowingRequest
    | PreviewBorrowingInterestRequest
    | PreviewLaborRemunerationBatchRequest
    | TaxPeriodPreviewRequest
    | ConfirmEnterpriseIncomeTaxQuarterRequest
    | PreviewEnterpriseIncomeTaxResultRequest
)


class AmendEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    event_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_facts_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=1000)
    replacement: ReplacementFacts

    @model_validator(mode="after")
    def same_organization(self) -> AmendEventRequest:
        if self.replacement.org_id != self.org_id:
            raise ValueError("replacement must belong to the same organization")
        return self


class DeleteEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    event_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_facts_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=1000)


class WithdrawBankImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    action_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=1000)
