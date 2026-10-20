"""Expose only delivered fact inputs and their local schema dependency closure."""

from copy import deepcopy
from functools import lru_cache
from urllib.parse import urlsplit

from ai_accounting_modular.contracts.public import (
    CONTRACT,
    ContractError,
    schema_document,
    validate_contract,
)


@lru_cache(maxsize=1)
def _documents():
    return {
        name: schema_document(name) for name in ("core.schema.json", "business-facts.schema.json")
    }


def _reference(source, reference):
    document, marker, name = reference.partition("#/$defs/")
    if not marker or not name or "/" in name:
        raise ContractError("storage_corrupt", "输入合同含无法解析的本地字段引用。")
    target = urlsplit(document).path.rsplit("/", 1)[-1] if document else source
    if target not in _documents() or name not in _documents()[target]["$defs"]:
        raise ContractError("storage_corrupt", "输入合同缺少实际引用的字段定义。")
    return target, name


def _fact_input(kind):
    source = "business-facts.schema.json"
    visited = set()

    def find(document, node):
        if "$ref" in node:
            key = _reference(document, node["$ref"])
            if key in visited:
                return None
            visited.add(key)
            return find(key[0], _documents()[key[0]]["$defs"][key[1]])
        if node.get("properties", {}).get("kind", {}).get("const") == kind:
            return document, node
        for branch in node.get("oneOf", ()):
            found = find(document, branch)
            if found is not None:
                return found
        return None

    found = find(source, _documents()[source]["$defs"]["Fact"])
    if found is None:
        raise ContractError("storage_corrupt", "已装配的业务类型缺少正式输入合同。")
    return found


@lru_cache(maxsize=1000)
def _input_document(kind):
    source, node = _fact_input(kind)
    definitions, pending = {}, []

    def rewrite(document, value):
        if isinstance(value, list):
            return [rewrite(document, child) for child in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, child in value.items():
            if key == "$id":
                continue
            if key == "$ref":
                target, name = _reference(document, child)
                local = target.removesuffix(".schema.json") + "__" + name
                if local not in definitions:
                    definitions[local] = None
                    pending.append((local, target, name))
                result[key] = "#/$defs/" + local
            else:
                result[key] = rewrite(document, child)
        return result

    result = {"$schema": "https://json-schema.org/draft/2020-12/schema", **rewrite(source, node)}
    while pending:
        local, document, name = pending.pop()
        definitions[local] = rewrite(document, _documents()[document]["$defs"][name])
    result["$defs"] = definitions
    return result


def result(registry, request):
    validate_contract("core.schema.json", "SchemaRequest", request)
    supported = registry.supported_kinds
    wanted = tuple(dict.fromkeys(request.get("kinds", ())))
    unknown = sorted(set(wanted) - set(supported))
    if unknown:
        raise ContractError("unsupported_business", "当前实例尚未支持：" + "、".join(unknown))
    return validate_contract(
        "core.schema.json",
        "SchemaResult",
        {
            "contract": CONTRACT,
            "supported_kinds": list(supported),
            "schemas": [
                {"kind": kind, "schema_document": deepcopy(_input_document(kind))}
                for kind in wanted
            ],
        },
    )
