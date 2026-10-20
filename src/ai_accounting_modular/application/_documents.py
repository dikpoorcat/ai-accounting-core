"""Export owner facts, then publish files outside the accounting transaction."""

from ai_accounting_modular.contracts.public import ContractError, schema_document


def capture(application, read):
    metadata = application.storage.company_metadata(read)
    scopes = application.records.document_status(read)["pending_scopes"]
    heads = []
    for key in scopes:
        if key.startswith("facts:"):
            _, family, period = key.split(":")
            heads.extend(read.repository("application").active_heads(family, period))
    facts = application.registry.export_heads(heads, read)
    objects = [] if "objects" in scopes else None
    kinds = schema_document("core.schema.json")["$defs"]["Object"]["oneOf"]
    for variant in kinds:
        if objects is None:
            break
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
    identity = {
        "credit_code": metadata.credit_code,
        "legal_name": metadata.legal_name,
        "accounting_standard": "small_enterprise",
        "currency": "CNY",
        "start_period": metadata.start_period,
        "working_period_to": max(
            metadata.start_period,
            read.repository("application").working_period_to() or metadata.start_period,
        ),
    }
    return application.records.capture_documents(identity, facts, objects, read, scopes=scopes)


def finish(application, company_id):
    expected_heads = None
    try:
        with application.storage.read_unit(company_id) as read:
            expected_heads = application.records.document_target_heads(read)
            snapshot = capture(application, read)
        prepared = application.records.prepare_documents(snapshot)
        with application.storage.maintenance_unit(company_id) as unit:
            return application.records.publish_documents(prepared, unit)
    except (ContractError, OSError) as error:
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
