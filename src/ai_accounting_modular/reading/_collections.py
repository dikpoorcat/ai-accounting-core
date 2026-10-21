"""Prepare complete projection indexes; decode only the requested collection page."""

import base64
from dataclasses import asdict, dataclass
from hashlib import sha256

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    ReadPage,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)


@dataclass(frozen=True)
class IndexItem:
    collection: str
    item_id: str
    sort_key: str
    value: dict
    business_id: str | None = None
    object_id: str | None = None
    group_key: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class PreparedItems:
    rows: tuple[tuple, ...]
    manifest: tuple[tuple[str, int, str], ...]

    def with_summary(self, summary):
        if "_collections" in summary:
            raise ContractError("invalid_input", "读取汇总不能覆盖集合目录。")
        return dict(summary) | {
            "_collections": {
                key: {"count": count, "digest": digest} for key, count, digest in self.manifest
            }
        }


@dataclass(frozen=True)
class PreparedModel:
    payload_json: str
    content_digest: str
    rows: tuple[tuple, ...]


def prepare_model(summary, *, items=None):
    """Prepare immutable publication bytes before taking the maintenance writer."""
    if items is not None and not isinstance(items, PreparedItems):
        raise TypeError("items must be prepared before publication")
    payload = items.with_summary(summary) if items is not None else summary
    raw = canonical_json(payload)
    return PreparedModel(
        raw, sha256(raw.encode("utf-8")).hexdigest(), items.rows if items is not None else ()
    )


@dataclass(frozen=True)
class CollectionFilter:
    business_id: str | None = None
    object_id: str | None = None
    group_key: str | None = None
    categories: tuple[str, ...] = ()
    item_id: str | None = None


def wire_page(page):
    return {
        "items": list(page.items),
        "page": {
            "total_count": page.total_count,
            "filtered_count": page.filtered_count,
            "returned_count": len(page.items),
            "has_more": page.next_cursor is not None,
            "next_cursor": page.next_cursor,
            "collection_version": page.version,
        },
    }


_INDEX_FIELDS = (
    "collection",
    "item_id",
    "sort_key",
    "business_id",
    "object_id",
    "group_key",
    "category",
)


def _text(value):
    return isinstance(value, str) and bool(value)


def prepare_items(items, *, collections=()):
    """All serialization and hashes precede the short publication transaction."""
    grouped = {}
    for key in collections:
        if not _text(key) or key in grouped:
            raise ContractError("invalid_input", "读取集合名称无效或重复。")
        grouped[key] = []
    rows, identities = [], set()
    for item in items:
        if not isinstance(item, IndexItem) or not isinstance(item.value, dict):
            raise ContractError("invalid_input", "读取索引须提供明确项目和展示内容。")
        index = {key: getattr(item, key) for key in _INDEX_FIELDS}
        if any(not _text(index[key]) for key in ("collection", "item_id", "sort_key")) or any(
            index[key] is not None and not _text(index[key])
            for key in ("business_id", "object_id", "group_key", "category")
        ):
            raise ContractError("invalid_input", "读取项目的集合、身份或排序字段无效。")
        for key in ("business_id", "object_id"):
            if index[key] is not None:
                validate_contract("core.schema.json", "Id", index[key])
        identity = item.collection, item.item_id
        if identity in identities:
            raise ContractError("invalid_input", "同一读取集合不能重复保存项目身份。")
        identities.add(identity)
        digest = content_digest({"index": index, "value": item.value})
        grouped.setdefault(item.collection, []).append((item.sort_key, item.item_id, digest))
        rows.append((*index.values(), canonical_json({"digest": digest, "value": item.value})))
    manifest = tuple(
        (key, len(values), content_digest([list(value) for value in sorted(values)]))
        for key, values in sorted(grouped.items())
    )
    return PreparedItems(tuple(rows), manifest)


def _filters(query):
    if not isinstance(query, CollectionFilter):
        raise ContractError("invalid_input", "读取集合须提供明确筛选条件。")
    for key in ("business_id", "object_id"):
        value = getattr(query, key)
        if value is not None:
            validate_contract("core.schema.json", "Id", value)
    if (
        any(value is not None and not _text(value) for value in (query.group_key, query.item_id))
        or not isinstance(query.categories, tuple)
        or any(not _text(x) for x in query.categories)
    ):
        raise ContractError("invalid_input", "读取集合的分组、项目或类别筛选无效。")
    if len(set(query.categories)) != len(query.categories):
        raise ContractError("invalid_input", "读取类别筛选不能重复。")


def _after(page, version):
    if not isinstance(page, PageQuery):
        raise ContractError("invalid_input", "读取集合须提供有界分页条件。")
    if page.expected_version is not None and page.expected_version != version:
        raise ContractError("snapshot_changed", "读取集合已经变化，请重新读取首页。")
    if page.cursor is None:
        return None
    try:
        value = loads_json(base64.b64decode(page.cursor, altchars=b"-_", validate=True).decode())
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "key"}
            or not isinstance(value["key"], list)
            or len(value["key"]) != 2
            or not all(isinstance(x, str) for x in value["key"])
        ):
            raise ValueError
    except (TypeError, ValueError, UnicodeError) as error:
        raise ContractError("invalid_input", "读取集合的分页标识无效。") from error
    if value["version"] != version:
        raise ContractError("snapshot_changed", "读取集合已经变化，请重新读取首页。")
    return tuple(value["key"])


def _decode(row):
    try:
        saved = loads_json(row["payload_json"])
        index = {
            key: row["collection_key" if key == "collection" else key] for key in _INDEX_FIELDS
        }
        if (
            not isinstance(saved, dict)
            or set(saved) != {"digest", "value"}
            or not isinstance(saved["value"], dict)
            or content_digest({"index": index, "value": saved["value"]}) != saved["digest"]
        ):
            raise ValueError
        return saved["value"]
    except (ContractError, KeyError, ValueError, TypeError) as error:
        raise ContractError(
            "storage_corrupt", "读取集合项目的内容或索引不完整，请重新生成。"
        ) from error


def collection_page(scope_key, component, collection, query, page, read):
    _filters(query)
    repository = read.repository("reading")
    state = repository.state(scope_key, component)
    if state is None or state["state"] != "ready":
        raise ContractError("read_model_not_ready", "所选读取集合尚未就绪。")
    payload = repository.payload(scope_key, component)
    if (
        payload is None
        or payload["head"] != state["ready_head"]
        or state["ready_head"] != state["wanted_head"]
    ):
        raise ContractError("storage_corrupt", "读取集合与已就绪范围不一致。")
    try:
        summary = loads_json(payload["payload_json"])
        if not isinstance(summary, dict) or content_digest(summary) != state["content_digest"]:
            raise ValueError
        manifests = summary.get("_collections")
        if not isinstance(manifests, dict):
            raise ValueError
    except (ContractError, ValueError, TypeError) as error:
        raise ContractError("storage_corrupt", "读取集合目录的内容摘要不一致。") from error
    manifest = manifests.get(collection)
    if not isinstance(manifest, dict) or set(manifest) != {"count", "digest"}:
        raise ContractError("invalid_input", "所选模型不提供这个读取集合。")
    filters = asdict(query)
    filters["categories"] = list(query.categories)
    version = content_digest(
        {
            "company": read.company_id,
            "database": read.database_id,
            "scope": scope_key,
            "component": component,
            "collection": collection,
            "digest": manifest["digest"],
            "filter": filters,
        }
    )
    after = _after(page, version)
    total, filtered = repository.item_counts(scope_key, component, collection, query)
    if type(manifest["count"]) is not int or total != manifest["count"]:
        raise ContractError("storage_corrupt", "读取集合项目数量与完整目录不一致。")
    rows = repository.item_page(scope_key, component, collection, query, after, page.limit + 1)
    more, rows = len(rows) > page.limit, rows[: page.limit]
    if any(row["head"] != state["ready_head"] for row in rows):
        raise ContractError("storage_corrupt", "读取集合中混入了其他版本的项目。")
    next_cursor = None
    if more:
        next_cursor = base64.urlsafe_b64encode(
            canonical_json(
                {"version": version, "key": [rows[-1]["sort_key"], rows[-1]["item_id"]]}
            ).encode()
        ).decode()
    return ReadPage(version, total, filtered, tuple(_decode(row) for row in rows), next_cursor)
