"""Later payments consume the remaining debt without invalidating an earlier offset."""

import pytest
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.domains.cash import CashFunding, CashPayment
from ai_accounting.kernel.domains.labor_assets import AssetAdvance, LaborProjectCost
from ai_accounting.kernel.domains.transactions import Allocation, Settlement
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.types import YearMonth


def allocation(kind, subject, obligation, amount):
    return Allocation(source_kind=kind, source_id=subject, obligation=obligation, amount_fen=amount)


def cash_payment(period, amount, source):
    return CashPayment(
        period=YearMonth(period),
        actual_date=period + "-15",
        direction="outflow",
        cash_account_id="cash",
        counterparty_id="designer",
        amount_fen=amount,
        allocations=(source,),
    )


def setup(tmp_path):
    company = Company(tmp_path / "book.sqlite")
    company.save(
        CashFunding(
            period=YearMonth("2026-01"),
            actual_date="2026-01-01",
            owner_id="owner",
            cash_account_id="cash",
            funding_kind="capital",
            amount_fen=1600000,
        ),
        "capital",
    )
    company.save(
        AssetAdvance(
            period=YearMonth("2026-01"),
            counterparty_id="designer",
            asset_type="intangible",
            amount_fen=800000,
            contractual_obligation_established=True,
        ),
        "advance",
    )
    company.save(
        cash_payment("2026-01", 800000, allocation("asset_advance", "advance", "primary", 800000)),
        "advance-paid",
    )
    company.save(
        LaborProjectCost(
            period=YearMonth("2026-01"),
            person_id="designer",
            project_id="design-project",
            gross_fee_fen=1600000,
            tax_treatment="not_withheld_not_filed",
            capitalization_conditions_confirmed=True,
        ),
        "cost",
    )
    company.save(
        Settlement(
            period=YearMonth("2026-01"),
            settlement_kind="advance_application",
            first=allocation("asset_advance", "advance", "advance", 800000),
            second=allocation("labor_project_cost", "cost", "net", 800000),
            offset_right_confirmed=True,
        ),
        "application",
    )
    company.publish("capital", "advance", "advance-paid", "cost", "application")
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            dict(connection.execute("SELECT balance_key,amount FROM balance"))[
                "labor_project_cost:cost:net"
            ]
            == 800000
        )
    return company


@pytest.mark.parametrize("closed", [False, True])
def test_later_remaining_payment_preserves_prior_offset_and_closed_snapshot(tmp_path, closed):
    company = setup(tmp_path)
    previous = company.current("application", "settlement")
    frozen = company.close("2026-01") if closed else None
    ledger = company.engine.ledger("2026-01")
    company.save(
        cash_payment("2026-02", 800000, allocation("labor_project_cost", "cost", "net", 800000)),
        "balance-paid",
    )
    assert "application" not in company.pending()
    company.publish("balance-paid")
    assert company.current("application", "settlement").id == previous.id
    assert company.engine.ledger("2026-01") == ledger
    assert not company.pending()
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            dict(connection.execute("SELECT balance_key,amount FROM balance")).get(
                "labor_project_cost:cost:net", 0
            )
            == 0
        )
    if closed:
        assert Periods(company.engine).closed_report("2026-01") == frozen


def test_same_month_excess_still_invalidates_and_rejects_publication(tmp_path):
    company = setup(tmp_path)
    original = company.current("application", "settlement")
    count = company.count("calculation")
    company.save(
        cash_payment("2026-01", 800001, allocation("labor_project_cost", "cost", "net", 800001)),
        "excess",
    )
    assert "application" in company.pending()
    with pytest.raises(KernelError) as error:
        company.publish("application", "excess")
    assert error.value.code == "overallocated_obligation"
    assert company.count("calculation") == count
    assert company.current("application", "settlement").id == original.id
