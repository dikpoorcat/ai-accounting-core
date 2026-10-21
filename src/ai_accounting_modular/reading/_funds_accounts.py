"""Account cards combine formal ledger positions with actual funds and current names."""

from ai_accounting_modular.contracts.public import QuerySelection, format_fen, parse_fen
from ai_accounting_modular.ledger.public import AccountBalanceKey

TYPES = {
    "bank_account": ("bank", "1002"),
    "cash_account": ("cash", "1001"),
    "platform_account": ("payment_platform", "1012"),
}
LABELS = {
    "pending": "银行流水尚未完成核对",
    "not_applicable": "本月无需银行核对",
    "complete": "本月银行流水已核对",
    "attention": "银行流水与账面需要核查",
}


def amount(cell):
    return cell["amount_fen"] if cell["state"] == "known" else None


def total(values):
    values = tuple(values)
    return (
        None
        if any(value is None for value in values)
        else format_fen(sum(parse_fen(value) for value in values))
    )


def positions(ledger, accounts, month, read):
    keys = [AccountBalanceKey(code) for code in ("1001", "1002", "1012")]
    keys.extend(AccountBalanceKey(TYPES[item.kind][1], item.account_id) for item in accounts)
    result = {}
    selection = QuerySelection("as_posted", month)
    for start in range(0, len(keys), 500):
        result.update(
            (item.key, item)
            for item in ledger.account_balances(
                keys[start : start + 500], month, read, selection=selection
            )
        )
    return result


def cards(activity, balances, objects):
    result = []
    for account in activity.accounts:
        kind, code = TYPES[account.kind]
        obj = objects[account.account_id]
        account_code = obj["data"].get("account_number", obj["data"].get("account_key", ""))
        balance = balances[AccountBalanceKey(code, account.account_id)]
        opening, closing = amount(balance.opening), amount(balance.ending)
        card = {
            "account_id": account.account_id,
            "type": kind,
            "code": account_code,
            "name": obj["name"],
            "active": None,
            "opening_fen": opening,
            "closing_fen": closing,
            "inflow_fen": format_fen(account.inflow_fen),
            "outflow_fen": format_fen(account.outflow_fen),
            "net_change_fen": format_fen(account.inflow_fen - account.outflow_fen),
            # A balance correction is not proof of an account identity reassignment.
            "attribution_adjustment_fen": None,
            "movement_count": account.movement_count,
            "last_activity_date": account.last_activity_date,
            "negative_balance": closing is not None and parse_fen(closing) < 0,
            "statement": {
                "inflow_fen": None
                if account.statement.inflow_fen is None
                else format_fen(account.statement.inflow_fen),
                "outflow_fen": None
                if account.statement.outflow_fen is None
                else format_fen(account.statement.outflow_fen),
                "transaction_count": account.statement.row_count,
                "coverage_state": account.statement.coverage,
                "last_activity_date": account.statement.last_activity_date,
                "account_code": account_code,
                "account_name": obj["name"],
            },
            "reconciliation": {
                "state": account.reconciliation,
                "label": LABELS[account.reconciliation],
                "difference_fen": None
                if account.reconciliation_difference_fen is None
                else format_fen(account.reconciliation_difference_fen),
            },
        }
        result.append((balance.first_recorded_period, card))
    return result


def attention(card):
    reconciliation = card["reconciliation"]
    return (
        card["closing_fen"] is None
        or card["negative_balance"]
        or not card["name"].strip()
        or card["name"] == "未提供账户名称"
        or (
            card["type"] == "bank"
            and (
                card["statement"]["coverage_state"] in {"missing", "partial"}
                or reconciliation["state"] in {"pending", "attention"}
                or reconciliation["difference_fen"] not in {None, "0"}
            )
        )
    )


def bank_summary(accounts):
    banks = [item for item in accounts if item.kind == "bank_account"]
    expected = [item for item in banks if item.statement.coverage != "not_applicable"]
    supplied = [item for item in banks if item.statement.count]
    missing = [item for item in expected if not item.statement.count]
    if not expected:
        coverage = "not_applicable"
    elif not supplied:
        coverage = "missing"
    elif missing or any(item.statement.coverage == "partial" for item in expected):
        coverage = "partial"
    else:
        coverage = "complete"
    return {
        "transaction_count": sum(item.statement.row_count for item in banks),
        "inflow_fen": None
        if not supplied
        else format_fen(sum(item.statement.inflow_fen for item in supplied)),
        "outflow_fen": None
        if not supplied
        else format_fen(sum(item.statement.outflow_fen for item in supplied)),
        "review_state": "complete"
        if all(item.reconciliation == "complete" for item in expected)
        else "pending",
        "coverage_state": coverage,
        "statement_count": sum(item.statement.count for item in banks),
        "expected_account_count": len(expected),
        "provided_account_count": len(supplied),
        "missing_account_count": len(missing),
    }


def summary(activity, balances, account_cards):
    aggregate = {code: balances[AccountBalanceKey(code)] for code in ("1001", "1002", "1012")}
    return {
        "opening_fen": total(amount(item.opening) for item in aggregate.values()),
        "total_fen": total(amount(item.ending) for item in aggregate.values()),
        "net_change_fen": format_fen(activity.inflow_fen - activity.outflow_fen),
        "inflow_fen": format_fen(activity.inflow_fen),
        "outflow_fen": format_fen(activity.outflow_fen),
        "internal_transfer_fen": format_fen(activity.internal_transfer_fen),
        "movement_count": activity.movement_count,
        "bank_opening_fen": amount(aggregate["1002"].opening),
        "bank_fen": amount(aggregate["1002"].ending),
        "cash_fen": amount(aggregate["1001"].ending),
        "payment_platform_fen": amount(aggregate["1012"].ending),
        "bank_inflow_fen": format_fen(
            sum(item.inflow_fen for item in activity.accounts if item.kind == "bank_account")
        ),
        "bank_outflow_fen": format_fen(
            sum(item.outflow_fen for item in activity.accounts if item.kind == "bank_account")
        ),
        "account_count": len(account_cards),
        **{
            kind + "_account_count": sum(card["type"] == kind for _, card in account_cards)
            for kind in ("bank", "cash", "payment_platform")
        },
        "attention_account_count": sum(attention(card) for _, card in account_cards),
        "bank_statement": bank_summary(activity.accounts),
    }
