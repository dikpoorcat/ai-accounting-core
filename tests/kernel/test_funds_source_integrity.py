"""Whole-source funds authentication precedes owner summary and pagination."""

import json

import pytest
import test_banking as banking
import test_investments as investments
from test_dashboard_bank_source_proof import closed_banks
from test_dashboard_funds_alignment import _publish_filter_funding
from test_integrity_content import damage

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead
from ai_accounting.kernel.types import canonical, digest

bank_book = banking.book
investment_book = investments.book
MONTH = "2026-09"


def _source_pair(book, closed):
    if closed:
        engine, _, _ = closed_banks(book)
        subjects = ("funding-bank-a", "funding-bank-b")
        expected = 1500
    else:
        engine, save, publish, _ = book
        banking.opening(save, publish)
        banking.funding(save, publish, subject="first", amount=1000)
        banking.funding(save, publish, subject="off-page", amount=2000)
        subjects, expected = ("first", "off-page"), 3000
    response = Dashboard(engine).funds(MONTH, limit=1)["data"]
    assert response["inflow_fen"] == response["total_fen"] == expected
    assert response["collections"]["movements"]["page"]["total_count"] == 2
    assert len(response["collections"]["movements"]["items"]) == 1
    with engine.store.connection(read_only=True) as connection:
        sources = {
            row["subject_id"]: dict(row) for row in connection.execute(
                "SELECT c.* FROM calculation c JOIN calculation_current h ON h.calculation_id=c.id "
                "WHERE h.subject_id IN (?,?)", subjects,
            )
        }
    return engine, sources[subjects[0]], sources[subjects[1]]


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("corruption", [
    "calculation_seal", "fact_seal", "fact_identity", "kind_identity",
    "duplicate_outcome", "body_and_mutable_digest",
])
def test_default_funds_reject_off_page_consumed_source_damage(bank_book, closed, corruption):
    engine, other, source = _source_pair(bank_book, closed)
    if corruption in {"calculation_seal", "fact_seal"}:
        field = "calculation_id" if corruption == "calculation_seal" else "fact_id"
        ident = source["id"] if corruption == "calculation_seal" else source["fact_id"]
        damage(engine, corruption, f"DELETE FROM {corruption} WHERE {field}=?", (ident,),
               foreign_keys=False)
    elif corruption == "fact_identity":
        damage(engine, "calculation", "UPDATE calculation SET fact_id=? WHERE id=?",
               (other["fact_id"], source["id"]))
    elif corruption == "kind_identity":
        damage(engine, "calculation", "UPDATE calculation SET kind='cash_funding' WHERE id=?",
               (source["id"],))
    elif corruption == "duplicate_outcome":
        damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
               ('{"values":{},' + source["outcome"][1:], source["id"]))
    else:
        outcome = json.loads(source["outcome"])
        outcome["balances"][0]["amount"] += 1
        damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
               (canonical(outcome), digest(outcome), source["id"]))
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).funds(MONTH, limit=1)
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("corruption", ["calculation_seal", "fact_seal", "fact_identity"])
def test_funds_authenticate_consumed_opening_state(bank_book, closed, corruption):
    engine, _, _ = _source_pair(bank_book, closed)
    if not closed:
        _, save, publish, _ = bank_book
        banking.opening(save, publish, bank="bank-b")
    with engine.store.connection(read_only=True) as connection:
        sources = {
            row["subject_id"]: dict(row) for row in connection.execute(
                "SELECT c.* FROM calculation c JOIN calculation_current h ON h.calculation_id=c.id "
                "WHERE h.subject_id IN ('opening-bank-a','opening-bank-b')"
            )
        }
    source = sources["opening-bank-a"]
    if corruption == "fact_identity":
        damage(engine, "calculation", "UPDATE calculation SET fact_id=? WHERE id=?",
               (sources["opening-bank-b"]["fact_id"], source["id"]))
    else:
        field = "calculation_id" if corruption == "calculation_seal" else "fact_id"
        ident = source["id"] if corruption == "calculation_seal" else source["fact_id"]
        damage(engine, corruption, f"DELETE FROM {corruption} WHERE {field}=?", (ident,),
               foreign_keys=False)
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).funds(MONTH, section="accounts", limit=1)
    assert failure.value.code == "content_integrity_failed"


def test_current_event_proof_loads_exact_results_as_other_month_history_grows(
    bank_book, monkeypatch
):
    from ai_accounting.kernel import integrity

    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 4)
    dashboard = Dashboard(engine)

    def measured():
        with dashboard._snapshot(MONTH) as snapshot:
            reader = FundsRead(snapshot)
            decoded = []
            original_object = integrity._object

            def observe_object(value, component, ident):
                if component == "calculation":
                    decoded.append((ident, len(value)))
                return original_object(value, component, ident)

            with monkeypatch.context() as patch:
                patch.setattr(integrity, "_object", observe_object)
                sql, parameters = reader.movements()
                assert reader.movements() == (sql, parameters)
            rows = list(snapshot.connection.execute(sql, parameters))
            assert len(rows) == 4 and sum(row["signed_amount"] for row in rows) == 4
            actual = snapshot.reads._verified_source_contents
            assert len(actual) == 4
            assert len(decoded) == 4 and {ident for ident, _ in decoded} == actual.keys()
            assert {item["values"]["actual_date"] for item in actual.values()} == {"2026-09-02"}
            return len(actual), sum(size for _, size in decoded)

    baseline = measured()
    _publish_filter_funding(engine, ["bank-a"] * 40, period="2026-08")
    assert measured() == baseline
    response = dashboard.funds(MONTH, limit=1)["data"]
    assert response["inflow_fen"] == 4 and response["opening_fen"] == 40
    assert response["total_fen"] == 44


@pytest.mark.parametrize("corruption", ["calculation_seal", "fact_identity", "body_and_digest"])
def test_default_twenty_row_page_authenticates_later_summary_sources(bank_book, corruption):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 25)
    dashboard = Dashboard(engine)
    data = dashboard.funds(MONTH)["data"]
    assert data["inflow_fen"] == data["total_fen"] == 25
    assert data["collections"]["movements"]["page"]["total_count"] == 25
    assert len(data["collections"]["movements"]["items"]) == 20
    off_page = "filter-2026-09-0024"
    assert off_page not in {
        item["subject_id"] for item in data["collections"]["movements"]["items"]
    }
    with engine.store.connection(read_only=True) as connection:
        source = dict(connection.execute(
            "SELECT c.* FROM calculation c JOIN calculation_current h ON h.calculation_id=c.id "
            "WHERE h.subject_id=?", (off_page,)
        ).fetchone())
        other_fact = connection.execute(
            "SELECT fact_id FROM fact_current WHERE subject_id='filter-2026-09-0000'"
        ).fetchone()[0]
    if corruption == "calculation_seal":
        damage(engine, "calculation_seal", "DELETE FROM calculation_seal WHERE calculation_id=?",
               (source["id"],), foreign_keys=False)
    elif corruption == "fact_identity":
        damage(engine, "calculation", "UPDATE calculation SET fact_id=? WHERE id=?",
               (other_fact, source["id"]))
    else:
        outcome = json.loads(source["outcome"])
        outcome["balances"][0]["amount"] += 1
        damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
               (canonical(outcome), digest(outcome), source["id"]))
    with pytest.raises(KernelError) as failure:
        dashboard.funds(MONTH)
    assert failure.value.code == "content_integrity_failed"


def _reviewed_closed_investment(book, monkeypatch):
    from ai_accounting.kernel import engine as engine_module

    engine, save, publish = book
    save("cash_funding", "capital", {
        "period": "2026-01", "owner_id": "owner", "funding_kind": "capital",
        "amount_fen": 12000, "actual_date": "2026-01-02", "cash_account_id": "cash",
    })
    save("money_fund_subscription", "buy", investments.subscription())
    publish("capital", "buy")
    with engine.store.connection(read_only=True) as connection:
        original = dict(connection.execute(
            "SELECT c.* FROM calculation c JOIN calculation_current h ON h.calculation_id=c.id "
            "WHERE h.subject_id='buy'"
        ).fetchone())
    payment = investments.payment("money_fund_subscription", "buy", 10100)
    payment.pop("bank_account_id")
    payment["cash_account_id"] = "cash"
    save("cash_payment", "paid", payment)
    publish("paid")
    with monkeypatch.context() as scope:
        scope.setattr(engine_module, "PROGRAM_VERSION", "synthetic-pre-close-fund-review")
        reviewed = publish("buy")
    review = next(item for item in reviewed["results"] if item["subject_id"] == "buy")
    assert review["impact"] == "review_no_impact"
    assert review["calculation_id"] != original["id"]
    investments.close(engine, "2026-01")
    data = Dashboard(engine).funds("2026-01")["data"]
    assert data["inflow_fen"] == 12000 and data["outflow_fen"] == 10100
    assert data["investments"]["closing_cost_fen"] == 10100
    assert data["investments"]["actual_payments_fen"] == 10100
    assert data["investments"]["event_count"] == 2
    return engine, original


def test_frozen_fund_voucher_keeps_its_real_original_owner_after_no_impact_review(
    investment_book, monkeypatch
):
    engine, original = _reviewed_closed_investment(investment_book, monkeypatch)
    with Dashboard(engine)._snapshot("2026-01") as snapshot:
        reader = FundsRead(snapshot)
        reader.account_summary()
        reader.investment_summary()
        assert original["id"] in snapshot.reads._verified_source_contents
        assert original["id"] in snapshot.reads._verified_saved_input_identities


def test_frozen_original_fund_owner_body_cannot_follow_its_mutable_digest(
    investment_book, monkeypatch
):
    engine, original = _reviewed_closed_investment(investment_book, monkeypatch)
    outcome = json.loads(original["outcome"])
    outcome["values"]["fund_id"] = "synthetic-wrong-fund"
    damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
           (canonical(outcome), digest(outcome), original["id"]))
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).funds("2026-01")
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("subject,field,value", [
    ("opening-bank-a", "bank_account_id", "synthetic-wrong-bank"),
    ("statement", "bank_account_id", "synthetic-wrong-bank"),
    ("reconciliation", "balanced", False),
    ("reconciliation", "statement_id", "synthetic-wrong-statement"),
])
def test_open_zero_amount_states_cannot_change_consumed_values_with_self_digest(
    bank_book, subject, field, value
):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    banking.funding(save, publish)
    banking.statement(save, publish, [banking.entry()])
    banking.reconciliation(save, publish, [banking.match()])
    baseline = Dashboard(engine).funds(MONTH)["data"]
    assert baseline["total_fen"] == 1000
    assert baseline["bank_statement"]["coverage_state"] == "complete"
    with engine.store.connection(read_only=True) as connection:
        source = dict(connection.execute(
            "SELECT c.* FROM calculation c JOIN calculation_current h ON h.calculation_id=c.id "
            "WHERE h.subject_id=?", (subject,)
        ).fetchone())
    outcome = json.loads(source["outcome"])
    assert not outcome["lines"]
    assert field in outcome["values"] and outcome["values"][field] != value
    outcome["values"][field] = value
    damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
           (canonical(outcome), digest(outcome), source["id"]))
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).funds(MONTH)
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("corruption", [
    None, "body_and_digest", "calculation_seal", "kind_identity",
])
def test_later_settlement_authenticates_its_exact_post_close_review_source(
    investment_book, monkeypatch, corruption
):
    from ai_accounting.kernel import engine as engine_module

    engine, save, publish = investment_book
    save("money_fund_subscription", "buy", investments.subscription())
    publish("buy")
    with engine.store.connection(read_only=True) as connection:
        original = dict(connection.execute(
            "SELECT c.* FROM calculation c JOIN calculation_current h ON h.calculation_id=c.id "
            "WHERE h.subject_id='buy'"
        ).fetchone())
    investments.close(engine, "2026-01")
    with monkeypatch.context() as scope:
        scope.setattr(engine_module, "PROGRAM_VERSION", "synthetic-after-close-review")
        reviewed = publish("buy")
    review = next(item for item in reviewed["results"] if item["subject_id"] == "buy")
    assert review["impact"] == "review_no_impact" and review["calculation_id"] != original["id"]
    save("cash_funding", "capital", {
        "period": "2026-02", "owner_id": "owner", "funding_kind": "capital",
        "amount_fen": 12000, "actual_date": "2026-02-02", "cash_account_id": "cash",
    })
    payment = investments.payment("money_fund_subscription", "buy", 10100, period="2026-02")
    payment.pop("bank_account_id")
    payment["cash_account_id"] = "cash"
    save("cash_payment", "paid", payment)
    publish("capital", "paid")
    with engine.store.connection(read_only=True) as connection:
        payment_outcome = json.loads(connection.execute(
            "SELECT c.outcome FROM calculation c JOIN calculation_current h "
            "ON h.calculation_id=c.id "
            "WHERE h.subject_id='paid'"
        ).fetchone()[0])
    source_id = payment_outcome["values"]["settlements"][0]["source_calculation"]
    assert source_id == review["calculation_id"]
    with engine.store.connection(read_only=True) as connection:
        source = dict(connection.execute(
            "SELECT * FROM calculation WHERE id=?", (source_id,)
        ).fetchone())

    def events():
        with Dashboard(engine)._snapshot("2026-02") as snapshot:
            query, parameters = FundsRead(snapshot).investment_events()
            rows = list(snapshot.connection.execute(query, parameters))
            assert len(rows) == 1 and rows[0]["settlement_fen"] == 10100
            assert rows[0]["fund_id"] == "fund-A"
            assert source_id in snapshot.reads._verified_saved_input_identities
            assert original["id"] not in snapshot.reads._verified_source_contents

    events()
    summary = Dashboard(engine).funds("2026-02")["data"]
    assert summary["investments"]["actual_payments_fen"] == 10100
    if corruption is None:
        return
    if corruption == "calculation_seal":
        damage(engine, "calculation_seal", "DELETE FROM calculation_seal WHERE calculation_id=?",
               (source_id,), foreign_keys=False)
    elif corruption == "kind_identity":
        damage(engine, "calculation", "UPDATE calculation SET kind='expense' WHERE id=?",
               (source_id,))
    else:
        outcome = json.loads(source["outcome"])
        outcome["values"]["fund_id"] = "synthetic-wrong-fund"
        damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
               (canonical(outcome), digest(outcome), source_id))
    with pytest.raises(KernelError) as failure:
        Dashboard(engine).funds("2026-02")
    assert failure.value.code == "content_integrity_failed"
    with pytest.raises(KernelError) as failure:
        events()
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("corruption", [None, "body_and_digest"])
def test_current_reversal_uses_original_month_frozen_source_anchor(bank_book, corruption):
    engine, save, publish, proof = bank_book
    fields = {
        "period": MONTH, "actual_date": "2026-09-03", "cash_account_id": "cash",
        "amount_fen": 1000,
    }
    save("managed_reserve_refund", "receipt", fields)
    publish("receipt")
    with engine.store.connection(read_only=True) as connection:
        original = dict(connection.execute(
            "SELECT c.* FROM calculation c JOIN calculation_current h ON h.calculation_id=c.id "
            "WHERE h.subject_id='receipt'"
        ).fetchone())
    investments.close(engine, MONTH)
    engine.amend_fact(
        "managed_reserve_refund", "receipt", fields | {"amount_fen": 700}, evidence=(proof,),
        expected_revision=1, recording_error_confirmed=True, request_id="amend-receipt",
    )
    preview = engine.preview(["receipt"], posting_period="2026-10")
    engine.confirm(
        ["receipt"], preview_digest=preview["digest"], epochs=preview["epochs"],
        posting_period="2026-10", request_id="receipt-correction",
    )

    def movements():
        with Dashboard(engine)._snapshot("2026-10") as snapshot:
            query, parameters = FundsRead(snapshot).movements()
            rows = list(snapshot.connection.execute(query, parameters))
            assert len(rows) == 2 and sum(row["signed_amount"] for row in rows) == -300
            reversal = next(row for row in rows if row["sign"] == -1)
            assert reversal["calculation_id"] == original["id"]

    movements()
    if corruption is None:
        return
    outcome = json.loads(original["outcome"])
    outcome["values"]["actual_date"] = "2026-09-04"
    damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
           (canonical(outcome), digest(outcome), original["id"]))
    with pytest.raises(KernelError) as failure:
        movements()
    assert failure.value.code == "content_integrity_failed"
