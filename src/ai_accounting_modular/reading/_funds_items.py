"""Frozen funds facts become display rows, without consumer accounting formulas."""

from decimal import Decimal

from pypinyin import Style, lazy_pinyin

from ai_accounting_modular.contracts.public import (
    ContractError,
    format_fen,
    parse_fen,
)

from . import _funds_progress
from ._collections import IndexItem
from ._counterparty_display import label as counterparty_label
from ._funds_accounts import amount


def key(*values: str | bool) -> str:
    """UTF-8 hex preserves text order; '/' sorts before every hex digit and ends a part."""
    return (
        "/".join(
            (str(int(value)) if isinstance(value, bool) else value)
            .encode("utf-8", "surrogatepass")
            .hex()
            for value in values
        )
        + "/"
    )


def name_key(name):
    return "".join(lazy_pinyin(name, style=Style.NORMAL)).casefold(), name


def date_key(month, day, name, identity):
    return key(month, day is None, day or "", *name_key(name), identity)


def identities(reader, activity, read):
    ids = {item.account_id for item in activity.accounts}
    for view in activity.movements:
        movement = view["movement"]
        if movement.get("party_id"):
            ids.add(movement["party_id"])
        ids.update(part.party_id for part in view["allocations"] if part.party_id is not None)
    ids, found = sorted(ids), {}
    for start in range(0, len(ids), 500):
        found.update(
            (obj["object_id"], obj)
            for obj in reader.identity.get_batch(ids[start : start + 500], read)
        )
    if found.keys() != set(ids):
        raise ContractError("storage_corrupt", "正式资金引用的账户或交易方身份不完整。")
    return found


def movements(activity, accounts, objects, sources, presence):
    result = []
    for view in activity.movements:
        actual = view["movement"]
        entry = sources.entries[view["source"]["result_id"]]
        account = accounts[actual["funds_account_id"]]
        internal = "transfer_group_id" in actual
        label, summary = sources.titles(view)
        party = counterparty_label(view["counterparty"], objects, direction=actual["direction"])
        value = {
            "id": actual["movement_id"],
            "subject_id": ""
            if entry.business_kind.startswith("investment.cooperation_")
            else entry.reference["business_id"],
            "date": actual["actual_on"],
            "account_id": account["account_id"],
            "account_code": account["code"],
            "account_name": account["name"],
            "account_type": account["type"],
            "direction": actual["direction"],
            "correction": False,
            "amount_fen": actual["amount_fen"],
            "signed_amount_fen": format_fen(
                parse_fen(actual["amount_fen"]) * (1 if actual["direction"] == "inflow" else -1)
            ),
            "type": label,
            "list_summary": summary,
            "party": party,
            "internal_transfer": internal,
        }
        value["has_business_progress"] = _funds_progress.available(
            value,
            view,
            sources.contents[view["source"]["result_id"]],
            presence,
            has_note=sources.has_note(view),
        )
        result.append(
            IndexItem(
                "movements",
                value["id"],
                date_key(entry.reference["business_period"], value["date"], party, value["id"]),
                value,
                business_id=entry.reference["business_id"],
                object_id=account["account_id"],
                category=account["type"],
            )
        )
    return result


def investment(activity, cooperation_objects):
    summary = {"closing_cost_fen": amount(activity.closing_cost)}
    items = []
    labels = {
        "money_fund": "货币基金",
        "bank_wealth": "银行理财",
        "other_fund": "其他基金",
        "bond": "债券",
        "equity": "对外股权",
    }
    for product in activity.products:
        identity = product.terms_id or product.fund_id
        value = {
            "fund_id": product.fund_id,
            "investment_id": identity,
            "name": product.name,
            "product_type": product.product_type,
            "category_label": labels[product.product_type],
            "closing_cost_fen": None
            if product.closing_cost is None
            else amount(product.closing_cost),
            "pending_receipts_fen": format_fen(product.pending_receipts_fen),
            "recognized_income_fen": format_fen(product.investment_income_fen),
            "accrued_income_fen": format_fen(product.accrued_income_fen),
            "actual_loss_fen": format_fen(product.actual_loss_fen),
            "activity_only": product.activity_only,
            "actual_payments_fen": format_fen(product.actual_payments_fen),
            "actual_receipts_fen": format_fen(product.actual_receipts_fen),
        }
        if product.holding_arrangement is not None and not product.activity_only:
            value["holding_label"] = {"current": "流动投资", "noncurrent": "非流动投资"}[
                product.holding_arrangement
            ]
        if product.quantity is not None:
            if product.quantity_unit == "ownership_fraction":
                percentage = format(Decimal(product.quantity) * 100, "f")
                if "." in percentage:
                    percentage = percentage.rstrip("0").rstrip(".")
                value["quantity_label"] = "持有权益比例 " + (percentage or "0") + "%"
            elif product.quantity_unit == "nominal_fen":
                value["quantity_label"] = (
                    "持有票面本金 " + format(Decimal(product.quantity) / 100, ".2f") + "元"
                )
            else:
                unit = {"units": "份", "shares": "股"}[product.quantity_unit]
                value["quantity_label"] = "持有 " + product.quantity + " " + unit
        if product.market_value_fen is not None:
            value.update(
                market_value_fen=format_fen(product.market_value_fen),
                market_as_of=product.market_as_of,
            )
        items.append(
            IndexItem(
                "investment_products",
                identity,
                key(*name_key(product.name), identity),
                value,
                object_id=product.fund_id,
                category=product.product_type,
            )
        )
    for position in activity.cooperation_positions:
        name = cooperation_objects[position.project_id]["name"]
        identity = position.contract_id
        value = {
            "fund_id": position.project_id,
            "investment_id": identity,
            "name": name,
            "product_type": "cooperation",
            "category_label": "合作项目",
            "closing_cost_fen": None,
            "actual_loss_fen": "0",
            "cooperation_held_fen": format_fen(position.held_fen),
            "cooperation_equity_fen": format_fen(position.company_capital_fen),
            "cooperation_operator_due_fen": format_fen(position.operator_due_fen),
            "cooperation_company_due_fen": format_fen(position.company_due_fen),
        }
        items.append(
            IndexItem(
                "investment_products",
                identity,
                key(*name_key(name), identity),
                value,
                object_id=position.project_id,
                category="cooperation",
            )
        )
    return summary, items
