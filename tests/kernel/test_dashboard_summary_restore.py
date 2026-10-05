"""The brief restores money summaries without loading unrelated history cards."""

import pytest
from test_banking import book as _bank_book
from test_dashboard_funds_alignment import _publish_filter_funding
from test_payroll import payroll, profile
from test_payroll_corrections import company as _company
from test_reports import book as _report_book
from test_reports import scenario

import ai_accounting.kernel.integrity as integrity
import ai_accounting.kernel.report_projection as party_projection
import ai_accounting.kernel.settlement_projection as settlement_projection
from ai_accounting.kernel.dashboard import Dashboard, _brief_workforce_cost, _position
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.response_contracts import validate_response

bank_book, company, report_book = _bank_book, _company, _report_book


def test_financial_position_reuses_all_bank_accounts_and_pagination_omits_summaries(
    bank_book, monkeypatch
):
    engine = bank_book[0]
    _publish_filter_funding(engine, ["bank-a", "bank-b"], period="2026-08")
    _publish_filter_funding(engine, ["bank-a", "bank-b", "bank-b"])
    calls = []
    original = FundsRead.account_summary

    def measured(self, **kwargs):
        result = original(self, **kwargs)
        calls.append((self.snap.period, set(self.account_rows), set(self.omitted_account_rows)))
        return result

    monkeypatch.setattr(FundsRead, "account_summary", measured)
    dashboard = Dashboard(engine)
    first = dashboard.brief("2026-09", limit=1)
    validate_response("dashboard_brief", first)
    data = first["data"]
    assert len(calls) == 1 and calls[0][1] == {("bank", "bank-a"), ("bank", "bank-b")}
    position = data["financial_position"]
    assert position["assets_fen"] == position["equity_fen"] == position["capital_fen"] == 5
    assert position["bank_fen"] == 5 and position["liabilities_fen"] == 0
    assert position["equation_valid"] and position["complete"]
    assert position["bank_calculation"] == {"opening_fen": 2, "inflow_fen": 3, "outflow_fen": 0}
    assert data["collections"]["activity"]["page"]["total_count"] == 3
    assert len(data["collections"]["activity"]["items"]) == 1
    for section in ("activity", "vouchers", "open_items"):
        page = dashboard.brief("2026-09", section=section, limit=1)
        validate_response("dashboard_brief", page)
        assert {"financial_position", "workforce_cost"}.isdisjoint(page["data"])
        assert page["data"]["position"] == data["position"]


def test_open_and_frozen_position_keep_the_same_business_amounts(report_book):
    engine, _, _, close = report_book
    scenario(report_book, tax=False)
    dashboard = Dashboard(engine)
    before = dashboard.brief("2026-02")["data"]["financial_position"]
    assert before["assets_fen"] == 50000
    assert before["liabilities_fen"] == 10000
    assert before["capital_fen"] == 50000 and before["cumulative_result_fen"] == -10000
    assert before["equity_fen"] == 40000 and before["equation_valid"]
    for period in ("2026-01", "2026-02"):
        close(period)
    assert dashboard.brief("2026-02")["data"]["financial_position"] == before


def test_current_payroll_summary_loads_only_current_adopted_content(company, monkeypatch):
    company.publish("january", "february")
    with company.engine.store.connection(read_only=True) as connection:
        current = {
            row[0] for row in connection.execute(
                "SELECT id FROM calculation WHERE subject_id='february'"
            )
        }
        historical = {
            row[0] for row in connection.execute(
                "SELECT id FROM calculation WHERE subject_id='january'"
            )
        }
    loaded = set()
    original = integrity._object

    def measured(raw, component, ident):
        if component == "calculation":
            loaded.add(ident)
        return original(raw, component, ident)

    monkeypatch.setattr(integrity, "_object", measured)
    with Dashboard(company.engine)._snapshot("2026-02") as snap:
        summary = _brief_workforce_cost(snap)
    assert current <= loaded and loaded.isdisjoint(historical)
    assert summary["employee"]["gross_salary_fen"] == 1000000
    assert summary["employee"]["employer_social_insurance_fen"] == 160000
    assert summary["total_fen"] == 1160000
    assert summary["employee"]["settlement_adjustment_fen"] == 0


def test_zero_journal_payroll_still_has_activity(company):
    company.save(
        profile(social_insurance_participating=False, social_insurance_base_fen=None),
        "profile", revision=1,
    )
    company.save(
        payroll(accounting_gross_salary_fen=0, tax_reported_salary_fen=0),
        "january", revision=1,
    )
    company.confirm_payroll("january")
    company.publish("january")
    data = Dashboard(company.engine).brief("2026-01")["data"]
    assert data["voucher_count"] == 0
    assert data["workforce_cost"]["has_activity"]
    assert data["workforce_cost"]["employee"]["has_activity"]
    assert data["workforce_cost"]["total_fen"] == 0


@pytest.mark.parametrize("quick_path", ["unavailable", "unsafe"])
def test_missing_party_quick_path_keeps_confirmable_business_amounts(
    report_book, monkeypatch, quick_path
):
    engine = report_book[0]
    scenario(report_book, classification=False, tax=False)
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-02") as snap:
        expected = _position(snap)
    # Aggregate unavailability is not a missing business fact. The fallback
    # still consumes the real typed expense and its exact saved voucher.
    monkeypatch.setattr(settlement_projection, "settlement_position_rows", lambda *_a, **_k: [])
    monkeypatch.setattr(
        party_projection, "_party_balance_position_lines",
        lambda *_a, **_k: None if quick_path == "unavailable"
        else party_projection._PARTY_POSITION_SUMMARY_UNSAFE,
    )
    with dashboard._snapshot("2026-02") as snap:
        actual = _position(snap)
    assert actual == expected
    assert actual["assets_fen"] == 50000 and actual["liabilities_fen"] == 10000


def _explicit_position_classification(book):
    engine, save, _, _ = book
    scenario(book, classification=False, tax=False)
    with engine.store.connection(read_only=True) as connection:
        version = connection.execute(
            "SELECT v.id FROM voucher_current h JOIN voucher_version v ON v.id=h.version_id "
            "JOIN calculation c ON c.id=v.calculation_id WHERE c.subject_id='cost'"
        ).fetchone()[0]
    save(
        "report_classification", "party-classification",
        {"period": "2026-02", "voucher_version_id": version,
         "counterparties": [{"line_no": 2, "counterparty_id": "supplier"}],
         "profit_details": [{"line_no": 1, "detail_code": "management_entertainment",
                             "amount_fen": 10000}]},
    )
    return version


def test_explicit_party_fallback_only_resolves_affected_saved_lines(report_book, monkeypatch):
    from ai_accounting.kernel.query_reads import QueryReads

    _explicit_position_classification(report_book)
    resolved = []
    original = QueryReads.report_line_relations_many

    def measured(self, grouped):
        resolved.extend(dict(line) for lines in grouped.values() for line in lines)
        return original(self, grouped)

    monkeypatch.setattr(QueryReads, "report_line_relations_many", measured)
    data = Dashboard(report_book[0]).brief("2026-02")["data"]
    assert [(line["account"], line["debit"], line["credit"]) for line in resolved] == [
        ("2202", 0, 10000),
    ]
    assert data["financial_position"]["assets_fen"] == 50000
    assert data["financial_position"]["liabilities_fen"] == 10000


@pytest.mark.parametrize("frozen", [False, True])
def test_position_fallback_rejects_damaged_historical_voucher_lines(report_book, frozen):
    from test_integrity_content import damage

    from ai_accounting.kernel.contracts import KernelError

    engine, _, _, close = report_book
    version = _explicit_position_classification(report_book)
    if frozen:
        close("2026-01")
        close("2026-02")
    dashboard = Dashboard(engine)
    assert dashboard.brief("2026-03")["data"]["financial_position"]["assets_fen"] == 40000
    # This history voucher is outside the complete current-month money proof.
    damage(
        engine, "voucher_line",
        "UPDATE voucher_line SET credit=credit+1 WHERE version_id=? AND line_no=2", (version,),
    )
    with pytest.raises(KernelError) as rejected:
        dashboard.brief("2026-03")
    assert rejected.value.code == "content_integrity_failed"
    assert rejected.value.response()["reason"] == "unbalanced_lines"
