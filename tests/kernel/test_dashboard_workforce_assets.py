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
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _project_cost_balances
from ai_accounting.kernel.dashboard_reads import adopted_head_metadata
from ai_accounting.kernel.domains.adjustments import EmployeeAdvance
from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
from ai_accounting.kernel.domains.opening import OpeningPayrollPayable
from ai_accounting.kernel.domains.payroll import LaborAccrual
from ai_accounting.kernel.domains.transactions import Allocation
from ai_accounting.kernel.response_contracts import (
    RESPONSE_ADAPTERS,
    http_response,
    validate_response,
)

company, labor_book, asset_book = _company, _labor_book, _asset_book
opening_book = _opening_book


def source_status(dashboard, subject, period):
    return BusinessQueries(dashboard.engine).business_status(subject, period)


def test_employee_line_scope_preserves_open_and_closed_responses(company, monkeypatch):
    company.publish("january", "february")
    company.close("2026-01")
    dashboard = Dashboard(company.engine)
    original = dashboard_module.payroll_list_head_metadata
    observed = []

    def scoped(snap, kinds, *, line_count_period=None):
        heads = original(snap, kinds, line_count_period=line_count_period)
        observed.append((snap.period, line_count_period, heads))
        return heads

    for period in ("2026-01", "2026-02"):
        monkeypatch.setattr(dashboard_module, "payroll_list_head_metadata", scoped)
        response = dashboard.employees(
            period, employee_id="employee", section="employees", preparation="deferred"
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
            "payroll_list_head_metadata",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            dashboard_module,
            "payroll_head_metadata",
            lambda snap, kinds, *, line_count_period=None: adopted_head_metadata(snap, kinds),
        )
        complete = dashboard.employees(
            period, employee_id="employee", section="employees", preparation="deferred"
        )
        assert response == complete


@pytest.mark.parametrize("section", ["payroll_sources", "settlement_events"])
def test_removed_employee_history_sections_are_rejected(company, section):
    company.publish("january", "february")
    with pytest.raises(KernelError) as failure:
        Dashboard(company.engine).employees(
            "2026-02", employee_id="employee", section=section, preparation="deferred"
        )
    assert failure.value.code == "invalid_command"


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
        response["data"]["workforce_cost"]["total_fen"]
        == response["data"]["employees"]["ledger_cost_fen"]
        + response["data"]["workforce_cost"]["personal_labor_fen"]
    )
    employees = response["data"]["employees"]
    employee = response["data"]["collections"]["employees"]["items"][0]
    sources = {
        component: source_status(dashboard, f"prior-{component}", "2026-01")
        for component in components
    }
    assert set(sources) == set(components)
    assert employee["direct_net_payments_fen"] == 15000
    assert employee["gross_salary_fen"] == employees["ledger_cost_fen"] == 0
    for component, source in sources.items():
        assert source["latest_fact"]["data"]["payroll_period"] == "2025-12"
        assert source["latest_fact"]["data"]["period"] == "2026-01"
        obligation = source["settlements"]["obligations"][0]
        assert obligation["key"] == f"opening_payroll_payable:prior-{component}:primary"
        assert obligation["name"] == "primary"
        assert obligation["source_amount_fen"] == 50000
        assert obligation["remaining_fen"] == (35000 if component == "net" else 50000)
    movement = next(
        item
        for item in sources["net"]["settlements"]["movements"]
        if item["settlement_business"]["subject_id"] == "prior-net-payment"
    )
    assert movement["posting_period"] == "2026-01"
    assert movement["signed_amount_fen"] == 15000
    assert movement["state"] == "resolved"
    assert movement["source_business"]["subject_id"] == "prior-net"
    assert sources["net"]["identity"]["subject_id"] == "prior-net"
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
            section="employees",
            preparation="deferred",
        )
    assert failure.value.code == "content_integrity_failed"
    with pytest.raises(KernelError) as business_failure:
        BusinessQueries(company.engine).business_status("january", "2026-01")
    assert business_failure.value.code == "content_integrity_failed"


def test_next_month_declaration_does_not_expand_owner_wage_payload(company):
    company.publish("january", "february")
    declare(company, period="2026-02", declaration_date="2026-02-06")
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-01")
    data = response["data"]["employees"]
    employee = response["data"]["collections"]["employees"]["items"][0]
    assert "declared_tax_fen" not in employee and "tax_details" not in employee
    assert {"payroll_sources", "settlement_events"}.isdisjoint(response["data"]["collections"])
    assert data["in_period_count"] == 0 and data["unknown_period_count"] == 1


def test_retained_disbursement_difference_and_payment_keep_source_period(company):
    company.publish("january", "february")
    _, declared = declare(company)
    adopt(company, declared)
    pay(company, 847400)
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-02")
    employee = response["data"]["collections"]["employees"]["items"][0]
    january = source_status(dashboard, "january", "2026-02")
    net = next(item for item in january["settlements"]["obligations"] if item["name"] == "net")
    assert net["paid_fen"] == 847400
    assert net["remaining_fen"] == 60000
    assert "payroll_sources" not in response["data"]["collections"]
    assert employee["outstanding_net_fen"] == 967400
    assert response["data"]["employees"]["outstanding_net_fen"] == 967400
    assert "settlement_events" not in response["data"]["collections"]
    movement = next(
        item
        for item in january["settlements"]["movements"]
        if item["settlement_business"]["subject_id"] == "payment"
    )
    assert movement["posting_period"] == "2026-02"
    assert movement["signed_amount_fen"] == 847400
    assert movement["source_business"]["subject_id"] == "january"
    assert january["identity"]["subject_id"] == "january"
    assert employee["direct_net_payments_fen"] == 847400


def test_later_disbursement_basis_does_not_change_owner_payment_amounts(company):
    company.publish("january", "february")
    _, declared = declare(company, period="2026-02")
    adopt(company, declared, period="2026-02")
    dashboard = Dashboard(company.engine)
    response = dashboard.employees("2026-01")
    employee = response["data"]["collections"]["employees"]["items"][0]
    source = source_status(dashboard, "january", "2026-01")
    assert employee["direct_net_payments_fen"] == 0
    assert "payroll_sources" not in response["data"]["collections"]
    assert all(item["paid_fen"] == 0 for item in source["settlements"]["obligations"])


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
    january = source_status(dashboard, "january", "2026-02")
    assert "settlement_events" not in response["data"]["collections"]
    movement = next(
        item
        for item in january["settlements"]["movements"]
        if item["settlement_business"]["subject_id"] == "owner-paid"
    )
    assert movement["mode"] == "advance"
    assert movement["posting_period"] == "2026-02"
    assert movement["signed_amount_fen"] == 907400
    assert movement["source_business"]["subject_id"] == "january"
    assert january["identity"]["subject_id"] == "january"
    assert (
        next(item for item in january["settlements"]["obligations"] if item["name"] == "net")[
            "remaining_fen"
        ] == 0
    )


def test_bonus_is_separate_but_included_in_workforce_breakdown(tmp_path, monkeypatch):
    company = Company(tmp_path / "bonus.sqlite")
    for source in bonus_sources():
        company.save(source.fact, source.subject_id)
    company.save(bonus(), "bonus")
    company.publish("bonus")
    dashboard = Dashboard(company.engine)
    data = dashboard.employees("2026-01")["data"]
    cost = data["employees"]
    assert (
        cost["annual_bonus_fen"]
        == cost["ledger_cost_fen"]
        == data["workforce_cost"]["total_fen"]
        == 3000000
    )
    assert cost["gross_salary_fen"] == 0
    assert not cost["checking"]

    def unexpected_historical_wage_scan(*_args, **_kwargs):
        pytest.fail("Brief workforce cost must not scan historical wage heads")

    monkeypatch.setattr(
        "ai_accounting.kernel.dashboard.payroll_head_metadata", unexpected_historical_wage_scan
    )
    assert "workforce_cost" not in dashboard.brief("2026-01", preparation="deferred")["data"]


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
    january = dashboard.employees("2026-01")["data"]
    march = dashboard.employees("2026-03")["data"]
    assert january["workforce_cost"]["total_fen"] == january["employees"]["ledger_cost_fen"]
    assert march["workforce_cost"]["total_fen"] == march["employees"]["ledger_cost_fen"]
    assert january["employees"]["gross_salary_fen"] == 1000000
    assert march["employees"]["gross_salary_fen"] == 0


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
    labor = response["data"]["workforce_cost"]
    source_response = dashboard.employees("2026-01", section="labor_sources")
    wire = http_response("dashboard_employees", source_response)
    labor_items = source_response["data"]["collections"]["labor_sources"]["items"]
    assert labor_items[0]["name"] == "未提供姓名或名称"
    assert (
        wire["data"]["collections"]["labor_sources"]["items"][0]["name"] == labor_items[0]["name"]
    )
    assert labor["personal_labor_fen"] == 500000
    assert labor_items[0]["withholding_method"] == "not_withheld_not_filed"
    assert "theoretical_tax_fen" not in labor_items[0]
    assert labor_items[0]["obligations"][0]["remaining_fen"] == 500000
    assert "settled_gross_fen" not in labor and "actual_withholding_tax_fen" not in labor
    assert (
        response["data"]["workforce_cost"]["total_fen"]
        == response["data"]["employees"]["ledger_cost_fen"]
        + response["data"]["workforce_cost"]["personal_labor_fen"]
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
    labor = data["workforce_cost"]

    assert labor["personal_labor_fen"] == 700000
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
    labor_movements = source_status(dashboard, labor["subject_id"], "2026-11")["settlements"][
        "movements"
    ]
    assert {item["mode"] for item in labor_movements} == {"offset", "payment"}
    assert assets["ledger_net_fen"] == 1600000
    assert not assets["checking"]
    assert assets["pending_intangible_count"] == (0 if activated else 1)
    assert assets["project_cost_fen"] == 0
    assert assets["intangible"]["active_net_fen"] == (1600000 if activated else 0)
    assert assets["fixed"]["active_net_fen"] == assets["fixed"]["active_count"] == 0
    assert assets["intangible"]["active_count"] == (1 if activated else 0)
    assert assets["intangible"]["pending_count"] == (0 if activated else 1)
    intangible_assets = dashboard.assets("2026-11", section="assets", asset_filter="intangible")[
        "data"
    ]["collections"]["assets"]["items"]
    assert intangible_assets[0]["settlement_scope"] == "成本来源结算（不分摊为本资产付款）"


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
    assert assets["ledger_net_fen"] == 150000
    assert not assets["checking"]
    asset_items = Dashboard(engine).assets("2026-03", section="assets", asset_filter="fixed")[
        "data"
    ]["collections"]["assets"]["items"]
    for item in asset_items:
        assert item["recognition_label"] == "2026-02（按月确认）"
        assert "source_parties" not in item and "source_party_label" not in item
        assert item["settlement_scope"] == "本验收批次结算"
        assert "settlements" not in item
        assert item["payment_summary"] == {
            "obligation_count": 2, "checking": False,
            "amount_fen": 150000, "paid_fen": 90000,
            "other_settled_fen": 0, "remaining_fen": 60000,
        }
        assert "purchase_price_fen" not in item and "payment_date" not in item


def test_unreleased_project_cost_is_reconciled_without_an_asset_card(tmp_path, monkeypatch):
    company = Company(tmp_path / "project.sqlite")
    company.save(LaborProjectCost.model_validate(cost()), "project-labor")
    company.publish("project-labor")
    def unexpected_settlement(*args, **kwargs):
        raise AssertionError("项目列表不应预装载已移除的付款详情")
    monkeypatch.setattr(BusinessQueries, "settlement_summary", unexpected_settlement)
    assets = Dashboard(company.engine).assets("2026-11")["data"]
    assert assets["registered_count"] == 0
    assert assets["project_cost_fen"] == 1600000
    assert assets["ledger_net_fen"] == 1600000
    assert not assets["checking"]
    project = Dashboard(company.engine).assets("2026-11", section="projects")["data"][
        "collections"
    ]["projects"]["items"][0]
    assert "settlement" not in project
    assert project["remaining_fen"] == 1600000
    assert assets["fixed"]["active_count"] == assets["intangible"]["active_count"] == 0
    assert assets["fixed"]["pending_count"] == assets["intangible"]["pending_count"] == 0


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
    assert historical["project_cost_fen"] == historical["ledger_net_fen"] == 1_600_000
    assert corrected["project_cost_fen"] == corrected["ledger_net_fen"] == 1_700_000
    assert not historical["checking"] and not corrected["checking"]
    assert historical["collections"]["projects"]["items"][0]["cost_fen"] == 1_600_000
    assert corrected["collections"]["projects"]["items"][0]["cost_fen"] == 1_700_000


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
    source = source_status(dashboard, "january", "2026-01")
    assert employee["direct_net_payments_fen"] == 0
    assert "payroll_sources" not in response["data"]["collections"]
    assert all(item["paid_fen"] == 0 for item in source["settlements"]["obligations"])


@pytest.mark.parametrize(
    "settlement_state", ["established", "unknown", "incomplete_known", "not_established"]
)
def test_employee_page_keeps_complete_month_end_payment_totals_and_exact_target(
    opening_book, monkeypatch, settlement_state
):
    engine, save, publish, package, _ = opening_book
    members = [
        ("opening_bank", "bank-opening", {"bank_account_id": "bank", "balance_fen": 1100000}),
        *[
            (
                "opening_payroll_payable",
                f"prior-{index:02}",
                {
                    "employee_id": f"employee-{index:02}",
                    "recipient_id": f"employee-{index:02}",
                    "payroll_period": "2025-12",
                    "component": "net",
                    "outstanding_fen": 50000,
                },
            )
            for index in range(22)
        ],
    ]
    package(members, period="2026-01")
    save(
        "payment",
        "prior-net-paid",
        {
            "period": "2026-02",
            "actual_date": "2026-02-10",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "employee-21",
            "amount_fen": 15000,
            "allocations": [
                {
                    "source_kind": "opening_payroll_payable",
                    "source_id": "prior-21",
                    "obligation": "primary",
                    "amount_fen": 15000,
                }
            ],
        },
    )
    publish("prior-net-paid")
    original_summary = dashboard_module._Snapshot.settlement_summary
    summary_scopes = []

    def checked_summary(self, *, subject_ids=None, current=False, include_history_counts=True):
        # Exercise the real source checks before testing propagation of a legal
        # summary contract. This does not claim normal writes damage a source.
        summary = original_summary(
            self,
            subject_ids=subject_ids,
            current=current,
            include_history_counts=include_history_counts,
        )
        assert summary["complete"] and not summary["issues"]
        summary_scopes.append(set(subject_ids))
        if settlement_state == "established":
            return summary
        if settlement_state == "not_established":
            # A complete empty obligation contract is not an incomplete state.
            return {**summary, "obligations": [], "status": "not_established"}
        obligations = [dict(item) for item in summary["obligations"]]
        if settlement_state == "unknown":
            target = next(
                item for item in obligations if item["source_business"]["subject_id"] == "prior-21"
            )
            for field in ("paid_fen", "period_paid_fen", "remaining_fen"):
                target[field] = None
            target["settlement_status"] = "unestablished"
        return {
            **summary,
            "obligations": obligations,
            "complete": False,
            "status": "partially_established",
            "issues": [{"field": "settlements", "message": "存在尚未确立的清偿关系"}],
        }

    monkeypatch.setattr(dashboard_module._Snapshot, "settlement_summary", checked_summary)
    dashboard = Dashboard(engine)
    first = dashboard.employees("2026-02")
    RESPONSE_ADAPTERS["dashboard_employees"].validate_python(first)
    data = first["data"]
    collection = data["collections"]["employees"]
    assert collection["page"]["returned_count"] == 20
    assert collection["page"]["total_count"] == 22 and collection["page"]["has_more"]
    assert "employee-21" not in {item["employee_id"] for item in collection["items"]}
    assert data["employees"]["net_salary_fen"] == 0
    unknown = settlement_state == "unknown"
    no_obligations = settlement_state == "not_established"
    assert data["employees"]["checking"] is (settlement_state in {"unknown", "incomplete_known"})
    assert data["employees"]["direct_net_payments_fen"] == (
        None if unknown else 0 if no_obligations else 15000
    )
    assert data["employees"]["other_net_settlements_fen"] == (None if unknown else 0)
    assert data["employees"]["outstanding_net_fen"] == (
        None if unknown else 0 if no_obligations else 1085000
    )
    focused = dashboard.employees("2026-02", employee_id="employee-21")["data"]
    assert focused["employee_id"] == "employee-21"
    assert [item["employee_id"] for item in focused["collections"]["employees"]["items"]] == [
        "employee-21"
    ]
    assert focused["collections"]["employees"]["items"][0]["outstanding_net_fen"] == (
        None if unknown else 0 if no_obligations else 35000
    )
    assert focused["employees"] == data["employees"]
    assert "payroll_sources" not in data["collections"]
    assert {"payroll_sources", "settlement_events"}.isdisjoint(focused["collections"])
    assert all(scope == {f"prior-{index:02}" for index in range(22)} for scope in summary_scopes)


def test_employee_empty_collection_has_known_zero_amounts_without_checking(
    opening_book, monkeypatch
):
    engine, _, _, package, _ = opening_book
    package(
        [
            ("opening_bank", "bank-opening", {"bank_account_id": "bank", "balance_fen": 100}),
            (
                "opening_equity",
                "capital-opening",
                {
                    "equity_kind": "paid_in_capital",
                    "holder_or_basis_id": "holder",
                    "balance_fen": 100,
                },
            ),
        ]
    )

    def no_obligations_read(*args, **kwargs):
        pytest.fail("An empty wage scope must not request a settlement summary")

    monkeypatch.setattr(dashboard_module._Snapshot, "settlement_summary", no_obligations_read)
    response = Dashboard(engine).employees("2026-01")
    RESPONSE_ADAPTERS["dashboard_employees"].validate_python(response)
    data = response["data"]
    assert not data["employees"]["checking"]
    assert data["collections"]["employees"]["items"] == []
    for field in ("direct_net_payments_fen", "other_net_settlements_fen", "outstanding_net_fen"):
        assert data["employees"][field] == 0


def test_employee_obligation_projection_keeps_full_core_cache_and_history(company, monkeypatch):
    import ai_accounting.kernel.settlement_freeze as freeze

    company.publish("january", "february")
    company.close("2026-01")
    original = freeze._key_publications
    history_calls = []

    def historical_counts(*args, **kwargs):
        history_calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(freeze, "_key_publications", historical_counts)
    with Dashboard(company.engine)._snapshot("2026-01") as snap:
        for current in (False, True):
            narrow = snap.settlement_summary(
                subject_ids={"january"}, current=current, include_history_counts=False
            )
            calls_before_full = len(history_calls)
            full = snap.settlement_summary(subject_ids={"january"}, current=current)
            assert len(history_calls) == calls_before_full + 1
            assert narrow == {
                key: value
                for key, value in full.items()
                if key not in {"business_count", "movement_count", "line_relation_count"}
            }
            assert (
                snap.settlement_summary(
                    subject_ids={"january"}, current=current, include_history_counts=False
                )
                is narrow
            )
            assert snap.settlement_summary(subject_ids={"january"}, current=current) is full


def test_employee_owner_projection_skips_removed_declaration_disbursement_and_current_reads(
    company, monkeypatch
):
    company.publish("january", "february")
    _, declaration = declare(company)
    adopt(company, declaration)
    original_by_kind = dashboard_module._Snapshot.by_kind
    original_summary = dashboard_module._Snapshot.settlement_summary
    original_calculation = dashboard_module._Snapshot.calculation

    def scoped_facts(self, *kinds):
        assert not set(kinds) & {"payroll_profile", "payroll_tax_declaration_actual"}
        return original_by_kind(self, *kinds)

    def historical_summary(self, *, subject_ids=None, current=False, include_history_counts=True):
        assert not current
        return original_summary(
            self,
            subject_ids=subject_ids,
            current=current,
            include_history_counts=include_history_counts,
        )

    def calculation_without_disbursement(self, ident):
        kind = self.connection.execute(
            "SELECT kind FROM calculation WHERE id=?", (ident,)
        ).fetchone()[0]
        assert kind != "payroll_disbursement_basis"
        return original_calculation(self, ident)

    monkeypatch.setattr(dashboard_module._Snapshot, "by_kind", scoped_facts)
    monkeypatch.setattr(dashboard_module._Snapshot, "settlement_summary", historical_summary)
    monkeypatch.setattr(dashboard_module._Snapshot, "calculation", calculation_without_disbursement)
    response = Dashboard(company.engine).employees("2026-01", employee_id="employee")
    RESPONSE_ADAPTERS["dashboard_employees"].validate_python(response)
    person = response["data"]["collections"]["employees"]["items"][0]
    assert (
        not {
            "tax_details",
            "declared_tax_fen",
            "field_sources",
            "profile_available",
            "social_insurance_base_fen",
        }
        & person.keys()
    )
    assert {"payroll_sources", "settlement_events"}.isdisjoint(response["data"]["collections"])
