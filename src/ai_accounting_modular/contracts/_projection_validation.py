"""Validate batches or explicitly selected properties of installed object contracts."""

from functools import lru_cache

from jsonschema import FormatChecker

from . import _native_validation, _validation
from ._errors import ContractError


def _pointer(value):
    return value.replace("~", "~0").replace("/", "~1")


@lru_cache(maxsize=256)
def _schema(schema_name, definition, fields):
    uri = _validation._uri(schema_name, definition)
    if fields is None:
        item = {"$ref": uri}
    else:
        document = _validation._selected_document(schema_name, definition)
        node = (
            document
            if schema_name == "dashboard-response-v2.json"
            else document["$defs"][definition]
        )
        if node.get("type") != "object" or not set(fields) <= node.get("properties", {}).keys():
            raise ContractError("invalid_contract", "只能选择已安装对象合同中声明的属性。")
        item = {
            "type": "object",
            "properties": {
                name: {"$ref": uri + ("/" if "#" in uri else "#/") + "properties/" + _pointer(name)}
                for name in fields
            },
            "required": [name for name in node.get("required", ()) if name in fields],
            "additionalProperties": False,
        }
    return {"type": "array", "items": item}


@lru_cache(maxsize=256)
def _compiled(schema_name, definition, fields):
    return _native_validation.validator(
        _schema(schema_name, definition, fields), registry=_validation._native_registry()
    )


@lru_cache(maxsize=256)
def _diagnostic(schema_name, definition, fields):
    return _validation._Validator(
        _schema(schema_name, definition, fields),
        registry=_validation._bundle()[1],
        format_checker=FormatChecker(),
    )


def validate_contract_items[T](
    schema_name: str, definition: str, values: list[T], *, fields: tuple[str, ...] | None = None
) -> list[T]:
    """Return the same batch after checking full items or only the selected properties.

    A property projection retains its original schema and required presence. It
    does not prove omitted fields or the full object's cross-property constraints.
    No schema or property path can be supplied outside the installed bundle.
    """
    if fields is not None and (
        type(fields) is not tuple
        or any(type(name) is not str for name in fields)
        or len(fields) != len(set(fields))
    ):
        raise ContractError("invalid_contract", "属性选择须为无重复字段名的元组。")
    try:
        _validation._check_json(values)
        if _compiled(schema_name, definition, fields).is_valid(values):
            return values
        error = next(_diagnostic(schema_name, definition, fields).iter_errors(values), None)
    except ContractError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ContractError("invalid_input", "数据包含无效 JSON 类型或字符") from error
    if error is not None:
        pointer = "/" + "/".join(_pointer(str(part)) for part in error.absolute_path)
        raise ContractError(
            "invalid_input", f"字段 {pointer} 不符合 {definition} 批量合同"
        ) from error
    return values
