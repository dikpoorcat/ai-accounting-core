"""Explicit, narrow next actions for a known business information gap.

The caller must select an action from a typed source; error text and codes never
generate an action. No resolution is emitted when the next action is uncertain.
"""

from __future__ import annotations

from collections.abc import Mapping

_COMMANDS = frozenset({
    "save_fact", "amend_fact", "find_facts", "prepare_fact_registration",
    "receive_material", "resolve_material", "resolve_material_group",
    "register_entity", "update_entity_profile", "inspect_material",
})


def validate_resolution(value: Mapping, *, registry=None) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("resolution must be a mapping")
    keys = set(value)
    if keys not in ({"command"}, {"fact_kind"}, {"candidates"}):
        raise ValueError("resolution requires exactly one typed target")
    if "command" in value:
        command = value["command"]
        if command not in _COMMANDS:
            raise ValueError("unknown resolution command")
        return {"command": command}
    if "fact_kind" in value:
        kind = value["fact_kind"]
        if registry is None or not isinstance(kind, str) or kind not in registry.models:
            raise ValueError("resolution fact kind must be registered")
        return {"fact_kind": kind}
    candidates = value["candidates"]
    if not isinstance(candidates, (list, tuple)) or not candidates or len(candidates) > 100:
        raise ValueError("resolution candidates must be a bounded nonempty list")
    if any(not isinstance(item, str) or not item or len(item) > 200 for item in candidates):
        raise ValueError("invalid resolution candidate")
    if len(set(candidates)) != len(candidates):
        raise ValueError("duplicate resolution candidate")
    return {"candidates": list(candidates)}
