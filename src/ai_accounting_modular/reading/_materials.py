"""Current facts and exact adopted results determine the only allowed sources."""

import base64
import binascii
import hashlib
from collections import defaultdict
from contextlib import ExitStack, contextmanager

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    fact_source_paths,
    loads_json,
    validate_contract,
)

from ._types import SourceDownload


def _batches(values):
    for start in range(0, len(values), 500):
        yield values[start : start + 500]


def _locator(fact, pointer):
    value = fact
    parts = pointer.split("/")[1:-1]
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value.get("location") if isinstance(value, dict) else None


def select(reader, query, read):
    request = {
        "company_id": read.company_id,
        "business_ids": [query["business_id"]],
        "include": ["facts", "results"],
    }
    exact = "result_id" in query
    if exact:
        request.update({key: query[key] for key in ("result_id", "generation")})
    try:
        result = reader.businesses.get_businesses(request, read)
    except ContractError as error:
        if exact and error.code in {"conflict", "snapshot_changed"}:
            raise ContractError(
                "snapshot_changed", "指定的开放凭证已经变化，请重新读取。"
            ) from error
        if error.code in {"not_found", "invalid_input"}:
            raise ContractError(
                "review_materials_not_found", "当前公司没有指定业务或采用结果。"
            ) from error
        raise
    if not result["businesses"]:
        raise ContractError("review_materials_not_found", "当前公司没有指定业务。")
    if (result["company_id"], result["database_id"]) != (read.company_id, read.database_id):
        raise ContractError("storage_corrupt", "业务读取结果不属于本次公司范围。")
    business = result["businesses"][0]
    if business["state"]["business_id"] != query["business_id"]:
        raise ContractError("storage_corrupt", "业务读取结果不属于指定身份。")
    locations = defaultdict(set)
    views = business["results"]
    if any("basis" not in view for view in views):
        raise ContractError("storage_corrupt", "来源查阅所需的正式采用依据缺失。")
    if exact:
        if len(views) != 1:
            raise ContractError("storage_corrupt", "精确来源查阅没有唯一的正式结果。")
        reference = views[0]["reference"]
        selection = {
            "mode": "frozen" if reference["state"] == "frozen" else "open_result",
            **{key: reference[key] for key in ("result_id", "generation", "posting_period")},
        }
        month = reference["business_period"]
        dependency = {"reference": reference, "basis": views[0]["basis"]}
    else:
        fact = business.get("fact")
        if fact is None:
            raise ContractError("review_materials_not_found", "该业务当前没有可查阅的有效事实。")
        selection = {"mode": "current", "fact_seq": business["state"]["seq"]}
        month = fact["period"]
        dependency = {
            "state": business["state"],
            "fact": fact,
            "results": [{"reference": v["reference"], "basis": v["basis"]} for v in views],
        }
        for source_id, pointers in fact_source_paths(fact).items():
            locations[source_id]
            for pointer in pointers:
                location = _locator(fact, pointer)
                if location:
                    locations[source_id].add(location)
    for view in views:
        for source in view["basis"]["source_locations"]:
            locations[source["source_id"]]
            if source.get("location"):
                locations[source["source_id"]].add(source["location"])
    ids = sorted(locations)
    sources = {}
    for batch in _batches(ids):
        sources.update((s["source_id"], s) for s in reader.records.resolve_sources(batch, read))
    if set(sources) != set(ids):
        raise ContractError("storage_corrupt", "正式采用引用的必要来源索引缺失。")
    # Moving the same registered bytes does not change this business selection.
    metadata = [
        {key: value for key, value in sources[source_id].items() if key != "relative_path"}
        for source_id in ids
    ]
    version = content_digest(
        {
            "company": read.company_id,
            "database": read.database_id,
            "business": query["business_id"],
            "selection": selection,
            "dependency": dependency,
            "sources": metadata,
        }
    )
    return {
        "company_id": read.company_id,
        "database_id": read.database_id,
        "business_id": query["business_id"],
        "business_label": f"{month}业务资料",
        "selection": selection,
        "snapshot_version": version,
        "sources": sources,
        "locations": locations,
        "ids": ids,
    }


def _after(query, version):
    if query.get("expected_version", version) != version:
        raise ContractError("snapshot_changed", "所选业务资料已变化，请重新读取。")
    if "cursor" not in query:
        return None
    try:
        value = loads_json(base64.b64decode(query["cursor"], altchars=b"-_", validate=True))
        if not isinstance(value, dict) or set(value) != {"version", "after"}:
            raise ValueError
        if value["version"] != version:
            raise ContractError("snapshot_changed", "资料分页标识不属于当前选择。")
        validate_contract("core.schema.json", "Id", value["after"])
        return value["after"]
    except (ContractError, ValueError, TypeError, binascii.Error) as error:
        if isinstance(error, ContractError) and error.code == "snapshot_changed":
            raise
        raise ContractError("invalid_input", "资料分页游标无效或不属于当前范围。") from error


def page(reader, query, read):
    validate_contract("review-materials.schema.json", "ReviewMaterialsQuery", query)
    selected = select(reader, query, read)
    version, ids = selected["snapshot_version"], selected["ids"]
    after = _after(query, version)
    remaining = [source_id for source_id in ids if after is None or source_id > after]
    current = remaining[: query.get("limit", 20)]
    statuses = {}
    for batch in _batches(current):
        statuses.update((s.source_id, s) for s in reader.records.source_status(batch, read))
    items = []
    for source_id in current:
        source = selected["sources"][source_id]
        status = statuses.get(source_id)
        if status is None or status.state == "not_found":
            raise ContractError("storage_corrupt", "已登记来源缺少受控位置检查结果。")
        is_text = source["kind"] == "owner_statement"
        available = status.state == "present"
        items.append(
            {
                "source_id": source_id,
                "kind": source["kind"],
                "label": "老板补充说明" if is_text else source["original_name"],
                "sha256": source["sha256"],
                "byte_size": len(source["text"].encode("utf-8"))
                if is_text
                else source["byte_size"],
                "media_type": "text/plain; charset=utf-8" if is_text else source["media_type"],
                "archive_month": source.get("archive_month"),
                "locations": sorted(selected["locations"][source_id]),
                "available": available,
                "unavailable_reason": None if available else status.state,
            }
        )
    more = len(remaining) > len(current)
    cursor = (
        base64.urlsafe_b64encode(
            canonical_json({"version": version, "after": current[-1]}).encode()
        ).decode()
        if more
        else None
    )
    response = {
        key: selected[key]
        for key in (
            "company_id",
            "database_id",
            "business_id",
            "business_label",
            "selection",
            "snapshot_version",
        )
    }
    response.update(
        schema_version=1,
        sources=items,
        page={
            "total_count": len(ids),
            "filtered_count": len(ids),
            "returned_count": len(items),
            "has_more": more,
            "next_cursor": cursor,
            "collection_version": version,
        },
    )
    return validate_contract("review-materials.schema.json", "ReviewMaterialsResponse", response)


@contextmanager
def content(reader, query):
    validate_contract("review-materials.schema.json", "SourceContentQuery", query)
    with ExitStack() as lifetime:
        with reader.storage.read_unit(query["company_id"]) as read:
            try:
                selected = select(reader, query, read)
            except ContractError as error:
                if error.code == "review_materials_not_found":
                    raise ContractError(
                        "review_source_not_found", "所选业务没有引用这份来源。"
                    ) from error
                raise
            if query["source_id"] not in selected["sources"]:
                raise ContractError("review_source_not_found", "所选业务没有引用这份来源。")
            _after(query, selected["snapshot_version"])
            source = selected["sources"][query["source_id"]]
            try:
                lease = lifetime.enter_context(
                    reader.records.lease_source(query["source_id"], read)
                )
            except ContractError as error:
                code = (
                    "source_content_mismatch"
                    if error.code == "source_corrupt"
                    else "source_unavailable"
                )
                if error.code not in {
                    "source_corrupt",
                    "source_missing",
                    "invalid_path",
                    "not_found",
                    "source_unavailable",
                }:
                    raise
                raise ContractError(code, "原件缺失或内容不符，请由会计AI补回正确资料。") from error
        # Records detached and verified the bytes. A fresh final selection is
        # needed: an old SQLite snapshot cannot observe a concurrent correction.
        with reader.storage.read_unit(query["company_id"]) as read:
            try:
                latest = select(reader, query, read)
            except ContractError as error:
                if error.code == "review_materials_not_found":
                    raise ContractError(
                        "snapshot_changed", "读取原件期间业务已变化，请重新读取。"
                    ) from error
                raise
            if latest["snapshot_version"] != selected["snapshot_version"]:
                raise ContractError("snapshot_changed", "读取原件期间业务资料已变化，请重新读取。")
        if source["kind"] == "owner_statement":
            raw = lease.read()
            if hashlib.sha256(raw).hexdigest() != source["sha256"]:
                raise ContractError("source_content_mismatch", "说明正文与来源摘要不符。")
            value = {
                "schema_version": 1,
                "company_id": selected["company_id"],
                "database_id": selected["database_id"],
                "source_id": source["source_id"],
                "kind": "owner_statement",
                "sha256": source["sha256"],
                "text": raw.decode("utf-8"),
            }
            yield validate_contract("review-materials.schema.json", "SourceTextContent", value)
        else:
            receipt = {
                "company_id": selected["company_id"],
                "database_id": selected["database_id"],
                "source_id": source["source_id"],
                "sha256": source["sha256"],
                "byte_size": source["byte_size"],
                "file_name": source["original_name"],
                "content_type": "application/octet-stream",
                "disposition": "attachment",
            }
            validate_contract("review-materials.schema.json", "SourceFileReceipt", receipt)
            yield SourceDownload(receipt, lease)
