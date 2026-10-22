"""Generate wire types, packaged schemas and AI guides from the design truth.

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

# Exact documented inputs, not a prefix fallback or a claim about an assembled instance.
BUSINESS_GUIDES = (
    (
        "经营与往来",
        (
            "operations.service_sale",
            "operations.expense",
            "operations.other_income",
            "operations.advance",
            "operations.advance_fulfillment",
            "operations.advance_refund",
            "operations.sale_return",
            "operations.expense_recovery",
            "operations.deposit",
            "operations.pass_through",
            "operations.pass_through_return",
            "operations.reimbursement",
            "operations.overpayment",
        ),
    ),
    (
        "用工与薪酬",
        (
            "people.employment",
            "people.relationship",
            "people.remuneration",
            "people.contribution_policy",
            "people.contribution_actual",
        ),
    ),
    (
        "资产与项目",
        (
            "assets.acquisition",
            "assets.activation",
            "assets.disposal",
            "assets.project_cost",
            "assets.project_release",
        ),
    ),
    (
        "借款与股东资金",
        (
            "financing.owner_funding",
            "financing.loan_contract",
            "financing.loan_drawdown",
            "financing.interest_period",
        ),
    ),
    ("货币基金", ("investment.money_fund_purchase", "investment.money_fund_redemption")),
    (
        "合作项目投资",
        (
            "investment.cooperation_contract",
            "investment.cooperation_funding",
            "investment.cooperation_collection",
            "investment.cooperation_payment",
            "investment.cooperation_operator_transfer",
            "investment.cooperation_distribution",
            "investment.cooperation_exit",
        ),
    ),
    (
        "实际资金、备用金与核对",
        (
            "money.movement",
            "money.transfer",
            "money.bank_statement",
            "money.platform_activity",
            "money.reserve_internal",
            "money.reconciliation",
        ),
    ),
    (
        "税费和期间口径",
        (
            "taxes.tax_point",
            "taxes.assessment",
            "taxes.actual_declaration",
            "taxes.tax_credit",
            "taxes.vat_policy",
            "taxes.surtax_policy",
            "taxes.used_asset_vat_policy",
            "taxes.income_tax_policy",
            "taxes.prepayment_basis",
        ),
    ),
    ("期初与报表接续", ("opening.package", "reports.continuation")),
)

GUIDE_LINKS = {
    "#合作项目投资": "accounting://guides/business/investment.cooperation_contract",
    "03-contracts.md": "accounting://guides/commands",
    "05-business-rules.md": "accounting://guides/business",
    "05b-financial-reporting.md": "accounting://guides/reporting",
    "08-ai-work-and-materials.md": "accounting://guides/work",
    "09-runtime-and-ai.md": "accounting://guides/runtime",
    "core.schema.json": "accounting://contracts",
    "business-facts.schema.json": "accounting://contracts",
}


def markdown_section(text, heading):
    """Extract an existing exact heading and its children; missing/duplicate is an error."""
    lines = text.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.rstrip() == heading]
    if len(matches) != 1:
        raise ValueError(f"Guide heading must exist exactly once: {heading}")
    start = matches[0]
    level = len(heading) - len(heading.lstrip("#"))
    for end in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6}) ", lines[end])
        if match and len(match[1]) <= level:
            return "".join(lines[start:end]).strip() + "\n"
    return "".join(lines[start:]).strip() + "\n"


def fact_kinds(document):
    kinds, visited = set(), set()

    def visit(node):
        if "$ref" in node:
            prefix = "#/$defs/"
            if not node["$ref"].startswith(prefix):
                raise ValueError("Fact guide discovery requires its actual local input union.")
            name = node["$ref"][len(prefix) :]
            if name not in visited:
                visited.add(name)
                visit(document["$defs"][name])
        kind = node.get("properties", {}).get("kind", {}).get("const")
        if kind is not None:
            if not isinstance(kind, str) or kind in kinds:
                raise ValueError(f"Fact kind must have one documented input: {kind}")
            kinds.add(kind)
        for branch in node.get("oneOf", ()):
            visit(branch)

    visit(document["$defs"]["Fact"])
    return kinds


def guide_outputs(root, business_document):
    source = root / "docs/redesign"

    def read(name):
        return (source / name).read_text(encoding="utf-8").replace("\r\n", "\n")

    def page(title, name, text):
        def link(match):
            label, target = match.groups()
            if target in GUIDE_LINKS:
                return f"[{label}]({GUIDE_LINKS[target]})"
            if ":" in target or target.startswith("#"):
                return match[0]
            name = target.split("#", 1)[0].rsplit("/", 1)[-1]
            if name in GUIDE_LINKS:
                return f"[{label}]({GUIDE_LINKS[name]})"
            # Design-only references are identified honestly, never dangling workspace links.
            return f"{label}（设计参考：{target}）"

        return {
            "title": title,
            "source": "docs/redesign/" + name,
            "text": re.sub(r"\[([^\]]+)\]\(([^\s)]+)\)", link, text).strip() + "\n",
        }

    operations = read("08-ai-work-and-materials.md")
    work = "\n\n".join(
        markdown_section(operations, "## " + heading)
        for heading in (
            "开始工作时取得什么",
            "资料如何进入系统",
            "不让老板承担程序协调",
            "当前待办只保留还需要做的事",
            "核对、更正和顾问审查",
        )
    )
    contracts = read("03-contracts.md")
    commands = "\n\n".join(
        markdown_section(contracts, "## " + heading)
        for heading in ("通用字段", "完整业务办理", "其他公开入口", "标准业务文档")
    )
    reporting = read("05b-financial-reporting.md")
    # Keep the installed operating text, not the developers' acceptance instructions.
    development = markdown_section(reporting, "## 开发验收")
    reporting = reporting[: reporting.index(development)].strip() + "\n"
    rules = read("05-business-rules.md")
    common = markdown_section(rules, "## 公共事实与计算条件")
    corrections = "\n".join(
        markdown_section(rules, "### " + heading)
        for heading in ("义务转移与多付重分类", "同一业务更正分类")
    )
    business, sections = {}, {}
    for heading, kinds in BUSINESS_GUIDES:
        level = "### " if heading in {"货币基金", "合作项目投资"} else "## "
        section = markdown_section(rules, level + heading)
        sections[heading] = page(heading, "05-business-rules.md", section)["text"]
        for kind in kinds:
            if kind in business:
                raise ValueError(f"Duplicate guide mapping: {kind}")
            business[kind] = heading
    actual = fact_kinds(business_document)
    if set(business) != actual:
        raise ValueError(
            f"Business guides differ from Fact inputs; missing={sorted(actual - business.keys())}; "
            f"extra={sorted(business.keys() - actual)}"
        )
    topics = {
        "work": page("会计 AI 工作引导", "08-ai-work-and-materials.md", work),
        "commands": page("公开命令与标准资料", "03-contracts.md", commands),
        "business": page(
            "按事实种类读取业务规则",
            "05-business-rules.md",
            "先用 accounting_schema 取得当前实例实际支持的 kind，再读取 "
            "accounting://guides/business/{kind}。规则文档不证明当前实例已装配该业务。\n",
        ),
        "reporting": page("报表期间、采用与未知", "05b-financial-reporting.md", reporting),
        "runtime": page(
            "安装、运行与 AI 接入", "09-runtime-and-ai.md", read("09-runtime-and-ai.md")
        ),
    }
    payload = {
        "format": "ai-accounting-guides/1",
        "topics": topics,
        "business": {
            "kinds": business,
            "sections": sections,
            "common": page("公共事实", "05-business-rules.md", common)["text"],
            "corrections": page("更正与清偿", "05-business-rules.md", corrections)["text"],
        },
    }
    return {
        root / "src/ai_accounting_modular/agent/_guides.json": (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
    }


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
    generated.update(guide_outputs(ROOT, documents[BASE + "business-facts.schema.json"]))
    return generated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = outputs()
    different = [
        path.relative_to(ROOT).as_posix()
        for path, content in expected.items()
        if not path.is_file() or path.read_bytes().replace(b"\r\n", b"\n") != content
    ]
    if args.check:
        if different:
            parser.exit(1, "Generated contracts differ:\n" + "\n".join(different) + "\n")
        print("Modular contract types, schemas and AI guides are synchronized.")
        return
    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    print(f"Generated {len(expected)} modular contract files.")


if __name__ == "__main__":
    main()
