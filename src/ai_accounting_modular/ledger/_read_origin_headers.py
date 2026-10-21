"""One capture-local binding of checked formal headers and their exact public sources."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract_items

from ._planning import source_digest
from ._types import ObligationDirectorySource
from ._values import fail

_HEAD_FIELDS = (
    "result_id",
    "business_id",
    "result_slot",
    "generation",
    "business_period",
    "posting_period",
    "state",
    "baseline_result_id",
    "calculation_key",
    "calculation_revision",
    "basis_digest",
    "target_digest",
)


def checked_items(schema, definition, values, *, fields=None):
    try:
        return validate_contract_items(schema, definition, values, fields=fields)
    except ContractError as error:
        if error.code == "invalid_contract":
            raise
        raise ContractError("storage_corrupt", "正式读取字段与合同不一致。") from error


def head_signature(row):
    return tuple((type(row[field]), row[field]) for field in _HEAD_FIELDS)


def source_headers(rows):
    """Build and check each actual result/generation once; never cache across calls."""
    heads, signatures = {}, {}
    for row in rows:
        identity = row["result_id"]
        signature = head_signature(row)
        if identity in signatures and signatures[identity] != signature:
            fail("storage_corrupt", "同一读快照的正式来源身份或代次相互冲突。")
        if identity not in heads:
            heads[identity], signatures[identity] = row, signature
    sources, references = [], []
    for row in heads.values():
        references.append(
            {
                field: row[field]
                for field in (
                    "business_id",
                    "result_slot",
                    "result_id",
                    "generation",
                    "business_period",
                    "posting_period",
                    "state",
                )
            }
        )
        if row["baseline_result_id"] is not None:
            references[-1]["baseline_result_id"] = row["baseline_result_id"]
        sources.append(
            {
                "business_id": row["business_id"],
                "result_slot": row["result_slot"],
                "result_id": row["result_id"],
                "result_generation": row["generation"],
                "content_digest": source_digest(
                    row["calculation_key"],
                    row["calculation_revision"],
                    row["basis_digest"],
                    row["target_digest"],
                ),
            }
        )
    checked_items("core.schema.json", "ResultRef", references)
    checked_items("business-facts.schema.json", "ResultSourceRef", sources)
    return tuple(
        ObligationDirectorySource(reference, source)
        for reference, source in zip(references, sources, strict=True)
    ), heads
