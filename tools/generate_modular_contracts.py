"""Generate Python wire types and packaged schemas from the design contracts.

The generated types describe fields for developers. Runtime constraints remain
in the original JSON Schema, including conditional and cross-field shapes.
"""

from __future__ import annotations

import argparse
import json
import keyword
import re
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/redesign/contracts"
TARGET = ROOT / "src/ai_accounting_modular/contracts"
SCHEMAS = (
    "core.schema.json",
    "business-facts.schema.json",
    "reporting.schema.json",
    "review-materials.schema.json",
    "dashboard-response-v2.json",
)
BASE = "https://ai-accounting.local/design/"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate schema key: {key}")
        result[key] = value
    return result


def identifier(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not name or name[0].isdigit() or keyword.iskeyword(name):
        name = "Wire_" + name
    return name


def title(value: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", value))


class TypeGenerator:
    """Translate only the field-shape vocabulary used by this contract set."""

    def __init__(self, documents):
        self.documents = documents
        self.names = {}
        self.nodes = {}
        self.exports = []
        self.used = {"JsonValue"}
        self.blocks = []
        for uri, document in documents.items():
            prefix = title(uri.rsplit("/", 1)[-1].split(".", 1)[0])
            for name, node in document.get("$defs", {}).items():
                proposed = name if not uri.startswith(BASE + "dashboard/") else prefix + "__" + name
                if proposed in self.used:
                    proposed = prefix + proposed
                pointer = "/$defs/" + name.replace("~", "~0").replace("/", "~1")
                self.register(uri, pointer, node, proposed)
                if not uri.startswith(BASE + "dashboard/"):
                    self.exports.append(self.names[uri, pointer])
            if uri.startswith(BASE + "dashboard/"):
                self.register(uri, "", document, title(uri.rsplit("/", 1)[-1]) + "Wire")
                self.exports.append(self.names[uri, ""])
        uri = BASE + "business-facts.schema.json"
        pointer = "/$defs/Dependency/properties/relation_kind"
        node = documents[uri]["$defs"]["Dependency"]["properties"]["relation_kind"]
        self.exports.append(self.register(uri, pointer, node, "DependencyRelationKind"))

    def register(self, uri, pointer, node, proposed):
        key = uri, pointer
        if key in self.names:
            return self.names[key]
        name = identifier(proposed)
        if name in self.used:
            original = name
            number = 2
            while name in self.used:
                name = original + str(number)
                number += 1
        self.used.add(name)
        self.names[key] = name
        self.nodes[key] = node
        return name

    def reference(self, uri, reference):
        target, fragment = urldefrag(urljoin(uri, reference))
        pointer = unquote(fragment)
        key = target, pointer
        if key not in self.names:
            node = self.documents[target]
            for part in pointer.lstrip("/").split("/") if pointer else ():
                node = node[part.replace("~1", "/").replace("~0", "~")]
            proposed = title(target.rsplit("/", 1)[-1]) + "Root"
            self.register(target, pointer, node, proposed)
        return self.names[key]

    def expression(self, node, uri, pointer, proposed, *, root=False):
        if not root and (uri, pointer) in self.names:
            return self.names[uri, pointer]
        if node is True or node == {}:
            return "JsonValue"
        if node is False:
            return "Never"
        if "$ref" in node:
            return self.reference(uri, node["$ref"])
        if "const" in node:
            return "Literal[" + repr(node["const"]) + "]"
        if "enum" in node:
            return "Literal[" + ", ".join(repr(value) for value in node["enum"]) + "]"
        for choice in ("oneOf", "anyOf"):
            if choice in node and node.get("type") != "object" and "properties" not in node:
                variants = [
                    self.expression(
                        child, uri, pointer + f"/{choice}/{index}", proposed + f"Variant{index + 1}"
                    )
                    for index, child in enumerate(node[choice])
                ]
                return " | ".join(dict.fromkeys(variants))
        kind = node.get("type")
        if kind == "object" or "properties" in node:
            properties = node.get("properties", {})
            extra = node.get("additionalProperties", True)
            if not properties and isinstance(extra, dict):
                value = self.expression(
                    extra, uri, pointer + "/additionalProperties", proposed + "Value"
                )
                return f"dict[str, {value}]"
            if not properties and extra is not False:
                return "dict[str, JsonValue]"
            if not root:
                return self.register(uri, pointer, node, proposed)
            return self.typed_dict(self.names[uri, pointer], node, uri, pointer)
        if kind == "array":
            item = self.expression(
                node.get("items", {}), uri, pointer + "/items", proposed + "Item"
            )
            return f"list[{item}]"
        if kind in {"string", "integer", "boolean", "null"}:
            return {"string": "str", "integer": "int", "boolean": "bool", "null": "None"}[kind]
        if "allOf" in node:
            # The sole shape-less allOf is a constrained Rate alias. Conditions
            # on object fields stay in JSON Schema and are not duplicate models.
            for index, child in enumerate(node["allOf"]):
                if "$ref" in child or "type" in child:
                    return self.expression(child, uri, pointer + f"/allOf/{index}", proposed)
        raise ValueError(f"Unsupported field shape at {uri}#{pointer}: {node}")

    def typed_dict(self, name, node, uri, pointer):
        required = set(node.get("required", ()))
        fields = []
        for field, child in node.get("properties", {}).items():
            part = field.replace("~", "~0").replace("/", "~1")
            annotation = self.expression(
                child, uri, pointer + "/properties/" + part, name + title(field)
            )
            if field not in required:
                annotation = f"NotRequired[{annotation}]"
            fields.append((field, annotation))
        if all(field.isidentifier() and not keyword.iskeyword(field) for field, _ in fields):
            return (
                "class "
                + name
                + "(TypedDict):\n"
                + (
                    "\n".join(f"    {field}: {annotation}" for field, annotation in fields)
                    or "    pass"
                )
            )
        entries = ",\n".join(f"    {field!r}: {annotation!r}" for field, annotation in fields)
        return f"{name} = TypedDict({name!r}, {{\n{entries}\n}})"

    def generate(self):
        position = 0
        while position < len(self.nodes):
            key = list(self.nodes)[position]
            uri, pointer = key
            name, node = self.names[key], self.nodes[key]
            expression = self.expression(node, uri, pointer, name, root=True)
            self.blocks.append(
                expression
                if expression.startswith(("class ", name + " = TypedDict"))
                else f"type {name} = {expression}"
            )
            position += 1
        header = (
            '"""Generated by tools/generate_modular_contracts.py; do not edit."""\n\n'
            "# ruff: noqa: E501\n"
            "from __future__ import annotations\n\n"
            "from typing import Literal, NotRequired, TypedDict\n\n"
            "type JsonValue = (\n"
            "    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]\n"
            ")\n\n"
        )
        if any("Never" in block for block in self.blocks):
            header = header.replace("Literal, NotRequired", "Literal, Never, NotRequired")
        exports = (
            "\n\n__all__ = [\n"
            + "".join(f"    {name!r},\n" for name in ["JsonValue", *self.exports])
            + "]\n"
        )
        return header + "\n\n".join(self.blocks) + exports


def outputs():
    documents, generated = {}, {}
    for name in SCHEMAS:
        data = (SOURCE / name).read_bytes().replace(b"\r\n", b"\n")
        document = json.loads(data, object_pairs_hook=unique_object)
        generated[TARGET / "_schemas" / name] = data
        if name == "dashboard-response-v2.json":
            documents.update(
                (BASE + "dashboard/" + namespace, schema) for namespace, schema in document.items()
            )
        else:
            documents[BASE + name] = document
    generated[TARGET / "_wire_types.py"] = TypeGenerator(documents).generate().encode("utf-8")
    return generated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = outputs()
    different = [
        path.relative_to(ROOT).as_posix()
        for path, content in expected.items()
        if not path.is_file() or path.read_bytes() != content
    ]
    if args.check:
        if different:
            parser.exit(1, "Generated contracts differ:\n" + "\n".join(different) + "\n")
        print("Modular contract types and packaged schemas are synchronized.")
        return
    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print(f"Generated {len(expected)} modular contract files.")


if __name__ == "__main__":
    main()
