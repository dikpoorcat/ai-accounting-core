"""Frozen funds facts become display rows, without consumer accounting formulas."""

from pypinyin import Style, lazy_pinyin

from ai_accounting_modular.contracts.public import (
    ContractError,
    format_fen,
    parse_fen,
)

from . import _funds_progress
from ._collections import IndexItem
from ._funds_accounts import amount

EVENTS = {
    "purchase_confirmation": "申购确认",
    "redemption_confirmation": "赎回确认",
    "purchase_payment": "申购实际付款",
    "redemption_receipt": "赎回实际到账",
}


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


def party_name(party_id, objects):
    return objects[party_id]["name"] if party_id is not None else ""


def movements(activity, accounts, objects, sources, presence):
    result = []
    for view in activity.movements:
        actual = view["movement"]
        entry = sources.entries[view["source"]["result_id"]]
        account = accounts[actual["funds_account_id"]]
        internal = "transfer_group_id" in actual
        label = sources.label(view).title
        parties = list(
            dict.fromkeys(
                party_name(part.party_id, objects) for part in view["allocations"] if part.party_id
            )
        )
        party = party_name(actual.get("party_id"), objects) or "、".join(parties)
        if not party:
            party = (
                "公司内部账户划转"
                if internal
                else "收款方未提供"
                if actual["direction"] == "outflow"
                else "付款方未提供"
            )
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
            "display_summary": actual.get("memo") or label,
            "list_summary": label,
            "party": party,
            "internal_transfer": internal,
        }
        value["has_business_progress"] = _funds_progress.available(
            value,
            view,
            sources.contents[view["source"]["result_id"]],
            presence,
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


def investment(activity):
    summary = {
        "opening_cost_fen": amount(activity.opening_cost),
        "closing_cost_fen": amount(activity.closing_cost),
        **{
            field: format_fen(getattr(activity, field))
            for field in (
                "subscription_cost_fen",
                "redemption_cost_fen",
                "investment_income_fen",
                "actual_payments_fen",
                "actual_receipts_fen",
            )
        },
        "event_count": activity.event_count,
    }
    products, items = {}, []
    for product in activity.products:
        products[product.fund_id] = product
        value = {
            "fund_id": product.fund_id,
            "name": product.name,
            "opening_cost_fen": amount(product.opening_cost),
            "closing_cost_fen": amount(product.closing_cost),
            **{
                field: format_fen(getattr(product, field))
                for field in (
                    "subscription_cost_fen",
                    "redemption_cost_fen",
                    "investment_income_fen",
                )
            },
        }
        items.append(
            IndexItem(
                "investment_products",
                product.fund_id,
                key(*name_key(product.name), product.fund_id),
                value,
                object_id=product.fund_id,
            )
        )
    for event in activity.events:
        name = products[event.fund_id].name
        period = event.business_period if event.kind.endswith("confirmation") else event.period
        value = {
            "id": event.event_id,
            "subject_id": event.business_id,
            "date": event.actual_on,
            "period": period,
            "fund_id": event.fund_id,
            "name": name,
            "type": ("更正原业务 · " if event.correction else "") + EVENTS[event.kind],
            **{
                field: None if value is None else format_fen(value)
                for field, value in (
                    ("cost_fen", event.cost_fen),
                    ("net_proceeds_fen", event.net_proceeds_fen),
                    ("investment_income_fen", event.gain_loss_fen),
                    ("settlement_fen", event.settlement_fen),
                )
            },
        }
        items.append(
            IndexItem(
                "investment_events",
                event.event_id,
                date_key(period, event.actual_on, name, event.event_id),
                value,
                business_id=event.business_id,
                object_id=event.fund_id,
                category=event.kind,
            )
        )
    return summary, items
