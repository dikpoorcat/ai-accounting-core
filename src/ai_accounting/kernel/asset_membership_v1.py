"""Released v1 read-only proof of frozen asset batch membership."""

from __future__ import annotations

import json

from .contracts import KernelError
from .history_encoding_v1 import digest
from .stored_json_v1 import load_outcome

_OWNER_KINDS = frozenset({"asset_activation_batch", "asset_consumption_month"})


def frozen_members(connection, owner_calculation_id: str) -> list[dict]:
    owner = connection.execute(
        "SELECT c.*, EXISTS(SELECT 1 FROM calculation_seal s "
        "WHERE s.calculation_id=c.id) AS _sealed FROM calculation c WHERE c.id=?",
        (owner_calculation_id,),
    ).fetchone()
    rows = list(
        connection.execute(
            "SELECT m.*, "
            "EXISTS(SELECT 1 FROM asset_batch_member other "
            "JOIN calculation other_owner ON other_owner.id=other.owner_calculation_id "
            "JOIN calculation batch_owner ON batch_owner.id=m.owner_calculation_id "
            "WHERE other.member_calculation_id=m.member_calculation_id "
            "AND other_owner.subject_id<>batch_owner.subject_id) AS _foreign_owned, "
            "EXISTS(SELECT 1 FROM dependency_calculation d "
            "WHERE d.calculation_id=m.owner_calculation_id "
            "AND d.upstream_id=m.member_calculation_id) AS _has_dependency "
            "FROM asset_batch_member m WHERE m.owner_calculation_id=? ORDER BY m.position",
            (owner_calculation_id,),
        )
    )
    selected = tuple(dict.fromkeys(row["member_calculation_id"] for row in rows))
    members_by_id = (
        {
            row["id"]: row
            for row in connection.execute(
                "SELECT c.*, EXISTS(SELECT 1 FROM calculation_seal s "
                "WHERE s.calculation_id=c.id) AS _sealed FROM calculation c "
                "WHERE c.id IN (SELECT value FROM json_each(?))",
                (json.dumps(selected),),
            )
        }
        if selected
        else {}
    )
    if owner is None or owner["kind"] not in _OWNER_KINDS:
        raise KernelError("asset_batch_identity", "资产汇总计算不存在")
    if not owner["_sealed"]:
        raise KernelError("asset_batch_unsealed", "资产汇总计算尚未封印")
    outcome = load_outcome(owner["outcome"])
    if digest(outcome) != owner["digest"]:
        raise KernelError("asset_batch_digest", "资产汇总结果摘要不匹配")
    members, position, balances = [], 1, []
    for row in rows:
        member = dict(row)
        member.pop("owner_calculation_id")
        member.pop("_foreign_owned")
        member.pop("_has_dependency")
        member["result_digest"] = member["result_digest"].hex()
        member["summary"] = json.loads(member["summary"])
        calculated = members_by_id.get(member["member_calculation_id"])
        expected_kind = (
            "asset_activation" if owner["kind"] == "asset_activation_batch" else "asset_consumption"
        )
        if calculated is None or (
            calculated["kind"], calculated["subject_id"],
            calculated["fact_id"], calculated["period"],
        ) != (
            expected_kind, member["member_subject_id"],
            member["member_fact_id"], owner["period"],
        ):
            raise KernelError("asset_batch_identity", "资产汇总成员身份不匹配")
        if not calculated["_sealed"]:
            raise KernelError("asset_batch_unsealed", "资产汇总成员尚未封印")
        if row["_foreign_owned"]:
            raise KernelError("asset_member_owned", "同一成员计算不得被不同批次采用")
        result = load_outcome(calculated["outcome"])
        count = len(result["lines"])
        if (
            member["position"] != len(members) + 1
            or digest(result).hex() != member["result_digest"]
            or calculated["digest"].hex() != member["result_digest"]
            or result["values"] != member["summary"]
            or result["values"].get("asset_id") != member["asset_id"]
            or member["line_count"] != count
            or member["line_start"] != (position if count else None)
            or outcome["lines"][position - 1 : position - 1 + count] != result["lines"]
        ):
            raise KernelError("asset_batch_digest", "资产汇总成员或分录范围不匹配")
        if not row["_has_dependency"]:
            raise KernelError("asset_batch_identity", "资产汇总缺少精确成员依赖")
        member["kind"] = expected_kind
        members.append(member)
        balances.extend(result["balances"])
        position += count
    values = outcome["values"]
    if (
        type(values.get("member_count")) is not int
        or len(members) != values.get("member_count")
        or balances != outcome["balances"]
        or position - 1 != len(outcome["lines"])
        or members != values.get("members")
        or digest(members).hex() != values.get("membership_digest")
    ):
        raise KernelError("asset_batch_digest", "资产汇总完整成员清单不匹配")
    return members
