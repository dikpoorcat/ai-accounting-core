"""Complete, precisely allocated settlement events; no voucher debit totals or fake cash."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    QuerySelection,
    format_fen,
)
from ai_accounting_modular.ledger.public import SettlementEventQuery

from ._brief_activity_sources import identities
from ._brief_open_sources import batches
from ._business_status_settlements import key
from ._funds_items import date_key


def capture(reader, selected, settlements, view, read):
    cutoff = (
        settlements.current["cutoff_period"]
        if view == "current"
        else settlements.historical["cutoff_period"]
    )
    selection = (
        QuerySelection("current") if view == "current" else QuerySelection("as_posted", cutoff)
    )
    references = [
        {"business_id": business, "slot": slot} for business, slot in settlements.allowed_sources
    ]
    events = []
    for batch in batches(references):
        page = PageQuery(500)
        while True:
            result = reader.ledger.query_business_settlements(
                SettlementEventQuery(read.start_period, cutoff, source_refs=batch),
                page,
                read,
                selection=selection,
            )
            events.extend(result.items)
            if result.next_cursor is None:
                break
            page = PageQuery(500, result.next_cursor, result.version)
    selected.cache.load_sources(event.use.source for event in events)
    picked = []
    for event in events:
        use, source = event.use, event.use.value["source"]
        consumer = selected.cache.saved[use.source["result_id"], use.source["result_generation"]]
        quantities = [
            item
            for item in consumer.basis["adopted_quantities"]
            if item["type"] == "obligation" and item["source"] == source
        ]
        if len(quantities) != 1:
            raise ContractError("storage_corrupt", "实际清偿事件缺少唯一的正式采用义务。")
        quantity = quantities[0]
        allowed = settlements.allowed_sources.get(key(source), ())
        adopted = [producer for producer in consumer.basis["source_results"] if producer in allowed]
        if not adopted:
            continue
        if len(adopted) != 1:
            raise ContractError("storage_corrupt", "实际清偿事件的业务采用身份不唯一。")
        _, meaning = settlements.meanings[adopted[0]["result_id"], key(source)]
        component = use.component
        negative = component == "reclassify_excess"
        period = use.value.get("period", consumer.reference["business_period"])
        identifier = use.value.get("settlement_id", use.value.get("relation_id"))
        if identifier is None:
            raise ContractError("storage_corrupt", "实际清偿事件缺少稳定身份。")
        value = {
            "id": identifier + ":" + source["business_id"] + ":" + source["slot"],
            "posting_period": period,
            "actual_on": use.value.get("actual_on"),
            "mode": {"money": "payment", "agency": "accepted", "offset": "offset"}.get(
                component, component
            ),
            "direction": -1 if negative else 1,
            "signed_amount_fen": format_fen((-1 if negative else 1) * int(use.value["amount_fen"])),
            "party": "",
            "purpose_label": meaning.description,
            "relation_state": "resolved",
            "funds_account_id": event.movement.value["funds_account_id"]
            if event.movement is not None
            else None,
        }
        picked.append((value, quantity.get("party_id")))
    objects = identities(reader.identity, [party for _, party in picked if party is not None], read)
    rows = []
    for row, party in picked:
        row["party"] = objects[party]["name"] if party in objects else ""
        rows.append(row)
    rows.sort(key=lambda row: date_key(row["posting_period"], row["actual_on"], "", row["id"]))
    if len({row["id"] for row in rows}) != len(rows):
        raise ContractError("storage_corrupt", "实际清偿范围出现重复事件身份。")
    return rows
