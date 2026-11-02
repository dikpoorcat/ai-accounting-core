"""Prove product-wide unit transitions before monthly weighted cost allocation."""

from copy import deepcopy
from fractions import Fraction
from types import SimpleNamespace

from ._common import checked, missing_investment_input, reject

UNIT_BASIS_KIND = "investment.unit_change_basis"


def ratio(value):
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError):
        reject("单位比例必须为有效的有源十进制数。", "invalid_input")
    if result < 0:
        reject("投资税务数量不能为负。", "invalid_input")
    return result


def rounded_fraction(value):
    sign = -1 if value < 0 else 1
    whole, rest = divmod(abs(value.numerator), value.denominator)
    return checked(sign * (whole + (2 * rest >= value.denominator)))


def quantity_text(value):
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        reject("期末持仓的实际单位不能由中间循环小数舍入推定。")
    places = max(twos, fives)
    scaled = value.numerator * 2 ** (places - twos) * 5 ** (places - fives)
    if not places:
        return str(scaled)
    digits = str(abs(scaled)).zfill(places + 1)
    return ("-" if scaled < 0 else "") + (digits[:-places] + "." + digits[-places:]).rstrip(
        "0"
    ).rstrip(".")


def reference_key(reference):
    return reference["business_id"], reference["slot"]


def view_key(view):
    return view.source["business_id"], view.event_index


def _source(view):
    return {"business_id": view.source["business_id"], "slot": "cost"}


def _consume_sources(live, event):
    for part in event.get("lot_allocations", ()):
        key, quantity = reference_key(part["source"]), ratio(part["quantity"])
        if key not in live or quantity > live[key]:
            reject("单位归一所采用的正式批次数量不足，不能合并不同单位。")
        live[key] -= quantity
        if not live[key]:
            del live[key]


def _advance(live, view):
    event, kind = view.event, view.event["event_kind"]
    if kind in {"opening", "acquisition"}:
        source = event["origin_ref"] if kind == "opening" else _source(view)
        live[reference_key(source)] = ratio(event["quantity"])
    elif kind in {"disposal", "loss", "unit_change", "capital_return", "debt_interest"}:
        allocations = event.get("lot_allocations", ())
        if not allocations:
            return
        _consume_sources(live, event)
        if kind in {"unit_change", "capital_return", "debt_interest"}:
            live[reference_key(_source(view))] = ratio(event["quantity"])


def _coverage(view):
    event = view.event
    before = sum((ratio(part["quantity"]) for part in event.get("lot_allocations", ())), Fraction())
    if not before:
        reject("份额转换缺少正式原批次数量。")
    return {
        reference_key(part["source"]): ratio(part["quantity"]) for part in event["lot_allocations"]
    }, ratio(event["quantity"]) / before


def _basis_groups(bases, product_id, events, all_events):
    groups = {}
    current = {view.source["business_id"] for view in events}
    all_sources = {view.source["business_id"]: view.source for view in all_events}
    for basis in bases:
        values = basis.target["values"]
        if values["product_id"] != product_id:
            continue
        all_members = {row["business_id"] for row in values["unit_change_sources"]}
        members = all_members & current
        if not members:
            continue
        frozen = {row["business_id"]: row for row in values["members"]}
        if any(all_sources.get(member) != frozen.get(member) for member in all_members):
            reject("产品单位依据的完整正式成员版本与本次税务来源不一致。")
        factor = ratio(values["to_units"]) / ratio(values["from_units"])
        for member in members:
            if member in groups:
                reject("同一正式份额转换不能采用两份产品单位依据。", "invalid_input")
            groups[member] = members, factor, basis
    return groups


def sourced_order(events, bases):
    """Reuse an adopted action's date/sequence only for its named old members."""
    members = {
        row["business_id"]: basis
        for basis in bases
        for row in basis.target["values"]["unit_change_sources"]
    }
    output = []
    for view in events:
        basis = members.get(view.source["business_id"])
        if basis is None or view.event["event_kind"] != "unit_change":
            output.append(view)
            continue
        values = basis.target["values"]
        if values["product_id"] != view.event["product_id"]:
            reject("产品单位变换依据不能采用另一产品的正式成员。", "invalid_input")
        event = dict(view.event)
        if "transaction_sequence" not in event and "transaction_sequence" in values:
            event["transaction_sequence"] = values["transaction_sequence"]
        output.append(
            SimpleNamespace(
                event=event,
                source=view.source,
                period=view.period,
                effective_on=view.effective_on or values["effective_on"],
                event_index=view.event_index,
                unit_group=basis.source["business_id"],
            )
        )
    return output


def normalize(previous, events, bases=(), adoptions=None, *, all_events=None):
    """Return final-unit factors, or explicit unresolved lot segments."""
    if not previous.get("unit_basis_unknown_sources") and not any(
        view.event["event_kind"] == "unit_change" for view in events
    ):
        return Fraction(1), {view_key(view): Fraction(1) for view in events}, [], []
    live = {
        reference_key(lot["source"]): ratio(lot["quantity"]) for lot in previous.get("eit_lots", ())
    }
    changes, unresolved = [], deepcopy(previous.get("unit_basis_unknown_sources", []))
    explicit = _basis_groups(
        bases,
        events[0].event["product_id"] if events else previous["product_id"],
        events,
        events if all_events is None else all_events,
    )
    position = 0
    while position < len(events):
        view = events[position]
        if view.event["event_kind"] != "unit_change":
            _advance(live, view)
            position += 1
            continue
        initial, covered, members, factor, adopted = dict(live), {}, [], None, None
        if not initial:
            reject("单位转换没有已采用的起点持仓，须先采用期初或取得来源，不能猜测顺序。")
        expected = explicit.get(view.source["business_id"])
        cursor = position
        while cursor < len(events):
            member = events[cursor]
            if member.event["event_kind"] == "income":
                cursor += 1
                continue
            if member.event["event_kind"] != "unit_change":
                break
            selections, member_factor = _coverage(member)
            if any(
                key in covered or key not in initial or initial[key] != units
                for key, units in selections.items()
            ):
                break
            if factor is not None and member_factor != factor:
                break
            if expected is not None and member.source["business_id"] not in expected[0]:
                break
            factor = member_factor
            covered.update(selections)
            members.append(member)
            _advance(live, member)
            cursor += 1
            if covered == initial:
                if expected is not None:
                    if {item.source["business_id"] for item in members} != expected[
                        0
                    ] or factor != expected[1]:
                        reject(
                            "有源产品转换组与实际完整批次切面或转换比例不一致。", "invalid_input"
                        )
                    adopted = expected[2]
                    frozen_members = {
                        row["business_id"]: row for row in adopted.target["values"]["members"]
                    }
                    if any(
                        frozen_members.get(item.source["business_id"]) != item.source
                        for item in members
                    ):
                        reject("产品单位依据采用的正式成员版本与本次税务来源不一致。")
                if not unresolved:
                    changes.append((position, factor))
                if adoptions is not None and not unresolved:
                    quantity = {
                        "type": "investment_unit_normalization",
                        "product_id": view.event["product_id"],
                        "ratio_numerator": str(factor.numerator),
                        "ratio_denominator": str(factor.denominator),
                        "source_events": [deepcopy(item.source) for item in members],
                    }
                    if adopted is not None:
                        quantity["basis_source"] = deepcopy(adopted.source)
                    adoptions.append(quantity)
                break
        if not members or covered != initial:
            if not members:
                _advance(live, view)
                cursor = position + 1
            for member in members or [view]:
                reference = {
                    "business_id": member.source["business_id"],
                    "slot": member.source["result_slot"],
                }
                if reference not in unresolved:
                    unresolved.append(reference)
        position = cursor
    factors = {}
    for index, view in enumerate(events):
        value = Fraction(1)
        for cut, factor in changes:
            if index < cut:
                value *= factor
        factors[view_key(view)] = value
    opening_factor = Fraction(1)
    for _, factor in changes:
        opening_factor *= factor
    segments = [
        {"source": {"business_id": key[0], "slot": key[1]}, "quantity": quantity_text(units)}
        for key, units in sorted(live.items())
    ]
    return opening_factor, factors, unresolved, segments


def require_comparable(unresolved, view):
    origin = unresolved[0]
    missing_investment_input(
        view.event,
        origin["business_id"],
        "/data",
        "金融商品买入价需要可比较的统一份额单位；正式批次只能证明部分转换或先后不明，须补充该转换的有源产品单位依据。",
        precision=("day", "transaction_sequence"),
        meaning="影响金融商品税务买入价的产品单位转换依据",
    )
