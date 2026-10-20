"""An authority-confirmed amount range has one tax-credit route, never two."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import format_fen

from ._common import adopt_source, amount, fact_for, missing, plan_for, reject, result_for, total
from ._tax_point import ranges, wire_ranges


def _intersection(first, second):
    return [(max(a, c), min(b, d)) for a, b in first for c, d in second if max(a, c) < min(b, d)]


def _difference(values, removed):
    result = []
    for begin, end in values:
        cursor = begin
        for first, last in sorted(_intersection([(begin, end)], removed)):
            if cursor < first:
                result.append((cursor, first))
            cursor = last
        if cursor < end:
            result.append((cursor, end))
    return result


def _portion(sale, selected):
    """Ranges establish coverage; they never invent the VAT of a partial row."""
    original = ranges(sale["ranges"], max(amount(row["to_fen"]) for row in sale["ranges"]))
    capacity = total(end - begin for begin, end in original)
    if capacity != -amount(sale["net_sales_fen"]) - amount(sale["vat_fen"]):
        reject("退回税源的原含税范围与正式净额、VAT不一致。")
    taken = _intersection(original, selected)
    gross = total(end - begin for begin, end in taken)
    return gross, -amount(sale["vat_fen"]) if gross == capacity else None, taken


def selected_rows(fact, item, views):
    """Return exact negative components covered by one confirmed source item."""
    candidates = [
        (index, view)
        for index, view in enumerate(views)
        if view.sale["source_business_id"] == item["return_id"]
        and amount(view.sale["net_sales_fen"]) <= 0
        and amount(view.sale["vat_fen"]) <= 0
        and (amount(view.sale["net_sales_fen"]) < 0 or amount(view.sale["vat_fen"]) < 0)
    ]
    if not candidates:
        missing(fact, "/data/returns", "退抵需要已采用的真实退回税销售，不能用原交易替代。")
    if any(not view.sale["ranges"] for _, view in candidates):
        missing(fact, "/data/returns/tax_ranges", "退回税源缺少可核对的原金额范围。")
    all_ranges = [
        pair
        for _, view in candidates
        for pair in ranges(
            view.sale["ranges"], max(amount(row["to_fen"]) for row in view.sale["ranges"])
        )
    ]
    _unique_ranges([(item["return_id"], begin, end) for begin, end in all_ranges])
    if "tax_ranges" in item:
        selection = ranges(item["tax_ranges"], max(end for _, end in all_ranges))
    else:
        gross = total(end - begin for begin, end in all_ranges)
        if len(candidates) != 1 or amount(item["gross_fen"]) != gross:
            missing(
                fact, "/data/returns/tax_ranges", "部分或多个税源的退抵必须明确所覆盖的原金额范围。"
            )
        selection = all_ranges
    if total(end - begin for begin, end in selection) != amount(item["gross_fen"]):
        reject("退抵明确范围合计必须等于其覆盖毛额。", "invalid_input")
    selected, partials, gross, vat = [], [], 0, 0
    for index, view in candidates:
        part, tax, covered = _portion(view.sale, selection)
        if covered:
            selected.append((index, part, tax, covered))
            gross = total((gross, part))
            if tax is None:
                partials.append(len(selected) - 1)
            else:
                vat = total((vat, tax))
    if gross != amount(item["gross_fen"]):
        reject("退抵覆盖毛额须与正式退回金额范围精确对应。", "invalid_input")
    if len(partials) > 1:
        missing(fact, "/data/returns", "多个部分税源缺少各自明确VAT；请按原税源分别列明覆盖项。")
    if partials:
        position = partials[0]
        index, part, _, covered = selected[position]
        actual_vat = amount(item["vat_fen"]) - vat
        source = views[index].sale
        if not (
            0 <= actual_vat <= -amount(source["vat_fen"])
            and 0 <= part - actual_vat <= -amount(source["net_sales_fen"])
        ):
            reject("部分退抵的明确VAT或净额超过该正式退回税源容量。", "invalid_input")
        selected[position] = index, part, actual_vat, covered
    elif vat != amount(item["vat_fen"]):
        reject("完整退回税源必须采用原已明确的全部VAT。", "invalid_input")
    return selected


def credit_peers(fact, context):
    original_id = fact["data"]["original_assessment_id"]
    return tuple(
        row
        for row in context.related_facts(original_id, "reference")
        if row["kind"] == "taxes.tax_credit"
        and row["data"]["original_assessment_id"] == original_id
    )


def _capacity(sale, gross, vat):
    capacity_vat, capacity_net = -amount(sale["vat_fen"]), -amount(sale["net_sales_fen"])
    capacity = total((capacity_vat, capacity_net))
    if (
        not 0 <= vat <= capacity_vat
        or not 0 <= gross - vat <= capacity_net
        or gross > capacity
        or gross == capacity
        and vat != capacity_vat
    ):
        reject("累计退抵的毛额、净额或VAT超过该真实退回税源的明确容量。")


def credit(fact, context):
    data = fact["data"]
    original = result_for(
        fact,
        context,
        data["original_assessment_id"],
        "taxes.assessment",
        "/data/original_assessment_id",
    )
    if original.target["values"]["type"] != "vat":
        reject("VAT退抵只能引用原VAT及附加计提。", "invalid_input")
    origin = original.basis["input"]
    if (data["start_month"], data["end_month"]) != (origin["start_month"], origin["end_month"]):
        reject("退抵明确原税期必须与原采用结果一致。", "invalid_input")
    if data["end_month"] >= fact["period"]:
        reject(
            "当前退抵入口承接原完整申报期之后的实际退抵；同税期特殊预缴退抵尚缺对应来源合同。",
            "unsupported_business",
        )
    if original.target["values"]["adopted_basis"] != "actual":
        missing(
            fact,
            "/data/original_filing_source",
            "原税期须已采用实际申报，不能凭参考计算制造退抵权利。",
        )
    plan = plan_for(
        fact,
        context,
        {
            key: data[key]
            for key in (
                "vat_credit_fen",
                "surtax_credit_fen",
                "exemption_reversal_fen",
                "disposition",
            )
        },
    )
    details = ("urban_credit_fen", "education_credit_fen", "local_education_credit_fen")
    if any(field in data for field in details):
        if not all(field in data for field in details) or total(
            amount(data[field]) for field in details
        ) != amount(data["surtax_credit_fen"]):
            reject("附加退抵三项须完整且合计等于明确附加退抵总额。", "invalid_input")
        plan["target"]["values"].update({field: data[field] for field in details})
    adopt_source(plan, original.source)
    views, periods, original_ranges = [], [], {}
    for item in data["returns"]:
        returned = context.get_fact(item["return_id"])
        if returned is None:
            missing(fact, "/data/returns", "所引用真实退回事实尚不存在。")
        if returned["kind"] not in ("operations.sale_return", "operations.advance_refund"):
            reject("退抵来源必须是销售退回或已计税预收退款。", "invalid_input")
        linked = returned["data"].get("sale_id", returned["data"].get("advance_id"))
        if linked != item["original_business_id"]:
            reject("退回关联的原业务与退抵指明来源不一致。", "invalid_input")
        if returned["period"] <= data["end_month"]:
            reject(
                "同原税期退回应归完整税期核对；本入口不以已采用税源冒充额外税局退款权利。",
                "unsupported_business",
            )
        if returned["period"] > fact["period"]:
            reject("真实退回不能晚于已确认退抵所属期。", "invalid_input")
        if returned["period"] not in periods:
            views.extend(context.tax_sales(returned["period"], returned["period"]))
            periods.append(returned["period"])
        outcome = result_for(fact, context, item["return_id"], returned["kind"], "/data/returns")
        originals = [
            q["sale"]
            for q in outcome.basis["adopted_quantities"]
            if q["type"] == "tax_sale"
            and q["sale"]["source_business_id"] == item["original_business_id"]
        ]
        if any(not sale["ranges"] for sale in originals):
            missing(fact, "/data/returns/tax_ranges", "原采用税源缺少可核对的含税金额范围。")
        allowed = [
            pair
            for sale in originals
            if data["start_month"] <= sale["tax_period"] <= data["end_month"]
            for pair in ranges(sale["ranges"], max(amount(row["to_fen"]) for row in sale["ranges"]))
        ]
        if not allowed:
            reject("退回必须保有属于原申报税期的精确采用税源。")
        original_ranges[item["return_id"]] = allowed
        adopt_source(plan, outcome.source)
    occupied, covered_vat, usages = [], 0, {}
    for item in data["returns"]:
        for index, _, tax, selected in selected_rows(fact, item, views):
            if _difference(selected, original_ranges[item["return_id"]]):
                reject("本次明确退抵范围必须属于所引用原申报期的采用税源。")
            for begin, end in selected:
                occupied.append((item["return_id"], begin, end))
            covered_vat = total((covered_vat, tax))
            used = usages.setdefault(index, [0, 0])
            used[0] += total(end - begin for begin, end in selected)
            used[1] += tax
            view = views[index]
            adopt_source(plan, view.source)
            quantity = {"type": "tax_sale", "sale": deepcopy(view.sale)}
            if quantity not in plan["basis"]["adopted_quantities"]:
                plan["basis"]["adopted_quantities"].append(quantity)
    if amount(data["vat_credit_fen"]) + amount(data["exemption_reversal_fen"]) != covered_vat:
        reject("已明确VAT退抵与原免税转回合计必须等于覆盖的红字VAT。", "invalid_input")
    _unique_ranges(occupied)
    peers = [
        row for row in credit_peers(fact, context) if row["business_id"] != fact["business_id"]
    ]
    related = {peer["business_id"]: peer for peer in peers}
    for item in data["returns"]:
        related.update(
            {
                peer["business_id"]: peer
                for peer in context.related_facts(item["return_id"], "reference")
                if peer["kind"] == "taxes.tax_credit" and peer["business_id"] != fact["business_id"]
            }
        )
    for peer in related.values():
        for item in peer["data"]["returns"]:
            if not any(item["return_id"] == row["return_id"] for row in data["returns"]):
                continue
            for index, gross, vat, selected in selected_rows(fact, item, views):
                occupied.extend((item["return_id"], begin, end) for begin, end in selected)
                used = usages.setdefault(index, [0, 0])
                used[0] += gross
                used[1] += vat
    _unique_ranges(occupied)
    for index, (gross, vat) in usages.items():
        _capacity(views[index].sale, gross, vat)
    metrics = original.target["values"]
    for field, capacity in (
        ("vat_credit_fen", "payable_vat_fen"),
        ("surtax_credit_fen", "surtax_fen"),
        ("exemption_reversal_fen", "relief_fen"),
    ):
        cumulative = total([amount(data[field]), *(amount(peer["data"][field]) for peer in peers)])
        if capacity not in metrics:
            if cumulative:
                missing(fact, "/data/" + field, "原实际申报没有明确可供本次转回的原金额。")
        elif cumulative > amount(metrics[capacity]):
            reject("累计退抵或免税转回不能超过原实际已采用金额。")
    for field, capacity in zip(
        details, ("urban_fen", "education_fen", "local_education_fen"), strict=True
    ):
        known = total(
            [
                amount(data.get(field, "0")),
                *(amount(peer["data"].get(field, "0")) for peer in peers),
            ]
        )
        if known > amount(metrics[capacity]):
            reject("已明确的同一附加税种累计退抵不能超过该税种原实际采用金额。")
    return plan


def _unique_ranges(rows):
    ordered = sorted(rows)
    if any(
        first[0] == last[0] and last[1] < first[2]
        for first, last in zip(ordered, ordered[1:], strict=False)
    ):
        reject("同一真实退回的金额范围不能被重复确认或重复退抵。")


def apply_credits(fact, context, plan, views):
    """Keep published TaxSale intact; subtract only exact confirmed coverage."""
    data, covered, credits = fact["data"], {}, []
    for credit_id in data.get("credit_ids", ()):
        source = fact_for(fact, context, credit_id, "taxes.tax_credit", "/data/credit_ids")
        if source["period"] > context.evaluation_period_to:
            reject("不能先于已明确退抵结果的所属月采用。")
        outcome = result_for(fact, context, credit_id, "taxes.tax_credit", "/data/credit_ids")
        peers = [
            row
            for row in context.related_facts(credit_id, "reference")
            if row["kind"] == "taxes.assessment"
            and row["business_id"] != fact["business_id"]
            and credit_id in row["data"].get("credit_ids", ())
        ]
        if peers:
            reject("同一已确认退抵只能被一个当前计提采用。")
        for item in source["data"]["returns"]:
            returned = context.get_fact(item["return_id"])
            if (
                returned is None
                or not data["start_month"] <= returned["period"] <= data["end_month"]
            ):
                reject("退抵应在覆盖真实退回月份的完整税费目标中采用。")
            for index, gross, vat, selection in selected_rows(fact, item, views):
                existing = covered.setdefault(index, {"gross": 0, "vat": 0, "ranges": []})
                existing["gross"] += gross
                existing["vat"] += vat
                existing["ranges"].extend(selection)
                _unique_ranges(
                    [(item["return_id"], begin, end) for begin, end in existing["ranges"]]
                )
        adopt_source(plan, outcome.source)
        credits.append(outcome)
    sales = []
    for index, view in enumerate(views):
        row = deepcopy(view.sale)
        if index in covered:
            taken = covered[index]
            _capacity(row, taken["gross"], taken["vat"])
            row["net_sales_fen"] = format_fen(
                amount(row["net_sales_fen"]) + taken["gross"] - taken["vat"]
            )
            row["vat_fen"] = format_fen(amount(row["vat_fen"]) + taken["vat"])
            original = ranges(row["ranges"], max(amount(x["to_fen"]) for x in row["ranges"]))
            row["ranges"] = wire_ranges(_difference(original, taken["ranges"]))
        sales.append(row)
    return sales, credits
