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
from .fact_requirements import MANAGEMENT_FACT
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
    RegisterPayrollContributionActualRequest,
    RegisterPayrollFirstWageTaxTreatmentRequest,
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
    reason: str = Field(
        default="", max_length=1000, json_schema_extra={"x-accounting-fact": MANAGEMENT_FACT}
    )
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
    reason: str = Field(
        default="", max_length=1000, json_schema_extra={"x-accounting-fact": MANAGEMENT_FACT}
    )


class WithdrawBankImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    action_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(
        default="", max_length=1000, json_schema_extra={"x-accounting-fact": MANAGEMENT_FACT}
    )


class CorrectionEventReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    expected_facts_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacement: ReplacementFacts


class PreviewCorrectionRequest(BaseModel):
    """Accounting changes; affected consumers are discovered by the kernel."""

    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    source_changes: list[
        RegisterPayrollContributionActualRequest | RegisterPayrollFirstWageTaxTreatmentRequest
    ] = Field(default_factory=list)
    event_replacements: list[CorrectionEventReplacement] = Field(default_factory=list)
    reason: str = Field(
        default="", max_length=1000, json_schema_extra={"x-accounting-fact": MANAGEMENT_FACT}
    )

    @model_validator(mode="after")
    def correction_scope(self):
        if not self.source_changes and not self.event_replacements:
            raise ValueError("provide source_changes or event_replacements")
        if any(change.org_id != self.org_id for change in self.source_changes) or any(
            change.replacement.org_id != self.org_id for change in self.event_replacements
        ):
            raise ValueError("all correction facts must belong to the same organization")
        ids = [change.event_id for change in self.event_replacements]
        if len(ids) != len(set(ids)):
            raise ValueError("event_replacements must be unique")
        keys = [change.idempotency_key for change in self.source_changes]
        if len(keys) != len(set(keys)):
            raise ValueError("source change keys must be unique")
        return self


class ConfirmCorrectionRequest(PreviewCorrectionRequest):
    idempotency_key: str = Field(min_length=1, max_length=200)
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
