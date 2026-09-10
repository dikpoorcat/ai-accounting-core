"""Wire command schemas generated from entry signatures and registered fact types."""

import inspect
from copy import deepcopy
from functools import reduce
from operator import or_
from typing import Annotated, Any, Literal, get_type_hints

from pydantic import ConfigDict, Field, create_model

from .backup import create_portable, run_backup_jobs
from .catalog import Catalog
from .engine import Engine
from .exports import Exports
from .periods import Periods
from .reports import Reports
from .workflow import Workflow

CONFIG = ConfigDict(extra="forbid", strict=True)


def command_schemas(registry):
    functions = {
        "create_company": Catalog.create_company,
        "restore_company": Catalog.restore_company,
        "preview": Engine.preview,
        "confirm": Engine.confirm,
        "overview": Engine.overview,
        "ledger": Engine.ledger,
        "trace": Engine.trace,
        "management": Periods.management,
        "inventory": Periods.inventory,
        "preview_close": Periods.preview_close,
        "close": Periods.close,
        "closed_report": Periods.closed_report,
        "preview_delete": Engine.preview_delete,
        "delete": Engine.delete,
        "save_payee": Exports.save_payee,
        "preview_export": Exports.preview,
        "confirm_export": Exports.confirm,
        "report": Reports.report,
        "preview_report_export": Reports.preview_export,
        "confirm_report_export": Reports.confirm_export,
        "workflow": Workflow.query,
        "obligation_basis": Workflow.obligation_basis,
        "backup": create_portable,
        "run_jobs": run_backup_jobs,
        "run_export_jobs": run_backup_jobs,
        "run_report_jobs": run_backup_jobs,
        "evidence": Engine.register_evidence,
        "rebuild": Engine.rebuild_projections,
        "jobs": Engine.jobs,
    }
    result = {}
    global_commands = {"create_company", "restore_company", "companies", "schema"}
    for name, function in functions.items():
        hints = get_type_hints(function)
        fields = {} if name in global_commands else {"company_id": (str, ...)}
        for parameter in inspect.signature(function).parameters.values():
            if parameter.name in {"self", "database"} or (
                name == "backup" and parameter.name == "source"
            ):
                continue
            default = ... if parameter.default is inspect.Parameter.empty else parameter.default
            fields[parameter.name] = (hints.get(parameter.name, Any), default)
        if name == "evidence":
            del fields["content"]
            fields["content_base64"] = (str, ...)
        result[name] = create_model(
            name + "Command", __config__=CONFIG, **fields
        ).model_json_schema()
    records = []
    for kind, model in registry.models.items():
        records.append(
            create_model(
                kind + "Registration",
                __config__=CONFIG,
                kind=(Literal[kind], ...),
                subject_id=(str, ...),
                data=(model, ...),
                evidence=(list[str], Field(min_length=1)),
                expected_revision=(int, Field(ge=0, strict=True)),
            )
        )
    record = Annotated[reduce(or_, records), Field(discriminator="kind")]
    # Single and batch commands share exactly the same registered fact models.
    record_schema = create_model(
        "FactRegistration", __config__=CONFIG, fact=(record, ...)
    ).model_json_schema()
    single = {
        "title": "save_factCommand",
        "allOf": [
            {
                "type": "object",
                "properties": {"company_id": {"type": "string"}, "request_id": {"type": "string"}},
                "required": ["company_id", "request_id"],
            },
            record_schema["properties"]["fact"],
        ],
        "$defs": record_schema["$defs"],
    }
    # Registration alternatives accept company/request metadata only in this single envelope.
    for definition in single["$defs"].values():
        if definition.get("title", "").endswith("Registration"):
            definition["properties"].update(
                company_id={"type": "string"}, request_id={"type": "string"}
            )
    result["save_fact"] = single
    amendment = deepcopy(single)
    amendment["title"] = "amend_factCommand"
    amendment["allOf"][0]["properties"]["recording_error_confirmed"] = {"const": True}
    amendment["allOf"][0]["required"].append("recording_error_confirmed")
    for definition in amendment["$defs"].values():
        if definition.get("title", "").endswith("Registration"):
            definition["properties"]["recording_error_confirmed"] = {"const": True}
    result["amend_fact"] = amendment
    result["save_facts"] = create_model(
        "SaveFactsCommand",
        __config__=CONFIG,
        company_id=(str, ...),
        request_id=(str, ...),
        facts=(list[record], Field(min_length=1, max_length=5000)),
    ).model_json_schema()
    for name in ("companies", "schema"):
        fields = {} if name in global_commands else {"company_id": (str, ...)}
        result[name] = create_model(
            name + "Command", __config__=CONFIG, **fields
        ).model_json_schema()
    return result
