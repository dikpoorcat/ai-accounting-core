"""Shared definitions for controlled company-payment facts and balance presentation."""

ACTUAL_PAYMENT_KINDS = ("payment", "cash_payment", "platform_payment")
SETTLEMENT_PAYMENT_KINDS = (*ACTUAL_PAYMENT_KINDS, "payroll_reserve_payment")
PAYMENT_FUNDS_ACCOUNT_BY_KIND = {
    "payment": "1002",
    "cash_payment": "1001",
    "platform_payment": "1012",
    "payroll_reserve_payment": "1002",
}
FUNDS_ACCOUNT_TYPE_BY_BALANCE_CATEGORY = {
    "bank": "bank",
    "cash": "cash",
    "platform": "payment_platform",
}


def payment_funds_account(kind: str) -> str:
    """Return the controlled ledger account for a typed settlement payment."""

    return PAYMENT_FUNDS_ACCOUNT_BY_KIND[kind]
