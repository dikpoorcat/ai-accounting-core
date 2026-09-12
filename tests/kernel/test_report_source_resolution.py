"""Reports use frozen settlement identities and conserve aggregated creditor lines."""

from dataclasses import replace

import pytest
from test_opening_continuation import book as opening_fixture
from test_payroll import contribution_policy, income_tax_policy, opening, payroll
from test_payroll import profile as employee_profile
from test_reports import book as report_book
from test_reports import cit, close_quarter, profile

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.reports import Reports


@pytest.fixture
def book(tmp_path):
    return report_book.__wrapped__(tmp_path)


@pytest.fixture
def opening_book(tmp_path):
    return opening_fixture.__wrapped__(tmp_path)


def accepted(creditors=(("alice", 60000), ("bob", 60000))):
    total = sum(amount for _, amount in creditors)
    return {
        "period": "2026-01",
        "cost_fen": total,
        "company_acceptance_confirmed": True,
        "assets": [{"asset_id": "computer", "asset_type": "fixed", "cost_fen": total}],
        "creditors": [{"employee_id": party, "amount_fen": amount} for party, amount in creditors],
    }


def setup(book, creditors=(("alice", 60000), ("bob", 60000))):
    engine, save, publish, _ = book
    profile(save, publish)
    cit(save, publish)
    data = accepted(creditors)
    save("reimbursed_asset_batch", "batch", data)
    save(
        "reimbursed_asset",
        "computer",
        {
            "period": "2026-01",
            "cost_fen": data["cost_fen"],
            "asset_type": "fixed",
            "company_acceptance_confirmed": True,
            "acceptance_id": "batch",
        },
    )
    save(
        "cash_funding",
        "capital",
        {
            "period": "2026-01",
            "actual_date": "2026-01-02",
            "amount_fen": 200000,
            "cash_account_id": "cash",
            "owner_id": "owner",
            "funding_kind": "capital",
        },
    )
    publish("batch", "computer", "capital")
    return Reports(engine)


def payment(book, subject, party, amount, period="2026-02"):
    _, save, publish, _ = book
    save(
        "cash_payment",
        subject,
        {
            "period": period,
            "actual_date": f"{period}-02",
            "direction": "outflow",
            "cash_account_id": "cash",
            "counterparty_id": party,
            "amount_fen": amount,
            "allocations": [
                {
                    "source_kind": "reimbursed_asset_batch",
                    "source_id": "batch",
                    "obligation": party,
                    "amount_fen": amount,
                }
            ],
        },
    )
    publish(subject)


def test_own_aggregate_conserves_creditors_and_partial_payments(book):
    report = setup(book)
    initial = report.report(2026, 1)
    assert initial["status"] == "ready", initial["fact_issues"]
    assert initial["statements"]["balance_sheet"]["39"]["ending_fen"] == 120000
    payment(book, "alice-part", "alice", 30000)
    payment(book, "bob-full", "bob", 60000)
    plan = report.report(2026, 1)
    assert plan["status"] == "ready", plan["fact_issues"]
    balance = plan["statements"]["balance_sheet"]
    assert balance["39"]["ending_fen"] == 30000
    assert balance["8"]["ending_fen"] == 0
    assert plan["statements"]["cash_flow_statement"]["12"]["current_fen"] == 90000
    with book[0].store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM voucher_line l JOIN voucher_version v ON v.id=l.version_id "
                "JOIN calculation c ON c.id=v.calculation_id WHERE c.subject_id='batch'"
            ).fetchone()[0]
            == 2
        )


def test_equal_amount_bank_batch_uses_each_exact_obligation(book):
    report = setup(book, (("alice", 80000), ("bob", 60000)))
    _, save, publish, _ = book
    save(
        "payment",
        "bank-batch",
        {
            "period": "2026-02",
            "actual_date": "2026-02-10",
            "direction": "outflow",
            "bank_account_id": "bank",
            "counterparty_id": "bank-batch-provider",
            "amount_fen": 120000,
            "payment_method": "bank_batch",
            "allocations": [
                {
                    "source_kind": "reimbursed_asset_batch",
                    "source_id": "batch",
                    "obligation": party,
                    "recipient_id": party,
                    "amount_fen": 60000,
                }
                for party in ("alice", "bob")
            ],
        },
    )
    publish("bank-batch")
    plan = report.report(2026, 1)
    assert plan["status"] == "ready", plan["fact_issues"]
    balance = plan["statements"]["balance_sheet"]
    assert balance["39"]["ending_fen"] == 20000
    assert balance["8"]["ending_fen"] == 0
    assert plan["statements"]["cash_flow_statement"]["12"]["current_fen"] == 120000


def test_closed_reversal_keeps_original_creditor_splits_and_frozen_report(book):
    report = setup(book)
    payment(book, "alice-full", "alice", 60000)
    close_quarter(book)
    old = report.preview_export(2026, 1)
    _, save, publish, close = book
    # A supported correction of the creditor distribution changes no total asset cost.
    save("reimbursed_asset_batch", "batch", accepted((("alice", 70000), ("bob", 50000))), 1)
    publish("batch", correction_period="2026-04")
    payment(book, "bob-full", "bob", 50000, "2026-04")
    cit(save, publish, "2026-06")
    for month in ("2026-04", "2026-05", "2026-06"):
        close(month)
    plan = report.preview_export(2026, 2)
    assert plan["status"] == "ready", plan["fact_issues"]
    assert plan["statements"]["balance_sheet"]["39"]["ending_fen"] == 10000
    assert plan["statements"]["balance_sheet"]["8"]["ending_fen"] == 0
    assert plan["statements"]["cash_flow_statement"]["12"]["year_to_date_fen"] == 110000
    after = report.preview_export(2026, 1)
    for key in ("digest", "statements", "report_fact_ids", "source_closes"):
        assert after[key] == old[key]


@pytest.mark.parametrize("problem", ["amount", "normal", "account", "duplicate_key", "party"])
def test_invalid_own_obligation_metadata_never_becomes_a_complete_report(
    book, monkeypatch, problem
):
    engine = book[0]
    original = engine.store.registry.evaluators["reimbursed_asset_batch"]

    def broken(version, context):
        outcome = original(version, context)
        obligations = [dict(item) for item in outcome.values["obligations"]]
        if problem == "amount":
            obligations[0]["amount_fen"] -= 1
        elif problem == "normal":
            for item in obligations:
                item["normal"] = "debit"
        elif problem == "account":
            for item in obligations:
                item["account"] = "224104"
        elif problem == "duplicate_key":
            obligations[1]["key"] = obligations[0]["key"]
        else:
            obligations[0]["counterparty_id"] = None
        return replace(outcome, values={**outcome.values, "obligations": obligations})

    monkeypatch.setitem(engine.store.registry.evaluators, "reimbursed_asset_batch", broken)
    plan = setup(book).report(2026, 1)
    assert plan["status"] == "needs_information"
    assert {issue["field"] for issue in plan["fact_issues"]} & {
        "report_source.obligations",
        "report_classification.counterparty_id",
    }


def test_one_explicit_party_cannot_replace_confirmed_group(book):
    report = setup(book)
    engine, save, _, _ = book
    with engine.store.connection(read_only=True) as connection:
        voucher = connection.execute(
            "SELECT v.id FROM voucher_version v JOIN calculation c ON c.id=v.calculation_id "
            "WHERE c.subject_id='batch'"
        ).fetchone()[0]
    save(
        "report_classification",
        "collapse-group",
        {
            "period": "2026-01",
            "voucher_version_id": voucher,
            "counterparties": [{"line_no": 2, "counterparty_id": "alice"}],
        },
    )
    plan = report.report(2026, 1)
    assert plan["status"] == "needs_information"
    assert "counterparty_id" in {issue["field"] for issue in plan["fact_issues"]}


def test_conflicting_classification_sources_do_not_depend_on_last_fact(book):
    report = setup(book)
    engine, save, _, _ = book
    with engine.store.connection(read_only=True) as connection:
        voucher = connection.execute(
            "SELECT v.id FROM voucher_version v JOIN calculation c ON c.id=v.calculation_id "
            "WHERE c.subject_id='batch'"
        ).fetchone()[0]
    for subject, party in (("first-classification", "alice"), ("second-classification", "bob")):
        save(
            "report_classification",
            subject,
            {
                "period": "2026-01",
                "voucher_version_id": voucher,
                "counterparties": [{"line_no": 2, "counterparty_id": party}],
            },
        )

    plan = report.report(2026, 1)

    assert plan["status"] == "needs_information"
    assert plan["statements"]["balance_sheet"]["39"]["ending_fen"] == 120000
    assert "report_classification" in {item["field"] for item in plan["fact_issues"]}


def test_unknown_creditor_propagates_nullable_balance_and_check(book, monkeypatch):
    engine = book[0]
    original = engine.store.registry.evaluators["reimbursed_asset_batch"]

    def without_one_creditor(version, context):
        outcome = original(version, context)
        obligations = [dict(item) for item in outcome.values["obligations"]]
        obligations[0]["counterparty_id"] = None
        return replace(outcome, values={**outcome.values, "obligations": obligations})

    monkeypatch.setitem(
        engine.store.registry.evaluators, "reimbursed_asset_batch", without_one_creditor
    )
    plan = setup(book).report(2026, 1)

    assert plan["status"] == "needs_information"
    assert plan["statements"]["balance_sheet"]["39"]["ending_fen"] is None
    assert (
        next(item for item in plan["checks"] if item["code"] == "balance_ending_fen")["passed"]
        is None
    )
    view = Dashboard(engine).quarterly_report(2026, 1)
    assert view["summary"]["assets_total_fen"] is None
    assert view["checks"]["passed"] == sum(
        item["passed"] is True for item in view["checks"]["items"]
    )
    assert view["export"]["available"] is False


def test_opening_reclass_keeps_its_frozen_counterparty(opening_book):
    engine, save, _, package, _ = opening_book
    package(
        [
            (
                "opening_obligation",
                "receivable",
                {
                    "counterparty_id": "customer",
                    "nature": "customer_receivable",
                    "outstanding_fen": 20000,
                    "business_reference": "prior-sale",
                },
            ),
            (
                "opening_equity",
                "equity",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 20000,
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
            "company_name": "Opening reclass test",
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
            "explanation": "Explicit zero",
        },
    )

    plan = Reports(engine).report(2026, 1)

    assert plan["status"] == "ready", plan["fact_issues"]
    assert plan["statements"]["balance_sheet"]["4"]["beginning_fen"] == 20000


@pytest.mark.parametrize("via_acceptance", [False, True])
def test_statutory_contributions_reuse_exact_obligations_without_fictional_creditor(
    book, via_acceptance
):
    engine, save, publish, _ = book
    profile(save, publish)
    cit(save, publish)
    save(
        "payroll_contribution_policy",
        "contributions",
        contribution_policy().model_dump(mode="json"),
    )
    save("payroll_income_tax_policy", "income-tax", income_tax_policy().model_dump(mode="json"))
    for person in ("alice", "bob"):
        save(
            "payroll_profile",
            "profile-" + person,
            employee_profile(employee_id=person, effective_to="2026-02").model_dump(mode="json"),
        )
        save(
            "payroll_opening_state",
            "opening-" + person,
            opening(employee_id=person).model_dump(mode="json"),
        )
        for period in ("2026-01", "2026-02"):
            subject = person + "-" + period
            save(
                "payroll",
                subject,
                payroll(
                    period=period, employee_id=person, profile_id="profile-" + person
                ).model_dump(mode="json"),
            )
            publish(subject)
    sources = [
        {
            "source_kind": "payroll",
            "source_id": person + "-2026-01",
            "obligation": "employee_social",
            "amount_fen": amount,
        }
        for person, amount in (("alice", 40000), ("bob", 80000))
    ]
    if via_acceptance:
        save(
            "reimbursement_acceptance",
            "paid-by-owner",
            {
                "period": "2026-03",
                "payer_id": "owner",
                "payer_kind": "owner",
                "company_acceptance_confirmed": True,
                "original_debt_paid_confirmed": True,
                "sources": [
                    source | {"recipient_id": "social-collection-agency"} for source in sources
                ],
            },
        )
        publish("paid-by-owner")
        sources = [
            {
                "source_kind": "reimbursement_acceptance",
                "source_id": "paid-by-owner",
                "obligation": "primary",
                "amount_fen": 100000,
            }
        ]
    save(
        "cash_payment",
        "paid-contributions",
        {
            "period": "2026-03",
            "actual_date": "2026-03-10",
            "cash_account_id": "cash",
            "direction": "outflow",
            "counterparty_id": "owner" if via_acceptance else "social-collection-agency",
            "amount_fen": sum(source["amount_fen"] for source in sources),
            "allocations": sources,
        },
    )
    publish("paid-contributions")
    plan = Reports(engine).report(2026, 1)
    assert plan["status"] == "ready", plan["fact_issues"]
    assert plan["statements"]["balance_sheet"]["39"]["ending_fen"] == (
        220000 if via_acceptance else 200000
    )
    assert plan["statements"]["balance_sheet"]["8"]["ending_fen"] == 0
    assert plan["statements"]["cash_flow_statement"]["4"]["current_fen"] == (
        100000 if via_acceptance else 120000
    )
    close_quarter(book)
    closed = Reports(engine).preview_export(2026, 1)
    assert closed["statements"] == plan["statements"]
