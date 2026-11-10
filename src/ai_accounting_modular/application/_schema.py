"""Discover assembled capabilities and select one request from the shared contract truth."""

from ai_accounting_modular.contracts.public import (
    COMMAND_REQUESTS,
    CONTRACT,
    ContractError,
    command_request_schema,
    fact_input_schema,
    validate_contract,
)


def result(application, request):
    validate_contract("core.schema.json", "SchemaRequest", request)
    supported = application.registry.supported_kinds
    commands = [name for name in COMMAND_REQUESTS if callable(getattr(application, name, None))]
    wanted = tuple(dict.fromkeys(request.get("kinds", ())))
    command = request.get("command")
    if command is not None and command not in commands:
        raise ContractError("unsupported_business", "当前实例没有此公开业务入口。")
    unknown = sorted(set(wanted) - set(supported))
    if unknown:
        raise ContractError("unsupported_business", "当前实例尚未支持：" + "、".join(unknown))
    response = {"contract": CONTRACT}
    if command is not None:
        selected = sorted(wanted) if command == "accounting_apply" else None
        response.update(
            command=command, request_schema=command_request_schema(command, kinds=selected)
        )
        if selected is not None:
            response["kinds"] = selected
    elif "kinds" in request:
        response.update(
            supported_kinds=list(supported),
            schemas=[{"kind": kind, "schema_document": fact_input_schema(kind)} for kind in wanted],
        )
    else:
        response.update(commands=commands, supported_kinds=list(supported))
    return validate_contract("core.schema.json", "SchemaResult", response)
