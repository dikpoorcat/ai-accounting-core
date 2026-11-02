"""Independent tax inventory: book cost never substitutes for a statutory buy price."""

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, localcontext
from fractions import Fraction

from ai_accounting_modular.contracts.public import ContractError

from ._common import amount, checked, missing_investment_input, reject, rounded, total
from ._investment_pools import ending_lots, partition
from ._investment_tax_lots import month_lots
from ._investment_units import (
    normalize,
    ratio,
    require_comparable,
    rounded_fraction,
    sourced_order,
)
from ._investment_units import (
    quantity_text as exact_quantity_text,
)


def tax_on_gross(value, rate):
    with localcontext() as precision:
        precision.prec = 80
        ratio = Decimal(rate)
        return rounded(Decimal(value) / (1 + ratio) * ratio)


def event_key(source, event_index):
    return (source["business_id"], source["result_slot"], source["content_digest"], event_index)


def allocate_interest_vat(views, tax):
    incomes = sorted(
        (view for view in views if taxable_interest(view.event) and interest_gross(view.event)),
        key=lambda view: event_key(view.source, view.event_index),
    )
    gross = total(interest_gross(view.event) for view in incomes)
    if not gross:
        return []
    result, cumulative, allocated = [], 0, 0
    with localcontext() as precision:
        precision.prec = 80
        for view in incomes:
            cumulative += interest_gross(view.event)
            through = rounded(Decimal(tax) * cumulative / gross)
            result.append(
                {
                    "type": "investment_income_vat",
                    "source": deepcopy(view.source),
                    "event_index": view.event_index,
                    "amount_fen": str(through - allocated),
                }
            )
            allocated = through
    return result


def is_financial_transfer(event):
    if event["event_kind"] != "disposal":
        return False
    instrument = event["tax_facts"]["instrument_type"]
    return (
        instrument not in {"partnership", "unlisted_equity"}
        and event.get("exit_mode") != "maturity"
    )


def taxable_interest(event):
    character = income_character(event)
    if event["tax_facts"]["instrument_type"] in {
        "bank_wealth",
        "contractual_asset_management",
    } and character in {"interest", "market_distribution", "fund_distribution"}:
        if "principal_protected" not in event["tax_facts"]:
            missing_investment_input(
                event,
                event["terms_id"],
                "/data/tax_facts/principal_protected",
                "理财利息的VAT处理须有合同关于本金能否全部收回的约定，不能由产品名或结算收益倒推。",
            )
        return event["tax_facts"]["principal_protected"]
    if character != "interest":
        return False
    if event["tax_facts"]["instrument_type"] in {"government_bond", "local_government_bond"}:
        issued = event["tax_facts"].get("original_issue_on")
        if issued is None:
            missing_investment_input(
                event,
                event["terms_id"],
                "/data/tax_facts/original_issue_on",
                "债券利息的增值税处理需要有来源的原始发行日期。",
            )
        return issued >= "2025-08-08"
    return True


def income_character(event):
    if event["event_kind"] == "disposal" and event.get("exit_mode") == "maturity":
        income = event.get("maturity_income")
        if income is None:
            if event["tax_facts"]["instrument_type"] in {
                "bank_wealth",
                "contractual_asset_management",
            }:
                missing_investment_input(
                    event,
                    event["terms_id"],
                    "/data/maturity_income",
                    "理财到期结算需要发行人有源的本金和收益组成，不能把买入价差猜成利息或默认免税。",
                )
            return None
        if "recognized_income_source" in income:
            return None
        return income["character"]
    return event.get("income_character")


def interest_gross(event):
    if event["event_kind"] == "disposal":
        return amount(event["maturity_income"]["amount_fen"])
    return amount(event["gross_price_fen"])


def _consume(pool, units, view):
    units = ratio(units)
    current = pool.get("quantity")
    if current is None or current <= 0 or units > current:
        missing_investment_input(
            view.event,
            view.source["business_id"],
            "/data/quantity",
            "投资税务退出缺少足够且有来源的持仓份额，不能使用会计成本代替买入价。",
        )
    before = pool.setdefault("interval_quantity", current)
    used = pool.setdefault("interval_consumed", Fraction())
    value = None
    with localcontext() as precision:
        precision.prec = 80
        if pool["vat_basis_fen"] is not None:
            original = pool.setdefault("interval_vat_basis_fen", pool["vat_basis_fen"])
            value = rounded_fraction(original * (used + units) / before) - rounded_fraction(
                original * used / before
            )
            pool["vat_basis_fen"] = checked(pool["vat_basis_fen"] - value)
    pool["quantity"] -= units
    pool["interval_consumed"] += units
    return value


def _reset(pool):
    for key in tuple(pool):
        if key.startswith("interval_"):
            pool.pop(key)


def _acquire(pool, event, factor=Fraction(1)):
    if event["event_kind"] == "opening":
        if "opening_vat_buy_basis_fen" not in event:
            source = event["origin_ref"]
            if source not in pool["vat_basis_unknown_sources"]:
                pool["vat_basis_unknown_sources"].append(deepcopy(source))
        pool["vat_basis_fen"] = (
            None
            if "opening_vat_buy_basis_fen" not in event or pool["vat_basis_fen"] is None
            else total((pool["vat_basis_fen"], amount(event["opening_vat_buy_basis_fen"])))
        )
        pool["quantity"] = (
            None
            if pool.get("quantity") is None
            else pool["quantity"] + ratio(event["quantity"]) * factor
        )
        _reset(pool)
        return
    gross = amount(event["gross_price_fen"])
    pool["vat_basis_fen"] = (
        None if pool["vat_basis_fen"] is None else total((pool["vat_basis_fen"], gross))
    )
    if "quantity" not in event or pool.get("quantity") is None:
        pool["quantity"] = None
    else:
        pool["quantity"] += ratio(event["quantity"]) * factor
    _reset(pool)


def _ordered_events(events, method):
    stock_kinds = {"acquisition", "disposal", "loss", "capital_return", "unit_change"}
    stock_events = [view for view in events if view.event["event_kind"] in stock_kinds]
    kinds = {view.event["event_kind"] for view in stock_events}
    sensitive = (
        method == "moving_weighted_average"
        and "acquisition" in kinds
        and len(kinds) > 1
        or bool(kinds & {"unit_change", "capital_return", "loss"})
        and len(kinds) > 1
    )
    complete_dates = all(view.effective_on is not None for view in stock_events)
    complete_sequence = all("transaction_sequence" in view.event for view in stock_events)
    if sensitive and not complete_dates:
        if not complete_sequence or not _distinct_sequences(stock_events):
            missing_investment_input(
                stock_events[0].event,
                stock_events[0].source["business_id"],
                "/data/transaction_sequence",
                "同一产品的税务均价涉及买卖或持仓调整，需要真实交易日期或完整顺序。",
            )
        opening = [view for view in events if view.event["event_kind"] == "opening"]
        income = [
            view for view in events if view.event["event_kind"] not in stock_kinds | {"opening"}
        ]
        return (
            opening
            + sorted(stock_events, key=lambda view: view.event["transaction_sequence"])
            + income
        )
    if sensitive:
        groups = defaultdict(list)
        for view in stock_events:
            groups[view.effective_on].append(view)
        for group in groups.values():
            if len({view.event["event_kind"] for view in group}) > 1 and (
                not all("transaction_sequence" in view.event for view in group)
                or not _distinct_sequences(group)
            ):
                missing_investment_input(
                    group[0].event,
                    group[0].source["business_id"],
                    "/data/transaction_sequence",
                    "同日影响税务均价的不同交易缺少真实日内顺序，不能按内部编号推定。",
                )
    return sorted(
        events,
        key=lambda view: (
            0 if view.event["event_kind"] == "opening" else 1,
            view.effective_on or view.period,
            view.event.get("transaction_sequence", 0),
            view.source["business_id"],
            view.event_index,
        ),
    )


def _distinct_sequences(events):
    seen = {}
    for view in events:
        number = view.event.get("transaction_sequence")
        group = getattr(view, "unit_group", (view.source["business_id"], view.event_index))
        if number in seen and seen[number] != group:
            return False
        seen[number] = group
    return True


def inventory_month(previous, views, method, *, unit_bases=(), adoptions=None):
    """Consume a single month's explicit formal events and previous tax positions."""
    with localcontext() as precision:
        precision.prec = 80
        return _inventory_month(previous, views, method, unit_bases, adoptions)


def _inventory_month(previous, views, method, unit_bases, adoptions):
    eit_lots_by_product, eit_disposals = month_lots(previous, views)
    previous_by_pool, groups, owners = partition(previous, views, is_financial_transfer)
    eit_lots = ending_lots(eit_lots_by_product, owners)
    pools = {
        pool: {
            "quantity": ratio(row["quantity"]) if "quantity" in row else None,
            "vat_basis_fen": amount(row["vat_basis_fen"]) if "vat_basis_fen" in row else None,
            "vat_basis_unknown_sources": deepcopy(row.get("vat_basis_unknown_sources", [])),
            "unit_basis_unknown_sources": deepcopy(row.get("unit_basis_unknown_sources", [])),
            "vat_unit_segments": deepcopy(row.get("vat_unit_segments", [])),
            "vat_pool_identity": deepcopy(row.get("vat_pool_identity", {})),
        }
        for pool, row in previous_by_pool.items()
    }
    difference, interest, disposals = 0, 0, []
    ordered_pools = sorted(groups, key=lambda pool: tuple(value or "" for value in pool))
    for pool_key in ordered_pools:
        product_id, events = pool_key[0], groups[pool_key]
        pool = pools.setdefault(
            pool_key,
            {
                "quantity": Fraction(),
                "vat_basis_fen": 0,
                "vat_basis_unknown_sources": [],
                "unit_basis_unknown_sources": [],
                "vat_unit_segments": [],
            },
        )
        old = previous_by_pool.get(pool_key, {"product_id": product_id, "eit_lots": []})
        pool["vat_pool_identity"] = {
            field: pool_key[index + 1]
            for index, field in enumerate(
                ("instrument_type", "legal_form", "quantity_unit", "instrument_reference")
            )
            if pool_key[index + 1] is not None
        }
        taxable_exits = any(is_financial_transfer(view.event) for view in events)
        try:
            ordered = _ordered_events(sourced_order(events, unit_bases), method)
            opening_factor, factors, unresolved, segments = normalize(
                old, ordered, unit_bases, adoptions, all_events=views
            )
        except ContractError as error:
            if (
                taxable_exits
                or error.code != "needs_information"
                or not any(view.event["event_kind"] == "unit_change" for view in events)
            ):
                raise
            ordered, factors, opening_factor = events, {}, Fraction(1)
            unresolved = [
                *old.get("unit_basis_unknown_sources", []),
                *(
                    {"business_id": view.source["business_id"], "slot": view.source["result_slot"]}
                    for view in events
                    if view.event["event_kind"] == "unit_change"
                ),
            ]
            segments = [
                {"source": deepcopy(lot["source"]), "quantity": lot["quantity"]}
                for lot in eit_lots.get(pool_key, [])
            ]
        if unresolved:
            if taxable_exits:
                require_comparable(
                    unresolved, next(view for view in events if is_financial_transfer(view.event))
                )
            pool["quantity"] = None
            pool["unit_basis_unknown_sources"], pool["vat_unit_segments"] = unresolved, segments
            for view in ordered:
                event, kind = view.event, view.event["event_kind"]
                if kind in {"acquisition", "opening"}:
                    _acquire(pool, event)
                elif kind == "capital_return" and pool["vat_basis_fen"] is not None:
                    pool["vat_basis_fen"] = checked(
                        pool["vat_basis_fen"] - amount(event["gross_price_fen"])
                    )
                elif kind in {"disposal", "loss"}:
                    pool["vat_basis_fen"] = None
                    disposal = {
                        "source": {
                            "business_id": view.source["business_id"],
                            "slot": view.source["result_slot"],
                        }
                    }
                    cost = eit_disposals[(view.source["business_id"], view.event_index)]
                    if cost is not None:
                        disposal["eit_basis_fen"] = str(cost)
                    disposals.append(disposal)
                if taxable_interest(event):
                    interest = total((interest, interest_gross(event)))
            if not segments:
                pool.update(
                    quantity=Fraction(),
                    vat_basis_fen=0,
                    unit_basis_unknown_sources=[],
                    vat_unit_segments=[],
                )
            continue
        if method == "weighted_average":
            if pool["quantity"] is not None:
                pool["quantity"] *= opening_factor
            for view in ordered:
                if view.event["event_kind"] in {"acquisition", "opening"}:
                    _acquire(
                        pool, view.event, factors[(view.source["business_id"], view.event_index)]
                    )
        for view in ordered:
            event, kind = view.event, view.event["event_kind"]
            if kind in {"acquisition", "opening"}:
                if method == "moving_weighted_average":
                    _acquire(pool, event)
            elif kind in {"disposal", "loss"}:
                if "quantity" not in event:
                    missing_investment_input(
                        event,
                        view.source["business_id"],
                        "/data/quantity",
                        "投资退出缺少有来源的处置份额。",
                    )
                units = ratio(event["quantity"])
                if method == "weighted_average":
                    units *= factors[(view.source["business_id"], view.event_index)]
                vat_cost = _consume(pool, units, view)
                eit_cost = eit_disposals[(view.source["business_id"], view.event_index)]
                disposal = {
                    "source": {
                        "business_id": view.source["business_id"],
                        "slot": view.source["result_slot"],
                    }
                }
                if vat_cost is not None:
                    disposal["vat_buy_price_fen"] = str(vat_cost)
                if eit_cost is not None:
                    disposal["eit_basis_fen"] = str(eit_cost)
                disposals.append(disposal)
                if is_financial_transfer(event):
                    if vat_cost is None:
                        if pool["vat_basis_unknown_sources"]:
                            origin = sorted(
                                pool["vat_basis_unknown_sources"],
                                key=lambda row: (row["business_id"], row["slot"]),
                            )[0]
                            missing_investment_input(
                                event,
                                origin["business_id"],
                                "/data/members",
                                f"期初投资成员{origin['slot']}的独立VAT买入价未知，须补齐该成员的tax_buy_basis_fen有源依据，不能使用会计成本或EIT税基替代。",
                                precision=("integer_fen",),
                                meaning=f"期初投资成员{origin['slot']}的VAT买入价",
                            )
                        missing_investment_input(
                            event,
                            view.source["business_id"],
                            "/data/lots",
                            "金融商品转让的独立VAT买入价未知，须接续有源税务成本。",
                        )
                    difference = total((difference, amount(event["gross_price_fen"]), -vat_cost))
            elif kind == "capital_return":
                returned = amount(event["gross_price_fen"])
                if pool["vat_basis_fen"] is not None and returned > pool["vat_basis_fen"]:
                    missing_investment_input(
                        event,
                        view.source["business_id"],
                        "/data/amount_fen",
                        "已确认返本超过当前独立税务成本，须核对返本与收益性质。",
                    )
                if pool["vat_basis_fen"] is not None:
                    pool["vat_basis_fen"] -= returned
                _reset(pool)
            elif kind == "unit_change":
                if method == "weighted_average":
                    continue
                if pool.get("quantity") is None or "quantity" not in event:
                    missing_investment_input(
                        event,
                        view.source["business_id"],
                        "/data/quantity_after",
                        "投资份额变动缺少明确的税务持仓数量。",
                    )
                before = sum(
                    (ratio(row["quantity"]) for row in event.get("lot_allocations", ())),
                    Fraction(),
                )
                if not before or before > pool["quantity"]:
                    reject("投资份额调整不能超过原税务持仓。", "invalid_input")
                pool["quantity"] += ratio(event["quantity"]) - before
                _reset(pool)
            if taxable_interest(event):
                interest = total((interest, interest_gross(event)))
    positions = []
    for pool_key in sorted(pools, key=lambda key: tuple(value or "" for value in key)):
        product_id, pool = pool_key[0], pools[pool_key]
        tax_lots = eit_lots.get(pool_key, [])
        if pool.get("quantity") == 0 and pool["vat_basis_fen"] is None:
            pool["vat_basis_fen"] = 0
            pool["vat_basis_unknown_sources"] = []
        if pool.get("quantity") == 0 and pool["vat_basis_fen"] == 0 and not tax_lots:
            continue
        row = {"product_id": product_id, "eit_lots": tax_lots}
        if pool["vat_pool_identity"]:
            row["vat_pool_identity"] = deepcopy(pool["vat_pool_identity"])
        if pool["vat_basis_fen"] is not None:
            row["vat_basis_fen"] = str(pool["vat_basis_fen"])
        if pool["vat_basis_unknown_sources"]:
            row["vat_basis_unknown_sources"] = sorted(
                pool["vat_basis_unknown_sources"],
                key=lambda source: (source["business_id"], source["slot"]),
            )
        if pool["unit_basis_unknown_sources"]:
            row["unit_basis_unknown_sources"] = deepcopy(pool["unit_basis_unknown_sources"])
            row["vat_unit_segments"] = deepcopy(pool["vat_unit_segments"])
        if all("eit_basis_fen" in lot for lot in tax_lots):
            row["eit_basis_fen"] = str(total(amount(lot["eit_basis_fen"]) for lot in tax_lots))
        if pool.get("quantity") is not None:
            row["quantity"] = exact_quantity_text(pool["quantity"])
        positions.append(row)
    return positions, disposals, difference, interest


def transfer_position(previous, difference, rate, *, period, tax_period, december=False):
    month = int(period[5:])
    first = period if tax_period == "month" else f"{period[:4]}-{((month - 1) // 3) * 3 + 1:02d}"
    same_period = previous.get("tax_period_start") == first
    opening_loss = (
        amount(previous.get("tax_period_opening_loss_fen", "0"))
        if same_period
        else amount(previous.get("closing_loss_fen", "0"))
    )
    opening_credit = amount(previous.get("closing_tax_carry_fen", "0"))
    cumulative = checked(
        difference + (amount(previous["tax_period_difference_fen"]) if same_period else 0)
    )
    previous_payable = amount(previous["payable_tax_fen"]) if same_period else 0
    remaining = max(0, opening_loss - cumulative)
    payable = tax_on_gross(max(0, cumulative - opening_loss), rate)
    closing_credit = 0 if december else tax_on_gross(remaining, rate)
    return {
        "current_difference_fen": str(difference),
        "opening_loss_fen": str(opening_loss),
        "tax_period_start": first,
        "tax_period_difference_fen": str(cumulative),
        "tax_period_opening_loss_fen": str(opening_loss),
        "closing_loss_fen": str(0 if december else remaining),
        "opening_tax_carry_fen": str(opening_credit),
        "closing_tax_carry_fen": str(closing_credit),
        "payable_tax_fen": str(payable),
        "recognized_tax_change_fen": str(
            checked(payable - previous_payable + opening_credit - closing_credit)
        ),
        "expired_loss_fen": str(remaining if december else 0),
    }
