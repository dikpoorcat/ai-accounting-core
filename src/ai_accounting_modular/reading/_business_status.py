"""One business progress read, independent of the full monthly page preparation."""

import base64
import binascii
from datetime import UTC, datetime

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from . import _business_status_display as display
from . import _business_status_events as events
from . import _business_status_selection as selection
from . import _business_status_settlements as settlements
from ._business_descriptions import Descriptions
from ._context import period
from ._monthly import normalize
from ._types import ModelDelivery


def _query(scope, query):
    allowed = {
        "subject_id",
        "settlement_view",
        "section",
        "limit",
        "cursor",
        "expected_version",
        "detail_scope_key",
        "voucher_version_id",
    }
    if not isinstance(query, dict) or set(query) - allowed or "subject_id" not in query:
        raise ContractError("invalid_input", "业务进展须指定业务身份及支持的读取参数。")
    validate_contract("core.schema.json", "Id", query["subject_id"])
    if query.get("settlement_view", "current") not in {"current", "historical"}:
        raise ContractError("invalid_input", "业务进展收付视图须为历史或当前。")
    if query.get("section", "settlement_events") != "settlement_events":
        raise ContractError("invalid_input", "业务进展只支持实际清偿记录分页。")
    paired = [name in query for name in ("detail_scope_key", "voucher_version_id")]
    if any(paired) and not all(paired):
        raise ContractError("invalid_input", "精确分项与凭证定位必须一起提供。")
    if all(paired) and (
        not isinstance(query["detail_scope_key"], str)
        or not query["detail_scope_key"]
        or not isinstance(query["voucher_version_id"], str)
    ):
        raise ContractError("invalid_input", "精确分项及凭证定位格式不正确。")
    expected = query.get("expected_version", scope.get("expected_head"))
    if expected is not None:
        validate_contract("core.schema.json", "Digest", expected)
    if "cursor" in query and expected is None:
        raise ContractError("invalid_input", "业务进展续页须带原业务进展版本。")
    page = PageQuery(query.get("limit", 20), query.get("cursor"), expected)
    return page


def _closure(value):
    if value.state == "open":
        return {"state": "open"}
    if value.closing_period == value.period:
        return {"state": "exact_close", "digest": value.head}
    return {
        "state": "covered_by_later_close",
        "sealing_boundary": value.closing_period,
        "sealing_digest": value.head,
    }


def _page(rows, query, version, cutoff, current_cutoff):
    if query.expected_version is not None and query.expected_version != version:
        raise ContractError("snapshot_changed", "这笔业务的进展已经变化，请重新读取。")
    offset = 0
    if query.cursor is not None:
        try:
            value = loads_json(
                base64.b64decode(query.cursor, altchars=b"-_", validate=True).decode()
            )
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "offset"}
                or type(value["offset"]) is not int
                or not 0 <= value["offset"] <= len(rows)
            ):
                raise ValueError
            if value["version"] != version:
                raise ContractError("snapshot_changed", "业务进展分页不属于当前范围，请重新读取。")
            offset = value["offset"]
        except (ContractError, ValueError, TypeError, UnicodeError, binascii.Error) as error:
            if isinstance(error, ContractError) and error.code == "snapshot_changed":
                raise
            raise ContractError("invalid_input", "业务进展分页标识无效。") from error
    items = rows[offset : offset + query.limit]
    more = offset + len(items) < len(rows)
    cursor = (
        base64.urlsafe_b64encode(
            canonical_json({"version": version, "offset": offset + len(items)}).encode()
        ).decode()
        if more
        else None
    )
    return {
        "items": items,
        "page": {
            "total_count": len(rows),
            "filtered_count": len(rows),
            "returned_count": len(items),
            "has_more": more,
            "next_cursor": cursor,
            "collection_version": version,
        },
        "scope_period": cutoff,
        "current_cutoff_period": current_cutoff,
        "cutoff_semantics": "实际清偿所属月截至所选视图月末",
    }


def detail(reader, scope, query):
    """Return ready ModelDelivery; every owner query uses this same company read snapshot."""
    page = _query(scope, query)
    normalized = normalize(scope)
    with reader.storage.read_unit(scope["company_id"]) as read:
        if normalized["period_to"] < read.start_period:
            raise ContractError("invalid_input", "不能查看建账前缺少逐月正式资料的业务进展。")
        selected = selection.select(reader, normalized, query, read)
        positions = settlements.capture(reader, normalized, selected, read)
        prose = Descriptions(reader, display.description_references(selected, positions), read)
        view = query.get("settlement_view", "current")
        actual_events = events.capture(reader, selected, positions, view, read, descriptions=prose)
        closure = reader.ledger.period_closure(normalized["period_to"], read)
        old_closure = (
            reader.ledger.period_closure(selected.content.reference["posting_period"], read)
            if selected.content is not None and selected.content.reference["state"] == "frozen"
            else None
        )
        fact = selected.business.get("fact")
        current = selected.current
        latest_period = (
            fact["period"]
            if fact is not None
            else current.reference["business_period"]
            if current is not None
            else selected.content.reference["business_period"]
        )
        deleted = selected.business["state"]["state"] == "withdrawn"
        data = {
            "identity": {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "subject_id": query["subject_id"],
                "kind": display.business_kind(selected),
            },
            "period": normalized["period_to"],
            "as_of": datetime.now(UTC).date().isoformat(),
            "latest_source": {"period": latest_period, "deleted": deleted},
            "closure": _closure(closure),
            "current_business_result": display.result(current),
            "frozen_adoption": display.result(
                selected.content, frozen=True, closing_period=old_closure.closing_period
            )
            if old_closure is not None and old_closure.state == "closed"
            else None,
            "review": {
                "status": "withdrawn"
                if deleted
                else "current"
                if current is not None
                else "checking"
            },
            "settlements": positions.historical,
            "display_profiles": display.profiles(
                reader, selected, positions, actual_events, read, descriptions=prose
            ),
            "current_followups": {"settlements": positions.current},
            "settlement_view": view,
            "detail_scope": selected.detail,
        }
        version = content_digest(
            {
                "data": {key: value for key, value in data.items() if key != "as_of"},
                "events": actual_events,
            }
        )
        selected_cutoff = (
            positions.current["cutoff_period"]
            if view == "current"
            else positions.historical["cutoff_period"]
        )
        data["collections"] = {
            "settlement_events": _page(
                actual_events, page, version, selected_cutoff, positions.current["cutoff_period"]
            )
        }
        payload = {
            "selected_period": period(
                normalized["period_to"],
                closure.closing_period if closure.state == "closed" else None,
            ),
            "snapshot_version": version,
            "data": data,
            "company_id": read.company_id,
            "database_id": read.database_id,
        }
        return ModelDelivery("ready", normalized, version, payload)
