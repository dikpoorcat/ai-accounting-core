"""Ledger-owned value checks and canonical content."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ai_accounting_modular.contracts.public import (
    ContractError,
    checked_fen,
    parse_fen,
    validate_contract,
)


def fail(code: str, message: str) -> None:
    raise ContractError(code, message)


def unique(values: Iterable[Any], message: str) -> None:
    items = list(values)
    if len(items) != len(set(items)):
        fail("invalid_input", message)


def check_lines(lines: list[dict]) -> None:
    unique((line["line_no"] for line in lines), "分录行号不能重复。")
    if [line["line_no"] for line in lines] != list(range(1, len(lines) + 1)):
        fail("invalid_input", "完整凭证分录须按连续行号排列。")
    debit = credit = 0
    for line in lines:
        d, c = parse_fen(line["debit_fen"]), parse_fen(line["credit_fen"])
        if (d > 0) == (c > 0):
            fail("invalid_input", "每条正式分录只能有一边的正金额。")
        debit, credit = checked_fen(debit + d), checked_fen(credit + c)
    if debit != credit:
        fail("unbalanced_voucher", "正式凭证借贷不平衡。")


def check_parts(resource: dict, *, obligation: bool = False) -> None:
    if "cashflow_parts" not in resource:
        return
    parts = resource["cashflow_parts"]
    keys = [(p["category"], p.get("source_business_id")) for p in parts]
    unique(keys, "同一正式资金分类不能重复。")
    amount = parse_fen(resource["amount_fen"])
    if sum(parse_fen(p["amount_fen"]) for p in parts) != amount:
        fail("invalid_input", "正式资金分类合计必须等于所属金额。")
    if obligation and amount == 0 and parts:
        fail("invalid_input", "零额义务的分类必须为空。")


def check_plan(plan: dict) -> None:
    validate_contract("business-facts.schema.json", "PostingPlan", plan)
    slot = plan["result_slot"]
    if plan["target"]["result_slot"] != slot or plan["basis"]["result_slot"] != slot:
        fail("invalid_input", "正式目标、采用依据与计划结果槽必须一致。")
    if plan["target"]["kind"] != plan["basis"]["kind"]:
        fail("invalid_input", "正式目标与采用依据的业务类型必须一致。")
    if plan["basis"]["period"] != plan["accounting_period"]:
        fail("invalid_input", "采用依据月份与计划核算月份不一致。")
    check_lines(plan["lines"])
    for name in ("obligations", "cost_lots"):
        unique((item["slot"] for item in plan[name]), "一份完整结果内公开资源槽不能重复。")
        for item in plan[name]:
            if item["business_id"] != plan["business_id"]:
                fail("invalid_input", "正式资源必须属于本计划业务。")
    for item in plan["obligations"]:
        check_parts(item, obligation=True)
    for item in plan["fund_movements"]:
        check_parts(item)
        if item["actual_on"][:7] != plan["accounting_period"]:
            fail("invalid_input", "真实资金实际日必须属于核算月。")
    for name in ("settlements", "cost_consumptions"):
        for item in plan[name]:
            if item["consumer"]["business_id"] != plan["business_id"]:
                fail("invalid_input", "清偿或成本消耗必须属于本计划业务。")
    for item in plan["obligation_reclassifications"]:
        if item["owner_business_id"] != plan["business_id"]:
            fail("invalid_input", "重分类必须属于本计划业务。")
    unique((x["settlement_id"] for x in plan["settlements"]), "清偿身份不能重复。")
    unique((x["relation_id"] for x in plan["obligation_reclassifications"]), "重分类身份不能重复。")
    unique((x["movement_id"] for x in plan["fund_movements"]), "真实资金身份不能重复。")
    unique((x["public_slot"] for x in plan["fund_movements"]), "真实资金公开槽不能重复。")
    unique(
        ((x["statement"], x["line"], x["detail_code"]) for x in plan["report_contributions"]),
        "正式报表贡献不能重复。",
    )
    expected_proof = agency_proof(plan["business_id"], plan["target"], plan["basis"])
    expected = [] if expected_proof is None else [expected_proof]
    if plan["agency_proofs"] != expected:
        fail("invalid_input", "代理清偿证明必须由已采用的合作实际收付目标唯一产生。")


def agency_proof(business_id, target, basis):
    kind = target["kind"]
    if kind not in ("investment.cooperation_collection", "investment.cooperation_payment"):
        return None
    values = target["values"]
    if parse_fen(values["company_amount_fen"]) == 0:
        return None
    proof = {
        "business_id": business_id,
        "public_slot": "agency",
        "source": values["source"],
        "direction": "inflow" if kind.endswith("collection") else "outflow",
        "party_id": values["party_id"],
        "operator_id": values["operator_id"],
        "amount_fen": values["company_amount_fen"],
        "period": basis["period"],
    }
    if "actual_on" in values:
        proof["actual_on"] = values["actual_on"]
    return proof
