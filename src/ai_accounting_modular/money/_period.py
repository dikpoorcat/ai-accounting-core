"""Closing checks reuse the complete bank match rule and current platform proof."""

from collections import defaultdict
from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    FactIssue,
    content_digest,
    validate_contract,
)

from ._common import accounting_input, amount, fail
from ._reconciliation import validate_complete_statement
from ._statements import validate_platform_movement


@dataclass(frozen=True)
class MoneyPeriodCheck:
    head: str
    blocking_issues: tuple[FactIssue, ...]


def _facts(repository, company_id, first, last, kind):
    query = {
        "company_id": company_id,
        "family": "money",
        "kind": kind,
        "period_from": first,
        "period_to": last,
        "limit": 500,
    }
    while True:
        values = repository.query(query)
        yield from values
        if len(values) < 500:
            return
        query["cursor"] = values[-1]["business_id"]


def _accounts(identity, read):
    for kind in ("bank_account", "platform_account"):
        query = {"action": "find", "company_id": read.company_id, "kind": kind, "limit": 500}
        while True:
            page = identity.find(query, read)
            yield from page["objects"]
            if not page["page"]["has_more"]:
                break
            query["cursor"] = page["page"]["next_cursor"]


def _issue(account, period, code, message, business_id=None):
    result = {
        "code": code,
        "field": "/bank_reconciliation"
        if account["kind"] == "bank_account"
        else "/platform_activity",
        "meaning": "本范围的完整原行与真实资金核对",
        "message": f"{period}，账户{account['object_id']}：{message}",
        "allowed_precision": ["month", "identity", "source"],
        "reusable_sources": [],
        "blocking_scope": "period",
    }
    if business_id is not None:
        result["business_id"] = business_id
    return validate_contract("core.schema.json", "FactIssue", result)


def _collections(first, last, read):
    repository = read.repository("money")
    statements, checks, platform, internal = {}, {}, {}, defaultdict(list)
    for kind in (
        "money.bank_statement",
        "money.reconciliation",
        "money.platform_activity",
        "money.reserve_internal",
    ):
        for fact in _facts(repository, read.company_id, first, last, kind):
            data = fact["data"]
            if kind == "money.bank_statement":
                key = data["funds_account_id"], fact["period"]
                if key in statements:
                    fail("同一账户月份有重复银行原清单。", "storage_corrupt")
                statements[key] = fact
            elif kind == "money.reconciliation":
                if data["statement_id"] in checks:
                    fail("同一银行清单有重复当前核对。", "storage_corrupt")
                checks[data["statement_id"]] = fact
            elif kind == "money.platform_activity":
                key = data["platform_account_id"], fact["period"]
                if key in platform:
                    fail("同一账户月份有重复平台原清单。", "storage_corrupt")
                platform[key] = fact
            else:
                internal[data["platform_account_id"], fact["period"]].append(fact)
    if not checks.keys() <= {fact["business_id"] for fact in statements.values()}:
        fail("当前银行核对缺少同范围原清单。", "storage_corrupt")
    return statements, checks, platform, internal


class _Check:
    def __init__(self, ledger, read):
        self.ledger, self.read = ledger, read
        self.views, self.sources, self.issues = {}, [], []

    def result(self, source):
        key = source["result_id"], source["result_generation"]
        if key not in self.views:
            self.views[key] = self.ledger.get_result(*key, self.read)
        return self.views[key]

    def source(self, fact):
        return self.ledger.get_result_source(fact["business_id"], "main", self.read)

    def pending(self, account, period, code, message, fact=None):
        self.issues.append(
            _issue(account, period, code, message, fact["business_id"] if fact else None)
        )

    def bank(self, account, period, statement, fact, actual):
        if fact is None:
            self.pending(
                account,
                period,
                "bank_reconciliation_missing",
                "原始清单尚未形成完整核对。",
                statement,
            )
            return
        source = self.source(fact)
        adopted = self.result(source)
        self.sources.append({"reconciliation": source})
        if adopted["target"]["kind"] != fact["kind"] or adopted["basis"].get(
            "input"
        ) != accounting_input(fact):
            fail("银行核对当前事实与正式采用不一致。", "storage_corrupt")
        refs = adopted["basis"]["source_results"]
        current = all(
            self.ledger.get_result_source(ref["business_id"], ref["result_slot"], self.read) == ref
            for ref in refs
        )
        if self.source(statement) not in refs or not current:
            self.pending(
                account,
                period,
                "bank_reconciliation_outdated",
                "原清单或账面起点已变化，需要按当前结果完整核对。",
                fact,
            )
            return
        has_opening = (
            account["data"].get("opened_period") == period
            and account["data"].get("zero_at_opening") is True
        )
        if not has_opening and len(refs) < 2:
            fail("完整银行核对没有保留必要的账面起点来源。", "storage_corrupt")
        try:
            validate_complete_statement(
                statement, actual, fact["data"]["matches"], amount(statement["data"]["opening_fen"])
            )
        except ContractError as error:
            if error.code != "dependency_conflict":
                raise
            self.pending(account, period, "bank_reconciliation_incomplete", error.message, fact)

    def platform(self, account, period, statement, internal, actual):
        consumed = set()
        rows = {row["row_id"]: row for row in statement["data"]["rows"]}
        malformed = False
        for movement in actual:
            data = self.result(movement.source)["basis"]["input"]
            try:
                row_ids = validate_platform_movement(
                    movement.value, rows, data.get("platform_row_ids", ())
                )
            except ContractError as error:
                if error.code != "dependency_conflict":
                    raise
                malformed = True
                continue
            malformed |= bool(consumed.intersection(row_ids))
            consumed.update(row_ids)
        for fact in internal:
            source = self.source(fact)
            self.sources.append({"internal": source})
            adopted = self.result(source)
            ids = set(fact["data"]["platform_row_ids"])
            if (
                adopted["target"]["kind"] != fact["kind"]
                or adopted["basis"]["input"] != accounting_input(fact)
                or consumed.intersection(ids)
                or not ids <= rows.keys()
            ):
                malformed = True
            consumed.update(ids)
        if malformed or consumed != rows.keys():
            self.pending(
                account,
                period,
                "platform_activity_incomplete",
                "原行须由匹配的真实资金或明确代管内部事项完整承接。",
                statement,
            )


def check_period(first, last, read, ledger, identity):
    for period in (first, last):
        validate_contract("core.schema.json", "Month", period)
    if first > last or read.view != "current":
        fail("期间核对须使用明确的当前事实范围。", "invalid_input")
    statements, checks, platform, internal = _collections(first, last, read)
    check = _Check(ledger, read)
    encountered = set()

    for account in _accounts(identity, read):
        account_id = account["object_id"]
        encountered.add((account["kind"], account_id))
        movements = ledger.fund_movements(account_id, first, last, read)
        by_month = defaultdict(list)
        for movement in movements:
            by_month[movement.value["actual_on"][:7]].append(movement)
        collection = statements if account["kind"] == "bank_account" else platform
        months = sorted(set(by_month) | {period for aid, period in collection if aid == account_id})
        if not months:
            continue
        check.sources.append(
            {
                "account_id": account_id,
                "kind": account["kind"],
                "opening": {
                    key: account["data"][key]
                    for key in ("opened_period", "zero_at_opening")
                    if key in account["data"]
                },
            }
        )
        for period in months:
            actual = by_month[period]
            check.sources.extend({"movement": item.value, "source": item.source} for item in actual)
            statement = collection.get((account_id, period))
            if statement is None:
                check.pending(
                    account, period, "statement_missing", "已有真实资金，尚无对应原始清单。"
                )
                continue
            check.sources.append({"statement": check.source(statement)})
            if account["kind"] == "bank_account":
                check.bank(account, period, statement, checks.get(statement["business_id"]), actual)
            else:
                check.platform(
                    account, period, statement, internal.get((account_id, period), ()), actual
                )
    expected = {("bank_account", aid) for aid, _ in statements} | {
        ("platform_account", aid) for aid, _ in platform
    }
    if not expected <= encountered:
        fail("原始清单引用的真实账户不存在或种类不符。", "storage_corrupt")
    return MoneyPeriodCheck(
        content_digest(
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "period_from": first,
                "period_to": last,
                "sources": sorted(check.sources, key=content_digest),
                "issues": check.issues,
            }
        ),
        tuple(check.issues),
    )
