"""Offline source and typed-template checks; never opens a business database.

All target identities are synthetic. A passing check proves the templates match
the present contracts, not that a future replay has executed or published them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from pydantic import ValidationError
from source_archive import verify_facts_sources

from ai_accounting.kernel.command_schema import command_models
from ai_accounting.kernel.entities import entity_profile_models
from ai_accounting.kernel.entity_references import references_from_data
from ai_accounting.kernel.fund_accounts import fund_profile_models
from ai_accounting.kernel.replay_document_materials import material_amount_field_issues
from ai_accounting.kernel.service import default_registry


class TemplateError(ValueError):
    pass


GENERATED_OUTPUTS = {
    "confirm_asset_activation_batch": {"asset_activation", "asset_activation_batch"},
    "confirm_asset_consumption_month": {"asset_consumption", "asset_consumption_month"},
    "confirm_material_allocation": {"material_period_allocation"},
    "confirm_payroll_preparation": {"payroll", "payroll_bounded"},
}


def entity_issues(kind, data, indexed, registry):
    """Reuse the kernel's declared roles, including nested generated asset inputs."""
    issues = []
    for reference in references_from_data(kind, data, registry=registry):
        if reference["reference_type"] != "entity":
            continue
        binding = marker(reference["entity_id"])
        entity = indexed.get(binding[0]) if binding and binding[1] == "entity_id" else None
        if entity is None or entity.get("kind") not in entity_profile_models():
            issues.append("entity role lacks a runtime entity binding: " + reference["path"])
        elif reference["kinds"] and entity["kind"] not in reference["kinds"]:
            issues.append("entity kind mismatch: " + reference["path"])
        elif reference["account_type"] and entity.get("account_type") != reference["account_type"]:
            issues.append("entity account type mismatch: " + reference["path"])
    if kind == "asset_activation_batch":
        for member in data.get("members", []):
            if "data" in member:
                issues.extend(entity_issues("asset_activation", member["data"], indexed, registry))
    return issues


def publishes_subject(step, target):
    if step.get("command") == "confirm":
        return any(
            ref["path"].startswith("/subjects/")
            and ref["target_key"] == target
            and ref["binding"] == "subject_id"
            for ref in step.get("references", [])
        )
    return {"preview", "confirm"} <= set(step.get("commands", [])) and target in set(
        step.get("inputs", [])
    ) | set(step.get("expected_fact_keys", []))


def walk(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + "/" + str(index))
    else:
        yield path, value


def marker(value):
    if isinstance(value, str) and value.startswith("$ref:"):
        parts = value[5:].rsplit(":", 1)
        if len(parts) != 2 or not all(parts):
            raise TemplateError("invalid runtime binding marker")
        return tuple(parts)
    return None


def synthetic(key, binding):
    if binding == "revision":
        return 1
    if binding == "epochs":
        return {"accounting": 1, "material": 1, "management": 1}
    if binding in {"preview_digest", "evidence_digest"}:
        return hashlib.sha256((key + ":" + binding).encode()).hexdigest()
    if binding in {
        "entity_id",
        "subject_id",
        "fact_id",
        "calculation_id",
        "company_id",
        "request_id",
        "voucher_version_id",
    }:
        return hashlib.sha256((key + ":" + binding).encode()).hexdigest()[:32]
    raise TemplateError("unsupported binding: " + binding)


def bind(value):
    if isinstance(value, dict):
        return {key: bind(child) for key, child in value.items()}
    if isinstance(value, list):
        return [bind(child) for child in value]
    reference = marker(value)
    return synthetic(*reference) if reference else value


def check_document(path: Path, root: Path):
    document = json.loads(path.read_text(encoding="utf-8"))
    registry = default_registry()
    amount_issues = material_amount_field_issues(document, registry=registry)
    if amount_issues:
        raise TemplateError(
            "material amount fields: " + json.dumps(amount_issues, ensure_ascii=False)
        )
    commands = command_models(registry)
    sections = ("sources", "entities", "facts", "materials", "steps")
    entries = [item for section in sections for item in document.get(section, [])]
    indexed = {}
    for item in entries:
        key = item["key"]
        if key in indexed:
            raise TemplateError("duplicate logical key: " + key)
        indexed[key] = item
    output_owners = {}
    output_kinds = {}
    for step in document.get("steps", []):
        for output in step.get("outputs", []):
            key = output["key"]
            if key in output_owners:
                raise TemplateError("duplicate runtime output key: " + key)
            if key in indexed and not (
                indexed[key].get("kind") == output.get("kind")
                and indexed[key].get("registration_command") == step.get("command")
            ):
                raise TemplateError("runtime output conflicts with input template: " + key)
            if output.get("kind") not in GENERATED_OUTPUTS.get(step.get("command"), set()):
                raise TemplateError("step does not return the declared fact kind: " + key)
            output_owners[key] = step["key"]
            output_kinds[key] = output["kind"]
        for key in step.get("expected_fact_keys", []):
            if indexed.get(key, {}).get("produced_by") == step["key"]:
                kind = indexed[key]["kind"]
                if not any(
                    kind in GENERATED_OUTPUTS.get(command, set())
                    for command in step.get("commands", [])
                ):
                    raise TemplateError("guide does not confirm the declared output: " + key)
                output_owners[key] = step["key"]
                output_kinds[key] = kind
    known = set(indexed) | set(output_owners) | {"target_company"}
    source_keys = {item["key"] for item in document["sources"]}
    source_by_key = {item["key"]: item for item in document["sources"]}
    entity_keys = {item["key"] for item in document.get("entities", [])}
    fact_keys = {
        item["key"] for section in ("facts", "materials") for item in document.get(section, [])
    }
    errors = []
    publication_by_subject = {}
    publication_nodes = {}
    for publication in document.get("publication_bindings", []):
        subject, producer = publication["subject_key"], publication["producer_step_key"]
        publication_key = publication["key"]
        if publication_key in known or publication_key in publication_nodes:
            raise TemplateError("duplicate publication event key: " + publication_key)
        if subject in publication_by_subject:
            raise TemplateError("duplicate subject publication: " + subject)
        if not publishes_subject(indexed.get(producer, {}), subject):
            errors.append((publication_key, "publication producer does not confirm this subject"))
        kind = indexed.get(subject, {}).get("kind", output_kinds.get(subject))
        if kind not in registry.evaluators or registry.models[kind].lane != "accounting":
            errors.append((publication_key, "publication subject has no accounting calculation"))
        if subject not in publication.get("depends_on", []):
            errors.append((publication_key, "publication must wait for saved subject"))
        publication_by_subject[subject] = publication_key
        publication_nodes[publication_key] = publication
    known |= set(publication_nodes)
    references = 0
    for item in entries:
        key = item["key"]
        for source in item.get("source_keys", item.get("sources", [])):
            if source not in source_keys:
                errors.append((key, "unknown source key"))
        for location in item.get("source_locations", []):
            if (
                isinstance(location, dict)
                and location.get("source_key") is not None
                and location["source_key"] not in source_keys
            ):
                errors.append((key, "unknown source location key"))
        for dependency in item.get("depends_on", []):
            if dependency not in known:
                errors.append((key, "unknown dependency"))
        data = item.get("data")
        if data is None:
            continue
        declared = {
            ref["path"]: (ref["target_key"], ref["binding"]) for ref in item.get("references", [])
        }
        actual = {pointer: marker(value) for pointer, value in walk(data) if marker(value)}
        if len(declared) != len(item.get("references", [])) or declared != actual:
            errors.append(
                (key, "binding paths differ: " + str(sorted(set(declared) ^ set(actual))))
            )
        references += len(actual)
        required_publications = set()
        for target, binding in actual.values():
            if target not in known:
                errors.append((key, "unknown binding target"))
            elif binding == "entity_id" and target not in entity_keys:
                errors.append((key, "entity binding must target a registered entity"))
            elif binding in {"subject_id", "fact_id", "revision"} and target not in fact_keys | set(
                output_owners
            ):
                errors.append((key, "fact binding must target a fact or typed runtime output"))
            if binding in {"calculation_id", "voucher_version_id"}:
                kind = indexed.get(target, {}).get("kind", output_kinds.get(target))
                if kind not in registry.evaluators or registry.models[kind].lane != "accounting":
                    errors.append((key, "calculation binding lacks an accounting fact"))
                elif target not in publication_by_subject:
                    errors.append((key, "calculation binding lacks a publication event"))
                else:
                    required_publications.add(publication_by_subject[target])
            if binding == "revision" and target in fact_keys | set(output_owners):
                kind = indexed.get(target, {}).get("kind", output_kinds.get(target))
                if kind not in registry.models:
                    errors.append((key, "revision target is not a registered fact type"))
                elif indexed.get(target, {}).get("produced_by") and target not in output_owners:
                    errors.append((key, "revision requires a confirmed typed output producer"))
                elif target not in output_owners and indexed.get(target, {}).get(
                    "registration_command"
                ) not in {
                    None,
                    "save_fact",
                    "save_facts",
                    registry.models[kind].registration_command,
                }:
                    errors.append((key, "revision target has no confirmed registration return"))
            if binding in {"preview_digest", "epochs"}:
                producer = indexed.get(target, {})
                if producer.get("command") not in {
                    "preview",
                    "prepare_payroll",
                    "prepare_asset_activation_batch",
                    "prepare_asset_consumption_month",
                    "preview_material_allocation",
                }:
                    errors.append((key, "preview binding must come from a preview/prepare step"))
            if binding == "company_id" and target != "target_company":
                errors.append((key, "company binding must use the new target company"))
            if binding == "request_id" and target != key:
                errors.append((key, "request binding must belong to its own execution step"))
            if target not in {key, "target_company"} | set(item.get("depends_on", [])) | set(
                item.get("fresh_subject_key_allocations", [])
            ):
                errors.append((key, "binding target missing from dependencies"))
            synthetic(target, binding)
        if required_publications != set(item.get("publication_dependencies", [])):
            errors.append((key, "published output wait dependencies differ from bindings"))
        kind = item.get("kind")
        if kind in registry.models:
            errors.extend((key, issue) for issue in entity_issues(kind, data, indexed, registry))
        if item.get("command") in {
            "prepare_asset_activation_batch",
            "confirm_asset_activation_batch",
        }:
            errors.extend(
                (key, issue)
                for issue in entity_issues("asset_activation_batch", data, indexed, registry)
            )
        if key in entity_keys:
            model = (
                fund_profile_models()[item["account_type"]]
                if item["kind"] == "fund_account"
                else entity_profile_models()[item["kind"]]
            )
        elif key in fact_keys:
            model = registry.input_models.get(item["kind"])
            if model is None:
                errors.append((key, "kind is not registerable: " + item["kind"]))
                continue
        else:
            command = item.get("command")
            if command not in commands:
                errors.append((key, "unknown command"))
                continue
            try:
                commands[command].validate_json(json.dumps(bind(data)), strict=True)
            except ValidationError as exc:
                errors.append(
                    (
                        key,
                        "command fields: "
                        + str([(error["loc"], error["type"]) for error in exc.errors()]),
                    )
                )
            continue
        try:
            model.model_validate_json(json.dumps(bind(data)), strict=True)
        except ValidationError as exc:
            errors.append(
                (
                    key,
                    "schema fields: "
                    + str([(error["loc"], error["type"]) for error in exc.errors()]),
                )
            )
        if item.get("kind") == "material_source_v2":
            if data["evidence_digest"] not in {
                source_by_key[source]["sha256"]
                for source in item.get("source_keys", [])
                if source in source_by_key
            }:
                errors.append((key, "material evidence digest has no preserved source"))
    coverage_count = 0
    for row in document.get("inventory_replay_plan", {}).get("rows", []):
        for covered in row.get("source_coverage", []):
            source = source_by_key.get(covered.get("source_key"))
            if source is None or covered.get("sha256") != source["sha256"]:
                errors.append((row["key"], "inventory source identity mismatch"))
            coverage_count += 1
            for basis in covered.get("basis", []):
                template_key = basis.get("template_key")
                if template_key is not None and template_key not in fact_keys:
                    errors.append((row["key"], "inventory template not registered"))
    # Explicit dependencies must form a DAG; outputs depend on their producer.
    graph = {
        key: [
            dep
            for dep in (*item.get("depends_on", []), *item.get("publication_dependencies", []))
            if dep in known
        ]
        for key, item in indexed.items()
    }
    for key, publication in publication_nodes.items():
        dependencies = publication.get("depends_on", [])
        if any(dependency not in known for dependency in dependencies):
            errors.append((key, "publication references an unknown dependency"))
        graph[key] = dependencies
    for key, owner in output_owners.items():
        graph.setdefault(key, []).append(owner)
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            raise TemplateError("cyclic dependency at: " + key)
        if key in visited:
            return
        visiting.add(key)
        for child in graph.get(key, []):
            visit(child)
        visiting.remove(key)
        visited.add(key)

    for key in graph:
        visit(key)
    if errors:
        raise TemplateError(
            "\n".join(key + ": " + error for key, error in errors[:60])
            + f"\nTotal failures: {len(errors)}"
        )
    sources = verify_facts_sources(root, document)
    return {
        "file": path.name,
        "sources": len(source_keys),
        "entities": len(document.get("entities", [])),
        "facts": len(document.get("facts", [])),
        "materials": len(document.get("materials", [])),
        "steps": len(document.get("steps", [])),
        "references": references,
        "publication_events": len(publication_nodes),
        "inventory_source_coverage": coverage_count,
        "source_verification": sources,
        "unresolved": len(document.get("unresolved", [])),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "business_writes": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", type=Path, help="Directory containing private *.final-facts.json templates"
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    files = sorted(args.directory.glob("*.final-facts.json"))
    if not files:
        parser.error("no final-facts templates found")
    try:
        results = [check_document(path, args.root) for path in files]
    except (TemplateError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"status": "verified_offline", "templates": results}, ensure_ascii=False, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
