"""EIT tax lots follow named holding sources, independently of VAT averaging."""

from copy import deepcopy
from decimal import Decimal, localcontext

from ._common import (
    amount,
    checked,
    investment_result_issue,
    missing_investment_input,
    reject,
    rounded,
    total,
)


def _key(reference):
    return reference["business_id"], reference["slot"]


def _lot(reference, units, basis, terms_id):
    result = {
        "source": deepcopy(reference),
        "quantity": str(units),
        "allocation_quantity": str(units),
        "consumed_quantity": "0",
        "terms_id": terms_id,
    }
    if basis is not None:
        result.update(eit_basis_fen=str(basis), allocation_basis_fen=str(basis))
    return result


def _take(lot, units):
    remaining = Decimal(lot["quantity"])
    if units < 0 or units > remaining:
        reject("所得税成本退出超过同一有源批次的剩余份额。", "invalid_input")
    before, consumed = Decimal(lot["allocation_quantity"]), Decimal(lot["consumed_quantity"])
    value = None
    if "allocation_basis_fen" in lot:
        with localcontext() as precision:
            precision.prec = 80
            original = Decimal(lot["allocation_basis_fen"])
            value = rounded(original * (consumed + units) / before) - rounded(
                original * consumed / before
            )
        lot["eit_basis_fen"] = str(checked(amount(lot["eit_basis_fen"]) - value))
    lot["quantity"] = str(remaining - units)
    lot["consumed_quantity"] = str(consumed + units)
    return value


def month_lots(previous, views):
    lots = {}
    for row in previous:
        product = lots.setdefault(row["product_id"], {})
        for lot in row.get("eit_lots", ()):
            key = _key(lot["source"])
            if key in product:
                reject("同一正式所得税批次不能重复出现在两个税务池。")
            product[key] = deepcopy(lot)
    pending = list(views)
    dispositions = {}
    while pending:
        current_sources = {view.source["business_id"] for view in pending}
        ready = [
            view
            for view in pending
            if not any(
                row["source"]["business_id"] in current_sources
                for row in view.event.get("lot_allocations", ())
            )
        ]
        if not ready:
            reject("所得税成本的批次接续包含循环。", "dependency_conflict")
        ready.sort(
            key=lambda view: (
                view.effective_on or view.period,
                view.event.get("transaction_sequence", 0),
                view.source["business_id"],
                view.event_index,
            )
        )
        for view in ready:
            pending.remove(view)
            event, kind = view.event, view.event["event_kind"]
            product = lots.setdefault(event["product_id"], {})
            successor = {"business_id": view.source["business_id"], "slot": "cost"}
            if kind in {"acquisition", "opening"}:
                reference = event["origin_ref"] if kind == "opening" else successor
                value = (
                    (
                        amount(event["opening_eit_tax_basis_fen"])
                        if "opening_eit_tax_basis_fen" in event
                        else None
                    )
                    if kind == "opening"
                    else total(
                        (
                            amount(event["gross_price_fen"]),
                            amount(event["transaction_fees_fen"]),
                            -amount(event["separate_declared_income_fen"]),
                        )
                    )
                )
                if value is not None and value < 0:
                    reject("所得税初始成本不能为负。", "invalid_input")
                if "quantity" not in event:
                    missing_investment_input(
                        event,
                        view.source["business_id"],
                        "/data/quantity",
                        "所得税成本批次缺少有来源的取得份额。",
                    )
                if _key(reference) in product:
                    reject("同一所得税成本批次不能重复取得。", "invalid_input")
                product[_key(reference)] = _lot(
                    reference, Decimal(event["quantity"]), value, event["terms_id"]
                )
            elif kind in {"disposal", "loss", "capital_return", "unit_change", "debt_interest"}:
                allocations = event.get("lot_allocations", ())
                if not allocations:
                    if kind == "debt_interest":
                        continue
                    investment_result_issue(
                        view, "所得税成本接续缺少核心已经采用的原批次，须核查正式业务的来源关联。"
                    )
                consumed, unknown, units = 0, False, Decimal(0)
                seen = set()
                for selection in allocations:
                    key = _key(selection["source"])
                    if key in seen or key not in product:
                        investment_result_issue(
                            view, "所得税成本的正式来源缺失或重复，不能用VAT均价代替。"
                        )
                    seen.add(key)
                    part = Decimal(selection["quantity"])
                    value = _take(product[key], part)
                    units += part
                    if value is None:
                        unknown = True
                    else:
                        consumed = total((consumed, value))
                if kind in {"disposal", "loss"}:
                    dispositions[(view.source["business_id"], view.event_index)] = (
                        None if unknown else consumed
                    )
                else:
                    basis = None if unknown else consumed
                    if kind == "capital_return" and basis is not None:
                        basis -= amount(event["gross_price_fen"])
                        if basis < 0:
                            missing_investment_input(
                                event,
                                view.source["business_id"],
                                "/data/amount_fen",
                                "返本超过该批次独立所得税成本，须核对税务性质。",
                            )
                    if kind == "unit_change":
                        units = Decimal(event["quantity"])
                    product[_key(successor)] = _lot(successor, units, basis, event["terms_id"])
                for key in seen:
                    if Decimal(product[key]["quantity"]) == 0:
                        del product[key]
    return {
        product: [values[key] for key in sorted(values)] for product, values in lots.items()
    }, dispositions
