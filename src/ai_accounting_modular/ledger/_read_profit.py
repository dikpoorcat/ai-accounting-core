"""Bounded investment profit contributions use the statement's effective journals."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import ReadPage

from ._balances import sum_amounts
from ._formal_output import target_vouchers
from ._journal_amounts import journal_amount
from ._planning import stored_source_reference
from ._profit import profit_rows
from ._read_contents import reference
from ._read_support import after_key, bounded, cursor, scope_selection, version
from ._types import ProfitContribution
from ._values import fail

ACCOUNTS = ("5111", "571106", "571107", "571108")


def contributions(scope, page, read, *, business_kinds=()):
    selected = scope_selection(scope, read)
    business_kinds = bounded(business_kinds, "text")
    repo = read.repository("ledger")
    table, where, args = repo.read_profit_scope(scope, selected, ACCOUNTS, business_kinds)
    identities = repo.read_identity_rows(table, where, args)
    query = {key: value for key, value in scope.items() if key != "expected_head"}
    query["business_kinds"] = business_kinds
    identity = version(read, "profit_contributions", query, selected, identities)
    if scope.get("expected_head", identity) != identity:
        fail("snapshot_changed", "投资损益的正式采用范围已经变化，请重新读取。")
    after = after_key(page, identity, (str, str, str, str))
    rows = repo.read_result_page(table, where, args, after=after, limit=page.limit + 1, flow=True)
    more, rows = len(rows) > page.limit, rows[: page.limit]
    heads = {row["result_id"]: row for row in rows}
    baseline_ids = (
        {row["baseline_result_id"] for row in rows if row["baseline_result_id"]}
        if selected.view == "as_posted"
        else set()
    )
    heads.update((row["result_id"], row) for row in repo.read_heads(baseline_ids))
    if not baseline_ids <= heads.keys():
        fail("storage_corrupt", "投资损益的正式基线不存在。")
    journals = target_vouchers(repo, list(heads.values()), headers_only=True)
    voucher_ids = {row["voucher_id"] for values in journals.values() for row in values}
    lines = defaultdict(list)
    for row in repo.read_profit_lines(voucher_ids, ACCOUNTS):
        lines[row["voucher_id"]].append(row)
    amounts = {}
    for key, values in journals.items():
        own = [line for journal in values for line in lines[journal["voucher_id"]]]
        amounts[key] = (
            profit_rows(own).get(20, 0),
            sum_amounts(
                journal_amount(row["debit_fen"]) - journal_amount(row["credit_fen"])
                for row in own
                if row["account_code"] != "5111"
            ),
        )
    items = []
    for row in rows:
        old = (
            heads[row["baseline_result_id"]] if row["baseline_result_id"] in baseline_ids else None
        )
        income, loss = amounts[row["result_id"]]
        before_income, before_loss = amounts[old["result_id"]] if old else (0, 0)
        items.append(
            ProfitContribution(
                reference(row),
                stored_source_reference(row),
                row["business_kind"],
                income,
                loss,
                before_income,
                before_loss,
                reference(old) if old else None,
                stored_source_reference(old) if old else None,
            )
        )
    next_cursor = (
        cursor(
            identity,
            [
                rows[-1][key]
                for key in ("posting_period", "business_id", "result_slot", "result_id")
            ],
        )
        if more
        else None
    )
    return ReadPage(identity, len(identities), len(identities), tuple(items), next_cursor)
