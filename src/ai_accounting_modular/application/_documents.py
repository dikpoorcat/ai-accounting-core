"""Export owner facts, then publish files outside the accounting transaction."""

from ai_accounting_modular.contracts.public import ContractError, schema_document
from ai_accounting_modular.storage.public import company_metadata

from ._work_scope import working_period


def export_objects(application, read):
    objects = []
    kinds = schema_document("core.schema.json")["$defs"]["Object"]["oneOf"]
    for variant in kinds:
        query = {
            "action": "find",
            "company_id": read.company_id,
            "kind": variant["properties"]["kind"]["const"],
            "limit": 500,
        }
        while True:
            page = application.identity.find(query, read)
            objects.extend(page["objects"])
            if not page["page"]["has_more"]:
                break
            query["cursor"] = page["page"]["next_cursor"]
    return objects


def company_identity(read):
    metadata = company_metadata(read)
    return {
        "credit_code": metadata.credit_code,
        "legal_name": metadata.legal_name,
        "accounting_standard": metadata.accounting_standard,
        "currency": metadata.currency,
        "start_period": metadata.start_period,
        "working_period_to": working_period(read),
    }


def capture(application, read):
    scopes = application.records.document_status(read)["pending_scopes"]
    heads = []
    for key in scopes:
        if key.startswith("facts:"):
            _, family, period = key.split(":")
            heads.extend(read.repository("application").active_heads(family, period))
    facts = application.registry.export_heads(heads, read)
    objects = export_objects(application, read) if "objects" in scopes else None
    return application.records.capture_documents(
        company_identity(read), facts, objects, read, scopes=scopes
    )


def finish(application, company_id, *, verify_ready=False):
    expected_heads = None
    # Normal work stays incremental. An actually detected broken collection
    # receives one full repair attempt against the then-current saved facts.
    for repairing in (False, True):
        try:
            with application.storage.maintenance_unit(company_id) as unit:
                if repairing:
                    application.records.request_document_repair(unit)
                else:
                    application.records.ensure_document_targets(unit)
            with application.storage.read_unit(company_id) as read:
                expected_heads = application.records.document_target_heads(read)
                status = application.records.document_status(read)
                if verify_ready and status["state"] == "ready":
                    with application.records.lease_document_set(read):
                        return status
                snapshot = capture(application, read)
            prepared = application.records.prepare_documents(snapshot)
            with application.storage.maintenance_unit(company_id) as unit:
                return application.records.publish_documents(prepared, unit)
        except (ContractError, OSError) as error:
            if not repairing and getattr(error, "code", None) in {
                "documents_corrupt",
                "documents_missing",
            }:
                continue
            if expected_heads is None:
                raise
            with application.storage.maintenance_unit(company_id) as unit:
                application.records.record_document_error(
                    sorted(expected_heads),
                    getattr(error, "code", "temporary_failure"),
                    str(error),
                    unit,
                    expected_heads=expected_heads,
                )
            with application.storage.read_unit(company_id) as read:
                return application.records.document_status(read)
