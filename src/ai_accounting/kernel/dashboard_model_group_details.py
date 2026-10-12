"""Native brief members with authenticated, bounded group and voucher reads.

Production shares one authoritative snapshot's complete grouping and successful
source proofs. Consumption authenticates a target block and its actual raw hits;
it never reconstructs the month's activities or historical obligation groups.
"""

from __future__ import annotations

from .contracts import KernelError
from .dashboard_pages import decode_cursor, seal_page


def _invalid():
    return KernelError(
        "content_integrity_failed",
        "业务组成员或精确凭证读取内容不一致",
        component="dashboard_read_model",
    )


def _voucher_bindings(snap, rows):
    """Bind saved owners and independently adopted frozen bases, even across months."""
    from .dashboard_model_materializer import _bindings, _presentation_sources

    rows = list(rows)
    identifiers = {
        row[field] for row in rows for field in ("basis_calculation_id", "voucher_calculation_id")
    }
    frozen = snap.month_journal.authenticated_frozen_adoptions(rows)
    identifiers.update(
        item[field]
        for item in frozen.values()
        for field in ("calculation_id", "adopted_calculation_id")
    )
    # Labels can expose an exact obligation source other than the voucher owner.
    # This is a same-snapshot native presentation read, never a current-head guess.
    identifiers = _presentation_sources(snap, identifiers)
    return _bindings(snap, identifiers)


def _native_group_page(dashboard, snap, section, group_key, *, after, limit):
    from .dashboard import _brief_activity_row
    from .dashboard_brief_groups import (
        activity_group_members,
        exact_open_voucher_rows,
        vouchers_for_rows,
    )
    from .dashboard_model_materializer import _bindings
    from .dashboard_open_groups import _data, _ordered_group_members, open_group_members

    if section == "activity":
        rows, page = activity_group_members(snap, group_key, after=after, limit=limit)
        member_sources = None
    else:
        native = open_group_members(snap, group_key, after=after, limit=limit)
        items, page = native["items"], native["page"]
        rows = exact_open_voucher_rows(snap, items)
        data = _data(snap)
        member_sources = _ordered_group_members(snap, data, group_key)[1]
    # A voucher shared by activities and old open obligations is rendered once
    # during this production snapshot. This cache is released with the snapshot.
    cards = snap.semantic_cache.setdefault("model_group_voucher_cards", {})
    unique_rows = {row["id"]: row for row in rows}
    missing = [row for ident, row in unique_rows.items() if ident not in cards]
    if missing:
        cards.update(
            (item["voucher_version_id"], item) for item in vouchers_for_rows(snap, missing)
        )
    vouchers = [cards[ident] for ident in unique_rows]
    if section == "activity":
        items = [_brief_activity_row(snap, row) for row in rows]
    voucher_sources = {ident: _voucher_bindings(snap, [row]) for ident, row in unique_rows.items()}
    item_sources, item_versions = [], []
    for item in items:
        version = item["voucher_version_id"]
        sources = set(voucher_sources.get(version, ()))
        if member_sources is not None:
            sources.update(
                _bindings(
                    snap,
                    {
                        member_sources[item["id"]]["source_calculation_id"],
                    },
                )
            )
        item_sources.append(tuple(sorted(sources)))
        item_versions.append((version,) if version else ())
    data = {
        "section": section,
        "group_key": group_key,
        "collections": {
            "members": {
                "items": items,
                "page": seal_page(
                    snap,
                    "brief-group",
                    section,
                    page,
                    {"group_key": group_key},
                ),
            },
            "vouchers": {
                "items": vouchers,
                "page": {
                    "total_count": len(vouchers),
                    "filtered_count": len(vouchers),
                    "returned_count": len(vouchers),
                    "has_more": False,
                    "next_cursor": None,
                },
            },
        },
    }
    return (
        {**dashboard._response(snap, data), "schema_version": 3},
        page,
        tuple(item_sources),
        tuple(item_versions),
    )


def materialize_group_blocks(dashboard, snap, *, block_size=20):
    """Produce all native groups once; each block preserves stable member order."""
    from .dashboard_brief_groups import activity_groups
    from .dashboard_model_materializer import MaterializedBlock
    from .dashboard_open_groups import _data
    from .response_contracts import validate_response

    if type(block_size) is not int or not 1 <= block_size <= 500:
        raise ValueError("看板读取块大小必须为 1 至 500")
    groups = {
        "activity": activity_groups(snap)[0],
        "open_items": _data(snap)["ordered"],
    }
    blocks = []
    for section, group_keys in groups.items():
        for group_key in group_keys:
            after, ordinal, seen = None, 0, set()
            count = None
            while True:
                payload, page, item_sources, item_versions = _native_group_page(
                    dashboard,
                    snap,
                    section,
                    group_key,
                    after=after,
                    limit=block_size,
                )
                validate_response("dashboard_brief_group", payload)
                members = payload["data"]["collections"]["members"]["items"]
                field = "key" if section == "activity" else "id"
                keys = tuple((item[field], item["voucher_version_id"] or "") for item in members)
                if (
                    count is not None
                    and count != page["filtered_count"]
                    or any(key[0] in seen for key in keys)
                ):
                    raise _invalid()
                count = page["filtered_count"]
                seen.update(key[0] for key in keys)
                blocks.append(
                    MaterializedBlock(
                        f"brief_group/{section}:{group_key}/members/{ordinal}",
                        "dashboard_brief_group",
                        payload,
                        keys=keys,
                        sources=tuple(
                            sorted({source for values in item_sources for source in values})
                        ),
                        voucher_version_ids=tuple(
                            sorted({ident for values in item_versions for ident in values})
                        ),
                        total_count=page["total_count"],
                        filtered_count=count,
                        item_sources=item_sources,
                        item_voucher_version_ids=item_versions,
                        item_fact_sources=tuple(() for _ in members),
                    )
                )
                if not page["has_more"]:
                    if len(seen) != count:
                        raise _invalid()
                    break
                following = page["next_cursor"]
                if not keys or following != keys[-1][0] or following == after:
                    raise _invalid()
                after, ordinal = following, ordinal + 1
    return tuple(blocks)


def _model(dashboard, snap):
    from .dashboard_model_runtime import authenticated_model

    model = authenticated_model(dashboard, snap)
    if model is not None and model.descriptor.content_version >= 5:
        # These models authenticate overviews only; members and exact vouchers
        # retain the publication version and use bounded native reads on demand.
        return None
    return model


def _member_selection(model, section, group_key, *, after, limit):
    refs = model.collection_references("brief_group", f"{section}:{group_key}", "members")
    if not refs:
        raise KernelError("dashboard_group_not_found", "这组业务已变化，请重新加载。")
    keys = [key[0] for ref in refs for key in ref.keys]
    if after is None:
        start = 0
    else:
        try:
            start = keys.index(after) + 1
        except ValueError as exc:
            raise KernelError(
                "dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。"
            ) from exc
    stop = min(start + limit, len(keys))
    items, vouchers, sources, facts, versions = [], {}, set(), set(), set()
    reference = refs[0]
    page = {
        "total_count": reference.total_count,
        "filtered_count": reference.filtered_count,
        **({"collection_version": reference.collection_version}
           if reference.collection_version is not None else {}),
    }
    offset = 0
    for ref in refs:
        end = offset + ref.item_count
        if offset < stop and end > start:
            block = model.read_page_block(ref.key)
            content = block.payload["data"]["collections"]
            positions = range(max(start - offset, 0), min(stop - offset, ref.item_count))
            card_index = {item["voucher_version_id"]: item for item in content["vouchers"]["items"]}
            for position in positions:
                member = content["members"]["items"][position]
                items.append(member)
                sources.update(block.item_sources[position])
                facts.update(block.item_fact_sources[position])
                for ident in block.item_voucher_version_ids[position]:
                    versions.add(ident)
                    card = card_index.get(ident)
                    if card is None or ident in vouchers and vouchers[ident] != card:
                        raise _invalid()
                    vouchers[ident] = card
        offset = end
    # An empty tail needs only the complete authenticated directory's context,
    # not the unreturned final member block or its unrelated source bodies.
    paging = {
        **page,
        "returned_count": len(items),
        "has_more": stop < len(keys),
        "next_cursor": keys[stop - 1] if stop < len(keys) and stop > start else None,
    }
    return items, list(vouchers.values()), paging, sources, facts, versions


def read_group(dashboard, snap, *, section, group_key, cursor=None, limit=20):
    """Read a member page and its exact vouchers from this request's model."""
    model = _model(dashboard, snap)
    if model is None:
        return None
    from .dashboard_model_reads import _verify_hits

    filters = {"group_key": group_key}
    refs = model.collection_references("brief_group", f"{section}:{group_key}", "members")
    if not refs:
        raise KernelError("dashboard_group_not_found", "这组业务已变化，请重新加载。")
    after = decode_cursor(
        snap, "brief-group", section, cursor, filters,
        collection_version=refs[0].collection_version,
    )
    items, vouchers, page, sources, facts, versions = _member_selection(
        model,
        section,
        group_key,
        after=after,
        limit=limit,
    )
    _verify_hits(
        snap,
        sources,
        facts,
        versions,
        voucher_items=vouchers,
        allow_historical_vouchers=section == "open_items",
    )
    data = {
        "section": section,
        "group_key": group_key,
        "collections": {
            "members": {
                "items": items,
                "page": seal_page(
                    snap,
                    "brief-group",
                    section,
                    page,
                    filters,
                ),
            },
            "vouchers": {
                "items": vouchers,
                "page": {
                    "total_count": len(vouchers),
                    "filtered_count": len(vouchers),
                    "returned_count": len(vouchers),
                    "has_more": False,
                    "next_cursor": None,
                },
            },
        },
    }
    return {**dashboard._response(snap, data), "schema_version": 3}


def read_focused_voucher(
    dashboard, snap, *, voucher_version_id=None, voucher_number=None, limit=20
):
    """Compose the existing native focused brief using authenticated exact directories."""
    if voucher_number is not None and not -(1 << 63) <= voucher_number < (1 << 63):
        # The native selector deliberately retains SQLite's integer binding
        # rejection. A derived directory must not turn it into a missing hit.
        return None
    model = _model(dashboard, snap)
    if model is None:
        return None
    from .dashboard_model_reads import _verify_hits, read_page

    target = []
    for ref in model.collection_references("brief", "default", "vouchers"):
        for position, key in enumerate(ref.keys):
            if (
                voucher_version_id is not None
                and key[1] == voucher_version_id
                or voucher_number is not None
                and key[0] == voucher_number
            ):
                target.append((ref, position))
    if not target:
        raise KernelError("dashboard_voucher_not_found", "所选月份没有这项精确业务")
    if len(target) != 1:
        raise _invalid()
    ref, position = target[0]
    block = model.read_page_block(ref.key)
    voucher = block.payload["data"]["collections"]["vouchers"]["items"][position]
    sources, facts = set(block.item_sources[position]), set(block.item_fact_sources[position])
    versions = set(block.item_voucher_version_ids[position])
    focused, summary = None, None
    selected = []
    for ref in model.page_references("brief_group"):
        if ref.key.startswith("brief_group/activity:"):
            selected.extend(
                (ref, offset)
                for offset, key in enumerate(ref.keys)
                if key[1] == voucher["voucher_version_id"]
            )
    if len(selected) == 1:
        ref, offset = selected[0]
        activity = model.read_page_block(ref.key)
        focused = activity.payload["data"]["collections"]["members"]["items"][offset]
        sources.update(activity.item_sources[offset])
        facts.update(activity.item_fact_sources[offset])
        matching = [
            (ref, offset)
            for ref in model.collection_references("brief", "default", "activity")
            for offset, key in enumerate(ref.keys)
            if key[0] == focused["group_key"]
        ]
        if len(matching) != 1:
            raise _invalid()
        ref, offset = matching[0]
        summary = model.read_page_block(ref.key).payload["data"]["collections"]["activity"][
            "items"
        ][offset]
    elif not selected:
        raise _invalid()
    _verify_hits(snap, sources, facts, versions, voucher_items=[voucher])
    overview = read_page(dashboard, snap, "brief", limit=limit)
    if overview is None:
        raise _invalid()
    return {
        **overview,
        "data": {
            **overview["data"],
            "focused_activity": focused,
            "focused_activity_group": summary,
            "focused_voucher": voucher,
        },
    }
