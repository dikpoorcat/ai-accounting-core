"""Tax oracles assert independently stated amounts and factual boundaries."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import ContractError, EvaluatedSlot, validate_contract
from ai_accounting_modular.domains.taxes._credits import selected_rows
from ai_accounting_modular.domains.taxes.public import create_taxes

from ._fixtures import (
    LOCATION,
    Context,
    actual_vat,
    assessment,
    confirmed_income,
    fact,
    income_inputs,
    policy,
    sale,
    source_ref,
    surtax,
    tax_sale,
    uid,
)


def evaluate(current, *sources, sales=(), profit="2000000"):
    return create_taxes().evaluate_slot(
        current["business_id"], "main", Context(current, *sources, sales=sales, profit=profit)
    )


def test_all_four_policy_variants_are_strict_sourced_no_voucher():
    rows = [
        policy(),
        surtax(),
        fact(
            "asset-policy",
            "taxes.used_asset_vat_policy",
            {
                "policy": deepcopy(policy()["data"]["policy"]),
                "tax_base_rate": "0.03",
                "payable_rate": "0.02",
            },
        ),
        fact(
            "income-policy",
            "taxes.income_tax_policy",
            {
                "policy": deepcopy(policy()["data"]["policy"]),
                "standard_rate": "0.25",
                "small_profit_rate": "0.20",
                "small_profit_taxable_fraction": "0.25",
                "small_profit_limit_fen": "300000000",
            },
        ),
    ]
    for row in rows:
        result = evaluate(row)
        assert result["lines"] == result["tax_sales"] == []
        assert result["target"]["values"]["policy_version"] == "synthetic-1"
        assert result["basis"]["input"] == row["data"]
        assert result["rule_refs"][0]["official_urls"]


def test_policy_reversed_date_or_impossible_rate_rejected():
    row = policy()
    row["data"]["policy"]["effective_to"] = "2022-12-31"
    with pytest.raises(ContractError):
        evaluate(row)
    row = policy(rate="0.01")
    row["data"]["payable_rate"] = "0.03"
    with pytest.raises(ContractError):
        evaluate(row)


def test_vat_three_surcharges_round_individually_and_preserve_unknown_settlement():
    result = evaluate(assessment(), policy(), surtax(), sales=[tax_sale("sale", "100000", "3000")])
    values = result["target"]["values"]
    assert {
        key: values[key]
        for key in (
            "payable_vat_fen",
            "urban_fen",
            "education_fen",
            "local_education_fen",
            "surtax_fen",
        )
    } == {
        "payable_vat_fen": "3000",
        "urban_fen": "105",
        "education_fen": "45",
        "local_education_fen": "30",
        "surtax_fen": "180",
    }
    assert "row_21_fen" not in values["filing_fields"]
    assert "row_25_fen" not in values["filing_fields"]
    assert values["filing_state"] == "partial"
    micro = evaluate(assessment(), policy(), surtax(), sales=[tax_sale("tiny", "300", "9")])
    assert micro["target"]["values"]["surtax_fen"] == "0"


def test_nominal_reduction_is_a_filing_display_not_a_second_gain():
    result = evaluate(
        assessment(),
        policy(rate="0.01"),
        surtax(zero=True),
        sales=[tax_sale("sale", "100000", "1000", rate="0.01")],
    )
    values = result["target"]["values"]
    assert values["payable_vat_fen"] == "1000"
    assert {
        key: values["filing_fields"][key] for key in ("row_15_fen", "row_16_fen", "row_20_fen")
    } == {"row_15_fen": "3000", "row_16_fen": "2000", "row_20_fen": "1000"}
    assert result["lines"] == []


@pytest.mark.parametrize(
    "operator,payable,relief", [("at_or_below", "0", "100000"), ("strictly_below", "100000", "0")]
)
def test_threshold_equality_is_explicit_not_inferred_from_new_year(operator, payable, relief):
    result = evaluate(
        assessment(),
        policy(rate="0.01", operator=operator),
        surtax(zero=True),
        sales=[tax_sale("sale", "10000000", "100000", rate="0.01", eligible=True)],
    )
    assert result["target"]["values"]["payable_vat_fen"] == payable
    assert result["target"]["values"]["relief_fen"] == relief


def test_period_rounding_is_separate_from_exemption():
    result = evaluate(
        assessment(),
        policy(rate="0.01"),
        surtax(zero=True),
        sales=[tax_sale("tiny", "17", "0", rate="0.01")],
    )
    assert result["target"]["values"]["rounding_adjustment_fen"] == "1"
    assert [
        (row["account_code"], row["debit_fen"], row["credit_fen"]) for row in result["lines"]
    ] == [("571105", "1", "0"), ("222101", "0", "1")]


def test_used_asset_taxable_net_is_not_accounting_disposal_net():
    result = evaluate(
        assessment(),
        policy(),
        surtax(zero=True),
        sales=[tax_sale("asset", "100000", "2000", bucket="used_asset")],
    )
    assert result["target"]["values"]["payable_vat_fen"] == "2000"
    assert result["target"]["values"]["filing_state"] == "unsupported_form"
    assert result["obligations"][0]["cashflow_weights"] == [
        {"category": "asset_disposal", "weight": "1"}
    ]


def test_negative_return_and_actual_zero_keep_vat_debit_without_fake_expense_claim_or_cash():
    current = assessment(policies=False)
    result = evaluate(current, actual_vat(current), sales=[tax_sale("return", "-10000", "-300")])
    metrics = result["target"]["values"]
    assert metrics["net_sales_fen"] == "-10000"
    assert metrics["accrued_vat_fen"] == "-300"
    assert metrics["payable_vat_fen"] == "0"
    assert "rounding_adjustment_fen" not in metrics and "relief_fen" not in metrics
    assert result["lines"] == result["fund_movements"] == []
    assert [(row["slot"], row["amount_fen"]) for row in result["obligations"]] == [
        ("vat", "0"),
        ("surtax", "0"),
    ]


def test_actual_declared_amounts_and_unknown_rows_are_never_filled_from_reference():
    current = assessment()
    actual = actual_vat(current, "900", urban_fen="1", education_fen="2", local_education_fen="3")
    result = evaluate(current, actual, policy(), surtax(), sales=[tax_sale("sale", "30000", "900")])
    values = result["target"]["values"]
    assert values["payable_vat_fen"] == "900" and values["surtax_fen"] == "6"
    assert values["rounding_adjustment_fen"] == "0"
    assert "row_1_fen" not in values["filing_fields"]
    assert not any(row["account_code"] == "571105" for row in result["lines"])


@pytest.mark.parametrize("payable", ["900", "0"])
def test_actual_vat_unexplained_difference_cannot_create_a_false_liability(payable):
    current = assessment(policies=False)
    with pytest.raises(ContractError, match="差额的真实会计原因") as failure:
        evaluate(current, actual_vat(current, payable), sales=[tax_sale("sale", "20000", "600")])
    assert failure.value.code == "needs_information"


def test_actual_zero_can_adopt_proven_or_explicit_exemption_without_fake_rounding():
    current = assessment()
    values = evaluate(
        current,
        actual_vat(current),
        policy(),
        surtax(),
        sales=[tax_sale("sale", "20000", "600", eligible=True)],
    )["target"]["values"]
    assert (values["payable_vat_fen"], values["relief_fen"], values["rounding_adjustment_fen"]) == (
        "0",
        "600",
        "0",
    )
    current = assessment(policies=False)
    result = evaluate(
        current,
        actual_vat(current, exemption_relief_fen="600"),
        sales=[tax_sale("sale", "20000", "600")],
    )
    assert result["target"]["values"]["relief_fen"] == "600"
    assert "rounding_adjustment_fen" not in result["target"]["values"]
    assert [
        (row["account_code"], row["debit_fen"], row["credit_fen"]) for row in result["lines"]
    ] == [("222101", "600", "0"), ("6301", "0", "600")]


def test_equivalent_decimal_rates_group_before_period_rounding():
    first = tax_sale("first", "17", "0", rate="0.01")
    second = tax_sale("second", "17", "0", rate="0.010")
    second.sale["nominal_rate"] = "0.030"
    result = evaluate(assessment(), policy(rate="0.01"), surtax(zero=True), sales=[first, second])
    values = result["target"]["values"]
    assert values["payable_vat_fen"] == "0"
    assert values["filing_state"] == "partial"
    assert values["filing_fields"]["row_15_fen"] == values["filing_fields"]["row_16_fen"] == "1"


def test_actual_inconsistent_prepaid_and_due_rejects_without_reference_fallback():
    current = assessment()
    actual = actual_vat(current, "900", prepaid_vat_fen="600", vat_due_fen="400")
    with pytest.raises(ContractError, match="预缴"):
        evaluate(current, actual, policy(), surtax())


def test_tax_point_month_precision_and_disjoint_amount_earliest_events():
    target = sale()
    point = fact(
        "tax-point",
        "taxes.tax_point",
        {
            "target_business_id": target["business_id"],
            "transaction_mode": "ordinary_service",
            "service_state": "started",
            "contractual_entitlement": "established",
            "events": [
                {
                    "event_id": uid("start"),
                    "type": "service_start",
                    "month": "2026-01",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "103000"}],
                },
                {
                    "event_id": uid("contract"),
                    "type": "contractual_entitlement",
                    "month": "2026-03",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "103000"}],
                },
                {
                    "event_id": uid("invoice"),
                    "type": "invoice",
                    "month": "2026-02",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "41200"}],
                },
            ],
        },
    )
    result = evaluate(point, target)
    assert result["tax_sales"] == []
    segments = result["target"]["values"]["segments"]
    assert [(row["tax_period"], row["ranges"]) for row in segments] == [
        ("2026-02", [{"from_fen": "0", "to_fen": "41200"}]),
        ("2026-03", [{"from_fen": "41200", "to_fen": "103000"}]),
    ]
    assert all("tax_on" not in row for row in segments)
    assert all("source" not in row for row in result["basis"]["input"]["events"])


def test_tax_point_duplicate_receipt_or_unallocated_bank_line_cannot_prove_sale():
    target = sale()
    event = {
        "event_id": uid("receipt"),
        "type": "receipt",
        "month": "2026-01",
        "on": "2026-01-09",
        "receipt_stage": "before_transaction",
        "receipt_source": {"business_id": uid("receipt"), "slot": "funds"},
        "source": LOCATION,
        "ranges": [{"from_fen": "0", "to_fen": "103000"}],
    }
    receipt = fact(
        "receipt",
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": uid("bank"),
            "direction": "inflow",
            "amount_fen": "103000",
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": uid("other-sale"), "slot": "primary"},
                    "amount_fen": "103000",
                }
            ],
        },
        effective_on="2026-01-09",
    )
    point = fact(
        "tax-point",
        "taxes.tax_point",
        {
            "target_business_id": target["business_id"],
            "transaction_mode": "phased_prepaid_service",
            "service_state": "not_started",
            "contractual_entitlement": "not_set",
            "events": [event],
        },
    )
    with pytest.raises(ContractError, match="分配金额"):
        evaluate(point, target, receipt)
    receipt["data"]["allocations"][0]["source"]["business_id"] = target["business_id"]
    result = evaluate(point, target, receipt)
    assert result["target"]["values"]["segments"] == [
        {"state": "not_due", "ranges": [{"from_fen": "0", "to_fen": "103000"}]}
    ]
    point["data"]["events"].append({**event, "event_id": uid("duplicate-receipt")})
    with pytest.raises(ContractError, match="重复使用"):
        evaluate(point, target, receipt)


def test_income_tax_shared_profit_and_single_final_rounding():
    current, basis, params = income_inputs()
    result = evaluate(current, basis, params)
    values = result["target"]["values"]
    assert values["profit_before_tax_fen"] == "2000000"
    assert values["cumulative_tax_fen"] == values["change_fen"] == "100000"
    assert result["basis"]["adopted_quantities"][-1]["profit_before_tax_fen"] == "2000000"
    result = evaluate(current, basis, params, profit="10")
    assert result["target"]["values"]["cumulative_tax_fen"] == "1"


def test_actual_income_adopts_cumulative_not_tax_due_and_does_not_require_reference_policy():
    current, _, _ = income_inputs()
    actual = fact(
        "income-actual",
        "taxes.actual_declaration",
        {
            "type": "income_tax",
            "assessment_id": current["business_id"],
            "tax_year": 2026,
            "tax_period_end": "2026-03",
            "basis_type": "filed_return",
            "source": LOCATION,
            "cumulative_tax_fen": "90000",
            "prepaid_tax_fen": "60000",
            "tax_due_fen": "30000",
        },
        "2026-04",
    )
    result = evaluate(current, actual)
    assert result["target"]["values"]["cumulative_tax_fen"] == "90000"
    assert result["target"]["values"]["change_fen"] == "90000"
    assert result["target"]["values"]["adopted_basis"] == "actual"
    assert result["fund_movements"] == []


def test_income_negative_cumulative_change_keeps_cash_separate():
    first = fact(
        "income-first",
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": "confirmed_provision",
            "tax_year": 2026,
            "tax_period_end": "2026-03",
            "cumulative_tax_fen": "90000",
            "source": LOCATION,
        },
        "2026-03",
    )
    second = fact(
        "income-next",
        "taxes.assessment",
        {**first["data"], "tax_period_end": "2026-06", "cumulative_tax_fen": "80000"},
        "2026-06",
    )
    result = evaluate(second, first)
    assert result["target"]["values"]["change_fen"] == "-10000"
    assert result["obligations"][0]["direction"] == "receivable"
    assert result["obligations"][0]["amount_fen"] == "10000"
    assert result["obligations"][0]["allowed_settlements"] == ["offset", "transfer"]
    assert result["fund_movements"] == []


def test_description_publishes_only_vat_pretax_and_declares_aggregates():
    current = assessment()
    description = create_taxes().describe_change(
        [{"op": "create", "expected_seq": 0, "fact": current}], Context(current)
    )
    assert description["publications"] == [
        {
            "business_id": current["business_id"],
            "result_slot": "main",
            "collection": "pre_tax",
            "period_from": "2026-01",
            "period_to": "2026-01",
        }
    ]
    assert any(
        edge["producer_kind"] == "aggregate" and edge["producer_id"] == "tax_sales"
        for edge in description["dependencies"]
    )
    current, basis, params = income_inputs()
    description = create_taxes().describe_change(
        [{"op": "create", "expected_seq": 0, "fact": current}], Context(current, basis, params)
    )
    assert description["publications"] == []
    assert any(
        edge["producer_kind"] == "aggregate" and edge["producer_id"] == "pre_tax"
        for edge in description["dependencies"]
    )
    validate_contract("business-facts.schema.json", "ChangeDescription", description)


def test_actual_partial_credit_vat_is_not_recomputed_from_a_flat_gross_ratio():
    # Four valid invoice lines: gross 1,51,18,33 and VAT 0,1,1,1.
    # The second line has gross51/VAT1; flattening gross103/VAT3 would
    # allocate round(3*52/103)-round(3*1/103)=2 to its original interval.
    current = fact("confirmed", "taxes.tax_credit", {})
    view = tax_sale(
        "red-note", "-100", "-3", period="2026-02", ranges=[{"from_fen": "0", "to_fen": "103"}]
    )
    item = {
        "return_id": uid("red-note"),
        "original_business_id": uid("original"),
        "gross_fen": "51",
        "vat_fen": "1",
        "tax_ranges": [{"from_fen": "1", "to_fen": "52"}],
    }
    assert selected_rows(current, item, [view]) == [(0, 51, 1, [(1, 52)])]


def test_multiple_partial_credit_sources_need_explicit_components_not_guessed_rates():
    current = fact("confirmed", "taxes.tax_credit", {})
    first = tax_sale(
        "red-note", "-100", "-3", period="2026-02", ranges=[{"from_fen": "0", "to_fen": "103"}]
    )
    second = tax_sale(
        "red-note",
        "-100",
        "-1",
        period="2026-02",
        rate="0.01",
        ranges=[{"from_fen": "103", "to_fen": "204"}],
    )
    item = {
        "return_id": uid("red-note"),
        "original_business_id": uid("original"),
        "gross_fen": "100",
        "vat_fen": "2",
        "tax_ranges": [{"from_fen": "0", "to_fen": "50"}, {"from_fen": "103", "to_fen": "153"}],
    }
    with pytest.raises(ContractError, match="多个部分税源"):
        selected_rows(current, item, [first, second])


def test_phased_prepaid_service_taxes_all_received_at_earlier_contract_or_first_start():
    target = sale()
    receipt = fact(
        "receipt",
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": uid("bank"),
            "direction": "inflow",
            "amount_fen": "103000",
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": target["business_id"], "slot": "primary"},
                    "amount_fen": "103000",
                }
            ],
        },
        effective_on="2026-01-09",
    )
    point = fact(
        "tax-point",
        "taxes.tax_point",
        {
            "target_business_id": target["business_id"],
            "transaction_mode": "phased_prepaid_service",
            "service_state": "started",
            "contractual_entitlement": "established",
            "events": [
                {
                    "event_id": uid("receipt"),
                    "type": "receipt",
                    "month": "2026-01",
                    "on": "2026-01-09",
                    "receipt_stage": "before_transaction",
                    "receipt_source": {"business_id": receipt["business_id"], "slot": "funds"},
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "103000"}],
                },
                {
                    "event_id": uid("contract"),
                    "type": "contractual_entitlement",
                    "month": "2026-02",
                    "on": "2026-02-10",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "103000"}],
                },
                {
                    "event_id": uid("start"),
                    "type": "service_start",
                    "month": "2026-02",
                    "on": "2026-02-15",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "20600"}],
                },
            ],
        },
        "2026-02",
    )
    plan = evaluate(point, target, receipt)
    segments = plan["target"]["values"]["segments"]
    assert len(segments) == 1
    assert segments[0]["tax_period"] == "2026-02" and segments[0]["tax_on"] == "2026-02-10"
    assert segments[0]["ranges"] == [{"from_fen": "0", "to_fen": "103000"}]
    assert plan["tax_sales"] == plan["lines"] == []


def test_actual_payroll_is_precise_sourced_allocation_without_computing_personal_tax():
    remuneration = fact(
        "bonus",
        "people.remuneration",
        {
            "type": "bonus",
            "person_id": uid("person"),
            "gross_fen": "10000",
            "expense_area": "administration",
            "tax_basis": {
                "tax_period": "2026-01",
                "tax_fen": "100",
                "basis_type": "final_statement",
                "source": LOCATION,
            },
        },
    )
    row = {
        "row_id": uid("payroll-row"),
        "person_id": uid("person"),
        "income_category": "bonus",
        "tax_period": "2026-01",
        "declared_tax_fen": "100",
        "source": LOCATION,
        "allocations": [{"remuneration_id": remuneration["business_id"], "tax_fen": "100"}],
    }
    declared = fact(
        "payroll-declaration",
        "taxes.actual_declaration",
        {
            "type": "payroll",
            "tax_period": "2026-01",
            "source": LOCATION,
            "rows": [row],
        },
        "2026-02",
    )
    result = evaluate(declared, remuneration)
    assert result["lines"] == result["obligations"] == result["tax_sales"] == []
    assert result["target"]["values"]["tax_period_end"] == "2026-01"
    assert "source" not in result["basis"]["input"]["rows"][0]
    row["allocations"][0]["tax_fen"] = "99"
    declared["data"]["rows"] = [row]
    with pytest.raises(ContractError, match="分配合计"):
        evaluate(declared, remuneration)


def test_annual_income_settlement_keeps_tax_year_independent_of_discovery_year():
    quarter = fact(
        "quarter-four",
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": "confirmed_provision",
            "tax_year": 2026,
            "tax_period_end": "2026-12",
            "cumulative_tax_fen": "80000",
            "source": LOCATION,
        },
        "2026-12",
    )
    annual = fact(
        "annual",
        "taxes.assessment",
        {
            **quarter["data"],
            "method": "annual_settlement",
            "cumulative_tax_fen": "95000",
        },
        "2027-05",
    )
    result = evaluate(annual, quarter)
    assert result["accounting_period"] == "2027-05"
    assert result["target"]["values"]["tax_year"] == 2026
    assert result["target"]["values"]["change_fen"] == "15000"
    assert result["basis"]["source_results"][0]["business_id"] == quarter["business_id"]


def test_reference_policy_must_cover_every_actual_day_but_does_not_block_matching_actual():
    params = policy()
    params["data"]["policy"]["effective_from"] = "2026-01-15"
    current = assessment()
    with pytest.raises(ContractError, match="完整覆盖"):
        evaluate(current, params, surtax())
    result = evaluate(
        current,
        params,
        surtax(),
        actual_vat(current, "300"),
        sales=[tax_sale("sale", "10000", "300")],
    )
    assert result["target"]["values"]["payable_vat_fen"] == "300"
    assert result["target"]["values"]["adopted_basis"] == "actual"
    assert all(
        problem["blocking_scope"] == "none"
        for problem in result["target"]["values"]["filing_issues"]
    )


def test_credit_selects_only_relevant_original_month_from_multimonth_return():
    jan, original = (
        assessment("jan"),
        sale("original", "206", tax_point={"type": "derived", "tax_point_id": uid("multi-point")}),
    )
    first = tax_sale("original", "100", "3", ranges=[{"from_fen": "0", "to_fen": "103"}])
    second = tax_sale(
        "original",
        "100",
        "3",
        period="2026-02",
        ranges=[{"from_fen": "103", "to_fen": "206"}],
    )
    returned = fact(
        "multi-return",
        "operations.sale_return",
        {
            "sale_id": original["business_id"],
            "customer_id": original["data"]["customer_id"],
            "gross_fen": "206",
            "credit_note_vat_fen": "6",
            "tax_ranges": [{"from_fen": "0", "to_fen": "206"}],
        },
        "2026-03",
    )
    credit = fact(
        "credit",
        "taxes.tax_credit",
        {
            "original_assessment_id": jan["business_id"],
            "start_month": "2026-01",
            "end_month": "2026-01",
            "returns": [
                {
                    "return_id": returned["business_id"],
                    "original_business_id": original["business_id"],
                    "gross_fen": "103",
                    "vat_fen": "3",
                    "tax_ranges": [{"from_fen": "0", "to_fen": "103"}],
                }
            ],
            "original_filing_source": LOCATION,
            "authority_source": LOCATION,
            "disposition": "refund",
            "vat_credit_fen": "3",
            "surtax_credit_fen": "0",
            "exemption_reversal_fen": "0",
        },
        "2026-03",
    )
    red1 = tax_sale("multi-return", "-100", "-3", period="2026-03", ranges=first.sale["ranges"])
    red2 = tax_sale("multi-return", "-100", "-3", period="2026-03", ranges=second.sale["ranges"])
    context = Context(
        credit, jan, original, returned, actual_vat(jan, "3"), sales=[first, second, red1, red2]
    )
    context.results[(returned["business_id"], "main")] = EvaluatedSlot(
        source_ref(returned["business_id"]),
        {
            "kind": returned["kind"],
            "result_slot": "main",
            "values": {"gross_fen": "206", "revenue_reversed_fen": "200", "vat_reversed_fen": "6"},
        },
        {
            "kind": returned["kind"],
            "result_slot": "main",
            "period": returned["period"],
            "input": returned["data"],
            "source_results": [source_ref(original["business_id"])],
            "policies": [],
            "source_locations": [LOCATION],
            "adopted_quantities": [
                {"type": "tax_sale", "sale": first.sale},
                {"type": "tax_sale", "sale": second.sale},
            ],
        },
    )
    result = create_taxes().evaluate_slot(credit["business_id"], "main", context)
    assert result["target"]["values"]["vat_credit_fen"] == "3"


@pytest.mark.parametrize("receipt_month", [None, "2026-02", "2026-03"])
def test_fulfillment_supported_invoice_does_not_wait_for_irrelevant_advance_receipt_mapping(
    receipt_month,
):
    target = fact(
        "fulfillment",
        "operations.advance_fulfillment",
        {
            "advance_id": uid("advance"),
            "gross_fen": "10300",
        },
        "2026-03",
    )
    events = [
        {
            "event_id": uid("start"),
            "type": "service_start",
            "month": "2026-01",
            "on": "2026-01-01",
            "source": LOCATION,
            "ranges": [{"from_fen": "0", "to_fen": "10300"}],
        },
        {
            "event_id": uid("invoice"),
            "type": "invoice",
            "month": "2026-02",
            "on": "2026-02-15",
            "source": LOCATION,
            "ranges": [{"from_fen": "0", "to_fen": "10300"}],
        },
    ]
    sources = [target]
    if receipt_month is not None:
        receipt = fact(
            "advance-receipt",
            "money.movement",
            {
                "type": "settlement",
                "funds_account_id": uid("bank"),
                "direction": "inflow",
                "amount_fen": "10300",
                "payment_method": "individual",
                "allocations": [
                    {
                        "source": {"business_id": uid("advance"), "slot": "primary"},
                        "amount_fen": "10300",
                    }
                ],
            },
            receipt_month,
            effective_on=receipt_month + "-01",
        )
        sources.append(receipt)
        events.append(
            {
                "event_id": uid("receipt"),
                "type": "receipt",
                "month": receipt_month,
                "on": receipt_month + "-01",
                "source": LOCATION,
                "receipt_source": {"business_id": receipt["business_id"], "slot": "funds"},
                "receipt_stage": "during_or_after_transaction",
                "ranges": [{"from_fen": "0", "to_fen": "10300"}],
            }
        )
    point = fact(
        "fulfillment-point",
        "taxes.tax_point",
        {
            "target_business_id": target["business_id"],
            "transaction_mode": "ordinary_service",
            "service_state": "started",
            "contractual_entitlement": "not_set",
            "events": events,
        },
        "2026-03",
    )
    segment = evaluate(point, *sources)["target"]["values"]["segments"][0]
    assert segment["tax_period"] == "2026-02" and segment["trigger"] == "invoice"
    if receipt_month == "2026-02":
        assert "tax_on" not in segment
    else:
        assert segment["tax_on"] == "2026-02-15"


def test_earlier_unmapped_receipt_requires_exact_fulfillment_relation():
    target = fact(
        "fulfillment",
        "operations.advance_fulfillment",
        {
            "advance_id": uid("advance"),
            "gross_fen": "10300",
        },
        "2026-03",
    )
    receipt = fact(
        "advance-receipt",
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": uid("bank"),
            "direction": "inflow",
            "amount_fen": "10300",
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": uid("advance"), "slot": "primary"},
                    "amount_fen": "10300",
                }
            ],
        },
        "2026-01",
        effective_on="2026-01-10",
    )
    point = fact(
        "fulfillment-point",
        "taxes.tax_point",
        {
            "target_business_id": target["business_id"],
            "transaction_mode": "ordinary_service",
            "service_state": "started",
            "contractual_entitlement": "not_set",
            "events": [
                {
                    "event_id": uid("start"),
                    "type": "service_start",
                    "month": "2026-01",
                    "on": "2026-01-01",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "10300"}],
                },
                {
                    "event_id": uid("invoice"),
                    "type": "invoice",
                    "month": "2026-02",
                    "source": LOCATION,
                    "ranges": [{"from_fen": "0", "to_fen": "10300"}],
                },
                {
                    "event_id": uid("receipt"),
                    "type": "receipt",
                    "month": "2026-01",
                    "on": "2026-01-10",
                    "source": LOCATION,
                    "receipt_source": {"business_id": receipt["business_id"], "slot": "funds"},
                    "receipt_stage": "during_or_after_transaction",
                    "ranges": [{"from_fen": "0", "to_fen": "10300"}],
                },
            ],
        },
        "2026-03",
    )
    with pytest.raises(ContractError, match="精确范围对应") as error:
        evaluate(point, target, receipt)
    assert error.value.code == "needs_information"
    assert error.value.fact_issues[0]["field"] == "/data/events/2/receipt_source"


def test_actual_invoice_rows_outside_supported_relationship_cannot_claim_ready():
    current = assessment()
    rows = {f"row{number}_fen": "0" for number in (1, 2, 3, 9, 10, 11, 15, 16, 17, 18, 19)}
    rows.update(
        row1_fen="10000",
        row3_fen="10000",
        row15_fen="300",
        prepaid_vat_fen="0",
        vat_due_fen="300",
        urban_due_fen="0",
        education_due_fen="0",
        local_education_due_fen="0",
    )
    declared = actual_vat(current, "300", **rows)
    result = evaluate(
        current, declared, policy(), surtax(), sales=[tax_sale("sale", "10000", "300")]
    )
    assert result["target"]["values"]["filing_state"] == "ready"
    declared["data"].update(row1_fen="0", row2_fen="999", row3_fen="0")
    result = evaluate(
        current, declared, policy(), surtax(), sales=[tax_sale("sale", "10000", "300")]
    )
    values = result["target"]["values"]
    assert values["payable_vat_fen"] == "300" and values["filing_state"] == "partial"
    assert any(issue["code"] == "unsupported_form" for issue in values["filing_issues"])
    declared["data"].pop("row1_fen")
    result = evaluate(
        current, declared, policy(), surtax(), sales=[tax_sale("sale", "10000", "300")]
    )
    assert "row_1_fen" not in result["target"]["values"]["filing_fields"]


@pytest.mark.parametrize(
    "total_field,detail_field", [("row9_fen", "row10_fen"), ("row17_fen", "row18_fen")]
)
def test_actual_exemption_detail_cannot_exceed_its_explicit_total(total_field, detail_field):
    current = assessment()
    declared = actual_vat(current, "300", **{total_field: "0", detail_field: "1"})
    with pytest.raises(ContractError, match="免税子项"):
        evaluate(declared, current)


@pytest.mark.parametrize(
    "received,original_refund,expected",
    [(0, "2000", "2000"), (2000, "2000", "0"), (2000, "3000", "1000")],
)
def test_income_actual_original_refund_deducts_realized_predecessors_not_its_own_cash(
    received, original_refund, expected
):
    before = confirmed_income("quarter-first", "2026-03", "10000")
    lower = confirmed_income("quarter-lower", "2026-06", "8000")
    current = fact(
        "annual",
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": "annual_settlement",
            "tax_year": 2026,
            "tax_period_end": "2026-12",
            "cumulative_tax_fen": "8000",
            "source": LOCATION,
        },
        "2027-05",
    )
    declared = fact(
        "annual-declaration",
        "taxes.actual_declaration",
        {
            "type": "income_tax",
            "assessment_id": current["business_id"],
            "tax_year": 2026,
            "tax_period_end": "2026-12",
            "basis_type": "filed_return",
            "source": LOCATION,
            "cumulative_tax_fen": "8000",
            "prepaid_tax_fen": str(8000 + int(original_refund)),
            "tax_due_fen": "-" + original_refund,
        },
        "2027-05",
    )
    context = Context(current, lower, before, declared)
    context.cash_receipts[(lower["business_id"], "tax_refund")] = received
    context.cash_receipts[(current["business_id"], "tax_refund")] = 9999
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    refund = next(row for row in result["obligations"] if row["slot"] == "tax_refund")
    assert refund["amount_fen"] == expected
    assert refund["allowed_settlements"] == ["money", "offset", "transfer"]


def test_midyear_income_requires_typed_cumulative_carry_and_does_not_guess_from_profit():
    current = confirmed_income("quarter-second", "2026-06", "8000")
    context = Context(current)
    context.read_context = SimpleNamespace(start_period="2026-04")
    with pytest.raises(ContractError, match="有源累计已计所得税") as error:
        create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert error.value.code == "needs_information"
    continuation = fact(
        "continuation",
        "reports.continuation",
        {
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": "2026-04",
            "opening_package_id": uid("opening"),
            "year_beginning_balance": [
                {"line": line, "beginning_fen": "0"} for line in range(1, 54)
            ],
            "prior_profit": [
                {
                    "line": line,
                    "quarter_to_date_fen": "5000"
                    if line == 31
                    else ("-5000" if line == 32 else "0"),
                    "year_to_date_fen": "5000" if line == 31 else ("-5000" if line == 32 else "0"),
                }
                for line in range(1, 33)
            ],
            "prior_cash": [
                {"line": line, "quarter_to_date_fen": "0", "year_to_date_fen": "0"}
                for line in range(1, 23)
            ],
        },
        "2026-04",
    )
    context.facts[continuation["business_id"]] = continuation
    # A strict public producer fixture, not an implementation of the report owner.
    context.results[(continuation["business_id"], "main")] = EvaluatedSlot(
        source_ref(continuation["business_id"]),
        {
            "kind": "reports.continuation",
            "result_slot": "main",
            "values": {
                "bookkeeping_start": "2026-04",
                "accounting_standard": "small_enterprise",
                "balance_row_count": 53,
                "profit_row_count": 32,
                "cash_row_count": 22,
            },
        },
        {
            "kind": "reports.continuation",
            "result_slot": "main",
            "period": "2026-04",
            "input": continuation["data"],
            "source_results": [],
            "policies": [],
            "source_locations": [LOCATION],
            "adopted_quantities": [
                {
                    "type": "income_tax_cumulative",
                    "tax_year": 2026,
                    "period_to": "2026-03",
                    "cumulative_tax_fen": "5000",
                }
            ],
        },
    )
    result = create_taxes().evaluate_slot(current["business_id"], "main", context)
    assert result["target"]["values"]["previous_cumulative_fen"] == "5000"
    assert result["target"]["values"]["change_fen"] == "3000"
    context.results[(continuation["business_id"], "main")].basis["adopted_quantities"][0][
        "period_to"
    ] = "2026-02"
    with pytest.raises(ContractError, match="有源累计已计所得税"):
        create_taxes().evaluate_slot(current["business_id"], "main", context)
