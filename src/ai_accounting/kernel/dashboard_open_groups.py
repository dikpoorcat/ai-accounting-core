"""Complete object/matter groups with bounded, exact obligation members.

Whole-scope reads retain only verified amounts and formal source locators. Facts,
results, settlement histories and vouchers are not hydrated to make group totals.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

from .contracts import KernelError
from .types import YearMonth, canonical, checked

CONFIGURATIONS = {
    "customer_receivables": ("待收客户款", "receivable"),
    "supplier_advances": ("待冲抵供应商预付款", "receivable"),
    "refundable_deposit_receivables": ("待收回保证金", "receivable"),
    "other_receivables": ("其他应收事项", "receivable"),
    "supplier_payables": ("待付供应商款", "payable"),
    "employee_payables": ("待付员工报销款", "payable"),
    "payroll_payables": ("待付工资、社保与个税", "payable"),
    "labor_payables": ("待付个人劳务及个税", "payable"),
    "other_payables": ("其他应付事项", "payable"),
}
CONTRIBUTIONS = frozenset(
    {
        "employee_social",
        "employer_social",
        "employee_housing",
        "employer_housing",
    }
)
PAYROLL_MATTER_KINDS = frozenset({"payroll", "payroll_bounded", "opening_payroll_payable"})


def _sum(values):
    total = 0
    for value in values:
        if value is None:
            return None
        total = checked(total + value)
    return total


def _status(members):
    """Keep a member's abnormal progress even if group amounts cancel out."""
    states = {member.get("settlement_status") if member else None for member in members}
    if None in states or "unestablished" in states or "unknown" in states:
        return "unestablished"
    if "checking" in states:
        return "checking"
    if "over_settled" in states:
        return "over_settled"
    if states == {"settled"}:
        return "settled"
    if "partial" in states or "settled" in states:
        return "partial"
    return "open"


def _source_headers(snap, sources):
    """Bind narrow consumed fact scalars to their exact sealed source identities.

    Reuse the established physical fact verifier's SQL hash fast path. Exceptional
    noncanonical/composite facts retain its strict raw decoder fallback.
    """
    sources = list(sources)
    identifiers = {row["source_calculation_id"] for row in sources}
    if not identifiers:
        return {}
    if None in identifiers:
        raise KernelError("content_integrity_failed", "待收待付的精确业务来源缺失")
    headers = {
        row["id"]: dict(row)
        for row in snap.connection.execute(
            "SELECT c.id,c.subject_id,c.kind,c.period,c.fact_id,"
            "f.subject_id fact_subject,f.period fact_period,f.digest fact_digest,s.kind fact_kind,"
            "EXISTS(SELECT 1 FROM calculation_seal z WHERE z.calculation_id=c.id) cs,"
            "EXISTS(SELECT 1 FROM fact_seal z WHERE z.fact_id=f.id) fs "
            "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
            "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id",
            (canonical(sorted(identifiers)),),
        )
    }
    if headers.keys() != identifiers:
        raise KernelError("content_integrity_failed", "待收待付的精确业务来源缺失")
    for item in sources:
        header = headers[item["source_calculation_id"]]
        business = item.get("source_business") or {}
        if (
            not header["cs"]
            or not header["fs"]
            or (header["subject_id"], header["kind"], header["period"])
            != (header["fact_subject"], header["fact_kind"], header["fact_period"])
            or (header["subject_id"], header["kind"], header["fact_id"])
            != (business.get("subject_id"), business.get("kind"), item.get("source_fact_id"))
        ):
            raise KernelError("content_integrity_failed", "待收待付的精确业务来源不匹配")
    snap.reads._verify_selected_fact_bodies(headers.values())
    return headers


def _locators(snap, sources):
    from .dashboard import LABOR_KINDS, PAYROLL_KINDS
    from .dashboard_sort import fact_sort_scalars

    selected = [
        row
        for row in sources
        if (row.get("source_business") or {}).get("kind")
        in PAYROLL_KINDS | LABOR_KINDS | {"opening_payroll_payable", "labor_project_cost"}
    ]
    headers = _source_headers(snap, selected)
    scalars = fact_sort_scalars(snap, headers.values()) if headers else {}
    return headers, scalars


def _identity(snap, source, scalars, supplements=None):
    from .dashboard import _open_item_display

    business = source.get("source_business") or {}
    kind = business.get("kind", "")
    data = scalars.get(source.get("source_fact_id"), {})
    component = data.get("component") if kind == "opening_payroll_payable" else source.get("name")
    party_id, missing, description = _open_item_display(
        kind,
        component,
        data,
        source.get("counterparty_id"),
    )
    supplement = (supplements or {}).get(source["key"])
    if not party_id and supplement is not None:
        party_id = supplement["party_id"]
    # Payroll source representations refer to the same formal payment matters.
    # The four contribution components form one employee group across months;
    # their exact month/component remain on each expanded member.
    if kind in PAYROLL_MATTER_KINDS:
        payment_matter = "tax" if component == "withheld_tax" else component
        if component in CONTRIBUTIONS:
            payment_matter, description = "contributions", "社保与公积金"
        matter = ("payroll", payment_matter)
    else:
        matter = (kind, component)
    # An obligation with no formal displayed object never borrows a display name
    # or the agency creditor of another employee's contribution as its identity.
    identity = party_id or source["key"]
    key = hashlib.sha256(
        canonical(
            [
                snap.store.company_id,
                source["category_key"],
                source.get("category"),
                identity,
                matter,
            ]
        ).encode("utf-8")
    ).hexdigest()
    return {
        "group_key": key,
        "party_id": party_id,
        "missing_party": missing,
        "description": description,
        "component": component,
        "fact_data": data,
    }


def _data(snap):
    if hasattr(snap, "_open_group_data"):
        return snap._open_group_data
    from .dashboard_party_supplements import pass_through_party_supplements
    from .dashboard_sort import _prime_party_metadata
    from .settlement_projection import settlement_dashboard_open_sources

    historical = settlement_dashboard_open_sources(snap.connection, snap.period, reads=snap.reads)
    sources = historical["obligations"]
    current = settlement_dashboard_open_sources(
        snap.connection,
        snap.period,
        current=True,
        page_keys={row["key"] for row in sources},
        reads=snap.reads,
    )
    _, scalars = _locators(snap, sources)
    supplements = pass_through_party_supplements(snap, sources)
    identities = {row["key"]: _identity(snap, row, scalars, supplements) for row in sources}
    parties = {value["party_id"] for value in identities.values() if value["party_id"]}
    _prime_party_metadata(snap, parties)
    grouped = defaultdict(list)
    for source in sources:
        grouped[identities[source["key"]]["group_key"]].append(source)
    ordered = sorted(
        grouped,
        key=lambda key: (
            snap.party(identities[grouped[key][0]["key"]]["party_id"])
            if identities[grouped[key][0]["key"]]["party_id"]
            else identities[grouped[key][0]["key"]]["missing_party"],
            identities[grouped[key][0]["key"]]["description"],
            key,
        ),
    )
    result = {
        "historical": historical,
        "current": current,
        "groups": grouped,
        "ordered": ordered,
        "identities": identities,
        "current_by_key": {row["key"]: row for row in current["obligations"]},
    }
    snap._open_group_data = result
    return result


def _page(keys, after, limit):
    if limit < 1:
        raise ValueError("page limit must be positive")
    if after is not None and after not in keys:
        raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。")
    start = keys.index(after) + 1 if after is not None else 0
    selected = keys[start : start + limit]
    more = start + len(selected) < len(keys)
    return selected, {
        "total_count": len(keys),
        "filtered_count": len(keys),
        "returned_count": len(selected),
        "has_more": more,
        "next_cursor": selected[-1] if more else None,
    }


def _totals(shared):
    result = {}
    for direction in ("receivable", "payable"):
        rows = [
            value
            for key, value in shared["categories"].items()
            if CONFIGURATIONS[key][1] == direction
        ]
        result[direction + "_count"] = sum(row["count"] for row in rows)
        result[direction + "_fen"] = _sum(row["amount"] for row in rows)
    return result | {
        "total_count": sum(row["count"] for row in shared["categories"].values()),
        "unestablished_count": 0,
        "complete": shared["complete"],
        "status": shared["status"],
        "issues": shared["issues"],
    }


def _group(snap, data, key):
    members = data["groups"][key]
    identity = data["identities"][members[0]["key"]]
    current = [data["current_by_key"].get(row["key"]) for row in members]
    amounts = {
        name: _sum(row[name] for row in members)
        for name in ("source_amount_fen", "paid_fen", "other_settled_fen", "remaining_fen")
    }
    remaining = amounts.pop("remaining_fen")
    current_remaining = _sum(row.get("remaining_fen") if row else None for row in current)
    return {
        "id": key,
        "group_key": key,
        "category_key": members[0]["category_key"],
        "party": snap.party_field(identity["party_id"], missing=identity["missing_party"])["party"],
        "description": identity["description"],
        "member_count": len(members),
        **amounts,
        "outstanding_fen": remaining,
        "current_outstanding_fen": current_remaining,
        "status": _status(members),
        "current_status": _status(current),
    }


def open_group_page(snap, *, after=None, limit=20, summary_only=False):
    data = _data(snap)
    selected, page = _page(data["ordered"], after, limit)
    items = [] if summary_only else [_group(snap, data, key) for key in selected]
    category_groups = defaultdict(list)
    for key, members in data["groups"].items():
        category_groups[members[0]["category_key"]].append(key)
    categories = []
    for key, (label, direction) in CONFIGURATIONS.items():
        total = data["historical"]["categories"].get(key)
        if total is None:
            continue
        categories.append(
            {
                "key": key,
                "label": label,
                "direction": direction,
                "unit": "笔",
                "count": total["count"],
                "group_count": len(category_groups[key]),
                "loaded_count": sum(
                    item["member_count"] for item in items if item["category_key"] == key
                ),
                "outstanding_fen": total["amount"],
            }
        )
    return _totals(data["historical"]) | {
        "group_count": len(data["groups"]),
        "categories": categories,
        "current_outstanding": _totals(data["current"]),
        "collection": {"items": items, "page": page},
        "cutoff_period": data["historical"]["cutoff_period"],
        "current_cutoff_period": data["current"].get(
            "current_cutoff_period", data["current"]["cutoff_period"]
        ),
    }


def open_group_members(snap, group_key, *, after=None, limit=20):
    from .dashboard import _contribution_identity, _open_item_display, _recognition
    from .dashboard_sort import fact_sort_scalars

    data = _data(snap)
    members = data["groups"].get(group_key)
    if members is None:
        raise KernelError("dashboard_group_not_found", "这组待收待付已变化，请重新加载。")
    # Source month is obtained from its exact adopted calculation. Member ordering
    # never substitutes a management date for the accounting month.
    headers = {
        row["id"]: dict(row)
        for row in snap.connection.execute(
            "SELECT c.id,c.fact_id,c.kind,c.period FROM json_each(?) ids "
            "JOIN calculation c ON c.id=ids.value",
            (canonical(sorted({row["source_calculation_id"] for row in members})),),
        )
    }
    ordered = sorted(
        members,
        key=lambda row: (
            headers[row["source_calculation_id"]]["period"],
            data["identities"][row["key"]]["component"] or "",
            row["key"],
        ),
    )
    selected, page = _page([row["key"] for row in ordered], after, limit)
    selected_set = set(selected)
    sources = [row for row in ordered if row["key"] in selected_set]
    selected_headers = _source_headers(snap, sources)
    scalars = fact_sort_scalars(snap, selected_headers.values())
    subjects = {(source.get("source_business") or {}).get("subject_id") for source in sources}
    subjects.discard(None)
    snap.metadata.prime_profiles("business", subjects)
    snap.management.prime(subjects)
    voucher_rows = {}
    for row in snap.connection.execute(
        "SELECT p.calculation_id,v.id,v.period,n.number FROM json_each(?) ids "
        "JOIN calculation_publication p ON p.calculation_id=ids.value "
        "JOIN voucher_version v ON v.calculation_id=p.calculation_id "
        "AND v.voucher_id=p.voucher_id AND v.reverses_id IS NULL "
        "JOIN voucher n ON n.id=v.voucher_id",
        (canonical(sorted(selected_headers)),),
    ):
        if row["calculation_id"] in voucher_rows:
            raise KernelError("content_integrity_failed", "待收待付的精确凭证来源不唯一")
        voucher_rows[row["calculation_id"]] = row
    result = []
    for source in sources:
        identity = data["identities"][source["key"]]
        business = source.get("source_business") or {}
        fact_data = scalars[source["source_fact_id"]]
        period = str(
            YearMonth.from_ordinal(selected_headers[source["source_calculation_id"]]["period"])
        )
        recognition = _recognition(fact_data, period)
        current = data["current_by_key"].get(source["key"])
        voucher = voucher_rows.get(source["source_calculation_id"])
        _, _, description = _open_item_display(
            business.get("kind", ""),
            identity["component"],
            fact_data,
            source.get("counterparty_id"),
        )
        profile = snap.profile("business", business.get("subject_id"))
        supplied = dict.fromkeys(
            value.strip()
            for value in (
                profile.get("purpose"),
                profile.get("note"),
                (snap.management.get(business.get("subject_id")) or {}).get("note"),
            )
            if isinstance(value, str) and value.strip()
        )
        purpose = "；".join(supplied) or None
        result.append(
            {
                "id": source["key"],
                "group_key": group_key,
                "category_key": source["category_key"],
                "party": snap.party_field(identity["party_id"], missing=identity["missing_party"])[
                    "party"
                ],
                "description": description,
                "purpose": purpose,
                "status": source["settlement_status"],
                **{
                    name: source[name]
                    for name in ("source_amount_fen", "paid_fen", "other_settled_fen")
                },
                "outstanding_fen": source["remaining_fen"],
                "current_status": current.get("settlement_status") if current else None,
                "current_outstanding_fen": current.get("remaining_fen") if current else None,
                "subject_id": business.get("subject_id"),
                "source_period": period,
                "date": recognition["date"],
                "recognition": recognition,
                "voucher_version_id": voucher["id"] if voucher else None,
                "voucher_number": voucher["number"] if voucher else None,
                **_contribution_identity(
                    snap.store.company_id, business.get("kind"), fact_data, identity["component"]
                ),
            }
        )
    return {"items": result, "page": page}
