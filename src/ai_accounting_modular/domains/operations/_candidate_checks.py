"""Whole-submission checks read complete economic balances without changing plans."""

from ai_accounting_modular.contracts.public import ContractError, ResourceCutoff

from ._common import amount, issue, reject, sum_amounts
from ._overpayment_pool import claims_for, pool_ranges


def _resource_day_issues(window, context):
    need_excess = window.certain.remaining_fen <= 0 or (
        window.excess_reclassified_max_fen > window.certain.money_settled_fen
    )
    need_consumption = window.certain.remaining_fen > 0
    causes = {}
    for use in window.uncertain_uses:
        is_excess = use.kind == "reclassification" and use.value["mode"] == "reclassify_excess"
        if (is_excess and not need_excess) or (not is_excess and not need_consumption):
            continue
        business_id = use.source["business_id"]
        cause = context.get_fact(business_id)
        if cause is None:
            reject("影响追偿时点的非资金变动缺少当前事实。")
        problem = issue(
            cause,
            "/effective_on",
            "同月先后会改变追偿是否成立，需要有来源的实际日。",
            precision=("day",),
        )
        problem["code"] = "resource_day_required"
        causes[business_id] = problem
    if not causes:
        reject("追偿时点存在无法核对的资源先后关系。")
    return causes


def _validate_overpayment_pool(fact, postings, context):
    source, occupied, ambiguous = fact["data"]["source"], [], []
    expected = pool_ranges(fact, context)
    for claim in claims_for(fact, context):
        posting = postings.get((claim["business_id"], "main"))
        result = context.get_result(claim["business_id"], "main") if posting is None else None
        if posting is None and result is None:
            reject("追偿池缺少当前完整main核算结果。", "invalid_input")
        basis = posting["basis"] if posting is not None else result.basis
        ranges = sorted(
            (amount(item, "from_fen"), amount(item, "to_fen"))
            for item in basis["adopted_quantities"]
            if item["type"] == "cumulative_range" and item["source"] == source
        )
        expected_ranges = [(span.before, span.after) for span in expected[claim["business_id"]]]
        if ranges != expected_ranges or sum_amounts(end - start for start, end in ranges) != amount(
            claim["data"]
        ):
            reject("追偿必须保全本笔实际采用的全部互不重叠现金区间。")
        occupied.extend(ranges)
        cutoff = ResourceCutoff(claim["period"], claim.get("effective_on"))
        (window,) = context.resource_windows([source], "obligation", cutoff)
        paid = window.certain.money_settled_fen
        if context.actual_settled_fen(source, cutoff) != paid:
            reject("追偿采用的真实付款累计与正式资金清偿结果不一致。")
        if any(start < 0 or end <= start or end > paid for start, end in ranges):
            reject("追偿区间不能借用其经济截止后才发生的现金。")
        if window.remaining_min_fen > 0 or window.certain.excess_reclassified_fen > paid:
            reject("截至该笔追偿时，实际超付不足以支持已经成立的追偿责任。")
        if window.remaining_max_fen > 0 or window.excess_reclassified_max_fen > paid:
            ambiguous.append(window)
    ordered = sorted(occupied)
    if any(right[0] < left[1] for left, right in zip(ordered, ordered[1:], strict=False)):
        reject("同一原债的不同追偿不能重复占用实际现金区间。")
    if ambiguous:
        causes = {}
        for window in ambiguous:
            causes.update(_resource_day_issues(window, context))
        raise ContractError(
            "needs_information",
            "同月非资金变动的先后会影响本次追偿成立。",
            fact_issues=[causes[key] for key in sorted(causes)],
        )


def validate_candidate(plan, context):
    postings = {(item["business_id"], item["result_slot"]): item for item in plan["plans"]}
    validated = set()
    for responsibility in plan["businesses"]:
        fact = responsibility.get("fact")
        if fact is None:
            continue
        kind, data = fact["kind"], fact["data"]
        if kind not in {"operations.pass_through_return", "operations.overpayment"}:
            continue
        cutoff = ResourceCutoff(fact["period"], fact.get("effective_on"))
        if kind == "operations.pass_through_return":
            collection, remittance = context.resource_states(
                [
                    {"business_id": data["pass_through_id"], "slot": "collection"},
                    {"business_id": data["pass_through_id"], "slot": "remittance"},
                ],
                "obligation",
                cutoff,
            )
            disposed = sum_amounts(
                (
                    remittance.money_settled_fen,
                    remittance.offset_settled_fen,
                    remittance.agency_settled_fen,
                    remittance.transferred_out_fen,
                )
            )
            if disposed > collection.money_settled_fen:
                reject("截至退款责任成立时，累计退款责任及已转付金额不能超过真实已收款。")
        else:
            key = data["source"]["business_id"], data["source"]["slot"]
            if key not in validated:
                _validate_overpayment_pool(fact, postings, context)
                validated.add(key)
