"""Omitted VAT choices retain relief while adopted accounting terms stay explicit."""

from copy import deepcopy
from uuid import uuid4

import pytest
from test_expense import sale_fact, vat_context
from test_tax_lifecycle import command, fact, policy, reach, result, sale
from test_tax_lifecycle import system as system

from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.ledger.public import LedgerQuery

CHOICES = ("waive_exemption", "waive_rate_reduction")


@pytest.mark.parametrize("reference", ["policy", "tax_point"])
def test_missing_supplied_reference_keeps_its_program_cause_and_saves_no_sale(system, reference):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule, "2026-01")
    facts = [earned]
    if reference == "tax_point":
        facts.append(rule)
        earned["data"]["tax_point"] = {"type": "derived", "tax_point_id": str(uuid4())}
    before = deepcopy(earned)
    with app.storage.read_unit(company) as read:
        head = read.head
    rejected = app.accounting_apply(command(company, *facts))
    assert rejected["error"] == (
        "invalid_input" if reference == "policy" else "dependency_conflict"
    ), rejected
    assert rejected["completion"] == "not_saved" and rejected["changes"] == "none"
    (problem,) = rejected["fact_issues"]
    assert problem["business_id"] == earned["business_id"]
    assert problem["field"] == (
        "/data/vat/policy_id" if reference == "policy" else "/data/tax_point/tax_point_id"
    )
    assert problem["code"] == (
        "reference_not_found" if reference == "policy" else "result_not_ready"
    )
    assert problem["reusable_sources"] == [{"source_id": ids["source"]}]
    assert earned == before
    with app.storage.read_unit(company) as read:
        assert read.head == head
    assert (
        app.accounting_get(
            {"company_id": company, "business_ids": [earned["business_id"]], "include": ["facts"]}
        )["businesses"]
        == []
    )


def test_earlier_known_tax_point_requires_real_source_continuity_without_changing_months(system):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule, "2026-01")
    earned["period"] = "2026-02"
    before = deepcopy(earned)
    rejected = app.accounting_apply(command(company, earned, rule))
    assert rejected["error"] == "dependency_conflict", rejected
    (problem,) = rejected["fact_issues"]
    assert problem["business_id"] == earned["business_id"]
    assert problem["field"] == "/data/tax_point"
    assert problem["code"] == "tax_source_required"
    assert "客户预收及其履约" in problem["message"] and "不能自动创建预收" in problem["message"]
    assert earned == before
    assert (
        app.accounting_get(
            {
                "company_id": company,
                "business_ids": [earned["business_id"], rule["business_id"]],
                "include": ["facts"],
            }
        )["businesses"]
        == []
    )


@pytest.mark.parametrize("invoice_kind", ["ordinary", "special", "uninvoiced"])
@pytest.mark.parametrize("omitted", [(CHOICES[0],), (CHOICES[1],), CHOICES])
def test_omitted_choices_match_explicit_false_without_changing_source(invoice_kind, omitted):
    current = sale_fact()
    current["data"]["vat"].update(
        invoice_kind=invoice_kind,
        exemption_eligible=True,
        waive_exemption=False,
        waive_rate_reduction=False,
    )
    context = vat_context(current, tax_base_rate="0.01", payable_rate="0.01")
    domain = create_operations()
    explicit = domain.evaluate_slot(current["business_id"], "main", context)
    for field in omitted:
        current["data"]["vat"].pop(field)
    before = deepcopy(current)
    defaulted = domain.evaluate_slot(current["business_id"], "main", context)
    assert current == before
    assert defaulted == explicit
    assert {key: defaulted["basis"]["input"]["vat"][key] for key in CHOICES} == {
        key: False for key in CHOICES
    }


@pytest.mark.parametrize("field", CHOICES)
def test_explicit_waiver_is_retained_when_the_other_choice_is_omitted(field):
    current = sale_fact()
    current["data"]["vat"].update(exemption_eligible=True)
    for key in CHOICES:
        current["data"]["vat"].pop(key)
    current["data"]["vat"][field] = True
    before = deepcopy(current)
    context = vat_context(current, tax_base_rate="0.01", payable_rate="0.01")
    plan = create_operations().evaluate_slot(current["business_id"], "main", context)
    assert current == before
    assert plan["basis"]["input"]["vat"][field] is True
    assert plan["basis"]["input"]["vat"][next(key for key in CHOICES if key != field)] is False
    published = plan["tax_sales"][0]
    assert published["exemption_eligible"] is (field != "waive_exemption")
    assert published["tax_base_rate"] == ("0.03" if field == "waive_rate_reduction" else "0.01")
    assert published["vat_fen"] == ("3000" if field == "waive_rate_reduction" else "1020")


def _omit(fact):
    for field in CHOICES:
        fact["data"]["vat"].pop(field)
    return fact


@pytest.mark.parametrize("omit_original", [False, True])
def test_taxed_advance_can_restate_equivalent_omitted_or_explicit_choices(system, omit_original):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, "2026-01", advance=True)
    terms = deepcopy(original["data"]["vat"])
    if omit_original:
        _omit(original)
    else:
        for field in CHOICES:
            terms.pop(field)
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": original["business_id"], "gross_fen": "10100", "vat": terms},
        period="2026-02",
    )
    before = deepcopy([original, fulfilled])
    saved = app.accounting_apply(command(company, fulfilled, original, rule))
    assert saved["completion"] == "complete", saved
    assert [original, fulfilled] == before
    adopted = result(app, company, original)
    assert all(adopted["basis"]["input"]["vat"][field] is False for field in CHOICES)
    fulfilled_result = result(app, company, fulfilled)
    assert all(fulfilled_result["basis"]["input"]["vat"][field] is False for field in CHOICES)
    assert fulfilled_result["target"]["values"]["revenue_fen"] == "10000"
    with app.storage.read_unit(company) as read:
        sales = app.ledger.tax_sales("2026-01", "2026-02", read)
    assert [(row.sale["source_business_id"], row.sale["vat_fen"]) for row in sales] == [
        (original["business_id"], "100")
    ]


def test_mixed_omitted_choices_share_one_cumulative_rounding_basis_for_fulfillment(system):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, advance=True)
    original["data"].update(amount_fen="102", tax_status="not_due")
    original["data"].pop("tax_point")
    first = fact(
        ids,
        "operations.advance_fulfillment",
        {
            "advance_id": original["business_id"],
            "gross_fen": "51",
            "vat": deepcopy(original["data"]["vat"]),
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-02",
                "source": {"source_id": ids["source"]},
            },
        },
        period="2026-02",
    )
    second = deepcopy(first)
    second["business_id"] = str(uuid4())
    _omit(second)
    before = deepcopy([original, first, second])
    saved = app.accounting_apply(command(company, second, first, original, rule))
    assert saved["completion"] == "complete", saved
    assert [original, first, second] == before
    adopted = [result(app, company, item) for item in (first, second)]
    assert sorted(int(row["target"]["values"]["vat_fen"]) for row in adopted) == [0, 1]
    assert sum(int(row["target"]["values"]["revenue_fen"]) for row in adopted) == 101
    assert all(row["basis"]["input"]["vat"][field] is False for row in adopted for field in CHOICES)
    with app.storage.read_unit(company) as read:
        assert (
            sum(
                int(row.sale["vat_fen"]) for row in app.ledger.tax_sales("2026-02", "2026-02", read)
            )
            == 1
        )


def test_closed_defaulted_sale_keeps_its_revenue_and_terms_during_later_tax_and_partial_receipt(
    system,
):
    app, company, ids = system
    rule = policy(ids)
    earned = _omit(sale(ids, rule, "2026-02"))
    source = deepcopy(earned)
    statement = fact(
        ids,
        "money.bank_statement",
        {"funds_account_id": ids["bank"], "opening_fen": "0", "closing_fen": "0", "rows": []},
    )
    reconciliation = fact(
        ids, "money.reconciliation", {"statement_id": statement["business_id"], "matches": []}
    )
    opening = fact(ids, "opening.package", {"basis": "newly_established", "members": []})
    saved = app.accounting_apply(command(company, opening, earned, rule, statement, reconciliation))
    assert saved["completion"] == "complete", saved
    checked = app.accounting_period({"company_id": company, "action": "check", "period": "2026-01"})
    assert checked["ready"], checked["blocking_issues"]
    closed = app.accounting_period(
        {
            "company_id": company,
            "action": "close",
            "period": "2026-01",
            "expected_head": checked["head"],
        }
    )
    assert closed["state"] == "closed", closed
    original = result(app, company, earned)
    assert original["target"]["values"]["revenue_fen"] == "10000"
    assert original["target"]["values"]["deferred_vat_fen"] == "100"
    assert all(original["basis"]["input"]["vat"][field] is False for field in CHOICES)
    received = fact(
        ids,
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": ids["bank"],
            "direction": "inflow",
            "amount_fen": "5050",
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": earned["business_id"], "slot": "primary"},
                    "amount_fen": "5050",
                }
            ],
        },
        period="2026-02",
        actual="2026-02-10",
    )
    saved = app.accounting_apply(command(company, received))
    assert saved["completion"] == "complete", saved
    assert earned == source
    assert result(app, company, earned) == original
    assert result(app, company, earned, "tax:2026-02")["target"]["values"]["vat_fen"] == "100"
    with app.storage.read_unit(company) as read:
        assert (
            app.ledger.pre_tax("2026-01", "2026-02", read).fields["operating_revenue"]["amount_fen"]
            == "10000"
        )
        obligations = app.ledger.query_obligations(LedgerQuery((earned["business_id"],)), read)
    assert obligations.items[0]["balance"]["remaining_fen"] == "5050"


@pytest.mark.parametrize("advance", [False, True])
def test_omitted_choices_remain_frozen_when_future_tax_releases_after_partial_return(
    system, advance
):
    app, company, ids = system
    rule = policy(ids)
    earned = _omit(sale(ids, rule, advance=advance))
    returned = fact(
        ids,
        "operations.advance_refund" if advance else "operations.sale_return",
        {"advance_id": earned["business_id"], "gross_fen": "2020"}
        if advance
        else {
            "sale_id": earned["business_id"],
            "customer_id": ids["party"],
            "gross_fen": "2020",
            "credit_note_vat_fen": "20",
        },
        period="2026-02",
    )
    saved = app.accounting_apply(command(company, returned, earned, rule))
    assert saved["completion"] == "complete", saved
    adopted = deepcopy(result(app, company, earned))
    saved = app.accounting_apply(command(company, reach(ids, "2026-03")))
    assert saved["completion"] == "complete", saved
    assert result(app, company, earned)["basis"] == adopted["basis"]
    assert result(app, company, earned)["vouchers"] == adopted["vouchers"]
    assert result(app, company, earned, "tax:2026-03")["target"]["values"]["vat_fen"] == "80"
    with app.storage.read_unit(company) as read:
        assert app.ledger.pre_tax("2026-01", "2026-03", read).fields["operating_revenue"][
            "amount_fen"
        ] == ("0" if advance else "8000")
