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
            "activity",
            "vouchers",
            "open_items",
        }
    ),
    "funds": frozenset(
        {"accounts", "movements", "statements", "investment_products", "investment_events"}
    ),
    "employees": frozenset({"employees", "labor_sources"}),
    "assets": frozenset({"assets", "projects"}),
    "business-status": frozenset({"settlement_events"}),
}

# Public response shapes stay unchanged. These internal profiles invalidate old
# seek positions and distinguish business-paired cards from voucher-number pages.
SORT_PROFILES = {
    ("brief", "activity"): "business-date-object/1",
    ("brief", "vouchers"): "voucher-number/1",
    ("brief", "open_items"): "object-matter/1",
    ("funds", "movements"): "business-date-object/1",
}


def cursor_sort_profile(cursor):
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode())).get("sort")
    except (ValueError, TypeError, UnicodeError, AttributeError):
        return None


def validate_page(endpoint, section, cursor, limit):
    if section is not None and section not in SECTIONS[endpoint]:
        raise KernelError("invalid_command", "不支持的明细集合")
    if cursor is not None and section is None:
        raise KernelError("invalid_command", "分页游标须指定明细集合")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise KernelError("invalid_command", "每页数量必须为 1 至 500")


def page_scope(snapshot, endpoint, section, filters, *, collection_version=None, sort_profile=None):
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
                "sort": sort_profile or SORT_PROFILES.get((endpoint, section)),
            }
        ).encode()
    ).hexdigest()


def decode_cursor(snapshot, endpoint, section, cursor, filters, *, collection_version=None, sort_profile=None):
    if cursor is None:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if not isinstance(value, dict):
            raise ValueError("cursor object")
        profile = sort_profile or SORT_PROFILES.get((endpoint, section))
        if value.get("sort") != profile:
            raise ValueError("sort")
        if value["scope"] != page_scope(
            snapshot, endpoint, section, filters, collection_version=collection_version,
            sort_profile=profile,
        ):
            raise ValueError("scope")
        if type(value["key"]) not in (str, int):
            raise ValueError("page key")
        return value["key"]
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise KernelError(
            "dashboard_snapshot_changed", "筛选、资料或分页位置已变化，请重新加载明细。"
        ) from exc


def seal_page(snapshot, endpoint, section, page, filters, *, sort_profile=None):
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
                        sort_profile=sort_profile,
                    ),
                    "key": page["next_cursor"],
                    "sort": sort_profile or SORT_PROFILES.get((endpoint, section)),
                }
            ).encode()
        ).decode()
    return page


def seal_collections(snapshot, endpoint, data, filters, *, sort_profiles=None):
    for section, collection in data.get("collections", {}).items():
        collection["page"] = seal_page(snapshot, endpoint, section, collection["page"], filters,
                                       sort_profile=(sort_profiles or {}).get(section))
    return data


def preparation_view(value):
    """A labelled page projection, never a replacement for period_readiness."""

    def readiness(part):
        if part is None:
            return None
        return {key: part[key] for key in ("period", "order_failure", "issues") if key in part}

    followups = value["current_followups"]
    settlements = followups["settlements"]
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
            "settlements": settlements,
            "external": followups["external"],
            "file_jobs": followups["file_jobs"],
            "tax_import_mapping": followups["tax_import_mapping"],
        },
    }
