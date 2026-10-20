"""Real funds survive corrected estimates; labels do not issue spending permission."""

import pytest

from ._cooperation_support import (
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
from ._support import assemble, change, fact


@pytest.mark.parametrize("closed", [False, True])
def test_corrected_income_and_rebuild_keep_real_receipt_without_relabelling_or_extra_profit(
    tmp_path, closed
):
    app, company = assemble(tmp_path / "original", taxes=True)
    agreement, rule = contract(), policy()
    supplied, sold = funding(agreement), sale(agreement, rule)
    collected, drawn = collection(agreement, sold), receipt(agreement, 100)
    actual = app.accounting_apply(
        submitted(company, agreement, rule, supplied, sold, collected, drawn)
    )
    assert actual["completion"] == "complete", actual
    if closed:
        with app.storage.write_unit(company) as unit:
            unit.repository("ledger").freeze_heads("2026-01")
    month = "2026-02" if closed else "2026-01"
    sold["data"].update(gross_fen="81", project_total_fen="160")
    claim = fact(
        "operations.overpayment",
        {
            "source": {"business_id": sold["business_id"], "slot": "primary"},
            "amount_fen": "20",
            "project_total_fen": "40",
        },
        period=month,
        actual=month + "-12",
    )
    correction = change(company, sold, correction_period=month if closed else None)
    correction["mutations"].append({"op": "create", "expected_seq": 0, "fact": claim})
    actual = app.accounting_apply(correction)
    assert actual["completion"] == "complete", str(actual)
    current = position(app, company, agreement, month)
    assert current["company_revenue_fen"] == current["company_profit_fen"] == "80"
    assert current["company_received_fen"] == current["company_profit_received_fen"] == "100"
    assert current["company_capital_returned_fen"] == "0"
    assert current["company_capital_fen"] == "80"
    assert current["pool_fen"] == "200" and current["held_fen"] == "100"
    assert current["company_trade_payable_fen"] == "20"
    rebuilt, other_company = assemble(tmp_path / "rebuilt", taxes=True)
    actual = rebuilt.accounting_apply(
        submitted(other_company, drawn, claim, collected, sold, supplied, rule, agreement)
    )
    assert actual["completion"] == "complete", str(actual)
    assert position(rebuilt, other_company, agreement, month) == current


def test_actual_partial_capital_returns_do_not_require_exit_or_debt_clearance_or_priority(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    charged = expense(agreement)
    into = operator(agreement, 20, into=True, day="2026-01-04")
    returned = operator(agreement, 20, day="2026-01-09")
    company_return = receipt(agreement, 10, kind="capital_return")
    actual = app.accounting_apply(
        submitted(company, agreement, funding(agreement), charged, into, returned, company_return)
    )
    assert actual["completion"] == "complete", actual
    current = position(app, company, agreement)
    assert current["status"] == "active"
    assert current["company_capital_returned_fen"] == "10"
    assert current["company_capital_fen"] == "80"
    assert current["held_fen"] == current["pool_fen"] == "90"
    assert current["company_trade_payable_fen"] == "10"
    assert current["operator_capital_fen"] == "-10"
    assert current["company_profit_fen"] == "-10"


def test_actual_replenishment_and_direct_operator_payment_do_not_require_a_prior_due(tmp_path):
    app, company = assemble(tmp_path)
    agreement = contract()
    added = funding(agreement, "7", day="2026-01-04")
    added["data"]["purpose"] = "replenishment"
    direct = receipt(agreement, 10, kind="operator_replenishment", origin="operator_direct")
    actual = app.accounting_apply(submitted(company, agreement, funding(agreement), added, direct))
    assert actual["completion"] == "complete", str(actual)
    current = position(app, company, agreement)
    assert current["company_funded_fen"] == "107"
    assert current["company_received_fen"] == current["operator_funded_fen"] == "10"
    assert current["pool_fen"] == "107" and current["held_fen"] == "97"
    assert current["company_profit_fen"] == "0"


@pytest.mark.parametrize("collection_day", [None, "2026-01-09", "2026-01-20"])
def test_only_actual_cash_uncertainty_requires_the_unknown_collections_day(
    tmp_path, collection_day
):
    app, company = assemble(tmp_path, taxes=True)
    agreement, rule = contract(), policy()
    sold = sale(agreement, rule)
    sold["data"].update(gross_fen="30", project_total_fen="60")
    collected = collection(agreement, sold)
    collected["data"]["project_amount_fen"] = "60"
    if collection_day is None:
        collected.pop("effective_on")
    else:
        collected["effective_on"] = collection_day
    actual = app.accounting_apply(
        submitted(
            company,
            agreement,
            rule,
            funding(agreement, "50"),
            sold,
            collected,
            receipt(agreement, 80),
        )
    )
    if collection_day is None:
        assert actual["error"] == "needs_information", actual
        assert actual["fact_issues"], actual
        assert all(
            issue["business_id"] == collected["business_id"] and issue["field"] == "/effective_on"
            for issue in actual["fact_issues"]
        ), actual
    elif collection_day == "2026-01-20":
        assert actual["completion"] == "not_saved", actual
        assert actual["error"] == "dependency_conflict", actual
    else:
        assert actual["completion"] == "complete", actual
        current = position(app, company, agreement)
        assert current["pool_fen"] == "30" and current["company_capital_fen"] == "0"
