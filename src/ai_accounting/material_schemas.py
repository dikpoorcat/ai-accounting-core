"""Source coverage facts; completion is derived from the posted component ledger."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .component_schemas import SourceReference
from .fact_requirements import MANAGEMENT_FACT, RecognitionPeriod


class MaterialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: uuid.UUID
    period_id: uuid.UUID


class MaterialColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str = Field(min_length=1, description="XLSX列字母；CSV从A开始编号。")
    role: Literal["amount", "context"]
    label: str = ""


class MaterialSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: uuid.UUID
    sheet: str | None = None
    header_row: int = Field(default=1, ge=0)
    columns: list[MaterialColumn] = Field(default_factory=list)
    total_rows: list[int] = Field(default_factory=list)
    sheet_columns: dict[str, list[MaterialColumn]] = Field(default_factory=dict)
    sheet_header_rows: dict[str, int] = Field(default_factory=dict)
    sheet_total_rows: dict[str, list[int]] = Field(default_factory=dict)
    # Non-tabular sources are reviewed by the AI, with visible original locations.
    passages: dict[str, str] = Field(
        default_factory=dict,
        description="原文位置（页码、段落等）到所核对原文的映射；不接受空白摘录。",
    )


class RegisterPeriodMaterialsRequest(MaterialRequest):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    sources: list[MaterialSourceInput] = Field(default_factory=list)


class MaterialComponentLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: SourceReference
    amount_fen: StrictInt = Field(gt=0)
    expected_facts_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    open_item_key: str | None = None


class MaterialResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_key: str
    treatment: Literal[
        "pending", "recognize", "duplicate", "supporting", "no_accounting", "other_period"
    ] = "pending"
    business_kind: str | None = Field(
        default=None,
        json_schema_extra={
            "x-accounting-fact": {"role": "accounting", "meaning": "business_nature"}
        },
    )
    amount_fen: StrictInt | None = Field(
        default=None,
        ge=0,
        description="整数分；优先复用原文件金额。",
        json_schema_extra={"x-accounting-fact": {"role": "accounting", "precision": "fen"}},
    )
    recognition_period: RecognitionPeriod = None
    required_facts: dict[str, str | StrictInt | bool | None] = Field(
        default_factory=dict, description="需逐项与组件匹配的核算事实；不填写管理说明。"
    )
    links: list[MaterialComponentLink] = Field(default_factory=list)
    duplicate_of: str | None = None
    target_period_id: uuid.UUID | None = None
    basis: str = ""
    non_accounting_reason: (
        Literal[
            "forecast", "balance_control", "not_company_business", "cancelled_before_recognition"
        ]
        | None
    ) = None
    employee_id: uuid.UUID | None = Field(
        default=None, json_schema_extra={"x-accounting-fact": MANAGEMENT_FACT}
    )
    export_category: Literal["salary", "labor", "reimbursement"] | None = Field(
        default=None, json_schema_extra={"x-accounting-fact": MANAGEMENT_FACT}
    )


class MaterialSplitPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_fen: StrictInt = Field(gt=0)
    label: str = ""


class MaterialItemSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_key: str
    parts: list[MaterialSplitPart] = Field(min_length=2)
    basis: str = Field(min_length=1)


class UpdatePeriodMaterialInventoryRequest(MaterialRequest):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reviewed_notes_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolutions: list[MaterialResolution] = Field(default_factory=list)
    splits: list[MaterialItemSplit] = Field(default_factory=list)
    subsequent_bank_resolutions: dict[str, list[MaterialComponentLink]] = Field(
        default_factory=dict
    )


class CompanyNotesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: uuid.UUID


class UpdateCompanyNotesRequest(CompanyNotesRequest):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(max_length=1_000_000)
