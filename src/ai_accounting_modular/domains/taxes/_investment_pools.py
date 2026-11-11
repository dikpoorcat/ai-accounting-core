"""A management product may hold several legally different financial instruments."""

from copy import deepcopy
from fractions import Fraction

from ._common import amount, investment_result_issue, missing_investment_input, reject, total
from ._investment_units import quantity_text, reference_key

FIELDS = ("instrument_type", "right_type", "quantity_unit", "instrument_reference")


def _rights(value):
    return "fund_units" if value in {"securities_fund", "contractual_fund"} else value


def stored_identity(row):
    result = deepcopy(row.get("vat_pool_identity", row.get("_unclassified_identity", {})))
    if "legal_form" in result:
        result["right_type"] = _rights(result.pop("legal_form"))
    return result


def identity(event):
    result = {field: event[field] for field in ("right_type", "quantity_unit") if field in event}
    if "tax_facts" in event:
        result["instrument_type"] = event["tax_facts"]["instrument_type"]
    if "financial_instrument_reference" in event:
        result["instrument_reference"] = event["financial_instrument_reference"]["reference"]
    return result


def key(product_id, declared, unclassified_terms_id=None):
    return product_id, *(declared.get(field) for field in FIELDS), unclassified_terms_id


def compatible(first, second):
    return all(first[field] == second[field] for field in first.keys() & second.keys())


def affects_disposal(view, disposal, method):
    """Monthly average uses the month; moving average uses the known transaction order."""
    if view is None:
        return True
    if view.period != disposal.period:
        return False
    if method != "moving_weighted_average":
        return True
    if (
        view.effective_on is not None
        and disposal.effective_on is not None
        and view.effective_on != disposal.effective_on
    ):
        return view.effective_on < disposal.effective_on
    first, last = view.event.get("transaction_sequence"), disposal.event.get("transaction_sequence")
    return first is None or last is None or first <= last


def _identity_conflict(first, second, view):
    changed = {key for key in first.keys() & second.keys() if first[key] != second[key]}
    if changed <= {"instrument_type", "instrument_reference"}:
        message = "当前投资税务身份与原池不同，现算法不能猜测拆分原始买入价或沿用旧身份。"
        investment_result_issue(view, message, unsupported=True)
    reject("正式原批次的真实会计权利或计量单位矛盾，不能混用来源。", "invalid_input")


def _legacy_boundary():
    reject(
        "旧冻结金融商品池缺少能够唯一分离法律权益及计量单位的原始税务身份；"
        "不能按账面成本比例拆分旧VAT买入价，需要按原正式来源办理税务持仓接续或更正。",
        "unsupported_business",
    )


def _missing_reference(lot, view=None):
    if view is not None:
        missing_investment_input(
            view.event,
            view.event["terms_id"],
            "/data/financial_instrument_reference",
            "原持仓尚未识别实际金融商品或权益参考，可能改变均价；须复用原券代码或份额类别，不能由其他批次或名称推定。",
            precision=(),
            meaning="影响买入价归属的有源金融商品或权益标识",
        )
    source, terms_id = lot["source"], lot.get("terms_id")
    message = (
        f"原正式批次{source['slot']}的金融商品或权益参考尚未识别，"
        "不能与已明确的其他类别按名称归为同一均价池；先查其有源投资税务依据，"
        "该身份不属于会计条款的待补字段。"
    )
    reject(
        message,
        "needs_information",
        fact_issues=[
            {
                "business_id": terms_id or source["business_id"],
                "field": "/",
                "code": "missing_fact",
                "meaning": "影响VAT均价的原持仓真实金融商品或权益标识",
                "message": message,
                "allowed_precision": [],
                "reusable_sources": [],
                "blocking_scope": "business",
            }
        ],
    )


def _merge(target, row):
    if "quantity" in target and "quantity" in row:
        target["quantity"] = quantity_text(Fraction(target["quantity"]) + Fraction(row["quantity"]))
    else:
        target.pop("quantity", None)
    if "vat_basis_fen" in target and "vat_basis_fen" in row:
        target["vat_basis_fen"] = str(
            total((amount(target["vat_basis_fen"]), amount(row["vat_basis_fen"])))
        )
    else:
        target.pop("vat_basis_fen", None)
    for field in (
        "eit_lots",
        "vat_basis_unknown_sources",
        "unit_basis_unknown_sources",
        "vat_unit_segments",
    ):
        for value in row.get(field, ()):
            if value not in target.setdefault(field, []):
                target[field].append(deepcopy(value))


def partition(previous, views, taxable_exit, method):
    """Classify from frozen rights; a fully referenced old pool can gain a new proof."""
    claims, claim_views = {}, {}
    for view in views:
        declared = identity(view.event)
        for allocation in view.event.get("lot_allocations", ()):
            source = reference_key(allocation["source"])
            old = claims.get(source)
            if old is not None and not compatible(old, declared):
                _identity_conflict(old, declared, view)
            claims[source] = {**(old or {}), **declared}
            claim_views[source] = view
    snapshots, owners = {}, {}
    for original in previous:
        row = deepcopy(original)
        declared = stored_identity(row)
        lots = [reference_key(lot["source"]) for lot in row.get("eit_lots", ())]
        proven = [claims[source] for source in lots if source in claims]
        for source in lots:
            if source in claims and not compatible(declared, claims[source]):
                _identity_conflict(declared, claims[source], claim_views[source])
        for field in FIELDS:
            if field in declared:
                continue
            values = {candidate[field] for candidate in proven if field in candidate}
            if len(values) > 1:
                _legacy_boundary()
            if values and lots and all(field in claims.get(source, {}) for source in lots):
                declared = {**declared, field: next(iter(values))}
            elif (
                values
                and field == "instrument_reference"
                and any(
                    taxable_exit(view.event)
                    and any(
                        reference_key(part["source"]) in lots
                        for part in view.event.get("lot_allocations", ())
                    )
                    for view in views
                )
            ):
                unknown = next(
                    lot
                    for lot in row["eit_lots"]
                    if field not in claims.get(reference_key(lot["source"]), {})
                )
                source = reference_key(unknown["source"])
                witness = next(
                    (
                        view
                        for view in views
                        if any(
                            reference_key(part["source"]) == source
                            for part in view.event.get("lot_allocations", ())
                        )
                    ),
                    None,
                )
                _missing_reference(unknown, witness)
        if declared and "instrument_type" in declared:
            row["vat_pool_identity"] = declared
        pool = key(row["product_id"], declared, row.get("unclassified_terms_id"))
        if pool in snapshots:
            _merge(snapshots[pool], row)
        else:
            snapshots[pool] = row
        for source in lots:
            if source in owners:
                reject("同一正式所得税批次不能同时归入两个金融商品池。")
            owners[source] = pool
    groups = {}
    pending = list(views)
    while pending:
        ready = [
            view
            for view in pending
            if all(
                reference_key(row["source"]) in owners
                for row in view.event.get("lot_allocations", ())
            )
        ]
        if not ready:
            reject("金融商品分池缺少正式原批次或包含循环来源。")
        for view in ready:
            pending.remove(view)
            event = view.event
            declared = identity(event)
            selected = {
                owners[reference_key(row["source"])] for row in event.get("lot_allocations", ())
            }
            if len(selected) > 1:
                _legacy_boundary()
            pool = (
                next(iter(selected))
                if selected
                else key(
                    event["product_id"],
                    declared,
                    event["terms_id"] if "instrument_type" not in declared else None,
                )
            )
            known = {
                field: pool[index + 1]
                for index, field in enumerate(FIELDS)
                if pool[index + 1] is not None
            }
            if pool[0] != event["product_id"]:
                reject("正式来源的产品身份与本次业务不一致，不能混用批次。", "invalid_input")
            if not compatible(known, declared):
                _identity_conflict(known, declared, view)
            groups.setdefault(pool, []).append(view)
            kind = event["event_kind"]
            if kind in {"opening", "acquisition", "unit_change", "capital_return", "debt_interest"}:
                source = (
                    event["origin_ref"]
                    if kind == "opening"
                    else {"business_id": view.source["business_id"], "slot": "cost"}
                )
                owners[reference_key(source)] = pool
    by_product = {}
    for pool in dict.fromkeys((*snapshots, *groups)):
        by_product.setdefault(pool[0], []).append(pool)
    exits = {}
    for view in views:
        if taxable_exit(view.event):
            exits.setdefault(view.event["product_id"], []).append(view)
    active = set(snapshots)
    active.update(
        pool
        for pool, rows in groups.items()
        if any(
            view.event["event_kind"] in {"acquisition", "opening"}
            and any(affects_disposal(view, exit, method) for exit in exits.get(pool[0], ()))
            for view in rows
        )
    )
    for pool in dict.fromkeys((*snapshots, *groups)):
        if any(value is None for value in pool[1:5]):
            alternatives = [
                other
                for other in by_product[pool[0]]
                if other != pool
                and other in active
                and other[0] == pool[0]
                and all(
                    a is None or b is None or a == b
                    for a, b in zip(pool[1:5], other[1:5], strict=True)
                )
            ]
            if alternatives and pool[0] in exits and pool in active:
                unidentified = next(
                    (
                        view
                        for view in groups.get(pool, ())
                        if "financial_instrument_reference" not in view.event
                    ),
                    None,
                )
                if (
                    unidentified is not None
                    and pool[4] is None
                    and any(other[4] is not None for other in alternatives)
                ):
                    _missing_reference({}, unidentified)
                if pool[4] is None and any(other[4] is not None for other in alternatives):
                    _missing_reference(snapshots[pool]["eit_lots"][0])
                _legacy_boundary()
    return snapshots, groups, owners


def ending_lots(lots, owners):
    result = {}
    for values in lots.values():
        for lot in values:
            pool = owners.get(reference_key(lot["source"]))
            if pool is None:
                reject("期末独立所得税批次缺少正式金融商品归属。")
            result.setdefault(pool, []).append(lot)
    return result
