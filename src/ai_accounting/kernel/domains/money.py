"""Shared definitions for controlled company-payment facts and balance presentation."""

from ..contracts import KernelError

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


def validate_transfer_material_direction(
    amount_fen, *, source_amounts, source_directions, dimension, expected_direction
):
    """Match one transfer side to every unchanged original amount and column direction."""
    directions = source_directions or (None,) * len(source_amounts)
    valid = bool(source_amounts) and len(directions) == len(source_amounts)
    positive = expected_direction == "inflow"
    for amount, direction in zip(source_amounts, directions, strict=False):
        if direction in {None, "signed_net"}:
            normalized_positive = amount > 0
        elif direction in {"inflow", "outflow"}:
            valid = valid and amount >= 0
            normalized_positive = direction == "inflow"
        else:
            valid = False
            normalized_positive = None
        valid = valid and amount != 0 and normalized_positive == positive
    valid = (
        valid
        and amount_fen != 0
        and all((amount > 0) == (amount_fen > 0) for amount in source_amounts)
    )
    if not valid:
        raise KernelError(
            "material_funds_direction_mismatch",
            "内部划转资料须保留原金额并按明确列方向匹配账户侧；联合组不得混合收支",
            dimension=dimension,
            expected_direction=expected_direction,
        )
