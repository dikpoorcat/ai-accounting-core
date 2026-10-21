"""One normalized identity for cached and uncached financial consumers."""

from ai_accounting_modular.contracts.public import content_digest

from ._financial import normalize
from ._financial_projection_types import FINANCIAL_PROJECTION_FORMAT, FinancialProjectionRequest
from ._values import fail


def request(kind, scope, read):
    if kind not in ("summary_values", "snapshot"):
        fail("invalid_input", "财务投影须明确选择金额摘要或完整三表。")
    normalized = normalize(scope, read)
    key = content_digest(
        {
            "format": FINANCIAL_PROJECTION_FORMAT,
            "company_id": read.company_id,
            "database_id": read.database_id,
            "kind": kind,
            "scope": normalized,
        }
    )
    return FinancialProjectionRequest(kind, normalized, key, ("requests", key))
