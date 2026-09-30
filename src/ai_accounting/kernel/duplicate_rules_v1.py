"""Frozen v1 duplicate signal normalization for historical close verification.

Only these business fields are required to reconstruct the private v1
candidate directory. Current duplicate detection may evolve separately.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .contracts import Fact
from .history_encoding_v1 import digest

ORIGIN_KINDS = frozenset(
    {
        "expense",
        "service_sale",
        "project_cost",
        "pass_through",
        "advance",
        "asset_advance",
        "refundable_deposit",
        "reimbursed_deposit",
        "asset",
        "reimbursed_asset",
        "reimbursed_asset_batch",
        "labor_accrual",
        "labor",
        "labor_project_cost",
        "money_fund_subscription",
        "loan_agreement",
    }
)


ACTUAL_MONEY_KINDS = frozenset(
    {
        "payment",
        "cash_payment",
        "platform_payment",
        "funding",
        "cash_funding",
        "platform_funding",
        "funds_transfer",
        "cash_bank_transfer",
        "bank_platform_transfer",
        "bank_income",
        "loan_drawdown",
        "managed_reserve_expense",
        "managed_reserve_refund",
        "payroll_reserve_payment",
    }
)


ELIGIBLE_KINDS = ORIGIN_KINDS | ACTUAL_MONEY_KINDS


SIGNATURE_FIELDS = {
    "expense": ("period", "counterparty_id", "amount_fen", "expense_class", "creditor_kind"),
    "service_sale": (
        "period",
        "customer_id",
        "gross_fen",
        "fulfillment_date",
        "vat_policy_id",
        "exemption_eligible",
        "tax_obligation_period",
        "tax_obligation_date",
    ),
    "project_cost": (
        "period",
        "project_id",
        "supplier_id",
        "amount_fen",
        "project_nature",
        "capitalization_conditions_confirmed",
    ),
    "pass_through": (
        "period",
        "payer_id",
        "beneficiary_id",
        "amount_fen",
        "rights_and_obligation_confirmed",
    ),
    "advance": (
        "period",
        "counterparty_id",
        "amount_fen",
        "side",
        "contractual_obligation_established",
        "vat_due_on_advance",
        "vat_policy_id",
        "exemption_eligible",
        "tax_obligation_period",
        "tax_obligation_date",
    ),
    "asset_advance": (
        "period",
        "counterparty_id",
        "asset_type",
        "amount_fen",
        "contractual_obligation_established",
    ),
    "refundable_deposit": ("period", "counterparty_id", "amount_fen", "refund_right_confirmed"),
    "reimbursed_deposit": (
        "period",
        "counterparty_id",
        "employee_id",
        "amount_fen",
        "company_acceptance_confirmed",
        "refund_right_confirmed",
    ),
    "asset": (
        "period",
        "asset_id",
        "asset_type",
        "supplier_id",
        "acquisition_date",
        "cost_fen",
        "acquisition_basis",
        "project_sources",
    ),
    "reimbursed_asset": (
        "period",
        "asset_id",
        "asset_type",
        "cost_fen",
        "company_acceptance_confirmed",
        "creditors",
        "acceptance_id",
        "acquisition_date",
    ),
    "reimbursed_asset_batch": (
        "period",
        "cost_fen",
        "company_acceptance_confirmed",
        "assets",
        "creditors",
        "acquisition_date",
    ),
    "labor_accrual": ("period", "person_id", "expense_class", "gross_fee_fen", "tax_treatment"),
    "labor": (
        "period",
        "person_id",
        "income_date",
        "policy_id",
        "expense_class",
        "recipient_tax_status",
        "remuneration_method",
        "withholding_method",
        "gross_payment_kind",
        "gross_payment_id",
        "fixed_fee_fen",
        "commission_base_fen",
        "commission_rate_ppm",
    ),
    "labor_project_cost": (
        "period",
        "person_id",
        "project_id",
        "gross_fee_fen",
        "tax_treatment",
        "capitalization_conditions_confirmed",
    ),
    "money_fund_subscription": (
        "period",
        "fund_id",
        "counterparty_id",
        "classification",
        "cost_basis",
        "purchase_price_fen",
        "acquisition_fees_fen",
        "excludes_declared_unpaid_distributions",
        "confirmation_date",
    ),
    "loan_agreement": (
        "period",
        "lender_id",
        "lender_is_licensed",
        "currency",
        "annual_rate_percent",
        "day_count_basis",
        "maturity_date",
        "loan_term",
    ),
    "payment": (
        "actual_date",
        "direction",
        "bank_account_id",
        "counterparty_id",
        "payment_method",
        "amount_fen",
        "allocations",
    ),
    "cash_payment": (
        "actual_date",
        "direction",
        "cash_account_id",
        "counterparty_id",
        "payment_method",
        "amount_fen",
        "allocations",
    ),
    "platform_payment": (
        "actual_date",
        "direction",
        "platform_account_id",
        "counterparty_id",
        "payment_method",
        "amount_fen",
        "movement_ids",
        "allocations",
    ),
    "funding": ("actual_date", "bank_account_id", "owner_id", "amount_fen", "funding_kind"),
    "cash_funding": ("actual_date", "cash_account_id", "owner_id", "amount_fen", "funding_kind"),
    "platform_funding": (
        "actual_date",
        "platform_account_id",
        "owner_id",
        "amount_fen",
        "funding_kind",
        "movement_ids",
    ),
    "funds_transfer": (
        "actual_date",
        "source_bank_account_id",
        "destination_bank_account_id",
        "amount_fen",
    ),
    "cash_bank_transfer": (
        "actual_date",
        "direction",
        "bank_account_id",
        "cash_account_id",
        "amount_fen",
    ),
    "bank_platform_transfer": (
        "actual_date",
        "direction",
        "bank_account_id",
        "platform_account_id",
        "amount_fen",
        "movement_ids",
    ),
    "bank_income": (
        "actual_date",
        "bank_account_id",
        "counterparty_id",
        "amount_fen",
        "income_kind",
        "entitlement_confirmed",
    ),
    "loan_drawdown": ("actual_date", "agreement_id", "bank_account_id", "principal_fen"),
    "managed_reserve_expense": (
        "actual_date",
        "bank_account_id",
        "cash_account_id",
        "platform_account_id",
        "movement_ids",
        "counterparty_id",
        "amount_fen",
    ),
    "managed_reserve_refund": (
        "actual_date",
        "bank_account_id",
        "cash_account_id",
        "platform_account_id",
        "movement_ids",
        "counterparty_id",
        "amount_fen",
    ),
    "payroll_reserve_payment": (
        "actual_date",
        "bank_account_id",
        "amount_fen",
        "allocations",
        "reserve_expense_fen",
        "return_period",
        "actual_return_date",
        "return_confirmed",
        "complete_group_confirmed",
    ),
}


DUPLICATE_ROLES = {kind: kind for kind in ELIGIBLE_KINDS}


def _money_leg(
    category: str,
    account_id: str,
    actual_date: str,
    direction: str,
    amount_fen: int,
    object_id: str | None,
    movement_ids: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "category": category,
        "account_id": account_id,
        "actual_date": actual_date,
        "direction": direction,
        "amount_fen": amount_fen,
        "object_id": object_id,
        "movement_ids": sorted(set(movement_ids)),
    }


def _actual_money_legs(fact: Fact) -> tuple[dict[str, Any], ...]:
    """Return each real company-funds side without its accounting treatment."""

    if fact.kind not in ACTUAL_MONEY_KINDS:
        return ()
    data = _fact_data(fact)
    actual_date = str(data["actual_date"])
    amount_fen = data.get("amount_fen", data.get("principal_fen"))
    movements = data.get("movement_ids", ())
    if fact.kind in {"payment", "cash_payment", "platform_payment"}:
        category = {
            "payment": "bank",
            "cash_payment": "cash",
            "platform_payment": "platform",
        }[fact.kind]
        return (
            _money_leg(
                category,
                data[f"{category}_account_id"],
                actual_date,
                data["direction"],
                amount_fen,
                data["counterparty_id"],
                movements,
            ),
        )
    if fact.kind in {"funding", "cash_funding", "platform_funding"}:
        category = {
            "funding": "bank",
            "cash_funding": "cash",
            "platform_funding": "platform",
        }[fact.kind]
        return (
            _money_leg(
                category,
                data[f"{category}_account_id"],
                actual_date,
                "inflow",
                amount_fen,
                data["owner_id"],
                movements,
            ),
        )
    if fact.kind in {"managed_reserve_expense", "managed_reserve_refund"}:
        category = next(
            category
            for category in ("bank", "cash", "platform")
            if data.get(f"{category}_account_id") is not None
        )
        return (
            _money_leg(
                category,
                data[f"{category}_account_id"],
                actual_date,
                "outflow" if fact.kind == "managed_reserve_expense" else "inflow",
                amount_fen,
                data.get("counterparty_id"),
                movements,
            ),
        )
    if fact.kind == "payroll_reserve_payment":
        return (
            _money_leg(
                "bank",
                data["bank_account_id"],
                actual_date,
                "outflow",
                amount_fen,
                data.get("counterparty_id", "payroll-group"),
            ),
        )
    if fact.kind == "bank_income":
        return (
            _money_leg(
                "bank",
                data["bank_account_id"],
                actual_date,
                "inflow",
                amount_fen,
                data["counterparty_id"],
            ),
        )
    if fact.kind == "loan_drawdown":
        return (
            _money_leg(
                "bank",
                data["bank_account_id"],
                actual_date,
                "inflow",
                amount_fen,
                "loan-agreement:" + data["agreement_id"],
            ),
        )
    if fact.kind == "funds_transfer":
        source, destination = data["source_bank_account_id"], data["destination_bank_account_id"]
        return (
            _money_leg(
                "bank",
                source,
                actual_date,
                "outflow",
                amount_fen,
                "funds-account:bank:" + destination,
            ),
            _money_leg(
                "bank",
                destination,
                actual_date,
                "inflow",
                amount_fen,
                "funds-account:bank:" + source,
            ),
        )
    if fact.kind == "cash_bank_transfer":
        bank, cash = data["bank_account_id"], data["cash_account_id"]
        withdrawal = data["direction"] == "withdrawal"
        return (
            _money_leg(
                "bank",
                bank,
                actual_date,
                "outflow" if withdrawal else "inflow",
                amount_fen,
                "funds-account:cash:" + cash,
            ),
            _money_leg(
                "cash",
                cash,
                actual_date,
                "inflow" if withdrawal else "outflow",
                amount_fen,
                "funds-account:bank:" + bank,
            ),
        )
    if fact.kind == "bank_platform_transfer":
        bank, platform = data["bank_account_id"], data["platform_account_id"]
        to_platform = data["direction"] == "bank_to_platform"
        return (
            _money_leg(
                "bank",
                bank,
                actual_date,
                "outflow" if to_platform else "inflow",
                amount_fen,
                "funds-account:platform:" + platform,
            ),
            _money_leg(
                "platform",
                platform,
                actual_date,
                "inflow" if to_platform else "outflow",
                amount_fen,
                "funds-account:bank:" + bank,
                movements,
            ),
        )
    raise RuntimeError(f"missing actual-money duplicate normalization for {fact.kind}")


def _fact_data(fact: Fact) -> dict[str, Any]:
    return fact.model_dump(mode="json")


def _signature(fact: Fact) -> str:
    data = _fact_data(fact)
    return digest(
        [DUPLICATE_ROLES[fact.kind], {key: data[key] for key in SIGNATURE_FIELDS[fact.kind]}]
    ).hex()
