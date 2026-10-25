"""Indexed original-row selection; only requested pages load descriptive bodies."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    ContractError,
    QuerySelection,
    content_digest,
    loads_json,
    validate_contract,
    validate_contract_items,
)
from ai_accounting_modular.ledger.public import ReviewMovementQuery

from ._common import fail
from ._original_rows import row_wire
from ._queries import page_values, wire
from ._read_types import OriginalRowView
from ._review_types import ReviewRowIdentity, ReviewRowLink, ReviewRowQuery, ReviewRowSelection


def _references(ledger, business_ids, read):
    result = {}
    identities = sorted(set(business_ids))
    for start in range(0, len(identities), 500):
        for ref in ledger.get_result_refs(
            identities[start : start + 500], read, selection=QuerySelection("current")
        ):
            if ref["result_slot"] != "main" or ref["business_id"] in result:
                fail("原行核查需要唯一的当前正式main结果。", "storage_corrupt")
            result[ref["business_id"]] = ref
    if set(result) != set(identities):
        fail("原行或确定关联缺少当前正式采用。", "storage_corrupt")
    return result


def _parents(repo, identities):
    result = {}
    for start in range(0, len(identities), 500):
        for row in repo.review_parents(identities[start : start + 500]):
            try:
                fact = loads_json(row["data_json"])
                kind = fact["kind"]
                definition = {
                    "money.bank_statement": "MoneyBankStatement",
                    "money.platform_activity": "MoneyPlatformActivity",
                }[kind]
                validate_contract("core.schema.json", "Digest", fact.pop("collection_digest"))
                validate_contract_items(
                    "business-facts.schema.json",
                    "Fact" + definition,
                    [{key: value for key, value in fact.items() if key != "data"}],
                    fields=("business_id", "kind", "period", "effective_on", "evidence_ids"),
                )
                field = (
                    "funds_account_id" if kind == "money.bank_statement" else "platform_account_id"
                )
                data_fields = (
                    (field, "opening_fen", "closing_fen")
                    if kind == "money.bank_statement"
                    else (field,)
                )
                validate_contract_items(
                    "business-facts.schema.json", definition, [fact["data"]], fields=data_fields
                )
                if (
                    fact["business_id"] != row["business_id"]
                    or row["schema_key"] != kind + "/storage_projection/1"
                    or row["effective_on"] != fact.get("effective_on")
                    or row["main_object_id"] != fact["data"][field]
                ):
                    raise ValueError("original parent mismatch")
                result[row["business_id"]] = (fact, content_digest(row["data_json"]))
            except (ContractError, KeyError, TypeError, ValueError) as error:
                raise ContractError(
                    "storage_corrupt", "原行核查的清单定位或完整摘要不一致。"
                ) from error
    if set(result) != set(identities):
        fail("原行缺少所属的当前清单。", "storage_corrupt")
    return result


def _funds(ledger, movement_ids, first, last, read):
    result = {}
    identities = sorted(set(movement_ids))
    for start in range(0, len(identities), 500):
        selected = ledger.capture_review_movements(
            ReviewMovementQuery(first, last, movement_ids=tuple(identities[start : start + 500])),
            read,
        )
        result.update((item.movement_id, item) for item in selected.items)
    if set(result) != set(identities):
        fail("原行的确定核对关系缺少当前真实资金。", "storage_corrupt")
    return result


def _links(owner, identities, read):
    if not identities:
        return (), ()
    repo, ledger = read.repository("money"), owner._ledger
    first, last = (
        min(item.actual_on[:7] for item in identities),
        max(item.actual_on[:7] for item in identities),
    )
    bank_rows = {item.row_id: item for item in identities if item.kind == "bank"}
    groups = repo.review_groups(tuple(bank_rows))
    refs = _references(ledger, (group["business_id"] for group in groups), read)
    movement_ids = [key for group in groups for key in group["members"].get("occurrence", ())]
    funds = _funds(ledger, movement_ids, first, last, read)
    links, versions = [], [wire(item) for item in funds.values()]
    for group in groups:
        ref = refs[group["business_id"]]
        movements = tuple(group["members"].get("occurrence", ()))
        if not movements:
            fail("已确定银行核对组没有真实资金。", "storage_corrupt")
        for row_id in group["members"].get("bank_row", ()):
            row = bank_rows.get(row_id)
            if row is None:
                continue
            if row.account_id != group["account_id"]:
                fail("原行与已确定核对组账户不一致。", "storage_corrupt")
            links.append(
                ReviewRowLink(
                    row.business_id,
                    row_id,
                    "reconciliation",
                    movements,
                    tuple(sorted({funds[key].reference["business_id"] for key in movements})),
                    ref,
                    group["group_id"],
                )
            )
        versions.append({"group": group, "adopted": ref})
    platform = defaultdict(dict)
    for row in identities:
        if row.kind == "platform":
            platform[(row.account_id, row.actual_on[:7])][row.row_id] = row
    for (account_id, period), rows in sorted(platform.items()):
        selected = ledger.capture_review_movements(
            ReviewMovementQuery(period, period, account_id=account_id), read
        )
        by_row = defaultdict(list)
        for item in selected.items:
            for row_id in item.platform_row_ids:
                if row_id in rows:
                    by_row[row_id].append(item)
        for row_id, actual in sorted(by_row.items()):
            links.append(
                ReviewRowLink(
                    rows[row_id].business_id,
                    row_id,
                    "platform_consumption",
                    tuple(sorted(item.movement_id for item in actual)),
                    tuple(sorted({item.reference["business_id"] for item in actual})),
                )
            )
            versions.extend(wire(item) for item in actual)
        internal_ids = repo.review_internal_ids(account_id, period)
        internals = [
            fact
            for start in range(0, len(internal_ids), 500)
            for fact in repo.review_internal_consumers(
                internal_ids[start : start + 500], account_id, period
            )
            if set(fact["data"]["platform_row_ids"]) & rows.keys()
        ]
        internal_refs = _references(ledger, (fact["business_id"] for fact in internals), read)
        for fact in internals:
            versions.append({"fact": fact, "adopted": internal_refs[fact["business_id"]]})
            for row_id in sorted(set(fact["data"]["platform_row_ids"]) & rows.keys()):
                if row_id in by_row:
                    fail("平台原行同时被真实资金及代管内部业务承接。", "storage_corrupt")
                links.append(
                    ReviewRowLink(
                        rows[row_id].business_id,
                        row_id,
                        "platform_consumption",
                        (),
                        (fact["business_id"],),
                    )
                )
    return tuple(sorted(links, key=_link_key)), versions


def _link_key(item):
    return item.statement_business_id, item.row_id, item.relation, item.group_id or ""


def capture(owner, query, read):
    if not isinstance(query, ReviewRowQuery):
        fail("原行核查须使用ReviewRowQuery。", "invalid_input")
    repo = read.repository("money")
    headers = repo.review_row_headers(query)
    statement_ids = tuple(sorted({row["business_id"] for row in headers}))
    parents = _parents(repo, statement_ids)
    refs = _references(owner._ledger, statement_ids, read)
    identities = []
    for row in headers:
        fact = parents[row["business_id"]][0]
        kind = "bank" if fact["kind"] == "money.bank_statement" else "platform"
        field = "funds_account_id" if kind == "bank" else "platform_account_id"
        if (
            row["account_id"] != fact["data"][field]
            or row["period"] != fact["period"]
            or row["kind"] != kind
            or row["actual_on"][:7] != row["period"]
            or row["direction"] not in ("in", "out")
        ):
            fail("原行核查的账户、月份或类型与清单不一致。", "storage_corrupt")
        validate_contract(
            "business-facts.schema.json",
            "BankStatementRow" if kind == "bank" else "PlatformOriginalRow",
            {
                "row_id": row["row_id"],
                "source": {"source_id": row["source_id"]},
                "actual_on": row["actual_on"],
                "direction": "inflow" if row["direction"] == "in" else "outflow",
                "amount_fen": str(row["amount_fen"]),
            },
        )
        identities.append(
            ReviewRowIdentity(
                row["business_id"],
                row["row_id"],
                row["account_id"],
                kind,
                row["actual_on"],
                "inflow" if row["direction"] == "in" else "outflow",
                row["amount_fen"],
                refs[row["business_id"]],
            )
        )
    values = tuple(identities)
    links, link_versions = _links(owner, values, read)
    identity = content_digest(
        wire(
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "query": query,
                "rows": values,
                "parents": {key: digest for key, (_, digest) in parents.items()},
                "links": links,
                "link_sources": link_versions,
            }
        )
    )
    return ReviewRowSelection(query, identity, values, links, statement_ids, read)


def _check(selection, read):
    if not isinstance(selection, ReviewRowSelection) or selection._read is not read:
        fail("原行核查目录必须在当前读取快照内重新取得。", "snapshot_changed")


def row_page(selection, page, read):
    _check(selection, read)
    values = [
        ((item.actual_on, item.business_id, item.row_id), item) for item in selection.identities
    ]
    found = page_values(selection.version, values, page)
    repo = read.repository("money")
    rows = {}
    for kind in ("bank", "platform"):
        wanted = tuple(item.row_id for item in found.items if item.kind == kind)
        if wanted:
            rows.update(((kind, row["row_id"]), row) for row in repo.review_rows(wanted, kind))
    groups = {
        (link.statement_business_id, link.row_id): link.group_id
        for link in selection.links
        if link.relation == "reconciliation"
    }
    result = []
    for item in found.items:
        saved = rows.get((item.kind, item.row_id))
        if saved is None or saved["business_id"] != item.business_id:
            fail("已选择的原行正文缺失或属于其他清单。", "storage_corrupt")
        value = row_wire(
            saved, "money.bank_statement" if item.kind == "bank" else "money.platform_activity"
        )
        if (
            value["actual_on"],
            value["direction"],
            int(value["amount_fen"]),
            saved["account_id"],
        ) != (item.actual_on, item.direction, item.amount_fen, item.account_id):
            fail("原行正文与已核查目录不一致。", "storage_corrupt")
        result.append(
            OriginalRowView(
                item.business_id,
                item.adopted,
                item.row_id,
                item.account_id,
                item.kind,
                item.actual_on,
                item.direction,
                item.amount_fen,
                value.get("reference"),
                value.get("counterparty_text"),
                value.get("memo"),
                value["source"],
                groups.get((item.business_id, item.row_id)),
            )
        )
    return type(found)(
        found.version, found.total_count, found.filtered_count, tuple(result), found.next_cursor
    )


def link_page(selection, page, read):
    _check(selection, read)
    return page_values(
        selection.version, [(_link_key(item), item) for item in selection.links], page
    )
