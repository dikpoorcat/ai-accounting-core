"""Semantic order, equal spans and the reporting owner's public read boundary."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.reporting.public import create_reporting

from ._support import assemble, continuation, get, main, opening, request


def test_row_order_and_management_date_are_not_new_accounting_results(tmp_path):
    app, company = assemble(tmp_path)
    initial = opening()
    current = continuation(initial)
    assert app.accounting_apply(request(company, current, initial))["completion"] == "complete"
    original = main(app, company, current)
    current["effective_on"] = (
        "2026-10-17"  # When the source was organized, not a historical journal day.
    )
    for name in ("year_beginning_balance", "prior_profit", "prior_cash"):
        current["data"][name].reverse()
    response = app.accounting_apply(request(company, current, op="replace", seq=1, seed=False))
    assert response.get("completion") == "complete", response
    assert response["accounting"] == "unchanged"
    assert main(app, company, current) == original
    assert get(app, company, current)["fact"] == current


def test_first_quarter_prior_year_and_quarter_are_same_range(tmp_path):
    app, company = assemble(tmp_path, "2026-02")
    initial = opening(period="2026-02")
    current = continuation(initial)
    for name in ("prior_profit", "prior_cash"):
        for row in current["data"][name]:
            row["quarter_to_date_fen"] = row["year_to_date_fen"]
    inconsistent = deepcopy(current)
    for line in (1, 21, 30):
        inconsistent["data"]["prior_profit"][line - 1]["quarter_to_date_fen"] = "200000"
    inconsistent["data"]["prior_profit"][31]["quarter_to_date_fen"] = "190000"
    response = app.accounting_apply(request(company, initial, inconsistent))
    assert response.get("completion") == "not_saved", response
    assert response["error"] == "dependency_conflict"
    response = app.accounting_apply(request(company, initial, current))
    assert response.get("completion") == "complete", response


def zero_continuation(initial):
    current = continuation(initial, carry="0")
    for name in ("year_beginning_balance", "prior_profit", "prior_cash"):
        for row in current["data"][name]:
            for column in row.keys() - {"line"}:
                row[column] = "0"
    return current


def test_explicit_sourced_zero_continuation_is_accepted_without_being_required(tmp_path):
    app, company = assemble(tmp_path)
    initial = opening(value="0", basis="newly_established")
    current = zero_continuation(initial)
    response = app.accounting_apply(request(company, initial, current))
    assert response.get("completion") == "complete", response


@pytest.mark.parametrize("with_continuation", [False, True])
def test_nonzero_new_company_opening_is_valid_but_not_a_zero_comparison_proof(
    tmp_path, with_continuation
):
    app, company = assemble(tmp_path)
    initial = opening(basis="newly_established")
    current = continuation(initial)
    rows = [initial, current] if with_continuation else [initial]
    response = app.accounting_apply(request(company, *rows))
    assert response.get("completion") == "complete", response
    with app.storage.read_unit(company) as read:
        view = app.ledger.pre_tax("2026-01", "2026-09", read)
        if with_continuation:
            assert view.fields["profit_before_tax"] == {"state": "known", "amount_fen": "100000"}
            assert [source["business_id"] for source in view.sources] == [current["business_id"]]
        else:
            assert view.fields["profit_before_tax"]["state"] == "unestablished"
            assert app.ledger.pre_tax("2026-07", "2026-07", read).fields["profit_before_tax"] == {
                "state": "known",
                "amount_fen": "0",
            }


def test_public_query_is_company_family_period_and_identity_scoped(tmp_path):
    app, company = assemble(tmp_path)
    initial = opening()
    current = continuation(initial)
    assert app.accounting_apply(request(company, initial, current))["completion"] == "complete"
    owner = create_reporting()
    query = {
        "company_id": company,
        "family": "reports",
        "period_from": "2026-07",
        "period_to": "2026-07",
        "limit": 500,
    }
    with app.storage.read_unit(company) as read:
        assert owner.query_current(query, read) == (current,)
        assert (
            owner.query_current({**query, "period_from": "2026-08", "period_to": "2026-08"}, read)
            == ()
        )
        assert owner.query_current({**query, "cursor": current["business_id"]}, read) == ()
        assert owner.query_current({**query, "status": "withdrawn"}, read) == ()
        with pytest.raises(ContractError):
            owner.query_current({**query, "company_id": str(uuid4())}, read)
        with pytest.raises(ContractError):
            owner.query_current({**query, "family": "opening"}, read)
        with pytest.raises(ContractError):
            owner.export_current([current["business_id"]] * 2, read)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE report_basis_fact SET schema_key='opening.package'",
        "UPDATE report_basis_fact SET effective_on='2026-07-01'",
        "UPDATE report_basis_fact SET "
        "data_json=json_set(data_json,'$.data.prior_profit[0].line',2)",
    ],
)
def test_corrupt_own_projection_or_body_is_detected_on_public_export(tmp_path, sql):
    app, company = assemble(tmp_path)
    initial = opening()
    current = continuation(initial)
    assert app.accounting_apply(request(company, initial, current))["completion"] == "complete"
    with app.storage.write_unit(company) as uow:
        uow.repository("ledger.reporting")._session.execute(sql, ())
    with app.storage.read_unit(company) as read:
        with pytest.raises(ContractError) as error:
            create_reporting().export_current([current["business_id"]], read)
        assert error.value.code == "storage_corrupt"
