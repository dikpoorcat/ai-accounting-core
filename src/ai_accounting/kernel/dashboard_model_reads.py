"""Bounded consumption of authenticated owner read models in one snapshot."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from .contracts import KernelError
from .dashboard_model_runtime import (
    authenticated_model,
    model_scope,
    producing_model,
    supports_model,
)
from .dashboard_pages import decode_cursor, seal_page
from .dashboard_read_model import load_model
from .stored_json import loads_unique
from .types import YearMonth, canonical


def _invalid():
    return KernelError("content_integrity_failed", "看板命中内容与原始来源不一致")


def read_quarterly_report(dashboard, connection, year, quarter, *, as_of):
    """Consume the complete quarterly aggregate in the already owned snapshot.

    The most recent registered month in this quarter supplies its model scope;
    a quarter does not require a business month at its calendar end. Closed
    report exports and fixed historical requests still use the native report
    APIs and their independent exact source checks.
    """
    if producing_model() or not supports_model(connection):
        return None
    start, end = f"{year:04d}-{quarter * 3 - 2:02d}", f"{year:04d}-{quarter * 3:02d}"
    # Registration belongs to immutable production receipts. A missing latest
    # locator must reach load_model's corruption checks, not select an earlier
    # month or disguise the damage as an unregistered-quarter native fallback.
    registered = connection.execute(
        "SELECT period FROM dashboard_read_model_commit WHERE period BETWEEN ? AND ? "
        "ORDER BY period DESC LIMIT 1",
        (start, end),
    ).fetchone()
    if registered is None:
        return None
    model = load_model(
        connection,
        model_scope(
            dashboard.engine,
            connection,
            registered[0],
            as_of=as_of,
        ),
    )
    if model is None:
        return None
    key = "quarterly_report/default/overview/0"
    if not model.has_block(key):
        return None
    block = model.read_page_block(key)
    # Strict native payload and quarter identity were checked by the loader.
    # The complete financial statements contain aggregates, not raw hits.
    if block.sources or block.fact_sources or block.voucher_version_ids:
        raise _invalid()
    return {
        **block.payload,
        "checked_at": datetime.now(UTC).isoformat(),
        "read_context": dashboard._read_context(connection, as_of),
    }


def _verify_hits(
    snap,
    sources,
    facts,
    vouchers,
    *,
    voucher_items=None,
    allow_historical_vouchers=False,
):
    """Authenticate only actual returned raw content, not aggregate ancestors."""
    sources, facts, vouchers = set(sources), set(facts), set(vouchers)
    calc_ids = {row[0] for row in sources}
    fact_ids = {row[0] for row in facts}
    from .dashboard_model_sources import _verify_model_voucher_rows, verify_model_sources

    normalized_lines = {}
    outcomes = verify_model_sources(
        snap.engine, snap.connection, calculation_ids=calc_ids, fact_ids=fact_ids,
        through_period=snap.month,
        normalized_lines=normalized_lines,
        expected_sources=sources, expected_facts=facts,
    )
    if vouchers:
        from .query_reads import (
            selected_voucher_sql,
            verify_current_publication_voucher_heads,
            verify_current_voucher_publications,
        )

        query, parameters = selected_voucher_sql(snap.period, voucher_ids=vouchers)
        rows = list(snap.connection.execute(query, parameters))
        if {row["id"] for row in rows} != vouchers:
            raise _invalid()
        if any(row["period"] > snap.month if allow_historical_vouchers
               else row["period"] != snap.month for row in rows):
            raise _invalid()
        snap.reads.verify_selected_voucher_adoptions(rows, through_period=snap.month)
        frozen = snap.month_journal.authenticated_frozen_adoptions(rows)
        publications = verify_current_voucher_publications(snap.connection, vouchers)
        # A reversal can consume only its old basis. Its current correction
        # publication is then outside the raw-hit batch, but still determines
        # the independently required replacement/reversal heads.
        pending = [row for row in publications.values()
                   if row["calculation_id"] not in calc_ids
                   and row["posting_period"] <= snap.month]
        closed_periods = {row[0] for row in snap.connection.execute(
            "SELECT period FROM json_each(?) ids JOIN period_close p ON p.period=ids.value",
            (canonical(sorted({row["posting_period"] for row in pending})),),
        )} if pending else set()
        verify_current_publication_voucher_heads(
            snap.connection,
            [row for row in pending if row["posting_period"] not in closed_periods],
        )
        physical = _verify_model_voucher_rows(
            snap.connection, {row["id"]: row for row in rows}, outcomes,
            normalized_lines=normalized_lines,
        )
        # The certified selection still binds each hit to the actual owner and
        # its adopted basis, including a no-impact review's newer calculation.
        if any(
            row["voucher_calculation_id"] not in calc_ids
            or (
                frozen[row["id"]]["adopted_calculation_id"]
                if row["id"] in frozen
                else row["basis_calculation_id"]
            )
            not in calc_ids
            for row in rows
        ):
            raise _invalid()
        if any(
            item[field] not in calc_ids
            for item in frozen.values()
            for field in ("calculation_id", "adopted_calculation_id")
        ):
            raise _invalid()
        headers = {row["id"]: row for row in rows}
        if voucher_items is None:
            return
        if {item["voucher_version_id"] for item in voucher_items} != vouchers:
            raise _invalid()
        for item in voucher_items:
            ident = item["voucher_version_id"]
            header = headers[ident]
            if (
                (
                    header["period"] > snap.month
                    if allow_historical_vouchers
                    else header["period"] != snap.month
                )
                or item["recognition"]["period"] != str(YearMonth.from_ordinal(header["period"]))
                or str(header["number"]) != item["number"]
                or header["total"] != item["amount_fen"]
                or header["reverses_id"] != item["reverses_version_id"]
                or [
                    (line["line_number"], line["code"], line["debit_fen"], line["credit_fen"])
                    for line in item["lines"]
                ]
                != [
                    (position, line["account"], line["debit"], line["credit"])
                    for position, line in enumerate(physical[ident]["lines"], 1)
                ]
            ):
                raise _invalid()


def _collection(model, snap, page, section, *, cursor, limit, filters, variant, original_page):
    refs = model.collection_references(page, variant, section)
    if not refs:
        raise _invalid()
    # The complete authenticated directory carries paging context. In
    # particular, vouchers are absent from the default brief: do not decode an
    # unreturned 500-row first block merely to discover its collection version.
    reference = refs[0]
    if original_page is not None and (
        (original_page["total_count"], original_page["filtered_count"],
         original_page.get("collection_version"))
        != (reference.total_count, reference.filtered_count, reference.collection_version)
    ):
        raise _invalid()
    original_page = {
        "total_count": reference.total_count,
        "filtered_count": reference.filtered_count,
        **({"collection_version": reference.collection_version}
           if reference.collection_version is not None else {}),
    }
    after = decode_cursor(
        snap,
        page,
        section,
        cursor,
        filters,
        collection_version=original_page.get("collection_version"),
    )
    keys = [key[0] for ref in refs for key in ref.keys]
    if after is None:
        start = 0
    else:
        try:
            start = keys.index(after) + 1
        except ValueError as exc:
            raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新读取") from exc
    stop = min(start + limit, len(keys))
    items, sources, facts, vouchers = [], set(), set(), set()
    offset = 0
    for reference in refs:
        end = offset + reference.item_count
        if offset < stop and end > start:
            block = model.read_page_block(reference.key)
            hit_page = block.payload["data"]["collections"][section]["page"]
            if any(
                hit_page.get(field) != original_page.get(field)
                for field in ("total_count", "filtered_count", "collection_version")
            ):
                raise _invalid()
            positions = range(max(start - offset, 0), min(stop - offset, reference.item_count))
            values = block.payload["data"]["collections"][section]["items"]
            for position in positions:
                items.append(values[position])
                sources.update(block.item_sources[position])
                facts.update(block.item_fact_sources[position])
                vouchers.update(block.item_voucher_version_ids[position])
        offset = end
    _verify_hits(
        snap,
        sources,
        facts,
        vouchers,
        voucher_items=items if page == "brief" and section == "vouchers" else None,
    )
    paging = {
        **original_page,
        "returned_count": len(items),
        "has_more": stop < len(keys),
        "next_cursor": keys[stop - 1] if stop < len(keys) and stop > start else None,
    }
    return {"items": items, "page": seal_page(snap, page, section, paging, filters)}


def read_page(
    dashboard,
    snap,
    page,
    *,
    section=None,
    cursor=None,
    limit=20,
    filters=None,
    select_first_account=False,
):
    """Return None for unsupported variants or authentic stale production.

    Damaged production content always fails. Fallback retains the complete
    native behavior; it is never a stale response or a hidden repair.
    """
    filters = filters or {}
    model = authenticated_model(dashboard, snap)
    if model is None:
        return None
    variant = "default"
    if page == "assets" and filters.get("asset_filter") == "active":
        variant = "active"
    elif page == "employees" and filters == {"employee_filter": "all", "employee_id": None}:
        variant = "all"
    elif page == "funds" and not select_first_account and not any(filters.values()):
        variant = "all"
    default_key = f"{page}/{variant}/overview/0"
    if not model.has_block(default_key):
        return None
    if page == "brief" and section is not None:
        # Native brief continuations return only this collection. Its complete
        # authenticated directory supplies counts and cursor context; decoding
        # an unrelated overview would produce money, tasks and default hits
        # that this response never consumes. Returned blocks and their actual
        # sources still receive their independent content checks below.
        collection = _collection(
            model, snap, page, section, cursor=cursor, limit=limit,
            filters=filters, variant=variant, original_page=None,
        )
        return {
            **dashboard._response(snap, {"collections": {section: collection}}),
            # Same Version19 contract as Dashboard.brief's native continuation.
            "schema_version": 19,
            "scope": section,
        }
    default = model.read_page_block(default_key)
    payload = default.payload
    data = payload["data"]
    same_selection = True
    if page == "funds":
        selected = data["selected_movement_account"]
        saved = {
            "movement_account_type": selected["type"] if selected else None,
            "movement_account_id": selected["account_id"] if selected else None,
            "statement_account_id": None,
        }
        if select_first_account:
            filters = {
                **filters,
                "movement_account_type": saved["movement_account_type"],
                "movement_account_id": saved["movement_account_id"],
            }
        same_selection = filters == saved
        if filters != saved and model.root.date_basis is None:
            return None
    elif page == "employees":
        same_selection = all(data[field] == value for field, value in filters.items())
        if model.root.date_basis is not None:
            from .dashboard_model_dates import original_date_states_valid

            same_selection = same_selection and original_date_states_valid(
                model.root.date_basis,
                snap.as_of,
            )
    elif page == "assets":
        same_selection = all(data[field] == value for field, value in filters.items())
    use_default = section is None and limit == 20 and same_selection
    if (
        page in {"funds", "employees", "assets"}
        and model.root.date_basis is not None
        and not use_default
    ):
        from .dashboard_model_filters import read_filtered_data

        data = read_filtered_data(
            model,
            snap,
            page,
            data,
            filters=filters,
            section=section,
            cursor=cursor,
            limit=limit,
        )
        if data is None:
            return None
    elif use_default:
        _verify_hits(
            snap,
            default.sources,
            default.fact_sources,
            default.voucher_version_ids,
            voucher_items=(data["collections"].get("vouchers", {}).get("items", ())
                           if page == "brief" else None),
        )
        collections = {}
        for name, collection in data["collections"].items():
            paging = dict(collection["page"])
            if paging.get("next_cursor") is not None:
                decoded = loads_unique(base64.urlsafe_b64decode(paging["next_cursor"].encode()))
                paging["next_cursor"] = decoded["key"]
            collections[name] = {
                **collection,
                "page": seal_page(
                    snap,
                    page,
                    name,
                    paging,
                    filters,
                ),
            }
        data = {**data, "collections": collections}
    else:
        sections = [section] if section else list(data["collections"])
        collections = {
            name: _collection(
                model,
                snap,
                page,
                name,
                cursor=cursor if name == section else None,
                limit=limit,
                filters=filters,
                variant=variant,
                original_page=data["collections"].get(name, {}).get("page"),
            )
            for name in sections
        }
        data = {**data, "collections": collections}
    if page == "brief" and section is None:
        shown = data["collections"]["open_items"]["items"]
        data = {
            **data,
            "open_items": {
                **data["open_items"],
                "categories": [
                    {
                        **category,
                        "loaded_count": sum(
                            item["member_count"]
                            for item in shown
                            if item["category_key"] == category["key"]
                        ),
                    }
                    for category in data["open_items"]["categories"]
                ],
            },
        }
        data = {
            **data,
            "generated_at": datetime.now(UTC).isoformat(),
            "owner_review_request": dashboard._owner_review_request(snap)
            if dashboard._owner_review_request is not None and data["month_state"] == "open"
            else None,
        }
    return {
        **dashboard._response(snap, data),
        "schema_version": payload["schema_version"],
        **({"scope": section or "overview"} if page == "brief" else {}),
    }
