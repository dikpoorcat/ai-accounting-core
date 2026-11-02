"""Explain the ledger's single formal investment-income amount without recalculating tax."""

from dataclasses import replace

from ._common import amount, fail, total
from ._investment_read_sources import source_contents
from ._read_funds import _contents, _pages
from ._read_types import InvestmentIncomeAdjustment

TAX_KINDS = ("taxes.financial_transfer_basis", "taxes.assessment")


def _quantities(body, value):
    quantities = [
        q for q in body.basis["adopted_quantities"] if q["type"] == "investment_income_adjustment"
    ]
    if quantities:
        if total(amount(q["amount_fen"]) for q in quantities) != value:
            fail("投资税费正式收益组成与总账不一致。", "storage_corrupt")
        return quantities
    if not value:
        return ()
    if body.reference["state"] != "frozen":
        fail("投资税费正式结果缺少完整的收益归属量。", "storage_corrupt")
    # A frozen predecessor has no new explanatory quantity. Its immutable
    # journal supplies the exact amount, with company-wide attribution only.
    return (
        {"component": "investment_vat", "attribution": "company_pool", "amount_fen": str(value)},
    )


def _origin(quantity, ledger, read, cache):
    if quantity["attribution"] == "company_pool":
        return None, None, None
    original = source_contents(ledger, [quantity["source"]], read, cache)
    target = original[quantity["source"]["result_id"]].target
    values = target["values"]
    events = [values["tax_event"]] if "tax_event" in values else values.get("tax_events", ())
    index = quantity["event_index"]
    if (
        not target["kind"].startswith("investment.")
        or index >= len(events)
        or events[index]["terms_id"] != quantity["terms_id"]
        or events[index]["product_id"] != quantity["product_id"]
    ):
        fail("投资利息税调整与原正式投资身份不一致。", "storage_corrupt")
    source = quantity["source"]
    key = source["business_id"], source["result_slot"], index
    return quantity["terms_id"], quantity["product_id"], key


def income(ledger, scope, read, cache):
    contributions = list(_pages(lambda page: ledger.profit_contributions(scope, page, read)))
    bodies = _contents(
        ledger,
        [
            ref
            for row in contributions
            for ref in (row.reference, row.baseline_reference)
            if ref is not None
        ],
        read,
        cache,
    )
    source_contents(
        ledger,
        [
            quantity["source"]
            for body in bodies.values()
            if body.target["kind"] in TAX_KINDS
            for quantity in body.basis["adopted_quantities"]
            if quantity["type"] == "investment_income_adjustment"
            and quantity["attribution"] == "event"
        ],
        read,
        cache,
    )
    profits, groups = {}, {}
    recognized, losses, net = [], [], []
    for row in contributions:
        if bodies[row.reference["result_id"]].source != row.source or (
            row.baseline_reference is not None
            and bodies[row.baseline_reference["result_id"]].source != row.baseline_source
        ):
            fail("投资收益贡献与精确正式正文的来源不一致。", "storage_corrupt")
        net.extend((row.investment_income_fen, -row.baseline_income_fen))
        for sign, ref, value, loss in (
            (1, row.reference, row.investment_income_fen, row.actual_loss_fen),
            (-1, row.baseline_reference, row.baseline_income_fen, row.baseline_actual_loss_fen),
        ):
            if ref is None:
                continue
            body = bodies[ref["result_id"]]
            profits[ref["result_id"]] = value, loss
            losses.append(sign * loss)
            if body.target["kind"] == "system.withdrawal":
                if value or loss:
                    fail("已退出的投资目标仍含有效损益凭证。", "storage_corrupt")
                continue
            if body.target["kind"] not in TAX_KINDS:
                if not body.target["kind"].startswith("investment."):
                    fail("正式投资收益存在没有专业归属的来源。", "storage_corrupt")
                recognized.append(sign * value)
                continue
            if loss:
                fail("投资税费来源不能混入实际投资损失。", "storage_corrupt")
            for quantity in _quantities(body, value):
                terms_id, product_id, origin = _origin(quantity, ledger, read, cache)
                key = (
                    row.reference["result_id"],
                    quantity["component"],
                    quantity["attribution"],
                    terms_id,
                    product_id,
                    origin,
                )
                adjustment = groups.get(key)
                if adjustment is None:
                    adjustment = InvestmentIncomeAdjustment(
                        row.reference["result_id"] + f":tax:{len(groups)}",
                        ref["business_id"],
                        row.reference["posting_period"]
                        if scope["view"] == "as_posted"
                        else ref["business_period"],
                        quantity["component"],
                        quantity["attribution"],
                        0,
                        row.source,
                        row.reference,
                        row.baseline_reference is not None,
                        terms_id,
                        product_id,
                    )
                groups[key] = replace(
                    adjustment,
                    amount_fen=total(
                        (adjustment.amount_fen, sign * amount(quantity["amount_fen"]))
                    ),
                )
    adjustments = tuple(item for item in groups.values() if item.amount_fen)
    gross, tax = total(recognized), total(item.amount_fen for item in adjustments)
    if total((gross, tax)) != total(net):
        fail("投资收益与正式总账贡献不能对平。", "storage_corrupt")
    return profits, tuple(adjustments), gross, tax, total(net), total(losses)
