"""Current cooperation cash rights and open trades, without fictional history."""

from copy import deepcopy

from ._common import amount, fail, format_fen, total
from ._cooperation_common import PREFIX
from ._cooperation_math import interval_share


def _snapshot(fact, member, contract):
    data = member["data"]
    company, operator = (
        amount(data["company_cash_right_fen"]),
        amount(data["operator_cash_right_fen"]),
    )
    receivable = payable = operator_receivable = operator_payable = 0
    trades = []
    identities, entry_keys = set(), set()
    rate = contract.target["values"]["company_revenue_expense_share"]
    for trade in sorted(data["open_trades"], key=lambda item: item["entry_key"]):
        identity = (trade["side"], trade["counterparty_id"], trade["original_business_reference"])
        if identity in identities or trade["entry_key"] in entry_keys:
            fail("合作期初不能重复登记同一真实未清业务或资源身份。", "invalid_input")
        identities.add(identity)
        entry_keys.add(trade["entry_key"])
        original, cleared = (
            amount(trade["original_project_amount_fen"]),
            amount(trade["project_cleared_before_fen"]),
        )
        offset = amount(trade["project_range_from_fen"])
        company_original = interval_share(0, original, rate, offset=offset)
        company_cleared = amount(trade["company_cleared_before_fen"])
        if not 0 <= cleared < original or not 0 <= company_cleared <= min(
            cleared, company_original
        ):
            fail("合作期初原项目金额及建账前实际清偿组成不一致。", "invalid_input")
        project_remaining, company_remaining = (
            original - cleared,
            company_original - company_cleared,
        )
        if company_remaining > project_remaining:
            fail("合作期初公司未清份额不能超过项目真实未清金额。", "invalid_input")
        if trade["side"] == "revenue":
            receivable = total((receivable, company_remaining))
            operator_receivable = total(
                (operator_receivable, project_remaining - company_remaining)
            )
        else:
            payable = total((payable, company_remaining))
            operator_payable = total((operator_payable, project_remaining - company_remaining))
        trades.append(
            {
                **trade,
                "project_remaining_fen": project_remaining,
                "company_remaining_fen": company_remaining,
                "remaining_range_from_fen": total((offset, cleared)),
            }
        )
    company_capital = total((company, receivable, -payable))
    operator_capital = total((operator, operator_receivable, -operator_payable))
    for key, value in (
        ("company_capital_fen", company_capital),
        ("operator_capital_fen", operator_capital),
    ):
        if key in data and amount(data[key]) != value:
            fail("合作期初资本断言与当前现金权利及未清往来不一致。", "invalid_input")
    operator_due, company_due = max(-operator, 0), max(-company, 0)
    pool, held = total((company, operator)), total((max(company, 0), -operator_due))
    if pool < 0 or held < 0:
        fail("合作期初项目现金或公司代管权利不能为负，不能补造借款。", "invalid_input")
    if data["status"] == "settled" and any(
        (company, operator, receivable, payable, operator_receivable, operator_payable)
    ):
        fail("已结清合作期初仍有实际现金或未清权利义务。", "invalid_input")
    quantity = {
        "type": "cooperation_opening",
        "source": {"business_id": fact["business_id"], "slot": "member:" + member["member_id"]},
        "contract_id": data["contract_id"],
        "as_of_period": fact["period"],
        "status": data["status"],
        **{
            key: format_fen(value)
            for key, value in (
                ("company_cash_right_fen", company),
                ("operator_cash_right_fen", operator),
                ("company_capital_fen", company_capital),
                ("operator_capital_fen", operator_capital),
                ("pool_fen", pool),
                ("held_fen", held),
                ("operator_due_fen", operator_due),
                ("company_due_fen", company_due),
            )
        },
    }
    return quantity, trades


def _balance(member, code, value, *, party, product, credit=False):
    return {
        "type": "opening_balance",
        "member_id": member["member_id"],
        "account_code": code,
        "party_id": party,
        "object_id": product,
        "debit_fen": "0" if credit else format_fen(value),
        "credit_fen": format_fen(value) if credit else "0",
    }


def opening_member(fact, member, context):
    data = member["data"]
    contract = context.get_result(data["contract_id"], "main")
    if contract is None or contract.target["kind"] != PREFIX + "contract":
        fail("合作期初须引用已采用的合作合同。", "invalid_input")
    if contract.basis["period"] > fact["period"]:
        fail("合作期初不能采用建账之后才成立的合同。", "invalid_input")
    snapshot, trades = _snapshot(fact, member, contract)
    terms = contract.target["values"]
    operator, project = terms["operator_id"], terms["project_id"]
    balances = [
        _balance(
            member, code, amount(snapshot[key]), party=operator, product=project, credit=credit
        )
        for key, code, credit in (
            ("held_fen", "122106", False),
            ("operator_due_fen", "122107", False),
            ("company_due_fen", "224106", True),
        )
        if amount(snapshot[key])
    ]
    obligations, quantities = [], [snapshot]
    for trade in trades:
        revenue = trade["side"] == "revenue"
        code, direction = ("1122", "receivable") if revenue else ("2202", "payable")
        source = {
            "business_id": fact["business_id"],
            "slot": "member:" + member["member_id"] + ":trade:" + trade["entry_key"],
        }
        value = trade["company_remaining_fen"]
        if value:
            balances.append(
                _balance(
                    member,
                    code,
                    value,
                    party=trade["counterparty_id"],
                    product=project,
                    credit=not revenue,
                )
            )
        obligations.append(
            {
                **source,
                "party_id": trade["counterparty_id"],
                "direction": direction,
                "category": "trade",
                "amount_fen": format_fen(value),
                "account_code": code,
                "allowed_settlements": ["money", "offset", "transfer", "agency"],
                "business_period": fact["period"],
                "cashflow_weights": [
                    {
                        "category": "customer_receipts"
                        if revenue
                        else "materials_services_payments",
                        "weight": "1",
                    }
                ],
            }
        )
        quantities.extend(
            [
                {
                    "type": "obligation",
                    "source": source,
                    "party_id": trade["counterparty_id"],
                    "direction": direction,
                    "account_code": code,
                    "amount_fen": format_fen(value),
                },
                {
                    "type": "cooperation_trade",
                    "contract_id": data["contract_id"],
                    "source": source,
                    "side": trade["side"],
                    "project_amount_fen": format_fen(trade["project_remaining_fen"]),
                    "company_net_amount_fen": format_fen(value),
                    "project_range_from_fen": format_fen(trade["remaining_range_from_fen"]),
                },
            ]
        )
    return {
        "member_id": member["member_id"],
        "balances": balances,
        "obligations": obligations,
        "cost_lots": [],
        "adopted_quantities": quantities,
        "source_results": [deepcopy(contract.source)],
    }


def opening_snapshot(contract_id, context, through):
    """Resolve only the related opening, never inspect another domain's private storage."""
    found = []
    for fact in context.related_facts(contract_id, "calculation", period_to=through):
        if fact["kind"] != "opening.package":
            continue
        result = context.get_result(fact["business_id"], "main")
        if result is None:
            fail("合作位置需要的期初包尚无正式采用结果。")
        for quantity in result.basis["adopted_quantities"]:
            if quantity["type"] == "cooperation_opening" and quantity["contract_id"] == contract_id:
                found.append((deepcopy(quantity), result))
    if len(found) > 1:
        fail("同一合作合同不能有多个当前期初位置。")
    return found[0] if found else (None, None)


def apply_snapshot(computed, snapshot):
    if snapshot is None:
        return computed
    for key in (
        "company_cash_right_fen",
        "operator_cash_right_fen",
        "company_capital_fen",
        "operator_capital_fen",
    ):
        computed[key] = total((computed[key], amount(snapshot[key])))
    company, operator = computed["company_cash_right_fen"], computed["operator_cash_right_fen"]
    computed.update(
        pool_fen=total((company, operator)),
        operator_due_fen=max(-operator, 0),
        company_due_fen=max(-company, 0),
        held_fen=total((max(company, 0), -max(-operator, 0))),
    )
    return computed
