"""Asset contract advances and earned capitalized labor, before actual settlement."""

from typing import ClassVar, Literal

from pydantic import StrictBool

from ..contracts import BalanceEffect, Fact, FactVersion, Line, NeedsInformation, Outcome
from ..types import PositiveFen
from .payroll import labor_accrual_outcome
from .transactions import Identifier, obligation, outcome


class AssetAdvance(Fact):
    """An established asset-purchase obligation, with its recoverable advance right."""

    kind: ClassVar[str] = "asset_advance"
    material_amount_aliases: ClassVar[dict[str, str]] = {
        "fact.amount_fen": "result.gross_fen",
        "result.book_advance_fen": "result.gross_fen",
    }
    counterparty_id: Identifier
    asset_type: Literal["fixed", "intangible"]
    amount_fen: PositiveFen
    contractual_obligation_established: StrictBool | None = None

    def scopes(self):
        return (str(self.period), f"party:{self.counterparty_id}")


def calculate_asset_advance(version: FactVersion, context) -> Outcome:
    fact: AssetAdvance = version.fact
    if fact.contractual_obligation_established is not True:
        raise NeedsInformation(
            "contractual_obligation_established", "需要有依据的资产购买预付义务，预计购买不构成应付"
        )
    return outcome(
        [Line("1123", debit=fact.amount_fen), Line("2202", credit=fact.amount_fen)],
        {
            "side": "supplier",
            "asset_type": fact.asset_type,
            "counterparty_id": fact.counterparty_id,
            "gross_fen": fact.amount_fen,
            "book_advance_fen": fact.amount_fen,
        },
        [
            obligation(
                version,
                amount=fact.amount_fen,
                account="2202",
                normal="credit",
                counterparty=fact.counterparty_id,
                cashflow="asset_acquisition",
            ),
            obligation(
                version,
                name="advance",
                amount=fact.amount_fen,
                account="1123",
                normal="debit",
                counterparty=fact.counterparty_id,
                cashflow="asset_acquisition",
            ),
        ],
    )


class LaborProjectCost(Fact):
    """Accepted labor creates one capitalized cost and one gross personal liability."""

    kind: ClassVar[str] = "labor_project_cost"
    identity_fields: ClassVar[tuple[str, ...]] = ("person_id", "project_id")
    material_amount_aliases: ClassVar[dict[str, str]] = {
        "fact.gross_fee_fen": "result.gross_fen",
        "result.capitalized_fen": "result.gross_fen",
    }
    person_id: Identifier
    project_id: Identifier
    gross_fee_fen: PositiveFen | None = None
    tax_treatment: Literal["not_withheld_not_filed"] | None = None
    capitalization_conditions_confirmed: StrictBool | None = None

    def scopes(self):
        return (str(self.period), f"project:{self.project_id}", f"person:{self.person_id}")


def calculate_labor_project_cost(version: FactVersion, context) -> Outcome:
    fact: LaborProjectCost = version.fact
    if fact.capitalization_conditions_confirmed is not True:
        raise NeedsInformation(
            "capitalization_conditions_confirmed", "需要已满足外购无形资产资本化条件的劳务验收事实"
        )
    result = labor_accrual_outcome(
        version,
        "189901",
        cashflow="asset_acquisition",
        extra_values={
            "project_id": fact.project_id,
            "capitalized_fen": fact.gross_fee_fen,
            "capital_account": "189901",
            "project_nature": "purchased_intangible",
        },
    )
    return Outcome(
        result.lines,
        result.values,
        result.balances
        + (BalanceEffect(f"project-cost:{version.subject_id}", fact.gross_fee_fen, "asset"),),
        result.explanation,
    )


def register(registry):
    registry.register(AssetAdvance, calculate_asset_advance)
    registry.register(LaborProjectCost, calculate_labor_project_cost)
