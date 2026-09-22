"""Funds presentation follows published money boundaries and documented investments."""

import hashlib

import pytest
import test_banking as banking
import test_investments as investments
import test_opening_continuation as openings
import test_payroll_reserve_payment as reserve_payroll
import test_platforms as platforms
from entity_fixture import seed_entities
from test_dashboard_transport import authenticated
from test_resident_service import resident as resident_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.entities import Entities

bank_book = banking.book
investment_book = investments.book
platform_book = platforms.book
opening_book = openings.book
payroll_book = reserve_payroll.company
resident = resident_fixture


def _publish_filter_funding(engine, accounts, *, period="2026-09"):
    contents = [
        f"Synthetic funds filter evidence {period} {index}".encode()
        for index in range(len(accounts))
    ]
    proofs = [hashlib.sha256(content).hexdigest() for content in contents]
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            [
                (bytes.fromhex(proof), content, "text/plain", "fixture")
                for proof, content in zip(proofs, contents, strict=True)
            ],
        )
        connection.commit()
    facts = [
        {
            "kind": "funding",
            "subject_id": f"filter-{period}-{index:04}",
            "data": {
                "period": period,
                "owner_id": f"filter-owner-{period}-{index:04}",
                "amount_fen": 1,
                "funding_kind": "capital",
                "actual_date": f"{period}-02",
                "bank_account_id": account,
            },
            "evidence": [proofs[index]],
            "expected_revision": 0,
        }
        for index, account in enumerate(accounts)
    ]
    seed_entities(
        engine,
        [
            *((account, "fund_account", "bank") for account in sorted(set(accounts))),
            *((fact["data"]["owner_id"], "person", None) for fact in facts),
        ],
    )
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
    assert {row["account_id"] for row in first_all["collections"]["movements"]["items"]} == {
        "bank-a"
    }
    filters = {"movement_account_type": "bank", "movement_account_id": "bank-b"}
    first = dashboard.funds("2026-09", **filters, section="movements", limit=1)
    assert first["data"]["total_fen"] == first["data"]["inflow_fen"] == 503
    assert first["data"]["movement_count"] == 503
    assert first["data"]["collections"]["movements"]["page"]["filtered_count"] == 2
    assert [row["account_id"] for row in first["data"]["collections"]["movements"]["items"]] == [
        "bank-b"
    ]
    second = dashboard.funds(
        "2026-09",
        **filters,
        section="movements",
        limit=1,
        cursor=first["data"]["collections"]["movements"]["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
    )["data"]
    movement_page = second["collections"]["movements"]
    assert len(movement_page["items"]) == 1 and not movement_page["page"]["has_more"]
    assert (
        movement_page["items"][0]["id"]
        != first["data"]["collections"]["movements"]["items"][0]["id"]
    )
    account_first = dashboard.funds("2026-09", **filters, section="accounts", limit=1)
    account_page = account_first["data"]["collections"]["accounts"]
    assert account_page["page"]["total_count"] == 2
    assert account_page["page"]["returned_count"] == 1
    next_accounts = dashboard.funds(
        "2026-09",
        **filters,
        section="accounts",
        limit=1,
        cursor=account_page["page"]["next_cursor"],
        expected_version=account_first["snapshot_version"],
    )["data"]
    assert (
        account_page["items"] + next_accounts["collections"]["accounts"]["items"]
        == first_all["collections"]["accounts"]["items"]
    )


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
    first = dashboard.funds("2026-09", statement_account_id="bank-b", section="statements", limit=1)
    bank = first["data"]["bank_statement"]
    assert bank["transaction_count"] == bank["inflow_fen"] == 503
    statements = first["data"]["collections"]["statements"]
    assert statements["page"]["filtered_count"] == 2
    assert [row["account_id"] for row in statements["items"]] == ["bank-b"]
    second = dashboard.funds(
        "2026-09",
        statement_account_id="bank-b",
        section="statements",
        cursor=statements["page"]["next_cursor"],
        expected_version=first["snapshot_version"],
        limit=1,
    )["data"]["collections"]["statements"]
    assert not second["page"]["has_more"] and len(second["items"]) == 1
    assert second["items"][0]["id"] != statements["items"][0]["id"]
    empty = dashboard.funds("2026-09", statement_account_id="not-this-company")["data"]
    assert empty["collections"]["statements"]["page"]["filtered_count"] == 0
    assert empty["bank_statement"]["transaction_count"] == 503


def test_movement_filter_requires_matching_account_type_and_identifier(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, bank="shared-id", amount=100)
    save(
        "cash_funding",
        "cash",
        {
            "period": "2026-09",
            "actual_date": "2026-09-02",
            "cash_account_id": "cash-id",
            "owner_id": "owner",
            "funding_kind": "capital",
            "amount_fen": 200,
        },
    )
    publish("cash")
    dashboard = Dashboard(engine)
    for kind, ident, amount in (("bank", "shared-id", 100), ("cash", "cash-id", 200)):
        data = dashboard.funds(
            "2026-09", movement_account_type=kind, movement_account_id=ident, section="movements"
        )["data"]
        assert data["total_fen"] == 300
        assert data["collections"]["movements"]["page"]["filtered_count"] == 1
        assert data["collections"]["movements"]["items"][0]["amount_fen"] == amount
        assert data["collections"]["movements"]["items"][0]["account_type"] == kind
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
    first = dashboard.funds("2026-09", section="movements", limit=1)
    cursor = first["data"]["collections"]["movements"]["page"]["next_cursor"]
    for changes in (
        {"section": "movements", "movement_account_type": "bank", "movement_account_id": "bank-a"},
        {"section": "movements", "statement_account_id": "bank-a"},
        {"section": "investment_events"},
        {"section": "statements"},
    ):
        query = {
            "cursor": cursor,
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
            "2026-10",
            section="movements",
            cursor=cursor,
            expected_version=october["snapshot_version"],
        )
    assert rejected.value.code == "dashboard_snapshot_changed"
    refreshed = dashboard.funds("2026-09")
    with pytest.raises(KernelError) as rejected:
        dashboard.funds(
            "2026-09",
            section="movements",
            cursor=cursor,
            expected_version=refreshed["snapshot_version"],
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
    filters = (
        "period=2026-09&section=movements&limit=1&"
        "movement_account_type=bank&movement_account_id=bank-b"
    )
    query = f"/api/dashboard/funds?company_id={companies[0]}&{filters}"
    status, _, _, first = http.request(query, headers=headers)
    assert status == 200 and first["data"]["total_fen"] == "3"
    assert first["data"]["collections"]["movements"]["page"]["filtered_count"] == 2
    cursor = first["data"]["collections"]["movements"]["page"]["next_cursor"]
    status, _, _, following = http.request(
        query + f"&cursor={cursor}&expected_version={first['snapshot_version']}",
        headers=headers,
    )
    assert status == 200 and not following["data"]["collections"]["movements"]["page"]["has_more"]
    other_query = f"/api/dashboard/funds?company_id={companies[1]}&{filters}"
    _, _, _, other = http.request(other_query, headers=headers)
    status, _, _, rejected = http.request(
        other_query + f"&cursor={cursor}&expected_version={other['snapshot_version']}",
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
    assert (
        sum(account["inflow_fen"] for account in data["collections"]["accounts"]["items"]) == 1200
    )


def test_bank_platform_transfer_is_always_internal(platform_book):
    engine, save, publish, _ = platform_book
    save(
        "bank_platform_transfer", "transfer", platforms.transfer_data(direction="bank_to_platform")
    )
    publish("transfer")
    before = Dashboard(engine).funds("2026-09")["data"]
    assert before["internal_transfer_fen"] == 1000
    assert before["outflow_fen"] == 0


def test_reserve_expense_and_refund_follow_actual_funds_directions(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, amount=1000)
    save(
        "managed_reserve_expense",
        "reserve-expense",
        {
            "period": "2026-09",
            "actual_date": "2026-09-02",
            "bank_account_id": "bank-a",
            "amount_fen": 300,
        },
    )
    save(
        "managed_reserve_refund",
        "reserve-refund",
        {
            "period": "2026-09",
            "actual_date": "2026-09-03",
            "bank_account_id": "bank-a",
            "amount_fen": 100,
        },
    )
    publish("reserve-expense", "reserve-refund")

    data = Dashboard(engine).funds("2026-09")["data"]
    assert data["inflow_fen"] == 1100
    assert data["outflow_fen"] == 300
    assert data["internal_transfer_fen"] == 0
    assert data["net_change_fen"] == data["total_fen"] == 800
    reserve = {
        row["component_kinds"][0]: row
        for row in data["collections"]["movements"]["items"]
        if row["component_kinds"][0].startswith("managed_reserve_")
    }
    assert reserve["managed_reserve_expense"]["direction"] == "outflow"
    assert reserve["managed_reserve_refund"]["direction"] == "inflow"
    assert not any(row["internal_transfer"] for row in reserve.values())


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
        Entities(engine).update_entity_profile(
            ident,
            {"display_number": "001"},
            source="synthetic explicit display number",
            expected_revision=1,
            request_id=ident,
        )
    data = Dashboard(engine).funds("2026-09")["data"]
    assert {account["account_id"] for account in data["collections"]["accounts"]["items"]} == {
        "bank-a",
        "bank-b",
    }
    assert [account["code"] for account in data["collections"]["accounts"]["items"]] == [
        "001",
        "001",
    ]
    assert {row["account_id"] for row in data["collections"]["movements"]["items"]} == {
        "bank-a",
        "bank-b",
    }


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
    reviewed = Dashboard(engine).funds("2026-09", section="statements")["data"]
    assert reviewed["bank_statement"]["coverage_state"] == "partial"
    assert reviewed["bank_statement"]["needs_review_count"] == 1
    assert reviewed["collections"]["statements"]["items"][0]["state"] == "needs_review"


def test_movement_identity_survives_earlier_period_publication(bank_book):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, subject="first", amount=1000)
    banking.funding(save, publish, subject="second", amount=500)
    before = Dashboard(engine).funds("2026-09")["data"]
    banking.funding(save, publish, subject="earlier", amount=200, day="2026-08-01")
    after = Dashboard(engine).funds("2026-09")["data"]
    assert [row["id"] for row in before["collections"]["movements"]["items"]] == [
        row["id"] for row in after["collections"]["movements"]["items"]
    ]
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
    assert february["collections"]["investment_events"]["items"][0]["date"] is None
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
    assert paid["collections"]["investment_events"]["items"][-1]["settlement_fen"] == 4100


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
    plan = engine.preview(["redeem"], posting_period="2026-03")
    engine.confirm(
        ["redeem"],
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        posting_period="2026-03",
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
    first = dashboard.funds("2026-01", section="investment_events", limit=1)
    details = first["data"]["investments"]
    assert details["subscription_cost_fen"] == 20200
    events = first["data"]["collections"]["investment_events"]
    assert details["event_count"] == 2 and len(events["items"]) == 1
    cursor = events["page"]["next_cursor"]
    second = dashboard.funds(
        "2026-01",
        section="investment_events",
        cursor=cursor,
        expected_version=first["snapshot_version"],
        limit=1,
    )
    assert (
        second["data"]["collections"]["investment_events"]["items"][0]["id"]
        != events["items"][0]["id"]
    )
    assert second["data"]["investments"]["subscription_cost_fen"] == 20200
    Entities(engine).update_entity_profile(
        "fund-A",
        {"display_name": "明确基金名称"},
        source="synthetic confirmed product name",
        expected_revision=1,
        request_id="fund-name",
    )
    with pytest.raises(KernelError) as failure:
        dashboard.funds(
            "2026-01",
            section="investment_events",
            cursor=cursor,
            expected_version=first["snapshot_version"],
            limit=1,
        )
    assert failure.value.code == "dashboard_snapshot_changed"


def test_payroll_batch_is_one_real_bank_exit_and_names_actual_recipients(payroll_book):
    company = payroll_book
    fact = reserve_payroll.prepare(company)
    company.publish("gross-batch")
    for person in ("one", "two"):
        Entities(company.engine).update_entity_profile(
            person,
            {"display_name": "姓名-" + person},
            source="synthetic recipient profile",
            expected_revision=1,
            request_id="person-" + person,
        )
    data = Dashboard(company.engine).funds("2026-02")["data"]
    assert data["outflow_fen"] == fact.amount_fen
    assert data["movement_count"] == 1
    assert data["payment_platform_account_count"] == 0
    assert data["collections"]["movements"]["items"][0]["party"] == "姓名-one、姓名-two"
