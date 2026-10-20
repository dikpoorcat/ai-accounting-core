"""Aggregate contracts keep current financial inputs and exact sources explicit."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    PretaxView,
    TaxSaleView,
    schema_document,
    validate_contract,
)


def test_publication_and_aggregate_dependency_use_named_collections_and_complete_periods():
    publication = {
        "business_id": str(uuid4()),
        "result_slot": "tax:2026-03",
        "collection": "tax_sales",
        "period_from": "2026-03",
        "period_to": "2026-03",
    }
    validate_contract("business-facts.schema.json", "ResultPublication", publication)
    edge = {
        "consumer_business_id": str(uuid4()),
        "consumer_result_slot": "main",
        "producer_kind": "aggregate",
        "producer_id": "pre_tax",
        "producer_public_slot": "main",
        "read_mode": "result",
        "relation_kind": "calculation",
        "period_from": "2026-01",
        "period_to": "2026-03",
    }
    validate_contract("business-facts.schema.json", "Dependency", edge)
    for key, value in (
        ("producer_id", str(uuid4())),
        ("producer_public_slot", "cost"),
        ("read_mode", "fact"),
        ("relation_kind", "settlement"),
        ("period_from", None),
    ):
        invalid = deepcopy(edge)
        if value is None:
            invalid.pop(key)
        else:
            invalid[key] = value
        with pytest.raises(ContractError):
            validate_contract("business-facts.schema.json", "Dependency", invalid)


def test_tax_point_resolution_carries_timing_ranges_without_tax_amounts():
    resolution = {
        "target_business_id": str(uuid4()),
        "segments": [
            {
                "state": "due",
                "ranges": [{"from_fen": "0", "to_fen": "10100"}],
                "tax_period": "2026-03",
                "trigger": "invoice",
                "event_ids": [str(uuid4())],
            },
            {"state": "not_due", "ranges": [{"from_fen": "10100", "to_fen": "20200"}]},
        ],
    }
    validate_contract("business-facts.schema.json", "TaxPointResolution", resolution)
    invalid = deepcopy(resolution)
    invalid["segments"][1]["tax_period"] = "2026-12"
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "TaxPointResolution", invalid)
    invalid = deepcopy(resolution)
    invalid["tax_sales"] = []
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "TaxPointResolution", invalid)


def test_actual_vat_can_keep_negative_sales_without_inventing_relief_or_rounding():
    metrics = {
        "type": "vat",
        "net_sales_fen": "-10000",
        "accrued_vat_fen": "-300",
        "payable_vat_fen": "0",
        "urban_fen": "0",
        "education_fen": "0",
        "local_education_fen": "0",
        "surtax_fen": "0",
        "adopted_basis": "actual",
        "filing_state": "partial",
        "filing_issues": [],
    }
    validate_contract("business-facts.schema.json", "VatAssessmentMetrics", metrics)
    calculated = {**metrics, "adopted_basis": "calculated"}
    with pytest.raises(ContractError):
        validate_contract("business-facts.schema.json", "VatAssessmentMetrics", calculated)
    with pytest.raises(ContractError):
        validate_contract(
            "business-facts.schema.json",
            "VatAssessmentMetrics",
            {**metrics, "payable_vat_fen": "-1"},
        )


def test_process_views_reuse_strict_values_and_preserve_unknown_pretax():
    source = {
        "business_id": str(uuid4()),
        "result_slot": "main",
        "result_id": str(uuid4()),
        "result_generation": 1,
        "content_digest": "0" * 64,
    }
    sale = {
        "source_business_id": str(uuid4()),
        "tax_period": "2026-03",
        "net_sales_fen": "10000",
        "vat_fen": "100",
        "nominal_rate": "0.03",
        "tax_base_rate": "0.01",
        "payable_rate": "0.01",
        "invoice_kind": "ordinary",
        "exemption_eligible": True,
        "filing_bucket": "standard",
        "ranges": [{"from_fen": "0", "to_fen": "10100"}],
    }
    assert TaxSaleView(sale, source).source == source
    issue = {
        "code": "continuation_required",
        "field": "/pre_tax",
        "meaning": "接账前本年累计利润",
        "message": "需要已有账簿的接续资料。",
        "allowed_precision": ["source"],
        "reusable_sources": [],
        "blocking_scope": "business",
    }
    unknown = {"state": "unestablished", "issue_indexes": [0]}
    fields = {
        "period_from": "2026-01",
        "period_to": "2026-06",
        "operating_revenue": unknown,
        "operating_cost": unknown,
        "profit_before_tax": unknown,
        "basis_digest": "1" * 64,
    }
    assert PretaxView(fields, (source,), (issue,)).fields["profit_before_tax"] == unknown
    with pytest.raises(ContractError):
        PretaxView({**fields, "profit_before_tax": {**unknown, "amount_fen": "0"}})


def test_unknown_report_detail_cannot_be_mistaken_for_known_zero_or_free_line():
    issue = {
        "code": "surtax_detail_required",
        "field": "/data/urban_credit_fen",
        "meaning": "实际附加税退抵中的城建税",
        "message": "总额已明确，但缺少可唯一确定各项的来源。",
        "allowed_precision": ["source"],
        "reusable_sources": [],
        "blocking_scope": "none",
    }
    unknown = {
        "statement": "profit",
        "line": 6,
        "detail_code": "tax_urban",
        "state": "unestablished",
        "issue": issue,
    }
    validate_contract("business-facts.schema.json", "ReportContribution", unknown)
    known_zero = {key: unknown[key] for key in ("statement", "line", "detail_code")}
    known_zero["amount_fen"] = "0"
    validate_contract("business-facts.schema.json", "ReportContribution", known_zero)
    for value in (
        {**unknown, "amount_fen": "0"},
        {**unknown, "line": 31},
        {**unknown, "issue": {}},
    ):
        with pytest.raises(ContractError):
            validate_contract("business-facts.schema.json", "ReportContribution", value)


def test_accounting_conditions_only_refer_to_retained_accounting_fields():
    def checked_condition(node, allowed):
        if isinstance(node, list):
            for value in node:
                checked_condition(value, allowed)
        elif isinstance(node, dict):
            assert set(node.get("required", ())) <= allowed
            assert set(node.get("properties", ())) <= allowed
            for key in ("allOf", "oneOf", "anyOf", "not", "if", "then", "else"):
                if key in node:
                    checked_condition(node[key], allowed)

    for name, definition in schema_document("business-facts.schema.json")["$defs"].items():
        if name.startswith("Accounting") and "properties" in definition:
            allowed = set(definition["properties"])
            for key in ("allOf", "oneOf", "anyOf", "not", "if", "then", "else"):
                if key in definition:
                    checked_condition(definition[key], allowed)
