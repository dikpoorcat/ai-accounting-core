"""Explicitly scoped reconstruction runtime; normal closes retain password grants."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .close_storage import verified_header
from .contracts import KernelError
from .engine import PROGRAM_VERSION
from .periods import Periods
from .types import YearMonth, digest

ScopeId = Annotated[str, Field(min_length=1, max_length=200)]
EvidenceDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
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
        first_period, last_period = YearMonth(first_period), YearMonth(last_period)
        self._require(first_period, last_period, owner_confirmation)
        arguments = [
            first_period,
            last_period,
            owner_confirmation,
            preview_digest,
            epochs,
            self.scope.scope_digest,
        ]
        request_hash = digest(["replay_close_range", arguments])
        cached = self.engine._cached(request_id, request_hash)
        if cached is not None:
            self.engine.request_result(request_id)
        plan_key = _key(request_id, "plan")
        plan_hash = digest(["replay_close_plan", arguments])
        plan = self.engine._cached(plan_key, plan_hash)
        if plan is None:
            if cached is not None:
                raise KernelError("request_content_invalid", "重放完成记录缺少实际计划")
            intent = self.service.replay_close_previews.get(
                (self.engine.store.company_id, preview_digest)
            )
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
            ):
                raise KernelError("preview_expired", "重放批量预览已失效，请重新核对")

            def start(connection):
                if _boundary(self.engine, connection) != intent["boundary"]:
                    raise KernelError("preview_expired", "重放来源版本或已关账前缀已变化")
                return {
                    "status": "replay_started",
                    "intent": intent,
                    "authority": dict(self.engine.audit_actor),
                    "batch_request_id": request_id,
                }

            plan = self.engine._write(
                plan_key,
                plan_hash,
                epochs,
                (),
                "replay_close_batch_start",
                start,
                checked_lanes=("accounting", "material", "management"),
            )
        else:
            self.engine.request_result(plan_key)
        if cached is None and plan["authority"] != self.engine.audit_actor:
            raise KernelError(
                "replay_authority_changed", "重放会话或负责人凭据已变化，请重新核对剩余月份"
            )
        intent = plan["intent"]
        if cached is None and intent["build_id"] != PROGRAM_VERSION:
            raise KernelError("preview_expired", "重放程序版本已变化，请重新核对剩余月份")
        baseline = intent["boundary"]
        results = []
        previous = baseline["prefix"]
        actor = dict(self.engine.audit_actor)
        self.engine.audit_actor = {
            **plan["authority"],
            "authorization_method": "replay_scope",
            "replay_scope_digest": self.scope.scope_digest,
            "replay_batch_request_id": request_id,
        }
        try:
            for ordinal in range(first_period.ordinal, last_period.ordinal + 1):
                period = str(YearMonth.from_ordinal(ordinal))
                child_key = _key(request_id, period)
                expected = {
                    **baseline["epochs"],
                    "accounting": baseline["epochs"]["accounting"] + len(results),
                    "material": baseline["epochs"]["material"] + len(results),
                }
                committed = self.engine.request_result(child_key)
                if committed["status"] == "committed":
                    result = committed["result"]
                    if (
                        committed["action"] != "close"
                        or result.get("period") != period
                        or result.get("status") != "closed"
                    ):
                        raise KernelError("request_content_invalid", "重放子请求与月份不一致")
                    with self.engine.store.connection(read_only=True) as connection:
                        connection.execute("BEGIN")
                        row = connection.execute(
                            "SELECT * FROM period_close WHERE period=?", (ordinal,)
                        ).fetchone()
                        header = verified_header(connection, row) if row else None
                        audit = connection.execute(
                            "SELECT payload FROM audit WHERE request_id=?", (child_key,)
                        ).fetchone()
                        small = header.root["small"] if header else None
                        if (
                            header is None
                            or header.logical_digest.hex() != result["digest"]
                            or audit is None
                            or json.loads(audit[0]).get("actor") != self.engine.audit_actor
                            or small["approval"] is not None
                            or small["owner_confirmation"] != owner_confirmation
                            or small["previous_close_digest"]
                            != (previous["digest"] if previous else None)
                            or small["read_version"]
                            != {
                                **expected,
                                "read_repair_revision": baseline["read_repair_revision"],
                            }
                        ):
                            raise KernelError(
                                "request_content_invalid", "重放子请求与实际冻结不一致"
                            )
                    results.append({**result, "request_id": child_key})
                    previous = {"period": period, "digest": result["digest"]}
                    continue

                if cached is not None:
                    raise KernelError("request_content_invalid", "重放完成记录缺少实际逐月提交")

                def check(connection, *_, _expected=expected, _previous=previous):
                    self.service.security.validate_authority(self.authority)
                    current = _boundary(self.engine, connection)
                    if current != {
                        "epochs": _expected,
                        "read_repair_revision": baseline["read_repair_revision"],
                        "prefix": _previous,
                    }:
                        raise KernelError("preview_expired", "重放期间来源版本或冻结前缀发生变化")
                    return None

                try:
                    with self.engine.store.connection(read_only=True) as connection:
                        connection.execute("BEGIN")
                        check(connection)
                    periods = Periods(self.engine, authorize_close=check)
                    preview = periods.preview_close(period, owner_confirmation=owner_confirmation)
                    if not results and preview["digest"] != intent["first_preview_digest"]:
                        raise KernelError("preview_expired", "重放首月核对内容已变化")
                    setting = self.service.catalog.company_settings(self.engine.store.company_id)
                    directory = setting["backup_directory"] or str(
                        self.service.catalog.root / "backups" / self.engine.store.path.parent.name
                    )
                    result = periods.close(
                        period,
                        owner_confirmation=owner_confirmation,
                        preview_digest=preview["digest"],
                        epochs=preview["epochs"],
                        request_id=child_key,
                        backup_directory=directory,
                    )
                except KernelError as exc:
                    return self._result(
                        request_id, first_period, last_period, results, period, exc.response()
                    )
                results.append({**result, "request_id": child_key})
                previous = {"period": period, "digest": result["digest"]}
            result = self._result(request_id, first_period, last_period, results, None, None)
            if cached is not None:
                if result != cached:
                    raise KernelError("request_content_invalid", "重放完成记录与实际逐月提交不一致")
                return cached
            return self.engine._write(
                request_id,
                request_hash,
                None,
                (),
                "replay_close_batch_complete",
                lambda connection: result,
            )
        finally:
            self.engine.audit_actor = actor

    def _result(self, request_id, first_period, last_period, results, blocked_period, issue):
        return {
            "status": "completed"
            if blocked_period is None
            else ("partial" if results else "blocked"),
            "company_id": self.engine.store.company_id,
            "database_id": self.engine.store.database_id,
            "request_id": request_id,
            "scope_digest": self.scope.scope_digest,
            "first_period": first_period,
            "last_period": last_period,
            "closed_through": results[-1]["period"] if results else None,
            "blocked_period": blocked_period,
            "issue": issue,
            "results": results,
            "authorization_method": "replay_scope",
        }
