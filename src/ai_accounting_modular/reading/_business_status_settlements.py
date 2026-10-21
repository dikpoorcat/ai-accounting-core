"""Use ledger positions and the same economic identities as the open-items projection."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import ContractError, QuerySelection, format_fen
from ai_accounting_modular.ledger.opening.public import (
    opening_obligation_references,
    opening_payroll_references,
)

from . import _brief_open_positions, _brief_open_rows, _brief_open_sources
from ._context import last_period


def key(reference):
    return reference["business_id"], reference["slot"]


@dataclass(frozen=True)
class Settlements:
    historical: dict
    current: dict
    positions: tuple
    current_positions: tuple
    allowed_sources: dict
    meanings: dict
    position_rows: tuple
    strict: bool


def _positions(reader, identifiers, cutoff, selection, read, references=()):
    result = []
    keys = references or tuple(sorted(identifiers))
    for batch in _brief_open_sources.batches(keys):
        result.extend(
            reader.ledger.get_obligation_positions(
                cutoff,
                read,
                selection=selection,
                **({"references": batch} if references else {"business_ids": batch}),
            )
        )
    return tuple(result)


def _meanings(selected, positions):
    selected.cache.load_sources(item.definition.source for item in positions)
    contents = {body.source["result_id"]: body for body in selected.cache.saved.values()}
    opening = {
        body.source["result_id"]: {
            key(item.source): item
            for item in (*opening_obligation_references(body), *opening_payroll_references(body))
        }
        for body in contents.values()
        if body.target["kind"] == "opening.package"
    }
    return {
        (item.definition.source["result_id"], key(item.definition.reference)): (
            contents[item.definition.source["result_id"]],
            _brief_open_rows._meaning(
                contents[item.definition.source["result_id"]], item.definition, opening
            ),
        )
        for item in positions
    }


def _wire(item, content, meaning):
    definition, values = item.definition, item.amounts
    return {
        "key": definition.reference["business_id"] + ":" + definition.reference["slot"],
        "name": "net"
        if meaning.component == "net_pay"
        else meaning.component or definition.reference["slot"],
        "direction": definition.value["direction"],
        "category_key": meaning.category,
        "source_period": meaning.payroll_period or content.reference["business_period"],
        "source_amount_fen": format_fen(values.amount_fen),
        "paid_fen": format_fen(values.money_settled_fen),
        "other_settled_fen": format_fen(_brief_open_rows._other(values)),
        "remaining_fen": format_fen(values.remaining_fen),
        "settlement_status": _brief_open_rows.status(
            values.remaining_fen, values.money_settled_fen, _brief_open_rows._other(values)
        ),
    }


def _summary(cutoff, items):
    statuses = {item["settlement_status"] for item in items}
    checking = any(item["remaining_fen"] is None for item in items)
    return {
        "cutoff_period": cutoff,
        "status": "checking"
        if checking
        else "over_settled"
        if "over_settled" in statuses
        else "complete",
        "checking": checking,
        "obligations": items,
    }


def _cooperation(reader, scope, selected, current_cutoff, read):
    position = selected.range.get("position")
    content = selected.content
    contract = position["contract_id"] if position else None
    if contract is None and content is not None:
        kind, values = content.target["kind"], content.target.get("values", {})
        if kind == "investment.cooperation_contract":
            contract = content.reference["business_id"]
        elif kind.startswith("investment.cooperation_"):
            contract = values.get("contract_id")
    if contract is None:
        return (), scope["period_to"]
    historical_scope = scope
    historical = reader.investment.cooperation_positions(
        historical_scope, read, contract_ids=(contract,)
    ).positions
    exact_position = (
        position is not None
        or content is not None
        and content.target["kind"] == "investment.cooperation_position"
    )
    if exact_position and not any(item.source == content.source for item in historical):
        month = content.reference["posting_period"]
        historical_scope = dict(scope) | {"period_from": month, "period_to": month, "as_of": month}
        historical = reader.investment.cooperation_positions(
            historical_scope, read, contract_ids=(contract,)
        ).positions
        if not any(item.source == content.source for item in historical):
            raise ContractError("snapshot_changed", "无法定位原合作应补的正式位置，请刷新原页面。")
    current = reader.investment.cooperation_positions(
        {
            "company_id": read.company_id,
            "period_from": current_cutoff,
            "period_to": current_cutoff,
            "view": "current",
        },
        read,
        contract_ids=(contract,),
    ).positions
    rows = _brief_open_positions.project(
        historical,
        current,
        company_id=read.company_id,
        cutoff_period=historical_scope["period_to"],
        identity=reader.identity,
        read=read,
    )
    if position:
        rows = [
            row
            for row in rows
            if row["_detail_scope"]["position"]["field"] == position["field"]
            and row["_detail_scope"]["position"]["operator_id"] == position["operator_id"]
            and row["_detail_scope"]["position"]["project_id"] == position["project_id"]
        ]
    return tuple(rows), historical_scope["period_to"]


def capture(reader, scope, selected, read):
    month = scope["period_to"]
    cutoff = max(last_period(reader, read) or month, month)
    chosen = selected.range
    entries = [*chosen.get("allocations", ()), *chosen.get("obligations", ())]
    expected = {}
    for entry in entries:
        identity = key(entry["source"])
        source = entry["producer"]
        if identity in expected and expected[identity] != source:
            raise ContractError("storage_corrupt", "业务分项包含冲突的义务采用身份。")
        expected[identity] = source
    subject = selected.business["state"]["business_id"]
    identifiers = {identity[0] for identity in expected} or {subject}
    references = tuple({"business_id": business, "slot": slot} for business, slot in expected)
    historical = _positions(
        reader, identifiers, month, QuerySelection("as_posted", month), read, references
    )
    current = _positions(reader, identifiers, cutoff, QuerySelection("current"), read, references)
    if not expected and selected.content is not None:
        candidates = historical
        if not any(item.definition.source == selected.content.source for item in candidates):
            original_month = selected.content.reference["posting_period"]
            candidates = _positions(
                reader,
                identifiers,
                original_month,
                QuerySelection("as_posted", original_month),
                read,
                references,
            )
        expected = {
            key(item.definition.reference): selected.content.source
            for item in candidates
            if item.definition.reference["business_id"] == subject
            and item.definition.source == selected.content.source
        }
    if expected:

        def matches(item):
            return expected.get(key(item.definition.reference)) == item.definition.source

        historical = tuple(item for item in historical if matches(item))
        if {key(item.definition.reference) for item in historical} != set(expected):
            reference = chosen.get("reference")
            if reference is None:
                raise ContractError("snapshot_changed", "原业务义务已变化，请重新打开业务。")
            month = reference["posting_period"]
            historical = tuple(
                item
                for item in _positions(
                    reader, identifiers, month, QuerySelection("as_posted", month), read, references
                )
                if matches(item)
            )
            if {key(item.definition.reference) for item in historical} != set(expected):
                raise ContractError(
                    "snapshot_changed", "无法定位原业务分项采用时的完整义务，请刷新原页面。"
                )
    else:
        historical = ()
    strict = selected.detail is not None or bool(chosen.get("allocations"))
    if strict:
        current = tuple(item for item in current if key(item.definition.reference) in expected)
    meanings = _meanings(selected, (*historical, *current))
    current_by_ref = {key(item.definition.reference): item for item in current}
    before, after, allowed = [], [], {}
    contents = {body.source["result_id"]: body for body in selected.cache.saved.values()}
    withdrawn = _brief_open_sources._withdrawn(
        reader, scope, historical, current_by_ref, contents, cutoff, read
    )
    for item in historical:
        definition = item.definition
        ident = key(definition.reference)
        body, meaning = meanings[definition.source["result_id"], ident]
        before.append(_wire(item, body, meaning))
        allowed.setdefault(ident, []).append(definition.source)
        latest = current_by_ref.get(ident)
        if latest is not None:
            actual_body, actual_meaning = meanings[latest.definition.source["result_id"], ident]
            same = _brief_open_rows._economic_identity(
                body, definition, meaning
            ) == _brief_open_rows._economic_identity(actual_body, latest.definition, actual_meaning)
        else:
            same = False
        if strict and not same:
            after.append(
                before[-1]
                | {
                    "source_amount_fen": None,
                    "paid_fen": None,
                    "other_settled_fen": None,
                    "remaining_fen": "0",
                    "settlement_status": "withdrawn"
                    if (definition.reference["business_id"], body.reference["result_slot"])
                    in withdrawn
                    else "reversed",
                }
            )
        elif latest is not None:
            after.append(_wire(latest, actual_body, actual_meaning))
            allowed[ident].append(latest.definition.source)
    if not strict:
        represented = {item["key"] for item in after}
        for item in current:
            body, meaning = meanings[
                item.definition.source["result_id"], key(item.definition.reference)
            ]
            row = _wire(item, body, meaning)
            if row["key"] not in represented:
                after.append(row)
            allowed.setdefault(key(item.definition.reference), []).append(item.definition.source)
    position_rows, position_cutoff = _cooperation(reader, scope, selected, cutoff, read)
    if position_rows and not historical:
        month = position_cutoff
    for row in position_rows:
        base = {
            "key": row["id"],
            "name": "primary",
            "direction": row["_detail_scope"]["direction"],
            "category_key": row["category_key"],
            "source_period": row["source_period"],
            "source_amount_fen": None,
            "paid_fen": None,
            "other_settled_fen": None,
            "remaining_fen": row["outstanding_fen"],
            "settlement_status": row["status"],
        }
        before.append(base)
        after.append(
            base
            | {
                "remaining_fen": row["current_outstanding_fen"],
                "settlement_status": row["current_status"] or "checking",
            }
        )
    return Settlements(
        _summary(month, before),
        _summary(cutoff, after),
        historical,
        current,
        allowed,
        meanings,
        position_rows,
        strict,
    )
