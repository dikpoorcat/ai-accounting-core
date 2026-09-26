"""Wire command schemas generated from entry signatures and registered fact types."""

import inspect
from functools import reduce
from operator import or_
from typing import Annotated, Any, Literal, get_type_hints

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, create_model

from .asset_batches import AssetBatches
from .backup import run_backup_jobs
from .business_queries import BusinessQueries
from .catalog import Catalog
from .close_review import CloseReview
from .contracts import KernelError, NeedsInformation
from .discovery import Discovery
from .display import Display
from .duplicates import DuplicateReview, Duplicates, SourceLocation
from .engine import Engine
from .entities import Entities, EntityProfile
from .exports import Exports
from .identity_corrections import IdentityCorrections
from .maintenance import Maintenance
from .materials import (
    MaterialGroupResolution,
    MaterialResolution,
    Materials,
    MaterialSource,
    Specification,
)
from .payroll_preparation import PayrollPreparation
from .periods import Periods
from .reports import Reports
from .tax_import import TaxImport
from .types import canonical
from .workflow import Workflow

CONFIG = ConfigDict(extra="forbid", strict=True)
Epochs = create_model(
    "StateVersions",
    __config__=CONFIG,
    accounting=(int, Field(ge=0)),
    material=(int, Field(ge=0)),
    management=(int, Field(ge=0)),
)


def command_models(registry):
    from .dashboard import Dashboard
    from .service import LocalService

    functions = {
        "prepare_fact_registration": Duplicates.prepare_fact_registration,
        "find_entities": Entities.find_entities,
        "register_entity": Entities.register_entity,
        "update_entity_profile": Entities.update_entity_profile,
        "preview_identity_correction": IdentityCorrections.preview_identity_correction,
        "confirm_identity_correction": IdentityCorrections.confirm_identity_correction,
        "create_company": Catalog.create_company,
        "restore_company": Catalog.restore_company,
        "company_settings": Catalog.company_settings,
        "configure_backup": Catalog.configure_backup,
        "preview": Engine.preview,
        "confirm": Engine.confirm,
        "overview": Engine.overview,
        "ledger": Engine.ledger,
        "trace": Engine.trace,
        "business_status": BusinessQueries.business_status,
        "period_readiness": BusinessQueries.period_readiness,
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
        "confirm_browser_report_export": Reports.confirm_browser_export,
        "workflow": Workflow.query,
        "obligation_basis": Workflow.obligation_basis,
        "prepare_obligations": Workflow.prepare_obligations,
        "confirm_obligations": Workflow.confirm_obligations,
        "backup": Engine.queue_backup,
        "run_jobs": run_backup_jobs,
        "run_export_jobs": run_backup_jobs,
        "run_report_jobs": run_backup_jobs,
        "evidence": Engine.register_evidence,
        "rebuild": Engine.rebuild_projections,
        "verify_integrity": Maintenance.verify_integrity,
        "repair_read_indexes": Maintenance.repair_read_indexes,
        "jobs": Engine.jobs,
        "request_result": Engine.request_result,
        "retry_job": Engine.retry_job,
        "operations": Catalog.operations,
        "inspect_material": Materials.inspect,
        "receive_material": Materials.receive,
        "resolve_material": Materials.resolve,
        "resolve_material_group": Materials.resolve_group,
        "material_completeness": Materials.check,
        "preview_material_allocation": Materials.preview_period_allocation,
        "confirm_material_allocation": Materials.confirm_period_allocation,
        "company_context": Discovery.company_context,
        "update_company_note": Discovery.update_company_note,
        "save_display_profile": Display.save_display_profile,
        "display_profiles": Display.display_profiles,
        "dashboard_context": LocalService.dashboard_context,
        "dashboard_brief": Dashboard.brief,
        "dashboard_funds": Dashboard.funds,
        "dashboard_employees": Dashboard.employees,
        "dashboard_assets": Dashboard.assets,
        "dashboard_business_status": Dashboard.business_status,
        "dashboard_quarterly_report": Dashboard.quarterly_report,
        "dashboard_period_preparation": Dashboard.period_preparation,
        "dashboard_close_review": CloseReview.read,
        "preview_period_commentary": Display.preview_period_commentary,
        "update_period_commentary": Display.update_period_commentary,
        "find_facts": Discovery.find_facts,
        "payroll_reuse_basis": PayrollPreparation.reuse_basis,
        "prepare_payroll": PayrollPreparation.prepare,
        "confirm_payroll_preparation": PayrollPreparation.confirm,
        "prepare_asset_activation_batch": AssetBatches.prepare_activation_batch,
        "confirm_asset_activation_batch": AssetBatches.confirm_activation_batch,
        "prepare_asset_consumption_month": AssetBatches.prepare_consumption_month,
        "confirm_asset_consumption_month": AssetBatches.confirm_consumption_month,
        "preview_tax_import": TaxImport.preview,
        "confirm_tax_import": TaxImport.confirm,
    }
    result = {}
    global_commands = {
        "create_company",
        "restore_company",
        "companies",
        "schema",
        "operations",
        "dashboard_context",
    }
    for name, function in functions.items():
        hints = get_type_hints(function, include_extras=True)
        fields = {} if name in global_commands else {"company_id": (str, ...)}
        for parameter in inspect.signature(function).parameters.values():
            if (
                parameter.name.startswith("_")
                or parameter.name in {"self", "database"}
                or (name == "backup" and parameter.name == "source")
            ):
                continue
            default = ... if parameter.default is inspect.Parameter.empty else parameter.default
            fields[parameter.name] = (hints.get(parameter.name, Any), default)
            if parameter.name == "epochs":
                fields[parameter.name] = (Epochs, default)
            elif parameter.name == "request_id":
                fields[parameter.name] = (str, Field(default=default, min_length=1, max_length=200))
        if name == "evidence":
            del fields["content"]
            fields["content_base64"] = (str, Field(max_length=((20 * 1024 * 1024 + 2) // 3) * 4))
        if name == "close":
            fields["approval_id"] = (str, Field(min_length=1, max_length=200))
        if name == "inspect_material":
            fields["specification"] = (Specification, ...)
        elif name == "receive_material":
            fields["data"] = (MaterialSource, ...)
        elif name == "resolve_material":
            fields["data"] = (MaterialResolution, ...)
        elif name == "resolve_material_group":
            fields["data"] = (MaterialGroupResolution, ...)
        elif name in {"register_entity", "update_entity_profile"}:
            fields["data"] = (EntityProfile, ...)
        result[name] = TypeAdapter(create_model(name + "Command", __config__=CONFIG, **fields))
    records = []
    for kind, model in registry.models.items():
        if model.registration_command:
            continue
        records.append(
            create_model(
                kind + "Registration",
                __config__=CONFIG,
                kind=(Literal[kind], ...),
                subject_id=(str, ...),
                data=(model, ...),
                evidence=(list[str], Field(min_length=1)),
                expected_revision=(int, Field(ge=0, strict=True)),
                review=(DuplicateReview | None, None),
                source_locations=(list[SourceLocation], Field(default_factory=list)),
            )
        )
    record = Annotated[reduce(or_, records), Field(discriminator="kind")]
    # Single and batch commands share exactly the same registered fact models.
    for name in ("save_fact", "amend_fact"):
        variants = []
        for registration in records:
            fields = {"company_id": (str, ...), "request_id": (str, ...)}
            if name == "amend_fact":
                fields["recording_error_confirmed"] = (Literal[True], ...)
            variants.append(
                create_model(name + registration.__name__, __base__=registration, **fields)
            )
        result[name] = TypeAdapter(Annotated[reduce(or_, variants), Field(discriminator="kind")])
    result["save_facts"] = TypeAdapter(
        create_model(
            "SaveFactsCommand",
            __config__=CONFIG,
            company_id=(str, ...),
            request_id=(str, ...),
            facts=(list[record], Field(min_length=1, max_length=5000)),
        )
    )
    for name in ("companies", "schema"):
        fields = {} if name in global_commands else {"company_id": (str, ...)}
        result[name] = TypeAdapter(create_model(name + "Command", __config__=CONFIG, **fields))
    return result


def command_schemas(registry):
    return {name: model.json_schema() for name, model in command_models(registry).items()}


def _registration_missing_issue(error, command, payload, registry):
    if command not in {"save_fact", "amend_fact", "save_facts"} or registry is None:
        return None
    location = error["loc"]
    if "data" not in location:
        return None
    field_path = location[location.index("data") + 1 :]
    if not field_path:
        return None
    item = payload
    prefix = ""
    if command == "save_facts":
        if len(location) < 2 or location[0] != "facts" or type(location[1]) is not int:
            return None
        index = location[1]
        facts = payload.get("facts")
        if not isinstance(facts, list) or index >= len(facts):
            return None
        item = facts[index]
        prefix = f"facts.{index}."
    if not isinstance(item, dict):
        return None
    kind = item.get("kind")
    model = registry.models.get(kind) if isinstance(kind, str) else None
    if model is None:
        return None
    field = ".".join(map(str, field_path))
    field_info = model.model_fields.get(str(field_path[0]))
    extra = field_info.json_schema_extra if field_info else None
    metadata = extra.get("x-accounting-fact", {}) if isinstance(extra, dict) else {}
    semantics = metadata.get(
        "role", "accounting" if model.lane == "accounting" else "management"
    )
    precision = metadata.get("precision")
    if precision is None and field_path[0] == "period":
        precision = "month"
    source = item.get("subject_id")
    return {
        "field": prefix + field,
        "message": "缺少必需管理资料" if semantics == "management" else "缺少必需核算事实",
        "semantics": semantics,
        "reusable_sources": [source] if isinstance(source, str) and source else [],
        "allowed_precision": [precision] if isinstance(precision, str) else [],
    }


def validate_command(models, command, payload, *, registry=None):
    if command not in models:
        raise KernelError("unknown_command", "不支持该业务命令")
    if not isinstance(payload, dict):
        raise KernelError("invalid_command", "命令必须是 JSON 对象")
    try:
        value = models[command].validate_json(canonical(payload), strict=True)
        return value.model_dump(mode="json")
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        missing = [
            issue
            for error in errors
            if error["type"] == "missing"
            if (issue := _registration_missing_issue(error, command, payload, registry)) is not None
        ]
        if missing:
            facts = payload.get("facts") if command == "save_facts" else [payload]
            kinds = (
                {
                    item["kind"]
                    for item in facts
                    if isinstance(item, dict) and isinstance(item.get("kind"), str)
                }
                if isinstance(facts, list)
                else set()
            )
            resolution = {"fact_kind": next(iter(kinds))} if len(kinds) == 1 else None
            raise NeedsInformation(
                missing, "缺少必需事实", resolution=resolution, registry=registry
            ) from exc
        raise KernelError("invalid_command", "命令字段或类型不符合接口约定", issues=errors) from exc
