"""Small shared fixtures for the owner's VAT period arrangements."""

from tests.modular.domains.operations.test_tax_lifecycle import command, fact


def save(app, company, *facts, **kwargs):
    saved = app.accounting_apply(command(company, *facts, **kwargs))
    assert saved["completion"] == "complete", saved
    return saved


def receipt(ids, earned, amount="10100", day="2026-03-08"):
    return fact(
        ids,
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": ids["bank"],
            "direction": "inflow",
            "amount_fen": amount,
            "payment_method": "individual",
            "counterparty_id": ids["party"],
            "allocations": [
                {
                    "source": {"business_id": earned["business_id"], "slot": "primary"},
                    "amount_fen": amount,
                }
            ],
        },
        period=day[:7],
        actual=day,
    )


def sales(app, company):
    with app.storage.read_unit(company) as read:
        return sorted(
            (row.sale["source_business_id"], row.sale["tax_period"], row.sale["vat_fen"])
            for row in app.ledger.tax_sales("2026-01", "2026-12", read)
        )
