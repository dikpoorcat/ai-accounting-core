"""Independent public-app financial oracles; freezing is an explicit white-box fixture.

These tests review financial selection, not delivery of an application close command.
No expected number is produced by ledger reporting helpers or fixed-equation functions.
"""

import json
from pathlib import Path

import pytest
from _financial_snapshot_review_support import (
    ASSET,
    BANK,
    PARTY_A,
    PARTY_B,
    PERSON,
    SOURCE,
    assemble,
    continuation,
    drawdown,
    expense,
    fact,
    loan_contract,
    member,
    opening,
    payment,
    sale,
    zero_rate_policy,
)

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.public import LedgerQuery


def amount(snapshot, table, line, column="current"):
    return snapshot[table][str(line)][column]


def known(value):
    return {"state": "known", "amount_fen": value}


def test_review_R01_full_fixed_numbers_through_real_sale_money_expense_and_income_tax(tmp_path):
    book = assemble(tmp_path)
    # Move the monthly example to a real quarter end supported by the EIT owner.
    sold = sale("R01-sold", "100000", "2026-03")
    cost = expense("R01-cost", "20000", "2026-03")
    income_tax = fact(
        "R01-eit",
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": "confirmed_provision",
            "tax_year": 2026,
            "tax_period_end": "2026-03",
            "cumulative_tax_fen": "4000",
            "source": {"source_id": SOURCE},
        },
        "2026-03",
    )
    book.submit(
        opening(cash="130000"),
        zero_rate_policy(),
        sold,
        cost,
        income_tax,
        payment("R01-received", sold, "100000", incoming=True, on="2026-03-21"),
        payment("R01-paid", cost, "20000", on="2026-03-22"),
    )
    report = book.snapshot("2026-03")
    cases = json.loads(
        (
            Path(__file__).resolve().parents[3] / "docs/design/examples/reporting-cases.json"
        ).read_text(encoding="utf-8")
    )
    fixed = cases["cases"][0]["expected_snapshot"]
    assert cases["cases"][0]["id"] == "R01"
    for table in ("balance_sheet", "profit_statement", "cash_flow_statement"):
        columns = (
            ("beginning", "ending") if table == "balance_sheet" else ("current", "year_to_date")
        )
        for line, row in fixed[table].items():
            for column in columns:
                assert report[table][line][column] == row[column], (table, line, column)
    assert report["pre_tax"]["profit_before_tax"] == known("80000")
    assert report["status"] == "ready", report["issues"]


def test_review_R02_two_customers_are_not_netted_into_one_receivable(tmp_path):
    book = assemble(tmp_path)
    receivable = member(
        "R02-receivable",
        {
            "type": "obligation",
            "counterparty_id": PARTY_A,
            "nature": "customer_receivable",
            "amount_fen": "10000",
            "original_business_reference": "old-A-sale",
        },
    )
    prepaid = fact(
        "R02-B-advance",
        "operations.advance",
        {
            "side": "customer",
            "counterparty_id": PARTY_B,
            "amount_fen": "3000",
            "tax_status": "not_due",
        },
    )
    book.submit(
        opening(equity="10000", members=[receivable]),
        prepaid,
        payment("R02-B-received", prepaid, "3000", incoming=True),
    )
    report = book.snapshot()
    assert amount(report, "balance_sheet", 4, "ending") == known("10000")
    assert amount(report, "balance_sheet", 34, "ending") == known("3000")
    assert amount(report, "balance_sheet", 1, "ending") == known("3000")
    assert report["status"] == "ready", report["issues"]


def test_review_complete_zero_origin_has_exact_rows_checks_and_no_fake_movements(tmp_path):
    book = assemble(tmp_path)
    book.submit(opening(basis="newly_established"))
    report = book.snapshot()
    assert report["status"] == "ready"
    assert report["issues"] == []
    for name, count in (
        ("balance_sheet", 53),
        ("profit_statement", 32),
        ("cash_flow_statement", 22),
    ):
        assert set(report[name]) == {str(i) for i in range(1, count + 1)}
        for row in report[name].values():
            for column in (
                ("beginning", "ending") if name == "balance_sheet" else ("current", "year_to_date")
            ):
                assert row[column] == known("0")
    assert len(report["checks"]) == 10
    assert {check["status"] for check in report["checks"]} == {"pass"}
    with book.app.storage.read_unit(book.company) as read:
        assert book.app.ledger.query_vouchers(LedgerQuery(), read).total == 0
        assert book.app.ledger.fund_movements(BANK, "2026-01", "2026-01", read) == ()


def test_review_R07_annual_beginning_differs_from_bookkeeping_and_month_cash_beginning(tmp_path):
    book = assemble(tmp_path, "2026-07")
    initial = opening(month="2026-07", cash="500000")
    sold = sale("july-service", "100000", "2026-07")
    book.submit(
        initial,
        continuation(initial),
        zero_rate_policy(),
        sold,
        payment("july-received", sold, "100000", incoming=True, on="2026-07-22"),
    )
    report = book.snapshot("2026-07")
    assert report["status"] == "ready", report["issues"]
    assert amount(report, "balance_sheet", 1, "beginning") == known("300000")
    assert amount(report, "balance_sheet", 1, "ending") == known("600000")
    assert amount(report, "cash_flow_statement", 21) == known("500000")
    assert amount(report, "cash_flow_statement", 21, "year_to_date") == known("300000")
    assert amount(report, "cash_flow_statement", 20, "year_to_date") == known("300000")
    assert amount(report, "cash_flow_statement", 22) == known("600000")
    assert amount(report, "cash_flow_statement", 22, "year_to_date") == known("600000")
    assert amount(report, "profit_statement", 30) == known("100000")
    assert amount(report, "profit_statement", 30, "year_to_date") == known("200000")


def test_review_R08_prior_quarter_added_once_and_not_in_later_quarter(tmp_path):
    book = assemble(tmp_path, "2026-05")
    initial = opening(month="2026-05", cash="500000")
    may, june = sale("may-sale", "20000", "2026-05"), sale("june-sale", "30000", "2026-06")
    book.submit(initial, continuation(initial, may=True), zero_rate_policy(), may, june)
    quarter = book.snapshot("2026-04", "2026-06")
    assert amount(quarter, "profit_statement", 1) == known("130000")
    assert amount(quarter, "profit_statement", 1, "year_to_date") == known("350000")
    july = book.snapshot("2026-07", "2026-09")
    assert amount(july, "profit_statement", 1) == known("0")
    assert amount(july, "profit_statement", 1, "year_to_date") == known("350000")
    assert amount(quarter, "cash_flow_statement", 21) == known("450000")
    assert amount(quarter, "cash_flow_statement", 21, "year_to_date") == known("300000")


def test_review_R09_missing_comparisons_preserve_current_facts_without_assumed_zero(tmp_path):
    book = assemble(tmp_path, "2026-07")
    book.submit(
        opening(month="2026-07", cash="500000", basis="newly_established"),
        expense("current-expense", "20000", "2026-07"),
    )
    report = book.snapshot("2026-07")
    assert report["status"] == "partial"
    assert amount(report, "balance_sheet", 1, "ending") == known("500000")
    assert amount(report, "balance_sheet", 1, "beginning")["state"] == "unestablished"
    assert amount(report, "profit_statement", 14) == known("20000")
    assert amount(report, "profit_statement", 14, "year_to_date")["state"] == "unestablished"
    assert report["pre_tax"]["profit_before_tax"]["state"] == "unestablished"


def test_review_opening_correction_uses_one_cutoff_basis_and_never_creates_cashflow(tmp_path):
    book = assemble(tmp_path)
    initial = opening(cash="100000")
    book.submit(initial)
    book.freeze_formal_heads_for_test("2026-01")
    for item in initial["data"]["members"]:
        item["data"]["balance_fen"] = "200000"
    book.replace(initial, correction_period="2026-02")
    january = book.snapshot(view="as_posted")
    february = book.snapshot("2026-02", view="as_posted")
    assert amount(january, "balance_sheet", 1, "beginning") == known("100000")
    assert amount(january, "balance_sheet", 1, "ending") == known("100000")
    for column in ("beginning", "ending"):
        assert amount(february, "balance_sheet", 1, column) == known("200000")
    for column in ("current", "year_to_date"):
        assert amount(february, "cash_flow_statement", 20, column) == known("0")
        assert amount(february, "cash_flow_statement", 21, column) == known("200000")
        assert amount(february, "cash_flow_statement", 22, column) == known("200000")
    assert february["status"] == "ready", february["issues"]


def test_review_R10_non_cash_corrections_use_deltas_and_final_current_target_once(tmp_path):
    book = assemble(tmp_path)
    business = expense("original-unpaid", "100000")
    book.submit(opening(), business)
    book.freeze_formal_heads_for_test("2026-01")
    business["data"]["amount_fen"] = "90000"
    business["data"]["report_details"][0]["amount_fen"] = "90000"
    book.replace(business, correction_period="2026-02")
    business["data"]["amount_fen"] = "80000"
    business["data"]["report_details"][0]["amount_fen"] = "80000"
    book.replace(business, seq=2, correction_period="2026-02")
    january = book.snapshot(view="as_posted")
    february = book.snapshot("2026-02", view="as_posted")
    current = book.snapshot()
    assert amount(january, "profit_statement", 14) == known("100000")
    assert amount(february, "profit_statement", 14) == known("-20000")
    assert amount(current, "profit_statement", 14) == known("80000")
    assert amount(february, "cash_flow_statement", 20) == known("0")
    assert amount(february, "profit_statement", 14, "year_to_date") == known("80000")
    assert current["status"] == february["status"] == "ready"


def test_review_R11_real_same_money_identity_has_only_each_frozen_period_delta(tmp_path):
    book = assemble(tmp_path)
    sold = sale("R11-sale", "100000")
    paid = payment("R11-received", sold, "100000", incoming=True)
    book.submit(opening(), zero_rate_policy(), sold, paid)
    book.freeze_formal_heads_for_test("2026-01")
    sold["data"]["gross_fen"] = "90000"
    paid["data"]["amount_fen"] = paid["data"]["allocations"][0]["amount_fen"] = "90000"
    book.replace(sold, paid, correction_period="2026-02")
    book.freeze_formal_heads_for_test("2026-02")
    sold["data"]["gross_fen"] = "80000"
    paid["data"]["amount_fen"] = paid["data"]["allocations"][0]["amount_fen"] = "80000"
    book.replace(sold, paid, seq=2, correction_period="2026-03")
    january = book.snapshot(view="as_posted")
    february = book.snapshot("2026-02", view="as_posted")
    march = book.snapshot("2026-03", view="as_posted")
    assert amount(january, "cash_flow_statement", 1) == known("100000")
    assert amount(february, "cash_flow_statement", 1) == known("-10000")
    assert amount(march, "cash_flow_statement", 1) == known("-10000")
    assert amount(march, "cash_flow_statement", 1, "year_to_date") == known("80000")
    assert amount(book.snapshot(), "cash_flow_statement", 1) == known("80000")
    with book.app.storage.read_unit(book.company) as read:
        movements = book.app.ledger.fund_movements(BANK, "2026-01", "2026-03", read)
        assert len(movements) == 1
        assert movements[0].value["amount_fen"] == "80000"


def test_review_R15_real_expense_to_asset_handoff_preserves_money_and_changes_classification(
    tmp_path,
):
    book = assemble(tmp_path)
    business = expense("R15-misclassified", "100000")
    book.submit(opening(cash="200000"), business, payment("R15-paid", business, "100000"))
    before = book.snapshot()
    assert amount(before, "cash_flow_statement", 6) == known("100000")
    business["kind"] = "assets.acquisition"
    business["data"] = {
        "type": "direct",
        "asset": {"asset_id": ASSET, "asset_type": "fixed", "cost_fen": "100000"},
        "supplier_id": PARTY_A,
    }
    book.replace(business)
    after = book.snapshot()
    assert amount(after, "profit_statement", 14) == known("0")
    assert amount(after, "balance_sheet", 21, "ending") == known("100000")
    assert amount(after, "cash_flow_statement", 6) == known("0")
    assert amount(after, "cash_flow_statement", 12) == known("100000")
    assert after["status"] == "ready", after["issues"]
    with book.app.storage.read_unit(book.company) as read:
        assert len(book.app.ledger.fund_movements(BANK, "2026-01", "2026-01", read)) == 1


def test_review_R16_frozen_expense_withdrawal_has_negative_profit_but_no_refund(tmp_path):
    book = assemble(tmp_path)
    business = expense("R16-wrong", "100000")
    book.submit(opening(), business)
    book.freeze_formal_heads_for_test("2026-01")
    book.submit(
        mutations=[{"op": "withdraw", "business_id": business["business_id"], "expected_seq": 1}],
        correction_period="2026-02",
        explanation="原误记费用没有对应的现实业务。",
    )
    january = book.snapshot(view="as_posted")
    february = book.snapshot("2026-02", view="as_posted")
    assert amount(january, "profit_statement", 14) == known("100000")
    assert amount(february, "profit_statement", 14) == known("-100000")
    assert amount(february, "cash_flow_statement", 20) == known("0")
    assert amount(book.snapshot(), "profit_statement", 14) == known("0")


def test_review_R18_distinct_loans_classify_at_exact_twelve_month_boundary(tmp_path):
    book = assemble(tmp_path)
    inside = loan_contract("loan-inside", "2027-12-31")
    outside = loan_contract("loan-outside", "2028-01-01")
    book.submit(
        opening(),
        inside,
        outside,
        drawdown("draw-inside", inside, "100000"),
        drawdown("draw-outside", outside, "200000"),
    )
    report = book.snapshot("2026-12")
    assert amount(report, "balance_sheet", 40, "ending") == known("100000")
    assert amount(report, "balance_sheet", 42, "ending") == known("200000")
    assert amount(report, "balance_sheet", 1, "ending") == known("300000")
    with book.app.storage.read_unit(book.company) as read:
        assert (
            book.app.ledger.query_vouchers(
                LedgerQuery(period_from="2026-12", period_to="2026-12"), read
            ).total
            == 0
        )


def test_review_R19_beginning_and_ending_classify_same_opening_loan_separately(tmp_path):
    book = assemble(tmp_path)
    contract = loan_contract("opening-loan-contract", "2027-06-30", month="2025-01")
    loan = member(
        "opening-loan-member",
        {
            "type": "loan",
            "contract_id": contract["business_id"],
            "lender_id": PARTY_B,
            "principal_fen": "100000",
            "accrued_interest_fen": "0",
            "interest_start_on": "2026-01-01",
        },
    )
    book.submit(contract, opening(cash="100000", equity="0", members=[loan]))
    report = book.snapshot("2026-12")
    assert amount(report, "balance_sheet", 40, "beginning") == known("0")
    assert amount(report, "balance_sheet", 42, "beginning") == known("100000")
    assert amount(report, "balance_sheet", 40, "ending") == known("100000")
    assert amount(report, "balance_sheet", 42, "ending") == known("0")


def test_review_as_posted_loan_terms_come_from_old_adoption_not_current_contract(tmp_path):
    book = assemble(tmp_path)
    contract = loan_contract("amended-contract", "2028-01-01")
    book.submit(opening(), contract, drawdown("amended-draw", contract, "100000"))
    book.freeze_formal_heads_for_test("2026-01")
    contract["data"]["matures_on"] = "2027-01-01"
    book.replace(contract, correction_period="2026-02")
    past = book.snapshot(view="as_posted")
    current = book.snapshot()
    assert amount(past, "balance_sheet", 40, "ending") == known("0")
    assert amount(past, "balance_sheet", 42, "ending") == known("100000")
    assert amount(current, "balance_sheet", 40, "ending") == known("100000")
    assert amount(current, "balance_sheet", 42, "ending") == known("0")


def test_review_unknown_cash_does_not_poison_profit_and_identical_frozen_result_cancels(tmp_path):
    book = assemble(tmp_path)
    debt = member(
        "unknown-reimbursement",
        {
            "type": "obligation",
            "counterparty_id": PARTY_A,
            "nature": "employee_reimbursement",
            "amount_fen": "10000",
            "original_business_reference": "unclassified-old-cost",
        },
    )
    initial = opening(cash="100000", equity="90000", members=[debt])
    paid = payment("unknown-cost-paid", initial, "5000", slot="member:" + debt["member_id"])
    book.submit(initial, paid, expense("known-expense", "20000"))
    january = book.snapshot()
    assert january["status"] == "partial"
    assert amount(january, "balance_sheet", 1, "ending") == known("95000")
    assert amount(january, "profit_statement", 30) == known("-20000")
    assert january["pre_tax"]["profit_before_tax"] == known("-20000")
    assert any(
        amount(january, "cash_flow_statement", line)["state"] == "unestablished"
        for line in range(1, 20)
    )
    book.freeze_formal_heads_for_test("2026-01")
    february = book.snapshot("2026-02", view="as_posted")
    for line in range(1, 21):
        assert amount(february, "cash_flow_statement", line) == known("0")
    assert amount(february, "cash_flow_statement", 21) == known("95000")
    assert amount(february, "cash_flow_statement", 22) == known("95000")
    assert amount(february, "profit_statement", 30) == known("0")


def test_review_R21_unknown_report_details_have_known_main_and_are_replaceable(tmp_path):
    book = assemble(tmp_path)
    business = expense("detail-missing", "100000", details=False)
    book.submit(opening(), business)
    before = book.snapshot()
    assert amount(before, "profit_statement", 14) == known("100000")
    assert before["pre_tax"]["profit_before_tax"] == known("-100000")
    for line in (15, 16, 17):
        assert amount(before, "profit_statement", line)["state"] == "unestablished"
    business["data"]["report_details"] = [{"code": "management_other", "amount_fen": "100000"}]
    book.replace(business)
    after = book.snapshot()
    for line in (15, 16, 17):
        assert amount(after, "profit_statement", line) == known("0")
    assert after["status"] == "ready"


def test_review_payroll_known_categories_do_not_need_fictitious_institution_parties(tmp_path):
    book = assemble(tmp_path)
    employed = fact(
        "review-employment",
        "people.employment",
        {
            "employee_id": PERSON,
            "effective_from": "2026-01",
            "social_insurance_participating": True,
            "housing_fund_participating": True,
            "social_insurance_base_fen": "100000",
            "housing_fund_base_fen": "100000",
            "contribution_shortfall": "reject",
        },
    )
    actual = fact(
        "review-contributions",
        "people.contribution_actual",
        {
            "employee_id": PERSON,
            "items": [
                {
                    "code": "social",
                    "base_kind": "social_insurance",
                    "state": "declared",
                    "employee_fen": "10000",
                    "employer_fen": "20000",
                },
                {
                    "code": "housing",
                    "base_kind": "housing_fund",
                    "state": "declared",
                    "employee_fen": "5000",
                    "employer_fen": "5000",
                },
            ],
        },
    )
    wage = fact(
        "review-wage",
        "people.remuneration",
        {
            "type": "wage",
            "person_id": PERSON,
            "gross_fen": "100000",
            "expense_area": "administration",
            "employment_id": employed["business_id"],
            "contribution_basis": "actual_required",
            "contribution_actual_id": actual["business_id"],
            "tax_basis": {
                "tax_period": "2026-01",
                "tax_fen": "2000",
                "basis_type": "final_statement",
                "source": {"source_id": SOURCE},
            },
        },
    )
    book.submit(opening(), employed, actual, wage)
    report = book.snapshot()
    assert amount(report, "profit_statement", 14) == known("125000")
    assert amount(report, "balance_sheet", 35, "ending") == known("108000")
    assert amount(report, "balance_sheet", 36, "ending") == known("2000")
    assert amount(report, "balance_sheet", 39, "ending") == known("15000")
    assert amount(report, "balance_sheet", 30, "ending") == known("0")
    assert amount(report, "balance_sheet", 53, "ending") == known("0")
    assert report["status"] == "ready", report["issues"]


def test_review_companies_with_identical_business_ids_remain_independent(tmp_path):
    first, second = assemble(tmp_path / "first"), assemble(tmp_path / "second")
    first.submit(opening(cash="100000"), expense("same-business", "10000"))
    second.submit(opening(cash="200000"), expense("same-business", "30000"))
    one, two = first.snapshot(), second.snapshot()
    assert one["database_id"] != two["database_id"]
    assert amount(one, "balance_sheet", 1, "ending") == known("100000")
    assert amount(two, "balance_sheet", 1, "ending") == known("200000")
    assert amount(one, "profit_statement", 14) == known("10000")
    assert amount(two, "profit_statement", 14) == known("30000")
    with first.app.storage.read_unit(first.company) as read:
        with pytest.raises(ContractError) as error:
            first.app.ledger.financial_snapshot(
                {
                    "company_id": second.company,
                    "view": "current",
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                },
                read,
            )
        assert error.value.code == "invalid_input"


def test_review_expected_head_tracks_report_scope_not_unrelated_global_changes(tmp_path):
    book = assemble(tmp_path)
    business = expense("scoped-expense", "10000")
    book.submit(opening(), business)
    original = book.snapshot()
    expected = original["selection"]["selection_digest"]
    book.submit(expense("future-unrelated", "20000", "2026-02"))
    repeated = book.snapshot(expected_head=expected)
    assert repeated["head"] > original["head"]
    assert repeated["selection"]["selection_digest"] == expected
    business["data"]["amount_fen"] = "5000"
    business["data"]["report_details"][0]["amount_fen"] = "5000"
    book.replace(business)
    with pytest.raises(ContractError) as error:
        book.snapshot(expected_head=expected)
    assert error.value.code == "snapshot_changed"


@pytest.mark.parametrize(
    "first,last",
    [
        ("2026-01", "2026-02"),
        ("2026-02", "2026-04"),
        ("2026-12", "2027-01"),
        ("2026-04", "2026-03"),
    ],
)
def test_review_rejects_ranges_that_are_neither_one_month_nor_one_natural_quarter(
    tmp_path, first, last
):
    book = assemble(tmp_path)
    book.submit(opening())
    with pytest.raises(ContractError) as error:
        book.snapshot(first, last)
    assert error.value.code == "invalid_input"
