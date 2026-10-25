"""Identity parts contain exactly one item; older aggregate files remain valid inputs."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract


@pytest.mark.parametrize("field,prefix", [("notes", "notes:"), ("sources", "sources:")])
@pytest.mark.parametrize("count", [0, 1, 2])
def test_identity_document_part_has_exactly_one_item(field, prefix, count):
    source_id = str(uuid4())
    item = (
        {"scope": "company", "text": "已核实的说明。", "evidence_ids": []}
        if field == "notes"
        else {
            "source_id": source_id,
            "kind": "owner_statement",
            "text": "已确认的现实信息。",
            "sha256": "a" * 64,
        }
    )
    value = {
        "format": "ai-accounting-standard/1",
        "key": prefix + ("company" if field == "notes" else source_id),
        field: [item] * count,
    }
    if count == 1:
        validate_contract("core.schema.json", "StandardPart", value)
    else:
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "StandardPart", value)


@pytest.mark.parametrize("field", ["notes", "sources"])
def test_existing_empty_aggregate_remains_a_readable_input(field):
    validate_contract(
        "core.schema.json",
        "StandardPart",
        {"format": "ai-accounting-standard/1", "key": field, field: []},
    )
