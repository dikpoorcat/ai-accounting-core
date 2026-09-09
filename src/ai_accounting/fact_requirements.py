"""Field meaning and actionable diagnostics, never accounting rule execution."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MANAGEMENT_FACT = {"role": "management", "affects_accounting": False}

RecognitionDate = Annotated[
    date | None,
    Field(
        description="核算确认截止日，不是外部申报日或付款日；支持按月确认的业务可改用recognition_period。",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "recognition",
                "precision": "day",
            }
        },
    ),
]
RecognitionPeriod = Annotated[
    str | None,
    Field(
        pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$",
        description="核算确认月份，与business_date二选一；月末仅是确认截止，不是实际申报或付款日期。",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "recognition",
                "precision": "month",
            }
        },
    ),
]
ExternalDeclarationDate = Annotated[
    date | None,
    Field(
        description="可选外部申报日期，仅作管理资料；不决定核算确认期间，不因未知而追问或阻止记账。",
        json_schema_extra={"x-accounting-fact": MANAGEMENT_FACT},
    ),
]
ActualFundsDate = Annotated[
    date,
    Field(
        description="本资金项的真实收付款日，必须依据资金事实提供；不能用确认月份月末或外部申报日代替。",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "cash_movement",
                "precision": "day",
            }
        },
    ),
]


class AccountingFactIssue(BaseModel):
    """Explain the failed facts without treating an error code as a question."""

    model_config = ConfigDict(extra="forbid")

    code: str
    kind: Literal["missing_accounting_fact", "conflicting_accounting_facts"]
    fields: list[str]
    alternatives: list[str] = Field(default_factory=list)
    actual_values: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, str] = Field(default_factory=dict)
    message: str


class AccountingFactError(ValueError):
    def __init__(self, issue: AccountingFactIssue, missing_information: list[str] | None = None):
        super().__init__(issue.code)
        self.issue = issue
        self.missing_information = missing_information or []

    def result(self) -> dict[str, Any]:
        result = {"data": {"fact_issues": [self.issue.model_dump(mode="json")]}}
        if self.issue.kind == "missing_accounting_fact":
            return result | {
                "status": "needs_information",
                "missing_information": self.missing_information,
            }
        return result | {"status": "rejected", "errors": [self.issue.code]}


def recognition_issue(
    *,
    code: str,
    business_date: date | None,
    recognition_period: str | None,
    posting_date: date,
    earliest: date | None = None,
    component_key: str | None = None,
    missing: bool = False,
) -> AccountingFactError:
    prefix = f"components.{component_key}." if component_key else ""
    return AccountingFactError(
        AccountingFactIssue(
            code=code,
            kind="missing_accounting_fact" if missing else "conflicting_accounting_facts",
            fields=[
                prefix + ("recognition_period" if recognition_period else "business_date"),
                "posting_date",
            ],
            alternatives=[prefix + "business_date", prefix + "recognition_period"],
            actual_values={
                "business_date": business_date,
                "recognition_period": recognition_period,
                "posting_date": posting_date,
            },
            expected={"recognition_not_before": earliest, "recognition_not_after": posting_date},
            context={"component_key": component_key} if component_key else {},
            message=(
                "核对结果或债务的核算确认日期／月份及记账日；先复用已有证据和明确来源。"
                "月份以月末为确认截止，不能用于证明月中已成立。"
                "外部申报日、垫付日等管理日期不是这里的缺项，不得据此索要或补造具体日期；"
                "不得为通过校验擅改真实资金日期、确认事实或记账期间。"
            ),
        ),
        [prefix + "business_date_or_recognition_period"] if missing else None,
    )
