"""Cost-supported money funds use the normal publisher and actual Payment path."""

import itertools

import pytest
from material_fixture import supporting_text

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.opening import CATEGORIES
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.types import YearMonth

CLASSIFICATION = "readily_redeemable_held_not_over_one_year"


@pytest.fixture
def book(tmp_path):
    registry = default_registry()
    engine = Engine(
        Store.create(tmp_path / "fund.sqlite", registry, "company", "911100000000000001", "db")
    )
    proof = engine.register_evidence(
        b"synthetic fund cost and settlement confirmations",
        "text/plain",
        "basis",
        request_id="proof",
    )["digest"]
    supporting_text(engine, proof)
    counter = itertools.count()

    def save(kind, subject, data, *, revision=0, evidence=True):
        return engine.save_fact(
            kind,
            subject,
            data,
            expected_revision=revision,
            evidence=(proof,) if evidence else (),
            request_id=f"save-{next(counter)}",
        )

    def publish(*subjects):
        preview = engine.preview(list(subjects))
        return engine.confirm(
            list(subjects),
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"publish-{next(counter)}",
        )

    return engine, save, publish


def subscription(**updates):
    return {
        "period": "2026-01",
        "fund_id": "fund-A",
        "counterparty_id": "fund-custodian",
        "classification": CLASSIFICATION,
        "cost_basis": "documented_cost",
        "purchase_price_fen": 10000,
        "acquisition_fees_fen": 100,
        "excludes_declared_unpaid_distributions": True,
        **updates,
    }


def redemption(cost=4000, proceeds=4100, **updates):
    return {
        "period": "2026-02",
        "fund_id": "fund-A",
        "counterparty_id": "fund-custodian",
        "classification": CLASSIFICATION,
        "cost_basis": "documented_cost",
        "costs": [{"source_kind": "money_fund_subscription", "source_id": "buy", "cost_fen": cost}],
        "net_proceeds_fen": proceeds,
        **updates,
    }


def payment(source_kind, source_id, amount, *, period="2026-01", direction="outflow"):
    return {
        "period": period,
        "actual_date": period + "-18",
        "direction": direction,
        "bank_account_id": "bank",
        "counterparty_id": "fund-custodian",
        "amount_fen": amount,
        "allocations": [
            {
                "source_kind": source_kind,
                "source_id": source_id,
                "obligation": "primary",
                "amount_fen": amount,
            }
        ],
    }


def balances(engine):
    with engine.store.connection(read_only=True) as connection:
        return [
            tuple(row)
            for row in connection.execute("SELECT * FROM balance ORDER BY category,balance_key")
        ]


def close(engine, period):
    periods = Periods(engine)
    with engine.store.connection(read_only=True) as connection:
        proof = bytes(
            connection.execute("SELECT digest FROM evidence ORDER BY rowid LIMIT 1").fetchone()[0]
        ).hex()
        kinds = {
            row[0]
            for row in connection.execute(
                "SELECT s.kind FROM subject s JOIN fact_current a ON a.subject_id=s.id "
                "JOIN fact_revision f ON f.id=a.fact_id WHERE f.period=?",
                (YearMonth(period).ordinal,),
            )
        }
    categories = {
        engine.store.registry.models[k].material_category
        for k in kinds
        if k in engine.store.registry.evaluators
    }
    for category in MATERIAL_CATEGORIES:
        busy = category in categories
        periods.inventory(
            period,
            category,
            evidence=[proof] if busy else [],
            expected=int(busy),
            no_business=not busy,
            confirmation_evidence=proof,
            request_id=f"inventory-{period}-{category}",
        )
    try:
        preview = periods.preview_close(period, owner_confirmation=proof)
    except KernelError as exc:
        pytest.fail(str(exc.response()))
    periods.close(
        period,
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=f"close-{period}",
    )


def test_purchase_partial_redemption_actual_cash_reports_and_rebuild(book):
    engine, save, publish = book
    save(
        "funding",
        "capital",
        {
            "period": "2026-01",
            "actual_date": "2026-01-01",
            "owner_id": "owner",
            "funding_kind": "capital",
            "amount_fen": 20000,
            "bank_account_id": "bank",
        },
    )
    save("money_fund_subscription", "buy", subscription())
    save("payment", "purchase-payment", payment("money_fund_subscription", "buy", 10100))
    publish("purchase-payment", "buy", "capital")
    save("money_fund_redemption", "redeem", redemption())
    save(
        "payment",
        "redemption-receipt",
        payment("money_fund_redemption", "redeem", 4100, period="2026-02", direction="inflow"),
    )
    publish("redemption-receipt", "redeem")
    before = balances(engine)
    assert any(row[1] == "money-fund-cost:buy" and row[-1] == 6100 for row in before), before
    engine.rebuild_projections(request_id="rebuild")
    assert balances(engine) == before
    save(
        "report_profile",
        "profile",
        {
            "period": "2026-01",
            "company_name": "测试公司",
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": "2026-01",
            "newly_established_zero_opening_confirmed": True,
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
            "explanation": "测试明确零所得税",
        },
    )
    report = Reports(engine).report(2026, 1)
    assert report["status"] == "ready", report
    assert report["statements"]["profit_statement"]["20"]["year_to_date_fen"] == 100
    assert report["statements"]["cash_flow_statement"]["8"]["year_to_date_fen"] == 4100
    assert report["statements"]["cash_flow_statement"]["9"]["year_to_date_fen"] == 0
    assert report["statements"]["cash_flow_statement"]["11"]["year_to_date_fen"] == 10100
    assert report["statements"]["balance_sheet"]["2"]["ending_fen"] == 6100
    # Only actual Payment sources enter bank matching, never the fund holding.
    for period, opening, closing, entries, matches in (
        (
            "2026-01",
            0,
            9900,
            [
                {"reference": "capital", "actual_date": "2026-01-01", "signed_fen": 20000},
                {"reference": "purchase", "actual_date": "2026-01-18", "signed_fen": -10100},
            ],
            [
                {"reference": "capital", "source_kind": "funding", "source_id": "capital"},
                {
                    "reference": "purchase",
                    "source_kind": "payment",
                    "source_id": "purchase-payment",
                },
            ],
        ),
        (
            "2026-02",
            9900,
            14000,
            [{"reference": "redeem", "actual_date": "2026-02-18", "signed_fen": 4100}],
            [{"reference": "redeem", "source_kind": "payment", "source_id": "redemption-receipt"}],
        ),
    ):
        if period == "2026-01":
            save(
                "bank_opening",
                "bank-start",
                {
                    "period": period,
                    "bank_account_id": "bank",
                    "opening_fen": 0,
                    "basis": "new_account",
                },
            )
            publish("bank-start")
        save(
            "bank_statement",
            "statement-" + period,
            {
                "period": period,
                "bank_account_id": "bank",
                "opening_fen": opening,
                "closing_fen": closing,
                "entries": entries,
            },
        )
        save(
            "bank_reconciliation",
            "reconcile-" + period,
            {
                "period": period,
                "bank_account_id": "bank",
                "statement_id": "statement-" + period,
                "matches": matches,
            },
        )
        publish("statement-" + period, "reconcile-" + period)
    save(
        "bank_statement",
        "statement-2026-03",
        {
            "period": "2026-03",
            "bank_account_id": "bank",
            "opening_fen": 14000,
            "closing_fen": 14000,
            "entries": [],
        },
    )
    save(
        "bank_reconciliation",
        "reconcile-2026-03",
        {
            "period": "2026-03",
            "bank_account_id": "bank",
            "statement_id": "statement-2026-03",
            "matches": [],
        },
    )
    publish("statement-2026-03", "reconcile-2026-03")
    for period in ("2026-01", "2026-02", "2026-03"):
        close(engine, period)
    assert Reports(engine).preview_export(2026, 1)["statements"] == report["statements"]
    save("money_fund_redemption", "redeem", redemption(cost=4050), revision=1)
    correction = engine.preview(["redeem"], correction_period="2026-04")
    engine.confirm(
        ["redeem"],
        preview_digest=correction["digest"],
        epochs=correction["epochs"],
        correction_period="2026-04",
        request_id="closed-cost-correction",
    )
    assert Reports(engine).preview_export(2026, 1)["statements"] == report["statements"]
    corrected_balances = balances(engine)
    assert ("bank", "bank", 14000) in corrected_balances
    assert ("short_term_investment", "money-fund-cost:buy", 6050) in corrected_balances
    engine.rebuild_projections(request_id="rebuild-closed-correction")
    assert balances(engine) == corrected_balances


@pytest.mark.parametrize(
    "field",
    [
        "classification",
        "cost_basis",
        "purchase_price_fen",
        "acquisition_fees_fen",
        "excludes_declared_unpaid_distributions",
    ],
)
def test_subscription_missing_accounting_basis_is_structured(book, field):
    engine, save, _ = book
    data = subscription()
    del data[field]
    save("money_fund_subscription", "buy", data)
    with pytest.raises(NeedsInformation) as exc:
        engine.preview(["buy"])
    assert exc.value.issues[0]["field"] == field


@pytest.mark.parametrize("field", ["classification", "cost_basis", "costs", "net_proceeds_fen"])
def test_redemption_missing_basis_is_not_guessed(book, field):
    engine, save, publish = book
    save("money_fund_subscription", "buy", subscription())
    publish("buy")
    data = redemption()
    del data[field]
    save("money_fund_redemption", "redeem", data)
    with pytest.raises(NeedsInformation) as exc:
        engine.preview(["redeem"])
    assert exc.value.issues[0]["field"] == field


def test_cost_allocation_includes_unpublished_peers_and_rejects_wrong_fund(book):
    engine, save, publish = book
    save("money_fund_subscription", "buy", subscription())
    publish("buy")
    save("money_fund_redemption", "one", redemption(cost=6000))
    save("money_fund_redemption", "two", redemption(cost=5000))
    with pytest.raises(KernelError, match="超过") as exc:
        engine.preview(["one", "two"])
    assert exc.value.code == "money_fund_cost_exceeded"
    save("money_fund_redemption", "two", redemption(cost=4000), revision=1)
    save("money_fund_redemption", "wrong-fund", redemption(cost=100, fund_id="different-fund"))
    with pytest.raises(KernelError) as exc:
        engine.preview(["wrong-fund"])
    assert exc.value.code == "money_fund_identity"


def test_opening_cost_is_not_activity_and_loss_redemption_can_settle_later(book):
    engine, save, publish = book
    counts = dict.fromkeys(CATEGORIES.values(), 0)
    counts.update(assets=1, equity=1)
    save(
        "opening_money_fund",
        "old-lot",
        {
            "period": "2026-01",
            "package_id": "opening",
            "fund_id": "fund-A",
            "original_lot_reference": "prior-cost-card",
            "classification": CLASSIFICATION,
            "cost_basis": "documented_cost",
            "cost_fen": 10000,
        },
    )
    save(
        "opening_equity",
        "equity",
        {
            "period": "2026-01",
            "package_id": "opening",
            "equity_kind": "paid_in_capital",
            "balance_fen": 10000,
            "holder_or_basis_id": "owner",
        },
    )
    save(
        "opening_package",
        "opening",
        {
            "period": "2026-01",
            "package_id": "opening",
            "counts": counts,
            "members": [
                {"kind": "opening_money_fund", "subject_id": "old-lot"},
                {"kind": "opening_equity", "subject_id": "equity"},
            ],
            "completeness_confirmed": True,
        },
    )
    publish("old-lot", "opening", "equity")
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher_current").fetchone()[0] == 0
        assert tuple(
            connection.execute(
                "SELECT account,debit,credit FROM opening_account WHERE account='1101'"
            ).fetchone()
        ) == ("1101", 10000, 0)
    save(
        "money_fund_redemption",
        "redeem",
        redemption(
            costs=[
                {"source_kind": "opening_money_fund", "source_id": "old-lot", "cost_fen": 10000}
            ],
            proceeds=9900,
        ),
    )
    publish("redeem")
    save(
        "payment",
        "receipt",
        payment("money_fund_redemption", "redeem", 9900, period="2026-03", direction="inflow"),
    )
    publish("receipt")
    before = balances(engine)
    engine.rebuild_projections(request_id="rebuild-opening")
    assert balances(engine) == before
    save(
        "continuation_report_profile",
        "profile",
        {
            "period": "2026-01",
            "company_name": "期初基金测试",
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
            "explanation": "测试零所得税",
        },
    )
    report = Reports(engine).report(2026, 1)
    assert report["status"] == "ready", report
    statements = report["statements"]
    assert statements["balance_sheet"]["2"]["beginning_fen"] == 10000
    assert statements["balance_sheet"]["2"]["ending_fen"] == 0
    assert statements["profit_statement"]["20"]["year_to_date_fen"] == -100
    assert statements["cash_flow_statement"]["8"]["year_to_date_fen"] == 9900
    assert statements["cash_flow_statement"]["11"]["year_to_date_fen"] == 0


def test_zero_proceeds_full_loss_does_not_fabricate_cash(book):
    engine, save, publish = book
    save("money_fund_subscription", "buy", subscription())
    publish("buy")
    save("money_fund_redemption", "redeem", redemption(cost=10100, proceeds=0))
    preview = engine.preview(["redeem"])
    assert not any(line.get("cashflow") for calc in preview["results"] for line in calc["lines"])
    publish("redeem")


def test_evidence_required_and_arbitrary_accounts_forbidden(book):
    engine, save, _ = book
    with pytest.raises(NeedsInformation) as exc:
        save("money_fund_subscription", "buy", subscription(), evidence=False)
    assert exc.value.issues[0]["field"] == "evidence"
    with pytest.raises(ValueError):
        save("money_fund_subscription", "free-account", subscription(account="1002"))


def test_cost_correction_keeps_actual_receipt_and_recomputes_profit(book):
    engine, save, publish = book
    save("money_fund_subscription", "buy", subscription())
    save("money_fund_redemption", "redeem", redemption())
    save(
        "payment",
        "receipt",
        payment("money_fund_redemption", "redeem", 4100, period="2026-02", direction="inflow"),
    )
    publish("buy", "redeem", "receipt")
    with engine.store.connection(read_only=True) as connection:
        funds_before = tuple(
            connection.execute(
                "SELECT id,digest FROM fact_revision WHERE subject_id='receipt'"
            ).fetchone()
        )
    save("money_fund_redemption", "redeem", redemption(cost=4050), revision=1)
    plan = engine.preview(["redeem"])
    result = next(row for row in plan["results"] if row["subject_id"] == "redeem")
    assert result["values"]["investment_income_fen"] == 50
    publish("redeem")
    with engine.store.connection(read_only=True) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT id,digest FROM fact_revision WHERE subject_id='receipt'"
                ).fetchone()
            )
            == funds_before
        )
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE category='bank' AND balance_key='bank'"
            ).fetchone()[0]
            == 4100
        )
    before = balances(engine)
    engine.rebuild_projections(request_id="rebuild-correction")
    assert balances(engine) == before


def test_multiple_lots_and_redemptions_consume_exact_cost_once(book):
    engine, save, publish = book
    save("money_fund_subscription", "buy", subscription())
    save(
        "money_fund_subscription",
        "buy2",
        subscription(purchase_price_fen=900, acquisition_fees_fen=0),
    )
    save("money_fund_redemption", "one", redemption(cost=4000))
    save(
        "money_fund_redemption",
        "two",
        redemption(
            proceeds=7100,
            costs=[
                {"source_kind": "money_fund_subscription", "source_id": "buy", "cost_fen": 6100},
                {"source_kind": "money_fund_subscription", "source_id": "buy2", "cost_fen": 900},
            ],
        ),
    )
    publish("two", "one", "buy2", "buy")
    assert not any(row[0] == "short_term_investment" for row in balances(engine))
    with pytest.raises(KernelError) as exc:
        save("money_fund_subscription", "bad-date", subscription(confirmation_date="2026-02-01"))
    assert exc.value.code


def test_bank_payment_day_is_not_inferred_from_later_fund_confirmation(book):
    engine, save, publish = book
    save("money_fund_subscription", "buy", subscription(confirmation_date="2026-01-19"))
    save("payment", "paid", payment("money_fund_subscription", "buy", 10100))
    publish("buy", "paid")
    # The fund confirms units a day after funds left the bank; both facts survive.
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT amount FROM balance WHERE category='bank' AND balance_key='bank'"
            ).fetchone()[0]
            == -10100
        )
    save(
        "money_fund_redemption",
        "redeem",
        redemption(period="2026-01", confirmation_date="2026-01-18"),
    )
    with pytest.raises(KernelError) as exc:
        engine.preview(["redeem"])
    assert exc.value.code == "redemption_before_acquisition"
