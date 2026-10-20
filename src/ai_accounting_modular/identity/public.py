"""Stable object identities and current records, without accounting side effects."""

from __future__ import annotations

from collections.abc import Sequence

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ContractError,
    CurrentObject,
    Object,
    ObjectsRequest,
    ObjectsResult,
    ReadContext,
    UnitOfWork,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from ._paging import collection_version, decode_cursor, encode_cursor
from ._repository import IdentityRepository, sort_key
from ._types import IdentityContext, IdentityPlan, PlannedObject

__all__ = [
    "Identity",
    "IdentityContext",
    "IdentityPlan",
    "create_identity",
    "repository_factory",
]


def repository_factory() -> RepositoryFactory:
    return RepositoryFactory(module_schema("identity"), IdentityRepository)


def _ids(ids: Sequence[str]) -> list[str]:
    if isinstance(ids, str | bytes) or not isinstance(ids, Sequence):
        raise ContractError("invalid_input", "对象身份必须是有界列表。")
    if len(ids) > 500:
        raise ContractError("invalid_input", "对象身份一次最多500项。")
    for object_id in ids:
        validate_contract("core.schema.json", "Id", object_id)
    return list(dict.fromkeys(ids))


def _content(obj):
    return {
        "kind": obj["kind"],
        "name": obj["name"],
        "data": obj["data"],
        "evidence_ids": sorted(set(obj.get("evidence_ids", ()))),
    }


class Identity:
    @staticmethod
    def _materialize(rows) -> list[CurrentObject]:
        objects = []
        for row in rows:
            try:
                obj = loads_json(row["data_json"])
                validate_contract("core.schema.json", "CurrentObject", obj)
                if (
                    any(
                        obj[key] != row[key]
                        for key in ("object_id", "kind", "name", "seq", "content_digest")
                    )
                    or sort_key(obj["name"]) != row["sort_key"]
                    or content_digest(_content(obj)) != obj["content_digest"]
                ):
                    raise ValueError("current object body and projections mismatch")
            except (ContractError, ValueError) as exc:
                raise ContractError(
                    "storage_corrupt", "对象档案正文、查询字段或内容摘要不一致。"
                ) from exc
            objects.append(obj)
        return objects

    def plan_change(self, objects: Sequence[Object], context: IdentityContext) -> IdentityPlan:
        read = context.read_context
        validate_contract("core.schema.json", "Id", context.command_id)
        if (
            isinstance(objects, str | bytes)
            or not isinstance(objects, Sequence)
            or len(objects) > 500
        ):
            raise ContractError("invalid_input", "一次最多提交500个完整对象档案。")
        inputs, seen = [], set()
        for obj in objects:
            validate_contract("core.schema.json", "Object", obj)
            object_id = obj["object_id"]
            if object_id in seen:
                raise ContractError("invalid_input", "同一单中，一个对象身份只能提交一次。")
            seen.add(object_id)
            inputs.append(loads_json(canonical_json(obj)))
        repository = read.repository("identity")
        rows = repository.get(list(seen))
        self._materialize(list(rows.values()))
        items = []
        for obj in inputs:
            row = rows.get(obj["object_id"])
            digest, expected = content_digest(_content(obj)), obj["expected_seq"]
            if row is None:
                if expected != 0:
                    raise ContractError("conflict", "对象尚未建立，不能用修改序号创建。")
                seq, changed = 1, True
            else:
                if obj["kind"] != row["kind"]:
                    raise ContractError("conflict", "已有对象不能改成另一种身份类型。")
                same = digest == row["content_digest"]
                if expected == 0 and same:
                    seq, changed = row["seq"], False
                elif expected == row["seq"]:
                    seq, changed = row["seq"] + (not same), not same
                elif same and row["seq"] == expected + 1 and row["latest_expected_seq"] == expected:
                    seq, changed = row["seq"], False
                else:
                    raise ContractError("conflict", f"对象内容已变化，当前序号为{row['seq']}。")
                if seq > MAX_FEN:
                    raise ContractError("conflict", "对象序号已达上限，不能继续改写。")
            target = _content(obj) | {
                "object_id": obj["object_id"],
                "seq": seq,
                "content_digest": digest,
            }
            items.append(
                PlannedObject(
                    canonical_json(target),
                    expected,
                    row["seq"] if row else None,
                    row["content_digest"] if row else None,
                    changed,
                )
            )
        self._check_references(inputs, seen, repository)
        return IdentityPlan(
            read.company_id, read.database_id, read.head, context.command_id, tuple(items)
        )

    @staticmethod
    def _check_references(inputs, seen, repository):
        references = [
            (index, key, obj["data"][key])
            for index, obj in enumerate(inputs)
            for key in ("custodian_id", "operator_id", "issuer_id", "project_id")
            if key in obj["data"]
        ]
        stored = repository.get(list({ref for _, _, ref in references} - seen))
        missing = [
            {
                "code": "object_not_found",
                "field": f"/objects/{index}/data/{field}",
                "meaning": "档案中明确引用的对象身份",
                "message": "引用对象尚未在当前公司建立。",
                "allowed_precision": ["identity"],
                "reusable_sources": [],
                "blocking_scope": "business",
            }
            for index, field, ref in references
            if ref not in seen and ref not in stored
        ]
        if missing:
            raise ContractError(
                "needs_information", "对象档案含当前公司不存在的引用。", fact_issues=missing
            )

    def apply_current(self, plan: IdentityPlan, uow: UnitOfWork) -> None:
        if not isinstance(plan, IdentityPlan) or (plan.company_id, plan.database_id, plan.head) != (
            uow.company_id,
            uow.database_id,
            uow.head,
        ):
            raise ContractError("conflict", "对象计划不属于本公司的当前事务快照。")
        repository = uow.repository("identity")
        rows = repository.get([obj["object_id"] for obj in plan.objects])
        self._materialize(list(rows.values()))
        for item in plan._items:
            target = item.target
            validate_contract("core.schema.json", "CurrentObject", target)
            if content_digest(_content(target)) != target["content_digest"]:
                raise ContractError("invalid_input", "对象计划内容摘要不正确。")
            row = rows.get(target["object_id"])
            if (row["seq"] if row else None, row["content_digest"] if row else None) != (
                item.before_seq,
                item.before_digest,
            ):
                raise ContractError("conflict", "计划之后对象又有改动，不能保存过时档案。")
        for item in plan._items:
            if item.changed:
                repository.save(item.target, item.expected_seq, plan.command_id)

    def get_batch(self, ids: Sequence[str], read_context: ReadContext) -> tuple[CurrentObject, ...]:
        wanted = _ids(ids)
        rows = read_context.repository("identity").get(wanted)
        return tuple(self._materialize([rows[key] for key in wanted if key in rows]))

    def find(self, query: ObjectsRequest, read_context: ReadContext) -> ObjectsResult:
        validate_contract("core.schema.json", "ObjectsRequest", query)
        if query["action"] != "find" or query["company_id"] != read_context.company_id:
            raise ContractError("invalid_input", "对象检索的公司或操作不匹配。")
        after = decode_cursor(query["cursor"], query, read_context) if "cursor" in query else None
        total, filtered, rows = read_context.repository("identity").page(
            query["kind"], query.get("name_contains"), after, query["limit"]
        )
        has_more, rows = len(rows) > query["limit"], rows[: query["limit"]]
        version = collection_version(query, read_context)
        result: ObjectsResult = {
            "company_id": read_context.company_id,
            "head": read_context.head,
            "objects": self._materialize(rows),
            "missing_object_ids": [],
            "page": {
                "total_count": total,
                "filtered_count": filtered,
                "returned_count": len(rows),
                "has_more": has_more,
                "next_cursor": encode_cursor(rows[-1], version) if has_more else None,
                "collection_version": version,
            },
        }
        return validate_contract("core.schema.json", "ObjectsResult", result)


def create_identity() -> Identity:
    return Identity()
