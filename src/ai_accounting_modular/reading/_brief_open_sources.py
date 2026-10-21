"""Capture only outstanding obligations and their exact formal producer meanings."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import ContractError, PageQuery, QuerySelection
from ai_accounting_modular.ledger.public import (
    AdoptedContent,
    ObligationPositionQuery,
    ResourcePosition,
)

from ._context import last_period


def batches(values):
    values = tuple(values)
    for offset in range(0, len(values), 500):
        yield values[offset : offset + 500]


def ref_key(reference):
    return reference["business_id"], reference["slot"]


def _complete(ledger, scope, read):
    request, page = dict(scope), PageQuery(500)
    request["period_from"] = read.start_period
    while True:
        result = ledger.query_adopted_targets(
            request, ("opening.package",), page, read, result_slots=("main",)
        )
        if any(
            item.target["kind"] == "opening.package"
            and item.target["values"]["bookkeeping_start"] == read.start_period
            for item in result.items
        ):
            return True
        if result.next_cursor is None:
            return False
        page = PageQuery(500, result.next_cursor, result.version)


@dataclass(frozen=True)
class OpenSnapshot:
    selected: tuple[ResourcePosition, ...]
    current: dict[tuple[str, str], ResourcePosition]
    contents: dict[str, AdoptedContent]
    current_cutoff: str
    complete: bool
    withdrawn: set[tuple[str, str]]


def _withdrawn(reader, scope, selected, current, contents, cutoff, read):
    missing = {
        (
            item.definition.reference["business_id"],
            contents[item.definition.source["result_id"]].reference["result_slot"],
        )
        for item in selected
        if ref_key(item.definition.reference) not in current
    }
    result = set()
    for batch in batches(sorted({business for business, _ in missing})):
        request = {
            "company_id": scope["company_id"],
            "period_from": read.start_period,
            "period_to": cutoff,
            "view": "current",
        }
        page = PageQuery(500)
        slots = tuple(sorted({slot for business, slot in missing if business in batch}))
        while True:
            values = reader.ledger.query_adopted_targets(
                request, (), page, read, business_ids=batch, result_slots=slots
            )
            result.update(
                (item.reference["business_id"], item.reference["result_slot"])
                for item in values.items
                if item.target["kind"] == "system.withdrawal"
            )
            if values.next_cursor is None:
                break
            page = PageQuery(500, values.next_cursor, values.version)
    return result


def capture(reader, scope, read, *, business_ids=None, complete=None):
    month = scope["period_to"]
    if business_ids is None:
        selected = reader.ledger.capture_obligation_positions(
            ObligationPositionQuery(month, include_settled=False),
            read,
            selection=QuerySelection("as_posted", month),
        ).items
        complete = _complete(reader.ledger, scope, read)
    else:
        if type(complete) is not bool:
            raise TypeError("a partial capture retains its verified base completeness")
        selected = tuple(
            item
            for batch in batches(sorted(set(business_ids)))
            for item in reader.ledger.get_obligation_positions(
                month, read, business_ids=batch, selection=QuerySelection("as_posted", month)
            )
            if item.amounts.remaining_fen != 0
        )
    current_cutoff = last_period(reader, read) or month
    current = {}
    for batch in batches(sorted({item.definition.reference["business_id"] for item in selected})):
        current.update(
            (ref_key(item.definition.reference), item)
            for item in reader.ledger.get_obligation_positions(
                current_cutoff,
                read,
                business_ids=batch,
                selection=QuerySelection("current"),
            )
        )
    sources = {}
    for item in (*selected, *current.values()):
        source = item.definition.source
        existing = sources.setdefault(source["result_id"], source)
        if existing != source:
            raise ContractError("storage_corrupt", "同一读快照的往来来源身份不一致。")
    contents = {}
    for batch in batches(sources.values()):
        refs = reader.ledger.get_adopted_sources(batch, read)
        for content in reader.ledger.get_adopted_contents([item.reference for item in refs], read):
            contents[content.reference["result_id"]] = content
    return OpenSnapshot(
        selected,
        current,
        contents,
        current_cutoff,
        complete,
        _withdrawn(reader, scope, selected, current, contents, current_cutoff, read),
    )
