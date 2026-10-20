"""Month precision is enough exactly when its possible cash order cannot matter."""

import pytest

from ._cooperation_support import (
    agency_payment,
    collection,
    contract,
    expense,
    funding,
    operator,
    policy,
    position,
    receipt,
    sale,
    submitted,
)
from ._support import assemble


@pytest.mark.parametrize(
    "funding_day,payment_day,expected",
    [
        ("2026-01-20", None, "needs_information"),
        ("2026-01-20", "2026-01-05", "dependency_conflict"),
        ("2026-01-20", "2026-01-21", "complete"),
        ("2026-01-01", None, "complete"),
    ],
)
def test_month_payment_can_cross_a_real_funding_day_but_cannot_precede_month_first(
    tmp_path, funding_day, payment_day, expected
):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement, day="2026-01-01")
    paid = agency_payment(agreement, charged)
    if payment_day is None:
        paid.pop("effective_on")
    else:
        paid["effective_on"] = payment_day
    actual = app.accounting_apply(
        submitted(company, agreement, funding(agreement, day=funding_day), charged, paid)
    )
    if expected == "complete":
        assert actual["completion"] == "complete", actual
        assert position(app, company, agreement)["pool_fen"] == "80"
    else:
        assert actual["error"] == expected, str(actual)
        if expected == "needs_information":
            assert all(
                item["business_id"] == paid["business_id"] and item["field"] == "/effective_on"
                for item in actual["fact_issues"]
            ), actual


def test_unknown_month_collection_is_certain_by_a_known_last_day_payment(tmp_path):
    app, company = assemble(tmp_path, taxes=True)
    agreement, rule = contract(), policy()
    sold = sale(agreement, rule)
    collected = collection(agreement, sold)
    collected.pop("effective_on")
    actual = app.accounting_apply(
        submitted(
            company, agreement, rule, sold, collected, receipt(agreement, 100, day="2026-01-31")
        )
    )
    assert actual["completion"] == "complete", str(actual)
    current = position(app, company, agreement)
    assert current["pool_fen"] == "100" and current["company_capital_fen"] == "0"


@pytest.mark.parametrize("opening_cash", [0, 20])
def test_pure_month_cash_does_not_assume_receipts_precede_payments(tmp_path, opening_cash):
    app, company = assemble(tmp_path)
    agreement = contract()
    into = operator(agreement, 20, into=True, day="2026-02-03")
    returned = operator(agreement, 20, day="2026-02-04")
    into.pop("effective_on")
    returned.pop("effective_on")
    facts = [agreement, into, returned]
    if opening_cash:
        facts.append(funding(agreement, "20", day="2026-01-01"))
    actual = app.accounting_apply(submitted(company, *facts))
    if opening_cash:
        assert actual["completion"] == "complete", actual
        assert position(app, company, agreement, "2026-02")["pool_fen"] == "20"
    else:
        assert actual["error"] == "needs_information", actual
        assert actual["fact_issues"], actual
        assert all(
            item["business_id"] in {into["business_id"], returned["business_id"]}
            and item["field"] == "/effective_on"
            for item in actual["fact_issues"]
        ), actual
