"""Adopt only explicit continuation numbers and the exact opening reference."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import canonical_json, parse_fen, validate_contract

from ._facts import KIND, problem, rows, total, validate_fact


def opening_result(fact, context):
    opening_id = fact["data"]["opening_package_id"]
    opening = context.get_result(opening_id, "main")
    if opening is None:
        problem(
            fact,
            "/data/opening_package_id",
            "报表接续需要本公司已经采用或本单已求值的期初包。",
            missing=True,
        )
    if (
        opening.source["business_id"] != opening_id
        or opening.target["kind"] != "opening.package"
        or opening.basis["kind"] != "opening.package"
        or opening.basis["period"] != context.read_context.start_period
        or opening.target["values"]["bookkeeping_start"] != context.read_context.start_period
    ):
        problem(
            fact,
            "/data/opening_package_id",
            "接续所引用的正式结果必须是本公司同一建账起点的期初包。",
        )
    cash = total(
        parse_fen(value["debit_fen"]) - parse_fen(value["credit_fen"])
        for value in opening.basis["adopted_quantities"]
        if value["type"] == "opening_balance" and value["account_code"] in {"1001", "1002", "1012"}
    )
    for column in ("quarter_to_date_fen", "year_to_date_fen"):
        if rows(fact, "prior_cash", column)[22] != cash:
            problem(
                fact,
                "/data/prior_cash",
                "接续现金流的两列期末现金必须等于正式期初包的建账起点货币资金。",
            )
    return opening


def source_locations(fact, context):
    """Resolve every evidence independently, preserving an explicit carry locator."""
    result = []
    for offset in range(0, len(fact["evidence_ids"]), 500):
        result.extend(context.resolve_sources(fact["evidence_ids"][offset : offset + 500]))
    carry = fact["data"].get("income_tax_carry")
    if carry:
        context.resolve_sources([carry["source"]["source_id"]])
        result.append(deepcopy(carry["source"]))
    return [item for _, item in sorted({canonical_json(item): item for item in result}.items())]


def evaluate(fact, context):
    validate_fact(fact, context.read_context.start_period)
    opening = opening_result(fact, context)
    locations = source_locations(fact, context)
    data = deepcopy(fact["data"])
    for name in ("year_beginning_balance", "prior_profit", "prior_cash"):
        data[name].sort(key=lambda row: row["line"])
    quantities = []
    if "income_tax_carry" in data:
        data["income_tax_carry"].pop("source")
        quantities.append({"type": "income_tax_cumulative", **data["income_tax_carry"]})
    plan = {
        "business_id": fact["business_id"],
        "result_slot": "main",
        "accounting_period": fact["period"],
        "calculation_key": KIND,
        "calculation_revision": 1,
        "rule_refs": [{"key": KIND, "revision": 1, "policy_business_ids": [], "official_urls": []}],
        "target": {
            "kind": KIND,
            "result_slot": "main",
            "values": {
                "bookkeeping_start": data["bookkeeping_start"],
                "accounting_standard": data["accounting_standard"],
                "balance_row_count": 53,
                "profit_row_count": 32,
                "cash_row_count": 22,
            },
        },
        "basis": {
            "kind": KIND,
            "result_slot": "main",
            "period": fact["period"],
            "input": data,
            "source_results": [deepcopy(opening.source)],
            "adopted_quantities": quantities,
            "policies": [],
            "source_locations": locations,
        },
        **{
            name: []
            for name in (
                "lines",
                "obligations",
                "settlements",
                "obligation_reclassifications",
                "cost_lots",
                "cost_consumptions",
                "fund_movements",
                "agency_proofs",
                "tax_sales",
                "report_contributions",
            )
        },
    }
    validate_contract("business-facts.schema.json", "PostingPlan", plan)
    return plan
