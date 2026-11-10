"""Public professional activity and owner positions share sourced month components."""

from dataclasses import asdict

from ai_accounting_modular.contracts.public import ReadInputs, content_digest

from ._common import fail, total
from ._investment_read_components import compose
from ._investment_read_holdings import pending_income
from ._investment_read_income import TAX_KINDS, income, recognized_income
from ._read_funds import _pages, _scope
from ._read_funds import activity as legacy_activity
from ._read_funds import positions as legacy_positions
from ._read_types import InvestmentActivity, InvestmentPositions


def _version(scope, read, value):
    return content_digest({"database_id": read.database_id, "scope": scope, **value})


def _expected(scope, version):
    if scope.get("expected_head", version) != version:
        fail("投资范围已经变化，请重新读取。", "snapshot_changed")


def activity(owner, scope, read):
    normalized, selected = _scope(scope, read)
    ledger, cache = owner._ledger, {}
    profits, adjustments, gross, vat, net, actual_loss = income(ledger, normalized, read, cache)
    pending = pending_income(ledger, normalized, selected, read, cache)
    legacy = legacy_activity(owner, normalized, read, cache=cache)
    rows = _pages(
        lambda page: ledger.query_adopted_targets(
            normalized,
            ("investment.position",),
            page,
            read,
            result_slots=("position:" + normalized["period_to"],),
        )
    )
    result = compose(
        owner,
        normalized,
        selected,
        read,
        cache,
        profits=profits,
        gross=gross,
        actual_loss=actual_loss,
        position_rows=rows,
        legacy=legacy,
        pending_income=pending,
        adjustments=adjustments,
    )
    inputs = result.inputs.merge(
        ReadInputs(
            posted_result_kinds=TAX_KINDS,
            business_result_kinds=TAX_KINDS if normalized["view"] == "current" else (),
            account_codes=("5111", "571106", "571107", "571108"),
            business_ids=tuple(sorted({body.reference["business_id"] for body in cache.values()})),
        )
    )
    version = _version(
        normalized,
        read,
        {
            "products": [asdict(item) for item in result.products],
            "events": [asdict(item) for item in result.events],
            "income_adjustments": [asdict(item) for item in adjustments],
            "income": [gross, vat, net, actual_loss],
            "cooperation": [asdict(item) for item in result.cooperation],
            "opening": result.opening,
            "closing": result.closing,
            "issues": list(result.issues),
        },
    )
    _expected(scope, version)
    return InvestmentActivity(
        version,
        result.opening,
        result.closing,
        total(item.subscription_cost_fen for item in result.products),
        total(item.redemption_cost_fen for item in result.products),
        net,
        result.payments_fen,
        result.receipts_fen,
        len(result.events) + len(adjustments),
        result.products,
        result.events,
        result.issues,
        inputs,
        result.cooperation,
        gross,
        vat,
        actual_loss,
        adjustments,
    )


def positions(owner, scope, read):
    normalized, selected = _scope(scope, read)
    ledger, cache = owner._ledger, {}
    profits, gross, actual_loss = recognized_income(ledger, normalized, read, cache)
    pending = pending_income(ledger, normalized, selected, read, cache)
    legacy_ids = tuple(
        key.removeprefix("legacy:")
        for key, value in pending.receipts.items()
        if value and key.startswith("legacy:")
    )
    legacy = legacy_positions(owner, normalized, selected, read, cache, fund_ids=legacy_ids)
    rows = _pages(lambda page: ledger.query_investment_holdings(normalized, page, read))
    result = compose(
        owner,
        normalized,
        selected,
        read,
        cache,
        profits=profits,
        gross=gross,
        actual_loss=actual_loss,
        position_rows=rows,
        legacy=legacy,
        pending_income=pending,
    )
    version = _version(
        normalized,
        read,
        {
            "products": [asdict(item) for item in result.products],
            "cooperation": [asdict(item) for item in result.cooperation],
            "closing": result.closing,
            "issues": list(result.issues),
        },
    )
    _expected(scope, version)
    return InvestmentPositions(
        version,
        result.closing,
        result.products,
        result.cooperation,
        result.issues,
        result.inputs,
    )
