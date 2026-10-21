"""Cooperation supplements are adopted positions, with no invented payment history."""

from ai_accounting_modular.contracts.public import ContractError, format_fen

from ._brief_open_groups import group_key
from ._brief_open_sources import batches

_DIRECTIONS = (
    ("receivable", "operator_due_fen", "合作投资经营者应补"),
    ("payable", "company_due_fen", "合作投资公司应补"),
)


def project(selected, current, *, company_id, cutoff_period, identity, read):
    selected = [item for item in selected if item.operator_due_fen or item.company_due_fen]
    ids = sorted(
        {object_id for item in selected for object_id in (item.operator_id, item.project_id)}
    )
    objects = {
        obj["object_id"]: obj for batch in batches(ids) for obj in identity.get_batch(batch, read)
    }
    if set(objects) != set(ids):
        raise ContractError("storage_corrupt", "正式合作应补位置引用的对象身份缺失。")
    latest = {item.contract_id: item for item in current}
    rows = []
    for item in selected:
        for direction, field, title in _DIRECTIONS:
            due = getattr(item, field)
            if due == 0:
                continue
            actual = latest.get(item.contract_id)
            current_due = (
                getattr(actual, field)
                if actual is not None
                and actual.operator_id == item.operator_id
                and actual.project_id == item.project_id
                else None
            )
            category = "other_" + ("receivables" if direction == "receivable" else "payables")
            key = group_key(
                company_id,
                direction,
                category,
                {"type": "cooperation_position", "contract_id": item.contract_id},
                item.operator_id,
                None,
            )
            project_name = objects[item.project_id]["name"]
            rows.append(
                {
                    "id": "cooperation-position:" + item.contract_id + ":" + direction,
                    "group_key": key,
                    "date": None,
                    "recognition": {
                        "precision": "month",
                        "period": item.as_of_period,
                        "date": None,
                        "label": item.as_of_period + " 按月确认",
                    },
                    "source_period": item.as_of_period,
                    "voucher_version_id": None,
                    "voucher_number": None,
                    "category_key": category,
                    "party": objects[item.operator_id]["name"],
                    "description": title,
                    "purpose": project_name,
                    "status": "current",
                    "source_amount_fen": None,
                    "paid_fen": None,
                    "other_settled_fen": None,
                    "outstanding_fen": format_fen(due),
                    "current_status": "current" if current_due is not None else None,
                    "current_outstanding_fen": None
                    if current_due is None
                    else format_fen(current_due),
                    "subject_id": item.contract_id,
                    "contribution_group_key": None,
                    "contribution_component": None,
                    "payroll_period": None,
                    "_matter_title": title + " · " + project_name,
                    "_detail_scope": {
                        "reference": item.reference,
                        "source": item.source,
                        "business_period": item.reference["business_period"],
                        "posting_period": item.reference["posting_period"],
                        "kind": "investment.cooperation_position",
                        "direction": direction,
                        "position": {
                            "contract_id": item.contract_id,
                            "operator_id": item.operator_id,
                            "project_id": item.project_id,
                            "as_of_period": item.as_of_period,
                            "cutoff_period": cutoff_period,
                            "field": field,
                        },
                        "obligations": [],
                        "allocations": [],
                    },
                }
            )
    return rows
