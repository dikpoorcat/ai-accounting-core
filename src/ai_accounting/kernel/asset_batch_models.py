"""Asset-only posting owners. Card facts and calculation outcomes stay independent."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

from .contracts import Fact, KernelError, Read
from .domains.transactions import Identifier
from .types import YearMonth

MEMBER_KINDS = frozenset({"asset_activation", "asset_consumption"})
OWNER_KINDS = frozenset({"asset_activation_batch", "asset_consumption_month"})
LIFECYCLE_KINDS = (
    "asset",
    "reimbursed_asset",
    "opening_asset",
    "opening_identity_binding",
    "asset_activation",
    "asset_disposal",
)


class ActivationMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    subject_id: Identifier


class AssetActivationBatch(Fact):
    kind: ClassVar[str] = "asset_activation_batch"
    registration_command: ClassVar[str] = "prepare_asset_activation_batch"
    identity_fields: ClassVar[tuple[str, ...]] = ("period",)
    members: tuple[ActivationMember, ...]
    activity_count_field: ClassVar[str] = "positive_member_count"

    @model_validator(mode="after")
    def unique_members(self):
        if len({m.subject_id for m in self.members}) != len(self.members):
            raise ValueError("启用批次不得重复成员")
        return self

    def reads(self):
        return tuple(
            Read(source, "asset_activation", "@" + m.subject_id)
            for m in self.members
            for source in ("fact", "calculation")
        )


class AssetConsumptionMonth(Fact):
    kind: ClassVar[str] = "asset_consumption_month"
    registration_command: ClassVar[str] = "prepare_asset_consumption_month"
    identity_fields: ClassVar[tuple[str, ...]] = ("period",)
    activity_count_field: ClassVar[str] = "positive_member_count"

    def reads(self):
        until = YearMonth.from_ordinal(self.period.ordinal + 1)
        return (
            *(Read("fact", kind, "*", until) for kind in LIFECYCLE_KINDS),
            Read("fact", "asset_consumption", str(self.period)),
            Read("calculation", "asset_consumption", str(self.period)),
        )


def asset_batch_command_required(version, context):
    raise KernelError("asset_batch_command_required", "资产批次须通过资产专用预览与确认入口处理")


def accounting_reads(version, outcome):
    return tuple(
        Read("calculation", member["kind"], "#" + member["member_calculation_id"])
        for member in outcome["values"]["members"]
    )


def accounting_projection(version, outcome, references):
    values = dict(outcome["values"])
    values.pop("membership_digest", None)
    values["members"] = [
        {
            "asset_id": m["asset_id"],
            "member_subject_id": m["member_subject_id"],
            "accounting": references.signature(m["member_calculation_id"]),
        }
        for m in values["members"]
    ]
    return {**outcome, "values": values}


def register(registry):
    for model in (AssetActivationBatch, AssetConsumptionMonth):
        registry.register(model, asset_batch_command_required)
        registry.register_accounting(model.kind, accounting_projection, accounting_reads)
