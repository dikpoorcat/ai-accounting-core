"""Actual company movements, allocation parts and statement coverage."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    ReadInputs,
    ReadPage,
)
from ai_accounting_modular.ledger.public import SettlementEventQuery

from ._common import amount, fail, total
from ._queries import page_values, scope_selection, version
from ._read_statements import KINDS as STATEMENT_KINDS
from ._read_statements import original_rows
from ._read_types import (
    AccountActivity,
    FundsActivity,
    MovementQuery,
    SettlementPart,
    StatementActivity,
)
from ._reconciliation import validate_complete_statement

_ACCOUNT_KINDS = ("bank_account", "cash_account", "platform_account")


def _movements(owner, scope, read):
    _, selected = scope_selection(scope, read)
    return owner._ledger.fund_movements(
        None, scope["period_from"], scope["period_to"], read, selection=selected
    )


def _parts(owner, movements, scope, read):
    if not movements:
        return {}
    _, selected = scope_selection(scope, read)
    events, cursor = [], None
    query = SettlementEventQuery(
        scope["period_from"],
        scope["period_to"],
        movement_ids=tuple(item.value["movement_id"] for item in movements),
        components=("money",),
    )
    while True:
        page = owner._ledger.query_settlement_events(
            query, PageQuery(500, cursor), read, selection=selected
        )
        events.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    sources = {item.use.source["result_id"]: item.use.source for item in events}
    obligations = {}
    refs = list(sources.values())
    for start in range(0, len(refs), 500):
        headers = owner._ledger.get_adopted_sources(refs[start : start + 500], read)
        for content in owner._ledger.get_adopted_contents(
            tuple(item.reference for item in headers), read
        ):
            for quantity in content.basis["adopted_quantities"]:
                if quantity["type"] == "obligation":
                    ref = quantity["source"]
                    key = (content.source["result_id"], ref["business_id"], ref["slot"])
                    if key in obligations:
                        fail("真实付款的采用依据重复定义同一义务。", "storage_corrupt")
                    obligations[key] = quantity
    values = defaultdict(list)
    for event in events:
        ref = event.use.value["source"]
        original = obligations.get((event.use.source["result_id"], ref["business_id"], ref["slot"]))
        if original is None:
            fail("真实付款分项缺少正式来源义务。", "storage_corrupt")
        values[event.movement.value["movement_id"]].append(
            SettlementPart(ref, amount(event.use.value["amount_fen"]), original.get("party_id"))
        )
    return {key: tuple(value) for key, value in values.items()}


def query_movements(owner, scope, query, page, read):
    if not isinstance(query, MovementQuery):
        fail("真实资金查询须使用 MovementQuery。", "invalid_input")
    definitions = _movements(owner, scope, read)
    values = sorted(
        ((item.value["actual_on"], item.reference["business_id"], item.reference["slot"]), item)
        for item in definitions
    )
    filtered = [
        (key, item)
        for key, item in values
        if query.account_id is None or item.value["funds_account_id"] == query.account_id
    ]
    identity = version(
        scope,
        read,
        "movements",
        {"query": query, "total_count": len(values), "movements": filtered},
    )
    found = page_values(identity, values, page, filtered=filtered)
    return ReadPage(
        found.version,
        found.total_count,
        found.filtered_count,
        movement_views(owner, found.items, scope, read),
        found.next_cursor,
    )


def movement_views(owner, definitions, scope, read):
    allocations = {}
    for start in range(0, len(definitions), 500):
        allocations.update(_parts(owner, definitions[start : start + 500], scope, read))
    return tuple(
        {
            "reference": item.reference,
            "source": item.source,
            "movement": item.value,
            "allocations": allocations.get(item.value["movement_id"], ()),
        }
        for item in definitions
    )


def _accounts(owner, read):
    result = {}
    for kind in _ACCOUNT_KINDS:
        query = {"action": "find", "company_id": read.company_id, "kind": kind, "limit": 500}
        while True:
            page = owner._identity.find(query, read)
            result.update((item["object_id"], item) for item in page["objects"])
            if not page["page"]["has_more"]:
                break
            query["cursor"] = page["page"]["next_cursor"]
    return result


def account_activity(owner, scope, read):
    definitions = _movements(owner, scope, read)
    statements, checks, rows, groups = original_rows(owner._ledger, scope, read)
    accounts = _accounts(owner, read)
    by_account, rows_by_account, statements_by_account = (
        defaultdict(list),
        defaultdict(list),
        defaultdict(list),
    )
    for item in definitions:
        by_account[item.value["funds_account_id"]].append(item)
    for row in rows:
        rows_by_account[row.account_id].append(row)
    for key, result in statements.items():
        statements_by_account[key[0]].append((key, result))
    if (set(by_account) | set(rows_by_account)) - set(accounts):
        fail("正式实际资金或原行缺少真实账户身份。", "storage_corrupt")
    values = []
    for account_id, obj in sorted(accounts.items()):
        actual, originals = by_account[account_id], rows_by_account[account_id]
        supplied = statements_by_account[account_id]
        months = {item.value["actual_on"][:7] for item in actual} | {key[1] for key, _ in supplied}
        if supplied:
            coverage = "partial" if len(supplied) < len(months) else "complete"
        else:
            coverage = "missing" if actual and obj["kind"] != "cash_account" else "not_applicable"
        state, difference = "not_applicable", None
        if obj["kind"] == "bank_account" and (supplied or actual):
            state = "complete"
            for period in sorted(months):
                statement, check = (
                    statements.get((account_id, period)),
                    checks.get((account_id, period)),
                )
                if statement is None or check is None:
                    state = "pending"
                    continue
                if statement.source not in check.basis["source_results"]:
                    state = "attention"
                    continue
                try:
                    validate_complete_statement(
                        {"data": statement.basis["input"]},
                        tuple(item for item in actual if item.value["actual_on"][:7] == period),
                        check.basis["input"]["matches"],
                        amount(statement.basis["input"]["opening_fen"]),
                    )
                except ContractError as error:
                    if error.code != "dependency_conflict":
                        raise
                    state = "attention"
            if state == "complete":
                difference = 0
        statement_summary = StatementActivity(
            len(supplied),
            len(originals),
            total(row.amount_fen for row in originals if row.direction == "inflow")
            if supplied
            else None,
            total(row.amount_fen for row in originals if row.direction == "outflow")
            if supplied
            else None,
            max((row.actual_on for row in originals), default=None),
            coverage,
        )
        values.append(
            AccountActivity(
                account_id,
                obj["kind"],
                obj["name"],
                total(
                    amount(item.value["amount_fen"])
                    for item in actual
                    if item.value["direction"] == "inflow"
                ),
                total(
                    amount(item.value["amount_fen"])
                    for item in actual
                    if item.value["direction"] == "outflow"
                ),
                len(actual),
                max((item.value["actual_on"] for item in actual), default=None),
                statement_summary,
                state,
                difference,
            )
        )
    outgoing = [item for item in definitions if item.value["direction"] == "outflow"]
    incoming = [item for item in definitions if item.value["direction"] == "inflow"]
    movements = movement_views(owner, definitions, scope, read)
    identity = version(
        scope,
        read,
        "account_activity",
        {
            "accounts": values,
            "movements": movements,
            "original_rows": rows,
            "reconciliation_groups": groups,
            "statements": [item.source for item in statements.values()],
            "checks": [item.source for item in checks.values()],
        },
    )
    return FundsActivity(
        identity,
        total(
            amount(item.value["amount_fen"])
            for item in incoming
            if "transfer_group_id" not in item.value
        ),
        total(
            amount(item.value["amount_fen"])
            for item in outgoing
            if "transfer_group_id" not in item.value
        ),
        total(
            amount(item.value["amount_fen"])
            for item in outgoing
            if "transfer_group_id" in item.value
        ),
        len(definitions),
        tuple(values),
        movements,
        rows,
        groups,
        ReadInputs(
            business_result_kinds=STATEMENT_KINDS,
            business_ids=tuple(sorted({item.reference["business_id"] for item in definitions})),
            current_fact_ids=tuple(
                sorted({item.reference["business_id"] for item in statements.values()})
            ),
            object_ids=tuple(sorted(accounts)),
            object_kinds=_ACCOUNT_KINDS,
            movements=True,
        ),
    )
