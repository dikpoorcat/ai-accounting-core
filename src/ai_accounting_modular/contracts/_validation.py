"""Validate only installed, local schemas; never retrieve a schema over a network."""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker, ValidationError, validators
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from ._dispatch import install_guards, one_of
from ._errors import ContractError
from ._scalars import MAX_FEN, MIN_FEN, parse_fen
from ._wire_types import JsonValue

CONTRACT = "ai-accounting-modular/1"
_BASE = "https://ai-accounting.local/design/"
_SCHEMAS = (
    "core.schema.json",
    "business-facts.schema.json",
    "reporting.schema.json",
    "review-materials.schema.json",
    "dashboard-response-v2.json",
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 对象包含重复字段")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("JSON 不接受 NaN 或 Infinity")


def loads_json(raw: str | bytes) -> JsonValue:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            raise ValueError("JSON 必须是 UTF-8 文本")
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        _check_json(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ContractError("invalid_input", "JSON 格式、字符或字段不符合约定") from error


def _check_json(value, *, allow_float=True, ancestors=None):
    if value is None or type(value) in {bool, int}:
        return
    if isinstance(value, str):
        value.encode("utf-8")
        return
    if type(value) is float:
        if allow_float and math.isfinite(value):
            return
        raise ValueError("不接受浮点业务金额或非有限数值")
    if type(value) not in {dict, list}:
        raise ValueError("不是 JSON 数据类型")
    ancestors = set() if ancestors is None else ancestors
    identity = id(value)
    if identity in ancestors:
        raise ValueError("JSON 不允许循环引用")
    ancestors.add(identity)
    try:
        if isinstance(value, dict):
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("JSON 字段名必须是字符串")
                key.encode("utf-8")
                _check_json(item, allow_float=allow_float, ancestors=ancestors)
        else:
            for item in value:
                _check_json(item, allow_float=allow_float, ancestors=ancestors)
    finally:
        ancestors.remove(identity)


def canonical_json(value: JsonValue) -> str:
    """Sort object keys only; business-significant array order is preserved."""
    _check_json(value, allow_float=False)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def content_digest(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _integer_bound(validator, bound, instance, schema):
    if isinstance(instance, str):
        try:
            value = parse_fen(instance)
        except ValueError:
            yield ValidationError("整数分必须使用规范 int64 字符串")
            return
        minimum = int(schema.get("x-integer-minimum", MIN_FEN))
        maximum = int(schema.get("x-integer-maximum", MAX_FEN))
        if not minimum <= value <= maximum:
            yield ValidationError("整数分超出合同范围")


def _wire_fen(validator, enabled, instance, schema):
    if enabled and isinstance(instance, str):
        try:
            parse_fen(instance)
        except ValueError:
            yield ValidationError("整数分必须使用规范 int64 字符串")


def _exact_pattern(validator, pattern, instance, schema):
    if isinstance(instance, str):
        # Anchored contract patterns specify the complete scalar. fullmatch
        # avoids Python's special `$` match immediately before a final newline.
        match = (
            re.fullmatch(pattern, instance)
            if pattern.startswith("^") and pattern.endswith("$")
            else re.search(pattern, instance)
        )
        if match is None:
            yield ValidationError("字符串格式不符合合同")


def _strict_const(validator, expected, instance, schema):
    if type(expected) in {int, bool} and type(instance) is not type(expected):
        yield ValidationError("固定值的类型不符合合同")
        return
    yield from Draft202012Validator.VALIDATORS["const"](validator, expected, instance, schema)


_Validator = validators.extend(
    Draft202012Validator,
    {
        "pattern": _exact_pattern,
        "const": _strict_const,
        "x-integer-minimum": _integer_bound,
        "x-integer-maximum": _integer_bound,
        "x-fen-int64": _wire_fen,
        "oneOf": one_of,
    },
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int
    ),
)


@lru_cache(maxsize=1)
def _bundle():
    documents, roots = {}, []
    registry = Registry()
    root = files("ai_accounting_modular.contracts").joinpath("_schemas")
    for name in _SCHEMAS:
        document = loads_json(root.joinpath(name).read_bytes())
        documents[name] = document
        if name == "dashboard-response-v2.json":
            for namespace, schema in document.items():
                uri = _BASE + "dashboard/" + namespace
                registry = registry.with_resource(
                    uri,
                    Resource.from_contents(schema, default_specification=DRAFT202012),
                )
                roots.append((schema, uri))
        else:
            resource = Resource.from_contents(document, default_specification=DRAFT202012)
            for uri in {_BASE + name, document["$id"]}:
                registry = registry.with_resource(uri, resource)
            roots.append((document, document["$id"]))
    install_guards((schema, registry.resolver(base_uri=uri)) for schema, uri in roots)
    return documents, registry


def _selected_document(schema_name: str, definition: str | None):
    documents, _ = _bundle()
    if schema_name not in documents:
        raise ContractError("invalid_contract", "未支持的合同名称")
    document = documents[schema_name]
    if schema_name == "dashboard-response-v2.json":
        if definition is None or definition not in document:
            raise ContractError("invalid_contract", "必须指定有效的看板合同名称")
        return document[definition]
    if definition is not None and definition not in document.get("$defs", {}):
        raise ContractError("invalid_contract", "未支持的合同类型")
    return document


def schema_document(schema_name: str, definition: str | None = None) -> dict:
    """Copy an installed source schema, with its declared external references."""
    result = deepcopy(_selected_document(schema_name, definition))
    if definition is not None:
        if schema_name != "dashboard-response-v2.json":
            result["$ref"] = "#/$defs/" + definition.replace("~", "~0").replace("/", "~1")
    return result


@lru_cache(maxsize=256)
def _validator(schema_name: str, definition: str):
    documents, registry = _bundle()
    _selected_document(schema_name, definition)
    if schema_name == "dashboard-response-v2.json":
        uri = _BASE + "dashboard/" + definition
    else:
        uri = (
            documents[schema_name]["$id"]
            + "#/$defs/"
            + definition.replace("~", "~0").replace("/", "~1")
        )
    return _Validator({"$ref": uri}, registry=registry, format_checker=FormatChecker())


def validate_contract[T](schema_name: str, definition: str, value: T) -> T:
    try:
        _check_json(value)
        error = next(_validator(schema_name, definition).iter_errors(value), None)
    except ContractError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ContractError("invalid_input", "数据包含无效 JSON 类型或字符") from error
    if error is not None:
        pointer = "/" + "/".join(
            str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path
        )
        raise ContractError("invalid_input", f"字段 {pointer} 不符合 {definition} 合同") from error
    return value
