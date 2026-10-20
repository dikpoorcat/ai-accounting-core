"""Monthly rights are derived results, not a second project ledger."""

from copy import deepcopy
from dataclasses import fields

from ai_accounting_modular.contracts.public import YearMonth

from ._common import adopt_obligation, amount, fail, format_fen, line, missing, total
from ._cooperation_common import PREFIX, adopt_result, cooperation_plan
from ._cooperation_events import inputs, totals
from ._cooperation_math import Totals, position

_TOTAL_KEYS = {
    field.name: field.name.removeprefix("company_")
    if field.name in {"company_funded_fen", "company_received_fen"}
    else field.name
    for field in fields(Totals)
}


def _previous(fact, period, context):
    first = max(fact["period"], context.read_context.start_period)
    if period == first:
        return None
    previous = str(YearMonth.from_ordinal(YearMonth(period).ordinal - 1))
    result = context.get_result(fact["business_id"], "position:" + previous)
    if result is None or result.target["kind"] != PREFIX + "position":
        fail("合作月末位置缺少前月的正式接续结果。")
    return result


def position_plan(fact, result_slot, context):
    period = result_slot.removeprefix("position:")
    previous = _previous(fact, period, context)
    adopted = inputs(
        fact["business_id"],
        context,
        period,
        excluding_consumer=(fact["business_id"], result_slot),
    )
    if any(
        event.period < context.read_context.start_period
        and any(getattr(event.delta, field.name) for field in fields(Totals))
        for event in adopted.events
    ):
        missing(
            fact,
            "/period",
            "开账前已发生的合作经营或出资需要完整位置接续；当前规则只承接开账后新发生的项目，不能默认为零。",
            precision=("source",),
            code="cooperation_opening_required",
        )
    previous_values = previous.target["values"] if previous is not None else {}
    previous_totals = Totals(
        **{field.name: amount(previous_values.get(field.name, "0")) for field in fields(Totals)}
    )
    monthly = totals(event for event in adopted.events if event.period == period)
    current = previous_totals.plus(
        **{field.name: getattr(monthly, field.name) for field in fields(Totals)}
    )
    computed = position(current)
    if computed["pool_fen"] < 0 or computed["held_fen"] < 0:
        fail("截至该月的已知项目现款或公司代管资金不足，不能推定有无来源借款。")
    receivable = total(
        max(state.remaining_fen, 0)
        if state.definition.value["direction"] == "receivable"
        else max(-state.remaining_fen, 0)
        for state in adopted.states
    )
    payable = total(
        max(state.remaining_fen, 0)
        if state.definition.value["direction"] == "payable"
        else max(-state.remaining_fen, 0)
        for state in adopted.states
    )
    if computed["company_capital_fen"] != (
        computed["held_fen"]
        + computed["operator_due_fen"]
        - computed["company_due_fen"]
        + receivable
        - payable
    ):
        fail("合作资本与代管、应补及当前净经营往来不一致。")
    ended = any(
        event.fact is not None and event.fact["kind"] == PREFIX + "exit" for event in adopted.events
    )
    settled = (
        ended
        and not adopted.project_unsettled
        and not any(
            (
                receivable,
                payable,
                *[
                    computed[key]
                    for key in (
                        "pool_fen",
                        "held_fen",
                        "operator_due_fen",
                        "company_due_fen",
                        "company_capital_fen",
                        "operator_capital_fen",
                    )
                ],
            )
        )
    )
    values = {
        "contract_id": fact["business_id"],
        "as_of_period": period,
        "status": "settled" if settled else "operations_ended" if ended else "active",
        **{key: format_fen(value) for key, value in computed.items()},
        **{field.name: format_fen(getattr(current, field.name)) for field in fields(Totals)},
        "company_trade_receivable_net_fen": format_fen(receivable),
        "company_trade_payable_fen": format_fen(payable),
    }
    contract = context.get_result(fact["business_id"], "main")
    accounting_input = {
        "contract_id": fact["business_id"],
        "company_revenue_expense_share": contract.target["values"]["company_revenue_expense_share"],
        "opening": "previous_adopted_position" if previous is not None else "zero_at_start",
        "period_totals": {
            _TOTAL_KEYS[field.name]: format_fen(getattr(monthly, field.name))
            for field in fields(Totals)
        },
        "ending_trade_receivable_net_fen": format_fen(receivable),
        "ending_trade_payable_fen": format_fen(payable),
        "operations_ended": ended,
    }
    position_fact = {**fact, "kind": PREFIX + "position", "period": period}
    position_fact.pop("effective_on", None)
    plan = cooperation_plan(position_fact, context, values, accounting_input=accounting_input)
    plan["result_slot"] = plan["target"]["result_slot"] = plan["basis"]["result_slot"] = result_slot
    adopt_result(plan, contract)
    if previous is not None:
        adopt_result(plan, previous)
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "cooperation_position",
                "position": {
                    "contract_id": fact["business_id"],
                    "as_of_period": previous_values["as_of_period"],
                    **{field.name: previous_values[field.name] for field in fields(Totals)},
                },
            }
        )
    for source in adopted.sources:
        if source not in plan["basis"]["source_results"]:
            plan["basis"]["source_results"].append(deepcopy(source))
    for trade in adopted.trades:
        plan["basis"]["adopted_quantities"].append(deepcopy(trade.quantity))
    for state in adopted.states:
        adopt_obligation(plan, state.definition)
    operator, project = fact["data"]["operator_id"], fact["data"]["project_id"]
    for field, account, increase_debit in (
        ("operator_due_fen", "122107", True),
        ("company_due_fen", "224106", False),
    ):
        delta = computed[field] - amount(previous_values.get(field, "0"))
        if not delta:
            continue
        debit = (delta > 0) == increase_debit
        line(
            plan,
            account,
            debit=abs(delta) if debit else 0,
            credit=0 if debit else abs(delta),
            party=operator,
            object_id=project,
            summary="按当前真实经营结果调整合作应补",
        )
        line(
            plan,
            "122106",
            debit=0 if debit else abs(delta),
            credit=abs(delta) if debit else 0,
            party=operator,
            object_id=project,
            summary="对应调整合作代管资金分类",
        )
    return plan
