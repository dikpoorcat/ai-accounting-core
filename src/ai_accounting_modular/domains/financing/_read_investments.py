"""Compose formal positions, month flows, real cash and pending income rights."""

from dataclasses import asdict

from ai_accounting_modular.contracts.public import ReadInputs, content_digest

from ._common import fail, total
from ._investment_events import EVENT_KINDS
from ._investment_read_cash import cash_events, with_product_cash
from ._investment_read_flows import flows
from ._investment_read_holdings import activity_descriptors, add, pending_income, products
from ._investment_read_income import TAX_KINDS, income
from ._read_funds import _pages, _scope
from ._read_types import InvestmentActivity


def activity(owner, scope, read):
    normalized, selected = _scope(scope, read)
    normalized.pop("expected_head", None)
    legacy = owner.money_fund_activity(normalized, read)
    ledger, cache = owner._ledger, {}
    positions = list(
        _pages(
            lambda page: ledger.query_adopted_targets(
                normalized,
                ("investment.position",),
                page,
                read,
                result_slots=("position:" + normalized["period_to"],),
            )
        )
    )
    profits, adjustments, gross, vat, net, actual_loss = income(ledger, normalized, read, cache)
    events, by_terms = flows(ledger, normalized, read, cache, profits)
    cash = cash_events(ledger, normalized, selected, read, cache)
    pending, pending_sources, receipts = pending_income(ledger, normalized, selected, read, cache)
    holdings = {
        item.target["values"]["terms_id"]: item.target["values"]
        for item in positions
        if item.target["kind"] == "investment.position"
    }
    related_terms = {
        item.terms_id for item in (*events, *cash, *adjustments) if item.terms_id is not None
    } | {key for key, value in receipts.items() if value and not key.startswith("legacy:")}
    missing = related_terms - holdings.keys()
    if missing:
        holdings.update(activity_descriptors(ledger, missing, pending_sources, read, cache))
    for item in (*events, *cash, *adjustments):
        if item.terms_id is None:
            continue
        product_id = item.product_id if hasattr(item, "product_id") else item.fund_id
        if holdings[item.terms_id]["product_id"] != product_id:
            fail("投资活动与正式月度持仓的产品身份不一致。", "storage_corrupt")
    identifiers = sorted({value["product_id"] for value in holdings.values()})
    names = {}
    for offset in range(0, len(identifiers), 500):
        names.update(
            (item["object_id"], item["name"])
            for item in owner._identity.get_batch(identifiers[offset : offset + 500], read)
        )
    output, extra_opening, extra_closing = products(
        legacy, holdings, by_terms, names, pending, receipts, related_terms
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
    payments_fen = total(
        (
            legacy.actual_payments_fen,
            total(item.settlement_fen for item in cash if item.kind == "acquisition_payment"),
        )
    )
    receipts_fen = total(
        (
            legacy.actual_receipts_fen,
            total(item.settlement_fen for item in cash if item.kind != "acquisition_payment"),
        )
    )
    output = with_product_cash(
        output, combined, payments_fen=payments_fen, receipts_fen=receipts_fen
    )
    cooperation = owner.cooperation_positions(normalized, read)
    inputs = legacy.inputs.merge(
        cooperation.inputs,
        ReadInputs(
            result_kinds=("investment.position", "opening.package"),
            posted_result_kinds=tuple(sorted(EVENT_KINDS | set(TAX_KINDS))),
            business_result_kinds=TAX_KINDS if normalized["view"] == "current" else (),
            account_codes=(
                "1101",
                "1131",
                "1132",
                "149901",
                "1501",
                "1511",
                "189902",
                "5111",
                "571106",
                "571107",
                "571108",
            ),
            obligation_categories=("investment",),
            business_ids=tuple(
                sorted(
                    {item.business_id for item in (*combined, *adjustments)}
                    | set(holdings)
                    | {body.reference["business_id"] for body in cache.values()}
                )
            ),
            object_ids=tuple(identifiers),
        ),
    )
    opening, closing = (
        add(legacy.opening_cost, extra_opening),
        add(legacy.closing_cost, extra_closing),
    )
    version = content_digest(
        {
            "database_id": read.database_id,
            "scope": normalized,
            "products": [asdict(item) for item in output],
            "events": [asdict(item) for item in combined],
            "income_adjustments": [asdict(item) for item in adjustments],
            "income": [gross, vat, net, actual_loss],
            "cooperation": [asdict(item) for item in cooperation.positions],
            "opening": opening,
            "closing": closing,
            "issues": list(legacy.issues),
        }
    )
    if scope.get("expected_head", version) != version:
        fail("投资范围已经变化，请重新读取。", "snapshot_changed")
    return InvestmentActivity(
        version,
        opening,
        closing,
        total(item.subscription_cost_fen for item in output),
        total(item.redemption_cost_fen for item in output),
        net,
        payments_fen,
        receipts_fen,
        len(combined) + len(adjustments),
        output,
        combined,
        legacy.issues,
        inputs,
        cooperation.positions,
        gross,
        vat,
        actual_loss,
        adjustments,
    )
