"""The VAT producer owns exemption income; whole-return rounding is not its proxy."""

from contextlib import nullcontext
from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, content_digest
from ai_accounting_modular.domains.taxes.public import Taxes
from tests.modular._projection_blocks import Blocks
from tests.modular.domains.financing._support import (
    COUNTERPARTY,
    SOURCE,
    assemble,
    fact,
    get,
    request,
)
from tests.modular.domains.taxes._fixtures import evidence
from tests.modular.domains.taxes.test_investment_tax_application import (
    inputs,
    journal_credit,
    qualification,
    quarter_return,
)

from ._working_case import case as case


def _sale(policy, day):
    return fact(
        "operations.service_sale",
        {
            "customer_id": COUNTERPARTY,
            "gross_fen": "51",
            "vat": {
                "policy_id": policy["business_id"],
                "exemption_eligible": False,
                "invoice_kind": "ordinary",
            },
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-07",
                "source": {"source_id": SOURCE},
            },
        },
        period="2026-07",
        actual=day,
    )


def _submit(app, company, *facts):
    body = request(company, *facts)
    body["sources"].append(evidence())
    response = app.accounting_apply(body)
    assert response.get("completion") == "complete", response


def _snapshot(app, company, view):
    scope = {
        "company_id": company,
        "period_from": "2026-07",
        "period_to": "2026-07",
        "view": view,
    }
    with app.storage.read_unit(company) as read:
        return app.ledger.financial_snapshot(scope, read)


def _assert_revision(app, company, current, result, revision):
    with app.storage.read_unit(company) as read:
        source = app.ledger.get_result_source(current["business_id"], "main", read)
    assert source["content_digest"] == content_digest(
        {
            "calculation_key": "taxes.assessment",
            "calculation_revision": revision,
            "basis_digest": content_digest(result["basis"]),
            "target_digest": content_digest(result["target"]),
        }
    )


def _actual(current):
    return fact(
        "taxes.actual_declaration",
        {
            "type": "vat",
            "assessment_id": current["business_id"],
            "start_month": "2026-07",
            "end_month": "2026-07",
            "basis_type": "filed_return",
            "source": {"source_id": SOURCE},
            "assessed_vat_fen": "1",
            "urban_fen": "0",
            "education_fen": "0",
            "local_education_fen": "0",
            "financial_transfer_vat_fen": "0",
            "investment_interest_vat_fen": "0",
        },
        period="2026-07",
    )


def _mixed_facts(*, actual=False):
    terms, acquired, disposed, policy, surtax, basis = inputs()
    acquired["data"].update(purchase_price_fen="10000", acquisition_fees_fen="0")
    disposed["data"]["gross_proceeds_fen"] = "10051"
    current = quarter_return(policy, surtax, "2026-07", "2026-07")
    return current, (
        terms,
        qualification(terms),
        acquired,
        disposed,
        policy,
        surtax,
        basis,
        _sale(policy, "2026-07-03"),
        current,
        *([_actual(current)] if actual else []),
    )


@pytest.mark.parametrize("actual", [False, True])
@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_financial_rounding_does_not_become_negative_ordinary_exemption_income(
    tmp_path, actual, view
):
    app, company = assemble(tmp_path, taxes=True)
    current, facts = _mixed_facts(actual=actual)
    _submit(app, company, *facts)
    result = get(app, company, current["business_id"])["businesses"][0]["results"][0]
    assert result["target"]["values"]["accrued_vat_fen"] == "2"
    assert result["target"]["values"]["payable_vat_fen"] == "1"
    assert result["target"]["values"]["rounding_adjustment_fen"] == "-1"
    assert journal_credit(app, company, "6301", "2026-07") == 0
    assert journal_credit(app, company, "5111", "2026-07") == 51
    report = _snapshot(app, company, view)
    assert report["profit_statement"]["20"]["current"] == {
        "state": "known",
        "amount_fen": "51",
    }
    assert report["profit_statement"]["22"]["current"] == {
        "state": "known",
        "amount_fen": "0",
    }
    assert report["profit_statement"]["23"]["current"] == {
        "state": "known",
        "amount_fen": "0",
    }

    (marker,) = [
        quantity
        for quantity in result["basis"]["adopted_quantities"]
        if quantity["type"] == "ordinary_vat_accounting"
    ]
    assert marker == {
        "type": "ordinary_vat_accounting",
        "exemption_income_fen": "0",
        "rounding_adjustment_fen": "0",
    }
    _assert_revision(app, company, current, result, 2)


@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_ordinary_rounding_income_stays_outside_exemption_detail(tmp_path, view):
    app, company = assemble(tmp_path, taxes=True)
    _, _, _, policy, surtax, _ = inputs()
    current = quarter_return(policy, surtax, "2026-07", "2026-07")
    _submit(
        app,
        company,
        policy,
        surtax,
        _sale(policy, "2026-07-03"),
        _sale(policy, "2026-07-04"),
        current,
    )
    assert journal_credit(app, company, "6301", "2026-07") == 1
    report = _snapshot(app, company, view)
    assert report["profit_statement"]["22"]["current"] == {
        "state": "known",
        "amount_fen": "1",
    }
    assert report["profit_statement"]["23"]["current"] == {
        "state": "known",
        "amount_fen": "0",
    }


@pytest.mark.parametrize("revision", [1, 2])
@pytest.mark.parametrize("frozen", [False, True])
def test_exact_vat_revision_retains_its_meaning_in_cold_and_warm_reports(
    case, monkeypatch, revision, frozen
):
    case.seed(expense=False)
    current, facts = _mixed_facts()
    evaluate = Taxes.evaluate_slot

    def legacy(self, business_id, result_slot, context):
        plan = evaluate(self, business_id, result_slot, context)
        if (
            plan["target"]["kind"] == "taxes.assessment"
            and "payable_vat_fen" in plan["target"]["values"]
        ):
            plan["calculation_revision"] = 1
            plan["rule_refs"][0]["revision"] = 1
            plan["basis"]["adopted_quantities"] = [
                quantity
                for quantity in plan["basis"]["adopted_quantities"]
                if quantity["type"] != "ordinary_vat_accounting"
            ]
        return plan

    with monkeypatch.context() if revision == 1 else nullcontext() as patch:
        if patch is not None:
            patch.setattr(Taxes, "evaluate_slot", legacy)
        _submit(case.app, case.company, *facts)
    if frozen:
        for month in range(1, 8):
            checked = case.prepare_close(f"2026-{month:02d}")
            assert checked["ready"], checked
            assert case.close(checked)["state"] == "closed"
    original = get(case.app, case.company, current["business_id"])["businesses"][0]["results"][0]
    _assert_revision(case.app, case.company, current, original, revision)
    assert original["reference"]["state"] == ("frozen" if frozen else "open")
    expected = {"state": "known", "amount_fen": "-1" if revision == 1 else "0"}
    blocks = Blocks()
    with case.app.storage.read_unit(case.company) as read:
        requests = tuple(
            case.app.ledger.financial_projection_request(
                "snapshot",
                {
                    "company_id": case.company,
                    "period_from": "2026-07",
                    "period_to": "2026-07",
                    "view": view,
                },
                read,
            )
            for view in ("current", "as_posted")
        )
        direct = {
            request.key: case.app.ledger.financial_snapshot(request.scope, read)
            for request in requests
        }
        blocks.publish(
            case.app.ledger.prepare_financial_projection(requests, (), (), read, rebuild=True)
        )
        for request in requests:
            assert direct[request.key]["profit_statement"]["23"]["current"] == expected
            for _ in range(2):
                warm = case.app.ledger.read_financial_projection(request, read, blocks)
                assert warm is not None
                assert warm.value["profit_statement"] == direct[request.key]["profit_statement"]
    assert (
        get(case.app, case.company, current["business_id"])["businesses"][0]["results"][0]
        == original
    )


@pytest.mark.parametrize("mode", ["direct", "cold"])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("corruption", ["missing", "duplicate", "exemption", "rounding"])
def test_real_vat_formal_accounting_corruption_is_not_hidden_by_zero_totals(
    tmp_path, mode, empty, corruption
):
    app, company = assemble(tmp_path, taxes=True)
    if empty:
        _, _, _, policy, surtax, _ = inputs()
        current = quarter_return(policy, surtax, "2026-07", "2026-07")
        facts = policy, surtax, current
    else:
        current, facts = _mixed_facts()
    _submit(app, company, *facts)
    result = get(app, company, current["business_id"])["businesses"][0]["results"][0]
    if empty:
        assert result["vouchers"] == []
    basis = deepcopy(result["basis"])
    (marker,) = [
        row for row in basis["adopted_quantities"] if row["type"] == "ordinary_vat_accounting"
    ]
    assert marker["exemption_income_fen"] == marker["rounding_adjustment_fen"] == "0"
    if corruption == "missing":
        basis["adopted_quantities"].remove(marker)
    elif corruption == "duplicate":
        basis["adopted_quantities"].append(deepcopy(marker))
    elif corruption == "exemption":
        marker["exemption_income_fen"] = "1"
    else:
        marker["rounding_adjustment_fen"] = "1"
    # Simulate persisted corruption in this disposable synthetic SQLite company.
    # Only the selected result body is damaged; no production result is altered.
    with app.storage.write_unit(company) as unit:
        unit.repository("ledger")._session.execute(
            "UPDATE adopted_result SET basis_json=? WHERE result_id=?",
            (canonical_json(basis), result["reference"]["result_id"]),
        )
    with pytest.raises(ContractError) as error:
        if mode == "direct":
            _snapshot(app, company, "current")
        else:
            with app.storage.read_unit(company) as read:
                request = app.ledger.financial_projection_request(
                    "snapshot",
                    {
                        "company_id": company,
                        "period_from": "2026-07",
                        "period_to": "2026-07",
                        "view": "current",
                    },
                    read,
                )
                app.ledger.prepare_financial_projection((request,), (), (), read, rebuild=True)
    assert error.value.code == "storage_corrupt"
    assert (
        "普通免税及舍入核算组成" in error.value.message
        or "与同源分录不能对平" in error.value.message
    )
