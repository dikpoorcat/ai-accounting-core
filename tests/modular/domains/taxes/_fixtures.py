"""Explicit test data, with no expected number calculated by production code."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from ai_accounting_modular.contracts.public import EvaluatedSlot, PretaxView, TaxSaleView
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.taxes.public import create_taxes


def uid(name):
    return str(uuid5(NAMESPACE_URL, "synthetic-taxes/" + name))


SOURCE, CUSTOMER, ACCOUNT = uid("source"), uid("customer"), uid("bank")
LOCATION = {"source_id": SOURCE, "location": "合成税务资料"}
POLICY_ID, SURTAX_ID = uid("vat-policy"), uid("surtax-policy")
OFFICIAL = "https://shanghai.chinatax.gov.cn/zzzb/zcwj/202602/t479283.html"


def fact(name, kind, data, period="2026-01", **extra):
    return {
        "business_id": uid(name),
        "kind": kind,
        "period": period,
        "evidence_ids": [SOURCE],
        "data": deepcopy(data),
        **extra,
    }


def policy(*, rate="0.03", threshold="10000000", operator="strictly_below"):
    return fact(
        "vat-policy",
        "taxes.vat_policy",
        {
            "policy": {
                "version": "synthetic-1",
                "official_url": OFFICIAL,
                "effective_from": "2023-01-01",
                "effective_to": "2027-12-31",
            },
            "nominal_rate": "0.03",
            "tax_base_rate": rate,
            "payable_rate": rate,
            "form_version": "small_taxpayer_2026",
            "threshold_fen": threshold,
            "threshold_period": "month",
            "threshold_operator": operator,
            "exemption_scope": "eligible_non_excluded_sales",
            "special_invoice_may_waive_exemption": True,
            "special_invoice_may_waive_rate_reduction": True,
        },
    )


def surtax(*, zero=False):
    return fact(
        "surtax-policy",
        "taxes.surtax_policy",
        {
            "policy": {
                "version": "synthetic-1",
                "official_url": OFFICIAL,
                "effective_from": "2023-01-01",
                "effective_to": "2027-12-31",
            },
            "urban_rate": "0" if zero else "0.07",
            "education_rate": "0" if zero else "0.03",
            "local_education_rate": "0" if zero else "0.02",
            "payable_fraction": "0.5",
        },
    )


def sale(name="sale", gross="103000", period="2026-01", *, eligible=False, tax_point=None):
    return fact(
        name,
        "operations.service_sale",
        {
            "customer_id": CUSTOMER,
            "gross_fen": gross,
            "vat": {
                "policy_id": POLICY_ID,
                "exemption_eligible": eligible,
                "invoice_kind": "ordinary",
                "waive_exemption": False,
                "waive_rate_reduction": False,
            },
            "tax_point": tax_point
            or {"type": "explicit", "tax_period": period, "source": LOCATION},
        },
        period,
    )


def assessment(name="assessment", period="2026-01", *, policies=True, credits=()):
    data = {
        "type": "vat",
        "start_month": period,
        "end_month": period,
        "filing_profile": {
            "taxpayer_type": "small_scale",
            "form_scope": "nonpilot",
            "source": LOCATION,
        },
    }
    if policies:
        data.update(vat_policy_id=POLICY_ID, surtax_policy_id=SURTAX_ID)
    if credits:
        data["credit_ids"] = list(credits)
    return fact(name, "taxes.assessment", data, period)


def actual_vat(assessment_fact, vat="0", *, period=None, **fields):
    data = assessment_fact["data"]
    return fact(
        "actual/" + assessment_fact["business_id"],
        "taxes.actual_declaration",
        {
            "type": "vat",
            "assessment_id": assessment_fact["business_id"],
            "start_month": data["start_month"],
            "end_month": data["end_month"],
            "basis_type": "filed_return",
            "source": LOCATION,
            "assessed_vat_fen": vat,
            "urban_fen": "0",
            "education_fen": "0",
            "local_education_fen": "0",
            **fields,
        },
        period or assessment_fact["period"],
    )


def tax_sale(
    name, net, vat, *, period="2026-01", rate="0.03", eligible=False, bucket="standard", ranges=None
):
    return TaxSaleView(
        {
            "source_business_id": uid(name),
            "tax_period": period,
            "net_sales_fen": net,
            "vat_fen": vat,
            "nominal_rate": "0.03",
            "tax_base_rate": rate,
            "payable_rate": rate,
            "invoice_kind": "ordinary",
            "exemption_eligible": eligible,
            "filing_bucket": bucket,
            "ranges": ranges or [],
        },
        source_ref(uid(name)),
    )


def source_ref(business_id):
    return {
        "business_id": business_id,
        "result_id": uid("result/" + business_id),
        "result_slot": "main",
        "result_generation": 1,
        "content_digest": "1" * 64,
    }


class Context:
    evaluation_period_to = "2027-12"
    read_context = SimpleNamespace(start_period="2026-01")

    def __init__(self, *facts, sales=(), profit="2000000"):
        self.facts = {row["business_id"]: deepcopy(row) for row in facts}
        self.sales = tuple(sales)
        self.profit = profit
        self.results = {}
        self.cash_receipts = {}

    def get_fact(self, identity):
        return deepcopy(self.facts.get(identity))

    def get_object(self, identity):
        return {
            "object_id": identity,
            "kind": "person" if identity == uid("person") else "organization",
        }

    def resolve_sources(self, ids):
        return [{"source_id": identity} for identity in ids]

    def related_facts(self, producer_id, relation_kind, **_):
        rows = []
        for row in self.facts.values():
            data = row["data"]
            if producer_id in (data.get("assessment_id"), data.get("original_assessment_id")):
                rows.append(row)
            elif producer_id in data.get("credit_ids", ()):
                rows.append(row)
        return tuple(deepcopy(rows))

    def facts_in_scope(self, kinds, start, end):
        return tuple(
            deepcopy(row)
            for row in self.facts.values()
            if row["kind"] in kinds and start <= row["period"] <= end
        )

    def tax_sales(self, start, end):
        return tuple(view for view in self.sales if start <= view.sale["tax_period"] <= end)

    def pre_tax(self, start, end):
        return PretaxView(
            {
                "period_from": start,
                "period_to": end,
                "operating_revenue": {"state": "known", "amount_fen": "3000000"},
                "operating_cost": {"state": "known", "amount_fen": "1000000"},
                "profit_before_tax": {"state": "known", "amount_fen": self.profit},
                "basis_digest": "2" * 64,
            }
        )

    def actual_settled_fen(self, source, cutoff):
        return self.cash_receipts.get((source["business_id"], source["slot"]), 0)

    def get_result(self, identity, slot):
        key = identity, slot
        if key not in self.results:
            source = self.get_fact(identity)
            if source is None:
                return None
            owner = create_taxes() if source["kind"].startswith("taxes.") else create_operations()
            plan = owner.evaluate_slot(identity, slot, self)
            self.results[key] = EvaluatedSlot(source_ref(identity), plan["target"], plan["basis"])
        return self.results[key]


def evidence():
    text = "合成税务软件验收资料，与真实公司无关。"
    return {
        "source_id": SOURCE,
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": "2026-01",
    }


def income_inputs():
    tax_policy = fact(
        "income-policy",
        "taxes.income_tax_policy",
        {
            "policy": deepcopy(policy()["data"]["policy"]),
            "standard_rate": "0.25",
            "small_profit_rate": "0.20",
            "small_profit_taxable_fraction": "0.25",
            "small_profit_limit_fen": "300000000",
        },
    )
    basis = fact(
        "prepayment",
        "taxes.prepayment_basis",
        {
            "tax_year": 2026,
            "quarter_end": "2026-03",
            "collection_method": "actual_profit",
            "treatment": "small_low_profit",
            "basis_source": LOCATION,
            "eligibility_source": LOCATION,
            "loss_deduction_fen": "0",
            "special_treatments": [],
        },
        "2026-03",
    )
    current = fact(
        "income",
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": "computed_provision",
            "tax_year": 2026,
            "quarter_end": "2026-03",
            "policy_id": tax_policy["business_id"],
            "prepayment_basis_id": basis["business_id"],
        },
        "2026-03",
    )
    return current, basis, tax_policy


def confirmed_income(name, end, cumulative, *, period=None):
    return fact(
        name,
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": "confirmed_provision",
            "tax_year": int(end[:4]),
            "tax_period_end": end,
            "cumulative_tax_fen": cumulative,
            "source": LOCATION,
        },
        period or end,
    )


def income_actual(current, cumulative, prepaid, due, *, period=None):
    return fact(
        "actual/" + current["business_id"],
        "taxes.actual_declaration",
        {
            "type": "income_tax",
            "assessment_id": current["business_id"],
            "tax_year": current["data"]["tax_year"],
            "tax_period_end": current["data"].get(
                "quarter_end", current["data"].get("tax_period_end")
            ),
            "basis_type": "filed_return",
            "source": LOCATION,
            "cumulative_tax_fen": cumulative,
            "prepaid_tax_fen": prepaid,
            "tax_due_fen": due,
        },
        period or current["period"],
    )
