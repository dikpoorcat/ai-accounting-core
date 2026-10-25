"""Hydrate only visible precise voucher descriptors, shared by lists and group expansion."""

import base64

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    canonical_json,
    content_digest,
    format_fen,
    parse_fen,
)
from ai_accounting_modular.ledger.public import VoucherLocator

from . import _brief_vouchers_display, _monthly
from . import _business_descriptions as descriptions
from ._brief_activity_amounts import amount, recognition, signed
from ._brief_activity_parts import category, parts
from ._brief_activity_sources import Contents, same_reference
from ._business_labels import business_label
from ._collections import CollectionFilter, wire_page


def locator(entry):
    return {
        "voucher_id": entry.voucher_id,
        "result_id": entry.result["result_id"],
        "generation": entry.result["generation"],
    }


def token(value):
    """Opaque exact identity, with no dependency on mutable number or summary text."""
    return base64.urlsafe_b64encode(canonical_json(value).encode()).decode().rstrip("=")


def assets(content):
    kind, values = content.target["kind"], content.target.get("values", {})
    cards = []
    if kind == "assets.acquisition":
        cards = [
            {
                "asset_id": item["asset_id"],
                "asset_type": item["asset_type"],
                "amount_fen": item["cost_fen"],
            }
            for item in values["assets"]
        ]
    elif kind == "assets.consumption":
        types = {item["asset_id"]: item["asset_type"] for item in content.basis["adopted_cards"]}
        cards = [
            {
                "asset_id": item["asset_id"],
                "asset_type": types[item["asset_id"]],
                "amount_fen": item["amount_fen"],
            }
            for item in values["items"]
        ]
    elif kind == "assets.disposal":
        adopted = [
            q
            for q in content.basis["adopted_quantities"]
            if q["type"] == "asset_card" and q["asset_id"] == values["asset_id"]
        ]
        if len(adopted) == 1:
            cards = [
                {
                    "asset_id": values["asset_id"],
                    "asset_type": adopted[0]["asset_type"],
                    "amount_fen": values["gross_proceeds_fen"],
                }
            ]
    return cards


def descriptor(entry, content, members, *, reverses=None, party_ids=()):
    label, value = business_label(content), amount(content)
    titles = list(dict.fromkeys(item["row"]["description"] for item in members))
    summary = "、".join(titles) or label.title
    voucher_summary = entry.summary
    if content.target["kind"] == "assets.activation":
        cards = content.target["values"]["assets"]
        voucher_summary = (
            "、".join(
                f"{count}项{name}"
                for kind, name in (("fixed", "固定资产"), ("intangible", "无形资产"))
                if (count := sum(card["asset_type"] == kind for card in cards))
            )
            + "启用"
        )
    sign = -1 if entry.role == "reversal" else 1
    group = (
        members[0]["row"]["group"]
        if members
        else category(content.target["kind"], "recognition", entry.role)
    )
    rec = recognition(content)
    return {
        "locator": locator(entry),
        "reference": content.reference,
        "party_ids": list(party_ids),
        "template": {
            "number": str(entry.number),
            "voucher_version_id": token(locator(entry)),
            "subject_id": entry.result["business_id"],
            "reverses_version_id": reverses,
            "date": rec["date"],
            "recognition": rec,
            "type": summary,
            "kind": content.target["kind"],
            "state": "冲正" if entry.role == "reversal" else "已入账",
            "group": group,
            "summary": voucher_summary,
            "list_summary": summary,
            "business_amount_fen": signed(value.amount_fen, sign),
            "business_amount_label": value.label,
            "business_amount_state": value.state,
            "has_business_progress": any(item["row"]["detail_scope_key"] for item in members),
        },
        "assets": assets(content),
    }


def for_descriptors(reader, descriptors, read):
    descriptors = list(descriptors)
    if not descriptors:
        return []
    if len(descriptors) > 500:
        raise ContractError("invalid_input", "一次最多展开500张精确凭证。")
    locators = [VoucherLocator(**item["locator"]) for item in descriptors]
    actual = reader.ledger.get_vouchers(locators, read)
    if len(actual) != len(descriptors):
        raise ContractError("storage_corrupt", "所选完整凭证数量与目录不一致。")
    codes = sorted({line["account_code"] for item in actual for line in item["lines"]})
    names = reader.ledger.account_names(codes)
    result = []
    for saved, voucher in zip(descriptors, actual, strict=True):
        if (
            voucher["voucher_id"] != saved["locator"]["voucher_id"]
            or voucher["result"]["result_id"] != saved["locator"]["result_id"]
            or voucher["result"]["generation"] != saved["locator"]["generation"]
        ):
            raise ContractError("storage_corrupt", "完整凭证与精确目录不一致。")
        cards = []
        for card in saved["assets"]:
            display = saved["display"]["assets"][card["asset_id"]]
            positions = [
                line["line_no"]
                for line in voucher["lines"]
                if line.get("object_id") == card["asset_id"]
            ]
            contiguous = bool(positions) and positions == list(
                range(positions[0], positions[-1] + 1)
            )
            cards.append(
                dict(card)
                | {
                    "name": display["name"],
                    "code": display["code"],
                    "calculation_id": voucher["result"]["result_id"],
                    "owner_calculation_id": voucher["result"]["result_id"],
                    "amount_label": saved["template"]["business_amount_label"],
                    "line_start": positions[0] if contiguous else None,
                    "line_count": len(positions) if contiguous else 0,
                }
            )
        card_by_id = {item["asset_id"]: item for item in cards}
        lines = []
        for line in voucher["lines"]:
            party_id = line.get("party_id")
            party_names = saved["display"]["parties"]
            if party_id is not None and party_id not in party_names:
                raise ContractError("storage_corrupt", "完整凭证交易方缺少正式依据中的显示身份。")
            known = party_id is not None and party_names[party_id] is not None
            name = party_names[party_id] if known else ""
            value = line["debit_fen"] if parse_fen(line["debit_fen"]) else line["credit_fen"]
            item = {
                "line_number": line["line_no"],
                "code": line["account_code"],
                "account": names[line["account_code"]],
                "debit_fen": line["debit_fen"],
                "credit_fen": line["credit_fen"],
                "party": name,
                "source_label": line["summary"],
                "parties": [{"id": party_id, "name": name, "amount_fen": value}]
                if party_id
                else [],
                "party_state": "known"
                if known
                else "name_missing"
                if party_id
                else "not_applicable",
            }
            card = card_by_id.get(line.get("object_id"))
            if card is not None:
                item["asset"] = card
            lines.append(item)
        single = cards[0] if len(cards) == 1 else None
        result.append(
            dict(saved["template"])
            | {
                # This field is the physical debit total, never the business amount.
                "amount_fen": format_fen(
                    sum(parse_fen(line["debit_fen"]) for line in voucher["lines"])
                ),
                "asset": {key: single[key] for key in ("asset_id", "asset_type", "name", "code")}
                if single
                else None,
                "asset_members": cards,
                "lines": lines,
            }
        )
    return result


def page(reader, snapshot, read, query):
    filters = CollectionFilter()
    if query.get("voucher_version_id"):
        filters = CollectionFilter(item_id=query["voucher_version_id"])
    elif query.get("voucher_number") is not None:
        filters = CollectionFilter(group_key=str(query["voucher_number"]))
    selected = _monthly.collection(
        snapshot,
        "brief_activity",
        "brief_vouchers",
        filters,
        PageQuery(query.get("limit", 20), query.get("cursor")),
        read,
    )
    value = wire_page(selected)
    value["items"] = for_descriptors(reader, selected.items, read)
    return value


def effective_headers(reader, references, read):
    """Preserve caller order while asking ledger, once per exact head, about inheritance."""
    references = list(references)
    unique = {}
    for ref in references:
        ident = ref["result_id"]
        if ident in unique and not same_reference(unique[ident], ref):
            raise ContractError("storage_corrupt", "同一正式结果出现不一致的精确定位。")
        unique[ident] = ref
    found, values = {}, list(unique.values())
    for start in range(0, len(values), 500):
        requested = values[start : start + 500]
        actual = reader.ledger.get_effective_voucher_directory(requested, read)
        if len(actual) != len(requested):
            raise ContractError("storage_corrupt", "正式结果的有效凭证目录不完整。")
        found.update((ref["result_id"], item) for ref, item in zip(requested, actual, strict=True))
    return tuple(found[ref["result_id"]] for ref in references)


def descriptors_for_references(reader, references, read, *, known_contents=()):
    """Per-input nullable descriptor, using ledger's exact effective journal inheritance."""
    entries = effective_headers(reader, references, read)
    cache = Contents(reader.ledger, read)
    cache.reuse(known_contents)
    cache.load([entry.result for entry in entries if entry is not None])
    unique = {token(locator(entry)): entry for entry in entries if entry is not None}
    prepared = {
        identity: parts(cache.get(entry.result), cache) for identity, entry in unique.items()
    }
    prose = descriptions.Descriptions(
        reader,
        (
            ref
            for identity, entry in unique.items()
            if entry.role != "reversal"
            for part in prepared[identity]
            for ref in descriptions.part_references(cache.get(entry.result), part, cache)
        ),
        read,
    )
    raw = []
    for identity, entry in unique.items():
        body = cache.get(entry.result)
        value = descriptor(
            entry,
            body,
            [],
            party_ids=_brief_vouchers_display.party_ids(body, prepared[identity], cache),
        )
        titles = [
            descriptions.part_display(body, part, cache, prose, entry.role)[0]
            for part in prepared[identity]
        ]
        if titles:
            value["template"]["type"] = value["template"]["list_summary"] = "、".join(
                dict.fromkeys(titles)
            )
        raw.append(value)
    bound = {
        item["template"]["voucher_version_id"]: item
        for item in _brief_vouchers_display.bind(reader, raw, read)
    }
    return tuple(None if entry is None else bound[token(locator(entry))] for entry in entries)


def display_head(reader, references, read):
    """Sparse dependency for historical unpaid sources, without persisting their descriptors."""
    return display_head_from_descriptors(descriptors_for_references(reader, references, read))


def display_head_from_descriptors(descriptors):
    return _brief_vouchers_display.head(descriptors)


def complete_page(items, version):
    return {
        "items": items,
        "page": {
            "total_count": len(items),
            "filtered_count": len(items),
            "returned_count": len(items),
            "has_more": False,
            "next_cursor": None,
            "collection_version": content_digest({"parent": version, "items": items}),
        },
    }
