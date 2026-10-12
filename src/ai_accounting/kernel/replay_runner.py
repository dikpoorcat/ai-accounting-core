"""Persistent typed replay preparation and transaction completion hooks."""

from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from .contracts import KernelError
from .replay_contracts import (
    REPLAY_COMMANDS,
    ReplayClassificationItem,
    ReplayConfirm,
    ReplayPrepare,
    ReplayStatusRequest,
)
from .types import YearMonth, canonical, digest


@dataclass(frozen=True)
class ReplayWriteBinding:
    company_id: str
    database_id: str
    database_path: str
    run_id: str
    step_id: str
    request_id: str
    request_digest: bytes | None
    scope_digest: str | None
    output_ref: str | None
    output_fields: tuple[str, ...]


_WRITE_BINDING = contextvars.ContextVar("replay_write_binding", default=None)


@contextmanager
def replay_write_scope(engine, *, run_id, step, request_id, request_digest, scope_digest=None):
    """Bind the next typed write to its durable replay receipt transactionally."""
    binding = ReplayWriteBinding(
        company_id=engine.store.company_id,
        database_id=engine.store.database_id,
        database_path=str(Path(engine.store.path).resolve()),
        run_id=run_id,
        step_id=step["step_id"],
        request_id=request_id,
        request_digest=request_digest,
        scope_digest=scope_digest,
        output_ref=step.get("output_ref"),
        output_fields=tuple(
            json.loads(step["output_fields"])
            if isinstance(step.get("output_fields"), str)
            else (step.get("output_fields") or ())
        ),
    )
    token = _WRITE_BINDING.set(binding)
    try:
        yield binding
    finally:
        _WRITE_BINDING.reset(token)


def replay_audit_actor(actor):
    """Return the ordinary audit actor augmented by the active replay identity."""
    binding = _WRITE_BINDING.get()
    if binding is None:
        return actor
    return {
        **actor,
        "authorization_method": "replay_scope",
        "replay_scope_digest": binding.scope_digest,
        "replay_run_id": binding.run_id,
        "replay_step_id": binding.step_id,
    }


def _check_binding(engine, connection, binding, request_id, action, request_hash):
    if (
        request_id != binding.request_id
        or (binding.request_digest is not None and bytes(request_hash) != binding.request_digest)
        or engine.store.company_id != binding.company_id
        or engine.store.database_id != binding.database_id
        or str(Path(engine.store.path).resolve()) != binding.database_path
    ):
        raise KernelError("replay_binding_mismatch", "重放步骤与当前类型化写入不一致")
    row = connection.execute(
        "SELECT command,request_id,request_digest,status FROM replay_step "
        "WHERE run_id=? AND step_id=?",
        (binding.run_id, binding.step_id),
    ).fetchone()
    if (
        row is None
        or row["request_id"] != request_id
        or (
            row["request_digest"] is not None
            and bytes(row["request_digest"]) != bytes(request_hash)
        )
        or row["status"] not in {"running", "pending"}
    ):
        raise KernelError("replay_step_changed", "重放步骤状态或稳定请求身份已变化")


def before_replay_write(engine, connection, *, request_id, action, request_hash):
    """Engine._write calls this after idempotency lookup and before domain work."""
    binding = _WRITE_BINDING.get()
    if binding is None:
        return
    _check_binding(engine, connection, binding, request_id, action, request_hash)
    from .replay_close import _boundary

    run = connection.execute(
        "SELECT checkpoint FROM replay_run WHERE id=?", (binding.run_id,)
    ).fetchone()
    if run is None or json.loads(run["checkpoint"]) != _boundary(engine, connection):
        raise KernelError("replay_checkpoint_changed", "重放期间事实版本或关账前缀已变化")
    connection.execute(
        "UPDATE replay_step SET status='running',request_digest=coalesce(request_digest,?),"
        "error_code=NULL,last_error=NULL "
        "WHERE run_id=? AND step_id=?",
        (bytes(request_hash), binding.run_id, binding.step_id),
    )
    connection.execute(
        "UPDATE replay_run SET status='running',current_step=? "
        "WHERE id=? AND status IN('pending','running')",
        (binding.step_id, binding.run_id),
    )


def _field(result, path):
    value = result
    for name in path.split("."):
        if not isinstance(value, dict) or name not in value:
            raise KernelError("replay_output_missing", "重放步骤没有返回声明的业务引用")
        value = value[name]
    return value


def after_replay_write(engine, connection, *, request_id, action, request_hash, result):
    """Persist the original typed response and output bindings in its write txn."""
    binding = _WRITE_BINDING.get()
    if binding is None:
        return
    _check_binding(engine, connection, binding, request_id, action, request_hash)
    connection.execute(
        "UPDATE replay_step SET status='completed',response=?,error_code=NULL,last_error=NULL "
        "WHERE run_id=? AND step_id=? AND request_id=?",
        (canonical(result), binding.run_id, binding.step_id, request_id),
    )
    if binding.output_ref:
        for field in binding.output_fields:
            connection.execute(
                "INSERT INTO replay_binding(run_id,reference,field,value,source_step) "
                "VALUES(?,?,?,?,?)",
                (
                    binding.run_id,
                    binding.output_ref,
                    field,
                    canonical(_field(result, field)),
                    binding.step_id,
                ),
            )
    from .replay_close import _boundary

    connection.execute(
        "UPDATE replay_run SET checkpoint=?,status='pending',current_step=NULL WHERE id=?",
        (canonical(_boundary(engine, connection)), binding.run_id),
    )


def compile_replay_steps(steps):
    """Return a stable topological order, inferring edges from replay references."""
    by_id = {item.step_id: item for item in steps}
    if len(by_id) != len(steps):
        raise KernelError("invalid_replay_plan", "重放步骤编号重复")
    outputs = {}
    output_fields = {}
    for item in steps:
        if item.output_ref:
            if item.output_ref in outputs:
                raise KernelError("invalid_replay_plan", "重放输出引用重复")
            outputs[item.output_ref] = item.step_id
            output_fields[item.output_ref] = set(item.output_fields)

    def refs(value):
        found = set()
        if isinstance(value, dict):
            if set(value) == {"$replay_ref", "field"} and all(
                isinstance(value[key], str) for key in ("$replay_ref", "field")
            ):
                ref = value["$replay_ref"]
                if ref not in outputs or value["field"] not in output_fields[ref]:
                    raise KernelError("invalid_replay_plan", "重放步骤引用了未声明的业务输出")
                found.add(outputs[ref])
            else:
                for child in value.values():
                    found.update(refs(child))
        elif isinstance(value, list):
            for child in value:
                found.update(refs(child))
        return found

    edges = {}
    for item in steps:
        dependencies = set(item.depends_on) | refs(item.payload)
        if item.step_id in dependencies or any(
            dependency not in by_id for dependency in dependencies
        ):
            raise KernelError("invalid_replay_plan", "重放步骤依赖不存在或形成自引用")
        edges[item.step_id] = dependencies

    order, remaining = [], list(by_id)
    complete = set()
    while remaining:
        ready = next((item for item in remaining if edges[item] <= complete), None)
        if ready is None:
            raise KernelError("replay_dependency_cycle", "重放步骤依赖存在循环")
        order.append(by_id[ready])
        complete.add(ready)
        remaining.remove(ready)
    return tuple(order), {key: tuple(sorted(value)) for key, value in edges.items()}


def validate_replay_sequence(steps, dependencies, first_period, last_period):
    """Enforce reconstruction-specific ordering without supplying business values."""
    by_id = {item.step_id: item for item in steps}

    def ancestors(step_id):
        found, pending = set(), list(dependencies[step_id])
        while pending:
            item = pending.pop()
            if item in found:
                continue
            found.add(item)
            pending.extend(dependencies[item])
        return found

    previews = {}
    closes = {}
    for step in steps:
        payload = step.payload
        period = payload.get("period") if isinstance(payload, dict) else None
        if step.command == "preview_close" and isinstance(period, str):
            previews[period] = step.step_id
        elif step.command == "close" and isinstance(period, str):
            closes[period] = step.step_id
    expected = [
        str(type(first_period).from_ordinal(value))
        for value in range(first_period.ordinal, last_period.ordinal + 1)
    ]
    if (
        set(closes) != set(expected)
        or set(previews) != set(expected)
        or len(closes) != sum(step.command == "close" for step in steps)
        or len(previews) != sum(step.command == "preview_close" for step in steps)
    ):
        raise KernelError("invalid_replay_plan", "重放必须为范围内每月提供关账预览和唯一关账步骤")
    for period in expected:
        if previews[period] not in ancestors(closes[period]):
            raise KernelError("invalid_replay_plan", "每月关账必须依赖本月重新核对结果")
    for previous, current in pairwise(expected):
        if closes[previous] not in ancestors(previews[current]):
            raise KernelError("invalid_replay_plan", "后续月份预览必须等待前月关账完成")
        if closes[previous] not in ancestors(closes[current]):
            raise KernelError("invalid_replay_plan", "月份关账必须按连续关账前缀排序")

    receive_steps = {step.step_id for step in steps if step.command == "receive_material"}
    group_steps_by_source = {}
    for candidate in steps:
        data = candidate.payload.get("data", {}) if isinstance(candidate.payload, dict) else {}
        if candidate.command == "resolve_material_group" and isinstance(data, dict):
            source_id = data.get("source_id")
            if isinstance(source_id, str):
                group_steps_by_source.setdefault(source_id, set()).add(candidate.step_id)
    payroll_prepares = {
        step.step_id for step in steps if step.command in {"payroll_reuse_basis", "prepare_payroll"}
    }
    required_predecessors = {
        "confirm": {"preview"},
        "confirm_identity_correction": {"preview_identity_correction"},
        "confirm_obligations": {"prepare_obligations"},
        "confirm_material_allocation": {"preview_material_allocation"},
        "confirm_tax_import": {"preview_tax_import"},
        "confirm_asset_activation_batch": {"prepare_asset_activation_batch"},
        "confirm_asset_consumption_month": {"prepare_asset_consumption_month"},
    }
    for step in steps:
        if step.command in {"resolve_material", "resolve_material_group"}:
            if not receive_steps.intersection(ancestors(step.step_id)):
                raise KernelError("invalid_replay_plan", "材料处置必须依赖已接收的原件")
        data = step.payload.get("data", {}) if isinstance(step.payload, dict) else {}
        if (
            step.command == "resolve_material"
            and isinstance(data, dict)
            and data.get("treatment") == "duplicate"
        ):
            source_id = data.get("source_id")
            if not isinstance(source_id, str) or not group_steps_by_source.get(
                source_id, set()
            ).intersection(ancestors(step.step_id)):
                raise KernelError("invalid_replay_plan", "材料复制成员必须依赖已确认的主组")
        if step.command == "confirm_payroll_preparation":
            if not payroll_prepares.intersection(ancestors(step.step_id)):
                raise KernelError("invalid_replay_plan", "工资确认必须沿既有工资准备路径执行")
        required = required_predecessors.get(step.command, set())
        if required and not any(
            by_id[parent].command in required for parent in ancestors(step.step_id)
        ):
            raise KernelError("invalid_replay_plan", "确认步骤必须依赖对应的专用准备结果")
        if step.command == "confirm":
            subjects = step.payload.get("subjects") if isinstance(step.payload, dict) else None
            matching = [
                by_id[parent]
                for parent in ancestors(step.step_id)
                if by_id[parent].command == "preview"
                and isinstance(subjects, list)
                and by_id[parent].payload.get("subjects") == subjects
            ]
            if not matching:
                raise KernelError("invalid_replay_plan", "正式发布必须使用同一组来源的专用预览")
        if step.command == "save_facts":
            facts = step.payload.get("facts", []) if isinstance(step.payload, dict) else []
            if any(
                isinstance(item, dict) and str(item.get("kind", "")).startswith("payroll_")
                for item in facts
            ):
                raise KernelError("invalid_replay_plan", "工资事实必须沿既有工资准备入口登记")

    opening_kinds = {
        "opening_bank",
        "opening_cash",
        "opening_obligation",
        "opening_asset",
        "opening_loan",
        "opening_tax",
        "opening_payroll_payable",
        "opening_payroll_state",
        "opening_equity",
        "opening_money_fund",
        "opening_package",
    }
    opening_subjects = {
        item.get("subject_id")
        for step in steps
        if step.command == "save_facts"
        for item in step.payload.get("facts", ())
        if isinstance(item, dict)
        and item.get("kind") in opening_kinds
        and isinstance(item.get("subject_id"), str)
    }
    if opening_subjects:
        opening_publications = {
            subject
            for step in steps
            if step.command == "confirm"
            for subject in step.payload.get("subjects", ())
            if isinstance(subject, str)
        }
        if not opening_subjects.issubset(opening_publications):
            raise KernelError("invalid_replay_plan", "重放期初来源必须通过正式预览和发布采用")
        first_preview = previews[expected[0]]
        if not any(
            by_id[parent].command == "confirm"
            and opening_subjects.intersection(by_id[parent].payload.get("subjects", ()))
            for parent in ancestors(first_preview)
        ):
            raise KernelError("invalid_replay_plan", "首月关账预览必须等待期初正式采用")

    planned_bank_statements = {}
    planned_bank_reconciliations = []
    for step in steps:
        if step.command != "save_facts" or not isinstance(step.payload, dict):
            continue
        for item in step.payload.get("facts", ()):
            if not isinstance(item, dict) or not isinstance(item.get("subject_id"), str):
                continue
            if item.get("kind") == "bank_statement":
                planned_bank_statements[item["subject_id"]] = step.step_id
            elif item.get("kind") == "bank_reconciliation":
                data = item.get("data", {})
                planned_bank_reconciliations.append(
                    (
                        item["subject_id"],
                        data.get("statement_id") if isinstance(data, dict) else None,
                    )
                )
    for reconciliation_subject, statement_subject in planned_bank_reconciliations:
        publishers = [
            step
            for step in steps
            if step.command == "confirm"
            and reconciliation_subject in step.payload.get("subjects", ())
        ]
        if not publishers:
            raise KernelError("invalid_replay_plan", "银行对账必须经过正式预览和发布")
        if statement_subject in planned_bank_statements and not any(
            statement_subject in publisher.payload.get("subjects", ())
            or any(
                by_id[parent].command == "confirm"
                and statement_subject in by_id[parent].payload.get("subjects", ())
                for parent in ancestors(publisher.step_id)
            )
            for publisher in publishers
        ):
            raise KernelError("invalid_replay_plan", "银行对账发布必须依赖所引用流水的正式发布")

    member_copies = {}
    for step in steps:
        data = step.payload.get("data", {}) if isinstance(step.payload, dict) else {}
        if (
            step.command == "resolve_material"
            and isinstance(data, dict)
            and data.get("treatment") == "duplicate"
            and isinstance(data.get("source_id"), str)
            and isinstance(data.get("location"), str)
        ):
            member_copies.setdefault(data["source_id"], {})[data["location"]] = step.step_id
    for step in steps:
        data = step.payload.get("data", {}) if isinstance(step.payload, dict) else {}
        if step.command != "resolve_material_group" or not isinstance(data, dict):
            continue
        source_id = data.get("source_id")
        members = data.get("members", ())
        if not isinstance(source_id, str) or not isinstance(members, list):
            continue
        copied = member_copies.get(source_id, {})
        if any(
            not isinstance(member, dict)
            or not isinstance(member.get("location"), str)
            or member["location"] not in copied
            for member in members
        ):
            raise KernelError("invalid_replay_plan", "复制组的每个成员都必须有对应的类型化复制处置")
        copy_member_steps = {
            copied[member["location"]]
            for member in members
            if isinstance(member, dict) and member.get("location") in copied
        }
        if copy_member_steps and not copy_member_steps.issubset(ancestors(step.step_id)):
            raise KernelError("invalid_replay_plan", "复制组必须依赖同组全部成员复制处置")


def _invalid_contract(error):
    return KernelError("invalid_replay_plan", "重放计划格式或依赖不符合合同")


def _request_key(seed, step_id):
    return "replay-step-" + digest([seed, step_id]).hex()


def _authority_data(authority):
    return {
        "catalog_instance_id": authority.catalog_instance_id,
        "owner_id": authority.owner_id,
        "session_id": authority.session_id,
        "credential_version": authority.credential_version,
    }


def _authority_from_data(value):
    from .security.service import Authority

    return Authority(**value)


def _schema_fingerprint(engine):
    from .versions import database_format

    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return database_format(connection, bundle=engine.store.bundle, kind="company")[
            "fingerprint"
        ]


def _boundary(engine, connection=None):
    from .replay_close import _boundary as close_boundary

    if connection is not None:
        return close_boundary(engine, connection)
    with engine.store.connection(read_only=True) as opened:
        opened.execute("BEGIN")
        return close_boundary(engine, opened)


def _safe_step_payload(service, company_id, seed, step):
    from .command_schema import validate_command

    if step.command not in REPLAY_COMMANDS:
        raise KernelError("invalid_replay_plan", "重放计划包含未允许的命令")
    payload = dict(step.payload)
    if step.command == "close":
        if payload.get("approval_id"):
            raise KernelError("invalid_replay_plan", "重放关账不能携带普通密码批准编号")
        payload["approval_id"] = "replay-scope-authority"
    model = service.command_models[step.command]
    schema = model.json_schema()
    fields = dict(schema.get("properties", {}))
    for definition in schema.get("$defs", {}).values():
        fields.update(definition.get("properties", {}))
    if "company_id" in fields:
        if payload.get("company_id", company_id) != company_id:
            raise KernelError("company_mismatch", "重放步骤超出当前公司范围")
        payload["company_id"] = company_id
    if "request_id" in fields:
        supplied = payload.get("request_id")
        stable = _request_key(seed, step.step_id)
        if supplied is not None and supplied != stable:
            raise KernelError("invalid_replay_plan", "重放步骤请求编号必须由计划稳定生成")
        payload["request_id"] = stable

    def placeholder_shape(shape):
        if not isinstance(shape, dict):
            return "replay-placeholder"
        if "$ref" in shape:
            target = shape["$ref"].split("/")[-1]
            shape = schema.get("$defs", {}).get(target, shape)
        if "anyOf" in shape:
            return placeholder_shape(
                next(
                    (item for item in shape["anyOf"] if item.get("type") != "null"),
                    shape["anyOf"][0],
                )
            )
        kind = shape.get("type")
        if kind == "object":
            properties = shape.get("properties", {})
            required = shape.get("required", list(properties))
            return {
                key: placeholder_shape(properties[key]) for key in required if key in properties
            }
        if kind == "array":
            return []
        if kind == "integer":
            return 0
        if kind == "number":
            return 0
        if kind == "boolean":
            return False
        if kind == "null":
            return None
        return "replay-placeholder"

    def placeholders(value, shape):
        if isinstance(value, dict):
            if set(value) == {"$replay_ref", "field"}:
                return placeholder_shape(shape)
            properties = shape.get("properties", {}) if isinstance(shape, dict) else {}
            return {
                key: placeholders(child, properties.get(key, {})) for key, child in value.items()
            }
        if isinstance(value, list):
            child_shape = shape.get("items", {}) if isinstance(shape, dict) else {}
            return [placeholders(child, child_shape) for child in value]
        return value

    try:
        validate_command(
            service.command_models,
            step.command,
            placeholders(payload, schema),
            registry=service.registry,
        )
    except (KernelError, ValueError, TypeError):
        raise KernelError("invalid_replay_plan", "重放步骤不符合现有类型化命令合同") from None
    return payload


def _dispatch_replay_close(service, engine, authority, payload, run_id, scope):
    """Use Periods.close under the explicit replay scope, without password approval."""
    from .periods import Periods

    period = payload["period"]
    scope.require_target(engine, period, period, payload["owner_confirmation"])
    engine.commit_guard = lambda: service._commit_authority(authority)
    engine.audit_actor = {
        **_authority_data(authority),
        "authorization_method": "replay_scope",
        "replay_scope_digest": scope.scope_digest,
        "replay_run_id": run_id,
    }

    def authorize(connection, selected_period, preview_digest, epochs):
        service.security.validate_authority(authority)
        scope.require_target(
            engine, selected_period, selected_period, payload["owner_confirmation"]
        )
        return None

    return Periods(engine, authorize_close=authorize).close(
        period,
        owner_confirmation=payload["owner_confirmation"],
        preview_digest=payload["preview_digest"],
        epochs=payload["epochs"],
        request_id=payload["request_id"],
        backup_directory=None,
    )


def _source_manifest_payload(request):
    return [item.model_dump(mode="json") for item in request.source_manifest]


def prepare_replay(service, engine, authority, payload):
    """Persist an immutable, scope/build/schema/authority-bound replay plan."""
    from .runtime_snapshot import capture_runtime_snapshot
    from .versions import database_format

    try:
        request = ReplayPrepare.model_validate_json(canonical(payload))
    except Exception as exc:
        raise _invalid_contract(exc) from None
    scope = service.replay_close_scope
    if scope is None:
        raise KernelError("replay_mode_required", "请先以明确范围启动重放测试服务")
    service.security.validate_authority(authority)
    if (
        request.company_id != engine.store.company_id
        or request.database_id != engine.store.database_id
        or authority.catalog_instance_id != scope.config.catalog_instance_id
    ):
        raise KernelError("replay_scope_mismatch", "重放计划不属于当前目录、公司或数据库")
    normalized = request.model_dump(mode="json")
    with engine.store.connection(read_only=True) as connection:
        prior = connection.execute(
            "SELECT id,payload,plan_digest,catalog_id,company_id,database_id,"
            "scope_digest,authority "
            "FROM replay_plan WHERE request_id=?",
            (request.request_id,),
        ).fetchone()
        if prior is not None:
            prior_payload = json.loads(prior["payload"])
            if prior_payload.get("request") != normalized:
                raise KernelError("idempotency_conflict", "同一准备请求编号携带了不同计划")
            if (
                prior["company_id"] != engine.store.company_id
                or prior["database_id"] != engine.store.database_id
                or prior["catalog_id"] != scope.config.catalog_instance_id
                or bytes(prior["scope_digest"]).hex() != scope.scope_digest
                or json.loads(prior["authority"]) != _authority_data(authority)
            ):
                raise KernelError("replay_context_changed", "计划绑定的会话、公司或范围已变化")
            prior_context = prior_payload["context"]
            return {
                "status": "prepared",
                "plan_id": prior["id"],
                "plan_digest": bytes(prior["plan_digest"]).hex(),
                "source_summary": prior_payload["source_summary"],
                "steps": [item["step_id"] for item in prior_payload["ordered_steps"]],
                "scope_digest": prior_context["scope_digest"],
                "build_id": prior_context["build_id"],
                "schema_fingerprint": prior_context["schema_fingerprint"],
                "boundary": prior_context["boundary"],
            }
    scope.require_target(
        engine, request.first_period, request.last_period, request.owner_confirmation
    )
    source_manifest = _source_manifest_payload(request)
    if digest(source_manifest).hex() != request.source_manifest_digest:
        raise KernelError("replay_source_manifest_mismatch", "公司源清单摘要与提交内容不一致")
    order, dependencies = compile_replay_steps(request.steps)
    validate_replay_sequence(order, dependencies, request.first_period, request.last_period)
    safe_payloads = {
        item.step_id: _safe_step_payload(service, request.company_id, request.request_id, item)
        for item in order
    }
    for item in order:
        if item.command not in {"preview_close", "close"}:
            continue
        close_payload = safe_payloads[item.step_id]
        if (
            close_payload.get("owner_confirmation") != request.owner_confirmation
            or not isinstance(close_payload.get("period"), str)
            or not str(request.first_period) <= close_payload["period"] <= str(request.last_period)
        ):
            raise KernelError("replay_scope_mismatch", "关账步骤超出计划月份或负责人确认范围")
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT id,payload,plan_digest,catalog_id,company_id,database_id,"
                "scope_digest,authority "
                "FROM replay_plan WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                prior_payload = json.loads(existing["payload"])
                if prior_payload.get("request") != normalized:
                    raise KernelError("idempotency_conflict", "同一准备请求编号携带了不同计划")
                if (
                    existing["company_id"] != engine.store.company_id
                    or existing["database_id"] != engine.store.database_id
                    or existing["catalog_id"] != scope.config.catalog_instance_id
                    or bytes(existing["scope_digest"]).hex() != scope.scope_digest
                    or json.loads(existing["authority"]) != _authority_data(authority)
                ):
                    raise KernelError("replay_context_changed", "计划绑定的会话、公司或范围已变化")
                prior_context = prior_payload["context"]
                connection.commit()
                return {
                    "status": "prepared",
                    "plan_id": existing["id"],
                    "plan_digest": bytes(existing["plan_digest"]).hex(),
                    "source_summary": prior_payload["source_summary"],
                    "steps": [item["step_id"] for item in prior_payload["ordered_steps"]],
                    "scope_digest": prior_context["scope_digest"],
                    "build_id": prior_context["build_id"],
                    "schema_fingerprint": prior_context["schema_fingerprint"],
                    "boundary": prior_context["boundary"],
                }
            boundary = _boundary(engine, connection)
            company_format = database_format(connection, bundle=engine.store.bundle, kind="company")
            evidence_refs = {
                item.evidence_digest for item in request.source_manifest if item.evidence_digest
            }
            evidence_refs.add(request.owner_confirmation)
            if evidence_refs:
                found = {
                    row[0].hex()
                    for row in connection.execute(
                        "SELECT digest FROM evidence WHERE digest IN ("
                        + ",".join("?" for _ in evidence_refs)
                        + ")",
                        tuple(bytes.fromhex(item) for item in sorted(evidence_refs)),
                    )
                }
                missing = set(evidence_refs) - found
                if missing:
                    raise KernelError("replay_evidence_missing", "重放源清单引用了尚未保全的证据")
            snapshot = capture_runtime_snapshot()
            inherited_history = [
                {
                    "plan_id": row["plan_id"],
                    "run_id": row["run_id"],
                    "request_id": row["request_id"],
                    "first_period": str(YearMonth.from_ordinal(row["first_period"])),
                    "last_period": str(YearMonth.from_ordinal(row["last_period"])),
                    "source_manifest_digest": bytes(row["source_manifest_digest"]).hex(),
                    "plan_digest": bytes(row["plan_digest"]).hex(),
                }
                for row in connection.execute(
                    "SELECT p.id AS plan_id,r.id AS run_id,p.request_id,p.first_period,"
                    "p.last_period,"
                    "p.source_manifest_digest,p.plan_digest FROM replay_run r "
                    "JOIN replay_plan p ON p.id=r.plan_id WHERE r.status='completed' "
                    "AND p.company_id=? AND p.database_id=? AND p.scope_digest=? "
                    "AND p.last_period<? ORDER BY p.first_period,p.last_period,p.id",
                    (
                        request.company_id,
                        request.database_id,
                        bytes.fromhex(scope.scope_digest),
                        request.first_period.ordinal,
                    ),
                )
            ]
            context = {
                "catalog_id": scope.config.catalog_instance_id,
                "scope_digest": scope.scope_digest,
                "schema_fingerprint": company_format["fingerprint"],
                "build_id": snapshot.build_id,
                "runtime_snapshot": snapshot.public(),
                "boundary": boundary,
            }
            compiled = {
                "request": normalized,
                "context": context,
                "ordered_steps": [
                    {
                        **item.model_dump(mode="json"),
                        "payload": safe_payloads[item.step_id],
                        "dependencies": list(dependencies[item.step_id]),
                    }
                    for item in order
                ],
                "source_summary": {
                    "digest": request.source_manifest_digest,
                    "count": len(source_manifest),
                    "adopt": sum(item["disposition"] == "adopt" for item in source_manifest),
                    "exclude": sum(item["disposition"] == "exclude" for item in source_manifest),
                    "by_kind": {
                        kind: sum(item["kind"] == kind for item in source_manifest)
                        for kind in sorted({item["kind"] for item in source_manifest})
                    },
                    "documents": [
                        {"source_id": item["source_id"], "evidence_digest": item["evidence_digest"]}
                        for item in source_manifest
                        if item["kind"] == "document"
                    ],
                    "preserved_evidence": sorted(request.preserved_evidence),
                    "inherited_completed_history": inherited_history,
                },
            }
            request_hash = digest(normalized)
            plan_id = "replay-plan-" + digest([request.request_id, request_hash.hex()]).hex()
            plan_digest = digest(compiled)
            authority_json = canonical(_authority_data(authority))
            connection.execute(
                "INSERT INTO replay_plan(id,request_id,payload,plan_digest,catalog_id,"
                "company_id,database_id,"
                "first_period,last_period,source_manifest_digest,owner_confirmation,scope_digest,"
                "schema_fingerprint,build_id,authority,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    request.request_id,
                    canonical(compiled),
                    plan_digest,
                    context["catalog_id"],
                    request.company_id,
                    request.database_id,
                    request.first_period.ordinal,
                    request.last_period.ordinal,
                    bytes.fromhex(request.source_manifest_digest),
                    bytes.fromhex(request.owner_confirmation),
                    bytes.fromhex(scope.scope_digest),
                    company_format["fingerprint"],
                    snapshot.build_id,
                    authority_json,
                    "prepared",
                    str(service.security.now()),
                ),
            )
            connection.commit()
            return {
                "status": "prepared",
                "plan_id": plan_id,
                "plan_digest": plan_digest.hex(),
                "source_summary": compiled["source_summary"],
                "steps": [item.step_id for item in order],
                "scope_digest": scope.scope_digest,
                "build_id": snapshot.build_id,
                "schema_fingerprint": company_format["fingerprint"],
                "boundary": boundary,
            }
        except BaseException:
            connection.rollback()
            raise


def confirm_replay(service, engine, authority, payload):
    """Queue a confirmed plan durably; return immediately without running a step."""
    from .runtime_snapshot import capture_runtime_snapshot
    from .versions import database_format

    try:
        request = ReplayConfirm.model_validate(payload)
    except Exception as exc:
        raise _invalid_contract(exc) from None
    service.security.validate_authority(authority)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            body = request.model_dump(mode="json")
            scope = service.replay_close_scope
            prior = connection.execute(
                "SELECT r.id,r.plan_id,r.payload,p.plan_digest,p.catalog_id,p.company_id,"
                "p.database_id,"
                "p.scope_digest,p.authority FROM replay_run r JOIN replay_plan p ON p.id=r.plan_id "
                "WHERE r.request_id=?",
                (request.request_id,),
            ).fetchone()
            if prior is not None:
                if prior["payload"] != canonical(body) or prior["plan_id"] != request.plan_id:
                    raise KernelError("idempotency_conflict", "同一确认请求编号携带了不同载荷")
                if bytes(prior["plan_digest"]).hex() != request.plan_digest:
                    raise KernelError("idempotency_conflict", "同一确认请求编号携带了不同计划摘要")
                if (
                    scope is None
                    or prior["company_id"] != engine.store.company_id
                    or prior["database_id"] != engine.store.database_id
                    or prior["catalog_id"] != scope.config.catalog_instance_id
                    or bytes(prior["scope_digest"]).hex() != scope.scope_digest
                    or json.loads(prior["authority"]) != _authority_data(authority)
                ):
                    raise KernelError(
                        "replay_context_changed", "确认回执绑定的会话、公司或范围已变化"
                    )
                connection.commit()
                return {"status": "pending", "run_id": prior["id"]}
            plan = connection.execute(
                "SELECT * FROM replay_plan WHERE id=?", (request.plan_id,)
            ).fetchone()
            if plan is None or bytes(plan["plan_digest"]).hex() != request.plan_digest:
                raise KernelError("replay_plan_missing", "重放计划不存在或摘要已变化")
            compiled = json.loads(plan["payload"])
            scope = service.replay_close_scope
            snapshot = capture_runtime_snapshot()
            fmt = database_format(connection, bundle=engine.store.bundle, kind="company")
            if (
                scope is None
                or plan["catalog_id"] != scope.config.catalog_instance_id
                or bytes(plan["scope_digest"]).hex() != scope.scope_digest
                or plan["build_id"] != snapshot.build_id
                or plan["schema_fingerprint"] != fmt["fingerprint"]
                or compiled["context"].get("runtime_snapshot") != snapshot.public()
                or json.loads(plan["authority"]) != _authority_data(authority)
                or _boundary(engine, connection) != compiled["context"]["boundary"]
            ):
                raise KernelError(
                    "replay_context_changed", "计划绑定的会话、范围、版本或关账前缀已变化"
                )
            run_id = "replay-run-" + digest([request.plan_id, request.request_id]).hex()
            checkpoint = compiled["context"]["boundary"]
            connection.execute(
                "INSERT INTO replay_run(id,plan_id,request_id,payload,status,checkpoint) "
                "VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    request.plan_id,
                    request.request_id,
                    canonical(body),
                    "pending",
                    canonical(checkpoint),
                ),
            )
            for position, step in enumerate(compiled["ordered_steps"]):
                step_request = _request_key(compiled["request"]["request_id"], step["step_id"])
                step_payload = step["payload"]
                connection.execute(
                    "INSERT INTO replay_step(run_id,position,step_id,command,payload,"
                    "request_id,request_digest,"
                    "dependencies,output_ref,output_fields,status) "
                    "VALUES(?,?,?,?,?,?,NULL,?,?,?,?)",
                    (
                        run_id,
                        position,
                        step["step_id"],
                        step["command"],
                        canonical(step_payload),
                        step_request,
                        canonical(step["dependencies"]),
                        step.get("output_ref"),
                        canonical(step.get("output_fields", [])),
                        "pending",
                    ),
                )
            connection.execute(
                "UPDATE replay_plan SET status='confirmed' WHERE id=?", (request.plan_id,)
            )
            connection.commit()
            return {"status": "pending", "run_id": run_id}
        except BaseException:
            connection.rollback()
            raise


def _validate_run_context(service, engine, plan, run, authority, *, check_checkpoint=True):
    from .runtime_snapshot import capture_runtime_snapshot

    issues = []
    try:
        service.security.validate_authority(authority)
    except Exception:
        issues.append("session")
    scope = service.replay_close_scope
    if scope is None or bytes(plan["scope_digest"]).hex() != scope.scope_digest:
        issues.append("scope")
    if (
        plan["company_id"] != engine.store.company_id
        or plan["database_id"] != engine.store.database_id
    ):
        issues.append("company")
    if json.loads(plan["authority"]) != _authority_data(authority):
        issues.append("authority")
    snapshot = capture_runtime_snapshot()
    if plan["build_id"] != snapshot.build_id:
        issues.append("build")
    with engine.store.connection(read_only=True) as connection:
        stored = connection.execute(
            "SELECT payload FROM replay_plan WHERE id=?", (plan["plan_id"],)
        ).fetchone()
    if (
        stored is None
        or json.loads(stored["payload"]).get("context", {}).get("runtime_snapshot")
        != snapshot.public()
    ):
        issues.append("runtime_snapshot")
    if plan["schema_fingerprint"] != _schema_fingerprint(engine):
        issues.append("schema")
    if check_checkpoint and json.loads(run["checkpoint"]) != _boundary(engine):
        issues.append("prefix_or_source_version")
    return issues


def replay_status(service, engine, authority, run_id, step_id=None):
    """Return a bounded status snapshot; never advances pending work."""
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT r.*,p.company_id,p.database_id,p.scope_digest,p.build_id,"
            "p.schema_fingerprint,p.authority "
            "FROM replay_run r JOIN replay_plan p ON p.id=r.plan_id WHERE r.id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KernelError("replay_run_missing", "重放运行记录不存在")
        issues = _validate_run_context(service, engine, row, row, authority)
        steps = [
            dict(item)
            for item in connection.execute(
                "SELECT step_id,command,status,error_code,last_error FROM replay_step "
                "WHERE run_id=? ORDER BY position LIMIT 100",
                (run_id,),
            )
        ]
        counts = connection.execute(
            "SELECT count(*) AS total,sum(status='completed') AS completed "
            "FROM replay_step WHERE run_id=?",
            (run_id,),
        ).fetchone()
        timings = {}
        for item in connection.execute(
            "SELECT s.step_id,t.stage,t.elapsed_ms FROM replay_step s "
            "JOIN operation_timing t ON t.request_id=s.request_id "
            "WHERE s.run_id=? ORDER BY s.position,t.stage LIMIT 600",
            (run_id,),
        ):
            timings.setdefault(item["step_id"], {})[item["stage"]] = item["elapsed_ms"]
        close_row = connection.execute(
            "SELECT period,digest FROM period_close ORDER BY period DESC LIMIT 1"
        ).fetchone()
        closed_prefix = None
        if close_row is not None:
            closed_prefix = {
                "period": str(YearMonth.from_ordinal(close_row["period"])),
                "digest": bytes(close_row["digest"]).hex(),
            }
        backup_row = connection.execute(
            "SELECT id,status,error_code,result FROM jobs WHERE kind='portable_backup' "
            "AND json_extract(payload,'$.replay_run_id')=? ORDER BY rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if backup_row is not None:
            backup_task = dict(backup_row)
            if backup_task["result"]:
                backup_task["result"] = json.loads(backup_task["result"])
        else:
            backup_task = None
        result = {
            "status": row["status"],
            "run_id": run_id,
            "current_step": row["current_step"],
            "context_valid": not issues,
            "context_issues": issues,
            "steps": steps,
            "steps_truncated": len(steps) == 100,
            "progress": {
                "completed_steps": counts["completed"] or 0,
                "total_steps": counts["total"],
                "current_phase": next(
                    (item["command"] for item in steps if item["step_id"] == row["current_step"]),
                    None,
                ),
                "closed_through": closed_prefix["period"] if closed_prefix else None,
                "closed_prefix": closed_prefix,
                "backup_task": backup_task,
                "step_timings_ms": timings,
            },
            "result": json.loads(row["result"]) if row["result"] else None,
        }
        if step_id is not None:
            receipt = connection.execute(
                "SELECT step_id,command,request_id,status,response,error_code,last_error "
                "FROM replay_step WHERE run_id=? AND step_id=?",
                (run_id, step_id),
            ).fetchone()
            if receipt is None:
                raise KernelError("replay_step_missing", "重放步骤不存在")
            result["receipt"] = dict(receipt)
            if receipt["response"]:
                result["receipt"]["response"] = json.loads(receipt["response"])
        return result


def resume_replay(service, engine, authority, run_id):
    """Recheck the persisted context and checkpoint, then leave execution to JobRunner."""
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT r.*,p.company_id,p.database_id,p.scope_digest,p.build_id,"
            "p.schema_fingerprint,p.authority "
            "FROM replay_run r JOIN replay_plan p ON p.id=r.plan_id WHERE r.id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KernelError("replay_run_missing", "重放运行记录不存在")
        issues = _validate_run_context(service, engine, row, row, authority)
        if issues:
            raise KernelError("replay_context_changed", "恢复前核对失败：" + ",".join(issues))
        if row["status"] in {"blocked", "failed"}:
            raise KernelError("replay_requires_new_plan", "阻断步骤须补齐资料并重新核对计划")
        running = connection.execute(
            "SELECT * FROM replay_step WHERE run_id=? AND status='running' "
            "ORDER BY position LIMIT 1",
            (run_id,),
        ).fetchone()
        if running:
            committed = connection.execute(
                "SELECT digest,result FROM request WHERE id=?", (running["request_id"],)
            ).fetchone()
            if committed:
                if running["request_digest"] is not None and bytes(
                    running["request_digest"]
                ) != bytes(committed["digest"]):
                    raise KernelError(
                        "request_content_invalid", "已提交业务请求与重放稳定载荷不一致"
                    )
                response = json.loads(committed["result"])
                connection.execute(
                    "UPDATE replay_step SET request_digest=?,status='completed',response=? "
                    "WHERE run_id=? AND step_id=?",
                    (committed["digest"], canonical(response), run_id, running["step_id"]),
                )
                if running["output_ref"]:
                    for field in json.loads(running["output_fields"]):
                        connection.execute(
                            "INSERT OR IGNORE INTO replay_binding(run_id,reference,field,"
                            "value,source_step) "
                            "VALUES(?,?,?,?,?)",
                            (
                                run_id,
                                running["output_ref"],
                                field,
                                canonical(_field(response, field)),
                                running["step_id"],
                            ),
                        )
            else:
                connection.execute(
                    "UPDATE replay_step SET status='pending' WHERE run_id=? AND step_id=?",
                    (run_id, running["step_id"]),
                )
        connection.execute(
            "UPDATE replay_run SET status='pending',current_step=NULL,last_error=NULL,"
            "error_code=NULL "
            "WHERE id=? AND status IN('running','pending')",
            (run_id,),
        )
        connection.commit()
    return {"status": "pending", "run_id": run_id}


def run_ready(service, limit=1, *, company_id=None):
    """Advance at most one ready typed step per worker call."""
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a nonnegative integer")
    if limit == 0:
        return []
    from .command_schema import validate_command
    from .engine import Engine

    for company in service.catalog.companies():
        if company_id is not None and company["id"] != company_id:
            continue
        store = service.catalog.bind(company["id"])
        engine = Engine(store)
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT r.*,p.company_id,p.database_id,p.scope_digest,p.build_id,"
                "p.schema_fingerprint,p.authority "
                "FROM replay_run r JOIN replay_plan p ON p.id=r.plan_id "
                "WHERE r.status IN('pending','running') ORDER BY r.rowid LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                continue
            if row["company_id"] != store.company_id or row["database_id"] != store.database_id:
                connection.rollback()
                continue
            ready = connection.execute(
                "SELECT s.* FROM replay_step s WHERE s.run_id=? AND s.status='pending' "
                "AND NOT EXISTS(SELECT 1 FROM json_each(s.dependencies) d "
                "LEFT JOIN replay_step p ON p.run_id=s.run_id AND p.step_id=d.value "
                "WHERE p.status IS NULL OR p.status<>'completed') ORDER BY s.position LIMIT 1",
                (row["id"],),
            ).fetchone()
            if ready is None:
                unfinished = connection.execute(
                    "SELECT count(*) FROM replay_step WHERE run_id=? AND status<>'completed'",
                    (row["id"],),
                ).fetchone()[0]
                if not unfinished:
                    connection.execute(
                        "UPDATE replay_run SET status='completed',finished_at=? WHERE id=?",
                        (str(service.security.now()), row["id"]),
                    )
                    connection.execute(
                        "UPDATE replay_plan SET status='completed' WHERE id=?", (row["plan_id"],)
                    )
                connection.commit()
                continue
            compiled = json.loads(
                connection.execute(
                    "SELECT payload FROM replay_plan WHERE id=?", (row["plan_id"],)
                ).fetchone()[0]
            )
            issues = _validate_run_context(
                service, engine, row, row, _authority_from_data(json.loads(row["authority"]))
            )
            if issues:
                connection.execute(
                    "UPDATE replay_run SET status='blocked',error_code='replay_context_changed',"
                    "last_error=? WHERE id=?",
                    (canonical(issues), row["id"]),
                )
                connection.commit()
                return [{"run_id": row["id"], "status": "blocked", "issues": issues}]
            bindings = {
                (item["reference"], item["field"]): json.loads(item["value"])
                for item in connection.execute(
                    "SELECT reference,field,value FROM replay_binding WHERE run_id=?", (row["id"],)
                )
            }
            step = dict(ready)
            connection.commit()

        try:
            authority = _authority_from_data(json.loads(row["authority"]))
            service.security.validate_authority(authority)
            raw_payload = json.loads(step["payload"])

            def resolve(value, *, current_bindings=bindings):
                if isinstance(value, dict):
                    if set(value) == {"$replay_ref", "field"}:
                        key = (value["$replay_ref"], value["field"])
                        if key not in current_bindings:
                            raise KernelError(
                                "replay_binding_missing", "前置步骤尚未产生所需业务引用"
                            )
                        return current_bindings[key]
                    return {
                        key: resolve(child, current_bindings=current_bindings)
                        for key, child in value.items()
                    }
                if isinstance(value, list):
                    return [resolve(child, current_bindings=current_bindings) for child in value]
                return value

            payload_value = resolve(raw_payload)
            payload_value["company_id"] = store.company_id
            command_schema = service.command_models[step["command"]].json_schema()
            command_fields = dict(command_schema.get("properties", {}))
            for definition in command_schema.get("$defs", {}).values():
                command_fields.update(definition.get("properties", {}))
            if "request_id" in command_fields:
                payload_value["request_id"] = step["request_id"]
            payload_value = validate_command(
                service.command_models, step["command"], payload_value, registry=service.registry
            )

            # Freeze the fully resolved wire body before invoking the typed entry.
            with store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE replay_step SET payload=?,status='running' "
                    "WHERE run_id=? AND step_id=? "
                    "AND status='pending'",
                    (canonical(payload_value), row["id"], step["step_id"]),
                )
                connection.execute(
                    "UPDATE replay_run SET status='running',current_step=? WHERE id=?",
                    (step["step_id"], row["id"]),
                )
                connection.commit()

            if step["command"] == "close":
                from .backup_tasks import _replay_close_policy, close_backup_policy

                period = payload_value["period"]
                last = compiled["request"]["last_period"]
                settings = service.catalog.company_settings(store.company_id)
                directory = settings["backup_directory"] or str(
                    service.catalog.root / "backups" / store.path.parent.name
                )
                context = close_backup_policy(
                    store.company_id,
                    store.database_id,
                    _replay_close_policy(
                        directory,
                        row["id"],
                        final=(YearMonth(period).ordinal == YearMonth(last).ordinal),
                    ),
                )
            else:
                context = nullcontext()
            action_engine = service.engine(store.company_id)
            with (
                context,
                replay_write_scope(
                    action_engine,
                    run_id=row["id"],
                    step=step,
                    request_id=step["request_id"],
                    request_digest=None,
                    scope_digest=service.replay_close_scope.scope_digest,
                ),
            ):
                service.security.validate_authority(authority)
                if step["command"] == "close":
                    result = _dispatch_replay_close(
                        service,
                        action_engine,
                        authority,
                        payload_value,
                        row["id"],
                        service.replay_close_scope,
                    )
                else:
                    result = service._dispatch(step["command"], payload_value, authority=authority)
            if (
                step["command"] == "preview_close"
                and payload_value.get("period") == compiled["request"]["first_period"]
                and not (result.get("manifest") or {}).get("opening_calculation_id")
            ):
                raise KernelError(
                    "replay_opening_adoption_required",
                    "首月关账前必须核对期初核算已由本月真实采用",
                )
            # Read-only preparations do not pass through Engine._write.
            with store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT status FROM replay_step WHERE run_id=? AND step_id=?",
                    (row["id"], step["step_id"]),
                ).fetchone()
                if existing["status"] != "completed":
                    connection.execute(
                        "UPDATE replay_step SET status='completed',response=? "
                        "WHERE run_id=? AND step_id=?",
                        (canonical(result), row["id"], step["step_id"]),
                    )
                    if step.get("output_ref"):
                        for field in json.loads(step["output_fields"]):
                            connection.execute(
                                "INSERT INTO replay_binding(run_id,reference,field,value,"
                                "source_step) "
                                "VALUES(?,?,?,?,?)",
                                (
                                    row["id"],
                                    step["output_ref"],
                                    field,
                                    canonical(_field(result, field)),
                                    step["step_id"],
                                ),
                            )
                    connection.execute(
                        "UPDATE replay_run SET checkpoint=?,status='pending',current_step=NULL "
                        "WHERE id=?",
                        (canonical(_boundary(engine)), row["id"]),
                    )
                connection.commit()
            return [{"run_id": row["id"], "step_id": step["step_id"], "status": "completed"}]
        except Exception as exc:
            code = getattr(exc, "code", "replay_step_failed")
            message = getattr(exc, "message", type(exc).__name__)
            with store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                step_now = connection.execute(
                    "SELECT status FROM replay_step WHERE run_id=? AND step_id=?",
                    (row["id"], step["step_id"]),
                ).fetchone()
                committed = bool(step_now and step_now["status"] == "completed")
                if step_now and step_now["status"] != "completed":
                    connection.execute(
                        "UPDATE replay_step SET status='blocked',error_code=?,last_error=? "
                        "WHERE run_id=? AND step_id=?",
                        (code, str(message)[:500], row["id"], step["step_id"]),
                    )
                    connection.execute(
                        "UPDATE replay_run SET status='blocked',error_code=?,last_error=? "
                        "WHERE id=?",
                        (code, str(message)[:500], row["id"]),
                    )
                connection.commit()
            return [
                {
                    "run_id": row["id"],
                    "step_id": step["step_id"],
                    "status": "completed" if committed else "blocked",
                    **({"response_lost": True} if committed else {"code": code}),
                }
            ]
    return []


class ReplayCommands:
    """Service-bound typed command adapters used by the normal command router."""

    def __init__(self, service, engine, authority):
        self.service, self.engine, self.authority = service, engine, authority

    def prepare_replay(self, company_id: str, request: ReplayPrepare):
        request = ReplayPrepare.model_validate_json(canonical(request))
        if company_id != self.engine.store.company_id or request.company_id != company_id:
            raise KernelError("company_mismatch", "重放计划超出当前公司范围")
        with self.service._commit_authority(self.authority):
            return prepare_replay(
                self.service, self.engine, self.authority, request.model_dump(mode="json")
            )

    def confirm_replay(self, company_id: str, request: ReplayConfirm):
        request = ReplayConfirm.model_validate_json(canonical(request))
        if company_id != self.engine.store.company_id:
            raise KernelError("company_mismatch", "重放确认超出当前公司范围")
        with self.service._commit_authority(self.authority):
            return confirm_replay(
                self.service, self.engine, self.authority, request.model_dump(mode="json")
            )

    def replay_status(self, company_id: str, request: ReplayStatusRequest):
        request = ReplayStatusRequest.model_validate_json(canonical(request))
        if company_id != self.engine.store.company_id:
            raise KernelError("company_mismatch", "重放状态超出当前公司范围")
        return replay_status(
            self.service, self.engine, self.authority, request.run_id, request.step_id
        )

    def resume_replay(self, company_id: str, run_id: str):
        if company_id != self.engine.store.company_id:
            raise KernelError("company_mismatch", "重放恢复超出当前公司范围")
        with self.service._commit_authority(self.authority):
            return resume_replay(self.service, self.engine, self.authority, run_id)

    def replay_classification_prepare(
        self, company_id: str, items: tuple[ReplayClassificationItem, ...]
    ):
        if company_id != self.engine.store.company_id:
            raise KernelError("company_mismatch", "分类准备超出当前公司范围")
        from .report_classification_prepare import prepare_typed_report_classifications

        return {
            "status": "prepared",
            "items": prepare_typed_report_classifications(
                self.engine, [item.model_dump(mode="json") for item in items]
            ),
        }
