"""Skip union branches only when their own required literals prove impossibility."""

from jsonschema import Draft202012Validator
from referencing import Resource
from referencing.jsonschema import DRAFT202012

_GUARDS = {}
_MAPS = ("$defs", "properties", "patternProperties", "dependentSchemas")
_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
_CHILDREN = (
    "items",
    "contains",
    "if",
    "then",
    "else",
    "not",
    "propertyNames",
    "additionalProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contentSchema",
)


def install_guards(schemas):
    """Compile installed schemas once; no business kinds or field names are hardcoded."""
    compiled, active = {}, set()

    def scope(node, resolver):
        if isinstance(node, dict) and "$id" in node:
            return resolver.in_subresource(
                Resource.from_contents(node, default_specification=DRAFT202012)
            )
        return resolver

    def compile_node(node, resolver):
        key = id(node)
        if key in compiled:
            return compiled[key][1]
        if not isinstance(node, dict) or key in active:
            return ()
        active.add(key)
        checks = []
        if "$ref" in node:
            resolved = resolver.lookup(node["$ref"])
            checks.extend(compile_node(resolved.contents, resolved.resolver))
        for child in node.get("allOf", ()):
            checks.extend(compile_node(child, scope(child, resolver)))
        required = set(node.get("required", ()))
        checks.extend((name, None, True) for name in sorted(required))
        for name, child in node.get("properties", {}).items():
            if not isinstance(child, dict):
                continue
            values = [child["const"]] if "const" in child else child.get("enum")
            if values and all(type(value) is str for value in values):
                checks.append((name, frozenset(values), name in required))
        active.remove(key)
        result = tuple(checks)
        compiled[key] = node, result
        return result

    def walk(node, resolver):
        if isinstance(node, dict):
            resolver = scope(node, resolver)
            compile_node(node, resolver)
            for keyword in _MAPS:
                for value in node.get(keyword, {}).values():
                    walk(value, resolver)
            for keyword in _LISTS:
                for value in node.get(keyword, ()):
                    walk(value, resolver)
            for keyword in _CHILDREN:
                if keyword in node:
                    walk(node[keyword], resolver)

    for schema, resolver in schemas:
        walk(schema, resolver)
    _GUARDS.update(compiled)


def possible_branch(branch, instance):
    """Reject only branches whose own necessary fields/literals cannot hold."""
    if not isinstance(instance, dict):
        return True
    entry = _GUARDS.get(id(branch))
    checks = entry[1] if entry is not None and entry[0] is branch else ()
    return all(
        (
            not required
            if name not in instance
            else values is None or type(instance[name]) is str and instance[name] in values
        )
        for name, values, required in checks
    )


def one_of(validator, branches, instance, schema):
    if isinstance(instance, dict):
        branches = [branch for branch in branches if possible_branch(branch, instance)]
    yield from Draft202012Validator.VALIDATORS["oneOf"](validator, branches, instance, schema)
