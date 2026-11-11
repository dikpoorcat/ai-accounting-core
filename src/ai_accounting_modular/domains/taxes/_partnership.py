"""Sourced partnership annual allocations and previously included distributions."""

from copy import deepcopy

from ai_accounting_modular.domains.financing.public import (
    investment_accounting_terms,
    investment_economic_events,
)

from ._common import adopt_source, amount, missing, plan_for, reject, total

KIND = "taxes.partnership_allocation"


def adopt_inclusions(fact, context, plan, actual):
    rows = fact["data"].get("partnership_inclusions", ())
    if not rows:
        return
    if fact["data"].get("method") != "annual_settlement":
        reject("合伙所得实际纳入证明只能用于年度所得税采用。", "invalid_input")
    if actual is None:
        missing(
            fact,
            "/data/partnership_inclusions",
            "已纳入合伙所得的证明只能由有源实际年度所得税采用承接，不能以分配声明或季度暂计代替。",
        )
    seen = set()
    for row in sorted(rows, key=lambda value: value["allocation_id"]):
        if row["allocation_id"] in seen:
            reject("年度实际税务采用不能重复包含同一合伙分配依据。", "invalid_input")
        seen.add(row["allocation_id"])
        result = context.get_result(row["allocation_id"], "main")
        if result is None or result.target["kind"] != KIND:
            reject("年度合伙所得采用必须引用正式合伙年度分配依据。", "invalid_input")
        values = result.target["values"]
        included, retained = (
            amount(row["included_income_fen"]),
            amount(row["taxed_retained_income_fen"]),
        )
        if values["tax_year"] != fact["data"]["tax_year"] or included != amount(
            values["annual_inclusion_fen"]
        ):
            reject("实际年度纳入的合伙所得与原年度分配依据不一致。", "invalid_input")
        if retained > included:
            reject("已纳入的留存收益不能超过该次实际纳入所得。", "invalid_input")
        adopt_source(plan, result.source)
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "partnership_tax_inclusion",
                "allocation_source": deepcopy(result.source),
                "investment_id": values["investment_id"],
                "tax_year": values["tax_year"],
                "included_income_fen": str(included),
                "taxed_retained_income_fen": str(retained),
            }
        )
    plan["basis"]["input"]["partnership_inclusions"] = sorted(
        deepcopy(list(rows)), key=lambda value: value["allocation_id"]
    )


def _previous_taxed(fact, context, plan):
    claims = fact["data"].get("previously_taxed_distributions", ())
    if not claims:
        return 0
    income_limits = {
        (row["source"]["business_id"], row["source"]["slot"]): amount(row["amount_fen"])
        for row in fact["data"]["distributions"]
    }
    income_used, capacity_used, capacities = {}, {}, {}
    previous_total = 0
    for claim in claims:
        income = claim["income_source"]["business_id"], claim["income_source"]["slot"]
        value = amount(claim["amount_fen"])
        income_used[income] = total((income_used.get(income, 0), value))
        if income_used[income] > income_limits.get(income, 0):
            reject("以前年度已纳入所得的本年分回不能超过对应本年正式收益。", "invalid_input")
        proof = context.get_result(claim["tax_assessment_id"], "main")
        if (
            proof is None
            or proof.target["kind"] != "taxes.assessment"
            or proof.basis["input"].get("method") != "annual_settlement"
            or proof.target["values"]["adopted_basis"] != "actual"
        ):
            reject(
                "此前已纳入所得须引用实际年度所得税采用，分配资料本身不能作证明。", "invalid_input"
            )
        year = proof.target["values"]["tax_year"]
        if year >= fact["data"]["tax_year"]:
            reject("以前年度分回关系必须指向更早税年的实际采用。", "invalid_input")
        quantities = [
            row
            for row in proof.basis["adopted_quantities"]
            if row["type"] == "partnership_tax_inclusion"
            and row["allocation_source"]["business_id"] == claim["allocation_id"]
            and row["investment_id"] == fact["data"]["investment_id"]
            and row["tax_year"] == year
        ]
        if len(quantities) != 1:
            reject("实际年度税务结果没有采用所指合伙权益的留存所得来源。", "invalid_input")
        key = claim["tax_assessment_id"], claim["allocation_id"]
        capacities[key] = amount(quantities[0]["taxed_retained_income_fen"])
        capacity_used[key] = total((capacity_used.get(key, 0), value))
        adopt_source(plan, proof.source)
        previous_total = total((previous_total, value))
    for (tax_id, allocation_id), used in capacity_used.items():
        related = context.related_facts(tax_id, "reference")
        for other in related:
            if other["kind"] != KIND or other["business_id"] == fact["business_id"]:
                continue
            used = total(
                (
                    used,
                    *(
                        amount(claim["amount_fen"])
                        for claim in other["data"].get("previously_taxed_distributions", ())
                        if claim["tax_assessment_id"] == tax_id
                        and claim["allocation_id"] == allocation_id
                    ),
                )
            )
        if used > capacities[tax_id, allocation_id]:
            reject(
                "本年及其他年度的分回累计超过此前实际纳入的留存所得，不能反复抵扣。",
                "invalid_input",
            )
    return previous_total


def allocation(fact, context):
    data = fact["data"]
    if data["tax_year"] != int(fact["period"][:4]):
        reject("合伙税务分配依据须属于其明确税年。", "invalid_input")
    terms = context.get_result(data["investment_id"], "main")
    if (
        terms is None
        or terms.target["kind"] != "investment.terms"
        or investment_accounting_terms(terms.target["values"])["right_type"]
        != "partnership_interest"
    ):
        reject("合伙税务分配依据须引用本企业实际持有的合伙权益正式条款。", "invalid_input")
    seen, book_income, results = set(), 0, [terms]
    ordered_distributions = sorted(
        data["distributions"], key=lambda row: (row["source"]["business_id"], row["source"]["slot"])
    )
    for distribution in ordered_distributions:
        reference = distribution["source"]
        key = reference["business_id"], reference["slot"]
        if key in seen or reference["slot"] != "main":
            reject("合伙收益核对须引用不重复的收益主结果。", "invalid_input")
        seen.add(key)
        result = context.get_result(*key)
        events = () if result is None else investment_economic_events(result.target["values"])
        event = events[0] if len(events) == 1 else None
        if (
            event is None
            or event["terms_id"] != data["investment_id"]
            or event["event_kind"] != "income"
        ):
            reject("合伙已分收益引用不属于同一投资的正式收益。", "invalid_input")
        part = amount(distribution["amount_fen"])
        if part > amount(event["accounting_income_fen"]) or result.basis["period"][:4] != str(
            data["tax_year"]
        ):
            reject("合伙本年账载收益金额或期间与正式来源不一致。", "invalid_input")
        book_income = total((book_income, part))
        results.append(result)
    if book_income != amount(data["distributed_income_fen"]):
        missing(
            fact, "/data/distributions", "合伙本年已分收益须与所引用的本年正式账载收益核对一致。"
        )
    values = {key: deepcopy(value) for key, value in data.items() if key != "source"}
    values["distributions"] = deepcopy(ordered_distributions)
    inclusion = max(0, amount(data["allocated_taxable_income_fen"]))
    values.update(
        annual_inclusion_fen=str(inclusion),
        accounting_income_fen=str(book_income),
        annual_adjustment_fen=str(inclusion - book_income),
    )
    plan = plan_for(fact, context, values)
    plan["basis"]["input"]["distributions"] = deepcopy(ordered_distributions)
    for result in results:
        adopt_source(plan, result.source)
    if "previously_taxed_distributions" in data:
        values["previously_taxed_distribution_fen"] = str(_previous_taxed(fact, context, plan))
        ordered_claims = sorted(
            deepcopy(data["previously_taxed_distributions"]),
            key=lambda row: (
                row["tax_assessment_id"],
                row["allocation_id"],
                row["income_source"]["business_id"],
                row["income_source"]["slot"],
            ),
        )
        values["previously_taxed_distributions"] = ordered_claims
        plan["basis"]["input"]["previously_taxed_distributions"] = deepcopy(ordered_claims)
    plan["rule_refs"][0]["official_urls"] = [
        "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5203416/content.html",
        "https://tianjin.chinatax.gov.cn/11200000000/0500/050002/zxftlist20230403.html",
    ]
    return plan
