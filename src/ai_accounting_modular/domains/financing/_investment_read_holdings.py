"""One row per sourced holding, with legacy monetary funds kept separate."""

from collections import defaultdict
from decimal import Decimal

from ai_accounting_modular.ledger.opening.public import (
    OpeningInvestmentReference,
    opening_obligation_references,
)
from ai_accounting_modular.ledger.public import ObligationPositionQuery

from ._common import amount, fail, total
from ._investment_events import EVENT_KINDS
from ._investment_read_sources import source_contents
from ._read_types import InvestmentProduct


def known(value):
    return {"state": "known", "amount_fen": str(value)}


def add(cell, value):
    return known(total((amount(cell["amount_fen"]), value))) if cell["state"] == "known" else cell


def pending_income(ledger, scope, selected, read, cache):
    query = ObligationPositionQuery(
        scope["period_to"],
        categories=("investment",),
        direction="receivable",
        include_settled=False,
    )
    positions = ledger.capture_obligation_positions(query, read, selection=selected).items
    bodies = source_contents(ledger, [item.definition.source for item in positions], read, cache)
    pending, receipts, terms_sources = defaultdict(int), defaultdict(int), []
    for item in positions:
        body = bodies[item.definition.source["result_id"]]
        if body.target["kind"] == "opening.package":
            reference = next(
                (
                    value
                    for value in opening_obligation_references(body)
                    if isinstance(value, OpeningInvestmentReference)
                    and value.source == item.definition.reference
                ),
                None,
            )
            if reference is None:
                fail("投资期初收益义务缺少准确成员来源。", "storage_corrupt")
            terms_id = reference.terms_id
            terms_sources.extend(
                source
                for source in body.basis["source_results"]
                if source["business_id"] == terms_id
            )
        elif body.target["kind"] in EVENT_KINDS:
            terms_id = body.target["values"]["terms_id"]
        elif body.target["kind"] == "investment.money_fund_redemption":
            terms_id = "legacy:" + body.target["values"]["fund_id"]
        else:
            continue
        receipts[terms_id] += item.amounts.remaining_fen
        if item.definition.value["account_code"] in {"1131", "1132"}:
            pending[terms_id] += item.amounts.remaining_fen
    return pending, terms_sources, receipts


def activity_descriptors(ledger, identifiers, sources, read, cache):
    """Income-only rights have no inventory snapshot; use their frozen terms, not zeros."""
    bodies = tuple(cache.values())
    for body in bodies:
        if (
            body.target["kind"] in EVENT_KINDS - {"investment.income"}
            and body.target["values"].get("terms_id") in identifiers
        ):
            fail("投资库存活动缺少同源正式月度持仓。", "storage_corrupt")
    refs = [
        source
        for source in sources
        if source["business_id"] in identifiers and source["result_slot"] == "main"
    ]
    refs.extend(
        source
        for body in bodies
        if body.target["kind"] != "system.withdrawal"
        for source in body.basis["source_results"]
        if source["business_id"] in identifiers and source["result_slot"] == "main"
    )
    adopted = source_contents(ledger, refs, read, cache).values()
    result = {}
    for body in sorted(
        adopted,
        key=lambda row: (
            row.reference["posting_period"],
            row.reference["generation"],
            row.reference["result_id"],
        ),
    ):
        if body.target["kind"] != "investment.terms":
            fail("投资收益活动没有采用正式投资条款。", "storage_corrupt")
        identifier = body.reference["business_id"]
        result[identifier] = {
            **body.target["values"],
            "terms_id": identifier,
            "quantity": None,
            "opening_cost_fen": None,
            "closing_cost_fen": None,
            "activity_only": True,
        }
    if result.keys() != identifiers:
        fail("投资收益活动缺少完整冻结条款身份。", "storage_corrupt")
    return result


def products(legacy, holdings, by_terms, names, pending, receipts, related_terms):
    rows, short = [], defaultdict(lambda: [0, 0])
    extra_opening, extra_closing = 0, 0
    for values in holdings.values():
        identifier = values["terms_id"]
        own_events = by_terms[identifier]
        activity_only = values.get("activity_only", False)
        opening, closing = (
            (0, 0)
            if activity_only
            else tuple(amount(values[key]) for key in ("opening_cost_fen", "closing_cost_fen"))
        )
        if values["measurement"] == "short_term_cost":
            short[values["product_id"]][0] += opening
            short[values["product_id"]][1] += closing
        else:
            extra_opening += opening
            extra_closing += closing
        if not any(
            (
                Decimal(values["quantity"]) if values["quantity"] is not None else None,
                receipts[identifier],
                own_events,
                opening,
                closing,
                identifier in related_terms,
            )
        ):
            continue
        rows.append(
            InvestmentProduct(
                values["product_id"],
                names[values["product_id"]],
                None if activity_only else known(opening),
                None if activity_only else known(closing),
                total(item.cost_fen for item in own_events if item.kind in {"acquire", "reinvest"}),
                total(item.cost_fen for item in own_events if item.kind in {"dispose", "loss"}),
                total(item.gain_loss_fen for item in own_events if item.gain_loss_fen is not None),
                identifier,
                values["product_type"],
                values["measurement"],
                values["quantity"],
                values["quantity_unit"],
                pending[identifier],
                values["holding_arrangement"],
                amount(values["market_value_fen"]) if "market_value_fen" in values else None,
                values.get("market_as_of"),
                receipts[identifier],
                total(item.actual_loss_fen for item in own_events),
                activity_only,
            )
        )
    legacy_events = {item.fund_id for item in legacy.events}
    for item in legacy.products:
        opening = add(item.opening_cost, -short[item.fund_id][0])
        closing = add(item.closing_cost, -short[item.fund_id][1])
        if (
            item.fund_id not in legacy_events
            and not receipts["legacy:" + item.fund_id]
            and opening == known(0)
            and closing == known(0)
        ):
            continue
        rows.append(
            InvestmentProduct(
                item.fund_id,
                item.name,
                opening,
                closing,
                item.subscription_cost_fen,
                item.redemption_cost_fen,
                item.investment_income_fen,
                None,
                "money_fund",
                "short_term_cost",
                None,
                None,
                0,
                pending_receipts_fen=receipts["legacy:" + item.fund_id],
            )
        )
    return (
        tuple(sorted(rows, key=lambda item: (item.fund_id, item.terms_id or ""))),
        extra_opening,
        extra_closing,
    )
