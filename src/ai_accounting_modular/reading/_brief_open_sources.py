"""Capture only outstanding obligations and their exact formal producer meanings."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger.public import (
    AdoptedContent,
    AdoptedOriginProjection,
    ObligationDirectoryItem,
    ObligationDirectoryQuery,
)

from ._brief_activity_sources import same_reference
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
    selected: tuple[ObligationDirectoryItem, ...]
    current: dict[tuple[str, str], ObligationDirectoryItem]
    contents: dict[str, AdoptedOriginProjection | AdoptedContent]
    current_cutoff: str
    complete: bool
    withdrawn: set[tuple[str, str]]


def _withdrawn(reader, scope, references, cutoff, read):
    missing = {(reference["business_id"], reference["result_slot"]) for reference in references}
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
        complete = _complete(reader.ledger, scope, read)
    elif type(complete) is not bool:
        raise TypeError("a partial capture retains its verified base completeness")
    current_cutoff = last_period(reader, read) or month
    directory = reader.ledger.capture_obligation_directory(
        ObligationDirectoryQuery(
            month,
            current_cutoff,
            include_origins=True,
            business_ids=None if business_ids is None else tuple(sorted(set(business_ids))),
            selected_view="as_posted",
        ),
        read,
    )
    selected = directory.selected
    current = {ref_key(item.reference): item for item in directory.current}
    contents = directory.origins
    if contents is None or any(
        (origin := contents.get(source.reference["result_id"])) is None
        or source.source != origin.source
        or not same_reference(source.reference, origin.reference)
        for source in directory.sources
    ):
        raise ContractError("storage_corrupt", "往来目录缺少对应的精确正式来源。")
    return OpenSnapshot(
        selected,
        current,
        contents,
        current_cutoff,
        complete,
        _withdrawn(
            reader,
            scope,
            [
                contents[item.source["result_id"]].reference
                for item in selected
                if ref_key(item.reference) not in current
            ],
            current_cutoff,
            read,
        ),
    )
