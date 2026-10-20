"""Whole-submission checks read complete economic balances without changing plans."""

from ai_accounting_modular.contracts.public import ResourceCutoff

from ._common import amount, reject, sum_amounts


def validate_candidate(plan, context):
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
            (state,) = context.resource_states([data["source"]], "obligation", cutoff)
            if (
                state.remaining_fen != 0
                or state.excess_reclassified_fen != amount(data)
                or not 0 < state.excess_reclassified_fen <= state.money_settled_fen
            ):
                reject("追收必须由该时点真实付款产生，并将当前原债的完整超付差额恰好转为追收。")
