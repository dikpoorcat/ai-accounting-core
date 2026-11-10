"""Capture registered material membership, independent of document file placement."""

from ai_accounting_modular.contracts.public import ContractError, content_digest, validate_contract

from ._changes import _missing_sources, source_ids
from ._types import CloseMaterials


def capture(first, last, read, explicit_sources):
    for period in (first, last):
        validate_contract("core.schema.json", "Month", period)
    if first > last:
        raise ContractError("invalid_input", "资料受核范围起月不能晚于末月。")
    selected = source_ids(explicit_sources, limit=None)
    rows = read.repository("records").close_material_metadata(first, last, selected)
    identities = tuple(row[0] for row in rows)
    _missing_sources(selected, identities)
    version = content_digest(
        {
            "company_id": read.company_id,
            "database_id": read.database_id,
            "period_from": first,
            "period_to": last,
            "source_ids": list(selected),
            "members": [list(row) for row in rows],
        }
    )
    return CloseMaterials(version, identities)
