"""Old dashboard contracts over synthetic typed SQLite business, without the retired ORM."""

import json

import pytest
from test_banking import book as _bank_book
from test_banking import entry, funding, opening, reconciliation, statement
from test_engine import close, publish, save
from test_engine import engine as _engine
from test_opening_continuation import book as _opening_book
from test_opening_continuation import complete_members
from test_payroll import bonus, bonus_sources
from test_payroll_corrections import Company, payment
from test_payroll_corrections import company as _payroll_company
from test_payroll_tax_declarations import declare
from test_payroll_withholding_actual import actual, august_case
from test_reimbursement_assets import accepted_batch, activation, batch_card
from test_reports import book as _report_book
from test_reports import cit
from test_reports import profile as report_profile

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.domains.assets import AssetAcquisition, AssetActivation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.http import wire_money
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store

bank_book, opening_book, report_book, engine = _bank_book, _opening_book, _report_book, _engine
payroll_company = _payroll_company


def test_summary_and_detail_pages_do_not_truncate_financial_totals(bank_book):
    book, _, _, proof = bank_book
    facts = [
        {
            "kind": "funding",
            "subject_id": f"receipt-{i:04}",
            "data": {
                "period": "2026-09",
                "owner_id": "owner",
                "amount_fen": 1,
                "funding_kind": "capital",
                "actual_date": "2026-09-02",
                "bank_account_id": "bank",
            },
            "evidence": [proof],
            "expected_revision": 0,
        }
        for i in range(501)
    ]
    book.save_facts(facts, request_id="501-sources")
    subjects = [item["subject_id"] for item in facts]
    preview = book.preview(subjects)
    book.confirm(
        subjects,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="501-postings",
    )
    dashboard = Dashboard(book, company_name="测试企业")
    data = dashboard.brief("2026-09", limit=500)["data"]
    assert data["voucher_count"] == 501
    assert len(data["vouchers"]) == 500
    assert data["total_debit_fen"] == data["position"]["bank_fen"] == 501
    assert data["voucher_page"] == {"has_more": True, "next_after_number": 500, "total_count": 501}
    following = dashboard.brief("2026-09", after_number=500, limit=500)["data"]
    assert len(following["vouchers"]) == 1
    assert following["total_debit_fen"] == 501
    assert sum(group["event_count"] for group in following["activity_groups"]) == 501
    assert sum(len(group["rows"]) for group in following["activity_groups"]) == 1
    funds = dashboard.funds("2026-09", limit=500)["data"]
    assert funds["total_fen"] == funds["inflow_fen"] == 501
    assert len(funds["movements"]) == 500
    following = dashboard.funds(
        "2026-09", after_movement=funds["movement_page"]["next_cursor"], limit=500
    )["data"]
    assert len(following["movements"]) == 1
    assert following["inflow_fen"] == 501


def test_bank_matching_counts_original_rows_not_payment_groups(bank_book):
    book, store, commit, _ = bank_book
    opening(store, commit)
    funding(store, commit)
    statement(store, commit, [entry("first", amount=400), entry("second", amount=600)])
    reconciliation(
        store,
        commit,
        [
            {"reference": "first", "source_kind": "funding", "source_id": "funding"},
            {"reference": "second", "source_kind": "funding", "source_id": "funding"},
        ],
    )
    dashboard = Dashboard(book)
    data = dashboard.funds("2026-09", limit=1)["data"]
    assert data["bank_statement"]["matched_count"] == 2
    assert data["bank_statement"]["transaction_count"] == 2
    assert len(data["bank_statement"]["rows"]) == 1
    assert data["accounts"][0]["reconciliation"]["state"] == "complete"
    assert data["accounts"][0]["active"] is None
    second = dashboard.funds(
        "2026-09", after_statement=data["bank_statement"]["page"]["next_cursor"], limit=1
    )["data"]
    assert second["bank_statement"]["rows"][0]["signed_amount_fen"] == 600
    assert second["bank_statement"]["inflow_fen"] == 1000


def test_unpublished_or_missing_inventory_is_not_complete(bank_book):
    book, store, commit, _ = bank_book
    store(
        "expense",
        "charge",
        {
            "period": "2026-09",
            "counterparty_id": "supplier",
            "amount_fen": 123,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
    )
    dashboard = Dashboard(book)
    data = dashboard.brief("2026-09")["data"]
    assert data["voucher_count"] == 0
    assert not data["material_completeness"]["satisfied"]
    assert any(issue["field"] == "charge" for issue in data["material_completeness"]["issues"])
    commit("charge")
    data = dashboard.brief("2026-09")["data"]
    assert not data["material_completeness"]["satisfied"]
    assert any(
        issue["field"].startswith("materials.") for issue in data["material_completeness"]["issues"]
    )
    assert data["vouchers"][0]["date"] is None
    assert data["vouchers"][0]["recognition"]["precision"] == "month"


def test_closed_history_preserves_old_version_and_open_correction_delta(engine):
    save(engine, amount=100)
    publish(engine)
    closed = close(engine)
    before = Dashboard(engine).brief("2026-01")["data"]
    save(engine, amount=125, revision=1, request="correct")
    publish(engine, request="correct-post", correction_period="2026-02")
    dashboard = Dashboard(engine)
    january = dashboard.brief("2026-01")["data"]
    february = dashboard.brief("2026-02")["data"]
    assert january["total_debit_fen"] == before["total_debit_fen"] == 100
    assert january["vouchers"][0]["components"][0]["facts"]["amount"] == 100
    assert february["position"]["month_expense_fen"] == 25
    assert february["position"]["liabilities_fen"] == 125
    assert sorted(v["components"][0]["facts"]["amount"] for v in february["vouchers"]) == [100, 125]
    with engine.store.connection(read_only=True) as connection:
        assert (
            json.loads(connection.execute("SELECT manifest FROM period_close").fetchone()[0])
            == closed
        )


def test_reviewed_no_impact_source_keeps_number_and_displays_new_evidence(engine):
    save(engine)
    _, original = publish(engine)
    proof = engine.register_evidence(
        b"clarified original", "text/plain", "clarified", request_id="second-evidence"
    )["digest"]
    engine.save_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100},
        evidence=(proof,),
        expected_revision=1,
        request_id="reviewed-fact",
    )
    _, reviewed = publish(engine, request="reviewed-publication")
    data = Dashboard(engine).brief("2026-01")["data"]
    assert data["vouchers"][0]["number"] == str(original["results"][0]["voucher_number"])
    assert data["vouchers"][0]["calculation_id"] == reviewed["results"][0]["calculation_id"]
    assert data["vouchers"][0]["evidence"] == [proof]


def test_actual_payroll_tax_and_unknown_management_stay_distinct(tmp_path):
    company = Company(tmp_path / "payroll.sqlite")
    wage, sources = august_case()
    for source in sources:
        company.save(source.fact, source.subject_id)
    company.save(actual(), "observed-tax")
    company.save(wage.fact, wage.subject_id)
    company.publish(wage.subject_id)
    Display(company.engine).save_display_profile(
        {
            "kind": "employee",
            "entity_id": "employee",
            "display_name": "测试人员",
            "employment_start": "2026-03",
            "source": "合成人员资料",
        },
        expected_revision=0,
        request_id="person-profile",
    )
    data = Dashboard(company.engine).employees("2026-08")["data"]
    employee = data["employees"]["items"][0]
    assert employee["name"] == "测试人员"
    assert employee["gross_salary_fen"] == 4000000
    assert employee["individual_income_tax_fen"] == 90000
    assert employee["net_salary_fen"] == 3910000
    assert employee["tax_withholding_start_date"] == "2026-03"
    assert employee["tax_details"][0]["calculated_tax_fen"] == 30000
    assert employee["tax_details"][0]["actual_withholding_tax_fen"] == 90000
    assert employee["recorded_net_payments_fen"] == 0
    assert employee["declared_tax_fen"] is None
    assert employee["in_period"] is None
    assert data["employees"]["unknown_period_count"] == 1
    assert data["employees"]["in_period_count"] is None
    assert data["employees"]["detail_reconciled"]
    assert wire_money(data)["employees"]["items"][0]["individual_income_tax_fen"] == "90000"


def test_bonus_remains_separate_from_regular_wages(tmp_path):
    company = Company(tmp_path / "bonus.sqlite")
    for source in bonus_sources():
        company.save(source.fact, source.subject_id)
    company.save(bonus(), "bonus")
    company.publish("bonus")
    employee = Dashboard(company.engine).employees("2026-01")["data"]["employees"]["items"][0]
    assert employee["annual_bonus_fen"] == 3000000
    assert employee["gross_salary_fen"] == 0
    assert employee["individual_income_tax_fen"] == 90000
    assert employee["company_cost_fen"] == 3000000
    assert employee["has_annual_bonus"]


def test_cross_month_payments_follow_source_employee_and_keep_month_end_outstanding(
    payroll_company,
):
    company = payroll_company
    company.publish("january", "february")
    dashboard = Dashboard(company.engine)
    original = dashboard.brief("2026-01")["data"]["open_items"]
    company.save(payment(), "salary-payment")
    company.publish("salary-payment")
    february = dashboard.employees("2026-02")["data"]["employees"]["items"][0]
    assert february["recorded_net_payments_fen"] == 907400
    assert dashboard.funds("2026-02")["data"]["outflow_fen"] == 907400
    january = dashboard.brief("2026-01")["data"]["open_items"]
    assert january["payable_fen"] == original["payable_fen"]
    assert january["current_outstanding"]["payable_fen"] == original["payable_fen"] - 907400


def test_closed_actual_declaration_uses_frozen_management_fact(payroll_company):
    company = payroll_company
    company.publish("january", "february")
    declaration, _ = declare(company, extra=0)
    dashboard = Dashboard(company.engine)
    assert (
        dashboard.employees("2026-01")["data"]["employees"]["items"][0]["declared_tax_fen"] == 12600
    )
    company.close("2026-01")
    proof = company.engine.register_evidence(
        b"Synthetic correction: declared withholding 12700 fen",
        "text/plain",
        "corrected declaration",
        request_id=company.request(),
    )["digest"]
    company.engine.amend_fact(
        declaration.kind,
        "declared",
        declaration.model_dump(mode="json") | {"declared_tax_fen": 12700},
        evidence=(proof,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id=company.request(),
    )
    assert (
        dashboard.employees("2026-01")["data"]["employees"]["items"][0]["declared_tax_fen"] == 12600
    )


def test_closed_asset_cost_correction_is_adjustment_not_new_acquisition(tmp_path):
    company = Company(tmp_path / "corrected-asset.sqlite")
    asset = AssetAcquisition(
        period="2026-01",
        asset_type="fixed",
        supplier_id="supplier",
        acquisition_date="2026-01-03",
        cost_fen=120000,
        acquisition_basis="direct_purchase",
    )
    company.save(asset, "computer")
    company.save(
        AssetActivation(
            period="2026-01",
            asset_id="computer",
            in_use_date="2026-01-05",
            useful_life_months=12,
            residual_fen=0,
            benefit_area="administration",
            rounding_policy="floor_final_remainder",
        ),
        "computer-use",
    )
    company.publish("computer", "computer-use")
    company.close("2026-01")
    company.save(asset.model_copy(update={"cost_fen": 132000}), "computer", revision=1)
    company.publish("computer", correction_period="2026-02")
    dashboard = Dashboard(company.engine)
    january = dashboard.assets("2026-01")["data"]
    february = dashboard.assets("2026-02")["data"]
    assert january["ledger_cost_fen"] == january["month_acquired_fen"] == 120000
    assert february["ledger_cost_fen"] == february["card_cost_fen"] == 132000
    assert february["reconciled"]
    assert february["month_acquired_count"] == february["month_acquired_fen"] == 0
    assert february["month_activated_count"] == 0
    assert february["month_cost_adjustment_fen"] == 12000
    assert february["fixed"]["month_cost_adjustment_fen"] == 12000


def test_batch_asset_cards_depreciation_and_disposal_use_single_cost(bank_book):
    book, store, commit, _ = bank_book
    store("reimbursed_asset_batch", "batch", accepted_batch())
    store("reimbursed_asset", "computer", batch_card())
    store("reimbursed_asset", "chair", batch_card(30000))
    store("asset_activation", "computer-use", activation())
    store("asset_activation", "chair-use", activation(asset_id="chair"))
    commit("batch", "computer", "chair", "computer-use", "chair-use")
    february = Dashboard(book).assets("2026-02")["data"]
    assert february["registered_count"] == 2
    assert february["card_cost_fen"] == february["ledger_cost_fen"] == 150000
    assert february["reconciled"]
    assert all(item["acquisition_date"] is None for item in february["fixed"]["items"])
    assert all(item["acquisition_reference"] == "1" for item in february["fixed"]["items"])
    for ident in ("computer", "chair"):
        store("asset_consumption", ident + "-march", {"period": "2026-03", "asset_id": ident})
    commit("computer-march", "chair-march")
    march = Dashboard(book).assets("2026-03")["data"]
    assert march["month_charge_fen"] == 12500
    assert march["card_net_fen"] == march["ledger_net_fen"] == 137500
    store(
        "asset_disposal",
        "computer-scrap",
        {
            "period": "2026-03",
            "asset_id": "computer",
            "disposal_date": "2026-03-31",
            "disposal_kind": "scrap",
            "gross_proceeds_fen": 0,
        },
    )
    commit("computer-scrap")
    disposed = Dashboard(book).assets("2026-03")["data"]
    assert disposed["reconciled"]
    assert disposed["active_count"] == 1
    assert disposed["ledger_net_fen"] == 27500
    computer = next(item for item in disposed["fixed"]["items"] if item["asset_id"] == "computer")
    assert computer["disposal"]["loss_fen"] == 110000
    assert computer["book_value_fen"] == 0


def test_opening_cards_and_bank_balances_are_not_current_movements(opening_book):
    book, store, _, package, _ = opening_book
    package(complete_members(store))
    dashboard = Dashboard(book)
    funds = dashboard.funds("2026-01")["data"]
    assets = dashboard.assets("2026-01")["data"]
    assert funds["opening_fen"] == funds["total_fen"] == 1010000
    assert funds["movement_count"] == 0
    assert funds["inflow_fen"] == 0
    assert assets["reconciled"]
    assert assets["ledger_net_fen"] == 100000
    assert assets["month_acquired_count"] == 0
    assert assets["fixed"]["items"][0]["acquisition_date"] is None


def test_opening_net_wage_payment_does_not_require_current_payroll(opening_book):
    book, store, commit, package, _ = opening_book
    package(complete_members(store))
    store(
        "payment",
        "old-wage-payment",
        {
            "period": "2026-01",
            "actual_date": "2026-01-20",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "employee",
            "amount_fen": 50000,
            "allocations": [
                {
                    "source_kind": "opening_payroll_payable",
                    "source_id": "wage-payable",
                    "obligation": "primary",
                    "amount_fen": 50000,
                }
            ],
        },
    )
    commit("old-wage-payment")
    data = Dashboard(book).employees("2026-01")["data"]["employees"]
    assert data["items"][0]["recorded_net_payments_fen"] == 50000
    assert not data["items"][0]["has_payroll_activity"]
    assert data["items"][0]["gross_salary_fen"] == 0


def test_quarterly_closed_display_and_export_share_frozen_plan(report_book):
    book, store, commit, close_period = report_book
    report_profile(store, commit)
    cit(store, commit)
    for period in ("2026-01", "2026-02", "2026-03"):
        close_period(period)
    expected = Reports(book).report(2026, 1, source="closed")
    data = Dashboard(book).quarterly_report(2026, 1)
    assert expected["status"] == "ready"
    assert data["export"]["available"]
    assert (
        data["export"]["preview_digest"]
        == data["technical"]["calculation_hash"]
        == expected["digest"]
    )
    assert data["export"]["epochs"] == expected["epochs"]
    assert (
        data["summary"]["assets_total_fen"]
        == expected["statements"]["balance_sheet"]["30"]["ending_fen"]
    )


def test_empty_catalog_company_and_money_precision(bank_book, tmp_path):
    book, store, commit, _ = bank_book
    empty = Engine(
        Store.create(
            tmp_path / "empty.sqlite", default_registry(), "empty", "911100000000000002", "empty-db"
        )
    )
    assert Dashboard(empty).context()["periods"] == []
    assert Dashboard(empty).brief()["data"] is None
    dashboard = Dashboard(book, company_name="合成公司")
    huge = 2**53 + 123
    funding(store, commit, amount=huge)
    response = wire_money(dashboard.funds("2026-09"))
    assert response["data"]["total_fen"] == str(huge)
    assert dashboard.context()["current_company"]["company_id"] == book.store.company_id
    with pytest.raises(ValueError):
        dashboard.funds("2026-09", limit=501)
