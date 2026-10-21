"""Current source metadata and bounded pages; files are observed only for returned items."""

import base64
import binascii
from dataclasses import asdict

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from . import _sources
from ._types import SourceStatus


def _result(records, sources, wanted, read):
    present = {item["source_id"]: item for item in sources}
    root = records._root(read)
    return {
        "company_id": read.company_id,
        "sources": list(sources),
        "missing_source_ids": sorted(set(wanted) - present.keys()),
        "source_statuses": [
            asdict(_sources.source_status(root, present[key]))
            if key in present
            else asdict(SourceStatus(key, "not_found", None))
            for key in wanted
        ],
    }


def _cursor(version, after):
    return base64.urlsafe_b64encode(
        canonical_json({"version": version, "after": after}).encode()
    ).decode()


def _after(cursor, version):
    if cursor is None:
        return None
    try:
        payload = loads_json(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or set(payload) != {"version", "after"}:
            raise ValueError
        validate_contract("core.schema.json", "Digest", payload["version"])
        after = payload["after"]
        if not isinstance(after, list) or len(after) != 2:
            raise ValueError
        if after[0] is not None:
            validate_contract("core.schema.json", "Month", after[0])
        validate_contract("core.schema.json", "Id", after[1])
    except (ValueError, TypeError, binascii.Error) as error:
        raise ContractError("invalid_input", "来源游标无效，请从第一页读取。") from error
    if payload["version"] != version:
        raise ContractError("snapshot_changed", "来源索引已改变，请从第一页读取。")
    return after


def query(records, request, read):
    validate_contract("core.schema.json", "SourcesRequest", request)
    if request["company_id"] != read.company_id:
        raise ContractError("invalid_input", "来源请求必须使用同一家公司的读取快照。")
    action = request["action"]
    if action == "get":
        ids = tuple(dict.fromkeys(request["source_ids"]))
        result = _result(records, records.resolve_sources(ids, read), ids, read)
    elif action == "find":
        first, last = request.get("period_from"), request.get("period_to")
        if first is not None and last is not None and first > last:
            raise ContractError("invalid_input", "来源起月不能晚于末月。")
        repository = read.repository("records")
        metadata = repository.source_query_metadata(first, last)
        version = content_digest(
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "period_from": first,
                "period_to": last,
                "metadata": [list(row) for row in metadata],
            }
        )
        after = _after(request.get("cursor"), version)
        ids = repository.source_query_ids(first, last, after, request["limit"] + 1)
        selected = ids[: request["limit"]]
        sources = records.resolve_sources(selected, read)
        by_id = {source["source_id"]: source for source in sources}
        sources = tuple(by_id[source_id] for source_id in selected)
        more = len(ids) > request["limit"]
        result = _result(records, sources, selected, read)
        result["page"] = {
            "total_count": len(metadata),
            "filtered_count": len(metadata),
            "returned_count": len(sources),
            "has_more": more,
            "next_cursor": _cursor(
                version, [sources[-1].get("archive_month"), sources[-1]["source_id"]]
            )
            if more
            else None,
            "collection_version": version,
        }
    elif action == "inspect":
        from ._source_inspection import inspect_source

        ids = (request["source_id"],)
        sources = records.resolve_sources(ids, read)
        result = _result(records, sources, ids, read)
        if sources:
            result["inspection"] = inspect_source(records, sources[0], request, read)
    else:
        raise ContractError("invalid_operation", "import须先准备来源并经公开资料计划原子登记。")
    return validate_contract("core.schema.json", "SourcesResult", result)
