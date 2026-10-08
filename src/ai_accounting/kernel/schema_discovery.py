"""On-demand wire contracts without changing the complete runtime validators."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import KernelError

REGISTRATION_COMMANDS = frozenset({"save_fact", "amend_fact", "save_facts"})


class SchemaQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    view: Literal["overview", "selected", "full"] = "overview"
    fact_kinds: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    response_types: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def selections(self):
        selected = bool(self.fact_kinds or self.commands or self.response_types)
        if self.view == "selected" and not selected:
            raise ValueError("selected requires at least one explicit selection")
        if self.view != "selected" and selected:
            raise ValueError("selections are only supported by the selected view")
        if REGISTRATION_COMMANDS.intersection(self.commands) and not self.fact_kinds:
            raise ValueError("registration command discovery requires fact_kinds")
        return self


def _fact_schema(kind, model):
    from .entity_references import declarations_for

    result = model.model_json_schema() | {"x-references": list(declarations_for(kind))}
    if model.registration_command:
        result["x-registration-command"] = model.registration_command
    if model.material_amount_aliases:
        result["x-material-amount-aliases"] = model.material_amount_aliases
    return result


def _references(value):
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            yield reference.removeprefix("#/$defs/")
        discriminator = value.get("discriminator", {})
        for reference in discriminator.get("mapping", {}).values():
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                yield reference.removeprefix("#/$defs/")
        for key, child in value.items():
            if key != "$defs":
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _prune_definitions(schema):
    """Keep the transitive local reference closure of a self-contained contract."""
    definitions = schema.get("$defs", {})
    needed, pending = set(), list(_references(schema))
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        if name not in definitions:
            raise RuntimeError(f"schema has an unresolved definition: {name}")
        needed.add(name)
        pending.extend(_references(definitions[name]))
    if needed:
        schema["$defs"] = {name: definitions[name] for name in sorted(needed)}
    else:
        schema.pop("$defs", None)
    return schema


def _registration_schema(adapter, command, fact_kinds):
    from .command_schema import selected_registration_schema

    return _prune_definitions(selected_registration_schema(adapter, command, fact_kinds))


def discover_schema(metadata, registry, command_models, query, *, response_adapters):
    """Return a directory, explicitly selected contracts, or the full contract.

    ``metadata`` contains protocol/runtime details, excluding generated fact,
    command and response contracts. Adapters remain complete and unmodified.
    """
    query = query if isinstance(query, SchemaQuery) else SchemaQuery.model_validate(query)
    for field, selected, known in (
        ("fact_kinds", query.fact_kinds, registry.models),
        ("commands", query.commands, command_models),
        ("response_types", query.response_types, response_adapters),
    ):
        unknown = sorted(set(selected) - set(known))
        if unknown:
            raise KernelError("invalid_command", "接口合同选择不存在", field=field, unknown=unknown)
    # Selected contracts are reused within a conversation/build. The initial
    # overview carries the operating instructions; do not retransmit them here.
    if query.view == "selected":
        result = {
            key: metadata[key]
            for key in ("format", "build_id", "build_identity", "database_formats")
            if key in metadata
        }
        result["agent_operating_protocol_version"] = metadata["agent_operating_protocol"]["version"]
        result["error_handling"] = {
            "version": metadata["error_handling"]["version"],
            "instruction": "按status、code、fact_issues和resolution处理；先查可复用来源，"
            "技术错误不直接转为业务追问；响应未确认时核对原请求或工作稿版本",
        }
    else:
        result = dict(metadata)
    result.update(
        view=query.view,
        schema_query=SchemaQuery.model_json_schema(),
        schema_discovery={
            "version": 2,
            "entry": "finance_local_schema / schema",
            "views": ["overview", "selected", "full"],
            "selection_fields": ["fact_kinds", "commands", "response_types"],
            "registration_rule": "选择通用登记命令时必须明确fact_kinds；专用事实使用其指定入口",
        },
    )
    if query.view != "full":
        for key in ("security_request_schema", "publication_contract", "period_close_contract"):
            result.pop(key, None)
        if "replay_close_contract" in result:
            result["replay_close_contract"] = {
                key: value for key, value in result["replay_close_contract"].items()
                if key != "scope_schema"
            }
    if query.view == "overview":
        result["facts"] = {
            kind: {
                "title": model.model_config.get("title") or model.__name__,
                "lane": model.lane,
                "material_category": model.material_category,
                "registration_commands": (
                    [model.registration_command] if model.registration_command
                    else sorted(REGISTRATION_COMMANDS)
                ),
            }
            for kind, model in sorted(registry.models.items())
        }
        result["commands"] = sorted(command_models)
        result["response_types"] = sorted(response_adapters)
        return result

    kinds = set(registry.models) if query.view == "full" else set(query.fact_kinds)
    commands = set(command_models) if query.view == "full" else set(query.commands)
    responses = set(response_adapters) if query.view == "full" else set(query.response_types)
    generic_kinds = {kind for kind in kinds if not registry.models[kind].registration_command}
    if query.view == "selected":
        commands.update(
            registry.models[kind].registration_command for kind in kinds
            if registry.models[kind].registration_command
        )
        if not generic_kinds:
            commands.difference_update(REGISTRATION_COMMANDS)
        result["selection"] = {
            "requested": query.model_dump(exclude={"view"}),
            "fact_kinds": sorted(kinds),
            "commands": sorted(commands),
            "response_types": sorted(responses),
        }
    result["facts"] = {kind: _fact_schema(kind, registry.models[kind]) for kind in sorted(kinds)}
    result["commands"] = sorted(commands)
    result["command_schemas"] = {
        command: (
            _registration_schema(command_models[command], command, generic_kinds)
            if query.view == "selected" and command in REGISTRATION_COMMANDS
            else command_models[command].json_schema()
        )
        for command in sorted(commands)
    }
    result["response_schemas"] = {
        name: response_adapters[name].json_schema() for name in sorted(responses)
    }
    return result
