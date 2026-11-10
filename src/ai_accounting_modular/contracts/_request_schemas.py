"""Self-contained request views derived from the installed contracts, never validators."""

from copy import deepcopy
from functools import cache, lru_cache
from types import MappingProxyType
from urllib.parse import urlsplit

from ._errors import ContractError
from ._validation import schema_document

COMMAND_REQUESTS = MappingProxyType(
    {
        "accounting_schema": "SchemaRequest",
        "accounting_context": "ContextRequest",
        "accounting_objects": "ObjectsRequest",
        "accounting_apply": "ApplyRequest",
        "accounting_cancel_month": "MonthCancelRequest",
        "accounting_get": "BusinessGet",
        "accounting_find": "BusinessQuery",
        "accounting_review": "ReviewRequest",
        "accounting_remuneration": "RemunerationRequest",
        "accounting_payment_export": "PaymentExportRequest",
        "accounting_sources": "SourcesRequest",
        "accounting_documents": "DocumentsRequest",
        "accounting_period": "PeriodRequest",
        "accounting_backup": "BackupRequest",
        "accounting_jobs": "JobsRequest",
        "accounting_report": "ReportRequest",
        "accounting_report_export": "ReportExportRequest",
    }
)
_FACTS = "business-facts.schema.json"


@cache
def _document(name):
    return schema_document(name)


def _reference(document, reference):
    path, marker, name = reference.partition("#/$defs/")
    target = urlsplit(path).path.rsplit("/", 1)[-1] if path else document
    if not marker or not name or "/" in name or name not in _document(target)["$defs"]:
        raise ContractError("storage_corrupt", "公开输入合同引用无法解析。")
    return target, name


@cache
def _fact_branch(family, kind):
    """Keep the original complete/partial leaf, including all its field constraints."""
    visited = set()

    def find(document, node):
        if "$ref" in node:
            target, name = _reference(document, node["$ref"])
            if (target, name) in visited:
                return None
            visited.add((target, name))
            found = find(target, _document(target)["$defs"][name])
            if found is not None:
                return found
        if node.get("properties", {}).get("kind", {}).get("const") == kind:
            return document, node
        for branch in node.get("oneOf", ()):
            found = find(document, branch)
            if found is not None:
                return found
        return None

    found = find(_FACTS, _document(_FACTS)["$defs"][family])
    if found is None:
        raise ContractError("storage_corrupt", "已装配的业务类型缺少正式输入合同。")
    return found


def _bundle(document, node, *, kinds=None):
    definitions, pending = {}, []

    def rewrite(source, value):
        if isinstance(value, list):
            return [rewrite(source, child) for child in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, child in value.items():
            if key == "$id":
                continue
            if key != "$ref":
                result[key] = rewrite(source, child)
                continue
            target, name = _reference(source, child)
            local = target.removesuffix(".schema.json") + "__" + name
            if local not in definitions:
                definitions[local] = None
                pending.append((local, target, name))
            result[key] = "#/$defs/" + local
        return result

    result = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **rewrite(document, node),
    }
    while pending:
        local, source, name = pending.pop()
        if kinds is not None and source == _FACTS and name in {"Fact", "PartialFact"}:
            # Empty selection still permits notes, sources, objects and withdrawal.
            # known_fact shares the selection; it cannot bring the whole catalog
            # back into a command that otherwise selected only a few fact kinds.
            definitions[local] = (
                {"oneOf": [rewrite(*_fact_branch(name, kind)) for kind in kinds]}
                if kinds
                else False
            )
        else:
            definitions[local] = rewrite(source, _document(source)["$defs"][name])
    result["$defs"] = definitions
    return result


@lru_cache(maxsize=128)
def _command_schema(command, kinds):
    return _bundle(
        "core.schema.json",
        _document("core.schema.json")["$defs"][COMMAND_REQUESTS[command]],
        kinds=kinds,
    )


def command_request_schema(command, *, kinds=None):
    """Expose one command; Apply always requires an explicit fact selection."""
    if command not in COMMAND_REQUESTS:
        raise ContractError("unsupported_business", "当前实例没有此公开业务入口。")
    if (command == "accounting_apply") != (kinds is not None):
        raise ContractError(
            "invalid_input", "accounting_apply 合同须提供 kinds（可为空）；其他命令不能传 kinds。"
        )
    selected = tuple(sorted(set(kinds))) if kinds is not None else None
    return deepcopy(_command_schema(command, selected))


@cache
def _fact_schema(kind):
    return _bundle(*_fact_branch("Fact", kind))


def fact_input_schema(kind):
    return deepcopy(_fact_schema(kind))


def isolated_request_schema(action):
    result = _bundle(
        "core.schema.json", _document("core.schema.json")["$defs"]["IsolatedTargetRequest"]
    )
    result["properties"]["action"] = {"const": action}
    return result
