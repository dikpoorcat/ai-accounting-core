"""Formal original rows with explicitly current descriptive supplements."""

from ai_accounting_modular.contracts.public import canonical_json

from ._common import amount, fail, stable_id
from ._queries import adopted_contents, page_values, version
from ._read_types import OriginalRowView, ReconciliationGroup, StatementQuery

KINDS = ("money.bank_statement", "money.platform_activity", "money.reconciliation")


def statement_data(ledger, scope, read):
    statements, checks = {}, {}
    for result in adopted_contents(ledger, scope, KINDS, read):
        kind = result.target["kind"]
        if kind == "money.reconciliation":
            key = (result.target["values"]["funds_account_id"], result.basis["period"])
            if key in checks:
                fail("同一账户月份有重复正式银行核对。", "storage_corrupt")
            checks[key] = result
        elif kind in KINDS[:2]:
            data = result.basis["input"]
            account = data["funds_account_id" if kind == KINDS[0] else "platform_account_id"]
            key = (account, result.basis["period"])
            if key in statements:
                fail("同一账户月份有重复正式原始清单。", "storage_corrupt")
            statements[key] = result
    return statements, checks


def original_rows(ledger, scope, read):
    statements, checks = statement_data(ledger, scope, read)
    ids = [item.reference["business_id"] for item in statements.values()]
    current = {}
    for start in range(0, len(ids), 500):
        current.update(
            {
                fact["business_id"]: fact
                for fact in read.repository("money").get(ids[start : start + 500])
            }
        )
    values, groups = [], []
    for (account, period), item in statements.items():
        data = item.basis["input"]
        locations = {canonical_json(location) for location in item.basis["source_locations"]}
        kind = "bank" if item.target["kind"] == KINDS[0] else "platform"
        fact = current.get(item.reference["business_id"])
        same_parent = (
            fact is not None and fact["kind"] == item.target["kind"] and fact["period"] == period
        )
        if same_parent:
            field = "funds_account_id" if kind == "bank" else "platform_account_id"
            same_parent = fact["data"][field] == account
        supplements = {row["row_id"]: row for row in fact["data"]["rows"]} if same_parent else {}
        check = checks.get((account, period))
        matches = {}
        if (
            check is not None
            and check.basis["input"]["statement_id"] == item.reference["business_id"]
            and item.source in check.basis["source_results"]
        ):
            for group in check.basis["input"]["matches"]:
                group_id = stable_id(
                    check.reference["business_id"],
                    "reconciliation/"
                    + "/".join(sorted(group["statement_row_ids"]))
                    + "/"
                    + "/".join(sorted(group["fund_movement_ids"])),
                )
                groups.append(
                    ReconciliationGroup(
                        group_id,
                        check.reference,
                        tuple(group["statement_row_ids"]),
                        tuple(group["fund_movement_ids"]),
                    )
                )
                for row_id in group["statement_row_ids"]:
                    matches[row_id] = group_id
        for row in data["rows"]:
            extra = supplements.get(row["row_id"], {})
            if any(
                extra.get(key) != row[key]
                for key in ("row_id", "actual_on", "direction", "amount_fen")
            ):
                extra = {}
            location = extra.get("source")
            if location is None or canonical_json(location) not in locations:
                extra = {}
                location = None
            values.append(
                OriginalRowView(
                    item.reference["business_id"],
                    item.reference,
                    row["row_id"],
                    account,
                    kind,
                    row["actual_on"],
                    row["direction"],
                    amount(row["amount_fen"]),
                    extra.get("reference"),
                    extra.get("counterparty_text"),
                    extra.get("memo"),
                    location,
                    matches.get(row["row_id"]),
                )
            )
    return statements, checks, tuple(values), tuple(groups)


def query_rows(owner, scope, query, page, read):
    if not isinstance(query, StatementQuery):
        fail("原行查询须明确账户及银行或平台类型。", "invalid_input")
    _, _, rows, _ = original_rows(owner._ledger, scope, read)
    values = sorted(((row.actual_on, row.business_id, row.row_id), row) for row in rows)
    filtered = [
        (key, row)
        for key, row in values
        if (query.account_id is None or row.account_id == query.account_id)
        and (query.kind is None or row.kind == query.kind)
    ]
    identity = version(
        scope, read, "original_rows", {"query": query, "total_count": len(values), "rows": filtered}
    )
    return page_values(identity, values, page, filtered=filtered)
