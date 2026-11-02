"""Controlled journal and public obligation effects of investment events."""

from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from ._common import adopt, amount, fail, line, obligation
from ._investment_rules import account_code


def _income_obligation(
    plan, party, value, *, slot="income", dividend=False, cashflow="investment_income"
):
    obligation(
        plan,
        slot,
        party,
        "receivable",
        value,
        "1131" if dividend else "1132",
        "investment",
        cashflow,
    )
    plan["obligations"][-1]["allowed_settlements"] = ["money", "offset", "transfer"]


def post(fact, context, plan, event, term_values, arrangement):
    data = fact["data"]
    suffix, code = fact["kind"].removeprefix("investment."), account_code(term_values, arrangement)
    party, product = data.get("counterparty_id"), term_values["product_id"]
    if suffix in {"acquire", "reinvest"}:
        line(plan, code, debit=event.cost, object_id=product, summary="确认有据投资取得成本")
        if suffix == "acquire":
            declared = amount(data["separate_declared_income_fen"])
            dividend = event.separate_income_character != "interest"
            if declared:
                line(
                    plan,
                    "1131" if dividend else "1132",
                    debit=declared,
                    party=party,
                    summary="单独确认取得价款中的既有收益权利",
                )
                _income_obligation(plan, party, declared, slot="declared_income", dividend=dividend)
            line(plan, "2241", credit=event.amount, party=party, summary="确认投资取得付款责任")
            obligation(
                plan,
                "primary",
                party,
                "payable",
                event.amount,
                "2241",
                "investment",
                "investment_acquisition",
            )
        else:
            reference = data["income_source"]
            (definition,) = context.resource_definitions([reference], "obligation")
            original = definition.value
            if (
                original["direction"] != "receivable"
                or original["party_id"] != party
                or original["account_code"] not in {"1131", "1132"}
            ):
                fail("红利转投只能采用同一实际交易方的正式投资收益应收。", "invalid_input")
            adopt(plan, definition.source)
            line(
                plan,
                original["account_code"],
                credit=event.amount,
                party=party,
                summary="有据收益权利直接转投",
            )
            plan["settlements"].append(
                {
                    "settlement_id": str(
                        uuid5(NAMESPACE_URL, "investment:reinvest:" + fact["business_id"])
                    ),
                    "source": deepcopy(reference),
                    "consumer": {"business_id": fact["business_id"], "slot": "main"},
                    "period": fact["period"],
                    "amount_fen": str(event.amount),
                    "method": "offset",
                }
            )
    elif suffix in {"dispose", "loss"}:
        line(plan, code, credit=event.cost, object_id=product, summary="转出本次实际退出账面成本")
        maturity = data.get("maturity_income")
        income_amount = amount(maturity["amount_fen"]) if maturity is not None else 0
        principal_amount = event.amount - income_amount
        line(
            plan,
            "1221",
            debit=principal_amount,
            party=party,
            summary="确认投资退出有据本金净款应收",
        )
        if principal_amount:
            obligation(
                plan,
                "primary",
                party,
                "receivable",
                principal_amount,
                "1221",
                "investment",
                "investment_recovery",
            )
        if income_amount and "recognized_income_source" not in maturity:
            dividend = maturity["character"] != "interest"
            line(
                plan,
                "1131" if dividend else "1132",
                debit=income_amount,
                party=party,
                summary="按发行人实际结算组成确认到期收益权利",
            )
            _income_obligation(plan, party, income_amount, dividend=dividend)
        loss_code = (
            "571106"
            if term_values["measurement"] == "long_bond_cost"
            else "571107"
            if term_values["measurement"] == "long_equity_cost"
            else "571108"
        )
        income_code = loss_code if suffix == "loss" and event.gain < 0 else "5111"
        line(
            plan,
            income_code,
            debit=max(-event.gain, 0),
            credit=max(event.gain, 0),
            summary="确认投资实际退出损益",
        )
    elif suffix in {"income", "debt_interest"}:
        dividend = suffix == "income" and data["income_character"] != "interest"
        receivable = "1131" if dividend else "1132"
        line(plan, receivable, debit=event.amount, party=party, summary="确认有据投资收益权利")
        _income_obligation(plan, party, event.amount, dividend=dividend)
        if event.book_change:
            line(
                plan,
                code,
                debit=max(event.book_change, 0),
                credit=max(-event.book_change, 0),
                object_id=product,
                summary="按合同付息期摊销债券折溢价",
            )
        line(
            plan,
            "5111",
            debit=max(-event.gain, 0),
            credit=max(event.gain, 0),
            summary="确认本期投资收益",
        )
    elif suffix == "capital_return":
        line(plan, code, credit=event.amount, object_id=product, summary="有据返本减少投资账面成本")
        line(plan, "1221", debit=event.amount, party=party, summary="确认返本权利")
        obligation(
            plan,
            "primary",
            party,
            "receivable",
            event.amount,
            "1221",
            "investment",
            "investment_recovery",
        )
    elif suffix == "holding_change":
        before = account_code(term_values, event.previous_arrangement)
        if before != code:
            line(plan, before, credit=event.amount, object_id=product, summary="转出原持有安排分类")
            line(
                plan,
                code,
                debit=event.amount,
                object_id=product,
                summary="按真实生效安排重分类投资",
            )
    return plan
