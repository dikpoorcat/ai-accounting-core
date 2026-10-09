"""Month-end unpaid remuneration covers wages and all established personal labor."""

import pytest
from test_labor_accrual import accrual
from test_labor_assets import cost
from test_opening_continuation import book as opening_book_fixture
from test_payroll import bonus, bonus_sources, labor, labor_policy
from test_payroll_corrections import Company
from test_payroll_corrections import company as payroll_company_fixture
from test_settlement_period_scopes import allocation, cash_payment, setup

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains.adjustments import EmployeeAdvance
from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
from ai_accounting.kernel.domains.transactions import Allocation, Payment
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.response_contracts import validate_response

opening_book = opening_book_fixture
payroll_company = payroll_company_fixture


def employee_data(engine, period, **options):
    options.setdefault("employee_filter", "all")
    response = Dashboard(engine).employees(period, **options)
    validate_response("dashboard_employees", response)
    return response["data"]


def net_payment(kind, subject, amount, *, period="2026-02", person="contractor"):
    return Payment(
        period=period,
        actual_date=period + "-15",
        direction="outflow",
        bank_account_id="bank",
        counterparty_id=person,
        amount_fen=amount,
        allocations=(
            Allocation(source_kind=kind, source_id=subject, obligation="net", amount_fen=amount),
        ),
    )


def owner_payment(kind, subject, obligation, amount, *, period="2026-02"):
    return EmployeeAdvance(
        period=period,
        payer_id="owner",
        payer_kind="owner",
        payment_on_behalf_confirmed=True,
        actual_creditor_payment_date=period + "-10",
        sources=(
            Allocation(
                source_kind=kind,
                source_id=subject,
                obligation=obligation,
                amount_fen=amount,
            ),
        ),
    )


def test_unpaid_remuneration_covers_prior_labor_beyond_current_page_and_employee_scope(
    payroll_company,
):
    company = payroll_company
    company.save(labor_policy(), "labor-policy")
    company.save(labor(person_id="prior-contractor"), "prior-labor")
    company.save(accrual(period="2026-02", person_id="current-contractor"), "current-labor")
    company.save(LaborProjectCost(**cost(period="2026-02")), "capital-labor")
    company.save(
        accrual(period="2026-03", person_id="future-contractor", gross_fee_fen=100000),
        "future-labor",
    )
    company.publish(
        "january", "february", "prior-labor", "current-labor", "capital-labor", "future-labor"
    )

    january = employee_data(company.engine, "2026-01")
    assert january["employees"]["outstanding_net_fen"] == 907400
    assert january["outstanding_remuneration_fen"] == 1747400

    february = employee_data(company.engine, "2026-02", limit=1)
    assert february["employees"]["outstanding_net_fen"] == 1814800
    # 1,814,800 wages + 840,000 taxed labor + 208,330 accrual + 1,600,000 capital labor.
    assert february["outstanding_remuneration_fen"] == 4463130
    first_page = february["collections"]["labor_sources"]
    assert first_page["page"]["total_count"] == 2
    assert first_page["page"]["returned_count"] == 1 and first_page["page"]["has_more"]
    assert {item["source_id"] for item in first_page["items"]}.isdisjoint(
        {"prior-labor", "future-labor"}
    )
    for options in (
        {"employee_filter": "employment_unknown"},
        {"employee_filter": "ended"},
        {"employee_id": "employee"},
        {"section": "employees"},
    ):
        selected = employee_data(company.engine, "2026-02", limit=1, **options)
        assert selected["employees"]["outstanding_net_fen"] == 1814800
        assert selected["outstanding_remuneration_fen"] == 4463130


def test_unpaid_remuneration_counts_labor_net_and_excludes_tax_and_owner_reimbursement(tmp_path):
    company = Company(tmp_path / "taxed-labor.sqlite")
    company.save(labor_policy(), "labor-policy")
    company.save(labor(), "labor")
    company.publish("labor")
    january = employee_data(company.engine, "2026-01")
    assert january["employees"]["outstanding_net_fen"] == 0
    assert january["outstanding_remuneration_fen"] == 840000

    company.save(owner_payment("labor", "labor", "tax", 160000), "owner-tax")
    company.save(owner_payment("labor", "labor", "net", 50000), "owner-net")
    company.save(net_payment("labor", "labor", 40000), "company-net")
    company.publish("owner-tax", "owner-net", "company-net")
    february = employee_data(company.engine, "2026-02")
    assert february["employees"]["outstanding_net_fen"] == 0
    assert february["employees"]["direct_net_payments_fen"] == 0
    # The person's net claim falls by 90,000; the new debt to the owner is separate.
    assert february["outstanding_remuneration_fen"] == 750000
    assert employee_data(company.engine, "2026-01")["outstanding_remuneration_fen"] == 840000


@pytest.mark.parametrize("closed", [False, True])
def test_unpaid_capital_labor_keeps_advance_offsets_and_later_partial_settlements(tmp_path, closed):
    company = setup(tmp_path)
    frozen = company.close("2026-01") if closed else None
    january_ledger = company.engine.ledger("2026-01")
    company.save(
        cash_payment("2026-02", 100000, allocation("labor_project_cost", "cost", "net", 100000)),
        "cash-net",
    )
    company.save(owner_payment("labor_project_cost", "cost", "net", 250000), "owner-net")
    company.publish("cash-net", "owner-net")
    company.save(
        cash_payment("2026-03", 200000, allocation("labor_project_cost", "cost", "net", 200000)),
        "future-net",
    )
    company.publish("future-net")

    for period, remaining in (("2026-01", 800000), ("2026-02", 450000), ("2026-03", 250000)):
        data = employee_data(company.engine, period)
        assert data["employees"]["outstanding_net_fen"] == 0
        assert data["outstanding_remuneration_fen"] == remaining
    assert company.engine.ledger("2026-01") == january_ledger
    if closed:
        assert Periods(company.engine).closed_report("2026-01") == frozen


def test_unpaid_remuneration_keeps_opening_net_wages_and_excludes_other_opening_components(
    opening_book,
):
    engine, save, publish, package, _ = opening_book
    components = (
        "net",
        "withheld_tax",
        "employee_social",
        "employee_housing",
        "employer_social",
        "employer_housing",
    )
    package(
        [
            ("opening_bank", "bank-start", {"bank_account_id": "bank", "balance_fen": 19000}),
            *[
                (
                    "opening_payroll_payable",
                    "prior-" + component,
                    {
                        "employee_id": "employee",
                        "recipient_id": "employee" if component == "net" else "authority",
                        "payroll_period": "2025-12",
                        "component": component,
                        "outstanding_fen": 2000,
                    },
                )
                for component in components
            ],
            (
                "opening_obligation",
                "prior-other",
                {
                    "counterparty_id": "contractor",
                    "nature": "other_payable",
                    "outstanding_fen": 7000,
                    "business_reference": "prior-other-payable",
                },
            ),
        ]
    )
    save("labor_accrual", "labor", accrual(gross_fee_fen=5000).model_dump(mode="json"))
    for subject, period, amount in (
        ("paid-january", "2026-01", 600),
        ("paid-february", "2026-02", 50),
    ):
        save(
            "payment",
            subject,
            {
                "period": period,
                "actual_date": period + "-10",
                "direction": "outflow",
                "bank_account_id": "bank",
                "counterparty_id": "employee",
                "amount_fen": amount,
                "allocations": [
                    {
                        "source_kind": "opening_payroll_payable",
                        "source_id": "prior-net",
                        "obligation": "primary",
                        "amount_fen": amount,
                    }
                ],
            },
        )
    publish("labor", "paid-january", "paid-february")
    for period, wages in (("2026-01", 1400), ("2026-02", 1350)):
        data = employee_data(engine, period, employee_id="employee")
        assert data["employees"]["outstanding_net_fen"] == wages
        assert data["collections"]["employees"]["items"][0]["outstanding_net_fen"] == wages
        assert data["outstanding_remuneration_fen"] == wages + 5000


@pytest.mark.parametrize("kind", ["labor_accrual", "labor_project_cost"])
@pytest.mark.parametrize("closed", [False, True])
def test_unpaid_labor_replacement_and_closed_compensation_keep_exact_month_end(
    tmp_path,
    kind,
    closed,
):
    company = Company(tmp_path / "corrected-labor.sqlite")

    def earned(amount):
        if kind == "labor_accrual":
            return accrual(gross_fee_fen=amount)
        return LaborProjectCost(
            **cost(period="2026-01", person_id="contractor", gross_fee_fen=amount)
        )

    company.save(earned(208330), "labor")
    company.publish("labor")
    frozen = company.close("2026-01") if closed else None
    january_ledger = company.engine.ledger("2026-01")
    company.save(earned(300000), "labor", revision=1)
    company.publish("labor", posting_period="2026-02" if closed else None)
    company.save(owner_payment(kind, "labor", "net", 100000), "owner-net")
    company.publish("owner-net")
    company.save(net_payment(kind, "labor", 50000, period="2026-03"), "future-net")
    company.publish("future-net")

    for period, amount in (
        ("2026-01", 208330 if closed else 300000),
        ("2026-02", 200000),
        ("2026-03", 150000),
    ):
        assert employee_data(company.engine, period)["outstanding_remuneration_fen"] == amount
    if closed:
        assert company.engine.ledger("2026-01") == january_ledger
        assert Periods(company.engine).closed_report("2026-01") == frozen


def test_unpaid_remuneration_includes_bonus_net_without_including_bonus_tax(tmp_path):
    company = Company(tmp_path / "bonus-labor.sqlite")
    for source in bonus_sources():
        company.save(source.fact, source.subject_id)
    company.save(bonus(), "bonus")
    company.save(accrual(), "labor")
    company.publish("bonus", "labor")
    data = employee_data(company.engine, "2026-01")
    assert data["employees"]["outstanding_net_fen"] == 2910000
    assert data["outstanding_remuneration_fen"] == 3118330


def test_unknown_labor_outstanding_propagates_without_erasing_known_employee_wages(
    payroll_company,
    monkeypatch,
):
    company = payroll_company
    company.save(accrual(), "labor")
    company.publish("january", "labor")
    monkeypatch.setattr(
        "ai_accounting.kernel.settlement_projection.settlement_labor_outstanding_net",
        lambda *_args, **_kwargs: None,
    )
    data = employee_data(company.engine, "2026-01", employee_id="employee")
    assert data["outstanding_remuneration_fen"] is None
    assert data["employees"]["outstanding_net_fen"] == 907400
    assert data["collections"]["employees"]["items"][0]["outstanding_net_fen"] == 907400


def test_no_established_remuneration_has_zero_unpaid_amount(tmp_path):
    company = Company(tmp_path / "no-remuneration.sqlite")
    company.save(labor_policy(), "labor-policy")
    data = employee_data(company.engine, "2026-01")
    assert data["employees"]["outstanding_net_fen"] == 0
    assert data["outstanding_remuneration_fen"] == 0
