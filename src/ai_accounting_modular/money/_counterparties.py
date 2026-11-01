"""Real parties and sourced company-owned channels share one business meaning."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    QuerySelection,
    validate_contract,
)


def counterparty_meaning(*, party_ids=(), internal_transfer=False, own_funds_channel=None):
    parties = list(dict.fromkeys(party for party in party_ids if party is not None))
    if internal_transfer:
        if parties or own_funds_channel is not None:
            raise ContractError("storage_corrupt", "公司内部划转不能同时带有外部对象或备用金渠道。")
        return {"state": "not_applicable", "reason": "internal_transfer"}
    if parties:
        if own_funds_channel is not None:
            raise ContractError("storage_corrupt", "真实外部对象与公司自有资金渠道不能同时采用。")
        return {"state": "known", "party_ids": parties}
    if own_funds_channel is not None:
        return {
            "state": "not_applicable",
            "reason": "own_funds_channel",
            "own_funds_channel": deepcopy(own_funds_channel),
        }
    return {"state": "missing"}


def own_funds_channels(owner, references, read):
    requested = {}
    for reference in references:
        for field in ("business_id", "result_id"):
            validate_contract("core.schema.json", "Id", reference[field])
        validate_contract("core.schema.json", "Slot", reference["result_slot"])
        generation = reference["generation"]
        if type(generation) is not int or generation < 1:
            raise ContractError("invalid_input", "资金渠道须指定有效正式结果版本。")
        key = reference["result_id"], generation
        identity = reference["business_id"], reference["result_slot"]
        previous = requested.setdefault(key, identity)
        if previous != identity:
            raise ContractError("invalid_input", "同一正式结果不能对应不同业务身份。")
    ids = sorted({business for business, slot in requested.values() if slot == "main"})
    result = {}
    for start in range(0, len(ids), 500):
        channels = read.repository("money").own_funds_channels(ids[start : start + 500])
        if not channels:
            continue
        current = owner._ledger.get_result_refs(
            tuple(channels), read, selection=QuerySelection("current")
        )
        for reference in current:
            key = reference["result_id"], reference["generation"]
            identity = reference["business_id"], reference["result_slot"]
            if reference["result_slot"] == "main" and requested.get(key) == identity:
                result[key] = channels[reference["business_id"]]
    return result
