"""Shared sourced positions and month amounts, independent of tax explanations."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import AmountCell, FactIssue, ReadInputs

from ._common import fail, total
from ._investment_events import EVENT_KINDS
from ._investment_read_cash import cash_events, with_product_cash
from ._investment_read_flows import flows
from ._investment_read_holdings import activity_descriptors, add, products
from ._read_funds import _pages
from ._read_types import CooperationPosition, InvestmentProduct, MoneyFundEvent


@dataclass(frozen=True)
class Components:
    opening: AmountCell
    closing: AmountCell
    products: tuple[InvestmentProduct, ...]
    events: tuple[MoneyFundEvent, ...]
    payments_fen: int
    receipts_fen: int
    cooperation: tuple[CooperationPosition, ...]
    issues: tuple[FactIssue, ...]
    inputs: ReadInputs


def _holdings(rows, month):
    values = {}
    for row in rows:
        if row.target["kind"] == "system.withdrawal":
            continue
        value = row.target["values"]
        identifier = value.get("terms_id")
        if (
            row.target["kind"] != "investment.position"
            or identifier != row.reference["business_id"]
            or value.get("as_of_period") != month
            or row.reference["result_slot"] != "position:" + month
            or identifier in values
        ):
            fail("投资正式月度持仓的身份、期间或唯一性不一致。", "storage_corrupt")
        values[identifier] = value
    return values


def _complete_holdings(ledger, holdings, related, scope, read):
    missing = sorted(related - holdings.keys())
    for offset in range(0, len(missing), 500):
        batch = tuple(missing[offset : offset + 500])
        rows = _pages(
            lambda page, batch=batch: ledger.query_adopted_targets(
                scope,
                ("investment.position",),
                page,
                read,
                business_ids=batch,
                result_slots=("position:" + scope["period_to"],),
            )
        )
        holdings.update(_holdings(rows, scope["period_to"]))


def _lot_businesses(holdings):
    return {
        source["business_id"]
        for values in holdings.values()
        for lot in values.get("lots", ())
        for name in ("source", "origin_source")
        if (source := lot.get(name)) is not None
    }


def compose(
    owner,
    scope,
    selected,
    read,
    cache,
    *,
    profits,
    gross,
    actual_loss,
    position_rows,
    legacy,
    pending_income,
    adjustments=(),
):
    ledger = owner._ledger
    events, by_terms = flows(ledger, scope, read, cache, profits)
    cash, cash_producers = cash_events(ledger, scope, selected, read, cache)
    pending, receipts = pending_income.accrued, pending_income.receipts
    holdings = _holdings(position_rows, scope["period_to"])
    related = {
        item.terms_id for item in (*events, *cash, *adjustments) if item.terms_id is not None
    } | {key for key, value in receipts.items() if value and not key.startswith("legacy:")}
    _complete_holdings(ledger, holdings, related, scope, read)
    missing = related - holdings.keys()
    if missing:
        holdings.update(
            activity_descriptors(ledger, missing, pending_income.terms_sources, read, cache)
        )
    for item in (*events, *cash, *adjustments):
        if item.terms_id is None:
            continue
        product_id = item.product_id if hasattr(item, "product_id") else item.fund_id
        if holdings[item.terms_id]["product_id"] != product_id:
            fail("投资活动与正式月度持仓的产品身份不一致。", "storage_corrupt")
    identifiers = sorted({value["product_id"] for value in holdings.values()})
    names = {item.fund_id: item.name for item in legacy.products if item.fund_id in identifiers}
    unnamed = [identifier for identifier in identifiers if identifier not in names]
    for offset in range(0, len(unnamed), 500):
        names.update(
            (item["object_id"], item["name"])
            for item in owner._identity.get_batch(unnamed[offset : offset + 500], read)
        )
    if set(names) != set(identifiers):
        fail("正式投资持仓缺少具体产品身份。", "storage_corrupt")
    output, extra_opening, extra_closing = products(
        legacy, holdings, by_terms, names, pending, receipts, related
    )
    if total(item.investment_income_fen for item in output) != gross:
        fail("投资产品确认收益与正式总账贡献不能对平。", "storage_corrupt")
    if total(item.actual_loss_fen for item in output) != actual_loss:
        fail("投资产品实际损失与正式总账贡献不能对平。", "storage_corrupt")
    combined = tuple(
        sorted(
            [*legacy.events, *events, *cash],
            key=lambda item: (item.period, item.actual_on or "", item.business_id, item.event_id),
        )
    )
    payments = total(
        (
            legacy.actual_payments_fen,
            total(item.settlement_fen for item in cash if item.kind == "acquisition_payment"),
        )
    )
    received = total(
        (
            legacy.actual_receipts_fen,
            total(item.settlement_fen for item in cash if item.kind != "acquisition_payment"),
        )
    )
    output = with_product_cash(output, combined, payments_fen=payments, receipts_fen=received)
    cooperation = owner.cooperation_positions(scope, read)
    inputs = legacy.inputs.merge(
        cooperation.inputs,
        ReadInputs(
            result_kinds=("investment.position", "opening.package"),
            posted_result_kinds=tuple(sorted(EVENT_KINDS)) if scope["view"] == "as_posted" else (),
            business_result_kinds=tuple(sorted(EVENT_KINDS)) if scope["view"] == "current" else (),
            obligation_categories=("investment",),
            business_ids=tuple(
                sorted(
                    {item.business_id for item in combined}
                    | set(holdings)
                    | set(pending_income.producer_ids)
                    | set(cash_producers)
                    | _lot_businesses(holdings)
                )
            ),
            object_ids=tuple(identifiers),
        ),
    )
    return Components(
        add(legacy.opening_cost, extra_opening),
        add(legacy.closing_cost, extra_closing),
        output,
        combined,
        payments,
        received,
        cooperation.positions,
        legacy.issues,
        inputs,
    )
