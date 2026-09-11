"""Real typed continuation: balances never become invented historical business."""

import itertools
import json

import pytest

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.opening import CATEGORIES
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.reports import BALANCE_NAMES, CASH_FLOW_NAMES, PROFIT_NAMES, Reports
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.payroll import CumulativeIncomeTaxPolicy


@pytest.fixture
def book(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite", default_registry(), "co", "911100000000000001", "db"
        )
    )
    evidence = engine.register_evidence(
        b"explicit synthetic prior ledger and opening cards",
        "text/plain",
        "opening basis",
        request_id="proof",
    )["digest"]
    counter = itertools.count()

    def save(kind, subject, data, *, revision=0, proof=evidence):
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=revision,
            request_id=f"save-{next(counter)}",
        )

    def publish(*subjects):
        preview = engine.preview(list(subjects))
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"publish-{next(counter)}",
        )

    def package(members, period="2026-01", *, publish_now=True):
        references = []
        counts = dict.fromkeys(CATEGORIES.values(), 0)
        for kind, subject, fields in members:
            save(kind, subject, {"period": period, "package_id": "opening", **fields})
            reference = {"kind": kind, "subject_id": subject}
            if kind == "opening_loan":
                reference["agreement_id"] = fields["agreement_id"]
            references.append(reference)
            counts[CATEGORIES[kind]] += 1
        data = {
            "period": period,
            "package_id": "opening",
            "counts": counts,
            "members": references,
            "completeness_confirmed": True,
        }
        save("opening_package", "opening", data)
        if publish_now:
            publish("opening", *[subject for _, subject, _ in members])
        return data

    return engine, save, publish, package, evidence


def complete_members(save):
    save(
        "loan_agreement",
        "agreement",
        {
            "period": "2026-01",
            "lender_id": "lender",
            "lender_is_licensed": True,
            "currency": "CNY",
            "annual_rate_percent": "12",
            "day_count_basis": "actual_360",
            "maturity_date": "2027-01-01",
            "loan_term": "short_term",
        },
    )
    return [
        ("opening_bank", "bank-opening", {"bank_account_id": "bank", "balance_fen": 1000000}),
        ("opening_cash", "cash-opening", {"cash_account_id": "cash", "balance_fen": 10000}),
        (
            "opening_obligation",
            "receivable",
            {
                "counterparty_id": "customer",
                "nature": "customer_receivable",
                "outstanding_fen": 20000,
                "business_reference": "prior-sale-1",
            },
        ),
        (
            "opening_asset",
            "machine",
            {
                "asset_type": "fixed",
                "cost_fen": 120000,
                "accumulated_fen": 20000,
                "in_use_date": "2025-10-12",
                "useful_life_months": 12,
                "completed_months": 2,
                "residual_fen": 0,
                "benefit_area": "administration",
                "rounding_policy": "floor_final_remainder",
            },
        ),
        (
            "opening_loan",
            "loan",
            {
                "agreement_id": "agreement",
                "lender_id": "lender",
                "loan_term": "short_term",
                "principal_fen": 300000,
                "accrued_interest_fen": 3000,
                "interest_start": "2026-01-01",
            },
        ),
        (
            "opening_tax",
            "vat-payable",
            {
                "authority_id": "tax-authority",
                "tax_kind": "vat",
                "balance_kind": "payable",
                "outstanding_fen": 20000,
                "tax_period": "2025-12",
            },
        ),
        (
            "opening_payroll_payable",
            "wage-payable",
            {
                "employee_id": "employee",
                "recipient_id": "employee",
                "payroll_period": "2025-12",
                "component": "net",
                "outstanding_fen": 50000,
            },
        ),
        (
            "opening_equity",
            "equity",
            {
                "equity_kind": "retained_earnings",
                "balance_fen": 757000,
                "holder_or_basis_id": "prior-statements",
            },
        ),
    ]


def test_complete_opening_has_no_voucher_income_expense_or_cashflow(book):
    engine, save, _, package, _ = book
    package(complete_members(save))
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher_version").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM monthly_account").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM monthly_cashflow").fetchone()[0] == 0
        assert tuple(
            connection.execute("SELECT sum(debit),sum(credit) FROM opening_account").fetchone()
        ) == (1150000, 1150000)
        before = [
            tuple(row)
            for row in connection.execute("SELECT * FROM balance ORDER BY category,balance_key")
        ]
    engine.rebuild_projections(request_id="rebuild")
    with engine.store.connection(read_only=True) as connection:
        assert [
            tuple(row)
            for row in connection.execute("SELECT * FROM balance ORDER BY category,balance_key")
        ] == before


@pytest.mark.parametrize(
    "problem", ["unbalanced", "missing_member", "wrong_count", "duplicate_bank"]
)
def test_incomplete_opening_is_not_publishable(book, problem):
    engine, save, publish, package, _ = book
    members = [
        ("opening_bank", "bank-opening", {"bank_account_id": "bank", "balance_fen": 100}),
        (
            "opening_equity",
            "equity",
            {"equity_kind": "paid_in_capital", "balance_fen": 100, "holder_or_basis_id": "owner"},
        ),
    ]
    if problem == "unbalanced":
        members[1][2]["balance_fen"] = 90
    if problem == "duplicate_bank":
        members.append(
            ("opening_bank", "bank-again", {"bank_account_id": "bank", "balance_fen": 0})
        )
    data = package(members, publish_now=False)
    if problem in {"missing_member", "wrong_count"}:
        if problem == "missing_member":
            data["members"].pop()
        else:
            data["counts"]["bank"] += 1
        engine.amend_fact(
            "opening_package",
            "opening",
            data,
            expected_revision=1,
            evidence=(book[-1],),
            recording_error_confirmed=True,
            request_id="amend",
        )
    with pytest.raises(KernelError):
        publish("opening", *[subject for _, subject, _ in members])
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM opening_account").fetchone()[0] == 0


def test_detail_cannot_be_consumed_before_package_and_later_real_receipt_settles_it(book):
    engine, save, publish, package, _ = book
    members = [
        (
            "opening_obligation",
            "receivable",
            {
                "counterparty_id": "customer",
                "nature": "customer_receivable",
                "outstanding_fen": 20000,
                "business_reference": "old-invoice",
            },
        ),
        (
            "opening_equity",
            "equity",
            {
                "equity_kind": "retained_earnings",
                "balance_fen": 20000,
                "holder_or_basis_id": "prior-statements",
            },
        ),
    ]
    package(members, publish_now=False)
    with pytest.raises(NeedsInformation):
        publish("receivable")
    publish("opening", "receivable", "equity")
    save(
        "payment",
        "receipt",
        {
            "period": "2026-01",
            "actual_date": "2026-01-15",
            "direction": "inflow",
            "bank_account_id": "bank",
            "counterparty_id": "customer",
            "amount_fen": 12000,
            "allocations": [
                {
                    "source_kind": "opening_obligation",
                    "source_id": "receivable",
                    "obligation": "primary",
                    "amount_fen": 12000,
                }
            ],
        },
    )
    publish("receipt")
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance "
                "WHERE balance_key='opening_obligation:receivable:primary'"
            ).fetchone()[0]
            == 8000
        )
        assert (
            connection.execute(
                "SELECT sum(debit) FROM monthly_account WHERE account='5001'"
            ).fetchone()[0]
            is None
        )


def test_asset_depreciation_and_split_loan_interest_continue_without_fake_drawdown(book):
    engine, save, publish, package, _ = book
    package(complete_members(save))
    save("asset_consumption", "depreciation", {"period": "2026-01", "asset_id": "machine"})
    save(
        "payment",
        "principal-payment",
        {
            "period": "2026-01",
            "actual_date": "2026-01-16",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "lender",
            "amount_fen": 100000,
            "allocations": [
                {
                    "source_kind": "opening_loan",
                    "source_id": "loan",
                    "obligation": "principal",
                    "amount_fen": 100000,
                }
            ],
        },
    )
    for subject, first, end in (
        ("interest-first", "2026-01-01", "2026-01-16"),
        ("interest-second", "2026-01-16", "2026-02-01"),
    ):
        save(
            "loan_interest",
            subject,
            {
                "period": "2026-01",
                "drawdown_id": "loan",
                "agreement_id": "agreement",
                "period_start": first,
                "period_end_exclusive": end,
            },
        )
    publish("depreciation", "principal-payment", "interest-first", "interest-second")
    with engine.store.connection(read_only=True) as connection:
        calculations = {
            row["subject_id"]: json.loads(row["outcome"])["values"]
            for row in connection.execute(
                "SELECT c.* FROM calculation c JOIN calculation_current a ON a.calculation_id=c.id"
            )
        }
        assert calculations["depreciation"]["consumption_fen"] == 10000
        assert calculations["depreciation"]["closing_accumulated_fen"] == 30000
        assert calculations["interest-first"]["interest_fen"] == 1500
        assert calculations["interest-second"]["interest_fen"] == 1067
        assert connection.execute("SELECT count(*) FROM fact_loan_drawdown").fetchone()[0] == 0


def test_january_continuation_report_uses_opening_cash_without_cashflow(book):
    engine, save, _, package, _ = book
    package(
        [
            ("opening_bank", "bank-opening", {"bank_account_id": "bank", "balance_fen": 10000}),
            (
                "opening_equity",
                "equity",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 10000,
                    "holder_or_basis_id": "owner",
                },
            ),
        ]
    )
    save(
        "continuation_report_profile",
        "profile",
        {
            "period": "2026-01",
            "company_name": "接续测试",
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": "2026-01",
            "opening_package_id": "opening",
        },
    )
    save(
        "report_income_tax_confirmation",
        "cit",
        {
            "period": "2026-03",
            "treatment": "zero",
            "cumulative_assessed_fen": 0,
            "calculation_id": None,
            "explanation": "明确为零",
        },
    )
    result = Reports(engine).report(2026, 1)
    assert result["status"] == "ready", result["fact_issues"]
    assert result["statements"]["balance_sheet"]["1"]["beginning_fen"] == 10000
    assert result["statements"]["cash_flow_statement"]["20"]["year_to_date_fen"] == 0
    assert result["statements"]["cash_flow_statement"]["21"]["year_to_date_fen"] == 10000


def _close_without_current_business(engine, period, proof):
    periods = Periods(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            period,
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=proof,
            request_id=f"inventory-{period}-{category}",
        )
    preview = periods.preview_close(period, owner_confirmation=proof)
    return periods.close(
        period,
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=f"close-{period}",
    )


def test_zero_opening_remains_a_frozen_boundary_after_close(book):
    engine, _, _, package, proof = book
    data = package([])
    _close_without_current_business(engine, "2026-01", proof)
    engine.amend_fact(
        "opening_package",
        "opening",
        data | {"period": "2026-02"},
        evidence=(proof,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="amend-zero",
    )
    preview = engine.preview(["opening"])
    with pytest.raises(KernelError) as error:
        engine.confirm(
            ["opening"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="move-zero",
        )
    assert error.value.code == "closed_opening_immutable"


def test_midyear_report_supplement_after_close_is_an_explicit_frozen_reference(book):
    engine, save, _, package, proof = book
    package(
        [
            ("opening_cash", "cash-opening", {"cash_account_id": "cash", "balance_fen": 10000}),
            (
                "opening_equity",
                "equity",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 10000,
                    "holder_or_basis_id": "owner",
                },
            ),
        ],
        period="2026-07",
    )
    save(
        "continuation_report_profile",
        "profile",
        {
            "period": "2026-07",
            "company_name": "年中接续",
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": "2026-07",
            "opening_package_id": "opening",
        },
    )
    save(
        "report_income_tax_confirmation",
        "cit",
        {
            "period": "2026-09",
            "treatment": "zero",
            "cumulative_assessed_fen": 0,
            "calculation_id": None,
            "explanation": "明确为零",
        },
    )
    assert any(
        item["field"] == "report_carry_forward"
        for item in Reports(engine).report(2026, 3)["fact_issues"]
    )
    for period in ("2026-07", "2026-08", "2026-09"):
        _close_without_current_business(engine, period, proof)
    with engine.store.connection(read_only=True) as connection:
        frozen = [
            tuple(row) for row in connection.execute("SELECT * FROM period_close ORDER BY period")
        ]
        epochs = engine.store.epochs(connection)
    prior_balance = {line: 0 for line in BALANCE_NAMES}
    for line in (1, 15, 30, 48, 52, 53):
        prior_balance[line] = 10000
    prior_cash = {line: 0 for line in CASH_FLOW_NAMES}
    prior_cash[21] = prior_cash[22] = 10000
    saved = save(
        "report_carry_forward",
        "prior-statements",
        {
            "period": "2026-07",
            "opening_package_id": "opening",
            "year_beginning_balance": [
                {"line": line, "beginning_fen": value} for line, value in prior_balance.items()
            ],
            "prior_profit": [
                {"line": line, "quarter_to_date_fen": 0, "year_to_date_fen": 0}
                for line in PROFIT_NAMES
            ],
            "prior_cash": [
                {"line": line, "quarter_to_date_fen": value, "year_to_date_fen": value}
                for line, value in prior_cash.items()
            ],
        },
    )
    report = Reports(engine)
    assert report.report(2026, 3, source="closed")["status"] == "needs_information"
    plan = report.preview_export(2026, 3, carry_forward_fact_id=saved["fact_id"])
    assert plan["status"] == "ready", plan["fact_issues"]
    assert saved["fact_id"] in plan["report_fact_ids"]
    with engine.store.connection(read_only=True) as connection:
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM period_close ORDER BY period")
        ] == frozen
        assert engine.store.epochs(connection)["accounting"] == epochs["accounting"]


def test_opening_bank_reconciles_first_real_statement(book):
    engine, save, publish, package, _ = book
    package(
        [
            ("opening_bank", "bank-opening", {"bank_account_id": "bank", "balance_fen": 10000}),
            (
                "opening_equity",
                "equity",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 10000,
                    "holder_or_basis_id": "owner",
                },
            ),
        ]
    )
    save(
        "bank_statement",
        "statement",
        {
            "period": "2026-01",
            "bank_account_id": "bank",
            "opening_fen": 10000,
            "closing_fen": 10000,
            "entries": [],
        },
    )
    save(
        "bank_reconciliation",
        "reconciliation",
        {
            "period": "2026-01",
            "bank_account_id": "bank",
            "statement_id": "statement",
            "matches": [],
        },
    )
    publish("statement", "reconciliation")
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher_current").fetchone()[0] == 0


def test_midyear_payroll_uses_confirmed_cumulative_state_without_recreating_six_months(book):
    engine, save, publish, package, _ = book
    policy = CumulativeIncomeTaxPolicy.china_resident_wage_withholding()
    save(
        "payroll_income_tax_policy",
        "tax-policy",
        {
            "period": "2026-07",
            "version": policy.version,
            "effective_from": policy.effective_from.isoformat(),
            "effective_to": None,
            "primary_source_url": policy.primary_source_url,
            "legal_basis_source_url": policy.legal_basis_source_url,
            "monthly_standard_deduction_fen": policy.monthly_standard_deduction_fen,
            "brackets": [
                {
                    "upper_bound_fen": item.upper_bound_fen,
                    "rate": str(item.rate),
                    "quick_deduction_fen": item.quick_deduction_fen,
                }
                for item in policy.brackets
            ],
        },
    )
    save(
        "payroll_contribution_policy",
        "contribution-policy",
        {
            "period": "2026-07",
            "version": "explicit-test-policy",
            "jurisdiction": "test",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "primary_source_url": "https://www.mof.gov.cn/",
            "rules": [
                {
                    "code": "pension",
                    "base_kind": "social_insurance",
                    "employee_rate": "0.08",
                    "employer_rate": "0.16",
                    "minimum_base_fen": 0,
                    "maximum_base_fen": 10000000,
                    "rounding": "half_up",
                    "enabled": True,
                }
            ],
        },
    )
    save(
        "payroll_profile",
        "employee-profile",
        {
            "period": "2026-07",
            "employee_id": "employee",
            "effective_from": "2026-01",
            "effective_to": None,
            "withholding_start_date": "2026-01-01",
            "social_insurance_base_fen": None,
            "housing_fund_base_fen": None,
            "social_insurance_participating": False,
            "housing_fund_participating": False,
            "contribution_shortfall": "reject",
        },
    )
    package(
        [
            (
                "opening_payroll_state",
                "cumulative",
                {
                    "employee_id": "employee",
                    "through_period": "2026-06",
                    "separate_method_already_used": False,
                    "cumulative_income_fen": 6000000,
                    "cumulative_tax_exempt_income_fen": 0,
                    "cumulative_standard_deduction_fen": 3000000,
                    "cumulative_employee_contributions_fen": 0,
                    "cumulative_special_additional_deduction_fen": 0,
                    "cumulative_other_legal_deduction_fen": 0,
                    "cumulative_tax_relief_fen": 0,
                    "cumulative_withheld_tax_fen": 90000,
                },
            )
        ],
        period="2026-07",
    )
    save(
        "payroll",
        "july-payroll",
        {
            "period": "2026-07",
            "employee_id": "employee",
            "profile_id": "employee-profile",
            "contribution_policy_id": "contribution-policy",
            "income_tax_policy_id": "tax-policy",
            "accounting_gross_salary_fen": 1000000,
            "tax_reported_salary_fen": 1000000,
            "tax_exempt_income_fen": 0,
            "special_additional_deduction_fen": 0,
            "other_legal_deduction_fen": 0,
            "tax_relief_fen": 0,
            "expense_class": "management",
            "contribution_basis": "policy_until_actual",
        },
    )
    publish("july-payroll")
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT outcome FROM calculation WHERE subject_id='july-payroll'"
        ).fetchone()
        values = json.loads(row[0])["values"]
        assert values["tax_fen"] == 15000
        assert values["tax_state"]["cumulative_income_fen"] == 7000000
        assert connection.execute("SELECT count(*) FROM voucher_current").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM fact_payroll").fetchone()[0] == 1
