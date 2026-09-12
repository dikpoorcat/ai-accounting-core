"""Funds presentation follows published money boundaries and documented investments."""

import pytest
import test_banking as banking
import test_investments as investments
import test_opening_continuation as openings
import test_payroll_reserve_payment as reserve_payroll
import test_platforms as platforms
from test_dashboard_transport import authenticated
from test_managed_reserve import scope_data
from test_resident_service import resident as resident_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display

bank_book = banking.book
investment_book = investments.book
platform_book = platforms.book
opening_book = openings.book
payroll_book = reserve_payroll.company
resident = resident_fixture


def _publish_filter_funding(engine, accounts, *, period="2026-09"):
    proof = engine.register_evidence(
        b"Synthetic funds filter evidence", "text/plain", "fixture", request_id="filter-proof"
    )["digest"]
    facts = [
        {
            "kind": "funding",
            "subject_id": f"filter-{period}-{index:04}",
            "data": {
                "period": period,
                "owner_id": "owner",
                "amount_fen": 1,
                "funding_kind": "capital",
                "actual_date": f"{period}-02",
                "bank_account_id": account,
            },
            "evidence": [proof],
            "expected_revision": 0,
        }
        for index, account in enumerate(accounts)
    ]
    engine.save_facts(facts, request_id=f"filter-facts-{period}")
    subjects = [fact["subject_id"] for fact in facts]
    preview = engine.preview(subjects)
    engine.confirm(
        subjects,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=f"filter-publish-{period}",
    )


def test_account_filter_covers_movements_beyond_the_first_500(bank_book):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 501 + ["bank-b"] * 2)
    dashboard = Dashboard(engine)
    first_all = dashboard.funds("2026-09", limit=500)["data"]
    assert {row["account_id"] for row in first_all["movements"]} == {"bank-a"}
    filters = {"movement_account_type": "bank", "movement_account_id": "bank-b"}
    first = dashboard.funds("2026-09", **filters, limit=1)
    assert first["data"]["total_fen"] == first["data"]["inflow_fen"] == 503
    assert first["data"]["movement_count"] == 503
    assert first["data"]["movement_page"]["total_count"] == 2
    assert [row["account_id"] for row in first["data"]["movements"]] == ["bank-b"]
    second = dashboard.funds(
        "2026-09",
        **filters,
        limit=1,
        after_movement=first["data"]["movement_page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )["data"]
    assert len(second["movements"]) == 1 and not second["movement_page"]["has_more"]
    assert second["movements"][0]["id"] != first["data"]["movements"][0]["id"]
    assert second["accounts"] == first_all["accounts"][:1]
    assert second["collections"]["accounts"]["page"]["total_count"] == 2
    assert second["collections"]["accounts"]["page"]["returned_count"] == 1
    next_accounts = dashboard.funds(
        "2026-09",
        **filters,
        section="accounts",
        limit=1,
        cursor=second["collections"]["accounts"]["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )["data"]
    assert second["accounts"] + next_accounts["accounts"] == first_all["accounts"]


def test_bank_filter_covers_all_provided_statement_rows(bank_book):
    engine, save, publish, _ = bank_book
    for account, count in (("bank-a", 501), ("bank-b", 2)):
        banking.opening(save, publish, account)
        banking.statement(
            save,
            publish,
            [banking.entry(f"row-{index:04}", amount=1) for index in range(count)],
            bank=account,
            subject=f"statement-{account}",
        )
    dashboard = Dashboard(engine)
    first = dashboard.funds("2026-09", statement_account_id="bank-b", limit=1)
    bank = first["data"]["bank_statement"]
    assert bank["transaction_count"] == bank["inflow_fen"] == 503
    assert bank["page"]["total_count"] == 2
    assert [row["account_id"] for row in bank["rows"]] == ["bank-b"]
    second = dashboard.funds(
        "2026-09",
        statement_account_id="bank-b",
        after_statement=bank["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
        limit=1,
    )["data"]["bank_statement"]
    assert not second["page"]["has_more"] and len(second["rows"]) == 1
    assert second["rows"][0]["id"] != bank["rows"][0]["id"]
    empty = dashboard.funds("2026-09", statement_account_id="not-this-company")["data"]
    assert empty["bank_statement"]["page"]["total_count"] == 0
    assert empty["bank_statement"]["transaction_count"] == 503


def test_movement_filter_keeps_account_type_and_identifier_together(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, bank="shared-id", amount=100)
    save(
        "cash_funding",
        "cash",
        {
            "period": "2026-09",
            "actual_date": "2026-09-02",
            "cash_account_id": "shared-id",
            "owner_id": "owner",
            "funding_kind": "capital",
            "amount_fen": 200,
        },
    )
    publish("cash")
    dashboard = Dashboard(engine)
    for kind, amount in (("bank", 100), ("cash", 200)):
        data = dashboard.funds(
            "2026-09", movement_account_type=kind, movement_account_id="shared-id"
        )["data"]
        assert data["total_fen"] == 300
        assert data["movement_page"]["total_count"] == 1
        assert data["movements"][0]["amount_fen"] == amount
        assert data["movements"][0]["account_type"] == kind
    for filters in (
        {"movement_account_id": "shared-id"},
        {"movement_account_type": "bank"},
        {"movement_account_type": "unsupported", "movement_account_id": "shared-id"},
        {"movement_account_type": "bank", "movement_account_id": ""},
        {"statement_account_id": ""},
    ):
        with pytest.raises(ValueError):
            dashboard.funds("2026-09", **filters)


def test_funds_cursor_cannot_cross_filters_sections_or_periods(bank_book):
    engine, save, publish, _ = bank_book
    _publish_filter_funding(engine, ["bank-a", "bank-a", "bank-b", "bank-b"])
    dashboard = Dashboard(engine)
    first = dashboard.funds("2026-09", limit=1)
    cursor = first["data"]["movement_page"]["next_cursor"]
    for changes in (
        {"movement_account_type": "bank", "movement_account_id": "bank-a"},
        {"statement_account_id": "bank-a"},
        {"after_investment": cursor, "after_movement": None},
        {"after_statement": cursor, "after_movement": None},
    ):
        query = {
            "after_movement": cursor,
            "expected_version": first["snapshot_version"],
            **changes,
        }
        with pytest.raises(KernelError) as rejected:
            dashboard.funds("2026-09", **query)
        assert rejected.value.code == "dashboard_snapshot_changed"
    banking.funding(save, publish, subject="later", day="2026-10-01")
    october = dashboard.funds("2026-10")
    with pytest.raises(KernelError) as rejected:
        dashboard.funds(
            "2026-10", after_movement=cursor, expected_version=october["snapshot_version"]
        )
    assert rejected.value.code == "dashboard_snapshot_changed"
    refreshed = dashboard.funds("2026-09")
    with pytest.raises(KernelError) as rejected:
        dashboard.funds(
            "2026-09", after_movement=cursor, expected_version=refreshed["snapshot_version"]
        )
    assert rejected.value.code == "dashboard_snapshot_changed"


def test_http_fund_filters_and_cursors_are_company_scoped(resident):
    service, _, _, http, _ = resident
    headers, _ = authenticated(resident)
    companies = [
        service.catalog.create_company(f"91310000123456789{suffix}", f"筛选测试{suffix}")["id"]
        for suffix in ("A", "B")
    ]
    for company in companies:
        _publish_filter_funding(service.engine(company), ["bank-a", "bank-b", "bank-b"])
    filters = "period=2026-09&limit=1&movement_account_type=bank&movement_account_id=bank-b"
    query = f"/api/dashboard/funds?company_id={companies[0]}&{filters}"
    status, _, _, first = http.request(query, headers=headers)
    assert status == 200 and first["data"]["total_fen"] == "3"
    assert first["data"]["movement_page"]["total_count"] == 2
    cursor = first["data"]["movement_page"]["next_cursor"]
    status, _, _, following = http.request(
        query + f"&after_movement={cursor}&expected_version={first['snapshot_version']}",
        headers=headers,
    )
    assert status == 200 and not following["data"]["movement_page"]["has_more"]
    other_query = f"/api/dashboard/funds?company_id={companies[1]}&{filters}"
    _, _, _, other = http.request(other_query, headers=headers)
    status, _, _, rejected = http.request(
        other_query + f"&after_movement={cursor}&expected_version={other['snapshot_version']}",
        headers=headers,
    )
    assert status == 409 and rejected["code"] == "dashboard_snapshot_changed"
    for extra in (
        "&movement_account_id=bank-a",
        "&statement_account_id=",
        "&unknown_filter=bank-a",
    ):
        assert http.request(query + extra, headers=headers)[0] == 400


def test_external_funds_exclude_both_bank_transfer_sides(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, amount=1000)
    save(
        "funds_transfer",
        "transfer",
        {
            "period": "2026-09",
            "actual_date": "2026-09-02",
            "source_bank_account_id": "bank-a",
            "destination_bank_account_id": "bank-b",
            "amount_fen": 200,
        },
    )
    publish("transfer")
    data = Dashboard(engine).funds("2026-09")["data"]
    assert data["inflow_fen"] == 1000
    assert data["outflow_fen"] == 0
    assert data["internal_transfer_fen"] == 200
    assert data["net_change_fen"] == data["total_fen"] == 1000
    assert sum(account["inflow_fen"] for account in data["accounts"]) == 1200


def test_cash_transfer_is_internal_but_reserve_expense_exits_company(platform_book):
    engine, save, publish, _ = platform_book
    save(
        "bank_platform_transfer", "transfer", platforms.transfer_data(direction="bank_to_platform")
    )
    publish("transfer")
    before = Dashboard(engine).funds("2026-09")["data"]
    assert before["internal_transfer_fen"] == 1000
    assert before["outflow_fen"] == 0
    save(
        "managed_reserve_scope",
        "scope",
        scope_data(
            costs=[dict(source_kind="bank_platform_transfer", source_id="transfer")],
            treatments=[dict(transfer_id="transfer", treatment="expense_on_boundary")],
        ),
    )
    publish("scope", "transfer")
    after = Dashboard(engine).funds("2026-09")["data"]
    assert after["outflow_fen"] == 1000
    assert after["internal_transfer_fen"] == 0
    assert after["payment_platform_account_count"] == 0
    assert after["account_count"] == 1
    assert not after["movements"][0]["internal_transfer"]


def test_cash_bank_transfer_uses_money_boundary(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, amount=1000)
    save(
        "cash_bank_transfer",
        "withdrawal",
        {
            "period": "2026-09",
            "actual_date": "2026-09-02",
            "direction": "withdrawal",
            "bank_account_id": "bank-a",
            "cash_account_id": "cash",
            "amount_fen": 200,
        },
    )
    publish("withdrawal")
    data = Dashboard(engine).funds("2026-09")["data"]
    assert data["inflow_fen"] == 1000 and data["outflow_fen"] == 0
    assert data["bank_fen"] == 800 and data["cash_fen"] == 200


def test_drafts_are_not_book_accounts_and_display_numbers_are_not_identity(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, amount=1000)
    banking.funding(save, publish, subject="second", bank="bank-b", amount=500)
    banking.funding(save, publish, subject="draft", bank="draft-bank", posted=False)
    for ident in ("bank-a", "bank-b"):
        Display(engine).save_display_profile(
            {
                "kind": "fund_account",
                "entity_id": ident,
                "display_number": "001",
                "source": "synthetic explicit display number",
            },
            expected_revision=0,
            request_id=ident,
        )
    data = Dashboard(engine).funds("2026-09")["data"]
    assert {account["account_id"] for account in data["accounts"]} == {"bank-a", "bank-b"}
    assert [account["code"] for account in data["accounts"]] == ["001", "001"]
    assert {row["account_id"] for row in data["movements"]} == {"bank-a", "bank-b"}


def test_bank_coverage_distinguishes_missing_partial_empty_and_review(bank_book):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    missing = Dashboard(engine).funds("2026-09")["data"]["bank_statement"]
    assert missing["coverage_state"] == "missing" and missing["inflow_fen"] is None
    _, original = banking.statement(save, publish, [])
    complete = Dashboard(engine).funds("2026-09")["data"]["bank_statement"]
    assert complete["coverage_state"] == "complete"
    assert complete["transaction_count"] == complete["inflow_fen"] == 0
    banking.opening(save, publish, "bank-b")
    partial = Dashboard(engine).funds("2026-09")["data"]["bank_statement"]
    assert partial["coverage_state"] == "partial" and partial["missing_account_count"] == 1
    changed = original | {"entries": [banking.entry(amount=123)], "closing_fen": 123}
    save("bank_statement", "statement", changed, revision=1)
    reviewed = Dashboard(engine).funds("2026-09")["data"]["bank_statement"]
    assert reviewed["coverage_state"] == "partial"
    assert reviewed["needs_review_count"] == 1
    assert reviewed["rows"][0]["state"] == "needs_review"


def test_movement_identity_survives_earlier_period_publication(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, subject="first", amount=1000)
    banking.funding(save, publish, subject="second", amount=500)
    before = Dashboard(engine).funds("2026-09")["data"]
    banking.funding(save, publish, subject="earlier", amount=200, day="2026-08-01")
    after = Dashboard(engine).funds("2026-09")["data"]
    assert [row["id"] for row in before["movements"]] == [row["id"] for row in after["movements"]]
    assert before["opening_fen"] == 0 and after["opening_fen"] == 200


def test_investment_confirmation_is_separate_from_actual_receipt(investment_book):
    engine, save, publish = investment_book
    save("money_fund_subscription", "buy", investments.subscription())
    publish("buy")
    january = Dashboard(engine).funds("2026-01")["data"]
    assert january["account_count"] == january["total_fen"] == 0
    assert january["bank_statement"]["coverage_state"] == "not_applicable"
    assert january["investments"]["closing_cost_fen"] == 10100
    assert january["investments"]["actual_payments_fen"] == 0
    save("money_fund_redemption", "redeem", investments.redemption())
    publish("redeem")
    february = Dashboard(engine).funds("2026-02")["data"]
    investment = february["investments"]
    assert investment["opening_cost_fen"] == 10100
    assert investment["closing_cost_fen"] == 6100
    assert investment["investment_income_fen"] == 100
    assert investment["actual_receipts_fen"] == 0
    assert investment["events"][0]["date"] is None
    save(
        "payment",
        "receipt",
        investments.payment(
            "money_fund_redemption",
            "redeem",
            4100,
            period="2026-02",
            direction="inflow",
        ),
    )
    publish("receipt")
    paid = Dashboard(engine).funds("2026-02")["data"]
    assert paid["inflow_fen"] == paid["investments"]["actual_receipts_fen"] == 4100
    assert paid["investments"]["event_count"] == 2
    assert paid["investments"]["events"][-1]["settlement_fen"] == 4100


def test_opening_investment_preserves_cost_without_inventing_subscription(opening_book):
    engine, _, _, package, _ = opening_book
    package(
        [
            (
                "opening_money_fund",
                "old-lot",
                {
                    "fund_id": "fund-A",
                    "original_lot_reference": "explicit-prior-lot",
                    "classification": investments.CLASSIFICATION,
                    "cost_basis": "documented_cost",
                    "cost_fen": 10000,
                },
            ),
            (
                "opening_equity",
                "capital",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 10000,
                    "holder_or_basis_id": "owner",
                },
            ),
        ]
    )
    data = Dashboard(engine).funds("2026-01")["data"]["investments"]
    assert data["opening_cost_fen"] == data["closing_cost_fen"] == 10000
    assert data["subscription_cost_fen"] == data["event_count"] == 0


def test_closed_investment_cost_stays_frozen_and_correction_is_delta(investment_book):
    engine, save, publish = investment_book
    save("money_fund_subscription", "buy", investments.subscription())
    publish("buy")
    investments.close(engine, "2026-01")
    save("money_fund_redemption", "redeem", investments.redemption())
    publish("redeem")
    investments.close(engine, "2026-02")
    original = Dashboard(engine).funds("2026-02")["data"]["investments"]
    save("money_fund_redemption", "redeem", investments.redemption(cost=4050), revision=1)
    plan = engine.preview(["redeem"], correction_period="2026-03")
    engine.confirm(
        ["redeem"],
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        correction_period="2026-03",
        request_id="cost-correction",
    )
    assert Dashboard(engine).funds("2026-02")["data"]["investments"] == original
    corrected = Dashboard(engine).funds("2026-03")["data"]["investments"]
    assert corrected["opening_cost_fen"] == 6100
    assert corrected["closing_cost_fen"] == 6050
    assert corrected["redemption_cost_fen"] == 50
    assert corrected["investment_income_fen"] == -50
    assert corrected["actual_receipts_fen"] == 0


def test_investment_detail_pages_preserve_totals_and_require_same_snapshot(investment_book):
    engine, save, publish = investment_book
    save("money_fund_subscription", "buy", investments.subscription())
    save("money_fund_subscription", "second", investments.subscription())
    publish("buy", "second")
    dashboard = Dashboard(engine)
    first = dashboard.funds("2026-01", limit=1)
    details = first["data"]["investments"]
    assert details["subscription_cost_fen"] == 20200
    assert details["event_count"] == 2 and len(details["events"]) == 1
    cursor = details["page"]["next_cursor"]
    second = dashboard.funds(
        "2026-01", after_investment=cursor, expected_version=first["snapshot_version"], limit=1
    )
    assert second["data"]["investments"]["events"][0]["id"] != details["events"][0]["id"]
    assert second["data"]["investments"]["subscription_cost_fen"] == 20200
    Display(engine).save_display_profile(
        {
            "kind": "asset",
            "entity_id": "fund-A",
            "display_name": "明确基金名称",
            "source": "synthetic confirmed product name",
        },
        expected_revision=0,
        request_id="fund-name",
    )
    with pytest.raises(KernelError) as failure:
        dashboard.funds(
            "2026-01", after_investment=cursor, expected_version=first["snapshot_version"], limit=1
        )
    assert failure.value.code == "dashboard_snapshot_changed"


def test_payroll_batch_is_one_real_bank_exit_and_names_actual_recipients(payroll_book):
    company = payroll_book
    fact = reserve_payroll.prepare(company)
    company.publish("scope", "gross-batch")
    for person in ("one", "two"):
        Display(company.engine).save_display_profile(
            {
                "kind": "employee",
                "entity_id": person,
                "display_name": "姓名-" + person,
                "source": "synthetic recipient profile",
            },
            expected_revision=0,
            request_id="person-" + person,
        )
    data = Dashboard(company.engine).funds("2026-02")["data"]
    assert data["outflow_fen"] == fact.amount_fen
    assert data["movement_count"] == 1
    assert data["payment_platform_account_count"] == 0
    assert data["movements"][0]["party"] == "姓名-one、姓名-two"
