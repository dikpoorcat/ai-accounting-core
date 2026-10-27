"""Compact publication identities, separate from owner bodies and collection proofs."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
)

BODY_COLLECTION = "_component_body"
FORMAT = "reading-component/1"
_OWNER_SEMANTICS = {"brief_activity", "brief_open_items"}
_FIELDS = {
    "format",
    "scope_key",
    "component_key",
    "head",
    "format_version",
    "body_digest",
    "semantic_digest",
}


def _corrupt():
    raise ContractError("storage_corrupt", "读取组件的发布描述或正文不一致，请重新生成。")


def _digest(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


@dataclass(frozen=True)
class Publication:
    descriptor_json: str
    descriptor_digest: str
    body_json: str


def prepare(scope_key, component, head, format_version, model, *, payload=None):
    """Bind one immutable owner body to its actual publication and public version."""
    if payload is None:
        payload = loads_json(model.payload_json)
    if not isinstance(payload, dict) or content_digest(payload) != model.content_digest:
        _corrupt()
    semantic = (
        payload.get("_semantic_digest") if component in _OWNER_SEMANTICS else model.content_digest
    )
    value = {
        "format": FORMAT,
        "scope_key": scope_key,
        "component_key": component,
        "head": head,
        "format_version": format_version,
        "body_digest": model.content_digest,
        "semantic_digest": semantic,
    }
    _validate(value)
    return Publication(canonical_json(value), content_digest(value), model.payload_json)


def _validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != _FIELDS
        or value["format"] != FORMAT
        or any(
            not isinstance(value[key], str) or not value[key]
            for key in ("scope_key", "component_key")
        )
        or type(value["head"]) is not int
        or value["head"] < 0
        or type(value["format_version"]) is not int
        or value["format_version"] < 1
        or any(not _digest(value[key]) for key in ("body_digest", "semantic_digest"))
        or (
            value["component_key"] not in _OWNER_SEMANTICS
            and value["semantic_digest"] != value["body_digest"]
        )
    ):
        _corrupt()


def read(scope_key, component, state, row):
    """Check the persisted descriptor itself before using its small version identity."""
    try:
        value = loads_json(row["payload_json"])
        _validate(value)
        if (
            (state["scope_key"], state["component_key"]) != (scope_key, component)
            or (row["scope_key"], row["component_key"]) != (scope_key, component)
            or (value["scope_key"], value["component_key"]) != (scope_key, component)
            or row["head"] != state["ready_head"]
            or value["head"] != state["ready_head"]
            or value["format_version"] != state["format_version"]
            or content_digest(value) != state["content_digest"]
        ):
            _corrupt()
        return value
    except (KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "读取组件的发布描述不完整，请重新生成。") from error


def body_state(state, descriptor):
    """Owner validators still bind their body digest, never the descriptor's digest."""
    return dict(
        state,
        content_digest=descriptor["body_digest"],
        descriptor_semantic_digest=descriptor["semantic_digest"],
    )


def body(scope_key, component, descriptor, row):
    """Check private row ownership; its consumer checks the actual body digest once."""
    try:
        if (
            row is None
            or (row["scope_key"], row["component_key"]) != (scope_key, component)
            or row["collection_key"] != BODY_COLLECTION
            or row["item_id"] != component
            or row["sort_key"] != component
            or row["head"] != descriptor["head"]
            or any(
                row[key] is not None
                for key in ("business_id", "object_id", "group_key", "category")
            )
        ):
            _corrupt()
        return {
            "scope_key": scope_key,
            "component_key": component,
            "head": row["head"],
            "payload_json": row["payload_json"],
        }
    except (KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "读取组件的实际正文不完整，请重新生成。") from error
