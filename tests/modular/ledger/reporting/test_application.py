"""Real report continuation, opening, bookkeeping and cumulative tax adoption."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, EvaluatedSlot, canonical_json
from ai_accounting_modular.ledger.public import LedgerQuery
from ai_accounting_modular.ledger.reporting.public import create_reporting

from ._support import (
    BANK,
    CARRY_SOURCE,
    SOURCE,
    assemble,
    continuation,
    expense,
    get,
    main,
    opening,
    request,
    uid,
    withdraw,
)


@pytest.fixture
def assembled(tmp_path):
    return assemble(tmp_path)


def known(app, company, start="2026-01", end="2026-09"):
    with app.storage.read_unit(company) as read:
        return app.ledger.pre_tax(start, end, read)


def profit_value(row, value):
    for item in row["data"]["prior_profit"]:
        if item["line"] in (1, 21, 30):
            item["year_to_date_fen"] = value
        elif item["line"] == 32:
            item["year_to_date_fen"] = str(int(value) - 10000)


def test_real_continuation_persists_complete_fact_no_history_money_or_tax_sales(assembled):
    app, company = assembled
    initial = opening()
    current = continuation(initial, carry="7000")
    submitted = request(company, current, initial)  # Ordering is declared, never positional.
    response = app.accounting_apply(submitted)
    assert response.get("completion") == "complete", response
    adopted = main(app, company, current)
    assert adopted["vouchers"] == []
    assert adopted["target"]["values"] == {
        "bookkeeping_start": "2026-07",
        "accounting_standard": "small_enterprise",
        "balance_row_count": 53,
        "profit_row_count": 32,
        "cash_row_count": 22,
    }
    assert adopted["basis"]["input"]["prior_profit"][30]["year_to_date_fen"] == "10000"
    assert adopted["basis"]["adopted_quantities"] == [
        {
            "type": "income_tax_cumulative",
            "tax_year": 2026,
            "period_to": "2026-06",
            "cumulative_tax_fen": "7000",
        }
    ]
    assert adopted["basis"]["source_results"][0]["business_id"] == initial["business_id"]
    assert "source" not in adopted["basis"]["input"]["income_tax_carry"]
    assert {item["source_id"] for item in adopted["basis"]["source_locations"]} == {
        SOURCE,
        CARRY_SOURCE,
    }
    with app.storage.read_unit(company) as read:
        assert create_reporting().export_current([current["business_id"]], read) == (current,)
        saved = read.repository("ledger.reporting")._session.fetchone(
            "SELECT data_json,main_object_id FROM report_basis_fact WHERE business_id=?",
            (current["business_id"],),
        )
        assert saved["data_json"] == canonical_json(current)
        assert saved["main_object_id"] is None
        assert app.ledger.query_obligations(LedgerQuery((current["business_id"],)), read).total == 0
        assert app.ledger.fund_movements(BANK, "2026-01", "2026-12", read) == ()
        assert app.ledger.tax_sales("2026-01", "2026-12", read) == ()
        first_head = read.head
    retried = app.accounting_apply(submitted)
    assert retried["accounting"] == "unchanged", retried
    with app.storage.read_unit(company) as read:
        assert read.head == first_head
    assert main(app, company, current) == adopted
    view = known(app, company)
    assert view.fields["profit_before_tax"] == {"state": "known", "amount_fen": "100000"}
    assert [source["business_id"] for source in view.sources] == [current["business_id"]]


def test_explicit_new_company_zero_and_missing_comparisons_do_not_block_expense(tmp_path):
    for name, basis, expected in (
        ("new", "newly_established", "known"),
        ("existing", "existing_ledger", "unestablished"),
    ):
        app, company = assemble(tmp_path / name)
        initial = opening(value="0", basis=basis)
        business = expense()
        response = app.accounting_apply(request(company, initial, business))
        assert response.get("completion") == "complete", response
        assert main(app, company, business)["vouchers"]
        view = known(app, company)
        assert view.fields["profit_before_tax"]["state"] == expected
        if basis == "newly_established":
            assert view.fields["profit_before_tax"]["amount_fen"] == "-20000"
        else:
            assert view.issues[0]["blocking_scope"] == "none"
        with app.storage.read_unit(company) as read:
            current = app.ledger.pre_tax("2026-07", "2026-07", read)
            assert current.fields["profit_before_tax"]["amount_fen"] == "-20000"


def test_mid_quarter_prior_is_selected_once_and_year_beginning_is_not_quarter_cash(tmp_path):
    app, company = assemble(tmp_path, "2026-05")
    initial = opening(period="2026-05")
    current = continuation(initial)
    assert current["data"]["prior_cash"][20] == {
        "line": 21,
        "quarter_to_date_fen": "450000",
        "year_to_date_fen": "300000",
    }
    may, june = expense(period="2026-05"), expense("30000", period="2026-06")
    response = app.accounting_apply(request(company, current, may, june, initial))
    assert response.get("completion") == "complete", response
    quarter = known(app, company, "2026-04", "2026-06")
    year = known(app, company, "2026-01", "2026-06")
    assert quarter.fields["profit_before_tax"]["amount_fen"] == "30000"
    assert year.fields["profit_before_tax"]["amount_fen"] == "250000"
    saved = main(app, company, current)["basis"]["input"]
    assert saved["year_beginning_balance"][0]["beginning_fen"] == "300000"
    assert saved["prior_cash"][20]["quarter_to_date_fen"] == "450000"
    assert (
        known(app, company, "2026-07", "2026-09").fields["profit_before_tax"]["amount_fen"] == "0"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_balance",
        "duplicate_profit",
        "duplicate_cash",
        "bs_formula",
        "bs_balance",
        "pl_formula",
        "cash_formula",
        "year_cash",
        "opening_cash",
        "wrong_start",
        "wrong_period",
        "quarter_flow",
        "carry_year",
        "carry_period",
        "missing_source",
        "missing_carry_source",
    ],
)
def test_wrong_continuation_is_rejected_atomically_before_any_current_data(assembled, mutation):
    app, company = assembled
    initial = opening()
    current = continuation(initial, carry="7000")
    data = current["data"]
    if mutation.startswith("duplicate_"):
        name = {
            "duplicate_balance": "year_beginning_balance",
            "duplicate_profit": "prior_profit",
            "duplicate_cash": "prior_cash",
        }[mutation]
        data[name][1]["line"] = 1
    elif mutation == "bs_formula":
        data["year_beginning_balance"][17]["beginning_fen"] = "1"
    elif mutation == "bs_balance":
        for line in (51, 52, 53):
            data["year_beginning_balance"][line - 1]["beginning_fen"] = "299999"
    elif mutation == "pl_formula":
        data["prior_profit"][29]["year_to_date_fen"] = "99999"
    elif mutation == "cash_formula":
        data["prior_cash"][12]["year_to_date_fen"] = "1"
    elif mutation == "year_cash":
        # Keep CF arithmetic correct while misidentifying annual beginning as quarter beginning.
        for line in (1, 7, 20):
            data["prior_cash"][line - 1]["year_to_date_fen"] = "0"
        data["prior_cash"][20]["year_to_date_fen"] = "500000"
    elif mutation == "opening_cash":
        for member in initial["data"]["members"]:
            member["data"]["balance_fen"] = "600000"
    elif mutation == "wrong_start":
        data["bookkeeping_start"] = "2026-08"
    elif mutation == "wrong_period":
        current["period"] = "2026-08"
    elif mutation == "quarter_flow":
        for line in (1, 21, 30, 32):
            data["prior_profit"][line - 1]["quarter_to_date_fen"] = "1"
    elif mutation == "carry_year":
        data["income_tax_carry"]["tax_year"] = 2025
    elif mutation == "carry_period":
        data["income_tax_carry"]["period_to"] = "2026-05"
    elif mutation == "missing_source":
        current["evidence_ids"] = []
    else:
        data["income_tax_carry"]["source"]["source_id"] = uid("unregistered")
    response = app.accounting_apply(request(company, initial, current))
    assert response.get("completion") == "not_saved", response
    if mutation == "missing_carry_source":
        assert response["error"] == "invalid_input", response
        assert response["fact_issues"][0]["code"] == "source_not_found"
        assert response["fact_issues"][0]["field"].endswith("/income_tax_carry/source/source_id")
    else:
        assert response["error"] in {"dependency_conflict", "needs_information"}, response
    assert response.get("fact_issues"), response
    with app.storage.read_unit(company) as read:
        assert read.head == 0
        assert create_reporting().export_current([current["business_id"]], read) == ()
        assert app.records.resolve_sources([SOURCE], read) == ()


def test_nonopening_or_missing_adopted_reference_cannot_supply_continuation(assembled):
    app, company = assembled
    initial = opening()
    current = continuation(initial)
    missing = app.accounting_apply(request(company, current))
    assert missing.get("completion") == "not_saved", missing
    unrelated = expense()
    current["data"]["opening_package_id"] = unrelated["business_id"]
    wrong = app.accounting_apply(request(company, current, unrelated))
    assert wrong.get("completion") == "not_saved", wrong
    assert wrong["error"] == "dependency_conflict"


def test_correction_withdraw_restore_unique_and_failed_replacement_keep_only_final(assembled):
    app, company = assembled
    initial = opening()
    current = continuation(initial, carry="7000")
    assert app.accounting_apply(request(company, initial, current))["completion"] == "complete"
    before = main(app, company, current)
    duplicate = deepcopy(current)
    duplicate["business_id"] = str(uuid4())
    rejected = app.accounting_apply(request(company, duplicate, seed=False))
    assert rejected["error"] == "conflict", rejected
    profit_value(current, "200000")
    updated = app.accounting_apply(request(company, current, op="replace", seq=1, seed=False))
    assert updated.get("completion") == "complete", updated
    after = main(app, company, current)
    assert after["reference"]["result_id"] == before["reference"]["result_id"]
    assert after["reference"]["generation"] == before["reference"]["generation"] + 1
    assert known(app, company).fields["profit_before_tax"]["amount_fen"] == "200000"
    bad = deepcopy(current)
    bad["data"]["prior_cash"][21]["year_to_date_fen"] = "0"
    assert (
        app.accounting_apply(request(company, bad, op="replace", seq=2, seed=False))["completion"]
        == "not_saved"
    )
    assert main(app, company, current) == after
    assert get(app, company, current)["fact"] == current
    withdrawn = app.accounting_apply(withdraw(company, current, seq=2))
    assert withdrawn.get("completion") == "complete", withdrawn
    assert known(app, company).fields["profit_before_tax"]["state"] == "unestablished"
    with app.storage.read_unit(company) as read:
        assert create_reporting().export_current([current["business_id"]], read) == ()
    restored = app.accounting_apply(request(company, current, op="restore", seq=3, seed=False))
    assert restored.get("completion") == "complete", restored
    assert known(app, company).fields["profit_before_tax"]["amount_fen"] == "200000"


def test_changed_opening_cash_rechecks_existing_continuation_in_same_atomic_unit(assembled):
    app, company = assembled
    initial = opening()
    current = continuation(initial)
    assert app.accounting_apply(request(company, initial, current))["completion"] == "complete"
    original = main(app, company, initial)
    for member in initial["data"]["members"]:
        member["data"]["balance_fen"] = "600000"
    rejected = app.accounting_apply(request(company, initial, op="replace", seq=1, seed=False))
    assert rejected.get("completion") == "not_saved", rejected
    assert main(app, company, initial) == original
    for line in (1, 7, 20):
        current["data"]["prior_cash"][line - 1]["year_to_date_fen"] = "300000"
    current["data"]["prior_cash"][21]["year_to_date_fen"] = "600000"
    for line in (21, 22):
        current["data"]["prior_cash"][line - 1]["quarter_to_date_fen"] = "600000"
    accepted = app.accounting_apply(
        request(company, current, initial, op="replace", seq=1, seed=False)
    )
    assert accepted.get("completion") == "complete", accepted


def test_owner_resolves_carry_source_independently_of_application_precheck(assembled):
    app, company = assembled
    initial = opening()
    assert app.accounting_apply(request(company, initial))["completion"] == "complete"
    adopted = main(app, company, initial)
    current = continuation(initial, carry="7000")
    calls = []

    def resolve(ids):
        calls.append(tuple(ids))
        if CARRY_SOURCE in ids:
            raise ContractError(
                "needs_information",
                "独立来源缺失",
                fact_issues=[
                    {
                        "code": "missing_source",
                        "field": "/data/income_tax_carry/source",
                        "meaning": "累计税依据",
                        "message": "独立来源缺失",
                        "allowed_precision": ["source"],
                        "reusable_sources": [],
                        "blocking_scope": "business",
                    }
                ],
            )
        return [{"source_id": identity} for identity in ids]

    with app.storage.read_unit(company) as read:
        source = app.ledger.get_result_source(initial["business_id"], "main", read)
    # Prove the owner invokes the resolver independently of application prechecks.
    context = SimpleNamespace(
        read_context=SimpleNamespace(start_period="2026-07"),
        get_fact=lambda _: current,
        get_result=lambda *_: EvaluatedSlot(source, adopted["target"], adopted["basis"]),
        resolve_sources=resolve,
    )
    with pytest.raises(ContractError, match="独立来源缺失"):
        create_reporting().evaluate_slot(current["business_id"], "main", context)
    assert calls == [(SOURCE,), (CARRY_SOURCE,)]
