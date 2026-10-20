"""Synthetic cooperation facts and public result reads shared by owner tests."""

from uuid import uuid4

from ._support import BANK, COUNTERPARTY, OWNER, SOURCE, fact, get, request

PROJECT = "78000000-0000-4000-8000-000000000001"


def contract():
    return fact(
        "investment.cooperation_contract",
        {
            "profile": "direct_joint_operation_cash_labor_v1",
            "project_id": PROJECT,
            "operator_id": OWNER,
            "company_initial_commitment_fen": "100",
            "company_revenue_expense_share": "0.5",
            "contract_source": {"source_id": SOURCE, "location": "合成固定份额合作合同"},
        },
    )


def submitted(company, *facts):
    result = request(company, *facts)
    result["objects"].append(
        {
            "object_id": PROJECT,
            "kind": "investment_product",
            "name": "合成共同经营",
            "expected_seq": 0,
            "data": {"category": "cooperation"},
        }
    )
    return result


def funding(agreement, value="100", day="2026-01-03"):
    return fact(
        "investment.cooperation_funding",
        {
            "contract_id": agreement["business_id"],
            "purpose": "initial",
            "amount_fen": value,
            "funds_account_id": BANK,
            "actual_on": day,
        },
    )


def position(app, company, agreement, month="2026-01"):
    business = get(app, company, agreement["business_id"])["businesses"][0]
    return next(
        result["target"]["values"]
        for result in business["results"]
        if result["target"]["result_slot"] == "position:" + month
    )


def expense(agreement, *, project="20", company="10", day="2026-01-05"):
    return fact(
        "operations.expense",
        {
            "creditor_id": COUNTERPARTY,
            "creditor_kind": "supplier",
            "amount_fen": company,
            "expense_class": "service",
            "cooperation_id": agreement["business_id"],
            "project_total_fen": project,
        },
        actual=day,
    )


def agency_payment(agreement, source, *, project="20", company=None, day="2026-01-06"):
    data = {
        "contract_id": agreement["business_id"],
        "source": {"business_id": source["business_id"], "slot": "primary"},
        "project_amount_fen": project,
    }
    if company is not None:
        data["company_amount_fen"] = company
    return fact("investment.cooperation_payment", data, actual=day)


def policy():
    return fact(
        "taxes.vat_policy",
        {
            "policy": {
                "version": "synthetic-cooperation",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "official_url": "https://example.gov.cn/synthetic-rule",
            },
            "nominal_rate": "0.03",
            "tax_base_rate": "0.01",
            "payable_rate": "0.01",
            "form_version": "small_taxpayer_2026",
            "threshold_fen": "0",
            "threshold_period": "month",
            "threshold_operator": "at_or_below",
            "exemption_scope": "eligible_non_excluded_sales",
            "special_invoice_may_waive_exemption": True,
            "special_invoice_may_waive_rate_reduction": True,
        },
    )


def sale(agreement, rule, *, day="2026-01-05"):
    return fact(
        "operations.service_sale",
        {
            "customer_id": COUNTERPARTY,
            "gross_fen": "101",
            "vat": {
                "policy_id": rule["business_id"],
                "exemption_eligible": False,
                "invoice_kind": "ordinary",
                "waive_exemption": False,
                "waive_rate_reduction": False,
            },
            "tax_point": {
                "type": "explicit",
                "tax_period": day[:7],
                "source": {"source_id": SOURCE},
            },
            "cooperation_id": agreement["business_id"],
            "project_total_fen": "200",
        },
        period=day[:7],
        actual=day,
    )


def collection(agreement, sold, *, day="2026-01-06"):
    return fact(
        "investment.cooperation_collection",
        {
            "contract_id": agreement["business_id"],
            "source": {"business_id": sold["business_id"], "slot": "primary"},
            "project_amount_fen": "200",
        },
        period=day[:7],
        actual=day,
    )


def receipt(agreement, value, *, kind="profit", origin="project_pool", day="2026-01-10"):
    return fact(
        "investment.cooperation_distribution",
        {
            "contract_id": agreement["business_id"],
            "origin": origin,
            "receipt_kind": kind,
            "amount_fen": str(value),
            "funds_account_id": BANK,
            "actual_on": day,
        },
        period=day[:7],
    )


def operator(agreement, value, *, into=False, profit=False, day="2026-01-11"):
    return fact(
        "investment.cooperation_operator_transfer",
        {
            "contract_id": agreement["business_id"],
            "direction": "into_pool" if into else "from_pool",
            "purpose": "working_capital"
            if into
            else "profit"
            if profit
            else "working_capital_return",
            "amount_fen": str(value),
        },
        period=day[:7],
        actual=day,
    )


def command(company, *facts):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "create", "expected_seq": 0, "fact": item} for item in facts],
    }
