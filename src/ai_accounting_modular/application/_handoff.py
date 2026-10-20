"""The one supported cross-owner correction keeps a single public business root."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import ContractError

_PAIR = frozenset(("operations.expense", "assets.acquisition"))


def supports(old_kind, new_kind):
    return old_kind != new_kind and frozenset((old_kind, new_kind)) == _PAIR


@dataclass(frozen=True)
class Handoff:
    business_id: str
    expected_seq: int
    old_owner: object
    new_owner: object

    def outgoing(self):
        return {
            "business_id": self.business_id,
            "op": "handoff_out",
            "expected_seq": self.expected_seq,
        }


def prepare(changes, previous_facts, registry):
    """Validate the real old/new typed facts before any participant saves content."""
    result = {}
    for change in changes:
        if not (change.changed and change.before and change.fact):
            continue
        old_kind, new_kind = change.before["kind"], change.fact["kind"]
        if old_kind == new_kind:
            continue
        if change.mutation["op"] != "replace" or not supports(old_kind, new_kind):
            raise ContractError(
                "unsupported_change_kind", "仅支持供应商费用与直接取得资产之间更正分类。"
            )
        old = previous_facts[change.business_id]
        expense, acquisition = (
            (old, change.fact) if old_kind == "operations.expense" else (change.fact, old)
        )
        if (
            expense["data"]["creditor_kind"] != "supplier"
            or acquisition["data"]["type"] != "direct"
        ):
            raise ContractError(
                "unsupported_change_kind", "同一采购分类交接须为供应商费用和直接取得资产。"
            )
        if expense["data"]["creditor_id"] != acquisition["data"]["supplier_id"]:
            _incompatible(
                change.fact,
                "/data/creditor_id" if new_kind == "operations.expense" else "/data/supplier_id",
                "同一采购更正分类须保持原交易方，不能借交接改成另一笔业务。",
            )
        if expense["data"]["amount_fen"] != acquisition["data"]["asset"]["cost_fen"]:
            _incompatible(
                change.fact,
                "/data/amount_fen" if new_kind == "operations.expense" else "/data/asset/cost_fen",
                "同一采购更正分类须保持原总金额，已有实际收付款继续按原业务承接。",
            )
        result[change.business_id] = Handoff(
            change.business_id,
            change.mutation["expected_seq"],
            registry.owner(old_kind),
            registry.owner(new_kind),
        )
    return result


def _incompatible(fact, field, message):
    raise ContractError(
        "dependency_conflict",
        message,
        fact_issues=[
            {
                "code": "incompatible_handoff",
                "business_id": fact["business_id"],
                "field": field,
                "meaning": "同一笔真实采购的交易方及总金额",
                "message": message,
                "allowed_precision": ["identity", "fen"],
                "reusable_sources": [{"source_id": sid} for sid in fact["evidence_ids"][:500]],
                "blocking_scope": "business",
            }
        ],
    )


def active_responsibilities(descriptions, handoffs):
    """An outgoing owner's cleanup cannot overwrite the unique adopting owner."""
    active, outgoing, owners = {}, set(), {}
    for owner, description in descriptions:
        for item in description["businesses"]:
            business_id = item["business_id"]
            if item["op"] == "handoff_out":
                expected = handoffs.get(business_id)
                if (
                    expected is None
                    or owner is not expected.old_owner
                    or business_id in outgoing
                    or item["expected_seq"] != expected.expected_seq
                    or "fact" in item
                    or item["complete_result_slots"]
                    or any(
                        edge["consumer_business_id"] == business_id
                        for edge in description["dependencies"]
                    )
                    or any(
                        value["business_id"] == business_id for value in description["publications"]
                    )
                ):
                    raise ContractError(
                        "invalid_input",
                        "退出责任须与已核对交接逐一匹配，不能携带事实、结果槽、依赖或发布。",
                    )
                outgoing.add(business_id)
            else:
                if business_id in active:
                    raise ContractError("invalid_input", "同一业务只能有一个有效采用责任。")
                active[business_id], owners[business_id] = item, owner
    for business_id, expected in handoffs.items():
        item = active.get(business_id)
        if (
            business_id not in outgoing
            or item is None
            or owners[business_id] is not expected.new_owner
            or item["op"] != "replace"
            or item["expected_seq"] != expected.expected_seq
            or item["complete_result_slots"] != ["main"]
            or "fact" not in item
        ):
            raise ContractError(
                "invalid_input", "分类交接必须由旧模块完整退出、新模块唯一承接同一main结果槽。"
            )
    return active


def verify_released(handoffs, read_context):
    grouped = {}
    for handoff in handoffs.values():
        grouped.setdefault(handoff.old_owner, []).append(handoff.business_id)
    for owner, ids in grouped.items():
        for offset in range(0, len(ids), 500):
            if owner.export_current(ids[offset : offset + 500], read_context):
                raise ContractError("storage_corrupt", "交接完成后旧模块仍保留了已退出的当前正文。")
