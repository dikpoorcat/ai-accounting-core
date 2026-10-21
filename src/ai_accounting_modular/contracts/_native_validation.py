"""Compile installed schemas once, retaining the existing Python scalar semantics."""

import re

import jsonschema_rs
from jsonschema import FormatChecker
from jsonschema.exceptions import FormatError

from ._dispatch import _CHILDREN, _LISTS, _MAPS
from ._validation_keywords import integer_bound, wire_fen


class _StrictInteger:
    def __init__(self, parent, value, path):
        pass

    def validate(self, instance):
        # Native integer already excludes bool. An allowed boolean union remains
        # valid; an integral Python float must not enter its integer branch.
        if type(instance) is float:
            raise ValueError("整数类型不接受浮点数")


class _StrictConstant:
    def __init__(self, parent, value, path):
        self.expected_type = type(value)

    def validate(self, instance):
        if type(instance) is not self.expected_type:
            raise ValueError("固定值的类型不符合合同")


class _PythonPattern:
    def __init__(self, parent, value, path):
        pattern = re.compile(value)
        self.match = (
            pattern.fullmatch if value.startswith("^") and value.endswith("$") else pattern.search
        )

    def validate(self, instance):
        if isinstance(instance, str) and self.match(instance) is None:
            raise ValueError("字符串格式不符合合同")


def _keyword(handler):
    class ScalarKeyword:
        def __init__(self, parent, value, path):
            self.parent, self.value = parent, value

        def validate(self, instance):
            error = next(handler(None, self.value, instance, self.parent), None)
            if error is not None:
                raise ValueError(error.message)

    return ScalarKeyword


_KEYWORDS = {
    "x-python-integer": _StrictInteger,
    "x-python-const": _StrictConstant,
    "x-python-pattern": _PythonPattern,
    "x-integer-minimum": _keyword(integer_bound),
    "x-integer-maximum": _keyword(integer_bound),
    "x-fen-int64": _keyword(wire_fen),
}


def _format_checker(check, raises):
    def conforms(instance):
        # Match FormatChecker's two exception boundaries without its repeated
        # name lookup or constructing a FormatError for an ordinary mismatch.
        try:
            try:
                result = check(instance)
            except raises:
                return False
            return bool(result)
        except FormatError:
            return False

    return conforms


_FORMATS = {
    name: _format_checker(check, raises)
    for name, (check, raises) in FormatChecker().checkers.items()
}


def compile_schema(node):
    """Annotate schema nodes only, without rewriting business data or public schemas."""
    if not isinstance(node, dict):
        return node
    result = dict(node)
    for key in _MAPS:
        if key in node:
            result[key] = {name: compile_schema(value) for name, value in node[key].items()}
    for key in _LISTS:
        if key in node:
            result[key] = [compile_schema(value) for value in node[key]]
    for key in _CHILDREN:
        if key in node:
            result[key] = compile_schema(node[key])
    types = node.get("type", ())
    types = (types,) if isinstance(types, str) else types
    if "integer" in types and "number" not in types:
        result["x-python-integer"] = True
    if type(node.get("const")) in {int, bool}:
        result["x-python-const"] = node["const"]
    if "pattern" in node:
        result["x-python-pattern"] = result.pop("pattern")
    return result


def _unavailable(uri):
    raise ValueError("合同只接受已安装的本地引用：" + uri)


def registry(documents, base):
    resources = []
    for name, document in documents.items():
        if name == "dashboard-response-v2.json":
            resources.extend(
                (base + "dashboard/" + key, compile_schema(schema))
                for key, schema in document.items()
            )
        else:
            compiled = compile_schema(document)
            resources.extend((uri, compiled) for uri in {base + name, document["$id"]})
    return jsonschema_rs.Registry(
        resources, draft=jsonschema_rs.Draft202012, retriever=_unavailable
    )


def validator(schema, *, registry):
    return jsonschema_rs.Draft202012Validator(
        schema,
        registry=registry,
        offline=True,
        keywords=_KEYWORDS,
        formats=_FORMATS,
        validate_formats=True,
    )
