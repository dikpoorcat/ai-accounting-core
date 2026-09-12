"""Bound page contexts and explicit projections of the shared business contracts."""

from __future__ import annotations

import base64
import hashlib
import json

from .contracts import KernelError
from .types import canonical

SECTIONS = {
    "brief": frozenset(
        {
            "vouchers",
            "businesses",
            "open_items",
            "settlement_events",
            "external_followups",
            "file_jobs",
        }
    ),
    "funds": frozenset(
        {"accounts", "movements", "statements", "investment_products", "investment_events"}
    ),
    "employees": frozenset({"employees", "payroll_sources", "labor_sources", "settlement_events"}),
    "assets": frozenset({"assets", "projects", "source_history", "settlement_events"}),
    "business-status": frozenset({"events", "settlement_events", "source_history", "file_jobs"}),
}


def validate_page(endpoint, section, cursor, limit):
    if section is not None and section not in SECTIONS[endpoint]:
        raise ValueError("不支持的明细集合")
    if cursor is not None and section is None:
        raise ValueError("分页游标须指定明细集合")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("每页数量必须为 1 至 500")


def page_scope(snapshot, endpoint, section, filters, *, collection_version=None):
    return hashlib.sha256(
        canonical(
            {
                "company": snapshot.store.company_id,
                "database": snapshot.store.database_id,
                "period": snapshot.period,
                "snapshot_version": snapshot.snapshot_version,
                "as_of": snapshot.as_of,
                "endpoint": endpoint,
                "section": section,
                "filters": filters,
                "collection_version": collection_version,
            }
        ).encode()
    ).hexdigest()


def decode_cursor(snapshot, endpoint, section, cursor, filters, *, collection_version=None):
    if cursor is None:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if value["scope"] != page_scope(
            snapshot, endpoint, section, filters, collection_version=collection_version
        ):
            raise ValueError("scope")
        if type(value["key"]) not in (str, int):
            raise ValueError("page key")
        return value["key"]
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise KernelError(
            "dashboard_snapshot_changed", "筛选、资料或分页位置已变化，请重新加载明细。"
        ) from exc


def seal_page(snapshot, endpoint, section, page, filters):
    page = dict(page)
    if page.get("next_cursor") is not None:
        page["next_cursor"] = base64.urlsafe_b64encode(
            canonical(
                {
                    "scope": page_scope(
                        snapshot,
                        endpoint,
                        section,
                        filters,
                        collection_version=page.get("collection_version"),
                    ),
                    "key": page["next_cursor"],
                }
            ).encode()
        ).decode()
    return page


def seal_collections(snapshot, endpoint, data, filters):
    for section, collection in data.get("collections", {}).items():
        collection["page"] = seal_page(snapshot, endpoint, section, collection["page"], filters)
    return data


def preparation_view(value):
    """A labelled page projection, never a replacement for period_readiness."""

    def nullable_sum(values):
        items = list(values)
        return None if any(item is None for item in items) else sum(items)

    def readiness(part):
        if part is None:
            return None
        return {key: part[key] for key in ("period", "order_failure", "issues") if key in part}

    followups = value["current_followups"]
    settlements = followups["settlements"]
    obligations = settlements["obligations"]
    unknown = settlements.get("unestablished_state_selections", ())
    frozen = value["frozen_readiness"]
    return {
        **{
            key: value[key]
            for key in (
                "company_id",
                "database_id",
                "period",
                "as_of",
                "as_of_semantics",
                "closure",
                "read_semantics",
            )
        },
        "projection": "dashboard_period_preparation",
        "frozen_readiness": None
        if frozen is None
        else {
            key: (
                {"status": item["status"]} if isinstance(item, dict) and "status" in item else item
            )
            for key, item in frozen.items()
        },
        "readiness": readiness(value["readiness"]),
        "current_followups": {
            "knowledge": followups["knowledge"],
            "affects_frozen_readiness": False,
            "materials": {
                "status": followups["materials"]["status"],
                "issues": followups["materials"]["issues"],
                "inventory_count": len(followups["materials"]["inventories"]),
                "coverage_digest": followups["materials"]["coverage"]["coverage_digest"],
            },
            "accounting": {
                key: followups["accounting"][key]
                for key in ("status", "issues", "pending_subject_id")
            }
            | {"unpublished_count": len(followups["accounting"]["unpublished"])},
            "close_requirements": {
                key: followups["close_requirements"][key] for key in ("status", "issues")
            },
            "settlements": {
                key: settlements[key]
                for key in ("status", "cutoff_period", "current_cutoff_period", "issues")
                if key in settlements
            }
            | {
                "obligation_count": len(obligations),
                "complete": settlements.get("complete", True),
                "unestablished_state_selection_count": len(unknown),
                "movement_count": settlements.get("movement_count", 0),
                "source_amount_fen": None
                if unknown
                else nullable_sum(item["source_amount_fen"] for item in obligations),
                "paid_fen": None
                if unknown
                else nullable_sum(item["paid_fen"] for item in obligations),
                "other_settled_fen": None
                if unknown
                else nullable_sum(item["other_settled_fen"] for item in obligations),
                "remaining_fen": None
                if unknown
                else nullable_sum(item["remaining_fen"] for item in obligations),
            },
            "external": followups["external"],
            "file_jobs": followups["file_jobs"],
        },
    }
