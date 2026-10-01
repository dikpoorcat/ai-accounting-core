"""Dashboard semantics follow existing published payroll, obligations and asset sources."""

from typing import get_args

import pytest
from entity_fixture import save_entity_display_profile
from test_integrity_content import damage
from test_labor_assets import book as _labor_book
from test_labor_assets import chain, cost
from test_opening_continuation import book as _opening_book
from test_payroll import bonus, bonus_sources, opening, payroll, profile
from test_payroll_corrections import Company, actual
from test_payroll_corrections import company as _company
from test_payroll_tax_declarations import adopt, declare, pay
from test_reimbursement_assets import accepted_batch, batch_card
from test_reimbursement_assets import book as _asset_book
from test_reimbursement_assets import pay as asset_payment

import ai_accounting.kernel.dashboard as dashboard_module
from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _project_cost_balances
from ai_accounting.kernel.dashboard_reads import adopted_head_metadata
from ai_accounting.kernel.domains.adjustments import EmployeeAdvance
from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
from ai_accounting.kernel.domains.opening import OpeningPayrollPayable
from ai_accounting.kernel.domains.payroll import LaborAccrual
from ai_accounting.kernel.domains.transactions import Allocation
from ai_accounting.kernel.response_contracts import http_response, validate_response

company, labor_book, asset_book = _company, _labor_book, _asset_book
opening_book = _opening_book


def settlement_events(dashboard, response, period, employee_id):
    return dashboard.employees(
        period,
        section="settlement_events",
        employee_id=employee_id,
        expected_version=response["snapshot_version"],
    )["data"]["collections"]["settlement_events"]["items"]


def payroll_sources(dashboard, response, period, employee_id):
    return dashboard.employees(
        period,
        section="payroll_sources",
        employee_id=employee_id,
        expected_version=response["snapshot_version"],
    )["data"]["collections"]["payroll_sources"]["items"]


def test_employee_line_scope_preserves_open_and_closed_responses(company, monkeypatch):
    company.publish("january", "february")
    company.close("2026-01")
    dashboard = Dashboard(company.engine)
    original = dashboard_module.payroll_head_metadata
    observed = []

    def scoped(snap, kinds, *, line_count_period=None):
        heads = original(snap, kinds, line_count_period=line_count_period)
        observed.append((snap.period, line_count_period, heads))
        return heads

    for period in ("2026-01", "2026-02"):
        monkeypatch.setattr(dashboard_module, "payroll_head_metadata", scoped)
        response = dashboard.employees(
            period, employee_id="employee", section="payroll_sources", preparation="deferred"
        )
        assert observed[-1][1] == period
        assert all(
            head["line_count"] is None
            for head in observed[-1][2]
            if head["posting_period"] != dashboard_module.YearMonth(period).ordinal
        )
        assert all(
            head["line_count"] is not None
            for head in observed[-1][2]
            if head["posting_period"] == dashboard_module.YearMonth(period).ordinal
        )
        monkeypatch.setattr(
            dashboard_module,
            "payroll_head_metadata",
            lambda snap, kinds, *, line_count_period=None: adopted_head_metadata(snap, kinds),
        )
        complete = dashboard.employees(
            period, employee_id="employee", section="payroll_sources", preparation="deferred"
        )
        assert response == complete


def test_payroll_source_cards_follow_selected_page_order(company, monkeypatch):
    company.publish("january", "february")
    original = dashboard_module.Calculations.selected

    def reversed_selected(self, *, kinds=None, subjects=None, posting_period=None):
        selected = original(
            self, kinds=kinds, subjects=subjects, posting_period=posting_period
        )
        if subjects == {"january", "february"}:
            return dict(reversed(list(selected.items())))
        return selected

    monkeypatch.setattr(dashboard_module.Calculations, "selected", reversed_selected)
    items = Dashboard(company.engine).employees(
        "2026-02", employee_id="employee", section="payroll_sources", preparation="deferred"
    )["data"]["collections"]["payroll_sources"]["items"]
    assert [item["source_id"] for item in items] == ["january", "february"]


def test_opening_payroll_keeps_source_period_components_and_later_payment(opening_book):
    engine, save, publish, package, _ = opening_book
    components = get_args(OpeningPayrollPayable.model_fields["component"].annotation)
    members = [
        (
            "opening_bank",
            "bank-opening",
            {"bank_account_id": "bank", "balance_fen": 50000 * len(components)},
        ),
        *[
            (
                "opening_payroll_payable",
                f"prior-{component}",
                {
                    "employee_id": "employee",
                    "recipient_id": "employee" if component == "net" else "authority",
                    "payroll_period": "2025-12",
                    "component": component,
                    "outstanding_fen": 50000,
                },
            )
            for component in components
        ],
    ]
    package(members, period="2026-01")
    save(
        "payment",
        "prior-net-payment",
        {
            "period": "2026-01",
            "actual_date": "2026-01-15",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "employee",
            "amount_fen": 15000,
            "allocations": [
                {
                    "source_kind": "opening_payroll_payable",
                    "source_id": "prior-net",
                    "obligation": "primary",
                    "amount_fen": 15000,
                }
            ],
        },
    )
    publish("prior-net-payment")
    ledger = engine.ledger("2026-01")
    dashboard = Dashboard(engine)
    response = dashboard.employees("2026-01")
    assert (
        dashboard.brief("2026-01", preparation="deferred")["data"]["workforce_cost"]
        == response["data"]["workforce_cost"]
    )
    employees = response["data"]["employees"]
    employee = response["data"]["collections"]["employees"]["items"][0]
    sources = {
        source["component"]: source
        for source in payroll_sources(dashboard, response, "2026-01", employee["employee_id"])
    }
    assert set(sources) == set(components)
    assert employee["direct_net_payments_fen"] == 15000
    assert employee["gross_salary_fen"] == employees["ledger_cost_fen"] == 0
    for component, source in sources.items():
        assert source["period"] == "2025-12"
        assert source["opening_period"] == "2026-01"
        assert source["declarations"] == []
        obligation = source["obligations"][0]
        assert obligation["key"] == f"opening_payroll_payable:prior-{component}:primary"
        assert obligation["name"] == "primary"
        assert obligation["amount_fen"] == 50000
        assert obligation["remaining_fen"] == (35000 if component == "net" else 50000)
        assert "movements" not in source
    movement = next(
        item
        for item in settlement_events(dashboard, response, "2026-01", employee["employee_id"])
        if item["settlement_business"]["subject_id"] == "prior-net-payment"
    )
    assert movement["posting_period"] == "2026-01"
    assert movement["amount_fen"] == 15000
    assert movement["recipient_id"] == "employee"
    assert movement["source_business"]["subject_id"] == "prior-net"
    assert movement["source_calculation_id"] == sources["net"]["calculation_id"]
    assert engine.ledger("2026-01") == ledger


@pytest.mark.parametrize(
    ("start", "end", "expected_state", "expected_member"),
    [
        ("2025-12", "2026-04", "in_period", True),
        ("2025-12", "2026-02-01", "in_period", True),
        ("2025-12", "2026-01", "ended", False),
        ("2026-03", "2026-04", "not_started", False),
        ("2025-12", None, "unknown", None),
    ],
)
def test_explicit_employment_interval_precedes_current_inactive_status(
    company, start, end, expected_state, expected_member
):
    company.publish("january", "february")
    save_entity_display_profile(
        company.engine,
        {
            "kind": "employee",
            "entity_id": "employee",
            "employment_status": "inactive",
            "employment_start": start,
            "employment_end": end,
            "source": "明确的入离职资料",
        },
        expected_revision=0,
        request_id="employment-interval",
    )
    data = Dashboard(company.engine).employees("2026-02")["data"]
    employees = data["employees"]
    person = data["collections"]["employees"]["items"][0]
    assert person["period_state"] == expected_state
    assert person["in_period"] is expected_member
    assert employees["in_period_count"] == int(expected_member is True)
    assert employees["unknown_period_count"] == int(expected_member is None)
    assert person["gross_salary_fen"] == 1000000


def test_later_exit_record_preserves_explicit_employment_in_closed_month(company):
    company.publish("january", "february")
    company.close("2026-01")
    before = company.engine.ledger("2026-01")
    save_entity_display_profile(
        company.engine,
        {
            "kind": "employee",
            "entity_id": "employee",
            "employment_status": "inactive",
            "employment_start": "2025-12",
            "employment_end": "2026-04",
            "source": "后来登记的明确离职资料",
        },
        expected_revision=0,
        request_id="later-employment-record",
    )
    data = Dashboard(company.engine).employees("2026-01")["data"]
    employees = data["employees"]
    assert data["collections"]["employees"]["items"][0]["in_period"] is True
    assert employees["in_period_count"] == 1
    assert company.engine.ledger("2026-01") == before


def test_closed_payroll_identity_source_cannot_hide_from_employee_detail(company):
    company.save(profile(employee_id="employee-2", effective_to="2026-02"), "profile-2")
    company.save(opening(employee_id="employee-2"), "opening-2")
    company.save(payroll(employee_id="employee-2", profile_id="profile-2"), "january-2")
    company.confirm_payroll("january-2")
    company.publish("january", "january-2")
    company.close("2026-01")
    with company.engine.store.connection() as connection:
        fact_id = connection.execute(
            "SELECT c.fact_id FROM calculation_current h JOIN calculation c "
            "ON c.id=h.calculation_id WHERE h.subject_id='january'"
        ).fetchone()[0]
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='immutable_fact_payroll_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_fact_payroll_UPDATE")
        connection.execute(
            "UPDATE fact_payroll SET employee_id='employee-2' WHERE revision_id=?", (fact_id,)
        )
        connection.execute(trigger)
    with pytest.raises(KernelError) as failure:
        Dashboard(company.engine).employees(
            "2026-01",
            employee_id="employee",
            section="payroll_sources",
            preparation="deferred",
        )
    assert failure.value.code == "content_integrity_failed"
    with pytest.raises(KernelError) as brief_failure:
        Dashboard(company.engine).brief("2026-01", preparation="deferred")
    assert brief_failure.value.code == "content_integrity_failed"


def test_next_month_declaration_is_attached_to_its_wage_source(company):
    company.publish("january", "february")
    declare(company, period="2026-02", declaration_date="2026-02-06")
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-01")
    data = response["data"]["employees"]
    employee = response["data"]["collections"]["employees"]["items"][0]
    assert employee["declared_tax_fen"] == 72600
    source = next(
        item
        for item in payroll_sources(dashboard, response, "2026-01", employee["employee_id"])
        if item["source_id"] == "january"
    )
    declaration = source["declarations"][0]
    assert declaration["tax_period"] == "2026-01"
    assert declaration["recording_period"] == "2026-02"
    assert declaration["date"] == "2026-02-06"
    assert declaration["recorded_later"]
    assert data["in_period_count"] == 0 and data["unknown_period_count"] == 1


def test_retained_disbursement_difference_and_payment_keep_source_period(company):
    company.publish("january", "february")
    _, declared = declare(company)
    adopt(company, declared)
    pay(company, 847400)
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-02")
    employee = response["data"]["collections"]["employees"]["items"][0]
    january = next(
        item
        for item in payroll_sources(dashboard, response, "2026-02", employee["employee_id"])
        if item["source_id"] == "january"
    )
    net = next(item for item in january["obligations"] if item["name"] == "net")
    assert net["paid_fen"] == 847400
    assert net["remaining_fen"] == 60000
    assert january["disbursements"][0]["held_fen"] == 60000
    assert "movements" not in january
    movement = next(
        item
        for item in settlement_events(dashboard, response, "2026-02", employee["employee_id"])
        if item["settlement_business"]["subject_id"] == "payment"
    )
    assert movement["posting_period"] == "2026-02"
    assert movement["amount_fen"] == 847400
    assert movement["source_business"]["subject_id"] == "january"
    assert movement["source_calculation_id"] == january["calculation_id"]
    assert employee["direct_net_payments_fen"] == 847400


def test_later_disbursement_basis_is_visible_from_its_wage_month(company):
    company.publish("january", "february")
    _, declared = declare(company, period="2026-02")
    adopt(company, declared, period="2026-02")
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-01")
    employee = response["data"]["collections"]["employees"]["items"][0]
    source = next(
        item
        for item in payroll_sources(dashboard, response, "2026-01", employee["employee_id"])
        if item["source_id"] == "january"
    )
    assert source["disbursements"][0]["recording_period"] == "2026-02"
    assert source["disbursements"][0]["target_net_fen"] == 847400
    assert not source["disbursements"][0]["needs_review"]
    assert all(item["paid_fen"] == 0 for item in source["obligations"])


def test_personal_advance_is_clearing_without_company_cash(company):
    company.publish("january", "february")
    company.save(
        EmployeeAdvance(
            period="2026-02",
            payer_id="owner",
            payer_kind="owner",
            payment_on_behalf_confirmed=True,
            actual_creditor_payment_date="2026-02-10",
            sources=(
                Allocation(
                    source_kind="payroll", source_id="january", obligation="net", amount_fen=907400
                ),
            ),
        ),
        "owner-paid",
    )
    company.publish("owner-paid")
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-02")
    employee = response["data"]["collections"]["employees"]["items"][0]
    assert employee["recorded_net_payments_fen"] == 907400
    assert employee["direct_net_payments_fen"] == 0
    assert employee["other_net_settlements_fen"] == 907400
    january = next(
        item
        for item in payroll_sources(dashboard, response, "2026-02", employee["employee_id"])
        if item["source_id"] == "january"
    )
    assert "movements" not in january
    movement = next(
        item
        for item in settlement_events(dashboard, response, "2026-02", employee["employee_id"])
        if item["settlement_business"]["subject_id"] == "owner-paid"
    )
    assert movement["mode"] == "advance"
    assert movement["posting_period"] == "2026-02"
    assert movement["amount_fen"] == 907400
    assert movement["source_business"]["subject_id"] == "january"
    assert movement["source_calculation_id"] == january["calculation_id"]
    assert (
        next(item for item in january["obligations"] if item["name"] == "net")["remaining_fen"] == 0
    )


def test_bonus_is_separate_but_included_in_workforce_breakdown(tmp_path, monkeypatch):
    company = Company(tmp_path / "bonus.sqlite")
    for source in bonus_sources():
        company.save(source.fact, source.subject_id)
    company.save(bonus(), "bonus")
    company.publish("bonus")
    dashboard = Dashboard(company.engine)
    data = dashboard.employees("2026-01")["data"]
    cost = data["workforce_cost"]["employee"]
    assert cost["annual_bonus_fen"] == cost["total_fen"] == 3000000
    assert cost["gross_salary_fen"] == 0
    assert cost["breakdown_available"]

    def unexpected_historical_wage_scan(*_args, **_kwargs):
        pytest.fail("Brief workforce cost must not scan historical wage heads")

    monkeypatch.setattr(
        "ai_accounting.kernel.dashboard.payroll_head_metadata", unexpected_historical_wage_scan
    )
    assert (
        dashboard.brief("2026-01", preparation="deferred")["data"]["workforce_cost"]
        == data["workforce_cost"]
    )


def test_brief_workforce_cost_rejects_changed_calculation_body(tmp_path):
    company = Company(tmp_path / "bonus-calculation-corrupt.sqlite")
    for source in bonus_sources():
        company.save(source.fact, source.subject_id)
    company.save(bonus(), "bonus")
    company.publish("bonus")
    with company.engine.store.connection() as connection:
        triggers = list(
            connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='calculation'"
            )
        )
        for trigger in triggers:
            connection.execute(f'DROP TRIGGER "{trigger["name"]}"')
        connection.execute(
            "UPDATE calculation SET outcome=json_set(outcome,'$.values.gross_fen',"
            "json_extract(outcome,'$.values.gross_fen')+100) WHERE subject_id='bonus'"
        )
        for trigger in triggers:
            connection.execute(trigger["sql"])
    with pytest.raises(KernelError) as failure:
        Dashboard(company.engine).brief("2026-01", preparation="deferred")
    assert failure.value.code == "content_integrity_failed"
    with pytest.raises(KernelError) as detail_failure:
        Dashboard(company.engine).employees("2026-01", preparation="deferred")
    assert detail_failure.value.code == "content_integrity_failed"


def test_brief_workforce_cost_matches_closed_month_and_later_reversal(company):
    company.publish("january", "february")
    company.close("2026-01")
    company.save(actual(), "actual")
    company.publish("actual", posting_period="2026-03")
    dashboard = Dashboard(company.engine)
    for period in ("2026-01", "2026-03"):
        detailed = dashboard.employees(period)["data"]["workforce_cost"]
        brief = dashboard.brief(period, preparation="deferred")["data"]["workforce_cost"]
        assert brief == detailed
    assert brief["employee"]["periods"][0]["has_reversal"]


def test_unpaid_labor_is_explicit_without_inferred_tax_or_gross_settlement(tmp_path):
    company = Company(tmp_path / "labor.sqlite")
    company.save(
        LaborAccrual(
            period="2026-01",
            person_id="person",
            expense_class="management",
            gross_fee_fen=500000,
            tax_treatment="not_withheld_not_filed",
        ),
        "labor",
    )
    company.publish("labor")
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-01")
    validate_response("dashboard_employees", response)
    labor = response["data"]["workforce_cost"]["personal_labor"]
    source_response = dashboard.employees("2026-01", section="labor_sources")
    wire = http_response("dashboard_employees", source_response)
    labor_items = source_response["data"]["collections"]["labor_sources"]["items"]
    assert labor_items[0]["name"] == "未提供姓名或名称"
    assert (
        wire["data"]["collections"]["labor_sources"]["items"][0]["name"]
        == labor_items[0]["name"]
    )
    assert labor["withholding_status"] == "not_withheld"
    assert labor_items[0]["withholding_method"] == "not_withheld_not_filed"
    assert labor_items[0]["theoretical_tax_fen"] is None
    assert labor_items[0]["obligations"][0]["remaining_fen"] == 500000
    assert "settled_gross_fen" not in labor and "actual_withholding_tax_fen" not in labor
    assert (
        dashboard.brief("2026-01", preparation="deferred")["data"]["workforce_cost"]
        == response["data"]["workforce_cost"]
    )


def test_personal_labor_items_only_include_selected_posting_month(tmp_path):
    company = Company(tmp_path / "labor-period.sqlite")
    company.save(
        LaborAccrual(
            period="2026-01",
            person_id="january-person",
            expense_class="management",
            gross_fee_fen=500000,
            tax_treatment="not_withheld_not_filed",
        ),
        "january-labor",
    )
    company.publish("january-labor")
    company.save(
        LaborAccrual(
            period="2026-02",
            person_id="february-person",
            expense_class="management",
            gross_fee_fen=700000,
            tax_treatment="not_withheld_not_filed",
        ),
        "february-labor",
    )
    company.publish("february-labor")

    data = Dashboard(company.engine).employees("2026-02")["data"]
    labor = data["workforce_cost"]["personal_labor"]

    assert labor["total_fen"] == 700000
    labor_items = data["collections"]["labor_sources"]["items"]
    assert [item["source_id"] for item in labor_items] == ["february-labor"]
    assert labor_items[0]["period"] == "2026-02"
    assert data["collections"]["labor_sources"]["page"]["total_count"] == 1


@pytest.mark.parametrize("activated", [False, True])
def test_capitalized_labor_and_pending_intangible_are_visible_without_double_cost(
    labor_book, activated
):
    engine, _, _ = labor_book
    chain(labor_book, activated=False)
    if activated:
        with engine.store.connection(read_only=True) as connection:
            evidence = engine.store.current_fact(connection, "asset").evidence
        members = [
            {
                "subject_id": "activation",
                "expected_revision": 0,
                "data": {
                    "period": "2026-11",
                    "asset_id": "asset",
                    "in_use_date": "2026-11-30",
                    "useful_life_months": 60,
                    "residual_fen": 0,
                    "benefit_area": "administration",
                    "rounding_policy": "floor_final_remainder",
                },
            }
        ]
        batches = AssetBatches(engine)
        preview = batches.prepare_activation_batch(
            "activation-batch", "2026-11", members, evidence=evidence, expected_revision=0
        )
        batches.confirm_activation_batch(
            "activation-batch",
            "2026-11",
            members,
            evidence=evidence,
            expected_revision=0,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="activate-batch",
        )
    dashboard = Dashboard(engine)
    assets = dashboard.assets("2026-11")["data"]
    workforce = dashboard.employees("2026-11")["data"]["workforce_cost"]
    assert workforce["total_fen"] == 0
    assert workforce["capitalized_labor_fen"] == 1600000
    labor = dashboard.employees("2026-11", section="labor_sources")["data"]["collections"][
        "labor_sources"
    ]["items"][0]
    assert labor["capitalized"]
    labor_movements = dashboard.employees(
        "2026-11", section="settlement_events", employee_id=labor["person_id"]
    )["data"]["collections"]["settlement_events"]["items"]
    assert {item["mode"] for item in labor_movements} == {"offset", "payment"}
    assert assets["ledger_net_fen"] == assets["card_net_fen"] == 1600000
    assert assets["reconciled"]
    assert assets["pending_intangible_count"] == (0 if activated else 1)
    assert assets["project_cost_fen"] == 0
    brief_assets = dashboard.brief("2026-11", preparation="deferred")["data"]["long_term_assets"]
    assert brief_assets["net_fen"] == 1600000
    assert brief_assets["intangible_net_fen"] == (1600000 if activated else 0)
    assert brief_assets["fixed_net_fen"] == brief_assets["fixed_active_count"] == 0
    assert brief_assets["intangible_active_count"] == (1 if activated else 0)
    assert brief_assets["pending_count"] == (0 if activated else 1)
    assert brief_assets["project_cost_fen"] == 0
    intangible_assets = dashboard.assets("2026-11", section="assets", asset_filter="intangible")[
        "data"
    ]["collections"]["assets"]["items"]
    assert intangible_assets[0]["source_label"] == "项目形成"


def test_batch_asset_uses_batch_settlement_and_month_precision(asset_book):
    engine, save, publish = asset_book
    save("reimbursed_asset_batch", "batch", accepted_batch())
    save("reimbursed_asset", "computer", batch_card())
    save("reimbursed_asset", "chair", batch_card(30000, asset_id="chair"))
    publish("batch", "computer", "chair")
    save(
        "payment",
        "alice-paid",
        asset_payment("reimbursed_asset_batch", "batch", "alice", "alice", 90000),
    )
    publish("alice-paid")
    assets = Dashboard(engine).assets("2026-03")["data"]
    assert assets["ledger_net_fen"] == assets["card_net_fen"] == 150000
    assert assets["reconciled"]
    asset_items = Dashboard(engine).assets("2026-03", section="assets", asset_filter="fixed")[
        "data"
    ]["collections"]["assets"]["items"]
    for item in asset_items:
        assert item["recognition_label"] == "2026-02（按月确认）"
        assert item["source_party_label"] == "本验收批次债权人"
        assert item["settlement_scope"] == "本验收批次结算"
        assert item["settlements"][0]["source_id"] == "batch"
        assert "purchase_price_fen" not in item and "payment_date" not in item


def test_unreleased_project_cost_is_reconciled_without_an_asset_card(tmp_path):
    company = Company(tmp_path / "project.sqlite")
    company.save(LaborProjectCost.model_validate(cost()), "project-labor")
    company.publish("project-labor")
    assets = Dashboard(company.engine).assets("2026-11")["data"]
    assert assets["registered_count"] == 0
    assert assets["project_cost_fen"] == 1600000
    assert assets["ledger_net_fen"] == assets["card_net_fen"] == 1600000
    assert assets["reconciled"]
    project = Dashboard(company.engine).assets("2026-11", section="projects")["data"][
        "collections"
    ]["projects"]["items"][0]
    assert project["settlement"]["obligations"][0]["remaining_fen"] == 1600000
    brief_assets = Dashboard(company.engine).brief("2026-11", preparation="deferred")["data"][
        "long_term_assets"
    ]
    assert brief_assets["net_fen"] == brief_assets["project_cost_fen"] == 1600000
    assert brief_assets["fixed_active_count"] == brief_assets["intangible_active_count"] == 0
    assert brief_assets["pending_count"] == 0


def test_project_cost_account_candidates_keep_frozen_month_and_exact_correction(tmp_path):
    company = Company(tmp_path / "project-correction.sqlite")
    company.save(LaborProjectCost.model_validate(cost(period="2026-01")), "project-labor")
    company.publish("project-labor")
    company.close("2026-01")
    company.save(
        LaborProjectCost.model_validate(cost(period="2026-01", gross_fee_fen=1_700_000)),
        "project-labor",
        revision=1,
    )
    company.publish("project-labor", posting_period="2026-02")
    dashboard = Dashboard(company.engine)
    historical = dashboard.assets("2026-01")["data"]
    corrected = dashboard.assets("2026-02")["data"]
    assert historical["project_cost_fen"] == historical["card_net_fen"] == 1_600_000
    assert corrected["project_cost_fen"] == corrected["card_net_fen"] == 1_700_000
    assert historical["reconciled"] and corrected["reconciled"]
    assert (
        dashboard.brief("2026-01", preparation="deferred")["data"]["long_term_assets"][
            "project_cost_fen"
        ]
        == 1_600_000
    )
    assert (
        dashboard.brief("2026-02", preparation="deferred")["data"]["long_term_assets"][
            "project_cost_fen"
        ]
        == 1_700_000
    )


def test_project_cost_rejects_forged_frozen_voucher_reference(tmp_path):
    company = Company(tmp_path / "project-adoption.sqlite")
    company.save(LaborProjectCost.model_validate(cost(period="2026-01")), "project-labor")
    company.publish("project-labor")
    company.close("2026-01")
    dashboard = Dashboard(company.engine)
    with dashboard._snapshot("2026-01") as snap:
        assert _project_cost_balances(snap)["project-cost:project-labor"] == 1_600_000
    damage(
        company.engine,
        "close_reference",
        "UPDATE close_reference SET related_id='forged' WHERE reference_type='voucher'",
    )
    with dashboard._snapshot("2026-01") as snap:
        with pytest.raises(KernelError) as failure:
            _project_cost_balances(snap)
    assert failure.value.code == "read_index_integrity_failed"


@pytest.mark.parametrize(
    "changed",
    [
        "json_set(outcome,'$.balances[0].amount',1700000)",
        "json_set(outcome,'$.balances',json('[]'))",
    ],
)
def test_brief_and_assets_reject_changed_project_balance_content(tmp_path, changed):
    company = Company(tmp_path / "project-content.sqlite")
    company.save(LaborProjectCost.model_validate(cost()), "project-labor")
    company.publish("project-labor")
    damage(
        company.engine,
        "calculation",
        f"UPDATE calculation SET outcome={changed} WHERE subject_id='project-labor'",
    )
    dashboard = Dashboard(company.engine)
    for read in (dashboard.brief, dashboard.assets):
        with pytest.raises(KernelError) as failure:
            read("2026-11", preparation="deferred")
        assert failure.value.code == "content_integrity_failed"


def test_disbursement_pending_change_is_not_presented_as_current_confirmation(company):
    company.publish("january", "february")
    _, declaration = declare(company)
    fact, _ = adopt(company, declaration)
    company.save(fact, "basis", revision=1)
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-01")
    employee = response["data"]["collections"]["employees"]["items"][0]
    source = next(
        item
        for item in payroll_sources(dashboard, response, "2026-01", employee["employee_id"])
        if item["source_id"] == "january"
    )
    assert source["disbursements"][0]["needs_review"]
