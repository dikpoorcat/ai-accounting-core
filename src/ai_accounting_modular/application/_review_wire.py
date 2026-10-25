"""Bounded review pages keep their financial scope and exact source identities."""

import base64
import binascii
from collections import defaultdict
from dataclasses import asdict, is_dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    format_fen,
    loads_json,
    validate_contract,
)


def wire(value, key=""):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {name: wire(item, name) for name, item in value.items()}
    if isinstance(value, tuple | list):
        return [wire(item) for item in value]
    return format_fen(value) if key.endswith("_fen") and type(value) is int else value


def fund(definition):
    return {
        "reference": definition.reference,
        "source": definition.source,
        "movement": definition.value,
    }


def item(collection, value, relationship="direct"):
    if collection == "obligations":
        result = {
            "reference": value.definition.reference,
            "source": value.definition.source,
            "obligation": value.definition.value,
            "amounts": wire(value.amounts),
        }
    elif collection == "settlements":
        result = {
            "use": wire(value.use),
            "movement": fund(value.movement) if value.movement else None,
        }
    else:
        result = wire(value)
    return result if collection == "links" else {"relationship": relationship, **result}


def continuation(request, version):
    if request.get("expected_version", version) != version:
        raise ContractError("snapshot_changed", "本事项的核查依据已变化，请重新读取受影响范围。")
    token = request.get("cursor")
    if token is None:
        return None
    try:
        saved = loads_json(base64.b64decode(token, altchars=b"-_", validate=True))
        if not isinstance(saved, dict) or set(saved) != {"version", "collection", "cursor"}:
            raise ValueError
        validate_contract("core.schema.json", "Digest", saved["version"])
        if not isinstance(saved["cursor"], str) or not saved["cursor"]:
            raise ValueError
    except (ContractError, ValueError, TypeError, binascii.Error) as error:
        raise ContractError("invalid_input", "核查分页游标无效，请重新读取。") from error
    if saved["collection"] != request["collection"]:
        raise ContractError("invalid_input", "核查游标只可接续原来的集合。")
    if saved["version"] != version:
        raise ContractError("snapshot_changed", "本事项的核查依据已变化，不能混用旧页面。")
    return saved["cursor"]


def cursor(version, collection, following):
    if following is None:
        return None
    return base64.urlsafe_b64encode(
        canonical_json({"version": version, "collection": collection, "cursor": following}).encode()
    ).decode()


def chunks(values):
    values = tuple(values)
    for start in range(0, len(values), 500):
        yield values[start : start + 500]


def _references(value, sources, object_ids, locations):
    """Only explicitly typed row fields contribute metadata; never expand a graph."""
    if isinstance(value, dict):
        if {"result_id", "result_generation", "business_id"} <= value.keys():
            sources[canonical_json(value)] = value
        if "source_id" in value:
            locations[canonical_json(value)] = value
        for name, child in value.items():
            if name in {"party_id", "account_id", "funds_account_id", "counterparty_id"} and child:
                object_ids.add(child)
            _references(child, sources, object_ids, locations)
    elif isinstance(value, list):
        for child in value:
            _references(child, sources, object_ids, locations)


def metadata(application, items, read):
    references, object_ids, locations = {}, set(), {}
    for row in items:
        _references(row, references, object_ids, locations)
    origins = {}
    for batch in chunks(references.values()):
        for entry in application.ledger.get_review_sources(batch, read):
            origins[canonical_json(entry.source)] = entry.reference
            for location in entry.source_locations:
                locations[canonical_json(location)] = location
    for row in items:
        source = row.get("source") or row.get("use", {}).get("source")
        if source is not None:
            row["origin"] = origins[canonical_json(source)]
        if "use" in row and row["movement"] is not None:
            row["movement"]["origin"] = origins[canonical_json(row["movement"]["source"])]
    names = []
    for batch in chunks(sorted(object_ids)):
        names.extend(
            {key: obj[key] for key in ("object_id", "kind", "name")}
            for obj in application.identity.get_batch(batch, read)
        )
    by_source = defaultdict(set)
    for location in locations.values():
        by_source[location["source_id"]].update(
            [location["location"]] if "location" in location else []
        )
    sources = []
    for batch in chunks(sorted(by_source)):
        resolved = application.records.resolve_sources(batch, read)
        if {source["source_id"] for source in resolved} != set(batch):
            raise ContractError("storage_corrupt", "核查结果引用的必要来源缺失。")
        sources.extend(
            {
                "source_id": source["source_id"],
                "kind": source["kind"],
                "name": source.get("original_name", "负责人说明"),
                "locations": sorted(by_source[source["source_id"]]),
            }
            for source in resolved
        )
    return names, sources
