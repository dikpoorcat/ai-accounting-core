"""Compose the existing brief from shared month components and bounded collections."""

from datetime import UTC, datetime

from ai_accounting_modular.contracts.public import ContractError

from . import _brief_groups, _brief_open, _brief_overview, _brief_vouchers, _close_review, _monthly

COMPONENTS = (
    _monthly.FINANCIAL,
    _monthly.FUNDS,
    _monthly.ASSETS,
    _monthly.EMPLOYEES,
    _monthly.BRIEF_ACTIVITY,
    _monthly.BRIEF_OPEN_ITEMS,
    _monthly.BRIEF_CONTEXT,
)


def build(reader, snapshot, read, query):
    selected = query.get("section")
    collections = {}
    if selected in (None, "activity"):
        collections["activity"] = _brief_groups.activity_page(snapshot, read, query)
    open_page = (
        _brief_open.page(snapshot, read, query) if selected in (None, "open_items") else None
    )
    if open_page is not None:
        collections.update(open_page["collections"])
    focused = None
    if selected == "vouchers" or "voucher_number" in query or "voucher_version_id" in query:
        vouchers = _brief_vouchers.page(reader, snapshot, read, query)
        collections["vouchers"] = vouchers
        if "voucher_number" in query or "voucher_version_id" in query:
            if not vouchers["items"]:
                if "voucher_version_id" in query:
                    raise ContractError(
                        "snapshot_changed", "所选凭证版本已经失效，请重新读取简报。"
                    )
                raise ContractError("not_found", "本月没有所定位的正式凭证。")
            focused = vouchers["items"][0]
    if selected is not None:
        return {
            "selected_period": snapshot.selected_period,
            "snapshot_version": snapshot.version,
            "data": {"collections": collections},
        }
    activity = snapshot.components["brief_activity"]
    context = snapshot.components["brief_context"]
    data = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "month_state": snapshot.selected_period["status"],
        "owner_review_request": _close_review.request(snapshot),
        **context,
        **_brief_overview.build(snapshot),
        **{
            name: activity[name]
            for name in ("activity_count", "group_count", "voucher_count", "activity_groups")
        },
        "focused_activity": None,
        "focused_activity_group": None,
        "focused_voucher": focused,
        "open_items": open_page["open_items"]
        if open_page is not None
        else snapshot.components["brief_open_items"]["open_items"],
        "collections": collections,
    }
    if query.get("activity_key"):
        member = _brief_groups.member_scope(snapshot, read, query["activity_key"])
        data["focused_activity"] = member["row"]
        data["focused_activity_group"] = collections["activity"]["items"][0]
    return {
        "selected_period": snapshot.selected_period,
        "snapshot_version": snapshot.version,
        "data": data,
    }


def group(reader, snapshot, read, query):
    if query["section"] == "activity":
        return _brief_groups.build(reader, snapshot, read, query)
    return {
        "selected_period": snapshot.selected_period,
        "snapshot_version": snapshot.version,
        "data": _brief_open.members(reader, snapshot, read, query),
    }
