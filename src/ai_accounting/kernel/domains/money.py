"""Shared definitions for controlled company-payment facts and balance presentation."""

ACTUAL_PAYMENT_KINDS = ("payment", "cash_payment", "platform_payment")
SETTLEMENT_PAYMENT_KINDS = (*ACTUAL_PAYMENT_KINDS, "payroll_reserve_payment")
FUNDS_ACCOUNT_BY_BALANCE_CATEGORY = {
    "bank": "1002",
    "cash": "1001",
    "platform": "1012",
}
PAYMENT_FUNDS_ACCOUNT_BY_KIND = {
    "payment": FUNDS_ACCOUNT_BY_BALANCE_CATEGORY["bank"],
    "cash_payment": FUNDS_ACCOUNT_BY_BALANCE_CATEGORY["cash"],
    "platform_payment": FUNDS_ACCOUNT_BY_BALANCE_CATEGORY["platform"],
    "payroll_reserve_payment": FUNDS_ACCOUNT_BY_BALANCE_CATEGORY["bank"],
}
FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY = {
    "bank": "bank",
    "cash": "cash",
    "platform": "payment_platform",
}


def payment_funds_account(kind: str) -> str:
    """Return the controlled ledger account for a typed settlement payment."""

    return PAYMENT_FUNDS_ACCOUNT_BY_KIND[kind]
