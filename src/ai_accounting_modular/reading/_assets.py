"""Publish complete asset-page collections and read only their requested pages."""

from ai_accounting_modular.contracts.public import ContractError, PageQuery

from . import _monthly
from ._assets_cards import card_value, project_value
from ._assets_summary import public_summary, summary
from ._collections import CollectionFilter, IndexItem, prepare_items, prepare_model, wire_page


def prepare(reader, scope, read):
    activity = reader.assets.asset_activity(scope, read)
    saved = summary(activity)
    entries = [
        IndexItem(
            "assets",
            card.asset_id,
            f"{index:020d}",
            card_value(card),
            business_id=card.origin["business_id"] if card.origin is not None else None,
            object_id=card.asset_id,
            category=f"{card.asset_type or 'unknown'}:{card.state}",
        )
        for index, card in enumerate(activity.cards)
    ]
    entries.extend(
        IndexItem(
            "projects",
            item.reference["business_id"] + ":" + item.reference["slot"],
            f"{index:020d}",
            project_value(item),
            business_id=item.reference["business_id"],
            object_id=item.project["object_id"],
        )
        for index, item in enumerate(activity.project_costs)
        if item.remaining_fen != 0
    )
    return prepare_model(saved, items=prepare_items(entries, collections=("assets", "projects")))


def _categories(filter):
    if filter == "all":
        return ()
    if filter in {"fixed", "intangible"}:
        return tuple(
            f"{filter}:{state}" for state in ("unestablished", "pending", "active", "exited")
        )
    return tuple(f"{kind}:{filter}" for kind in ("fixed", "intangible"))


def _page(snapshot, name, filters, query, read):
    page = _monthly.collection(
        snapshot,
        "assets",
        name,
        filters,
        PageQuery(query.get("limit", 20), query.get("cursor")),
        read,
    )
    return wire_page(page)


def build(snapshot, read, query, *, identity):
    saved = snapshot.components["assets"]
    asset_id, project_id = query.get("asset_id"), query.get("project_id")
    requested_ids = tuple(
        dict.fromkeys(value for value in (asset_id, project_id) if value is not None)
    )
    objects = (
        {obj["object_id"]: obj for obj in identity.get_batch(requested_ids, read)}
        if requested_ids
        else {}
    )
    for value, kind, label in ((asset_id, "asset", "资产"), (project_id, "project", "项目")):
        if value is not None and (value not in objects or objects[value]["kind"] != kind):
            raise ContractError("not_found", "本公司没有所定位的" + label + "身份。")
    data = public_summary(saved)
    asset_filter = query.get("asset_filter", "all")
    collections = {}
    requested = (query["section"],) if "section" in query else ("assets", "projects")
    for name in requested:
        filters = (
            CollectionFilter(object_id=asset_id, categories=_categories(asset_filter))
            if name == "assets"
            else CollectionFilter(object_id=project_id)
        )
        collections[name] = _page(snapshot, name, filters, query, read)
    data.update(
        asset_id=asset_id, project_id=project_id, asset_filter=asset_filter, collections=collections
    )
    return {
        "selected_period": snapshot.selected_period,
        "snapshot_version": snapshot.version,
        "data": data,
    }
