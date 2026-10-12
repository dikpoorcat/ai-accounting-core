"""Finite native filters over the authenticated complete dashboard directory.

The first directory slot remains the native seek key. Auxiliary slots are a
versioned, finite projection of already produced native rows, never caller JSON
or a second accounting reducer. Only blocks containing returned rows are read.
"""

from __future__ import annotations

from .contracts import KernelError
from .dashboard_pages import decode_cursor, seal_page

# Part of the producer content contract; changes require its version advance.
FILTER_DIRECTORY_VERSION = 3
_WIDTHS = {
    ("funds", "movements"): 3,
    ("funds", "statements"): 2,
    ("employees", "employees"): 5,
    ("employees", "labor_workers"): 4,
    ("employees", "labor_sources"): 2,
    ("assets", "assets"): 3,
    ("assets", "projects"): 2,
}


def _invalid():
    return KernelError("content_integrity_failed", "看板筛选目录与完整原生范围不一致")


def decorate_keys(page, section, keys, items):
    """Project native row predicates once during authoritative production."""
    if len(keys) != len(items):
        raise _invalid()
    result = []
    for key, item in zip(keys, items, strict=True):
        if (page, section) == ("funds", "movements"):
            entry = (key, item["account_type"], item["account_id"])
        elif (page, section) == ("funds", "statements"):
            entry = (key, item["account_id"])
        elif (page, section) == ("employees", "employees"):
            entry = (
                key,
                item.get("period_state", "unknown"),
                "true"
                if item.get("in_period") is True
                else "false"
                if item.get("in_period") is False
                else "unknown",
                item["selection_status"],
                ",".join(sorted(item["period_states"])) or "-",
            )
        elif (page, section) == ("employees", "labor_workers"):
            if key != item["person_id"]:
                raise _invalid()
            entry = (
                key,
                "true" if item["in_period"] is True else "false"
                if item["in_period"] is False else "unknown",
                item["employment_state"],
                ",".join(sorted(item["period_states"])) or "-",
            )
        elif (page, section) == ("employees", "labor_sources"):
            entry = (key, item["person_id"])
        elif (page, section) == ("assets", "assets"):
            entry = (key, item["asset_type"], item["status"])
        elif (page, section) == ("assets", "projects"):
            entry = (key, item["project_id"])
        else:
            entry = (key,)
        _check_entry(page, section, entry)
        result.append(entry)
    return tuple(result)


def _check_entry(page, section, entry):
    if len(entry) != _WIDTHS.get((page, section), 1):
        raise _invalid()
    if type(entry[0]) not in (str, int) or any(
        type(value) is not str or not value for value in entry[1:]
    ):
        raise _invalid()
    if (page, section) == ("funds", "movements"):
        from .dashboard_funds import FUND_TYPES

        if entry[1] not in FUND_TYPES.values():
            raise _invalid()
    elif (page, section) == ("employees", "employees"):
        if (
            entry[1] not in {"unknown", "not_started", "ended", "in_period"}
            or entry[2] not in {"true", "false", "unknown"}
            or entry[3] not in {"established", "unestablished"}
            or entry[4] != "-"
            and (
                not set(entry[4].split(","))
                <= {"regular", "unpaid_leave", "departed", "labor", "labor_ended", "unknown"}
                or entry[4] != ",".join(sorted(set(entry[4].split(","))))
            )
        ):
            raise _invalid()
    elif (page, section) == ("employees", "labor_workers"):
        states = {"regular", "unpaid_leave", "departed", "labor", "labor_ended", "unknown"}
        if (entry[1] not in {"true", "false", "unknown"} or entry[2] not in states
            or entry[3] != "-" and (
                not set(entry[3].split(",")) <= states
                or entry[3] != ",".join(sorted(set(entry[3].split(","))))
            )):
            raise _invalid()
    elif (page, section) == ("assets", "assets"):
        if entry[1] not in {"fixed", "intangible"} or entry[2] not in {
            "active",
            "pending_activation",
            "disposed",
            "retired",
        }:
            raise _invalid()


def directory_matches(page, section, entry, filters, *, employee_states=None):
    """Mirror native filter semantics without loading non-hit business bodies."""
    _check_entry(page, section, entry)
    if (page, section) == ("funds", "movements"):
        return not filters.get("movement_account_type") or entry[1:] == (
            filters["movement_account_type"],
            filters["movement_account_id"],
        )
    if (page, section) == ("funds", "statements"):
        return (
            not filters.get("statement_account_id") or entry[1] == filters["statement_account_id"]
        )
    if (page, section) == ("employees", "employees"):
        if employee_states is None or entry[0] not in employee_states:
            raise _invalid()
        selected = filters["employee_filter"]
        monthly_states = set() if entry[4] == "-" else set(entry[4].split(","))
        matches = (
            selected == "all"
            or selected == "in_period"
            and entry[2] == "true"
            or selected == "unknown"
            and (entry[1] == "unknown" or entry[3] == "unestablished")
            or selected == "ended"
            and entry[1] == "ended"
            or selected == "employment_active"
            and "regular" in monthly_states
            or selected.startswith("employment_")
            and selected != "employment_active"
            and (
                selected.removeprefix("employment_") in monthly_states
                or selected == "employment_departed"
                and employee_states[entry[0]] == "departed"
                or selected == "employment_unknown"
                and entry[1] == "unknown"
            )
        )
        return matches and (
            filters.get("employee_id") is None or entry[0] == filters["employee_id"]
        )
    if (page, section) == ("employees", "labor_workers"):
        from .employee_employment import labor_filter_matches

        membership = True if entry[1] == "true" else False if entry[1] == "false" else None
        states = () if entry[3] == "-" else entry[3].split(",")
        return labor_filter_matches(filters["employee_filter"], membership, states, entry[2]) and (
            filters.get("employee_id") is None or entry[0] == filters["employee_id"]
        )
    if (page, section) == ("employees", "labor_sources"):
        return filters.get("employee_id") is None or entry[1] == filters["employee_id"]
    if (page, section) == ("assets", "assets"):
        selected = filters["asset_filter"]
        matches = (
            selected == "all"
            or selected == "active"
            and entry[2] == "active"
            or selected in {"fixed", "intangible"}
            and entry[1] == selected
            or selected == "pending"
            and entry[2] == "pending_activation"
            or selected == "exited"
            and entry[2] in {"disposed", "retired"}
        )
        return matches and (filters.get("asset_id") is None or entry[0] == filters["asset_id"])
    if (page, section) == ("assets", "projects"):
        return filters.get("project_id") is None or entry[1] == filters["project_id"]
    return True


def read_filtered_collection(
    model,
    snap,
    page,
    section,
    *,
    filters,
    cursor=None,
    limit=20,
    employee_states=None,
):
    """Consume complete certified counts and authenticate only returned raw hits.

    Absence of the necessary producer variant is an explicit native fallback.
    Present but damaged directory/content never falls back or repairs itself.
    """
    variant = (
        "all"
        if (page, section)
        in {
            ("funds", "movements"),
            ("employees", "employees"),
            ("employees", "labor_workers"),
        }
        else "default"
    )
    refs = model.collection_references(page, variant, section)
    if not refs:
        return None
    total = refs[0].total_count
    if total is None or any(
        ref.filtered_count != total or ref.total_count != total for ref in refs
    ):
        raise _invalid()
    directory = [
        (ref, position, entry)
        for ref in refs
        for position, entry in enumerate(ref.keys)
        if directory_matches(page, section, entry, filters, employee_states=employee_states)
    ]
    after = decode_cursor(snap, page, section, cursor, filters)
    start = 0
    if after is not None:
        found = next(
            (index for index, (_, _, entry) in enumerate(directory) if entry[0] == after), None
        )
        if found is None:
            raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新读取")
        start = found + 1
    selected = directory[start : start + limit]
    items, sources, facts, vouchers = [], set(), set(), set()
    # A page can span blocks or a sparse filter can hit many blocks; each hit
    # block is authenticated once. Nothing is retained across requests.
    blocks = {}
    for ref, position, entry in selected:
        if ref.key not in blocks:
            blocks[ref.key] = model.read_page_block(ref.key)
        block = blocks[ref.key]
        item = block.payload["data"]["collections"][section]["items"][position]
        # Match the exact projection in both authenticated root and body. The
        # loader also checks their metadata equality and native response type.
        if decorate_keys(page, section, [entry[0]], [item])[0] != entry:
            raise _invalid()
        if (page, section) == ("employees", "employees"):
            item = {**item, "employment_state": employee_states[entry[0]]}
        items.append(item)
        sources.update(block.item_sources[position])
        facts.update(block.item_fact_sources[position])
        vouchers.update(block.item_voucher_version_ids[position])
    from .dashboard_model_reads import _verify_hits

    _verify_hits(snap, sources, facts, vouchers)
    more = start + len(items) < len(directory)
    paging = {
        "total_count": total,
        "filtered_count": len(directory),
        "returned_count": len(items),
        "has_more": more,
        "next_cursor": selected[-1][2][0] if more and selected else None,
    }
    return {"items": items, "page": seal_page(snap, page, section, paging, filters)}


def read_filtered_data(model, snap, page, data, *, filters, section=None, cursor=None, limit=20):
    """Keep certified company-wide summaries while adapting native selection."""
    if page not in {"funds", "employees", "assets"}:
        return None
    states = None
    if page == "employees":
        from .dashboard_model_dates import employee_states

        states = employee_states(model.root.date_basis, snap.as_of)
    collections = {}
    for name in [section] if section else data["collections"]:
        collection = read_filtered_collection(
            model,
            snap,
            page,
            name,
            filters=filters,
            cursor=cursor if name == section else None,
            limit=limit,
            employee_states=states,
        )
        if collection is None:
            return None
        collections[name] = collection
    result = {**data, "collections": collections}
    if page == "funds":
        result["selected_movement_account"] = (
            {
                "type": filters["movement_account_type"],
                "account_id": filters["movement_account_id"],
            }
            if filters.get("movement_account_type")
            else None
        )
    else:
        result.update(filters)
    return result
