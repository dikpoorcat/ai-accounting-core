"""Money-fund reading consumes formal values and actual allocated money only."""

import base64
from collections import defaultdict
from dataclasses import asdict

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    QuerySelection,
    ReadPage,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.ledger.public import (
    AccountBalanceKey,
    CostPositionQuery,
    SettlementEventQuery,
)

from ._common import amount, fail, total
from ._read_types import MoneyFundActivity, MoneyFundEvent, MoneyFundProduct

KINDS = ("investment.money_fund_purchase", "investment.money_fund_redemption")


def _scope(scope, read):
    validate_contract("core.schema.json", "LedgerScope", scope)
    if (
        scope["company_id"] != read.company_id
        or scope["period_from"] != scope["period_to"]
        or "object_ids" in scope
    ):
        fail("基金月度读取须指定同一公司的一个明确月份。", "invalid_input")
    selected = QuerySelection(
        scope["view"],
        scope.get("as_of", scope["period_to"] if scope["view"] == "as_posted" else None),
    )
    if selected.view == "as_posted" and selected.as_of != scope["period_to"]:
        fail("基金历史月的正式采用截止必须等于所选月份。", "invalid_input")
    normalized = {key: value for key, value in scope.items() if key != "expected_head"}
    if selected.as_of is not None:
        normalized["as_of"] = selected.as_of
    return normalized, selected


def _pages(call):
    cursor = None
    while True:
        page = call(PageQuery(500, cursor))
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def _contents(ledger, refs, read):
    unique = {ref["result_id"]: ref for ref in refs}
    refs, values = list(unique.values()), {}
    for start in range(0, len(refs), 500):
        values.update(
            (item.reference["result_id"], item)
            for item in ledger.get_adopted_contents(refs[start : start + 500], read)
        )
    return values


def _confirmations(ledger, scope, read):
    values = list(_pages(lambda page: ledger.query_adopted_values(scope, KINDS, page, read)))
    contents = _contents(
        ledger,
        [
            ref
            for item in values
            for ref in (item.reference, item.baseline_reference)
            if ref is not None
        ],
        read,
    )
    events = []
    for item in values:
        if item.target == item.baseline_target:
            continue
        for sign, target, ref in (
            (-1, item.baseline_target, item.baseline_reference),
            (1, item.target, item.reference),
        ):
            if target is None or target["kind"] not in KINDS:
                continue
            body = contents[ref["result_id"]]
            data = target["values"]
            purchase = target["kind"] == KINDS[0]
            events.append(
                MoneyFundEvent(
                    item.reference["result_id"] + (":baseline" if sign < 0 else ":target"),
                    ref["business_id"],
                    data["fund_id"],
                    "purchase_confirmation" if purchase else "redemption_confirmation",
                    item.reference["posting_period"]
                    if scope["view"] == "as_posted"
                    else ref["business_period"],
                    body.basis["period"],
                    body.basis.get("effective_on"),
                    body.source,
                    ref,
                    sign < 0 or item.baseline_reference is not None,
                    sign * amount(data["cost_fen"]),
                    None if purchase else sign * amount(data["net_proceeds_fen"]),
                    None if purchase else sign * amount(data["gain_loss_fen"]),
                    None,
                    None,
                )
            )
    return events


def _settlements(ledger, scope, selected, read):
    query = SettlementEventQuery(
        scope["period_from"], scope["period_to"], components=("money",), source_business_kinds=KINDS
    )
    uses = list(
        _pages(lambda page: ledger.query_settlement_events(query, page, read, selection=selected))
    )
    business_ids = list(dict.fromkeys(item.use.value["source"]["business_id"] for item in uses))
    directory = []
    for start in range(0, len(business_ids), 500):
        directory.extend(
            _pages(
                lambda page, batch=business_ids[start : start + 500]: (
                    ledger.query_adopted_directory(
                        scope, KINDS, page, read, business_ids=tuple(batch), result_slots=("main",)
                    )
                )
            )
        )
    contents = _contents(ledger, [item.reference for item in directory], read)
    producers = {item.reference["business_id"]: item for item in contents.values()}
    consumer_ids = list(dict.fromkeys(item.use.source["business_id"] for item in uses))
    consumers = []
    for start in range(0, len(consumer_ids), 500):
        consumers.extend(
            _pages(
                lambda page, batch=consumer_ids[start : start + 500]: (
                    ledger.query_adopted_directory(
                        scope, (), page, read, business_ids=tuple(batch), result_slots=("main",)
                    )
                )
            )
        )
    by_consumer = {item.reference["business_id"]: item.reference for item in consumers}
    events = []
    for item in uses:
        use, movement = item.use, item.movement
        source = producers.get(use.value["source"]["business_id"])
        if source is None or source.target["kind"] not in KINDS or movement is None:
            fail("基金实际清偿缺少同口径的正式确认或真实资金。", "storage_corrupt")
        purchase = source.target["kind"] == KINDS[0]
        ref = by_consumer.get(use.source["business_id"])
        if ref is None or (ref["result_id"], ref["generation"]) != (
            use.source["result_id"],
            use.source["result_generation"],
        ):
            fail("基金实际款项的正式采用定位不一致。", "storage_corrupt")
        events.append(
            MoneyFundEvent(
                use.value["settlement_id"],
                use.source["business_id"],
                source.target["values"]["fund_id"],
                "purchase_payment" if purchase else "redemption_receipt",
                use.value["period"],
                source.basis["period"],
                movement.value["actual_on"],
                use.source,
                ref,
                False,
                None,
                None,
                None,
                amount(use.value["amount_fen"]),
                movement.value["movement_id"],
            )
        )
    return events


def _events(owner, scope, read):
    normalized, selected = _scope(scope, read)
    events = _confirmations(owner._ledger, normalized, read) + _settlements(
        owner._ledger, normalized, selected, read
    )
    events.sort(
        key=lambda item: (
            item.period,
            item.actual_on is None,
            item.actual_on or "",
            item.business_id,
            item.event_id,
        )
    )
    return normalized, selected, events


def _version(scope, read, collection, value):
    normalized, _ = _scope(scope, read)
    value = content_digest(
        {
            "company_id": read.company_id,
            "database_id": read.database_id,
            "scope": normalized,
            "collection": collection,
            "value": value,
        }
    )
    if scope.get("expected_head", value) != value:
        fail("基金范围已经变化，请重新读取。", "snapshot_changed")
    return value


def query_events(owner, scope, page, read):
    if not isinstance(page, PageQuery):
        fail("基金事件分页须使用共同 PageQuery。", "invalid_input")
    _, _, events = _events(owner, scope, read)
    identity = _version(scope, read, "money_fund_events", [asdict(item) for item in events])
    if page.expected_version is not None and page.expected_version != identity:
        fail("基金事件版本已变化，请从第一页读取。", "snapshot_changed")
    offset = 0
    if page.cursor is not None:
        try:
            saved = loads_json(
                base64.urlsafe_b64decode(page.cursor.encode("ascii")).decode("utf-8")
            )
            if (
                set(saved) != {"version", "offset"}
                or type(saved["offset"]) is not int
                or saved["offset"] <= 0
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, ContractError) as error:
            raise ContractError("invalid_input", "基金事件分页游标无效。") from error
        if saved["version"] != identity:
            fail("基金事件范围或版本已经变化，请从第一页读取。", "snapshot_changed")
        if saved["offset"] >= len(events):
            fail("基金事件分页位置已经变化，请从第一页读取。", "snapshot_changed")
        offset = saved["offset"]
    found = tuple(events[offset : offset + page.limit])
    next_offset = offset + len(found)
    cursor = (
        base64.urlsafe_b64encode(
            canonical_json({"version": identity, "offset": next_offset}).encode("utf-8")
        ).decode("ascii")
        if next_offset < len(events)
        else None
    )
    return ReadPage(identity, len(events), len(events), found, cursor)


def _balance_cell(cell, local_issues, issues, indexes):
    """Only transfer the referenced explanations; amounts stay ledger values."""
    if cell["state"] == "known":
        return cell
    selected = []
    for position in cell["issue_indexes"]:
        try:
            issue = local_issues[position]
        except IndexError as error:
            raise ContractError("storage_corrupt", "正式基金余额缺少对应的缺项说明。") from error
        key = canonical_json(issue)
        if key not in indexes:
            indexes[key] = len(issues)
            issues.append(issue)
        selected.append(indexes[key])
    return {**cell, "issue_indexes": sorted(set(selected))}


def activity(owner, scope, read):
    _, selected, events = _events(owner, scope, read)
    cost_query = CostPositionQuery(
        scope["period_to"], categories=("money_fund",), include_consumed=True
    )
    cursor, fund_ids = None, {item.fund_id for item in events}
    while True:
        page = owner._ledger.query_cost_positions(
            cost_query, PageQuery(500, cursor), read, selection=selected
        )
        fund_ids.update(item.definition.value["object_id"] for item in page.page.items)
        if page.page.next_cursor is None:
            break
        cursor = page.page.next_cursor
    ids = sorted(fund_ids)
    objects = {}
    balances, issues, issue_indexes = {}, [], {}
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        objects.update((obj["object_id"], obj) for obj in owner._identity.get_batch(batch, read))
        for item in owner._ledger.account_balances(
            tuple(AccountBalanceKey("1101", identity) for identity in batch),
            scope["period_to"],
            read,
            selection=selected,
        ):
            balances[item.key.object_id] = tuple(
                _balance_cell(cell, item.issues, issues, issue_indexes)
                for cell in (item.opening, item.ending)
            )
    if set(objects) != fund_ids:
        fail("正式基金成本或事件缺少具体产品身份。", "storage_corrupt")
    overall = owner._ledger.account_balances(
        (AccountBalanceKey("1101"),), scope["period_to"], read, selection=selected
    )[0]
    opening, closing = (
        _balance_cell(cell, overall.issues, issues, issue_indexes)
        for cell in (overall.opening, overall.ending)
    )
    by_product = defaultdict(list)
    for event in events:
        by_product[event.fund_id].append(event)
    products = []
    for identity in ids:
        own = by_product[identity]
        products.append(
            MoneyFundProduct(
                identity,
                objects[identity]["name"],
                *balances[identity],
                total(item.cost_fen for item in own if item.kind == "purchase_confirmation"),
                total(item.cost_fen for item in own if item.kind == "redemption_confirmation"),
                total(item.gain_loss_fen for item in own if item.kind == "redemption_confirmation"),
            )
        )
    data = {
        "products": [asdict(item) for item in products],
        "opening": opening,
        "closing": closing,
        "events": [asdict(item) for item in events],
        "issues": issues,
    }
    identity = _version(scope, read, "money_fund_activity", data)
    return MoneyFundActivity(
        identity,
        opening,
        closing,
        total(item.subscription_cost_fen for item in products),
        total(item.redemption_cost_fen for item in products),
        total(item.investment_income_fen for item in products),
        total(item.settlement_fen for item in events if item.kind == "purchase_payment"),
        total(item.settlement_fen for item in events if item.kind == "redemption_receipt"),
        len(events),
        tuple(products),
        tuple(events),
        tuple(issues),
    )
