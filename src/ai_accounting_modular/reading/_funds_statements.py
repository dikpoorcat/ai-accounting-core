"""Original bank text and whole, formally matched payment groups."""

from ai_accounting_modular.contracts.public import format_fen, parse_fen

from ._collections import IndexItem
from ._funds_items import date_key, party_name


def batches(activity, sources, objects):
    movements = {item["movement"]["movement_id"]: item for item in activity.movements}
    complete_accounts = {
        item.account_id for item in activity.accounts if item.reconciliation == "complete"
    }
    rows_by_group = {}
    for row in activity.original_rows:
        rows_by_group.setdefault(row.match_group_id, []).append(row)
    groups = [
        group
        for group in activity.reconciliation_groups
        if len(rows_by_group.get(group.group_id, ())) == len(group.statement_row_ids)
        and all(
            row.kind == "bank" and row.account_id in complete_accounts
            for row in rows_by_group[group.group_id]
        )
    ]
    result = {}
    for group in groups:
        items, total, complete = [], 0, True
        for identity in group.movement_ids:
            view = movements.get(identity)
            if view is None:
                complete = False
                break
            actual = view["movement"]
            value = parse_fen(actual["amount_fen"])
            total += value
            parts = view["allocations"]
            if not parts:
                items.append(
                    {
                        "party": party_name(actual.get("party_id"), objects) or "交易方未提供",
                        "amount_fen": actual["amount_fen"],
                    }
                )
                continue
            items.extend(
                {
                    "party": party_name(part.party_id, objects) or "交易方未提供",
                    "amount_fen": format_fen(part.amount_fen),
                }
                for part in parts
            )
            remainder = value - sum(part.amount_fen for part in parts)
            if remainder:
                data = sources.contents[view["source"]["result_id"]].basis["input"]
                if (
                    data.get("type") != "payroll_reserve"
                    or parse_fen(data["reserve_expense_fen"]) != remainder
                ):
                    complete = False
                    break
                items.append({"party": "工资代发备用金费用", "amount_fen": format_fen(remainder)})
        if complete and len(items) > 1:
            result[group.group_id] = {
                "bank_row_count": len(group.statement_row_ids),
                "total_fen": format_fen(total),
                "items": items,
            }
    return result


def statements(activity, accounts, matched):
    result = []
    for row in activity.original_rows:
        if row.kind != "bank":
            continue
        account = accounts[row.account_id]
        value = {
            "id": row.row_id,
            "date": row.actual_on,
            "account_id": row.account_id,
            "account_code": account["code"],
            "account_name": account["name"],
            "direction": row.direction,
            "amount_fen": format_fen(row.amount_fen),
            "signed_amount_fen": format_fen(
                row.amount_fen * (1 if row.direction == "inflow" else -1)
            ),
            "party": row.counterparty_text or "",
            "memo": row.memo or "",
        }
        if row.match_group_id in matched:
            value["batch_payment"] = matched[row.match_group_id]
        result.append(
            IndexItem(
                "statements",
                row.row_id,
                date_key(row.actual_on[:7], row.actual_on, value["party"], row.row_id),
                value,
                business_id=row.business_id,
                object_id=row.account_id,
                category="bank",
            )
        )
    return result
