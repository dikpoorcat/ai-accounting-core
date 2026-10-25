"""Actual-date principal reductions, with no repayment-result dependency cycle."""

from collections import defaultdict
from copy import deepcopy
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

from ai_accounting_modular.contracts.public import ResourceCutoff, parse_rate

from ._common import (
    adopt_obligation,
    amount,
    base_plan,
    checked,
    fact_error,
    fail,
    format_fen,
    line,
    missing,
    obligation,
    total,
)


def interval(fact):
    data = fact["data"]
    start, end = date.fromisoformat(data["start_on"]), date.fromisoformat(data["end_before"])
    if (
        end <= start
        or start.isoformat()[:7] != fact["period"]
        or (end - timedelta(days=1)).isoformat()[:7] != fact["period"]
    ):
        fail("计息区间须含首不含末，且全部天数属于核算月。", "invalid_input")
    return start, end


def principal_basis(fact, context):
    ref = fact["data"]["principal_source"]
    (definition,) = context.resource_definitions([ref], "obligation")
    ob = definition.value
    if (
        ob["direction"] != "payable"
        or ob["category"] != "loan_principal"
        or ob["account_code"] not in {"2001", "2501"}
    ):
        fact_error(
            fact,
            "/data/principal_source",
            "计息来源必须是机构放款或期初贷款本金，不能将股东往来套入机构利息。",
            error="invalid_input",
            code="incompatible_source_result",
        )
    source = context.get_result(definition.source["business_id"], definition.source["result_slot"])
    if source is None:
        fact_error(
            fact,
            "/data/principal_source",
            "所引用的本金来源尚无正式采用结果，请检查上游核算。",
            error="dependency_conflict",
            code="result_not_ready",
        )
    quantities = source.basis["adopted_quantities"]
    principals = [x for x in quantities if x["type"] == "loan_principal" and x["source"] == ref]
    if len(principals) != 1:
        fact_error(
            fact,
            "/data/principal_source",
            "所引用的本金正式结果未提供唯一的loan_principal采用数量，需要修正来源结果。",
            error="dependency_conflict",
            code="incompatible_source_result",
        )
    principal = principals[0]
    terms = [
        x
        for x in quantities
        if x["type"] == "loan_terms" and x["contract_id"] == principal["contract_id"]
    ]
    if len(terms) != 1:
        fact_error(
            fact,
            "/data/principal_source",
            "所引用的本金正式结果未提供对应loan_terms合同条款，需要修正来源结果。",
            error="dependency_conflict",
            code="incompatible_source_result",
        )
    terms = terms[0]
    if (
        terms["lender_id"] != ob.get("party_id")
        or principal["outstanding_fen"] != ob["amount_fen"]
        or principal["end_before"] != terms["matures_on"]
    ):
        fact_error(
            fact,
            "/data/principal_source",
            "本金正式结果中的本金、贷款方或合同期限互相矛盾，需要核对来源结果。",
            error="dependency_conflict",
            code="inconsistent_fact",
        )
    return definition, principal, terms


def repayment_events(fact, context, principal):
    """Read typed real-payment facts; final candidate checks their valid allocation."""
    ref, events, sources = fact["data"]["principal_source"], defaultdict(list), set()
    related = {
        item["business_id"]: item
        for relation in ("settlement", "calculation")
        for item in context.related_facts(ref["business_id"], relation, period_to=fact["period"])
    }
    for other in related.values():
        data = other["data"]
        if other["kind"] == "money.movement" and data["type"] == "settlement":
            allocations = data["allocations"]
            actual = other["effective_on"]
            if data["direction"] != "outflow":
                # The money owner rejects a wrong-direction allocation atomically.
                continue
        elif other["kind"] == "operations.reimbursement":
            allocations = data.get("allocations", data.get("sources", ()))
            if not any(x["source"] == ref for x in allocations):
                continue
            if data["type"] != "personal_payment":
                missing(
                    other,
                    "/data/type",
                    "当前变体只说明公司接受债务，计息还需要有据的实际代还日；"
                    "有真实付款资料时使用personal_payment，不重复确认已经给出的接受日期。",
                    code="actual_payment_basis_required",
                )
            actual = other["effective_on"]
        else:
            continue
        paid = total(amount(x["amount_fen"]) for x in allocations if x["source"] == ref)
        if not paid or actual >= fact["data"]["end_before"]:
            continue
        if actual < principal["start_on"]:
            fail("本金实际偿还不能早于本金来源的计息起点。")
        events[actual].append(paid)
        sources.update(other["evidence_ids"])
    return {day: total(values) for day, values in events.items()}, tuple(sorted(sources))


def segments(principal, rate, denominator, start, end, events):
    """Round each actual principal segment exactly; no per-fen iteration."""
    reductions = sorted((date.fromisoformat(day), value) for day, value in events.items())
    outstanding = max(0, principal - total(value for day, value in reductions if day <= start))
    opening = outstanding
    changes = [(day, value) for day, value in reductions if start < day < end]
    result, current = [], start
    with localcontext() as context:
        context.prec = 80
        for until, paid in [*changes, (end, 0)]:
            days = (until - current).days
            exact = Decimal(outstanding) * rate * days / denominator
            rounded = checked(int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP)))
            result.append((current.isoformat(), until.isoformat(), outstanding, rounded, exact))
            outstanding = max(0, outstanding - paid)
            current = until
        return opening, result, sum((x[4] for x in result), Decimal(0))


def interest(fact, context):
    start, end = interval(fact)
    definition, principal, terms = principal_basis(fact, context)
    if start.isoformat() < principal["start_on"] or end.isoformat() > terms["matures_on"]:
        fail("计息区间不能越过有据本金起点或合同到期日。")
    events, source_ids = repayment_events(fact, context, principal)
    opening, parts, exact = segments(
        amount(principal["outstanding_fen"]),
        parse_rate(terms["annual_rate"]),
        360 if terms["day_count"] == "actual_360" else 365,
        start,
        end,
        events,
    )
    value = total(x[3] for x in parts)
    plan = base_plan(
        fact,
        context,
        {
            "principal_fen": format_fen(opening),
            "interest_fen": format_fen(value),
            "actual_days": (end - start).days,
            "annual_rate": terms["annual_rate"],
            "unrounded_interest": format(exact, "f"),
        },
    )
    adopt_obligation(plan, definition)
    plan["basis"]["adopted_quantities"].append(deepcopy(terms))
    for first, last, outstanding, _, _ in parts:
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "loan_principal",
                "source": deepcopy(fact["data"]["principal_source"]),
                "contract_id": principal["contract_id"],
                "outstanding_fen": format_fen(outstanding),
                "start_on": first,
                "end_before": last,
            }
        )
    for source in context.resolve_sources(source_ids):
        if source not in plan["basis"]["source_locations"]:
            plan["basis"]["source_locations"].append(source)
    line(plan, "560301", debit=value, summary="按真实未偿本金分段计提利息")
    line(plan, "2231", credit=value, party=terms["lender_id"], summary="确认机构贷款应付利息")
    obligation(
        plan,
        "interest",
        terms["lender_id"],
        "payable",
        value,
        "2231",
        "interest",
        "interest_payments",
    )
    plan["report_contributions"].append(
        {
            "statement": "profit",
            "line": 19,
            "detail_code": "finance_interest",
            "amount_fen": format_fen(value),
        }
    )
    return plan


def validate_interest(plan, context):
    for responsibility in plan["businesses"]:
        fact = responsibility.get("fact")
        if fact is None or fact["kind"] != "financing.interest_period":
            continue
        first, last = interval(fact)
        ref = fact["data"]["principal_source"]
        for other in context.related_facts(ref["business_id"], "calculation"):
            if (
                other["business_id"] == fact["business_id"]
                or other["kind"] != "financing.interest_period"
                or other["data"]["principal_source"] != ref
            ):
                continue
            other_first, other_last = interval(other)
            if first < other_last and other_first < last:
                fail("同一本金来源的计息区间不能重叠。")
        definition, principal, _ = principal_basis(fact, context)
        events, _ = repayment_events(fact, context, principal)
        # Distinguish genuine repayment facts from other nonmoney uses. A known
        # repayment cannot silently leave a different formal principal balance.
        for day in sorted(
            {first.isoformat(), *[x for x in events if first.isoformat() < x < last.isoformat()]}
        ):
            (window,) = context.resource_windows([ref], "obligation", ResourceCutoff(day[:7], day))
            factual = max(
                0,
                amount(definition.value["amount_fen"])
                - total(value for actual, value in events.items() if actual <= day),
            )
            minimum = max(0, window.remaining_min_fen)
            maximum = max(0, window.remaining_max_fen)
            if minimum != maximum:
                missing(
                    fact,
                    "/data/principal_source",
                    "本金非资金变动的先后会改变计息，需要真实清偿日期。",
                    precision=("day",),
                )
            if minimum != factual:
                fail("计息采用的实际未偿本金与完整正式清偿结果不一致。")
