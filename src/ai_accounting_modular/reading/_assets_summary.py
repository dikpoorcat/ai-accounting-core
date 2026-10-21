"""Complete asset totals are prepared before selecting either collection's page."""

from dataclasses import asdict

from ai_accounting_modular.contracts.public import ContractError, format_fen, parse_fen, sum_fen

from ._assets_cards import amount

_LEDGER_FIELDS = {
    "fixed_asset_cost_fen": "fixed_cost",
    "accumulated_depreciation_fen": "fixed_accumulated",
    "fixed_asset_net_fen": "fixed_net",
    "intangible_asset_cost_fen": "intangible_cost",
    "accumulated_amortization_fen": "intangible_accumulated",
    "intangible_asset_net_fen": "intangible_net",
    "ledger_cost_fen": "long_term_cost",
    "ledger_accumulated_fen": "long_term_accumulated",
    "ledger_net_fen": "long_term_net",
}


def _total(*values):
    return None if None in values else format_fen(sum_fen(values))


def _type_summary(value):
    fixed = value.asset_type == "fixed"
    result = {
        key: getattr(value, key)
        for key in (
            "registered_count",
            "unestablished_count",
            "active_count",
            "pending_count",
            "month_acquired_count",
        )
    }
    result.update(
        {
            key: amount(getattr(value, key))
            for key in (
                "active_cost_fen",
                "active_accumulated_fen",
                "active_net_fen",
                "pending_cost_fen",
                "month_acquired_fen",
                "month_cost_adjustment_fen",
            )
        }
    )
    result["month_depreciation_fen" if fixed else "month_amortization_fen"] = amount(
        value.month_charge_fen
    )
    result["disposed_count" if fixed else "retired_count"] = value.exited_count
    result["month_disposed_count" if fixed else "month_retired_count"] = value.month_exited_count
    if fixed:
        result["month_activated_count"] = value.month_activated_count
    return result


def summary(activity):
    fixed, intangible = activity.fixed, activity.intangible
    return {
        "_owner_version": activity.version,
        "_ledger": {**asdict(activity.ledger), "issues": list(activity.ledger.issues)},
        "active_count": fixed.active_count + intangible.active_count,
        "registered_count": len(activity.cards),
        "unestablished_count": sum(card.state == "unestablished" for card in activity.cards),
        "pending_fixed_count": fixed.pending_count,
        "pending_fixed_cost_fen": amount(fixed.pending_cost_fen),
        "pending_intangible_count": intangible.pending_count,
        "pending_intangible_cost_fen": amount(intangible.pending_cost_fen),
        "project_cost_fen": amount(activity.project_totals.remaining_fen),
        "established_card_totals": None,
        "month_charge_fen": _total(fixed.month_charge_fen, intangible.month_charge_fen),
        "month_acquired_count": fixed.month_acquired_count + intangible.month_acquired_count,
        "month_acquired_fen": _total(fixed.month_acquired_fen, intangible.month_acquired_fen),
        "month_cost_adjustment_fen": _total(
            fixed.month_cost_adjustment_fen, intangible.month_cost_adjustment_fen
        ),
        "month_activated_count": fixed.month_activated_count + intangible.month_activated_count,
        "month_exited_count": fixed.month_exited_count + intangible.month_exited_count,
        "fixed": _type_summary(fixed),
        "intangible": _type_summary(intangible),
        "checking": bool(activity.card_issues)
        or any(card.state == "unestablished" for card in activity.cards),
    }


def public_summary(saved):
    ledger = saved["_ledger"]
    if any(ledger[key]["state"] != "known" for key in _LEDGER_FIELDS.values()):
        raise ContractError(
            "needs_information",
            "所选月份缺少正式期初或资产余额依据，暂不能展示完整资产汇总。",
            fact_issues=ledger["issues"],
        )
    result = {key: value for key, value in saved.items() if not key.startswith("_")}
    result.update({key: ledger[source]["amount_fen"] for key, source in _LEDGER_FIELDS.items()})
    result["active_ledger_net_fen"] = format_fen(
        sum_fen(
            (
                parse_fen(ledger["fixed_net"]["amount_fen"]),
                parse_fen(ledger["intangible_net"]["amount_fen"]),
            )
        )
    )
    fixed, intangible = result["fixed"], result["intangible"]
    represented_cost = _total(
        *(
            parse_fen(value) if value is not None else None
            for value in (
                fixed["active_cost_fen"],
                intangible["active_cost_fen"],
                fixed["pending_cost_fen"],
                intangible["pending_cost_fen"],
                result["project_cost_fen"],
            )
        )
    )
    represented_accumulated = _total(
        *(
            parse_fen(value) if value is not None else None
            for value in (fixed["active_accumulated_fen"], intangible["active_accumulated_fen"])
        )
    )
    result["checking"] |= (
        represented_cost != result["ledger_cost_fen"]
        or represented_accumulated != result["ledger_accumulated_fen"]
    )
    return result
