"""Compile documentary material and monthly close dependencies without database reads.

The compiler accepts source business keys only. Saved versions and calculations
are bound to the receipts of this run, never to an earlier reconstruction.
"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from pydantic import ValidationError

from .materials import (
    MaterialGroupResolution,
    MaterialPeriodAllocation,
    MaterialResolution,
    MaterialSource,
)
from .periods import MATERIAL_CATEGORIES
from .types import YearMonth, canonical


def _reference(value):
    if isinstance(value, str) and value.startswith("$ref:"):
        key, separator, binding = value[5:].rpartition(":")
        if separator and key and binding:
            return key, binding
    return None


def _references(value):
    result = _reference(value)
    if result:
        yield result
    elif isinstance(value, dict):
        for child in value.values():
            yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def material_amount_field_issues(document, *, registry=None):
    """Check fact amount dimensions without evaluating business or inventing results.

    Result values remain the runtime's responsibility: the registry does not
    declare a general schema for evaluator outputs. An alias never revives an
    absent fact field; its source must exist in the current input model.
    """
    if registry is None:
        from .service import default_registry

        registry = default_registry()
    input_models = registry.input_models
    kinds = {}
    for item in document.get("facts", ()):
        kinds.setdefault(item.get("key"), set()).add(item.get("kind"))
    for step in document.get("steps", ()):
        for output in step.get("outputs", ()):
            kinds.setdefault(output.get("key"), set()).add(output.get("kind"))
    allowed_by_kind = {}

    def integer_schema(schema, definitions, seen=()):
        reference = schema.get("$ref")
        if reference:
            if not reference.startswith("#/$defs/") or reference in seen:
                return False
            return integer_schema(
                definitions.get(reference.removeprefix("#/$defs/"), {}),
                definitions,
                (*seen, reference),
            )
        alternatives = schema.get("anyOf")
        if alternatives:
            non_null = [item for item in alternatives if item.get("type") != "null"]
            return bool(non_null) and all(
                integer_schema(item, definitions, seen) for item in non_null
            )
        return schema.get("type") == "integer"

    def links(value, path):
        if isinstance(value, dict):
            if "calculation_id" in value and "amount_field" in value:
                yield value, path
            for key, child in value.items():
                yield from links(child, path + "/" + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from links(child, path + f"/{index}")

    issues = []
    for item in document.get("materials", ()):
        for link, path in links(item.get("data", {}), item.get("key", "materials") + "/data"):
            amount_field = link["amount_field"]
            if not isinstance(amount_field, str) or not amount_field.startswith("fact."):
                continue
            reference = _reference(link["calculation_id"])
            if not reference or reference[1] != "calculation_id":
                continue  # The identity validator reports old or malformed references.
            candidates = kinds.get(reference[0], set())
            kind = next(iter(candidates)) if len(candidates) == 1 else None
            model = input_models.get(kind)
            allowed = []
            code = "material_amount_target_kind_unknown"
            if model is not None:
                if kind not in allowed_by_kind:
                    schema = model.model_json_schema()
                    allowed_by_kind[kind] = sorted(
                        "fact." + name
                        for name, field in schema.get("properties", {}).items()
                        if name.endswith("_fen")
                        and integer_schema(field, schema.get("$defs", {}))
                    )
                allowed = allowed_by_kind[kind]
                if amount_field in allowed:
                    continue
                code = "material_amount_field_invalid"
            issues.append(
                {
                    "path": path + "/amount_field",
                    "code": code,
                    "message": (
                        f"{reference[0]} ({kind}): {amount_field}; "
                        f"current integer fact amount fields: {allowed}"
                    ),
                    "target_key": reference[0],
                    "target_kind": kind,
                    "amount_field": amount_field,
                    "allowed_fields": allowed,
                }
            )
    return issues


def _step_id(step):
    return step["step_id"] if isinstance(step, dict) else step.step_id


def _dependencies(builder, item, material_keys):
    dependencies = set()
    for key in item.get("source_keys", ()):
        if key not in builder.source_digests:
            builder.issue(item["key"], "material_source_missing", f"资料来源未列入最终清单：{key}")
        if key in builder.source_steps:
            dependencies.add(builder.source_steps[key])
    for key in item.get("depends_on", ()):
        dependency = builder.dependency(key, published=False)
        if dependency:
            dependencies.add(dependency)
    published = {key for key, binding in _references(item["data"]) if binding == "calculation_id"}
    for key in item.get("publication_dependencies", ()):
        published.add(key.removeprefix("publication:"))
    for key in published:
        dependency = builder.dependency(key, published=True)
        if dependency:
            dependencies.add(dependency)
    for key, binding in _references(item["data"]):
        if key in material_keys:
            dependencies.add(builder.node_id(key))
        elif binding in {"fact_id", "revision", "subject_id"}:
            dependency = builder.dependency(key, published=False)
            if dependency:
                dependencies.add(dependency)
    return dependencies


def _check_digests(builder, value, path):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("evidence_digest") and child is not None:
                if child not in builder.source_digests.values():
                    builder.issue(
                        path + "/" + key,
                        "material_evidence_missing",
                        "资料依据摘要未列入本公司来源清单",
                    )
            else:
                _check_digests(builder, child, path + "/" + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_digests(builder, child, path + f"/{index}")


def _check_runtime_identities(builder, value, path):
    bindings = {
        "source_id": "subject_id",
        "source_fact_id": "fact_id",
        "duplicate_source_id": "subject_id",
        "calculation_id": "calculation_id",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if key in bindings and child is not None:
                reference = _reference(child)
                if not reference or reference[1] != bindings[key]:
                    builder.issue(
                        path + "/" + key,
                        "material_runtime_identity_forbidden",
                        "资料身份和正式计算只允许本轮逻辑引用，不能输入旧库编号",
                    )
            else:
                _check_runtime_identities(builder, child, path + "/" + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_runtime_identities(builder, child, path + f"/{index}")


def _duplicate_dependencies(builder, materials):
    """Locate the exact primary group or ordinary primary disposition."""
    groups, resolutions = {}, {}
    for item in materials:
        data = item["data"]
        if item["kind"] == "material_group_resolution":
            for member in data["members"]:
                groups.setdefault((data["source_id"], member["location"]), []).append(item)
        elif item["kind"] == "material_resolution_v2":
            resolutions.setdefault((data["source_id"], data["location"]), []).append(item)
    edges = {}
    for item in materials:
        data = item["data"]
        if data.get("treatment") != "duplicate":
            continue
        target = data.get("duplicate_source_id"), data.get("duplicate_location")
        candidates = groups.get(target, [])
        if data.get("links"):
            selected = data["links"]
            candidates = [
                group
                for group in candidates
                if _contains_complete_links(group["data"]["links"], selected)
            ]
        if not candidates and not data.get("links"):
            candidates = resolutions.get(target, [])
        if len(candidates) != 1:
            builder.issue(
                item["key"],
                "material_duplicate_target_not_unique",
                "复制资料必须唯一定位本轮主组或主资料处置",
            )
            continue
        edges.setdefault(item["key"], set()).add(builder.node_id(candidates[0]["key"]))
    for item in materials:
        if item["kind"] != "material_group_resolution":
            continue
        data = item["data"]
        members = [
            resolutions.get((data["source_id"], member["location"]), [])
            for member in data["members"]
        ]
        copied = [
            member
            for member in members
            if any(x["data"].get("treatment") == "duplicate" for x in member)
        ]
        if copied:
            if len(copied) != len(members) or any(len(member) != 1 for member in members):
                builder.issue(
                    item["key"],
                    "material_copy_members_incomplete",
                    "复制组必须逐项具备唯一的完整复制成员",
                )
            else:
                edges.setdefault(item["key"], set()).update(
                    builder.node_id(member[0]["key"]) for member in members
                )
    return edges


def _contains_complete_links(available, selected):
    def identity(link):
        return link["calculation_id"], link["amount_field"], link["amount_fen"]

    values = [identity(x) for x in available]
    choices = [identity(x) for x in selected]
    return len(set(choices)) == len(choices) and all(values.count(x) == 1 for x in choices)


def _merge_declared_allocations(builder, document, materials):
    """Consume every explicit pair, sharing an identical material declaration."""
    declared = [
        item
        for item in document.get("steps", ())
        if item.get("command") in {"preview_material_allocation", "confirm_material_allocation"}
    ]
    previews = {
        item["key"]: item for item in declared if item["command"] == "preview_material_allocation"
    }
    if len({item["key"] for item in declared}) != len(declared):
        builder.issue("steps", "material_allocation_step_duplicate", "归期步骤键必须唯一")
    sources = {item["key"]: item for item in materials if item["kind"] == "material_source_v2"}
    by_source, consumed = {}, set()
    for item in materials:
        if item["kind"] == "material_period_allocation":
            by_source.setdefault(item["data"]["source_id"], []).append(item)
    for confirmation in declared:
        if confirmation["command"] != "confirm_material_allocation":
            continue
        key, data = confirmation["key"], confirmation.get("data", {})
        reference = _reference(data.get("preview_digest"))
        preview = previews.get(reference[0]) if reference else None
        if preview is None or reference[1] not in {"digest", "preview_digest"}:
            builder.issue(
                key, "material_allocation_preview_missing", "确认必须引用唯一声明的归期预览"
            )
            continue
        preview_key, preview_data = preview["key"], preview.get("data", {})
        if preview_key in consumed:
            builder.issue(key, "material_allocation_preview_reused", "每项归期预览只确认一次")
            continue
        consumed.add(preview_key)
        assignments = preview_data.get("assignments")
        if (
            data.get("source_id") != preview_data.get("source_id")
            or not isinstance(assignments, list)
            or not assignments
            or ("assignments" in data and data["assignments"] != assignments)
        ):
            builder.issue(
                key, "material_allocation_pair_mismatch", "预览和确认须有同一来源及完整相同归期"
            )
            continue
        allowed = {
            "source_id",
            "assignments",
            "company_id",
            "expected_revision",
            "preview_digest",
            "epochs",
            "request_id",
        }
        if set(data) - allowed or set(preview_data) - {"source_id", "assignments", "company_id"}:
            builder.issue(key, "material_allocation_field_unsupported", "归期步骤含不支持字段")
            continue
        if (
            any(
                body.get("company_id", "$ref:target_company:company_id")
                != "$ref:target_company:company_id"
                for body in (data, preview_data)
            )
            or ("epochs" in data and data["epochs"] != f"$ref:{preview_key}:epochs")
            or ("request_id" in data and data["request_id"] != f"$ref:{key}:request_id")
        ):
            builder.issue(
                key,
                "material_allocation_runtime_input_forbidden",
                "公司、时期版本及请求键必须使用本轮归期步骤的明确逻辑引用",
            )
            continue
        source_ref = _reference(data["source_id"])
        source = sources.get(source_ref[0]) if source_ref else None
        if source is None or source_ref[1] != "subject_id":
            builder.issue(key, "material_allocation_source_missing", "归期必须使用本轮声明资料来源")
            continue
        outputs = confirmation.get("outputs", [])
        if not outputs or any(
            output.get("kind") != "material_period_allocation"
            or not isinstance(output.get("key"), str)
            for output in outputs
        ):
            builder.issue(key, "material_allocation_output_invalid", "归期确认须明确声明类型化输出")
            continue
        allocation_data = {
            "period": source["data"]["period"],
            "source_id": data["source_id"],
            "source_fact_id": f"$ref:{source_ref[0]}:fact_id",
            "entries": deepcopy(assignments),
        }
        try:
            normalized = MaterialPeriodAllocation.model_validate_json(canonical(allocation_data))
        except ValidationError as error:
            builder.issue(key, "material_template_invalid", str(error))
            continue
        existing = by_source.get(data["source_id"], [])
        matches = [
            item
            for item in existing
            if MaterialPeriodAllocation.model_validate_json(canonical(item["data"])).entries
            == normalized.entries
        ]
        if existing and (len(matches) != 1 or len(existing) != 1):
            builder.issue(
                key, "material_allocation_source_conflict", "同一资料的归期声明冲突或不唯一"
            )
            continue
        if matches:
            allocation = matches[0]
        else:
            allocation = {
                "key": outputs[0]["key"],
                "kind": "material_period_allocation",
                "data": allocation_data,
                "source_keys": deepcopy(source.get("source_keys", [])),
                "depends_on": [],
            }
            if any(item["key"] == allocation["key"] for item in materials):
                builder.issue(key, "material_key_duplicate", "归期输出键与其他资料冲突")
                continue
            materials.append(allocation)
            by_source[data["source_id"]] = [allocation]
        allocation.setdefault("_allocation_aliases", []).append(
            (preview_key, key, [output["key"] for output in outputs])
        )
        dependencies = set(allocation.get("depends_on", [])) | set(preview.get("depends_on", []))
        dependencies.update(confirmation.get("depends_on", []))
        dependencies.add(source_ref[0])
        dependencies.difference_update({preview_key, key, allocation["key"]})
        allocation["depends_on"] = sorted(dependencies)
    for key in set(previews) - consumed:
        builder.issue(key, "material_allocation_confirmation_missing", "声明的归期预览缺少确认步骤")


def compile_materials_and_closes(builder, document):
    """Append material receipts, complete inventories and chronological closes."""
    for issue in material_amount_field_issues(document):
        builder.issue(
            issue["path"],
            issue["code"],
            issue["message"],
            **{
                key: value
                for key, value in issue.items()
                if key not in {"path", "code", "message"}
            },
        )
    material_models = {
        model.kind: model
        for model in (
            MaterialSource,
            MaterialResolution,
            MaterialGroupResolution,
            MaterialPeriodAllocation,
        )
    }
    materials = []
    for index, item in enumerate(document.get("materials", [])):
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            builder.issue(
                f"materials/{index}", "material_template_invalid", "资料模板必须有稳定业务键"
            )
            continue
        model = material_models.get(item.get("kind"))
        if model is None:
            builder.issue(item["key"], "material_command_unsupported", "资料种类不受支持")
            continue
        try:
            model.model_validate_json(canonical(item.get("data")))
        except ValidationError as error:
            for issue in error.errors(include_input=False, include_url=False):
                path = item["key"] + "/" + "/".join(str(x) for x in issue["loc"])
                builder.issue(path, "material_template_invalid", issue["msg"])
            continue
        if any(existing["key"] == item["key"] for existing in materials):
            builder.issue(item["key"], "material_key_duplicate", "资料模板业务键重复")
            continue
        materials.append(deepcopy(item))
    _merge_declared_allocations(builder, document, materials)
    kinds = {
        "material_source_v2": "receive_material",
        "material_resolution_v2": "resolve_material",
        "material_group_resolution": "resolve_material_group",
        "material_period_allocation": "confirm_material_allocation",
    }
    keys = {item["key"] for item in materials}
    # Allocate identities for this compilation, before resolving forward references.
    for item in materials:
        key = item["key"]
        if (key, "subject_id") in builder.logical_ids:
            builder.issue(
                key, "material_identity_preexisting", "资料主体标识必须由本轮编译重新创建"
            )
        if item["kind"] != "material_period_allocation":
            builder.logical_ids[key, "subject_id"] = str(uuid4())
        builder.save_steps[key] = builder.node_id(key)
        for preview_key, confirm_key, output_keys in item.get("_allocation_aliases", ()):
            builder.save_steps[preview_key] = builder.node_id(key + ":preview")
            for alias in [confirm_key, *output_keys]:
                builder.save_steps[alias] = builder.node_id(key)
    copy_edges = _duplicate_dependencies(builder, materials)
    allocations = {
        _reference(x["data"].get("source_id"))[0]: x["key"]
        for x in materials
        if x["kind"] == "material_period_allocation" and _reference(x["data"].get("source_id"))
    }
    received = {}
    for item in materials:
        key, kind = item["key"], item["kind"]
        data = deepcopy(item["data"])
        command = kinds.get(kind)
        if command is None or item.get("registration_command", command) != command:
            builder.issue(key, "material_command_unsupported", "资料种类或专用登记入口不受支持")
            continue
        _check_digests(builder, data, key)
        _check_runtime_identities(builder, data, key)
        dependencies = _dependencies(builder, item, keys) | copy_edges.get(key, set())
        if kind in {"material_resolution_v2", "material_group_resolution"}:
            source = _reference(data.get("source_id"))
            if source and source[0] in allocations:
                dependencies.add(builder.node_id(allocations[source[0]]))
        evidence = sorted(
            {
                builder.source_digests[x]
                for x in item.get("source_keys", ())
                if x in builder.source_digests
            }
        )
        if kind == "material_period_allocation":
            preview_key = key + ":preview"
            preview = builder.add_step(
                preview_key,
                "preview_material_allocation",
                {
                    "source_id": data["source_id"],
                    "assignments": data["entries"],
                },
                depends_on=tuple(sorted(dependencies)),
                outputs={
                    "digest": "digest",
                    "epochs": "epochs",
                    "expected_revision": "expected_revision",
                },
            )
            step = builder.add_step(
                key,
                command,
                {
                    "source_id": data["source_id"],
                    "assignments": data["entries"],
                    "preview_digest": f"$ref:{preview_key}:digest",
                    "epochs": f"$ref:{preview_key}:epochs",
                    "expected_revision": f"$ref:{preview_key}:expected_revision",
                },
                depends_on=(preview,),
                outputs={"subject_id": "subject_id", "fact_id": "fact_id", "revision": "revision"},
            )
            for preview_alias, confirm_alias, output_keys in item.get("_allocation_aliases", ()):
                for binding, field in (
                    ("preview_digest", "digest"),
                    ("digest", "digest"),
                    ("epochs", "epochs"),
                    ("expected_revision", "expected_revision"),
                ):
                    builder.bind(preview_alias, binding, preview_key, field)
                for alias in [confirm_alias, *output_keys]:
                    for binding in ("subject_id", "fact_id", "revision"):
                        builder.bind(alias, binding, key, binding)
        else:
            outputs = {"fact_id": "fact_id", "revision": "revision"}
            if kind == "material_source_v2":
                outputs.update(
                    allocation_fact_id="allocation_fact_id",
                    allocation_revision="allocation_revision",
                )
                if data["evidence_digest"] not in evidence:
                    builder.issue(
                        key,
                        "material_original_evidence_missing",
                        "资料接收必须引用本公司实际原件摘要",
                    )
                received.setdefault((data["period"], data["category"]), set()).add(
                    data["evidence_digest"]
                )
            step = builder.add_step(
                key,
                command,
                {
                    "subject_id": f"$ref:{key}:subject_id",
                    "data": data,
                    "evidence": evidence,
                    "expected_revision": 0,
                },
                depends_on=tuple(sorted(dependencies)),
                outputs=outputs,
            )
        builder.save_steps[key] = step
    _compile_inventories_and_closes(builder, document, received)


def _compile_inventories_and_closes(builder, document, received):
    scope = document["scope"]
    first, last = YearMonth(scope["first_period"]), YearMonth(scope["last_period"])
    periods = [str(YearMonth.from_ordinal(x)) for x in range(first.ordinal, last.ordinal + 1)]
    plan = document.get("inventory_replay_plan", {})
    if plan.get("source_supporting_templates") or plan.get(
        "source_inspection_and_template_required"
    ):
        builder.issue(
            "inventory_replay_plan",
            "inventory_source_preparation_incomplete",
            "清单仍有未完成的原件接收或解析模板",
        )
    rows = plan.get("rows", [])
    by_period = {period: {} for period in periods}
    all_received = {item for values in received.values() for item in values}
    for row in rows:
        period, category = row.get("period"), row.get("category")
        if (
            period not in by_period
            or category not in MATERIAL_CATEGORIES
            or category in by_period[period]
        ):
            builder.issue(
                row.get("key", "inventory"), "inventory_scope_invalid", "清单月份、类别越界或重复"
            )
            continue
        by_period[period][category] = row
    before_inventories = tuple(_step_id(step) for step in builder.steps)
    inventory_steps = []
    for period in periods:
        entries = []
        for category in MATERIAL_CATEGORIES:
            row = by_period[period].get(category)
            if row is None:
                builder.issue(
                    f"inventory/{period}/{category}",
                    "inventory_category_missing",
                    "每月必须完整提供六类清单",
                )
                continue
            basis = row.get("basis", {})
            source_key = basis.get("confirmation_source_key", basis.get("source_key"))
            confirmation = basis.get("confirmation_sha256")
            if not source_key or builder.source_digests.get(source_key) != confirmation:
                builder.issue(
                    row["key"],
                    "inventory_confirmation_missing",
                    "清单确认必须匹配本公司保全来源摘要",
                )
            evidence = set(received.get((period, category), ()))
            for coverage in row.get("source_coverage", ()):
                sha = coverage.get("sha256")
                if (
                    builder.source_digests.get(coverage.get("source_key")) != sha
                    or sha not in all_received
                ):
                    builder.issue(
                        row["key"],
                        "inventory_coverage_not_received",
                        "清单覆盖原件必须有本轮资料接收节点及一致摘要",
                    )
                if sha:
                    evidence.add(sha)
            no_business = row.get("no_business")
            if (
                type(no_business) is not bool
                or (no_business and (evidence or not basis.get("explicit_no_business_passage")))
                or (not no_business and not evidence)
            ):
                builder.issue(
                    row["key"],
                    "inventory_business_basis_invalid",
                    "无业务须有明确原话且无资料；有业务须有实际覆盖资料",
                )
            if row.get("missing_information"):
                builder.issue(
                    row["key"], "inventory_information_missing", "清单覆盖仍有未决资料事实"
                )
            entries.append(
                {
                    "period": period,
                    "category": category,
                    "evidence": sorted(evidence),
                    "expected": len(evidence),
                    "no_business": no_business,
                    "confirmation_evidence": confirmation,
                }
            )
        key = "inventory-batch:" + period
        inventory_steps.append(
            builder.add_step(
                key,
                "inventory_batch",
                {"inventories": entries},
                depends_on=before_inventories,
                outputs={"inventory_results": "results"},
            )
        )
        for index, entry in enumerate(entries):
            builder.bind(
                f"inventory:{period}:{entry['category']}",
                "inventory_id",
                key,
                f"results.{index}.inventory_id",
            )
    close_dependencies = before_inventories + tuple(inventory_steps)
    previous = None
    for period in periods:
        preview_key = "close-preview:" + period
        dependencies = close_dependencies + ((previous,) if previous else ())
        preview = builder.add_step(
            preview_key,
            "preview_close",
            {"period": period, "owner_confirmation": builder.owner_confirmation},
            depends_on=dependencies,
            outputs={"digest": "digest", "epochs": "epochs"},
        )
        previous = builder.add_step(
            "close:" + period,
            "close",
            {
                "period": period,
                "owner_confirmation": builder.owner_confirmation,
                "preview_digest": f"$ref:{preview_key}:digest",
                "epochs": f"$ref:{preview_key}:epochs",
            },
            depends_on=(preview,),
        )
