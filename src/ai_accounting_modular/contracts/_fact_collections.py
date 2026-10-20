"""The three normalized current-fact collections have one integrity rule."""

from copy import deepcopy

from ._errors import ContractError
from ._validation import content_digest, validate_contract

_COLLECTIONS = {
    "money.bank_statement": "rows",
    "money.platform_activity": "rows",
    "opening.package": "members",
}
_DIGEST = "collection_digest"


def _check_field(fact, field):
    if fact["kind"] not in _COLLECTIONS or field != _COLLECTIONS[fact["kind"]]:
        raise ValueError("not a declared normalized fact collection")


def split_fact_collection(fact, field):
    """Return the sole parent projection and the separately stored ordered body."""
    validate_contract("business-facts.schema.json", "Fact", fact)
    _check_field(fact, field)
    parent = deepcopy(fact)
    items = parent["data"].pop(field)
    parent[_DIGEST] = content_digest(items)
    return parent, items


def restore_fact_collection(parent, field, items):
    """Reject lost, altered or reordered children before exporting a complete Fact."""
    try:
        _check_field(parent, field)
        value = deepcopy(parent)
        expected = value.pop(_DIGEST)
        validate_contract("core.schema.json", "Digest", expected)
        if type(items) is not list or field in value["data"]:
            raise ValueError("duplicated or malformed normalized collection")
        if content_digest(items) != expected:
            raise ValueError("normalized collection differs from its saved digest")
        value["data"][field] = deepcopy(items)
        validate_contract("business-facts.schema.json", "Fact", value)
        return value
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "规范化事实的完整明细或校验摘要不一致。") from error
