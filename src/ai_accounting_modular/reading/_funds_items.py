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

EVENTS = {
    "acquire": "投资取得确认",
    "dispose": "投资退出确认",
    "income": "投资收益确认",
    "capital_return": "投资本金返还",
    "reinvest": "投资收益再投",
    "unit_change": "投资份额变动",
    "loss": "投资实际损失",
    "debt_interest": "债券付息与摊销",
    "holding_change": "投资持有安排变更",
    "market_estimate": "投资市场估值更新",
    "acquisition_payment": "投资实际付款",
    "disposal_receipt": "投资退出到账",
    "income_receipt": "投资收益到账",
    "capital_return_receipt": "投资本金到账",
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


def movements(activity, accounts, objects, sources, presence):
    result = []
    for view in activity.movements:
        actual = view["movement"]
        entry = sources.entries[view["source"]["result_id"]]
        account = accounts[actual["funds_account_id"]]
        internal = "transfer_group_id" in actual
        label, summary, full_summary = sources.titles(view)
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
            "display_summary": full_summary,
            "list_summary": summary,
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


def investment(activity, cooperation_objects):
    summary = {
        "opening_cost_fen": amount(activity.opening_cost),
        "closing_cost_fen": amount(activity.closing_cost),
        **{
            field: format_fen(getattr(activity, field))
            for field in (
                "subscription_cost_fen",
                "redemption_cost_fen",
                "investment_income_fen",
                "recognized_income_fen",
                "vat_adjustment_fen",
                "actual_loss_fen",
                "actual_payments_fen",
                "actual_receipts_fen",
            )
        },
        "event_count": activity.event_count,
        "cooperation_count": len(activity.cooperation_positions),
    }
    products, items = {}, []
    for product in activity.products:
        identity = product.terms_id or product.fund_id
        products[identity] = product
        value = {
            "fund_id": product.fund_id,
            "name": product.name,
            "opening_cost_fen": None
            if product.opening_cost is None
            else amount(product.opening_cost),
            "closing_cost_fen": None
            if product.closing_cost is None
            else amount(product.closing_cost),
            **{
                field: format_fen(getattr(product, field))
                for field in (
                    "subscription_cost_fen",
                    "redemption_cost_fen",
                )
            },
            "recognized_income_fen": format_fen(product.investment_income_fen),
            "actual_loss_fen": format_fen(product.actual_loss_fen),
            "accrued_income_fen": format_fen(product.accrued_income_fen),
            "pending_receipts_fen": format_fen(product.pending_receipts_fen),
            "activity_only": product.activity_only,
        }
        category = product.product_type
        value.update(
            investment_id=identity,
            product_type=category,
            category_label={
                "money_fund": "货币基金",
                "bank_wealth": "银行理财",
                "other_fund": "其他基金",
                "bond": "债券",
                "equity": "对外股权",
            }[category],
        )
        units = product.quantity
        arrangement = product.holding_arrangement
        if arrangement is not None and not product.activity_only:
            value["holding_label"] = {"current": "流动投资", "noncurrent": "非流动投资"}[
                arrangement
            ]
        if units is not None:
            if product.quantity_unit == "ownership_fraction":
                percentage = format(Decimal(units) * 100, "f")
                if "." in percentage:
                    percentage = percentage.rstrip("0").rstrip(".")
                value["quantity_label"] = "持有权益比例 " + (percentage or "0") + "%"
            elif product.quantity_unit == "nominal_fen":
                value["quantity_label"] = (
                    "持有票面本金 " + format(Decimal(units) / 100, ".2f") + "元"
                )
            else:
                unit = {"units": "份", "shares": "股"}[product.quantity_unit]
                value["quantity_label"] = "持有 " + units + " " + unit
        market = product.market_value_fen
        if market is not None:
            value.update(market_value_fen=format_fen(market), market_as_of=product.market_as_of)
        items.append(
            IndexItem(
                "investment_products",
                identity,
                key(*name_key(product.name), identity),
                value,
                object_id=product.fund_id,
            )
        )
    for event in activity.events:
        identity = getattr(event, "terms_id", None) or event.fund_id
        if identity not in products:
            raise ContractError("storage_corrupt", "投资活动缺少同一持仓的产品身份。")
        name = products[identity].name
        period = event.business_period if event.kind.endswith("confirmation") else event.period
        value = {
            "id": event.event_id,
            "subject_id": event.business_id,
            "date": event.actual_on,
            "period": period,
            "fund_id": event.fund_id,
            "investment_id": identity,
            "attribution": "event",
            "name": name,
            "type": ("更正原业务 · " if event.correction else "") + EVENTS[event.kind],
            **{
                field: None if value is None else format_fen(value)
                for field, value in (
                    ("cost_fen", event.cost_fen),
                    ("net_proceeds_fen", event.net_proceeds_fen),
                    ("investment_income_fen", event.gain_loss_fen),
                    ("settlement_fen", event.settlement_fen),
                    ("actual_loss_fen", getattr(event, "actual_loss_fen", 0)),
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
    for adjustment in activity.income_adjustments:
        if adjustment.attribution == "event":
            if adjustment.terms_id not in products:
                raise ContractError("storage_corrupt", "投资税调整缺少原持仓的产品身份。")
            name = products[adjustment.terms_id].name
        else:
            name = "公司统一税费调整"
        label = {
            "transfer_vat": "金融转让增值税调整",
            "interest_vat": "投资利息增值税调整",
            "investment_vat": "投资增值税调整",
        }[adjustment.component]
        value = {
            "id": adjustment.event_id,
            "subject_id": adjustment.business_id,
            "date": None,
            "period": adjustment.period,
            "fund_id": adjustment.product_id,
            "investment_id": adjustment.terms_id,
            "attribution": adjustment.attribution,
            "name": name,
            "type": ("更正原业务 · " if adjustment.correction else "") + label,
            "cost_fen": None,
            "net_proceeds_fen": None,
            "investment_income_fen": format_fen(adjustment.amount_fen),
            "settlement_fen": None,
            "actual_loss_fen": None,
        }
        items.append(
            IndexItem(
                "investment_events",
                adjustment.event_id,
                date_key(adjustment.period, None, name, adjustment.event_id),
                value,
                business_id=adjustment.business_id,
                object_id=adjustment.product_id,
                category=adjustment.component,
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
            "opening_cost_fen": None,
            "closing_cost_fen": None,
            "subscription_cost_fen": "0",
            "redemption_cost_fen": "0",
            "recognized_income_fen": "0",
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
