"""Source identities follow declared source schemas, never JSON key names."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urljoin

from ._dispatch import possible_branch
from ._errors import ContractError
from ._native_validation import validator as compiled_validator
from ._validation import _bundle, _native_registry, validate_contract
from ._wire_types import Fact, PartialFact


@dataclass(frozen=True)
class _Plan:
    capture: bool = False
    capture_id: bool = False
    properties: tuple[tuple[str, _Plan], ...] = ()
    items: _Plan | None = None
    children: tuple[_Plan, ...] = ()
    alternatives: tuple[tuple[Callable, _Plan], ...] = ()
    condition: tuple[Callable, _Plan, _Plan] | None = None

    @property
    def empty(self):
        return not (
            self.capture
            or self.capture_id
            or self.properties
            or self.items
            or self.children
            or self.alternatives
            or self.condition
        )

    def source_paths(self, value, pointer=""):
        if self.capture:
            yield value["source_id"], pointer + "/source_id"
        if self.capture_id:
            yield value, pointer
        if isinstance(value, dict):
            for name, child in self.properties:
                if name in value:
                    yield from child.source_paths(value[name], pointer + "/" + _pointer_part(name))
        if self.items is not None and isinstance(value, list):
            for index, item in enumerate(value):
                yield from self.items.source_paths(item, pointer + "/" + str(index))
        for child in self.children:
            yield from child.source_paths(value, pointer)
        for matches, child in self.alternatives:
            if matches(value):
                yield from child.source_paths(value, pointer)
        if self.condition is not None:
            matches, then, otherwise = self.condition
            yield from (then if matches(value) else otherwise).source_paths(value, pointer)


_EMPTY = _Plan()


def _pointer_part(part):
    return str(part).replace("~", "~0").replace("/", "~1")


def _child_uri(uri, *parts):
    return uri + "/" + "/".join(_pointer_part(part) for part in parts)


@lru_cache(maxsize=2)
def _source_plan(definition):
    documents, registry = _bundle()
    source_schema = documents["core.schema.json"]["$defs"]["SourceLocator"]
    source_id_schema = documents["core.schema.json"]["$defs"]["SourceId"]
    partial_locator = documents["business-facts.schema.json"]["$defs"]["PartialSourceLocator"]
    compiled, active = {}, set()

    def predicate(uri):
        branch = registry.resolver().lookup(uri).contents
        validator = compiled_validator({"$ref": uri}, registry=_native_registry())
        return lambda value: possible_branch(branch, value) and validator.is_valid(value)

    def compile_node(uri):
        if uri in compiled:
            return compiled[uri]
        if uri in active:
            raise ContractError("invalid_contract", "业务来源合同存在无法展开的递归声明。")
        active.add(uri)
        node = registry.resolver().lookup(uri).contents
        if not isinstance(node, dict):
            plan = _EMPTY
        elif node is source_schema:
            plan = _Plan(capture=True)
        elif node is source_id_schema:
            plan = _Plan(capture_id=True)
        elif node is partial_locator:
            plan = _Plan(properties=(("source_id", _Plan(capture_id=True)),))
        else:
            children = []
            if "$ref" in node:
                children.append(compile_node(urljoin(uri, node["$ref"])))
            children.extend(
                compile_node(_child_uri(uri, "allOf", index))
                for index in range(len(node.get("allOf", ())))
            )
            properties = tuple(
                (name, child)
                for name in node.get("properties", {})
                if not (child := compile_node(_child_uri(uri, "properties", name))).empty
            )
            items = compile_node(_child_uri(uri, "items")) if "items" in node else _EMPTY
            alternatives = []
            for keyword in ("oneOf", "anyOf"):
                for index in range(len(node.get(keyword, ()))):
                    branch_uri = _child_uri(uri, keyword, index)
                    child = compile_node(branch_uri)
                    if not child.empty:
                        alternatives.append((predicate(branch_uri), child))
            condition = None
            if "if" in node:
                then = compile_node(_child_uri(uri, "then")) if "then" in node else _EMPTY
                otherwise = compile_node(_child_uri(uri, "else")) if "else" in node else _EMPTY
                if not then.empty or not otherwise.empty:
                    condition = (predicate(_child_uri(uri, "if")), then, otherwise)
            plan = _Plan(
                properties=properties,
                items=None if items.empty else items,
                children=tuple(child for child in children if not child.empty),
                alternatives=tuple(alternatives),
                condition=condition,
            )
        active.remove(uri)
        compiled[uri] = plan
        return plan

    return compile_node(documents["business-facts.schema.json"]["$id"] + "#/$defs/" + definition)


def fact_source_paths(fact: Fact) -> dict[str, tuple[str, ...]]:
    """Map declared source identities to every exact JSON Pointer in this fact."""
    return _paths(fact, "Fact")


def _paths(fact, definition):
    validate_contract("business-facts.schema.json", definition, fact)
    return _validated_paths(fact, definition)


def _validated_paths(fact, definition):
    """The caller owns the strict validation of this exact, unchanged value."""
    paths = {}
    for index, source in enumerate(fact.get("evidence_ids", ())):
        paths.setdefault(source, set()).add(f"/evidence_ids/{index}")
    for source, pointer in _source_plan(definition).source_paths(fact):
        paths.setdefault(source, set()).add(pointer)
    return {source: tuple(sorted(paths[source])) for source in sorted(paths)}


def fact_source_ids(fact: Fact) -> tuple[str, ...]:
    """Return all declared fact sources, sorted and deduplicated by identity.

    Locator locations do not create another source identity. Business references,
    object identities and arbitrary strings are never treated as source IDs.
    """
    return tuple(fact_source_paths(fact))


def partial_fact_source_paths(fact: PartialFact) -> dict[str, tuple[str, ...]]:
    """Keep declared sources even when the surrounding business is incomplete."""
    return _paths(fact, "PartialFact")


def partial_fact_source_ids(fact: PartialFact) -> tuple[str, ...]:
    return tuple(partial_fact_source_paths(fact))
