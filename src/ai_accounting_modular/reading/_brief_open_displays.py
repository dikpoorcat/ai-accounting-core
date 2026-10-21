"""Bind actual effective voucher displays without computing off-page templates or amounts."""

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import AdoptedContent, AdoptedOriginProjection

from . import _brief_vouchers as vouchers
from . import _brief_vouchers_display as display
from ._brief_activity_parts import parts
from ._brief_activity_sources import Contents, same_reference
from ._brief_open_sources import batches

# These complete input shapes have no allocations or asset cards. Everything
# else keeps the existing full-body participant rules.
_SIMPLE = frozenset(
    {
        "operations.expense",
        "operations.service_sale",
        "operations.sale_return",
        "operations.expense_recovery",
        "operations.pass_through",
    }
)


def capture(reader, references, read, *, known_origins=()):
    entries = vouchers.effective_headers(reader, references, read)
    origins = {}
    for origin in known_origins:
        ref, source = origin.reference, origin.source
        old = origins.get(ref["result_id"])
        if (
            type(ref["generation"]) is not int
            or type(source["result_generation"]) is not int
            or source["result_generation"] != ref["generation"]
            or any(source[key] != ref[key] for key in ("business_id", "result_id", "result_slot"))
            or (old is not None and old != origin)
        ):
            raise ContractError("storage_corrupt", "复用显示来源的精确定位或完整身份相互冲突。")
        origins[ref["result_id"]] = origin
    owners = {}
    for entry in entries:
        if entry is None:
            continue
        ref = entry.result
        old = origins.get(ref["result_id"])
        if old is not None and not same_reference(old.reference, ref):
            raise ContractError("storage_corrupt", "有效凭证与已有正式来源的精确定位不一致。")
        owners[ref["result_id"]] = ref
    for batch in batches([ref for identity, ref in owners.items() if identity not in origins]):
        actual = reader.ledger.get_obligation_origins(batch, read)
        if len(actual) != len(batch) or any(
            not same_reference(origin.reference, ref)
            for origin, ref in zip(actual, batch, strict=True)
        ):
            raise ContractError("storage_corrupt", "有效凭证缺少精确的冻结来源。")
        origins.update((origin.reference["result_id"], origin) for origin in actual)
    cache = Contents(reader.ledger, read)
    cache.reuse(origin for origin in origins.values() if isinstance(origin, AdoptedContent))
    raw, unique = [], set()
    for entry in entries:
        if entry is None:
            continue
        identity = vouchers.token(vouchers.locator(entry))
        if identity in unique:
            continue
        origin = origins[entry.result["result_id"]]
        if isinstance(origin, AdoptedOriginProjection) and origin.target_fields["kind"] in _SIMPLE:
            parties, cards = display.party_ids(origin, (), cache), []
        else:
            cache.load((entry.result,))
            origin = cache.get(entry.result)
            parties = display.party_ids(origin, parts(origin, cache), cache)
            cards = vouchers.assets(origin)
        unique.add(identity)
        raw.append({"token": identity, "party_ids": parties, "assets": cards})
    bound = {item["token"]: item for item in display.bind(reader, raw, read)}
    return tuple(
        None if entry is None else bound[vouchers.token(vouchers.locator(entry))]
        for entry in entries
    )
