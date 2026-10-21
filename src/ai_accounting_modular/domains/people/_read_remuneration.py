"""Build one complete payroll month from public adopted results and resource uses."""

from dataclasses import asdict, fields

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    QuerySelection,
    ReadInputs,
    content_digest,
    validate_contract,
)
from ai_accounting_modular.ledger.public import ObligationDirectoryQuery, SettlementEventQuery

from ._read_sources import (
    OUTSTANDING_ACCOUNTS,
    Contents,
    batches,
    outstanding_basis,
    remuneration_meaning,
)
from ._read_totals import people, summary
from ._read_types import (
    RemunerationActivity,
    RemunerationAmounts,
    RemunerationEntry,
    RemunerationPosition,
    RemunerationSettlement,
)

_KINDS = ("people.remuneration", "opening.package")
_UNPAID_CATEGORIES = ("net_pay", "social", "housing", "withholding")


def _all(query):
    request = PageQuery(500)
    result = []
    while True:
        page = query(request)
        result.extend(page.items)
        if page.next_cursor is None:
            return result
        request = PageQuery(500, page.next_cursor, page.version)


def _json(value):
    if isinstance(value, tuple | list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    return value


def _entry(adopted, source, sign):
    return RemunerationEntry(
        f"{adopted['result_id']}:{adopted['generation']}:{'target' if sign == 1 else 'baseline'}",
        adopted,
        source.reference,
        source.source,
        sign,
        source.business_period,
        source.person_id,
        source.remuneration_type,
        source.expense_area,
        source.project_id,
        source.tax_state,
        source.tax_period,
        RemunerationAmounts(
            **{
                field.name: sign * getattr(source.amounts, field.name)
                for field in fields(RemunerationAmounts)
            }
        ),
    )


def _flow(ledger, scope, read, contents):
    rows = _all(
        lambda page: ledger.query_adopted_values(scope, ("people.remuneration",), page, read)
    )
    contents.load(
        ref for row in rows for ref in (row.reference, row.baseline_reference) if ref is not None
    )
    entries = []
    for row in rows:
        previous = (
            contents.remuneration(contents.exact[row.baseline_reference["result_id"]])
            if row.baseline_reference is not None
            else None
        )
        current = contents.remuneration(contents.exact[row.reference["result_id"]])
        if (
            previous is not None
            and current is not None
            and remuneration_meaning(previous) == remuneration_meaning(current)
        ):
            continue
        if previous is not None:
            entries.append(_entry(row.reference, previous, -1))
        if current is not None:
            entries.append(_entry(row.reference, current, 1))
    return entries


def _recovery_parents(ledger, month, read, selection, contents, events, unpaid_claims):
    claims = {item.reference["business_id"] for item in unpaid_claims}
    # Unpaid claims survive quiet months; monthly sources/targets also retain just-settled claims.
    claims.update(event.use.value["source"]["business_id"] for event in events)
    claims.update(
        event.use.value["target"]["business_id"]
        for event in events
        if event.use.component == "reclassify_excess"
    )
    parents = {}
    for batch in batches(sorted(claims)):
        query = SettlementEventQuery(
            read.start_period,
            month,
            consumer_business_ids=batch,
            components=("reclassify_excess",),
            source_business_kinds=_KINDS,
        )
        links = _all(
            lambda page, query=query: ledger.query_business_settlements(
                query, page, read, selection=selection
            )
        )
        contents.select(link.use.value["source"]["business_id"] for link in links)
        for link in links:
            parent = contents.source(link.use.value["source"])
            target = link.use.value["target"]
            if parent is not None:
                parents[target["business_id"], target["slot"]] = parent
    return parents


def _events(ledger, month, read, selection, contents, unpaid_claims):
    query = SettlementEventQuery(
        month,
        month,
        source_business_kinds=(*_KINDS, "operations.overpayment"),
    )
    events = _all(
        lambda page: ledger.query_settlement_events(query, page, read, selection=selection)
    )
    parents = _recovery_parents(ledger, month, read, selection, contents, events, unpaid_claims)
    refs = [event.use.value["source"] for event in events]
    contents.select(ref["business_id"] for ref in refs)
    result = []
    for event, ref in zip(events, refs, strict=True):
        info = contents.source(ref)
        recovery = False
        if info is None:
            info = parents.get((ref["business_id"], ref["slot"]))
            recovery = info is not None
        if info is not None:
            result.append(
                RemunerationSettlement(
                    info.person_id,
                    info.period,
                    info.kind,
                    info.component,
                    event,
                    recovery,
                    info.reference if recovery else None,
                )
            )
    return result, parents


def _positions(ledger, month, read, selection, contents, entries, events, parents, unpaid_payroll):
    ids = {item.reference["business_id"] for item in unpaid_payroll}
    ids.update(entry.effective["business_id"] for entry in entries)
    ids.update(event.event.use.value["source"]["business_id"] for event in events)
    ids.update(business_id for business_id, _ in parents)
    ids.update(info.reference["business_id"] for info in parents.values())
    contents.select(ids)
    positions = []
    for batch in batches(sorted(ids)):
        rows = ledger.get_obligation_positions(month, read, business_ids=batch, selection=selection)
        for position in rows:
            ref = position.definition.reference
            info = contents.source(ref)
            recovery = False
            if info is None:
                info = parents.get((ref["business_id"], ref["slot"]))
                recovery = info is not None
            if info is not None:
                positions.append(
                    RemunerationPosition(
                        info.person_id,
                        info.period,
                        info.kind,
                        info.component,
                        info.project_id,
                        position,
                        recovery,
                        info.reference if recovery else None,
                    )
                )
    return positions


def remuneration_activity(ledger, scope, read):
    if ledger is None:
        raise ContractError("unsupported_business", "薪酬读取尚未装配正式账务读取端口。")
    validate_contract("core.schema.json", "LedgerScope", scope)
    month = scope["period_to"]
    if (
        scope["company_id"] != read.company_id
        or scope["period_from"] != month
        or "object_ids" in scope
        or (scope["view"] == "as_posted" and scope.get("as_of", month) != month)
        or (scope["view"] == "current" and "as_of" in scope)
    ):
        raise ContractError("invalid_input", "薪酬活动须指定同公司单月及对应正式采用截止。")
    selection = QuerySelection(scope["view"], month if scope["view"] == "as_posted" else None)
    normalized = {key: value for key, value in scope.items() if key != "expected_head"}
    if selection.as_of is not None:
        normalized["as_of"] = selection.as_of
    contents = Contents(ledger, read, selection)
    entries = _flow(ledger, normalized, read, contents)
    unpaid = ledger.capture_obligation_directory(
        ObligationDirectoryQuery(
            month,
            month,
            include_origins=False,
            categories=(*_UNPAID_CATEGORIES, "other"),
            selected_view=selection.view,
        ),
        read,
    ).selected
    claims = tuple(
        item for item in unpaid if item.category == "other" and item.direction == "receivable"
    )
    payroll = tuple(item for item in unpaid if item.category in _UNPAID_CATEGORIES)
    events, parents = _events(ledger, month, read, selection, contents, claims)
    positions = _positions(
        ledger, month, read, selection, contents, entries, events, parents, payroll
    )
    known, issues = outstanding_basis(ledger, month, read, selection)
    roster = contents.roster(normalized)
    persons = people(entries, positions, events, roster, known=known)
    totals = summary(entries, positions, events, known=known)
    sources = contents.activity_sources(entries, positions, events)
    consumed = {source.reference["business_id"] for source in sources}
    consumed.update(contents.roster_business_ids)
    consumed.update(item.position.definition.reference["business_id"] for item in positions)
    consumed.update(
        identity
        for item in events
        for identity in (
            item.event.use.source["business_id"],
            item.event.use.value["source"]["business_id"],
        )
    )
    payload = {
        "company": read.company_id,
        "database": read.database_id,
        "scope": normalized,
        "summary": asdict(totals),
        "persons": [asdict(item) for item in persons],
        "entries": [asdict(item) for item in entries],
        "positions": [asdict(item) for item in positions],
        "settlements": [asdict(item) for item in events],
        "sources": [asdict(item) for item in sources],
        "roster": [asdict(item) for item in roster],
        "issues": list(issues),
    }
    version = content_digest(_json(payload))
    if scope.get("expected_head", version) != version:
        raise ContractError("snapshot_changed", "所选薪酬活动已经变化，请重新读取。")
    return RemunerationActivity(
        version,
        normalized,
        totals,
        persons,
        tuple(entries),
        tuple(positions),
        tuple(events),
        sources,
        roster,
        issues,
        ReadInputs(
            result_kinds=_KINDS,
            account_codes=OUTSTANDING_ACCOUNTS,
            obligation_categories=_UNPAID_CATEGORIES,
            business_ids=tuple(sorted(consumed)),
        ),
    )
