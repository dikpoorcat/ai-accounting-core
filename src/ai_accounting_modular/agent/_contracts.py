"""Only reachable request definitions, bundled from the shipped contract truth."""

from copy import deepcopy
from functools import lru_cache
from urllib.parse import urlsplit

from ai_accounting_modular.contracts.public import ContractError, schema_document

REQUESTS = {
    "accounting_schema": "SchemaRequest",
    "accounting_context": "ContextRequest",
    "accounting_objects": "ObjectsRequest",
    "accounting_apply": "ApplyRequest",
    "accounting_get": "BusinessGet",
    "accounting_find": "BusinessQuery",
    "accounting_sources": "SourcesRequest",
    "accounting_documents": "DocumentsRequest",
    "accounting_period": "PeriodRequest",
    "accounting_backup": "BackupRequest",
    "accounting_jobs": "JobsRequest",
    "accounting_report": "ReportRequest",
}


@lru_cache(maxsize=len(REQUESTS) + 1)
def _bundle(name):
    documents = {
        key: schema_document(key)
        for key in (
            "core.schema.json",
            "business-facts.schema.json",
        )
    }
    definitions, pending = {}, []

    def rewrite(document, node):
        if isinstance(node, list):
            return [rewrite(document, value) for value in node]
        if not isinstance(node, dict):
            return node
        result = {}
        for key, value in node.items():
            if key == "$id":
                continue
            if key != "$ref":
                result[key] = rewrite(document, value)
                continue
            path, marker, definition = value.partition("#/$defs/")
            target = urlsplit(path).path.rsplit("/", 1)[-1] if path else document
            if (
                not marker
                or target not in documents
                or definition not in documents[target]["$defs"]
            ):
                raise ContractError("storage_corrupt", "公开输入合同引用无法解析。")
            local = target.removesuffix(".schema.json") + "__" + definition
            if local not in definitions:
                definitions[local] = None
                pending.append((local, target, definition))
            result[key] = "#/$defs/" + local
        return result

    result = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **rewrite("core.schema.json", documents["core.schema.json"]["$defs"][name]),
    }
    while pending:
        local, document, definition = pending.pop()
        definitions[local] = rewrite(document, documents[document]["$defs"][definition])
    result["$defs"] = definitions
    return result


def request_schema(command):
    return deepcopy(_bundle(REQUESTS[command]))


def isolated_schema(action):
    result = deepcopy(_bundle("IsolatedTargetRequest"))
    result["properties"]["action"] = {"const": action}
    return result
