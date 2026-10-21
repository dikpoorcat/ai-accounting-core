"""One complete funds month, with hot pages served only from its published item index."""

from ai_accounting_modular.contracts.public import ContractError, PageQuery

from . import _funds_accounts, _funds_items, _funds_sources, _funds_statements, _monthly
from ._collections import CollectionFilter, IndexItem, prepare_items, prepare_model, wire_page

COMPONENT = "funds"
COLLECTIONS = ("accounts", "movements", "statements", "investment_products", "investment_events")


def prepare(reader, scope, read):
    activity = reader.money.account_activity(scope, read)
    funds = reader.investment.money_fund_activity(scope, read)
    balances = _funds_accounts.positions(reader.ledger, activity.accounts, scope["period_to"], read)
    objects = _funds_items.identities(reader, activity, read)
    cards = _funds_accounts.cards(activity, balances, objects)
    sources = _funds_sources.load(reader, activity, scope, read)
    items = [
        IndexItem(
            "accounts",
            card["account_id"],
            _funds_items.key(
                first or "9999-99", *_funds_items.name_key(card["name"]), card["account_id"]
            ),
            card,
            object_id=card["account_id"],
            category=card["type"],
        )
        for first, card in cards
    ]
    ordered = sorted(items, key=lambda item: (item.sort_key, item.item_id))
    accounts = {card["account_id"]: card for _, card in cards}
    items.extend(_funds_items.movements(activity, accounts, objects, sources))
    matched = _funds_statements.batches(activity, sources, objects)
    items.extend(_funds_statements.statements(activity, accounts, matched))
    investments, fund_items = _funds_items.investment(funds)
    items.extend(fund_items)
    summary = _funds_accounts.summary(activity, balances, cards)
    summary["investments"] = investments
    first = ordered[0].value if ordered else None
    return prepare_model(
        {
            "data": summary,
            "first_account": None
            if first is None
            else {"type": first["type"], "account_id": first["account_id"]},
        },
        items=prepare_items(items, collections=COLLECTIONS),
    )


def _account(snapshot, read, account_id, kind):
    found = _monthly.collection(
        snapshot,
        COMPONENT,
        "accounts",
        CollectionFilter(object_id=account_id, categories=(kind,)),
        PageQuery(1),
        read,
    )
    if not found.items:
        raise ContractError("not_found", "所选资金账户不属于本公司或账户类型不符。")


def build(snapshot, read, query):
    """Use the already parsed monthly query; only published identity lookup remains here."""
    summary = snapshot.components[COMPONENT]
    section, cursor = query.get("section"), query.get("cursor")
    mode = query.get("movement_account_selection", "all")
    account_id, kind = query.get("movement_account_id"), query.get("movement_account_type")
    selected = summary["first_account"] if mode == "first" else None
    if account_id is not None:
        _account(snapshot, read, account_id, kind)
        selected = {"type": kind, "account_id": account_id}
    statement_id = query.get("statement_account_id")
    if statement_id is not None:
        _account(snapshot, read, statement_id, "bank")
    result = {}
    for name in COLLECTIONS if section is None else (section,):
        selection = CollectionFilter()
        if name == "movements" and selected is not None:
            selection = CollectionFilter(
                object_id=selected["account_id"], categories=(selected["type"],)
            )
        elif name == "statements" and statement_id is not None:
            selection = CollectionFilter(object_id=statement_id)
        result[name] = wire_page(
            _monthly.collection(
                snapshot,
                COMPONENT,
                name,
                selection,
                PageQuery(query.get("limit", 20), cursor),
                read,
            )
        )
    return {
        "selected_period": snapshot.selected_period,
        "snapshot_version": snapshot.version,
        "data": dict(summary["data"])
        | {"selected_movement_account": selected, "collections": result},
    }
