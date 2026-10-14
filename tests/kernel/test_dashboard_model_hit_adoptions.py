"""Returned posting effects retain exact adoption and physical voucher duties."""

import pytest
from dashboard_model_fixture import produce
from opening_fixture import adopt_empty_opening
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import activate
from test_asset_batches import asset_engine as asset_engine
from test_asset_owner_frozen_scope import amend_activation
from test_banking import book as book
from test_banking import funding, opening
from test_dashboard_funds_alignment import _publish_filter_funding
from test_dashboard_model_transactions import raw_read, source_body_work
from test_integrity_content import damage
from test_investments import book as _investment_book
from test_investments import payment, subscription
from test_labor_assets import cost
from test_open_correction_publication_heads import _remove_reverse
from test_payroll_corrections import Company
from test_payroll_corrections import company as company
from test_publication_periods import company as _position_company
from test_publication_periods import publish as position_publish
from test_publication_periods import save as position_save
from test_reports import book as report_book
from test_reports import profile

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_model_runtime import stable_payload
from ai_accounting.kernel.dashboard_model_sources import verify_model_sources
from ai_accounting.kernel.domains.labor_assets import LaborProjectCost
from ai_accounting.kernel.domains.payroll import LaborAccrual
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.types import YearMonth

investment_book, position_company = _investment_book, _position_company


def _reject(operation):
    with pytest.raises(KernelError) as failure:
        operation()
    assert failure.value.code == "content_integrity_failed"


def _model_funds(engine, period, monkeypatch):
    import ai_accounting.kernel.dashboard as dashboard_module

    def no_fallback(*_args, **_kwargs):
        pytest.fail("the model-hit assertion fell back to the native funds reader")

    with monkeypatch.context() as guard:
        guard.setattr(dashboard_module, "_funds", no_fallback)
        return Dashboard(engine).funds(period, movement_account_selection="first")


def _source(engine, subject):
    with engine.store.connection(read_only=True) as connection:
        return connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id=?", (subject,),
        ).fetchone()[0]


def _verify_source(engine, ident, period):
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        return verify_model_sources(
            engine, connection, calculation_ids=[ident],
            through_period=YearMonth(period).ordinal,
        )


def test_correction_only_and_replacement_only_hits_require_the_independent_reverse(tmp_path):
    engine, save, publish, close = report_book.__wrapped__(tmp_path)
    profile(save, publish)
    data = {
        "period": "2026-01", "actual_date": "2026-01-03", "owner_id": "owner",
        "funding_kind": "capital", "amount_fen": 100, "cash_account_id": "cash",
    }
    save("cash_funding", "capital", data)
    publish("capital")
    old = _source(engine, "capital")
    close("2026-01")
    with engine.store.connection(read_only=True) as connection:
        proof = connection.execute("SELECT digest FROM evidence ORDER BY rowid LIMIT 1")
        proof = proof.fetchone()[0].hex()
    engine.amend_fact(
        "cash_funding", "capital", {**data, "amount_fen": 120}, evidence=(proof,),
        expected_revision=1, recording_error_confirmed=True, request_id="correct-capital",
    )
    publish("capital", posting_period="2026-03")
    dashboard = Dashboard(engine)
    first = dashboard.funds("2026-03", section="movements", limit=1)
    collection = first["data"]["collections"]["movements"]
    assert collection["items"][0]["correction"] is True
    assert collection["items"][0]["id"].split(":")[1] == old
    options = {
        "section": "movements", "limit": 1, "cursor": collection["page"]["next_cursor"],
        "expected_version": first["snapshot_version"],
    }
    second = dashboard.funds("2026-03", **options)
    replacement = second["data"]["collections"]["movements"]["items"]
    assert len(replacement) == 1 and replacement[0]["signed_amount_fen"] == 120
    assert replacement[0]["correction"] is False
    for arguments in ({"section": "movements", "limit": 1}, options):
        with raw_read():
            expected = dashboard.funds("2026-03", **arguments)
        assert stable_payload(dashboard.funds("2026-03", **arguments)) == stable_payload(expected)
    # The frozen original is still its own basis, even after a new current head.
    assert set(_verify_source(engine, old, "2026-01")) == {old}
    _remove_reverse(engine)
    for arguments in ({}, {"section": "movements", "limit": 1}, options):
        _reject(lambda arguments=arguments: dashboard.funds("2026-03", **arguments))


@pytest.mark.parametrize("target", ["publication", "current", "voucher_current", "lines"])
def test_actual_funds_hit_cannot_outlive_its_posting_proof(book, monkeypatch, target):
    engine, save, publish, _ = book
    opening(save, publish)
    funding(save, publish, subject="shown", amount=17)
    produce(engine, "2026-09")
    ident = _source(engine, "shown")
    assert _model_funds(engine, "2026-09", monkeypatch)["data"]["net_change_fen"] == 17
    if target == "publication":
        damage(engine, "calculation_publication",
               "DELETE FROM calculation_publication WHERE calculation_id=?", (ident,),
               foreign_keys=False)
    elif target == "current":
        damage(engine, "calculation_current",
               "DELETE FROM calculation_current WHERE calculation_id=?", (ident,))
    elif target == "voucher_current":
        damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE version_id IN("
               "SELECT id FROM voucher_version WHERE calculation_id=?)", (ident,))
    else:
        damage(engine, "voucher_line", "UPDATE voucher_line SET credit=credit+1 "
               "WHERE credit>0 AND version_id IN(SELECT id FROM voucher_version "
               "WHERE calculation_id=?)", (ident,))
    _reject(lambda: _model_funds(engine, "2026-09", monkeypatch))


def test_zero_publication_and_future_unrelated_head_keep_their_actual_cutoff(book):
    engine, save, publish, _ = book
    opening(save, publish)
    zero = _source(engine, "opening-bank-a")
    assert set(_verify_source(engine, zero, "2026-09")) == {zero}
    funding(save, publish, subject="future", amount=7, day="2026-10-03")
    future = _source(engine, "future")
    damage(engine, "calculation_current", "DELETE FROM calculation_current WHERE calculation_id=?",
           (future,))
    assert set(_verify_source(engine, future, "2026-09")) == {future}
    _reject(lambda: _verify_source(engine, future, "2026-10"))
    assert set(_verify_source(engine, zero, "2026-09")) == {zero}


def test_hit_adoption_work_does_not_expand_with_unreturned_postings(book, monkeypatch):
    engine = book[0]
    _publish_filter_funding(engine, ["bank-a"] * 3)
    produce(engine, "2026-09")

    def read():
        with source_body_work(monkeypatch) as work:
            response = _model_funds(engine, "2026-09", monkeypatch)
        assert len(work["outcomes"]) == len(work["facts"]) == 3
        assert work["lineages"] == []
        return response["data"]["collections"]["movements"]["items"], work

    before = read()
    _publish_filter_funding(engine, ["bank-a"] * 15, period="2026-08")
    produce(engine, "2026-09")
    assert read() == before


@pytest.mark.parametrize("target", [
    "owner_publication", "member_current", "owner_current", "owner_period",
])
def test_asset_member_hit_proves_its_exact_current_owner(asset_engine, target):
    engine, proof = asset_engine
    activate(engine, proof)
    Maintenance(engine).rebuild_dashboard_read_model("2026-01", request_id="member-model")
    produce(engine, "2026-01")
    assert len(Dashboard(engine).assets("2026-01")["data"]["collections"]["assets"]["items"]) == 2
    if target == "owner_publication":
        damage(engine, "calculation_publication", "DELETE FROM calculation_publication "
               "WHERE subject_id='activation-batch'", foreign_keys=False)
    elif target == "owner_period":
        damage(engine, "calculation", "UPDATE calculation SET period=period+1 "
               "WHERE subject_id='activation-batch'")
    else:
        subject = "activate-fixed-a" if target == "member_current" else "activation-batch"
        damage(engine, "calculation_current", "DELETE FROM calculation_current WHERE subject_id=?",
               (subject,))
    _reject(lambda: Dashboard(engine).assets("2026-01"))


def test_closed_asset_member_keeps_its_frozen_basis_after_a_new_owner_review(tmp_path):
    company = Company(tmp_path / "frozen-member.sqlite", opening_period="2026-02")
    prepare_batch_assets(company)
    engine = company.engine
    old = _source(engine, "activate-computer")
    company.close("2026-02")
    amend_activation(
        company, company.owner_confirmation, revision=1,
        posting_period="2026-02", benefit_area="administration",
    )
    assert _source(engine, "activate-computer") != old
    assert set(_verify_source(engine, old, "2026-02")) == {old}
    with raw_read():
        expected = Dashboard(engine).assets("2026-02")
    assert stable_payload(Dashboard(engine).assets("2026-02")) == stable_payload(expected)


def test_investment_confirmation_hit_keeps_physical_voucher_without_brief_card_fields(
    investment_book, monkeypatch,
):
    engine, save, publish = investment_book
    adopt_empty_opening(engine)
    save("money_fund_subscription", "buy", subscription())
    publish("buy")
    produce(engine, "2026-01")
    dashboard = Dashboard(engine)
    options = {"movement_account_selection": "first"}
    actual = _model_funds(engine, "2026-01", monkeypatch)
    items = actual["data"]["collections"]["investment_events"]["items"]
    assert len(items) == 1 and items[0]["id"].endswith(":confirmation")
    assert "voucher_version_id" not in items[0] and "lines" not in items[0]
    with raw_read():
        expected = dashboard.funds("2026-01", **options)
    assert stable_payload(actual) == stable_payload(expected)
    ident = _source(engine, "buy")
    damage(engine, "voucher_line", "UPDATE voucher_line SET credit=credit+1 "
           "WHERE credit>0 AND version_id IN(SELECT id FROM voucher_version "
           "WHERE calculation_id=?)", (ident,))
    _reject(lambda: _model_funds(engine, "2026-01", monkeypatch))


@pytest.mark.parametrize("target", ["publication", "current"])
def test_displayed_monthly_payroll_rejects_missing_current_adoption(company, target):
    company.publish("january", "february")
    engine = company.engine
    before = Dashboard(engine).employees("2026-02", employee_filter="all")
    assert before["data"]["collections"]["employees"]["items"]
    ident = _source(engine, "february")
    table = "calculation_publication" if target == "publication" else "calculation_current"
    damage(engine, table, f"DELETE FROM {table} WHERE calculation_id=?", (ident,),
           foreign_keys=False)
    _reject(lambda: Dashboard(engine).employees("2026-02", employee_filter="all"))


@pytest.mark.parametrize("kind", ["labor", "project"])
def test_displayed_labor_and_project_sources_require_their_actual_publication(tmp_path, kind):
    company = Company(tmp_path / "source.sqlite")
    if kind == "labor":
        fact = LaborAccrual(
            period="2026-01", person_id="person", expense_class="management",
            gross_fee_fen=500000, tax_treatment="not_withheld_not_filed",
        )
        period, page, section = "2026-01", "employees", "labor_sources"
    else:
        fact = LaborProjectCost.model_validate(cost())
        period, page, section = "2026-11", "assets", "projects"
    company.save(fact, "shown")
    company.publish("shown")
    operation = getattr(Dashboard(company.engine), page)
    options = {"section": section}
    if page == "employees":
        options["employee_filter"] = "all"
    assert operation(period, **options)["data"]["collections"][section]["items"]
    damage(company.engine, "calculation_publication", "DELETE FROM calculation_publication "
           "WHERE subject_id='shown'", foreign_keys=False)
    _reject(lambda: operation(period, **options))


@pytest.mark.parametrize("amount", [0, 100])
def test_no_impact_review_adopts_the_existing_zero_or_nonzero_head(position_company, amount):
    position_save(position_company, amount)
    position_publish(position_company, "first")
    engine = position_company[0]
    old = _source(engine, "position")
    position_save(position_company, amount, 1)
    position_publish(position_company, "review")
    current = _source(engine, "position")
    assert old != current
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT mode FROM calculation_publication "
                                  "WHERE calculation_id=?", (current,)).fetchone()[0] == (
            "review_no_impact"
        )
    assert set(_verify_source(engine, current, "2026-01")) == {current}
    assert set(_verify_source(engine, old, "2026-01")) == {old}


def test_obsolete_open_source_cannot_claim_current_by_pointing_back_past_its_successor(
    position_company,
):
    position_save(position_company, 100)
    position_publish(position_company, "first")
    engine = position_company[0]
    old = _source(engine, "position")
    position_save(position_company, 120, 1)
    position_publish(position_company, "replacement")
    assert set(_verify_source(engine, old, "2026-01")) == {old}
    damage(engine, "calculation_current", "UPDATE calculation_current SET calculation_id=? "
           "WHERE subject_id='position'", (old,))
    _reject(lambda: _verify_source(engine, old, "2026-01"))


def test_real_withdrawal_removes_movement_but_retains_the_saved_historical_source(book):
    engine, save, publish, proof = book
    opening(save, publish)
    funding(save, publish, subject="withdraw", amount=17)
    old = _source(engine, "withdraw")
    preview = engine.preview_delete("withdraw", recording_error_evidence=proof)
    engine.delete(
        "withdraw", recording_error_evidence=proof, preview_digest=preview["digest"],
        epochs=preview["epochs"], request_id="withdraw-actual-posting",
    )
    response = Dashboard(engine).funds("2026-09", section="movements")
    assert response["data"]["collections"]["movements"]["items"] == []
    assert response["data"]["total_fen"] == response["data"]["net_change_fen"] == 0
    assert set(_verify_source(engine, old, "2026-09")) == {old}


def test_investment_settlement_binds_its_payment_voucher_and_exact_source(investment_book):
    engine, save, publish = investment_book
    adopt_empty_opening(engine)
    save("funding", "capital", {
        "period": "2026-01", "owner_id": "owner", "amount_fen": 20000,
        "funding_kind": "capital", "actual_date": "2026-01-01", "bank_account_id": "bank",
    })
    save("money_fund_subscription", "buy", subscription())
    publish("capital", "buy")
    save("payment", "paid", payment("money_fund_subscription", "buy", 10100))
    publish("paid")
    dashboard = Dashboard(engine)
    options = {"section": "investment_events", "limit": 20}
    actual = dashboard.funds("2026-01", **options)
    items = actual["data"]["collections"]["investment_events"]["items"]
    assert len(items) == 2 and any(":settlement:" in item["id"] for item in items)
    with raw_read():
        expected = dashboard.funds("2026-01", **options)
    assert stable_payload(actual) == stable_payload(expected)
    ident = _source(engine, "paid")
    damage(engine, "voucher_current", "DELETE FROM voucher_current WHERE version_id IN("
           "SELECT id FROM voucher_version WHERE calculation_id=?)", (ident,))
    _reject(lambda: dashboard.funds("2026-01", **options))
