"""Explicitly scoped reconstruction runtime; normal closes retain password grants."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .close_storage import verified_header
from .contracts import KernelError
from .engine import PROGRAM_VERSION
from .periods import Periods
from .types import EvidenceDigest, YearMonth, digest, evidence_digest_bytes

ScopeId = Annotated[str, Field(min_length=1, max_length=200)]
SCOPE_FORMAT = "ai-accounting-kernel/2/replay-close-scope/1"


class ReplayTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    company_id: ScopeId
    database_id: ScopeId
    first_period: YearMonth
    last_period: YearMonth
    owner_confirmation: EvidenceDigest

    @model_validator(mode="after")
    def bounded(self):
        if not 0 <= self.last_period.ordinal - self.first_period.ordinal < 120:
            raise ValueError("replay scope must contain 1 to 120 consecutive months")
        return self


class ReplayScopeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    format: Literal["ai-accounting-kernel/2/replay-close-scope/1"]
    catalog_instance_id: ScopeId
    targets: Annotated[tuple[ReplayTarget, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique(self):
        if len({target.company_id for target in self.targets}) != len(self.targets):
            raise ValueError("each company must have one explicit replay scope")
        return self


class ReplayCloseScope:
    def __init__(self, config: ReplayScopeConfig):
        self.config = config
        self.scope_digest = digest(config.model_dump(mode="json")).hex()

    @classmethod
    def from_config(cls, config: dict, catalog_id: str):
        from .types import canonical

        try:
            parsed = ReplayScopeConfig.model_validate_json(canonical(config))
        except (ValidationError, TypeError, ValueError):
            raise KernelError("invalid_replay_scope", "重放范围文件格式或月份范围无效") from None
        if parsed.catalog_instance_id != catalog_id:
            raise KernelError("replay_scope_mismatch", "重放范围不属于当前资料目录")
        return cls(parsed)

    def require_target(self, engine, first_period, last_period, owner_confirmation):
        from .versions import database_format

        first, last = YearMonth(first_period), YearMonth(last_period)
        target = next(
            (item for item in self.config.targets if item.company_id == engine.store.company_id),
            None,
        )
        if (
            target is None
            or target.database_id != engine.store.database_id
            or not target.first_period <= first <= last <= target.last_period
            or target.owner_confirmation != owner_confirmation
        ):
            raise KernelError("replay_scope_mismatch", "公司、数据库、月份或确认原件超出重放范围")
        with engine.store.connection(read_only=True) as connection:
            if (
                database_format(connection, bundle=engine.store.bundle, kind="company")["status"]
                != "draft"
            ):
                raise KernelError("replay_test_database_required", "批量重放仅用于开发测试公司库")
            if (
                connection.execute(
                    "SELECT 1 FROM evidence WHERE digest=?", (bytes.fromhex(owner_confirmation),)
                ).fetchone()
                is None
            ):
                raise KernelError("replay_confirmation_missing", "重放范围确认原件尚未登记")
        return target


def _boundary(engine, connection):
    epochs = engine.store.epochs(connection)
    repair = connection.execute("SELECT read_repair_revision FROM state WHERE id=1").fetchone()[0]
    row = connection.execute("SELECT * FROM period_close ORDER BY period DESC LIMIT 1").fetchone()
    prefix = None
    if row is not None:
        header = verified_header(connection, row)
        prefix = {
            "period": str(YearMonth.from_ordinal(header.period)),
            "digest": header.logical_digest.hex(),
        }
    return {"epochs": epochs, "read_repair_revision": repair, "prefix": prefix}


def _key(request_id, suffix):
    return "replay-" + digest([request_id, suffix]).hex()


class ReplayClose:
    def __init__(self, service, engine, authority):
        self.service, self.engine, self.authority = service, engine, authority
        self.scope = service.replay_close_scope

    def _require(self, first_period, last_period, owner_confirmation):
        if self.scope is None:
            raise KernelError("replay_mode_required", "请先以明确范围启动重放测试服务")
        self.service.security.validate_authority(self.authority)
        return self.scope.require_target(self.engine, first_period, last_period, owner_confirmation)

    def preview(
        self, first_period: YearMonth, last_period: YearMonth, *, owner_confirmation: EvidenceDigest
    ):
        evidence_digest_bytes(owner_confirmation, "owner_confirmation")
        first_period, last_period = YearMonth(first_period), YearMonth(last_period)
        target = self._require(first_period, last_period, owner_confirmation)
        with self.engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            boundary = _boundary(self.engine, connection)
        expected_first = (
            YearMonth(boundary["prefix"]["period"]).ordinal + 1
            if boundary["prefix"]
            else target.first_period.ordinal
        )
        if expected_first != first_period.ordinal:
            raise KernelError("replay_start_period", "重放须从当前已关账后的首月继续")
        first_review = Periods(self.engine).preview_close(
            first_period, owner_confirmation=owner_confirmation
        )
        if (
            first_review["epochs"] != boundary["epochs"]
            or first_review["manifest"]["read_version"]["read_repair_revision"]
            != boundary["read_repair_revision"]
        ):
            raise KernelError("preview_expired", "重放预览期间来源版本已变化")
        intent = {
            "company_id": self.engine.store.company_id,
            "database_id": self.engine.store.database_id,
            "first_period": first_period,
            "last_period": last_period,
            "owner_confirmation": owner_confirmation,
            "scope_digest": self.scope.scope_digest,
            "build_id": PROGRAM_VERSION,
            "boundary": boundary,
            "first_preview_digest": first_review["digest"],
        }
        hashed = digest(intent).hex()
        for key in list(self.service.replay_close_previews):
            if key[0] == self.engine.store.company_id:
                self.service.replay_close_previews.pop(key, None)
        self.service.replay_close_previews[(self.engine.store.company_id, hashed)] = intent
        return {
            "status": "replay_preview",
            **intent,
            "digest": hashed,
            "epochs": boundary["epochs"],
            "first_month_review": first_review,
            "periods": [
                str(YearMonth.from_ordinal(i))
                for i in range(first_period.ordinal, last_period.ordinal + 1)
            ],
            "authorization_method": "replay_scope",
            "later_month_review": "prepared_after_previous_month_closes",
        }

    def confirm(
        self,
        first_period: YearMonth,
        last_period: YearMonth,
        *,
        owner_confirmation: EvidenceDigest,
        preview_digest: EvidenceDigest,
        epochs: dict,
        request_id: str,
    ):
        """Compatibility facade that queues the durable typed replay runner."""
        from .replay_runner import confirm_replay, prepare_replay

        evidence_digest_bytes(owner_confirmation, "owner_confirmation")
        first_period, last_period = YearMonth(first_period), YearMonth(last_period)
        self._require(first_period, last_period, owner_confirmation)
        intent = self.service.replay_close_previews.get(
            (self.engine.store.company_id, preview_digest)
        )
        with self.engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            live_boundary = _boundary(self.engine, connection)
        if (
            intent is None
            or any(
                intent[field] != value
                for field, value in (
                    ("first_period", first_period),
                    ("last_period", last_period),
                    ("owner_confirmation", owner_confirmation),
                    ("scope_digest", self.scope.scope_digest),
                )
            )
            or intent["boundary"]["epochs"] != epochs
            or intent["boundary"] != live_boundary
        ):
            raise KernelError("preview_expired", "重放批量预览已失效，请重新核对")

        source_manifest = [
            {
                "source_id": "owner-confirmation",
                "kind": "confirmation",
                "period": None,
                "evidence_digest": owner_confirmation,
                "disposition": "reference",
                "depends_on": [],
            }
        ]
        steps = []
        previous_close = None
        for ordinal in range(first_period.ordinal, last_period.ordinal + 1):
            period = str(YearMonth.from_ordinal(ordinal))
            suffix = period.replace("-", "")
            review_ref = f"review-{suffix}"
            steps.append(
                {
                    "step_id": f"preview-{suffix}",
                    "command": "preview_close",
                    "payload": {"period": period, "owner_confirmation": owner_confirmation},
                    "depends_on": [previous_close] if previous_close else [],
                    "output_ref": review_ref,
                    "output_fields": ["digest", "epochs", "manifest.opening_calculation_id"],
                }
            )
            steps.append(
                {
                    "step_id": f"close-{suffix}",
                    "command": "close",
                    "payload": {
                        "period": period,
                        "owner_confirmation": owner_confirmation,
                        "preview_digest": {"$replay_ref": review_ref, "field": "digest"},
                        "epochs": {"$replay_ref": review_ref, "field": "epochs"},
                    },
                    "depends_on": [f"preview-{suffix}"],
                }
            )
            previous_close = f"close-{suffix}"
        plan_request_id = "legacy-replay-plan-" + digest([request_id, preview_digest]).hex()
        prepare_payload = {
            "company_id": self.engine.store.company_id,
            "database_id": self.engine.store.database_id,
            "first_period": str(first_period),
            "last_period": str(last_period),
            "owner_confirmation": owner_confirmation,
            "source_manifest_digest": digest(source_manifest).hex(),
            "source_manifest": source_manifest,
            "preserved_evidence": [owner_confirmation],
            "steps": steps,
            "request_id": plan_request_id,
        }
        with self.service._commit_authority(self.authority):
            plan = prepare_replay(self.service, self.engine, self.authority, prepare_payload)
            confirm_request_id = (
                "legacy-replay-confirm-" + digest([request_id, preview_digest]).hex()
            )
            return confirm_replay(
                self.service,
                self.engine,
                self.authority,
                {
                    "plan_id": plan["plan_id"],
                    "plan_digest": plan["plan_digest"],
                    "request_id": confirm_request_id,
                },
            )
