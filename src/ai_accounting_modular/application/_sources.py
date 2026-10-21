"""Source registration has its own bounded input, without executing accounting facts."""

from contextlib import ExitStack
from uuid import uuid4

from ai_accounting_modular.contracts.public import SourcesRequest, SourcesResult, validate_contract
from ai_accounting_modular.reading.public import ReadingImpact
from ai_accounting_modular.records.public import RecordsChange, RecordsContext

from . import _documents


def run(application, request: SourcesRequest) -> SourcesResult:
    validate_contract("core.schema.json", "SourcesRequest", request)
    company_id = request["company_id"]
    if request["action"] != "import":
        with application.storage.read_unit(company_id) as read:
            return application.records.sources(request, read)
    with ExitStack() as stack:
        with application.storage.read_unit(company_id) as read:
            sources = application.records.prepare_source_imports(request["sources"], read)
            prepared = stack.enter_context(application.records.prepare_sources(sources, read))
        with application.storage.write_unit(company_id) as unit:
            plan = application.records.plan_change(
                RecordsChange(sources=sources),
                RecordsContext(unit, str(uuid4()), prepared_sources=prepared),
            )
            application.records.apply_current(plan, unit)
            if plan.changed_source_ids:
                application.records.mark_document_targets(("sources",), unit)
                application._notify_reading(
                    ReadingImpact(
                        changes={
                            "families": [],
                            "business_ids": [],
                            "object_ids": [],
                            "periods": [],
                            "slots": [],
                        },
                        source_ids=tuple(plan.changed_source_ids),
                    ),
                    unit,
                )
    with application.storage.read_unit(company_id) as read:
        status = application.records.document_status(read)
    if status["state"] != "ready":
        _documents.finish(application, company_id)
    with application.storage.read_unit(company_id) as read:
        return application.records.sources(
            {
                "company_id": company_id,
                "action": "get",
                "source_ids": [source["source_id"] for source in sources],
            },
            read,
        )
