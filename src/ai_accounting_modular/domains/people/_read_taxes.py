"""Read later payroll-tax transfers and cutoff compositions from exact adoptions."""

from collections import defaultdict
from dataclasses import fields, replace

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    QuerySelection,
    RemunerationPositionItem,
    format_fen,
    parse_fen,
    validate_contract,
)

from ._common import total
from ._read_sources import Contents, batches, tax_payroll_basis
from ._read_types import RemunerationAmounts, RemunerationComposition, RemunerationEntry

TAX_KIND = "people.remuneration_tax"


def tax_entry(adopted, content, sign):
    values = content.target["values"]
    additional = parse_fen(values["additional_tax_fen"])
    quantity, source = tax_payroll_basis(content)
    amount = sign * additional
    return RemunerationEntry(
        f"{adopted['result_id']}:{adopted['generation']}:"
        + ("target" if sign == 1 else "baseline"),
        adopted,
        content.reference,
        content.source,
        sign,
        quantity["period"],
        values["person_id"],
        values["remuneration_type"],
        quantity.get("expense_area"),
        quantity.get("project_id"),
        values["basis_type"],
        values["tax_period"],
        RemunerationAmounts(tax_fen=amount, net_fen=-amount),
        source,
    )


def compositions(ledger, contents, sources, month, read, selection):
    """Select only tax targets that cite wages involved in this read, including zero targets."""
    references = {
        item.reference["business_id"]: {
            "business_id": item.reference["business_id"],
            "slot": "main",
        }
        for item in sources
    }
    contents.select(references)
    selected_sources = [
        source
        for identifier in sorted(references)
        if (content := contents.selected.get(identifier)) is not None
        and (source := contents.remuneration(content)) is not None
    ]
    targets = defaultdict(dict)
    scope = {
        "company_id": read.company_id,
        "period_from": read.start_period,
        "period_to": month,
        "view": selection.view,
    }
    if selection.as_of is not None:
        scope["as_of"] = selection.as_of
    for batch in batches(references.values()):
        page = PageQuery(500)
        while True:
            result = ledger.query_adopted_targets(
                scope, (TAX_KIND,), page, read, source_refs=batch, result_slots=("main",)
            )
            contents.load(item.reference for item in result.items)
            for item in result.items:
                content = contents.exact[item.reference["result_id"]]
                if content.target["kind"] == TAX_KIND:
                    values = content.target["values"]
                    tax_payroll_basis(content)
                    targets[values["remuneration_id"]][item.reference["business_id"]] = content
            if result.next_cursor is None:
                break
            page = PageQuery(500, result.next_cursor, result.version)
    result = []
    for source in selected_sources:
        taxes = sorted(
            targets[source.reference["business_id"]].values(),
            key=lambda item: (item.target["values"]["tax_period"], item.reference["business_id"]),
        )
        additional = total(parse_fen(item.target["values"]["additional_tax_fen"]) for item in taxes)
        assumed = total(
            parse_fen(item.target["values"]["additional_tax_fen"])
            for item in taxes
            if item.target["values"]["tax_period"] == source.tax_period
        )
        held = max(source.amounts.held_fen - assumed, 0)
        net = total((source.amounts.net_fen, -additional))
        states = {source.tax_period: source.tax_state} if source.tax_period else {}
        states.update(
            (item.target["values"]["tax_period"], item.target["values"]["basis_type"])
            for item in taxes
        )
        kinds = set(states.values()) or {source.tax_state}
        result.append(
            RemunerationComposition(
                source.source,
                tuple(item.source for item in taxes),
                replace(
                    source.amounts,
                    net_fen=net,
                    tax_fen=total((source.amounts.tax_fen, additional)),
                    held_fen=held,
                    disbursement_target_fen=total((net, -held)),
                ),
                next(iter(kinds)) if len(kinds) == 1 else "mixed",
            )
        )
    return tuple(result)


def remuneration_positions(
    ledger, business_ids, period, read, *, view="current"
) -> tuple[RemunerationPositionItem, ...]:
    """Read selected wages and their exact unpaid capacity without building a monthly roster."""
    if ledger is None:
        raise ContractError("unsupported_business", "薪酬读取尚未装配正式账务读取端口。")
    validate_contract("core.schema.json", "Month", period)
    if (
        not isinstance(business_ids, tuple | list)
        or not 1 <= len(business_ids) <= 500
        or view not in ("current", "as_posted")
    ):
        raise ContractError(
            "invalid_input", "薪酬可付额须指定1至500个不重复报酬身份和明确读取口径。"
        )
    for identity in business_ids:
        validate_contract("core.schema.json", "Id", identity)
    if len(set(business_ids)) != len(business_ids):
        raise ContractError("invalid_input", "薪酬可付额不能重复指定同一报酬。")
    selection = QuerySelection(view, period if view == "as_posted" else None)
    contents = Contents(ledger, read, selection)
    contents.select(business_ids)
    active, withdrawn = [], set()
    for identity in business_ids:
        content = contents.selected.get(identity)
        if content is None or content.reference["business_period"] > period:
            raise ContractError("not_found", "指定月份没有该身份的有效正式报酬结果。")
        if content.target["kind"] == "system.withdrawal":
            if content.target["business_kind"] != "people.remuneration":
                raise ContractError("invalid_input", "薪酬可付额只能查询工资、奖金或劳务报酬。")
            withdrawn.add(identity)
            continue
        source = contents.remuneration(content)
        if source is None:
            raise ContractError("invalid_input", "薪酬可付额只能查询工资、奖金或劳务报酬。")
        active.append(source)
    combined = {
        item.remuneration["business_id"]: item
        for item in compositions(ledger, contents, active, period, read, selection)
    }
    refs = tuple(
        {"business_id": item.reference["business_id"], "slot": "net_pay"} for item in active
    )
    positions = (
        ledger.get_obligation_positions(period, read, references=refs, selection=selection)
        if refs
        else ()
    )
    remaining = {
        item.definition.reference["business_id"]: item.amounts.remaining_fen for item in positions
    }
    if len(positions) != len(active) or set(remaining) != set(combined):
        raise ContractError("storage_corrupt", "薪酬可付额缺少唯一的正式净薪债务。")
    result: list[RemunerationPositionItem] = []
    for identity in business_ids:
        if identity in withdrawn:
            result.append({"business_id": identity, "state": "withdrawn"})
            continue
        source = contents.remuneration(contents.selected[identity])
        composition = combined[identity]
        basis_input = contents.selected[identity].basis["input"]
        payment_confirmed = (
            basis_input.get("disbursement_basis", {}).get("type") == "current_payable"
            or "tax_basis" in basis_input
            or bool(composition.tax_targets)
        )
        result.append(
            {
                "business_id": identity,
                "state": "active",
                "person_id": source.person_id,
                "remuneration_type": source.remuneration_type,
                "business_period": source.business_period,
                "remuneration": composition.remuneration,
                "tax_targets": list(composition.tax_targets),
                "amounts": {
                    field.name: format_fen(getattr(composition.amounts, field.name))
                    for field in fields(RemunerationAmounts)
                },
                "tax_state": composition.tax_state,
                "payment_composition_state": "confirmed" if payment_confirmed else "unconfirmed",
                "remaining_fen": format_fen(remaining[identity]),
                "payable_fen": format_fen(
                    max(remaining[identity] - composition.amounts.held_fen, 0)
                ),
            }
        )
    return tuple(result)
