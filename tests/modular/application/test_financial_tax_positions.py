"""Formal tax positions split a loss carry from accrued, unpaid and paid financial VAT."""

from copy import deepcopy

import pytest

from ai_accounting_modular.ledger.public import FormalChange
from tests.modular._projection_blocks import Blocks
from tests.modular.domains.financing._support import payment
from tests.modular.domains.taxes.test_investment_tax_application import (
    advance,
    new_trade,
    quarter_return,
    start_quarter,
    submit,
)

from ._working_case import case as case


def snapshot(case, month, view="current"):
    scope = {"company_id": case.company, "period_from": month, "period_to": month, "view": view}
    with case.kernel.storage.read_unit(case.company) as read:
        return case.app.ledger.financial_snapshot(scope, read)


def balances(report, asset, payable):
    assert report["balance_sheet"]["14"]["ending"] == {"state": "known", "amount_fen": str(asset)}
    assert report["balance_sheet"]["36"]["ending"] == {"state": "known", "amount_fen": str(payable)}


@pytest.mark.parametrize("assessed,paid", [(False, False), (True, False), (True, True)])
def test_financial_loss_carry_never_nets_prior_period_unpaid_or_unassessed_tax(
    case, assessed, paid
):
    case.seed(expense=False)
    terms, policy, surtax, basis = start_quarter(case.app, case.company)
    balances(snapshot(case, "2026-07"), 0, 10000)
    current = quarter_return(policy, surtax, "2026-07", "2026-09")
    if assessed:
        submit(case.app, case.company, current)
        balances(snapshot(case, "2026-09"), 0, 10000)
    submit(case.app, case.company, *new_trade(terms, "2026-10", 1010000, 0))
    if paid:
        submit(
            case.app,
            case.company,
            advance("2026-10"),
            payment(current, 10000, slot="financial_transfer_vat", day="2026-10-16"),
        )
    for view in ("current", "as_posted"):
        balances(snapshot(case, "2026-10", view), 10000, 0 if paid else 10000)
    # A December return extends the actual monthly formal positions through expiry.
    december_return = quarter_return(policy, surtax, "2026-10", "2026-12")
    submit(case.app, case.company, december_return)
    for view in ("current", "as_posted"):
        balances(snapshot(case, "2026-12", view), 0, 0 if paid else 10000)


def test_financial_tax_projection_and_closed_selection_retain_their_actual_sources(case):
    case.seed(expense=False)
    terms, policy, surtax, basis = start_quarter(case.app, case.company)
    current = quarter_return(policy, surtax, "2026-07", "2026-09")
    submit(case.app, case.company, current)
    for month in range(1, 10):
        checked = case.check(f"2026-{month:02d}")
        assert checked["ready"], str(checked)
        assert case.close(checked)["state"] == "closed"
    frozen = snapshot(case, "2026-09", "as_posted")
    balances(frozen, 0, 10000)
    submitted = new_trade(terms, "2026-10", 1010000, 0)
    submit(case.app, case.company, *submitted)
    scopes = [
        {"company_id": case.company, "period_from": month, "period_to": month, "view": view}
        for month, view in (
            ("2026-09", "as_posted"),
            ("2026-10", "current"),
            ("2026-10", "as_posted"),
        )
    ]
    blocks = Blocks()
    with case.kernel.storage.read_unit(case.company) as read:
        requests = tuple(
            case.app.ledger.financial_projection_request("snapshot", scope, read)
            for scope in scopes
        )
        blocks.publish(
            case.app.ledger.prepare_financial_projection(requests, (), (), read, rebuild=True)
        )
        for request in requests:
            actual = case.app.ledger.read_financial_projection(request, read, blocks).value
            balances(actual, 0 if request.scope["period_to"] == "2026-09" else 10000, 10000)
    # Restating the earlier gross proceeds must preserve the original closed quarter.
    original_id = next(
        item["business_id"]
        for item in case.app.accounting_find(
            {
                "company_id": case.company,
                "family": "investment",
                "period_from": "2026-07",
                "period_to": "2026-07",
                "limit": 50,
            }
        )["items"]
        if item["kind"] == "investment.dispose"
    )
    original = case.get(original_id)["businesses"][0]["fact"]
    corrected = deepcopy(original)
    corrected["data"]["gross_proceeds_fen"] = "2111000"
    changed = case.app.accounting_apply(
        case.request(
            correction_period="2026-10",
            mutations=[{"op": "replace", "expected_seq": 1, "fact": corrected}],
        )
    )
    assert changed.get("completion") == "complete", changed.get("message", changed)
    after = snapshot(case, "2026-09", "as_posted")
    assert after["balance_sheet"] == frozen["balance_sheet"]
    balances(snapshot(case, "2026-10"), 10000, 11000)
    with case.kernel.storage.read_unit(case.company) as read:
        affected = case.app.ledger.get_results(
            [basis["business_id"], current["business_id"], terms["business_id"], original_id], read
        )
        signals = tuple(
            FormalChange(
                item["reference"]["business_id"],
                item["reference"]["result_slot"],
                (item["reference"]["business_period"],),
                (item["reference"]["posting_period"],),
                (item["target"]["kind"],),
            )
            for item in affected
        )
        blocks.publish(
            case.app.ledger.prepare_financial_projection(requests, signals, (), read, base=blocks)
        )
        for request in requests:
            actual = case.app.ledger.read_financial_projection(request, read, blocks).value
            assert actual == case.app.ledger.financial_snapshot(request.scope, read)
