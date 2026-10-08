"""Recoverable working inputs, never a second business registration model."""

from __future__ import annotations

import math
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .contracts import KernelError
from .types import EvidenceDigest, SubjectId, canonical

Identifier = Annotated[str, Field(min_length=1, max_length=200)]


class DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DraftSourceRef(DraftModel):
    evidence_digest: EvidenceDigest | None = None
    source_id: SubjectId | None = None
    subject_id: SubjectId | None = None
    fact_id: Identifier | None = None
    fact_revision: Annotated[int, Field(ge=1)] | None = None
    location: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def referenced(self):
        if not any((self.evidence_digest, self.source_id, self.subject_id, self.fact_id)):
            raise ValueError("a source reference requires an explicit source identity")
        return self


class DraftCandidate(DraftModel):
    id: Identifier
    command: Identifier
    payload: dict[str, JsonValue]


class DraftQuestion(DraftModel):
    id: Identifier
    question: Annotated[str, Field(min_length=1)]
    answer: str | None = None
    source_refs: list[DraftSourceRef] | None = None


class DraftPendingRequest(DraftModel):
    command: Identifier
    payload: dict[str, JsonValue]
    request_id: Identifier


class DraftResultRef(DraftModel):
    request_id: Identifier | None = None
    command: Identifier | None = None
    subject_id: SubjectId | None = None
    fact_id: Identifier | None = None
    fact_revision: Annotated[int, Field(ge=1)] | None = None
    job_id: Identifier | None = None
    note: str | None = None

    @model_validator(mode="after")
    def referenced(self):
        if not any((self.request_id, self.subject_id, self.fact_id, self.job_id)):
            raise ValueError("a result reference requires an explicit result identity")
        return self


class WorkDraft(DraftModel):
    """Optional sections stay absent unless the caller actually supplied them."""

    candidates: list[DraftCandidate] | None = None
    source_refs: list[DraftSourceRef] | None = None
    questions: list[DraftQuestion] | None = None
    resume_note: str | None = None
    next_step: str | None = None
    pending_requests: list[DraftPendingRequest] | None = None
    result_refs: list[DraftResultRef] | None = None

    @model_validator(mode="after")
    def unique_ids(self):
        for values, field in (
            (self.candidates, "id"),
            (self.questions, "id"),
            (self.pending_requests, "request_id"),
        ):
            identifiers = [getattr(item, field) for item in values or ()]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("working document identifiers must be unique within a section")
        return self


# Existing typed business inputs only. Directory, credentials, closing approval,
# background jobs and arbitrary journal entry operations are not working inputs.
DRAFT_COMMANDS = frozenset(
    {
        "save_fact",
        "amend_fact",
        "save_facts",
        "prepare_fact_registration",
        "register_entity",
        "update_entity_profile",
        "update_company_note",
        "receive_material",
        "resolve_material",
        "resolve_material_group",
        "preview_material_allocation",
        "confirm_material_allocation",
        "preview",
        "confirm",
        "preview_delete",
        "delete",
        "save_payee",
        "prepare_obligations",
        "confirm_obligations",
        "prepare_payroll",
        "confirm_payroll_preparation",
        "prepare_asset_activation_batch",
        "confirm_asset_activation_batch",
        "prepare_asset_consumption_month",
        "confirm_asset_consumption_month",
        "preview_identity_correction",
        "confirm_identity_correction",
        "preview_tax_import",
        "confirm_tax_import",
    }
)

PENDING_REQUEST_COMMANDS = frozenset(
    {
        "save_fact",
        "amend_fact",
        "save_facts",
        "register_entity",
        "update_entity_profile",
        "update_company_note",
        "receive_material",
        "resolve_material",
        "resolve_material_group",
        "confirm_material_allocation",
        "confirm",
        "delete",
        "save_payee",
        "confirm_obligations",
        "confirm_payroll_preparation",
        "confirm_asset_activation_batch",
        "confirm_asset_consumption_month",
        "confirm_identity_correction",
        "confirm_tax_import",
    }
)

_PROHIBITED_KEYS = frozenset(
    {
        "password",
        "passwd",
        "authorization",
        "bearer",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "approval_id",
        "approval_token",
        "owner_approval_token",
        "login_token",
        "credential",
        "credentials",
        "client_secret",
        "content_base64",
        "original_base64",
        "raw_bytes",
        "parse_result",
        "parsed_items",
        "full_parse",
        "registered_fact_snapshots",
        "session_token",
        "recovery_code",
        "recovery_codes",
        "api_key",
        "api_token",
        "secret_key",
        "private_key",
        "owner_token",
        "owner_session_token",
    }
)


def validate_json_content(value: Any) -> None:
    """Accept only actual JSON values; reject secrets and duplicated snapshots."""
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise ValueError("nonfinite numbers are not JSON")
    if type(value) is list:
        for item in value:
            validate_json_content(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            if key.lower().replace("-", "_") in _PROHIBITED_KEYS:
                raise ValueError(
                    "work drafts cannot contain credentials, approval tokens or snapshots"
                )
            validate_json_content(item)
        return
    raise ValueError("working inputs must contain only JSON values")


def draft_json(value: WorkDraft | dict) -> dict:
    """Validate without materializing absent fields or business defaults."""
    raw = (
        value.model_dump(mode="json", exclude_unset=True) if isinstance(value, WorkDraft) else value
    )
    validate_json_content(raw)
    if not isinstance(raw, dict):
        raise ValueError("the work draft must be a JSON object")
    WorkDraft.model_validate(raw, strict=True)
    for section in ("candidates", "pending_requests"):
        for candidate in raw.get(section) or []:
            for item in _fact_entries(candidate["command"], candidate["payload"]):
                if not isinstance(item, dict):
                    raise ValueError("provided fact inputs must be objects")
                if "kind" in item and (not isinstance(item["kind"], str) or not item["kind"]):
                    raise ValueError("provided fact kinds must be nonempty strings")
    return raw


def _fact_entries(command: str, payload: dict):
    if command == "save_facts":
        facts = payload.get("facts", [])
        if not isinstance(facts, list):
            raise ValueError("provided facts must be an array")
        return facts
    if command in {"save_fact", "amend_fact", "prepare_fact_registration"} or "kind" in payload:
        return [payload]
    return []


def target_warnings(draft: dict, *, registry, commands) -> list[dict]:
    """Old candidates remain readable when the current build changes their route."""
    warnings = []
    for section in ("candidates", "pending_requests"):
        for candidate in draft.get(section) or []:
            command, payload = candidate["command"], candidate["payload"]
            if command not in DRAFT_COMMANDS or command not in commands:
                warnings.append({"code": "draft_command_unavailable", "command": command})
                continue
            for item in _fact_entries(command, payload):
                if not isinstance(item, dict):
                    raise ValueError("provided fact inputs must be objects")
                if "kind" not in item:
                    continue
                kind = item["kind"]
                if not isinstance(kind, str) or not kind:
                    raise ValueError("provided fact kinds must be nonempty strings")
                model = registry.models.get(kind)
                if model is None:
                    warnings.append(
                        {"code": "draft_fact_kind_unavailable", "command": command, "kind": kind}
                    )
                elif (
                    command
                    in {"save_fact", "amend_fact", "save_facts", "prepare_fact_registration"}
                    and model.registration_command
                ):
                    warnings.append(
                        {
                            "code": "draft_registration_route_changed",
                            "command": command,
                            "kind": kind,
                        }
                    )
                elif (
                    command
                    not in {"save_fact", "amend_fact", "save_facts", "prepare_fact_registration"}
                    and model.registration_command
                    and command != model.registration_command
                ):
                    warnings.append(
                        {
                            "code": "draft_registration_route_changed",
                            "command": command,
                            "kind": kind,
                        }
                    )
    return [
        dict(items) for items in dict.fromkeys(tuple(sorted(item.items())) for item in warnings)
    ]


def validate_targets(draft: dict, *, registry, commands, company_id: str) -> None:
    warnings = target_warnings(draft, registry=registry, commands=commands)
    if warnings:
        raise KernelError(
            "work_draft_target_invalid", "工作稿的目标入口或事实类型不符合当前合同", issues=warnings
        )
    for section in ("candidates", "pending_requests"):
        for item in draft.get(section) or []:
            payload = item["payload"]
            if "company_id" in payload and payload["company_id"] != company_id:
                raise KernelError("work_draft_scope_mismatch", "候选请求不能指向另一家公司")
            if section == "pending_requests" and (
                payload.get("request_id") != item["request_id"]
                or payload.get("company_id") != company_id
                or item["command"] not in PENDING_REQUEST_COMMANDS
            ):
                raise KernelError(
                    "work_draft_request_invalid", "待核对请求必须保存完整载荷及原请求编号"
                )


def preserve_pending_requests(
    previous: dict | None, current: dict, *, request_result, company_id, database_id
) -> None:
    """Editing candidates cannot silently erase an unresolved original request."""
    if previous is None:
        return
    current_requests = {item["request_id"]: item for item in current.get("pending_requests") or []}
    result_ids = {item.get("request_id") for item in current.get("result_refs") or []}
    for item in previous.get("pending_requests") or []:
        ident = item["request_id"]
        replacement = current_requests.get(ident)
        if replacement is not None and canonical(replacement) != canonical(item):
            raise KernelError(
                "work_draft_request_conflict",
                "原请求编号对应的命令及完整载荷不能被改写",
                request_id=ident,
            )
        if replacement is None:
            receipt = request_result(ident) if ident in result_ids and request_result else None
            if not isinstance(receipt, dict) or (
                receipt.get("status") != "committed"
                or receipt.get("company_id") != company_id
                or receipt.get("database_id") != database_id
                or receipt.get("submitted_request_id") != ident
            ):
                raise KernelError(
                    "work_draft_pending_request_required",
                    "原请求尚无当前公司已提交回执，完整载荷不能随候选编辑丢失",
                    request_id=ident,
                )
