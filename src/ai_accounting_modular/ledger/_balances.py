"""Capacity checks use complete selected resources, never page subtotals."""

from __future__ import annotations

import json
from collections import defaultdict

from ai_accounting_modular.contracts.public import checked_fen, format_fen

from ._timing import ResultTimes
from ._values import agency_proof, fail


def sum_amounts(values):
    try:
        return checked_fen(sum(checked_fen(value) for value in values))
    except ValueError:
        fail("invalid_input", "资源累计金额超出有符号64位整数分范围。")


def obligation_remaining(amount, totals):
    return sum_amounts(
        (
            amount,
            -totals.get("money", 0),
            -totals.get("offset", 0),
            -totals.get("agency", 0),
            -totals.get("transfer", 0),
            totals.get("reclassify_excess", 0),
        )
    )


def obligation_balances(repo, heads, *, through=None):
    head_by_id = {h["result_id"]: h for h in heads}
    source_ids = {h["business_id"] for h in heads}
    obligations = repo.children("obligation", list(head_by_id))
    settlements = repo.incoming("settlement", source_ids, through=through)
    relations = repo.incoming("obligation_reclassification", source_ids, through=through)
    totals = defaultdict(lambda: defaultdict(int))
    for row in settlements:
        key = row["source_business_id"], row["source_public_slot"]
        totals[key][row["method"]] = sum_amounts((totals[key][row["method"]], row["amount_fen"]))
    for row in relations:
        key = row["source_business_id"], row["source_public_slot"]
        totals[key][row["mode"]] = sum_amounts((totals[key][row["mode"]], row["amount_fen"]))
    results = []
    seen = set()
    for ob in obligations:
        key = head_by_id[ob["result_id"]]["business_id"], ob["public_slot"]
        if key in seen:
            fail("dependency_conflict", "同业务的公开义务槽不唯一。")
        seen.add(key)
        total = totals[key]
        remaining = obligation_remaining(ob["amount_fen"], total)
        results.append((ob, dict(total), remaining))
    return results


def unique_movements(repo, movement_ids):
    movements = repo.movements(movement_ids)
    if len(movements) != len({m["movement_id"] for m in movements}):
        fail("dependency_conflict", "同一真实资金身份不能被多个当前结果重复采用。")
    return movements


def validate_capacities(repo, business_ids, retired_keys=()):
    """Check touched producers and all incoming consumers; no company-wide scan."""
    heads = repo.current_metadata(business_ids)
    by_id = {h["result_id"]: h for h in heads}
    times = ResultTimes(repo, heads)
    obligations = {}
    for ob, total, remaining in obligation_balances(repo, heads):
        key = by_id[ob["result_id"]]["business_id"], ob["public_slot"]
        obligations[key] = ob
        if remaining < 0:
            fail("dependency_conflict", "实际清偿超过当前义务，请同单明确差额追收或相应关系。")
        if total.get("reclassify_excess", 0):
            before = remaining - total["reclassify_excess"]
            if before >= 0 or remaining != 0:
                fail("dependency_conflict", "超付重分类只能将真实负余额恢复为零。")
    settlements = repo.incoming("settlement", business_ids)
    relations = repo.incoming("obligation_reclassification", business_ids)
    movement_ids = {r["movement_id"] for r in settlements if r["method"] == "money"}
    movements = unique_movements(repo, movement_ids)
    movement_map = {m["movement_id"]: m for m in movements}
    allocated = defaultdict(int)
    for row in repo.settlements_by_movements(movement_ids):
        allocated[row["movement_id"]] = sum_amounts(
            (allocated[row["movement_id"]], row["amount_fen"])
        )
    agency_totals = defaultdict(int)
    for row in settlements:
        key = row["source_business_id"], row["source_public_slot"]
        ob = obligations.get(key)
        if ob is None:
            fail("dependency_conflict", "仍被清偿的义务不能退出。")
        times.require_available(ob["result_id"], row["period"])
        if row["method"] not in json.loads(ob["allowed_settlements_json"]):
            fail("dependency_conflict", "该义务不允许此清偿方式。")
        expected_direction = "in" if ob["direction"] == "receivable" else "out"
        if row["method"] == "money":
            move = movement_map.get(row["movement_id"])
            if move is None or move["result_id"] != row["result_id"]:
                fail("dependency_conflict", "资金清偿须采用本业务的真实资金发生。")
            if move["direction"] != expected_direction:
                fail("dependency_conflict", "实际资金方向与义务清偿方向不符。")
            if move["party_id"] is not None and move["party_id"] != ob["party_id"]:
                fail("dependency_conflict", "实际资金交易方与被清偿义务不符。")
            if row["actual_on"] is not None and row["actual_on"] != move["actual_on"]:
                fail("dependency_conflict", "清偿实际日与同一真实资金实际日不符。")
            times.require_available(ob["result_id"], row["period"], move["actual_on"])
            if allocated[move["movement_id"]] > move["amount_fen"]:
                fail("dependency_conflict", "同一实际资金不能重复或超额清偿。")
        elif row["method"] == "agency":
            adopted = repo.result(row["result_id"])
            proof = agency_proof(
                adopted["business_id"],
                json.loads(adopted["target_json"]),
                json.loads(adopted["basis_json"]),
            )
            if (
                proof is None
                or row["agency_business_id"] != proof["business_id"]
                or key != (proof["source"]["business_id"], proof["source"]["slot"])
                or ob["category"] != "trade"
                or ob["party_id"] != proof["party_id"]
                or proof["direction"] != {"in": "inflow", "out": "outflow"}[expected_direction]
            ):
                fail("dependency_conflict", "代理清偿与正式证明的原义务、交易方或方向不符。")
            agency_totals[row["result_id"]] = sum_amounts(
                (agency_totals[row["result_id"]], row["amount_fen"])
            )
            if agency_totals[row["result_id"]] > int(proof["amount_fen"]):
                fail("dependency_conflict", "代理清偿超出正式证明金额。")
        if row["method"] != "money":
            times.require_available(ob["result_id"], row["period"], times.usage_day(row))
    for result_id, total in agency_totals.items():
        adopted = repo.result(result_id)
        proof = agency_proof(
            adopted["business_id"],
            json.loads(adopted["target_json"]),
            json.loads(adopted["basis_json"]),
        )
        if total != int(proof["amount_fen"]):
            fail("dependency_conflict", "代理清偿合计须等于正式证明金额。")
    for row in relations:
        source = obligations.get((row["source_business_id"], row["source_public_slot"]))
        target_key = row["target_business_id"], row["target_public_slot"]
        target = obligations.get(target_key)
        if target is None:
            target_heads = repo.current_metadata([target_key[0]])
            found = repo.children("obligation", [h["result_id"] for h in target_heads])
            target = next((o for o in found if o["public_slot"] == target_key[1]), None)
        if source is None or target is None:
            fail("dependency_conflict", "义务重分类必须有完整的当前来源和目标。")
        owner = times.head(row["result_id"])
        times.require_available(source["result_id"], owner["business_period"], times.usage_day(row))
        if (source["direction"] == target["direction"]) != (row["mode"] == "transfer"):
            fail("dependency_conflict", "转债方向或超付追收方向不符。")
        if row["mode"] == "reclassify_excess" and source["party_id"] != target["party_id"]:
            fail("dependency_conflict", "超付追收必须对应同一真实交易方。")
        if row["mode"] == "transfer" and "transfer" not in json.loads(
            source["allowed_settlements_json"]
        ):
            fail("dependency_conflict", "该义务不允许非资金转债。")
    target_totals = defaultdict(int)
    for row in repo.transfers_to(business_ids):
        key = row["target_business_id"], row["target_public_slot"]
        target_totals[key] = sum_amounts((target_totals[key], row["amount_fen"]))
    for key, total in target_totals.items():
        if key not in obligations or obligations[key]["amount_fen"] != total:
            fail("dependency_conflict", "转债或追收来源合计须与新义务金额一致。")
    lots = {}
    for row in repo.children("cost_lot", list(by_id)):
        key = by_id[row["result_id"]]["business_id"], row["public_slot"]
        if key in lots:
            fail("dependency_conflict", "同业务的公开成本槽不唯一。")
        lots[key] = row
    consumed = defaultdict(int)
    for row in repo.incoming("cost_consumption", business_ids):
        key = row["source_business_id"], row["source_public_slot"]
        consumed[key] = sum_amounts((consumed[key], row["amount_fen"]))
        if key in lots:
            source = lots[key]
            times.require_available(source["result_id"], row["period"], times.usage_day(row))
    for key, amount in consumed.items():
        if key not in lots or amount > lots[key]["amount_fen"]:
            fail("dependency_conflict", "来源成本不存在或已被超额消耗。")
    for key in retired_keys:
        if key in obligations or key in lots:
            continue
        if any(
            (r["source_business_id"], r["source_public_slot"]) == key
            for r in settlements + relations
        ):
            fail("dependency_conflict", "仍有有效使用的公开资源不能退出。")


def balance_wire(ob, totals, remaining, reference):
    result = {
        "reference": {"business_id": reference["business_id"], "slot": ob["public_slot"]},
        "adopted_result": reference,
        "direction": ob["direction"],
        "amount_fen": format_fen(ob["amount_fen"]),
        "money_settled_fen": format_fen(totals.get("money", 0)),
        "offset_settled_fen": format_fen(totals.get("offset", 0)),
        "agency_settled_fen": format_fen(totals.get("agency", 0)),
        "transferred_out_fen": format_fen(totals.get("transfer", 0)),
        "excess_reclassified_fen": format_fen(totals.get("reclassify_excess", 0)),
        "remaining_fen": format_fen(remaining),
    }
    if ob["party_id"] is not None:
        result["party_id"] = ob["party_id"]
    return result
