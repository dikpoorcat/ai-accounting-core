"""Actual settlements retain their economic purpose and exact origin."""

from ai_accounting_modular.ledger.opening.public import (
    OpeningInvestmentReference,
    opening_obligation_references,
)
from ai_accounting_modular.ledger.public import SettlementEventQuery

from ._common import amount, fail
from ._investment_events import EVENT_KINDS
from ._investment_read_sources import source_contents
from ._read_funds import _pages
from ._read_types import InvestmentEvent


def cash_events(ledger, scope, selected, read, cache):
    query = SettlementEventQuery(
        scope["period_from"],
        scope["period_to"],
        components=("money",),
        source_business_kinds=tuple(sorted(EVENT_KINDS | {"opening.package"})),
    )
    uses = list(
        _pages(lambda page: ledger.query_settlement_events(query, page, read, selection=selected))
    )
    producer_refs = {tuple(sorted(item.use.value["source"].items())) for item in uses}
    definitions = []
    refs = [dict(value) for value in producer_refs]
    for offset in range(0, len(refs), 500):
        definitions.extend(
            ledger.resource_definitions(
                refs[offset : offset + 500], "obligation", read, selection=selected
            )
        )
    definitions = [item for item in definitions if item.value["category"] == "investment"]
    eligible = {(item.reference["business_id"], item.reference["slot"]) for item in definitions}
    uses = [
        item
        for item in uses
        if (item.use.value["source"]["business_id"], item.use.value["source"]["slot"]) in eligible
    ]
    producers = source_contents(ledger, [item.source for item in definitions], read, cache)
    by_reference = {
        (item.reference["business_id"], item.reference["slot"]): producers[item.source["result_id"]]
        for item in definitions
    }
    consumers = source_contents(ledger, [item.use.source for item in uses], read, cache)
    metadata_by_result = {
        identity: {
            (item.source["business_id"], item.source["slot"]): item
            for item in opening_obligation_references(producer)
            if isinstance(item, OpeningInvestmentReference)
        }
        for identity, producer in producers.items()
        if producer.target["kind"] == "opening.package"
    }
    opening, term_sources = {}, []
    for definition in definitions:
        producer = producers[definition.source["result_id"]]
        if producer.target["kind"] != "opening.package":
            continue
        key = definition.reference["business_id"], definition.reference["slot"]
        metadata = metadata_by_result[definition.source["result_id"]].get(key)
        if (
            metadata is None
            or definition.value["account_code"] not in {"1131", "1132"}
            or definition.value["direction"] != "receivable"
        ):
            fail("期初投资收益实收缺少同源正式收益身份。", "storage_corrupt")
        sources = [
            source
            for source in producer.basis["source_results"]
            if source["business_id"] == metadata.terms_id and source["result_slot"] == "main"
        ]
        if len(sources) != 1:
            fail("期初投资收益实收缺少唯一的冻结投资条款。", "storage_corrupt")
        opening[key] = metadata, sources[0]
        term_sources.extend(sources)
    terms = source_contents(ledger, term_sources, read, cache)
    events = []
    for item in uses:
        if item.movement is None:
            fail("投资真实资金清偿缺少正式资金来源。", "storage_corrupt")
        reference = item.use.value["source"]
        producer = by_reference[reference["business_id"], reference["slot"]]
        key = reference["business_id"], reference["slot"]
        if key in opening:
            metadata, terms_source = opening[key]
            adopted_terms = terms[terms_source["result_id"]]
            if adopted_terms.target["kind"] != "investment.terms":
                fail("期初投资收益实收采用的冻结条款类别不一致。", "storage_corrupt")
            values = {
                "terms_id": metadata.terms_id,
                "product_id": adopted_terms.target["values"]["product_id"],
            }
        else:
            values = producer.target["values"]
        consumer = consumers[item.use.source["result_id"]]
        if item.movement.value["direction"] == "outflow":
            kind = "acquisition_payment"
        elif key in opening or reference["slot"] in {"income", "declared_income"}:
            kind = "income_receipt"
        elif producer.target["kind"] == "investment.capital_return":
            kind = "capital_return_receipt"
        else:
            kind = "disposal_receipt"
        events.append(
            InvestmentEvent(
                item.use.value["settlement_id"],
                item.use.source["business_id"],
                values["product_id"],
                kind,
                item.use.value["period"],
                producer.basis["period"],
                item.movement.value["actual_on"],
                item.use.source,
                consumer.reference,
                False,
                None,
                None,
                None,
                amount(item.use.value["amount_fen"]),
                item.movement.value["movement_id"],
                values["terms_id"],
            )
        )
    return events
