"""Bank export selectors: monetary facts are deliberately not accepted."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PreviewMybankExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org_id: uuid.UUID
    payroll_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    template_path: str = Field(min_length=1)
    recipients_path: str = Field(min_length=1)
    include_salary: bool = True
    scope: Literal["complete", "selected"] = "complete"
    employee_ids: list[uuid.UUID] | None = Field(default=None, min_length=1)
    reimbursement_open_item_ids: list[uuid.UUID] = Field(default_factory=list)
    reimbursement_employee_ids: list[uuid.UUID] | None = Field(
        default=None,
        min_length=1,
        description="负责人已明确本次报销人员时填写；缺少任何一人的内核应付款即阻止生成。",
    )

    @model_validator(mode="after")
    def unique_sources(self) -> PreviewMybankExportRequest:
        for values in (
            self.employee_ids or [],
            self.reimbursement_open_item_ids,
            self.reimbursement_employee_ids or [],
        ):
            if len(values) != len(set(values)):
                raise ValueError("来源或员工不能重复选择")
        if (
            self.scope == "selected"
            and not self.include_salary
            and not self.reimbursement_open_item_ids
        ):
            raise ValueError("至少选择工资或内核报销应付款来源")
        return self


class GenerateMybankExportRequest(PreviewMybankExportRequest):
    expected_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_dir: str = Field(min_length=1)


class ImportMybankPaymentSourceRequest(BaseModel):
    """Import original evidence, never accept an export-time amount override."""

    model_config = ConfigDict(extra="forbid")
    org_id: uuid.UUID
    payroll_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    source_kind: Literal["actual_tax", "payment_register"]
    evidence_id: uuid.UUID
    expected_revision: int = Field(ge=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=200)
