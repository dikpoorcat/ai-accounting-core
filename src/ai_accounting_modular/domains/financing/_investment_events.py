"""Replay a scoped candidate holding from typed current facts and opening lots."""

from contextlib import closing
from copy import deepcopy
from decimal import Decimal, localcontext

from ._common import amount, checked, fail, missing, total
from ._investment_coupon_rights import acquired_coupons, holding_coupon_dates
from ._investment_lots import ActiveLots
from ._investment_math import Event, Lot, quantity, quantity_total, rebase, reference_key, rounded
from ._investment_order import ordered_facts
from ._investment_rules import (
    separate_income_character,
    validate_adopted_terms,
    validate_income_character,
)

PREFIX = "investment."
KINDS = tuple(
    PREFIX + name
    for name in (
        "terms",
        "acquire",
        "income",
        "dispose",
        "capital_return",
        "reinvest",
        "unit_change",
        "loss",
        "debt_interest",
        "holding_change",
        "market_estimate",
        "unit_change_basis",
    )
)
EVENT_KINDS = frozenset(KINDS) - {PREFIX + "terms", PREFIX + "unit_change_basis"}
STATEFUL_KINDS = frozenset(
    PREFIX + name
    for name in (
        "acquire",
        "dispose",
        "capital_return",
        "reinvest",
        "unit_change",
        "loss",
        "debt_interest",
    )
)
POSITION_KINDS = STATEFUL_KINDS | {PREFIX + "holding_change", PREFIX + "market_estimate"}


def terms_result(fact, context):
    terms_id = fact["data"]["terms_id"]
    result = context.get_result(terms_id, "main")
    if result is None or result.target["kind"] != PREFIX + "terms":
        fail("投资事实必须采用本公司已登记的明确投资条款正式结果。")
    if result.basis["period"] > fact["period"]:
        fail("投资事项不能早于其采用条款的生效月份。")
    validate_adopted_terms(result, terms_id)
    return result


def opening_lots(terms_id, context, *, opening_refs):
    result = []
    for reference in opening_refs:
        adopted = context.get_result(reference["business_id"], reference["slot"])
        if adopted is None:
            fail("投资期初批次缺少同单已声明的正式结果。")
        for item in adopted.basis["adopted_quantities"]:
            if item["type"] == "investment_lot" and item["terms_id"] == terms_id:
                result.append(Lot.from_wire(item))
    return result


def _find(lots, source):
    return lots.find(source)


def _select(lots, fact, terms, origins):
    data, selected = fact["data"], []
    required = quantity(data["quantity"])
    if required <= 0:
        fail("投资取得或退出数量必须大于零。", "invalid_input")
    explicit = data.get("lot_allocations")
    if terms["cost_method"] == "specific_identification":
        if not explicit:
            missing(
                fact,
                "/data/lot_allocations",
                "该持仓的真实合同要求指定批次，须从交易资料确定本次退出来源。",
            )
        seen = set()
        for item in explicit:
            lot, used = _find(lots, item["source"]), quantity(item["quantity"])
            key = reference_key(lot.source)
            if key in seen:
                fail("同一次投资退出不能重复选择同一批次。", "invalid_input")
            seen.add(key)
            selected.append((lot, used))
        if quantity_total(used for _, used in selected) != required:
            fail("指定批次的退出数量合计必须等于本次真实退出数量。", "invalid_input")
    else:
        if explicit:
            fail("该持仓已采用先进先出法，不能由每笔输入临时改变选批方法。", "invalid_input")
        with closing(lots.fifo(required, origins)) as fifo:
            for lot in fifo:
                used = min(required, lot.remaining_quantity)
                if used > 0:
                    selected.append((lot, used))
                    required = quantity_total((required, used.copy_negate()))
                if not required:
                    break
        if required:
            fail("投资退出数量超过当前全部正式持仓。")
    return selected


def _acquire(fact, terms, *, reinvest=False):
    data = fact["data"]
    units = quantity(data["quantity"])
    if units <= 0 or terms["quantity_unit"] == "ownership_fraction" and units > 1:
        fail("投资实际取得数量不在该计量单位的允许范围内。", "invalid_input")
    if terms["quantity_unit"] == "nominal_fen" and units != units.to_integral_value():
        fail("债券票面本金使用整数分。", "invalid_input")
    if terms["quantity_unit"] == "nominal_fen":
        checked(int(units))
    price = (
        amount(data["amount_fen"]) - amount(data["acquisition_fees_fen"])
        if reinvest
        else amount(data["purchase_price_fen"])
    )
    declared = 0 if reinvest else amount(data["separate_declared_income_fen"])
    fees = amount(data["acquisition_fees_fen"])
    cost = total((price, fees, -declared))
    if min(price, cost) < 0 or declared > price:
        fail("投资价款中的独立收益权利不能超过实际取得价款。", "invalid_input")
    coupon_dates = acquired_coupons(fact, terms, units, declared)
    return Lot(
        {"business_id": fact["business_id"], "slot": "cost"},
        data["terms_id"],
        terms["product_id"],
        terms["quantity_unit"],
        units,
        cost,
        cost,
        fact["period"],
        fact.get("effective_on"),
        acquisition_sequence=data.get("transaction_sequence"),
        acquired_coupon_dates=coupon_dates,
        acquisition_origin=data.get("acquisition_origin"),
    )


def _debt_interest(fact, lot, terms, event):
    schedule = terms.get("coupon_schedule", ())
    payment_on = fact["data"]["payment_on"]
    if payment_on in (lot.acquired_coupon_dates or ()) or (
        lot.acquired_on is not None and payment_on <= lot.acquired_on
    ):
        fail(
            "该期利息在取得前或取得当日已经到息，不能再确认持有收益或摊销已结束期间。",
            "invalid_input",
        )
    matches = [item for item in schedule if item["payment_on"] == payment_on]
    if len(matches) != 1 or terms["legal_form"] != "direct_bond":
        fail("债券利息须对应已采用真实债券合同中的唯一付息日。", "invalid_input")
    if payment_on[:7] != fact["period"]:
        fail("债券付息所属月须与本次核算月份相同。", "invalid_input")
    if "effective_on" in fact and fact["effective_on"] != payment_on:
        fail("债券合同付息确认日不能与已知核算日矛盾。", "invalid_input")
    if lot.unit != "nominal_fen":
        fail("债券利息只能采用有据票面本金批次。", "invalid_input")
    face = int(lot.remaining_quantity)
    future = sum(item["payment_on"] >= payment_on for item in schedule)
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        coupon = rounded(Decimal(face) * Decimal(matches[0]["coupon_rate"]))
        amortization = (
            rounded(Decimal(lot.remaining_cost - face - lot.remaining_interest) / Decimal(future))
            if terms["measurement"] == "long_bond_cost"
            else 0
        )
    event.quantity, event.amount = lot.remaining_quantity, coupon
    if lot.remaining_interest > coupon:
        fail("债券已含账面应计利息不能超过本合同付息期的有据票面利息。")
    event.book_change = -amortization - lot.remaining_interest
    event.gain = coupon + event.book_change
    return rebase(
        lot,
        event,
        carrying_after=lot.remaining_cost + event.book_change,
        capitalized_interest_after=0,
    )


def _replay(terms_id, terms, facts, *, initial=(), start_period=None, observe=None, ordered=False):
    """The whole covered fact set is validated before any current state is saved."""
    facts = tuple(facts) if ordered else ordered_facts(facts)
    if any(fact["data"]["terms_id"] != terms_id for fact in facts):
        fail("投资候选事实不能跨持仓混算。", "invalid_input")
    origins = {fact["business_id"]: fact for fact in facts}
    lots, rows, arrangement = ActiveLots(deepcopy(initial)), {}, terms["holding_arrangement"]
    if terms["quantity_unit"] == "ownership_fraction" and lots.quantity > 1:
        fail("同一持仓的期初股权比例合计不能超过100%。", "invalid_input")
    coupons = set()
    for fact in facts:
        data, suffix = fact["data"], fact["kind"].removeprefix(PREFIX)
        event = Event(fact["business_id"])
        if suffix in {"acquire", "reinvest"}:
            event.separate_income_character = separate_income_character(fact, terms)
            lot = _acquire(fact, terms, reinvest=suffix == "reinvest")
            lots.append(lot)
            event.quantity, event.cost = lot.original_quantity, lot.original_cost
            event.book_change = lot.original_cost
            event.amount = (
                amount(data["amount_fen"])
                if suffix == "reinvest"
                else total(
                    (amount(data["purchase_price_fen"]), amount(data["acquisition_fees_fen"]))
                )
            )
            event.created.append(lot.wire())
        elif suffix in {"dispose", "loss"}:
            selections = (
                [(_find(lots, data["source"]), quantity(data["quantity"]))]
                if suffix == "loss"
                else _select(lots, fact, terms, origins)
            )
            for lot, used in selections:
                if (
                    suffix == "dispose"
                    and data["exit_mode"] == "maturity"
                    and terms["legal_form"] == "direct_bond"
                ):
                    final_on = terms["coupon_schedule"][-1]["payment_on"]
                    if (
                        fact["period"] < final_on[:7]
                        or fact.get("effective_on", final_on) < final_on
                    ):
                        fail("合同尚未到期的债券退出须按实际提前转让或赎回办理。", "invalid_input")
                    root = reference_key(lot.origin_source or lot.source)
                    due = [
                        day
                        for day in holding_coupon_dates(lot, terms, start_period=start_period)
                        if day[:7] <= fact["period"]
                    ]
                    if any((root, day) not in coupons for day in due):
                        missing(
                            fact,
                            "/data/exit_mode",
                            "到期退出前须先按已有债券合同完成已到付息日的利息与折溢价核算；"
                            "可以同单办理debt_interest，不等待实际收款，也不能改用到期差价代替。",
                        )
                event.uses.append(lot.consume(used))
            event.quantity = quantity_total(quantity(use["quantity"]) for use in event.uses)
            event.cost = total(amount(use["cost_fen"]) for use in event.uses)
            event.amount = (
                amount(data["recovered_fen"])
                if suffix == "loss"
                else total((amount(data["gross_proceeds_fen"]), -amount(data["disposal_fees_fen"])))
            )
            if event.amount < 0:
                fail("投资处置费用不能超过本次有据处置价款。", "invalid_input")
            event.gain, event.book_change = event.amount - event.cost, -event.cost
            if suffix == "dispose" and "maturity_income" in data:
                maturity = data["maturity_income"]
                if data["exit_mode"] != "maturity" or terms["legal_form"] == "direct_bond":
                    fail(
                        "到期收益组成只用于非直接债券的实际到期结算，债券仍按合同付息核算。",
                        "invalid_input",
                    )
                validate_income_character(terms, maturity["character"])
                income = amount(maturity["amount_fen"])
                if income > event.amount:
                    fail("发行人明确的到期收益不能超过本次实际净结算总额。", "invalid_input")
                if "recognized_income_source" in maturity:
                    event.gain -= income
        elif suffix == "income":
            if terms["legal_form"] == "direct_bond":
                fail(
                    "直接债券利息须通过debt_interest采用合同计算，不能以手填收益金额绕过。",
                    "invalid_input",
                )
            validate_income_character(terms, data["income_character"])
            event.amount = event.gain = amount(data["amount_fen"])
        elif suffix in {"capital_return", "unit_change", "debt_interest"}:
            lot = _find(lots, data["source"])
            if suffix == "capital_return":
                event.amount = amount(data["amount_fen"])
                if event.amount > lot.remaining_cost:
                    fail("有据返本不能超过该批次当前账面成本。")
                event.quantity, event.book_change = lot.remaining_quantity, -event.amount
                successor = rebase(lot, event, carrying_after=lot.remaining_cost - event.amount)
            elif suffix == "unit_change":
                event.quantity = quantity(data["quantity_after"])
                if lot.unit == "nominal_fen" and event.quantity != lot.remaining_quantity:
                    fail(
                        "债券份额拆分不能改变票面本金；真实本金退出须使用相应退出事实。",
                        "invalid_input",
                    )
                successor = rebase(lot, event, quantity_after=event.quantity)
            else:
                root = reference_key(lot.origin_source or lot.source)
                coupon_key = root, data["payment_on"]
                if coupon_key in coupons:
                    fail("同一债券批次的同一合同付息日不能重复确认。", "invalid_input")
                if any(
                    (root, day) not in coupons
                    for day in holding_coupon_dates(lot, terms, start_period=start_period)
                    if day < data["payment_on"]
                ):
                    missing(
                        fact,
                        "/data/payment_on",
                        "须先按合同完成前序已到息期间的核算，可以同单补齐；"
                        "不能把漏记期间的折溢价移入本次付息期。",
                    )
                coupons.add(coupon_key)
                successor = _debt_interest(fact, lot, terms, event)
            lots.append(successor)
        elif suffix == "holding_change":
            if terms["measurement"] != "other_financial_cost":
                fail("当前只有其他金融成本模型按有据持有安排办理流动性重分类。", "invalid_input")
            if data["effective_from"][:7] != fact["period"]:
                fail("投资持有安排变更必须在有据生效月份办理。", "invalid_input")
            event.previous_arrangement, arrangement = arrangement, data["holding_arrangement"]
            event.amount = lots.cost
        elif suffix == "market_estimate":
            if data["as_of_on"][:7] != fact["period"]:
                fail("投资市场估值须属于资料明确的估值月份。", "invalid_input")
            event.amount = amount(data["estimated_value_fen"])
        lots.synchronize(event.uses)
        rows[fact["business_id"]] = event
        if terms["quantity_unit"] == "ownership_fraction" and lots.quantity > 1:
            fail("同一持仓的现有股权比例合计不能超过100%。", "invalid_input")
        if observe is not None:
            observe(fact, event, lots, arrangement)
    return rows, tuple(lots), arrangement


def replay(terms_id, terms, facts, *, initial=(), start_period=None, observe=None, ordered=False):
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        return _replay(
            terms_id,
            terms,
            facts,
            initial=initial,
            start_period=start_period,
            observe=observe,
            ordered=ordered,
        )
