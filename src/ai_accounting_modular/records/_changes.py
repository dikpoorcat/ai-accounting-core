"""Plan current context changes, compare their prerequisites, then save once."""

from __future__ import annotations

from functools import lru_cache

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
    schema_document,
    validate_contract,
)

from ._sources import check_source
from ._types import PlannedRecord, RecordsChange, RecordsContext, RecordsPlan


@lru_cache(maxsize=1)
def _change_limits():
    fields = schema_document("core.schema.json", "ApplyRequest")["$defs"]["ApplyRequest"][
        "properties"
    ]
    return {field: fields[field]["maxItems"] for field in ("sources", "notes", "unresolved")}


def source_ids(ids, *, limit=500):
    if not isinstance(ids, (list, tuple)) or (limit is not None and len(ids) > limit):
        raise ContractError("invalid_input", f"一次最多引用{limit}份来源。")
    for source_id in ids:
        validate_contract("core.schema.json", "Id", source_id)
    return tuple(sorted(set(ids)))


def note_current(row, repository, evidence_ids=None):
    result = {
        "scope": row["scope_key"],
        "seq": row["seq"],
        "state": row["state"],
        "text": row["note_text"],
        "basis_head": row["basis_head"],
        "evidence_ids": list(repository.uses("note", row["scope_key"]))
        if evidence_ids is None
        else evidence_ids,
    }
    validate_contract("core.schema.json", "CurrentNote", result)
    if row["content_digest"] != note_digest(result):
        raise ContractError("storage_corrupt", "当前说明与保存的摘要不一致。")
    return result


def note_digest(current):
    return content_digest(
        {key: current[key] for key in ("scope", "state", "text", "basis_head", "evidence_ids")}
    )


def _next(value):
    if value >= MAX_FEN:
        raise ContractError("conflict", "当前序号已达上限。")
    return value + 1


def _missing_sources(required, available):
    missing = sorted(set(required) - set(available))
    if missing:
        raise ContractError(
            "needs_information",
            "当前资料引用尚未登记。",
            fact_issues=[
                {
                    "code": "source_not_found",
                    "field": "/evidence_ids",
                    "meaning": "支撑当前业务的资料来源",
                    "message": f"缺少来源：{source_id}",
                    "allowed_precision": ["identity"],
                    "reusable_sources": [],
                    "blocking_scope": "business",
                }
                for source_id in missing
            ],
        )


def plan_change(change: RecordsChange, context: RecordsContext, root) -> RecordsPlan:
    if not isinstance(change, RecordsChange) or not isinstance(context, RecordsContext):
        raise ContractError("invalid_input", "资料变化必须使用公开计划合同。")
    read = context.read_context
    validate_contract("core.schema.json", "Id", context.command_id)
    repository = read.repository("records")
    for field, label in (("sources", "来源"), ("notes", "说明")):
        if len(getattr(change, field)) > _change_limits()[field]:
            raise ContractError(
                "invalid_input", f"本单{label}超过合同允许的{_change_limits()[field]}项。"
            )
    sources = []
    seen = set()
    for value in change.sources:
        source = loads_json(canonical_json(value))
        check_source(root, source)
        if source["source_id"] in seen:
            raise ContractError("invalid_input", "同一来源身份不能在一单中重复出现。")
        seen.add(source["source_id"])
        sources.append(source)
    existing_sources = repository.sources(tuple(seen))
    source_plans = []
    for source in sources:
        old = existing_sources.get(source["source_id"])
        if old and any(
            old.get(key) != source.get(key) for key in ("kind", "sha256", "byte_size", "text")
        ):
            raise ContractError("conflict", "来源身份对应固定内容，改变内容必须使用新身份。")
        source_plans.append(
            PlannedRecord(
                source["source_id"],
                content_digest(old) if old else None,
                canonical_json(source),
                old != source,
            )
        )
    notes, required = [], set()
    note_scopes = set()
    for value in change.notes:
        validate_contract("core.schema.json", "NoteChange", value)
        note = loads_json(canonical_json(value))
        note["evidence_ids"] = list(source_ids(note["evidence_ids"]))
        scope = note["scope"]
        if scope in note_scopes:
            raise ContractError("invalid_input", "同一说明范围不能重复提交。")
        note_scopes.add(scope)
        basis = None
        if scope.startswith("period:"):
            basis = context.note_basis_heads.get(scope)
            if basis is None or basis != note["expected_basis_head"]:
                raise ContractError("conflict", "月份说明依据已经变化，请先读取当前业务结果。")
        elif "expected_basis_head" in note:
            raise ContractError("invalid_input", "公司现实说明不采用月份核算依据。")
        old = repository.notes((scope,)).get(scope)
        target = {
            "scope": scope,
            "seq": 1,
            "state": "current",
            "text": note["text"],
            "basis_head": basis,
            "evidence_ids": note["evidence_ids"],
        }
        digest = note_digest(target)
        expected = note["expected_seq"]
        if old is None:
            if expected != 0:
                raise ContractError("conflict", "该说明尚未建立，创建前提应为0。")
            changed = True
        else:
            note_current(old, repository)
            same = old["content_digest"] == digest
            if expected == old["seq"]:
                changed = not same
            elif same and old["seq"] == expected + 1 and old["latest_expected_seq"] == expected:
                changed = False
            else:
                raise ContractError("conflict", f"说明已变化，当前序号为{old['seq']}。")
            target["seq"] = _next(old["seq"]) if changed else old["seq"]
        required.update(note["evidence_ids"])
        notes.append(
            PlannedRecord(
                scope,
                content_digest(old) if old else None,
                canonical_json({"current": target, "expected_seq": expected}),
                changed,
            )
        )
    issue_plan = None
    has_scope = change.unresolved_scope is not None or change.expected_scope_head is not None
    if change.unresolved is None:
        if has_scope:
            raise ContractError("invalid_input", "待办范围前提必须与完整待办集合一起提交。")
    else:
        scope, expected = change.unresolved_scope, change.expected_scope_head
        if (
            not isinstance(scope, str)
            or not 1 <= len(scope) <= 200
            or type(expected) is not int
            or not 0 <= expected <= MAX_FEN
        ):
            raise ContractError("invalid_input", "替换待办须提供明确范围及当前范围序号。")
        if len(change.unresolved) > _change_limits()["unresolved"]:
            raise ContractError("invalid_input", "当前待办数量超过业务合同允许范围。")
        issues, issue_ids = [], set()
        for value in change.unresolved:
            validate_contract("core.schema.json", "CurrentIssue", value)
            issue = loads_json(canonical_json(value))
            issue["evidence_ids"] = list(source_ids(issue["evidence_ids"]))
            if issue["scope"] != scope or issue["issue_id"] in issue_ids:
                raise ContractError("invalid_input", "待办必须属于指定范围，且身份不重复。")
            if (
                "known_fact" in issue
                and "business_id" in issue
                and issue["known_fact"].get("business_id", issue["business_id"])
                != issue["business_id"]
            ):
                raise ContractError("invalid_input", "待办的已知业务与业务身份不一致。")
            issue_ids.add(issue["issue_id"])
            required.update(issue["evidence_ids"])
            issues.append(issue)
        issues.sort(key=lambda item: item["issue_id"])
        if any(owner != scope for owner in repository.issue_owners(tuple(issue_ids)).values()):
            raise ContractError("conflict", "待办身份已属于另一个范围，不能覆盖其他工作。")
        old = repository.issue_scope(scope)
        current = old["head"] if old else 0
        digest = content_digest(issues)
        same = digest == (old["content_digest"] if old else content_digest([]))
        if expected == current:
            changed = not same
        elif old and same and current == expected + 1 and old["latest_expected_head"] == expected:
            changed = False
        else:
            raise ContractError("conflict", f"该范围待办已变化，当前序号为{current}。")
        target = {
            "scope": scope,
            "head": _next(current) if changed else current,
            "issues": issues,
            "expected_head": expected,
        }
        issue_plan = PlannedRecord(
            scope, content_digest(old) if old else None, canonical_json(target), changed
        )
    available = repository.sources(tuple(required))
    available.update({source["source_id"]: source for source in sources})
    _missing_sources(required, available)
    return RecordsPlan(
        read.company_id,
        read.database_id,
        read.head,
        context.command_id,
        tuple(source_plans),
        tuple(notes),
        issue_plan,
    )


def apply_current(plan, uow, root):
    if not isinstance(plan, RecordsPlan) or (plan.company_id, plan.database_id, plan.head) != (
        uow.company_id,
        uow.database_id,
        uow.head,
    ):
        raise ContractError("conflict", "资料计划不属于当前公司的事务快照。")
    repository = uow.repository("records")
    stored = repository.sources(tuple(item.key for item in plan._sources))
    for item in plan._sources:
        source = loads_json(item.target_json)
        check_source(root, source)
        old = stored.get(item.key)
        if item.before_digest != (content_digest(old) if old else None):
            raise ContractError("conflict", "来源计划已过时，不能覆盖更新的位置。")
    for item in plan._notes:
        old = repository.notes((item.key,)).get(item.key)
        if item.before_digest != (content_digest(old) if old else None):
            raise ContractError("conflict", "说明计划已过时。")
    if plan._issue_scope is not None:
        old = repository.issue_scope(plan._issue_scope.key)
        if plan._issue_scope.before_digest != (content_digest(old) if old else None):
            raise ContractError("conflict", "待办计划已过时。")
    for item in plan._sources:
        if item.changed:
            repository.save_source(loads_json(item.target_json))
    for item in plan._notes:
        if item.changed:
            value = loads_json(item.target_json)
            current = value["current"]
            repository.save_note(
                current, value["expected_seq"], plan.command_id, note_digest(current)
            )
            repository.replace_uses("note", item.key, current["evidence_ids"])
    if plan._issue_scope is not None and plan._issue_scope.changed:
        value = loads_json(plan._issue_scope.target_json)
        repository.replace_issues(
            value["scope"],
            value["head"],
            value["expected_head"],
            plan.command_id,
            content_digest(value["issues"]),
            value["issues"],
        )
