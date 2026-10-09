"""Old dashboard contracts over synthetic typed SQLite business, without the retired ORM."""

from typing import ClassVar
from unittest.mock import patch

import pytest
from entity_fixture import save_entity_display_profile, seed_registration_entities
from schema_fixture import test_bundle
from test_banking import book as _bank_book
from test_banking import entry, funding, opening, reconciliation, statement
from test_engine import close, publish, save
from test_engine import engine as _engine
from test_opening_continuation import book as _opening_book
from test_opening_continuation import complete_members
from test_payroll import bonus, bonus_sources, payroll
from test_payroll_corrections import Company, payment
from test_payroll_corrections import company as _payroll_company
from test_payroll_tax_declarations import declare
from test_payroll_withholding_actual import actual, august_case
from test_reimbursement_assets import accepted_batch, activation, batch_card
from test_reports import book as _report_book
from test_reports import cit
from test_reports import profile as report_profile

from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import Fact, KernelError, Line, Outcome, Registry
from ai_accounting.kernel.dashboard import Dashboard, _funds, _open_items, _position
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.domains.assets import AssetAcquisition, AssetActivation
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.http import wire_money
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import PositiveFen, YearMonth

bank_book, opening_book, report_book, engine = _bank_book, _opening_book, _report_book, _engine


def diagnostic_vouchers(engine, period):
    """Exercise retained diagnostic presentation separately from the owner response."""
    with Dashboard(engine)._snapshot(period) as snapshot:
        items = [snapshot.voucher(row) for row in snapshot.month_journal]
        snapshot.attach_recorded_times()
        return items


def diagnostic_funds(engine, period, **options):
    with Dashboard(engine)._snapshot(period) as snapshot:
        data = _funds(snapshot, **options)
        data["bank_statement"] = FundsRead(snapshot).bank_summary()
        snapshot.attach_recorded_times()
        return data


def diagnostic_position(engine, period):
    with Dashboard(engine)._snapshot(period) as snapshot:
        return _position(snapshot)


def diagnostic_open_items(engine, period):
    with Dashboard(engine)._snapshot(period) as snapshot:
        data = _open_items(snapshot)
        snapshot.attach_recorded_times()
        return data


def explicit_checks(dashboard, period):
    context = dashboard.brief(period)["read_context"]
    return dashboard.period_preparation(
        period, expected_read_version=context["read_version"], as_of=context["as_of"]
    )["data"]["brief_checks"]


def _assert_asset_member_summary_parity(engine, period):
    dashboard = Dashboard(engine)
    with patch("ai_accounting.kernel.period_balances.balance_totals", return_value=[]):
        complete = dashboard.assets(period, preparation="deferred")["data"]
    bounded = dashboard.assets(period, preparation="deferred")["data"]
    assert bounded == complete


def _assert_brief_asset_summary_parity(engine, period):
    dashboard = Dashboard(engine)
    complete = dashboard.assets(period, preparation="deferred")["data"]
    brief = dashboard.brief(period, preparation="deferred")["data"]
    assert brief["long_term_assets"] == {
        "net_fen": complete["ledger_net_fen"],
        "fixed_active_count": complete["fixed"]["active_count"],
        "intangible_active_count": complete["intangible"]["active_count"],
    }
    position = brief["financial_position"]
    for field in ("fixed_asset_net_fen", "intangible_asset_net_fen"):
        assert position[field] == complete[field]
    assert position["fixed_asset_net_fen"] == (
        position["fixed_asset_cost_fen"] - position["accumulated_depreciation_fen"]
    )
    assert position["intangible_asset_net_fen"] == (
        position["intangible_asset_cost_fen"] - position["accumulated_amortization_fen"]
    )
    assert complete["ledger_net_fen"] == (
        complete["fixed_asset_net_fen"] + complete["intangible_asset_net_fen"]
    )


payroll_company = _payroll_company


def test_summary_and_detail_pages_do_not_truncate_financial_totals(bank_book):
    book, _, _, proof = bank_book
    facts = [
        {
            "kind": "funding",
            "subject_id": f"receipt-{i:04}",
            "data": {
                "period": "2026-09",
                "owner_id": f"owner-{i:04}",
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
    for item in facts:
        seed_registration_entities(book, item["kind"], item["data"])
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
    assert data["activity_count"] == 501
    assert len(data["collections"]["activity"]["items"]) == 500
    assert data["funds_overview"]["bank_fen"] == 501
    page = data["collections"]["activity"]["page"]
    assert page["has_more"] and page["total_count"] == page["filtered_count"] == 501
    assert page["returned_count"] == 500
    following = dashboard.brief(
        "2026-09",
        section="activity",
        cursor=page["next_cursor"],
        limit=500,
        expected_version=dashboard.brief("2026-09")["snapshot_version"],
    )["data"]
    assert len(following["collections"]["activity"]["items"]) == 1
    assert following["funds_overview"]["bank_fen"] == 501
    assert sum(group["event_count"] for group in following["activity_groups"]) == 501
    default = dashboard.brief("2026-09")["data"]
    assert len(default["collections"]["activity"]["items"]) == 20
    assert default["activity_count"] == default["funds_overview"]["bank_fen"] == 501
    with dashboard._snapshot("2026-09") as snapshot:
        assert snapshot.month_journal.totals()["debit"] == 501
    funds = dashboard.funds("2026-09", limit=500)["data"]
    assert funds["total_fen"] == funds["inflow_fen"] == 501
    assert len(funds["collections"]["movements"]["items"]) == 500
    following = dashboard.funds(
        "2026-09",
        section="movements",
        cursor=funds["collections"]["movements"]["page"]["next_cursor"],
        limit=500,
        expected_version=dashboard.funds("2026-09")["snapshot_version"],
    )["data"]
    assert len(following["collections"]["movements"]["items"]) == 1
    assert following["inflow_fen"] == 501


def test_owner_default_activity_page_keeps_complete_monthly_expense(bank_book):
    book, _, _, proof = bank_book
    data = {
        "period": "2026-09",
        "counterparty_id": "expense-supplier",
        "amount_fen": 1,
        "expense_class": "administration",
        "creditor_kind": "supplier",
    }
    seed_registration_entities(book, "expense", data)
    facts = [
        {
            "kind": "expense",
            "subject_id": f"expense-{index:03}",
            "data": data,
            "evidence": [proof],
            "expected_revision": 0,
        }
        for index in range(21)
    ]
    book.save_facts(facts, request_id="owner-expenses")
    subjects = [item["subject_id"] for item in facts]
    preview = book.preview(subjects)
    book.confirm(
        subjects,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="owner-expenses-published",
    )
    dashboard = Dashboard(book)
    response = dashboard.brief("2026-09")
    result = response["data"]
    groups = result["collections"]["activity"]["items"]
    assert len(groups) == result["group_count"] == 1
    assert groups[0]["member_count"] == groups[0]["amount_fen"] == 21
    assert result["activity_count"] == 21
    members = dashboard.brief_group(
        "2026-09",
        section="activity",
        group_key=groups[0]["group_key"],
        expected_version=response["snapshot_version"],
    )["data"]["collections"]["members"]
    assert len(members["items"]) == 20
    assert members["page"]["total_count"] == 21 and members["page"]["has_more"]
    following = dashboard.brief_group(
        "2026-09",
        section="activity",
        group_key=groups[0]["group_key"],
        expected_version=response["snapshot_version"],
        cursor=members["page"]["next_cursor"],
    )["data"]["collections"]["members"]
    assert len(following["items"]) == 1 and not following["page"]["has_more"]
    assert result["position"]["month_revenue_fen"] == 0
    assert result["position"]["month_expense_fen"] == 21
    assert result["position"]["month_result_fen"] == -21
    assert result["open_items"]["payable_fen"] == 21
    assert sum(group["event_count"] for group in result["activity_groups"]) == 21


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
    save_entity_display_profile(
        book,
        {
            "kind": "counterparty",
            "entity_id": "owner",
            "display_name": "明确出资人",
            "source": "合成展示资料",
        },
        expected_revision=0,
        request_id="bank-row-owner",
    )
    dashboard = Dashboard(book)
    data = dashboard.funds("2026-09", limit=1)["data"]
    assert diagnostic_funds(book, "2026-09", limit=1)["bank_statement"]["matched_count"] == 2
    assert data["bank_statement"]["transaction_count"] == 2
    statement_rows = data["collections"]["statements"]
    assert len(statement_rows["items"]) == 1
    assert statement_rows["items"][0]["party"] == "明确出资人"
    assert data["collections"]["accounts"]["items"][0]["reconciliation"]["state"] == "complete"
    assert data["collections"]["accounts"]["items"][0]["active"] is True
    second = dashboard.funds(
        "2026-09",
        section="statements",
        cursor=statement_rows["page"]["next_cursor"],
        limit=1,
        expected_version=dashboard.funds("2026-09")["snapshot_version"],
    )["data"]
    second_rows = second["collections"]["statements"]["items"]
    assert second_rows[0]["signed_amount_fen"] == 600
    assert second_rows[0]["party"] == "明确出资人"
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
    assert data["activity_count"] == 0
    checks = explicit_checks(dashboard, "2026-09")
    assert not checks["material_completeness"]["satisfied"]
    assert any(issue["field"] == "charge" for issue in checks["issues"])
    commit("charge")
    data = dashboard.brief("2026-09")["data"]
    checks = explicit_checks(dashboard, "2026-09")
    assert not checks["material_completeness"]["satisfied"]
    assert any(
        issue["field"].startswith("materials.")
        for issue in checks["material_completeness"]["issues"]
    )
    group = data["collections"]["activity"]["items"][0]
    assert group["date_from"] is group["date_to"] is None
    assert group["has_month_recognition"]
    member = dashboard.brief_group(
        "2026-09",
        section="activity",
        group_key=group["group_key"],
    )["data"]["collections"]["members"]["items"][0]
    assert member["date"] is None
    assert member["recognition"]["precision"] == "month"


def test_closed_history_preserves_old_version_and_open_correction_delta(engine):
    save(engine, amount=100)
    publish(engine)
    closed = close(engine)
    before = Dashboard(engine).brief("2026-01")["data"]
    save(engine, amount=125, revision=1, request="correct")
    publish(engine, request="correct-post", posting_period="2026-02")
    dashboard = Dashboard(engine)
    january = dashboard.brief("2026-01")["data"]
    february = dashboard.brief("2026-02")["data"]
    assert (
        january["position"]["month_expense_fen"] == before["position"]["month_expense_fen"] == 100
    )
    assert (
        sum(line["debit_fen"] for line in diagnostic_vouchers(engine, "2026-01")[0]["lines"]) == 100
    )
    assert february["position"]["month_expense_fen"] == 25
    # This synthetic calculator supplies no creditor identity for its 2202 line.
    position = diagnostic_position(engine, "2026-02")
    assert position["liabilities_fen"] is None
    assert position["equation_valid"] is None
    assert position["issues"]
    assert not position["complete"]
    # Party classification does not change the posted operating result.
    assert february["position"]["complete"]
    assert not any(risk["key"] == "amounts" for risk in february["risks"])
    assert sorted(
        sum(line["debit_fen"] for line in v["lines"])
        for v in diagnostic_vouchers(engine, "2026-02")
    ) == [100, 125]
    with engine.store.connection(read_only=True) as connection:
        from ai_accounting.kernel.close_storage import decode_close

        assert (
            decode_close(connection, connection.execute("SELECT * FROM period_close").fetchone())
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
    dashboard = Dashboard(engine)
    data = dashboard.brief("2026-01")["data"]
    diagnostic = diagnostic_vouchers(engine, "2026-01")[0]
    assert diagnostic["number"] == str(original["results"][0]["voucher_number"])
    assert diagnostic["calculation_id"] == reviewed["results"][0]["calculation_id"]
    assert diagnostic["evidence"] == [proof]
    group = data["collections"]["activity"]["items"][0]
    member = dashboard.brief_group(
        "2026-01",
        section="activity",
        group_key=group["group_key"],
    )["data"]["collections"]["members"]["items"][0]
    assert member["voucher_version_id"] == diagnostic["voucher_version_id"]


def test_actual_payroll_tax_and_unknown_management_stay_distinct(tmp_path):
    company = Company(tmp_path / "payroll.sqlite")
    wage, sources = august_case()
    for source in sources:
        company.save(source.fact, source.subject_id)
    company.save(actual(), "observed-tax")
    company.save(wage.fact, wage.subject_id)
    company.confirm_payroll(wage.subject_id)
    company.publish(wage.subject_id)
    save_entity_display_profile(
        company.engine,
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
    data = Dashboard(company.engine).employees("2026-08", employee_filter="all")["data"]
    employee = data["collections"]["employees"]["items"][0]
    assert employee["name"] == "测试人员"
    assert employee["gross_salary_fen"] == 4000000
    assert employee["individual_income_tax_fen"] == 90000
    assert employee["net_salary_fen"] == 3910000
    values = company.current(wage.subject_id).values
    assert values["tax_input"]["withholding_start_date"] == "2026-03"
    assert values["calculated_tax_fen"] == 30000
    assert values["tax_fen"] == 90000
    assert employee["recorded_net_payments_fen"] == 0
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM fact_payroll_tax_declaration_actual"
            ).fetchone()[0]
            == 0
        )
    assert employee["in_period"] is None
    assert data["employees"]["unknown_period_count"] == 1
    assert data["employees"]["in_period_count"] == 0
    assert data["workforce_cost"]["total_fen"] == employee["company_cost_fen"]
    assert (
        wire_money(data)["collections"]["employees"]["items"][0]["individual_income_tax_fen"]
        == "90000"
    )


def test_payroll_open_items_show_the_employee_for_every_payroll_component(payroll_company):
    company = payroll_company
    company.publish("january")
    profile = save_entity_display_profile(
        company.engine,
        {
            "kind": "employee",
            "entity_id": "employee",
            "display_name": "测试员工",
            "source": "合成人员资料",
        },
        expected_revision=0,
        request_id="open-items-employee-profile",
    )

    payroll_items = Dashboard(company.engine).brief("2026-01", section="open_items")["data"][
        "collections"
    ]["open_items"]["items"]
    diagnostics = diagnostic_open_items(company.engine, "2026-01")["collection"]["items"]
    by_component = {item["name"]: item for item in diagnostics}

    assert set(by_component) == {"net", "tax", "employee_social", "employer_social"}
    assert {item["party"] for item in payroll_items} == {"测试员工"}
    assert all("source_period" not in item for item in payroll_items)
    assert {item["party_key"] for item in diagnostics} == {"employee"}
    assert {name: item["description"] for name, item in by_component.items()} == {
        "net": "实发工资",
        "tax": "代扣个人所得税",
        "employee_social": "个人社保",
        "employer_social": "单位社保",
    }
    assert by_component["employee_social"]["creditor_id"] is None
    assert by_component["employee_social"]["field_sources"]["party"]["id"] == profile["id"]
    social = next(item for item in payroll_items if item["description"] == "社保")
    assert social["member_count"] == 2
    contributions = Dashboard(company.engine).brief_group(
        "2026-01",
        section="open_items",
        group_key=social["group_key"],
    )["data"]["collections"]["members"]["items"]
    assert {item["contribution_component"] for item in contributions} == {
        "employee_social",
        "employer_social",
    }
    assert {item["payroll_period"] for item in contributions} == {"2026-01"}
    assert len({item["contribution_group_key"] for item in contributions}) == 1
    assert all(item["contribution_group_key"] for item in contributions)
    assert sum(item["outstanding_fen"] for item in contributions) == social["outstanding_fen"]


def test_bonus_remains_separate_from_regular_wages(tmp_path):
    company = Company(tmp_path / "bonus.sqlite")
    for source in bonus_sources():
        company.save(source.fact, source.subject_id)
    company.save(bonus(), "bonus")
    company.publish("bonus")
    employee = Dashboard(company.engine).employees("2026-01", employee_filter="all")[
        "data"
    ]["collections"]["employees"][
        "items"
    ][0]
    assert employee["annual_bonus_fen"] == 3000000
    assert employee["gross_salary_fen"] == 0
    assert employee["individual_income_tax_fen"] == 90000
    assert employee["company_cost_fen"] == 3000000
    assert employee["has_annual_bonus"]


def test_opening_contributions_keep_four_obligations_and_form_two_prior_month_matters(opening_book):
    engine, save, _, package, _ = opening_book
    members = complete_members(save)
    wage = next(fields for kind, _, fields in members if kind == "opening_payroll_payable")
    members = [member for member in members if member[0] != "opening_payroll_payable"]
    components = ("employee_social", "employer_social", "employee_housing", "employer_housing")
    members.extend(
        (
            "opening_payroll_payable",
            component,
            {**wage, "component": component, "outstanding_fen": 12_500},
        )
        for component in components
    )
    package(members)
    response = Dashboard(engine).brief("2026-01", section="open_items", limit=2)
    data = response["data"]
    collection = data["collections"]["open_items"]
    items = list(collection["items"])
    while collection["page"]["has_more"]:
        collection = Dashboard(engine).brief(
            "2026-01",
            section="open_items",
            limit=2,
            cursor=collection["page"]["next_cursor"],
            expected_version=response["snapshot_version"],
        )["data"]["collections"]["open_items"]
        items.extend(collection["items"])
    contribution_groups = [item for item in items if item["description"] in {"社保", "公积金"}]
    assert {item["description"] for item in contribution_groups} == {"社保", "公积金"}
    assert all(item["member_count"] == 2 for item in contribution_groups)
    contributions = []
    for group in contribution_groups:
        member_collection = Dashboard(engine).brief_group(
            "2026-01",
            section="open_items",
            group_key=group["group_key"],
            limit=1,
            expected_version=response["snapshot_version"],
        )["data"]["collections"]["members"]
        members = list(member_collection["items"])
        assert len(members) == 1 and member_collection["page"]["has_more"]
        following = Dashboard(engine).brief_group(
            "2026-01",
            section="open_items",
            group_key=group["group_key"],
            limit=1,
            cursor=member_collection["page"]["next_cursor"],
            expected_version=response["snapshot_version"],
        )["data"]["collections"]["members"]
        members.extend(following["items"])
        assert not following["page"]["has_more"]
        family = "social" if group["description"] == "社保" else "housing"
        assert all(item["contribution_component"].endswith(family) for item in members)
        assert sum(item["outstanding_fen"] for item in members) == group["outstanding_fen"]
        contributions.extend(members)
    assert len(contributions) == 4
    assert {item["contribution_component"] for item in contributions} == set(components)
    assert {item["payroll_period"] for item in contributions} == {"2025-12"}
    assert len({item["contribution_group_key"] for item in contributions}) == 1
    assert all(item["contribution_group_key"] for item in contributions)
    assert sum(item["outstanding_fen"] for item in contributions) == 50_000


def test_cross_month_payments_follow_source_employee_and_keep_month_end_outstanding(
    payroll_company,
):
    company = payroll_company
    company.publish("january", "february")
    dashboard = Dashboard(company.engine)
    original = dashboard.brief("2026-01")["data"]["open_items"]
    original_items = dashboard.brief("2026-01", section="open_items")["data"]["collections"][
        "open_items"
    ]["items"]
    original_net = next(item for item in original_items if item["description"] == "实发工资")
    assert all("source_period" not in item for item in original_items)
    assert original_net["status"] == "open"
    assert original_net["current_status"] == "open"
    assert original_net["current_outstanding_fen"] == original_net["outstanding_fen"]
    company.save(payment(), "salary-payment")
    company.publish("salary-payment")
    february = dashboard.employees("2026-02", employee_filter="all")["data"][
        "collections"
    ]["employees"]["items"][0]
    assert february["recorded_net_payments_fen"] == 907400
    assert dashboard.funds("2026-02")["data"]["outflow_fen"] == 907400
    january = dashboard.brief("2026-01")["data"]["open_items"]
    assert january["payable_fen"] == original["payable_fen"]
    assert (
        diagnostic_open_items(company.engine, "2026-01")["current_outstanding"]["payable_fen"]
        == original["payable_fen"] - 907400
    )
    january_items = dashboard.brief("2026-01", section="open_items")["data"]["collections"][
        "open_items"
    ]["items"]
    january_net = next(item for item in january_items if item["description"] == "实发工资")
    assert january_net["status"] == "open"
    assert january_net["current_status"] == "settled"
    assert january_net["current_outstanding_fen"] == 0


def test_closed_actual_declaration_shows_existing_correction_without_reposting(payroll_company):
    company = payroll_company
    company.publish("january", "february")
    declaration, _ = declare(company, extra=0)
    Dashboard(company.engine)
    assert (
        BusinessQueries(company.engine).business_status("declared", "2026-01")["latest_fact"][
            "data"
        ]["declared_tax_fen"]
        == 12600
    )
    company.close("2026-01")
    original_ledger = company.engine.ledger("2026-01")
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
        BusinessQueries(company.engine).business_status("declared", "2026-01")["latest_fact"][
            "data"
        ]["declared_tax_fen"]
        == 12700
    )
    assert company.engine.ledger("2026-01") == original_ledger


def test_business_status_keeps_distinct_current_and_frozen_amounts(payroll_company):
    company = payroll_company
    company.publish("january", "february")
    company.close("2026-01")
    dashboard = Dashboard(company.engine)
    frozen_items = dashboard.brief("2026-01", section="open_items")["data"]["collections"][
        "open_items"
    ]["items"]
    company.save(
        payroll(accounting_gross_salary_fen=1_500_000, tax_reported_salary_fen=1_500_000),
        "january",
        revision=1,
    )
    company.confirm_payroll("january")
    company.publish("january", posting_period="2026-02")

    data = Dashboard(company.engine).business_status("2026-01", "january", as_of="2026-02-28")[
        "data"
    ]

    assert data["current_business_result"]["amount_fen"] == 1_500_000
    assert data["current_business_result"]["amount_label"] == "税前工资"
    assert data["frozen_adoption"]["amount_fen"] == 1_000_000
    assert data["frozen_adoption"]["amount_label"] == "税前工资"
    corrected_items = dashboard.brief("2026-01", section="open_items")["data"]["collections"][
        "open_items"
    ]["items"]
    fields = (
        "group_key",
        "outstanding_fen",
        "source_amount_fen",
        "member_count",
        "party",
        "description",
    )
    assert [tuple(item[field] for field in fields) for item in corrected_items] == [
        tuple(item[field] for field in fields) for item in frozen_items
    ]


def test_closed_asset_cost_correction_is_adjustment_not_new_acquisition(tmp_path):
    company = Company(tmp_path / "corrected-asset.sqlite")
    asset = AssetAcquisition(
        asset_id="computer",
        period="2026-01",
        asset_type="fixed",
        supplier_id="supplier",
        acquisition_date="2026-01-03",
        cost_fen=120000,
        acquisition_basis="direct_purchase",
    )
    saved = company.save(asset, "computer")
    company.publish("computer")
    activation_fact = AssetActivation(
        period="2026-01",
        asset_id="computer",
        in_use_date="2026-01-05",
        useful_life_months=12,
        residual_fen=0,
        benefit_area="administration",
        rounding_policy="floor_final_remainder",
    )
    with company.engine.store.connection(read_only=True) as connection:
        evidence = company.engine.store.fact(connection, saved["fact_id"]).evidence
    batches = AssetBatches(company.engine)
    members = [
        {
            "subject_id": "computer-use",
            "expected_revision": 0,
            "data": activation_fact.model_dump(mode="json"),
        }
    ]
    options = {"evidence": evidence, "expected_revision": 0}
    preview = batches.prepare_activation_batch("activation-batch", "2026-01", members, **options)
    batches.confirm_activation_batch(
        "activation-batch",
        "2026-01",
        members,
        **options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )
    company.close("2026-01")
    company.save(asset.model_copy(update={"cost_fen": 132000}), "computer", revision=1)
    members[0]["expected_revision"] = 1
    correction_options = {
        "evidence": evidence,
        "expected_revision": 1,
        "posting_period": "2026-02",
    }
    preview = batches.prepare_activation_batch(
        "activation-batch", "2026-01", members, **correction_options
    )
    batches.confirm_activation_batch(
        "activation-batch",
        "2026-01",
        members,
        **correction_options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=company.request(),
    )
    dashboard = Dashboard(company.engine)
    january = dashboard.assets("2026-01")["data"]
    february = dashboard.assets("2026-02")["data"]
    assert january["ledger_cost_fen"] == january["month_acquired_fen"] == 120000
    assert (
        february["ledger_cost_fen"]
        == sum(item["cost_fen"] for item in february["collections"]["assets"]["items"])
        == 132000
    )
    assert february["month_acquired_count"] == february["month_acquired_fen"] == 0
    assert february["month_activated_count"] == 0
    assert february["month_cost_adjustment_fen"] == 12000
    assert february["fixed"]["month_cost_adjustment_fen"] == 12000
    _assert_asset_member_summary_parity(company.engine, "2026-01")
    _assert_asset_member_summary_parity(company.engine, "2026-02")
    _assert_brief_asset_summary_parity(company.engine, "2026-01")
    _assert_brief_asset_summary_parity(company.engine, "2026-02")
    # A frozen balance root covers the old amount. The old projection row is
    # not a certified source for deciding whether cost minus carrying is charge.
    with company.engine.store.connection() as connection:
        changed = connection.execute(
            "UPDATE period_balance SET amount=amount+1 WHERE posting_period=? AND balance_key=?",
            (YearMonth("2026-01").ordinal, "asset:computer:carrying"),
        )
        assert changed.rowcount == 1
    from ai_accounting.kernel.dashboard import _Snapshot

    original_member_events = _Snapshot.asset_member_events

    def checked_member_events(snapshot, *args, **kwargs):
        assert "asset_ids" not in kwargs, "old balance row selected the full member walk"
        return original_member_events(snapshot, *args, **kwargs)

    with patch.object(_Snapshot, "asset_member_events", checked_member_events):
        assert dashboard.assets("2026-02")["data"] == february
    with company.engine.store.connection() as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_calculation_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_calculation_UPDATE")
        changed = connection.execute(
            "UPDATE calculation SET outcome=json_set(outcome,'$.balances[0].amount',0) "
            "WHERE id=(SELECT id FROM calculation WHERE kind='asset' AND period=? "
            "ORDER BY rowid LIMIT 1)",
            (YearMonth("2026-01").ordinal,),
        )
        assert changed.rowcount == 1
        connection.execute(trigger)
    with pytest.raises(KernelError) as failure:
        dashboard.assets("2026-02")
    assert failure.value.code == "content_integrity_failed"


def test_batch_asset_cards_depreciation_and_disposal_use_single_cost(bank_book):
    book, store, commit, proof = bank_book
    store("reimbursed_asset_batch", "batch", accepted_batch())
    store("reimbursed_asset", "computer", batch_card())
    store("reimbursed_asset", "chair", batch_card(30000, asset_id="chair"))
    commit("batch", "computer", "chair")
    batches = AssetBatches(book)
    members = [
        {"subject_id": "computer-use", "expected_revision": 0, "data": activation()},
        {
            "subject_id": "chair-use",
            "expected_revision": 0,
            "data": activation(asset_id="chair"),
        },
    ]
    options = {"evidence": (proof,), "expected_revision": 0}
    preview = batches.prepare_activation_batch("activation-batch", "2026-02", members, **options)
    batches.confirm_activation_batch(
        "activation-batch",
        "2026-02",
        members,
        **options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="activate-batch",
    )
    february = Dashboard(book).assets("2026-02")["data"]
    assert february["registered_count"] == 2
    assert (
        sum(item["cost_fen"] for item in february["collections"]["assets"]["items"])
        == february["ledger_cost_fen"]
        == 150000
    )
    february_assets = Dashboard(book).assets("2026-02", section="assets", asset_filter="fixed")[
        "data"
    ]["collections"]["assets"]["items"]
    assert all(item["acquisition_date"] is None for item in february_assets)
    assert all(item["settlement_scope"] == "本验收批次结算" for item in february_assets)
    assert all(
        item["payment_summary"]
        == {
            "obligation_count": 2,
            "checking": False,
            "amount_fen": 150000,
            "paid_fen": 0,
            "other_settled_fen": 0,
            "remaining_fen": 150000,
        }
        for item in february_assets
    )
    preview = batches.prepare_consumption_month("2026-03", **options)
    batches.confirm_consumption_month(
        "2026-03",
        **options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="consume-march",
    )
    march = Dashboard(book).assets("2026-03")["data"]
    assert march["month_charge_fen"] == 12500
    assert (
        sum(item["book_value_fen"] for item in march["collections"]["assets"]["items"])
        == march["ledger_net_fen"]
        == 137500
    )
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
    amended_members = [dict(item, expected_revision=1) for item in members]
    activation_options = {"evidence": (proof,), "expected_revision": 1}
    preview = batches.prepare_activation_batch(
        "activation-batch", "2026-02", amended_members, **activation_options
    )
    batches.confirm_activation_batch(
        "activation-batch",
        "2026-02",
        amended_members,
        **activation_options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="activate-after-disposal",
    )
    disposed = Dashboard(book).assets("2026-03")["data"]
    assert disposed["active_count"] == 1
    assert disposed["ledger_net_fen"] == 27500
    disposed_assets = Dashboard(book).assets("2026-03", section="assets", asset_filter="fixed")[
        "data"
    ]["collections"]["assets"]["items"]
    computer = next(item for item in disposed_assets if item["asset_id"] == "computer")
    assert computer["disposal"]["loss_fen"] == 110000
    assert "settlement" not in computer["disposal"]
    assert computer["book_value_fen"] == 0
    _assert_asset_member_summary_parity(book, "2026-02")
    _assert_asset_member_summary_parity(book, "2026-03")
    _assert_brief_asset_summary_parity(book, "2026-02")
    _assert_brief_asset_summary_parity(book, "2026-03")


def test_opening_cards_and_bank_balances_are_not_current_movements(opening_book):
    book, store, _, package, _ = opening_book
    package(complete_members(store))
    dashboard = Dashboard(book)
    funds = dashboard.funds("2026-01")["data"]
    assets = dashboard.assets("2026-01")["data"]
    assert funds["opening_fen"] == funds["total_fen"] == 1010000
    assert funds["movement_count"] == 0
    assert funds["inflow_fen"] == 0
    assert (
        sum(item["book_value_fen"] for item in assets["collections"]["assets"]["items"])
        == assets["ledger_net_fen"]
    )
    assert assets["ledger_net_fen"] == 100000
    assert assets["month_acquired_count"] == 0
    asset_items = dashboard.assets("2026-01", section="assets", asset_filter="fixed")["data"][
        "collections"
    ]["assets"]["items"]
    assert asset_items[0]["acquisition_date"] is None
    _assert_asset_member_summary_parity(book, "2026-01")
    _assert_brief_asset_summary_parity(book, "2026-01")


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
    data = Dashboard(book).employees("2026-01", employee_filter="all")["data"][
        "collections"
    ]["employees"]
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
    assert data["export"]["preview_digest"] == expected["digest"]
    assert data["export"]["epochs"] == expected["epochs"]
    assert (
        data["summary"]["assets_total_fen"]
        == expected["statements"]["balance_sheet"]["30"]["ending_fen"]
    )
    assert (
        data["summary"]["liabilities_total_fen"]
        == expected["statements"]["balance_sheet"]["47"]["ending_fen"]
    )


def test_empty_catalog_company_and_money_precision(bank_book, tmp_path):
    book, store, commit, _ = bank_book
    empty = Engine(
        Store.create(
            tmp_path / "empty.sqlite",
            production_bundle(),
            "empty",
            "911100000000000002",
            "empty-db",
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
    with pytest.raises(KernelError) as error:
        dashboard.funds("2026-09", limit=501)
    assert error.value.code == "invalid_command"


@pytest.mark.parametrize("zero_ending", [False, True])
def test_unmapped_month_account_is_reported_instead_of_silently_dropped(tmp_path, zero_ending):
    """A non-zero account outside every mapping table is collected, not dropped from 收入/费用."""

    class MappedExpense(Fact):
        kind: ClassVar[str] = "test_mapped_expense"
        amount: PositiveFen

    class MappedRevenue(Fact):
        kind: ClassVar[str] = "test_mapped_revenue"
        amount: PositiveFen

    class UnmappedExpense(Fact):
        kind: ClassVar[str] = "test_unmapped_expense"
        amount: PositiveFen

    class UnmappedRevenue(Fact):
        kind: ClassVar[str] = "test_unmapped_revenue"
        amount: PositiveFen

    class PreviousUnmappedBalance(Fact):
        kind: ClassVar[str] = "test_previous_unmapped_balance"

    def post(debit, credit):
        def calculate(version, context):
            amount = version.fact.amount
            return Outcome(
                (Line(debit, debit=amount), Line(credit, credit=amount)), {"amount": amount}
            )

        return calculate

    registry = Registry()
    registry.register(MappedExpense, post("5602", "2001"))
    registry.register(MappedRevenue, post("2001", "5001"))
    registry.register(UnmappedExpense, post("199901", "2001"))
    registry.register(UnmappedRevenue, post("2001", "199902"))
    registry.register(
        PreviousUnmappedBalance,
        lambda version, context: Outcome(
            (Line("199901", credit=700), Line("199902", debit=900), Line("2001", credit=200)), {}
        ),
    )
    engine = Engine(
        Store.create(
            tmp_path / "unmapped-account.sqlite",
            test_bundle(registry),
            "company-u",
            "91310000123456789U",
            "db-u",
        )
    )
    proof = engine.register_evidence(
        b"synthetic unmapped account", "text/plain", "unmapped", request_id="unmapped-evidence"
    )["digest"]
    if zero_ending:
        engine.save_fact(
            "test_previous_unmapped_balance",
            "previous-unmapped",
            {"period": "2025-12"},
            evidence=(proof,),
            expected_revision=0,
            request_id="previous-unmapped",
        )
        preview = engine.preview(["previous-unmapped"])
        engine.confirm(
            ["previous-unmapped"],
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="post-previous-unmapped",
        )
    facts = (
        ("mapped-expense", "test_mapped_expense", 100),
        ("mapped-revenue", "test_mapped_revenue", 400),
        ("unmapped-expense", "test_unmapped_expense", 700),
        ("unmapped-revenue", "test_unmapped_revenue", 900),
    )
    for subject, kind, amount in facts:
        engine.save_fact(
            kind,
            subject,
            {"period": "2026-01", "amount": amount},
            evidence=(proof,),
            expected_revision=0,
            request_id=f"save-{subject}",
        )
    subjects = [subject for subject, _, _ in facts]
    preview = engine.preview(subjects)
    engine.confirm(
        subjects,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="post-unmapped-accounts",
    )
    data = Dashboard(engine).brief("2026-01")["data"]
    position = diagnostic_position(engine, "2026-01")
    assert data["position"]["complete"] is False
    risk_keys = {item["key"] for item in data["risks"]}
    assert "month_amounts" in risk_keys
    assert ("ending_amounts" in risk_keys) is not zero_ending
    assert data["position"]["month_revenue_fen"] == 400
    assert data["position"]["month_expense_fen"] == 100
    assert data["position"]["month_result_fen"] == 300
    # Only the mapped accounts move 收入/费用; the unmapped amounts stay out of both sums.
    assert position["month_revenue_fen"] == 400
    assert position["month_expense_fen"] == 100
    assert position["month_result_fen"] == 300
    reported = [
        issue
        for issue in position["issues"]
        if issue["field"] == "account_mapping" and "amount_fen" in issue
    ]
    assert sorted(reported, key=lambda issue: issue["account"]) == [
        {
            "field": "account_mapping",
            "message": "存在未映射的非零账户余额，本月收入、费用不含该金额",
            "semantics": "accounting",
            "account": "199901",
            "amount_fen": 700,
        },
        {
            "field": "account_mapping",
            "message": "存在未映射的非零账户余额，本月收入、费用不含该金额",
            "semantics": "accounting",
            "account": "199902",
            "amount_fen": -900,
        },
    ]
    assert position["complete"] is False
    assert [wire_money(issue)["amount_fen"] for issue in reported] == ["700", "-900"]
