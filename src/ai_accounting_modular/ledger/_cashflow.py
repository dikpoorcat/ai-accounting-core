"""Classify full formal money targets; adoption history subtracts frozen baselines."""

import json
from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    ContractError,
    format_fen,
    parse_fen,
    validate_contract,
)
from ai_accounting_modular.ledger.reporting.public import cash_totals

from ._balances import sum_amounts
from ._financial_cells import add, known

_ROWS = {
    "customer_receipts": (1, 1),
    "other_operating_receipts": (2, 1),
    "materials_services_payments": (3, -1),
    "employee_payments": (4, -1),
    "tax_payments": (5, -1),
    "other_operating_payments": (6, -1),
    "investment_recovery": (8, 1),
    "investment_income": (9, 1),
    "asset_disposal": (10, 1),
    "investment_acquisition": (11, -1),
    "asset_acquisition": (12, -1),
    "loan_receipts": (14, 1),
    "capital_receipts": (15, 1),
    "loan_repayment": (16, -1),
    "interest_payments": (17, -1),
    "profit_distributions": (18, -1),
}


def money_targets(rows):
    """Return per stable movement/category signed report quantities, or None if unknown."""
    groups = defaultdict(list)
    for row in rows:
        if row["transfer_group_id"] is not None:
            groups[row["transfer_group_id"]].append(row)
    result = {}
    for row in rows:
        value = None
        if row["cashflow_parts_json"] is not None:
            parts = json.loads(row["cashflow_parts_json"])
            if (
                len({item["category"] for item in parts}) == len(parts)
                and sum_amounts(parse_fen(item["amount_fen"]) for item in parts)
                == row["amount_fen"]
            ):
                if any(item["category"] == "none" for item in parts):
                    group = groups.get(row["transfer_group_id"], [])
                    if (
                        len(parts) == 1
                        and len(group) == 2
                        and len({v["account_id"] for v in group}) == 2
                        and all(
                            v["cashflow_parts_json"] is not None
                            and json.loads(v["cashflow_parts_json"])
                            == [{"category": "none", "amount_fen": format_fen(v["amount_fen"])}]
                            for v in group
                        )
                        and sum_amounts(
                            v["amount_fen"] * (1 if v["direction"] == "in" else -1) for v in group
                        )
                        == 0
                    ):
                        value = {}
                elif all(item["category"] in _ROWS for item in parts):
                    value = {
                        item["category"]: sum_amounts(
                            (
                                parse_fen(item["amount_fen"])
                                * (1 if row["direction"] == "in" else -1)
                                * _ROWS[item["category"]][1],
                            )
                        )
                        for item in parts
                    }
        result[row["movement_id"], row["public_slot"]] = value
    return result


def cash(selection, first, last, column):
    coefficients = selection.flow(first, last)
    heads = [selection.heads[key] for key in coefficients]
    values = selection.children("result_fund_movement", heads)
    result = selection.prior("cash", first, last)
    for head in heads:
        sign = coefficients[head["result_id"]]
        if values[head["result_id"]]:
            selection.source(head)
        for target in money_targets(values[head["result_id"]]).values():
            if target is None:
                for line in range(1, 21):
                    result[line] = selection.issues.unknown(
                        f"/cash_flow_statement/{line}/{column}",
                        "正式资金分类",
                        "真实资金已保存，但该范围存在尚未确定的正式现金分类或不完整内部划转。",
                        code="cashflow_classification_missing",
                    )
            else:
                for category, amount in target.items():
                    line = _ROWS[category][0]
                    result[line] = add(result[line], known(sign * amount))
    result.update(cash_totals(result))
    return result


def contributions(selection, first, last, business_ids=()):
    result = []
    heads = [
        h
        for h in selection.repo.adoptions_between(first, last)
        if not business_ids or h["business_id"] in business_ids
    ]
    baseline_ids = {h["baseline_result_id"] for h in heads if h["baseline_result_id"]}
    bases = (
        {h["result_id"]: h for h in selection.repo.financial_results(baseline_ids)}
        if baseline_ids
        else {}
    )
    children = selection.children("result_fund_movement", [*heads, *bases.values()])
    for head in heads:
        new = money_targets(children[head["result_id"]])
        old = (
            money_targets(children[head["baseline_result_id"]])
            if head["baseline_result_id"]
            else {}
        )
        for movement, slot in sorted(new.keys() | old.keys()):
            target, baseline = new.get((movement, slot), {}), old.get((movement, slot), {})
            if target is None or baseline is None:
                raise ContractError(
                    "needs_information",
                    "正式现金流明细存在未知分类，不能返回漏项的完整清单。",
                    fact_issues=[
                        {
                            "code": "cashflow_classification_missing",
                            "business_id": head["business_id"],
                            "field": "/fund_movements/cashflow_parts",
                            "meaning": "该笔真实资金的正式分类",
                            "message": "补齐有来源的分类后可读取完整采用明细。",
                            "allowed_precision": ["source"],
                            "reusable_sources": [],
                            "blocking_scope": "none",
                        }
                    ],
                )
            for category in sorted(target.keys() | baseline.keys()):
                amount = sum_amounts((target.get(category, 0), -baseline.get(category, 0)))
                if amount:
                    value = {
                        "business_id": head["business_id"],
                        "movement_id": movement,
                        "public_slot": slot,
                        "adoption_period": head["posting_period"],
                        "source_result": selection.source(head),
                        "kind": "supplement" if head["baseline_result_id"] else "baseline",
                        "category": category,
                        "signed_amount_fen": format_fen(amount),
                    }
                    validate_contract("reporting.schema.json", "CashflowContribution", value)
                    result.append(value)
    return sorted(
        result,
        key=lambda item: (
            item["adoption_period"],
            item["business_id"],
            item["movement_id"],
            item["category"],
        ),
    )
