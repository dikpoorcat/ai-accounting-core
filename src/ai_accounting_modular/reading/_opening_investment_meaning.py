"""Shared labels for the opening owner's typed investment obligation references."""

from ai_accounting_modular.ledger.opening.public import (
    OpeningCooperationTradeReference,
    OpeningInvestmentReference,
)

from ._business_labels import BusinessLabel


def label(reference):
    if isinstance(reference, OpeningInvestmentReference):
        return (
            BusinessLabel("opening_investment_interest", "期初投资应收利息")
            if reference.income_kind == "interest"
            else BusinessLabel("opening_investment_distribution", "期初投资应收分配")
        )
    if isinstance(reference, OpeningCooperationTradeReference):
        return (
            BusinessLabel("opening_cooperation_receivable", "期初合作经营应收款")
            if reference.side == "revenue"
            else BusinessLabel("opening_cooperation_payable", "期初合作经营应付款")
        )
    return None
