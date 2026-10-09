"""Employee cards retain month-end money without payroll-history projections."""

import pytest
from test_integrity_content import damage
from test_labor_assets import cost
from test_opening_continuation import book as opening_book_fixture
from test_payroll_corrections import company as company_fixture
from test_reimbursement_assets import book as labor_book_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Calculations, Dashboard, _Snapshot
from ai_accounting.kernel.response_contracts import validate_response

opening_book = opening_book_fixture
labor_book = labor_book_fixture
company = company_fixture


@pytest.fixture
def employee_money(opening_book):
    engine, save, publish, package, _ = opening_book
    package([
        ("opening_bank", "bank-start", {"bank_account_id": "bank", "balance_fen": 2000}),
        ("opening_payroll_payable", "prior-net", {
            "employee_id": "employee", "recipient_id": "employee", "payroll_period": "2025-12",
            "component": "net", "outstanding_fen": 2000,
        }),
    ])
    for subject, period, amount in (("paid-january", "2026-01", 600),
                                    ("paid-february", "2026-02", 50)):
        save("payment", subject, {
            "period": period, "actual_date": period + "-10", "direction": "outflow",
            "bank_account_id": "bank", "counterparty_id": "employee", "amount_fen": amount,
            "allocations": [{"source_kind": "opening_payroll_payable", "source_id": "prior-net",
                             "obligation": "primary", "amount_fen": amount}],
        })
    publish("paid-january", "paid-february")
    return engine


def test_focused_employee_keeps_month_money_without_history_reads_or_preloading(
    employee_money, monkeypatch,
):
    engine = employee_money
    original_selected = Calculations.selected
    original_summary = _Snapshot.settlement_summary
    summaries = []

    def no_history(self, *, kinds=None, subjects=None, posting_period=None):
        assert not kinds or "opening_payroll_payable" not in kinds
        return original_selected(
            self, kinds=kinds, subjects=subjects, posting_period=posting_period
        )

    def watched_summary(snapshot, **kwargs):
        result = original_summary(snapshot, **kwargs)
        summaries.append((kwargs, hasattr(snapshot, "people_asset_settlements")))
        return result

    monkeypatch.setattr(Calculations, "selected", no_history)
    monkeypatch.setattr(_Snapshot, "settlement_summary", watched_summary)
    dashboard = Dashboard(engine)
    for period, paid, remaining in (("2026-01", 600, 1400), ("2026-02", 50, 1350)):
        response = dashboard.employees(period, employee_id="employee", employee_filter="all")
        validate_response("dashboard_employees", response)
        assert response["schema_version"] == 11
        data = response["data"]
        assert set(data["collections"]) == {"employees", "labor_sources"}
        item = data["collections"]["employees"]["items"][0]
        assert item["net_salary_fen"] == 0
        assert (
            item["direct_net_payments_fen"] == data["employees"]["direct_net_payments_fen"] == paid
        )
        assert item["outstanding_net_fen"] == data["employees"]["outstanding_net_fen"] == remaining
    assert summaries
    assert all(options["include_history_counts"] is False and not preloaded
               for options, preloaded in summaries)


@pytest.mark.parametrize("section", ["payroll_sources", "settlement_events"])
def test_employee_dashboard_rejects_removed_collections(employee_money, section):
    with pytest.raises(KernelError) as failure:
        Dashboard(employee_money).employees("2026-01", employee_id="employee", section=section, employee_filter="all")
    assert failure.value.code == "invalid_command"


def test_focused_employee_still_rejects_damaged_monthly_wage_source(company):
    company.publish("january", "february")
    damage(company.engine, "calculation",
           "UPDATE calculation SET outcome=json_set(outcome,'$.values.gross_fen',1) "
           "WHERE subject_id='february'")
    with pytest.raises(KernelError) as failure:
        Dashboard(company.engine).employees("2026-02", employee_id="employee", employee_filter="all")
    assert failure.value.code == "content_integrity_failed"


def test_labor_payment_details_keep_their_shared_summary_cache(labor_book, monkeypatch):
    engine, save, publish = labor_book
    save("labor_project_cost", "project-labor", cost())
    publish("project-labor")
    original = _Snapshot.settlement_summary
    snapshots = []

    def watched(snapshot, **kwargs):
        result = original(snapshot, **kwargs)
        snapshots.append(snapshot)
        return result

    monkeypatch.setattr(_Snapshot, "settlement_summary", watched)
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-11") as snap:
        from ai_accounting.kernel.dashboard import _employees
        data = _employees(snap, sections={"labor_sources"})
        assert "project-labor" in snap.people_asset_settlements
    labor = data["collections"]["labor_sources"]["items"][0]
    assert labor["subject_id"] == "project-labor"
    assert labor["obligations"][0]["remaining_fen"] == 1600000
    assert snapshots
