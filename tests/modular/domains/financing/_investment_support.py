"""Independent synthetic investment facts for the ordinary public application."""

from ._support import COUNTERPARTY, PRODUCT, SOURCE, fact


def terms(
    *, product_type="money_fund", measurement="short_term_cost", method="fifo", schedule=None
):
    legal = (
        "direct_bond"
        if product_type == "bond"
        else "company_equity"
        if product_type == "equity"
        else "securities_fund"
    )
    tax_type = (
        "corporate_bond"
        if product_type == "bond"
        else "unlisted_equity"
        if product_type == "equity"
        else "securities_fund"
    )
    data = {
        "product_id": PRODUCT,
        "product_type": product_type,
        "legal_form": legal,
        "measurement": measurement,
        "quantity_unit": "nominal_fen" if product_type == "bond" else "units",
        "cost_method": method,
        "holding_arrangement": "current" if measurement == "short_term_cost" else "noncurrent",
        "terms_source": {"source_id": SOURCE, "location": "合成投资合同"},
        "tax_facts": {
            "instrument_type": tax_type,
            "source": {"source_id": SOURCE, "location": "合成权利与税务事实"},
        },
    }
    if schedule is not None:
        data["coupon_schedule"] = schedule
    if legal in {"direct_bond", "company_equity", "partnership_interest"}:
        data["initial_holding_intent"] = (
            "short_term" if measurement == "short_term_cost" else "long_term"
        )
    return fact("investment.terms", data)


def acquire(agreement, value="100", units="3", day="2026-01-02"):
    return fact(
        "investment.acquire",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "quantity": units,
            "purchase_price_fen": value,
            "acquisition_fees_fen": "0",
            "separate_declared_income_fen": "0",
        },
        period=day[:7],
        actual=day,
    )


def dispose(agreement, units="1", price="40", day="2026-01-04", *, selected=None):
    data = {
        "terms_id": agreement["business_id"],
        "counterparty_id": COUNTERPARTY,
        "quantity": units,
        "gross_proceeds_fen": price,
        "disposal_fees_fen": "0",
        "exit_mode": "redemption",
    }
    if selected is not None:
        data["lot_allocations"] = selected
    return fact("investment.dispose", data, period=day[:7], actual=day)


def source(item):
    return {"business_id": item["business_id"], "slot": "cost"}
