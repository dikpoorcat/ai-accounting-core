"""Strict wire contracts for the durable, typed reconstruction runner."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import EvidenceDigest, YearMonth

ReplayId = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Ref = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")]

# Commands here are deliberately an allowlist. They are dispatched through the
# ordinary command models and domain entry points; there is no SQL or journal API.
REPLAY_COMMANDS = frozenset(
    {
        "evidence",
        "register_entity",
        "update_entity_profile",
        "register_worker",
        "change_workforce_relationship",
        "correct_fund_account_identity",
        "preview_identity_correction",
        "confirm_identity_correction",
        "save_facts",
        "preview",
        "confirm",
        "prepare_fact_registration",
        "confirm_payroll_preparation",
        "payroll_reuse_basis",
        "prepare_payroll",
        "receive_material",
        "resolve_material",
        "resolve_material_group",
        "prepare_obligations",
        "confirm_obligations",
        "preview_material_allocation",
        "confirm_material_allocation",
        "preview_tax_import",
        "confirm_tax_import",
        "prepare_asset_activation_batch",
        "confirm_asset_activation_batch",
        "prepare_asset_consumption_month",
        "confirm_asset_consumption_month",
        "inventory",
        "inventory_batch",
        "update_company_note",
        "save_payee",
        "update_period_commentary",
        "replay_classification_prepare",
        "preview_close",
        "close",
    }
)


class ReplaySource(BaseModel):
    """One source entry in the owner's already preserved company manifest."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_id: Ref
    kind: Literal["document", "evidence", "confirmation", "source_record"]
    period: YearMonth | None = None
    evidence_digest: EvidenceDigest | None = None
    disposition: Literal["adopt", "exclude", "reference"]
    depends_on: tuple[Ref, ...] = ()

    @model_validator(mode="after")
    def evidence_for_material(self):
        if self.kind in {"document", "evidence", "confirmation"} and self.evidence_digest is None:
            raise ValueError("preserved source documents require an evidence digest")
        return self


class ReplayStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    step_id: Ref
    command: Literal[
        "evidence",
        "register_entity",
        "update_entity_profile",
        "register_worker",
        "change_workforce_relationship",
        "correct_fund_account_identity",
        "preview_identity_correction",
        "confirm_identity_correction",
        "save_facts",
        "preview",
        "confirm",
        "prepare_fact_registration",
        "confirm_payroll_preparation",
        "payroll_reuse_basis",
        "prepare_payroll",
        "receive_material",
        "resolve_material",
        "resolve_material_group",
        "prepare_obligations",
        "confirm_obligations",
        "preview_material_allocation",
        "confirm_material_allocation",
        "preview_tax_import",
        "confirm_tax_import",
        "prepare_asset_activation_batch",
        "confirm_asset_activation_batch",
        "prepare_asset_consumption_month",
        "confirm_asset_consumption_month",
        "inventory",
        "inventory_batch",
        "update_company_note",
        "save_payee",
        "update_period_commentary",
        "replay_classification_prepare",
        "preview_close",
        "close",
    ]
    payload: dict[str, Any]
    depends_on: tuple[Ref, ...] = ()
    output_ref: Ref | None = None
    # Domain binding describes typed output fields whose identities can be
    # referenced later. It never supplies accounting values or line direction.
    output_fields: tuple[
        Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_.]{0,127}$")], ...
    ] = ()

    @model_validator(mode="after")
    def unique_edges(self):
        if self.step_id in self.depends_on or len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("replay step dependencies must be unique and cannot be self edges")
        if (self.output_ref is None) != (not self.output_fields):
            raise ValueError(
                "an output reference and at least one output field must be declared together"
            )
        return self


class ReplayPrepare(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    company_id: ReplayId
    database_id: ReplayId
    first_period: YearMonth
    last_period: YearMonth
    owner_confirmation: EvidenceDigest
    source_manifest_digest: EvidenceDigest
    source_manifest: Annotated[tuple[ReplaySource, ...], Field(min_length=1, max_length=20000)]
    preserved_evidence: tuple[EvidenceDigest, ...] = ()
    steps: Annotated[tuple[ReplayStep, ...], Field(min_length=1, max_length=20000)]
    request_id: ReplayId

    @model_validator(mode="after")
    def bounded_and_unique(self):
        if not 0 <= self.last_period.ordinal - self.first_period.ordinal < 120:
            raise ValueError("replay range must contain 1 to 120 consecutive months")
        source_ids = [item.source_id for item in self.source_manifest]
        step_ids = [item.step_id for item in self.steps]
        if len(set(source_ids)) != len(source_ids) or len(set(step_ids)) != len(step_ids):
            raise ValueError("source and step identifiers must be unique")
        source_set = set(source_ids)
        if any(not set(item.depends_on).issubset(source_set) for item in self.source_manifest):
            raise ValueError("source dependencies must resolve within the submitted manifest")
        remaining = {item.source_id: set(item.depends_on) for item in self.source_manifest}
        completed = set()
        while remaining:
            ready = next(
                (
                    key
                    for key, dependencies in remaining.items()
                    if dependencies.issubset(completed)
                ),
                None,
            )
            if ready is None:
                raise ValueError("source manifest dependencies must be acyclic")
            completed.add(ready)
            remaining.pop(ready)
        if len(set(self.preserved_evidence)) != len(self.preserved_evidence):
            raise ValueError("preserved evidence references must be unique")
        if not set(self.preserved_evidence).issubset(
            {item.evidence_digest for item in self.source_manifest if item.evidence_digest}
        ):
            raise ValueError("preserved evidence must be listed in the source manifest")
        if not any(
            item.kind == "confirmation" and item.evidence_digest == self.owner_confirmation
            for item in self.source_manifest
        ):
            raise ValueError("owner confirmation must be an explicit source-manifest item")
        return self


class ReplayConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    plan_id: ReplayId
    plan_digest: EvidenceDigest
    request_id: ReplayId


class ReplayStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    run_id: ReplayId
    step_id: Ref | None = None


class ReplayClassificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source_id: Ref
    source_subject_id: ReplayId
    source_calculation_id: ReplayId
    obligation_key: ReplayId
    payment_revision_id: ReplayId
    payment_allocation_scope: ReplayId
