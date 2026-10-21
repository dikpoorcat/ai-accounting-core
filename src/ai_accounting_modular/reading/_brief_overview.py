"""Map shared complete owner totals into the existing brief, without accounting rules."""

from ai_accounting_modular.contracts.public import format_fen, parse_fen, sum_fen


def amount(cell):
    return cell["amount_fen"] if cell["state"] == "known" else None


def _sum(*values):
    return None if None in values else format_fen(sum_fen(parse_fen(value) for value in values))


def _financial(financial, funds):
    names = {
        "assets_fen": "assets",
        "liabilities_fen": "liabilities",
        "capital_fen": "capital",
        "equity_fen": "equity",
        "bank_fen": "bank",
        "fixed_asset_cost_fen": "fixed_cost",
        "accumulated_depreciation_fen": "fixed_accumulated",
        "fixed_asset_net_fen": "fixed_net",
        "intangible_asset_cost_fen": "intangible_cost",
        "accumulated_amortization_fen": "intangible_accumulated",
        "intangible_asset_net_fen": "intangible_net",
        "cumulative_result_fen": "retained_profit",
    }
    result = {name: amount(financial[source]) for name, source in names.items()}
    other = [
        result[name] for name in ("bank_fen", "fixed_asset_net_fen", "intangible_asset_net_fen")
    ]
    assets = result["assets_fen"]
    result["other_assets_fen"] = (
        None
        if assets is None or None in other
        else format_fen(sum_fen((parse_fen(assets), *(-parse_fen(value) for value in other))))
    )
    equation = [result[name] for name in ("assets_fen", "liabilities_fen", "equity_fen")]
    result.update(
        equation_valid=None
        if None in equation
        else parse_fen(equation[0]) == sum_fen(parse_fen(value) for value in equation[1:]),
        complete=all(value is not None for value in result.values()),
        issues=[
            {"message": message}
            for message in dict.fromkeys(issue["message"] for issue in financial["issues"])
        ],
        bank_calculation={
            "opening_fen": funds["bank_opening_fen"],
            "inflow_fen": funds["bank_inflow_fen"],
            "outflow_fen": funds["bank_outflow_fen"],
        },
        liability_calculation={
            "current_fen": amount(financial["current_liabilities"]),
            "non_current_fen": amount(financial["non_current_liabilities"]),
        },
    )
    return result


def _withholding(states):
    if not states:
        return "本月无个人劳务费用。"
    if states == ["not_withheld_not_filed"]:
        return "本月劳务按尚未扣缴及申报的事实入账，后续付款与申报分别核对。"
    if "not_withheld_not_filed" in states:
        return "部分劳务尚未扣缴及申报；其余按已提供的扣税依据入账，付款与申报分别核对。"
    return "劳务扣税按已提供的核算依据入账，实际付款与申报状态分别核对。"


def workforce(saved):
    employee, flags = saved["employees"], saved["_workforce"]
    fields = (
        "gross_salary_fen",
        "annual_bonus_fen",
        "employer_social_insurance_fen",
        "employer_housing_fund_fen",
        "employee_social_insurance_fen",
        "employee_housing_fund_fen",
    )
    controlled = _sum(*(employee[name] for name in fields[:4]))
    total = employee["ledger_cost_fen"]
    adjustment = (
        None
        if total is None or controlled is None
        else format_fen(sum_fen((parse_fen(total), -parse_fen(controlled))))
    )
    return {
        "has_activity": flags["has_activity"],
        "total_fen": saved["workforce_cost"]["total_fen"],
        "capitalized_labor_fen": saved["workforce_cost"]["capitalized_labor_fen"],
        "employee": {
            "has_activity": flags["employee_has_activity"],
            "breakdown_available": adjustment == "0",
            "reason": None if adjustment == "0" else "人工成本与已列组成尚需核对。",
            "total_fen": total,
            "controlled_total_fen": controlled,
            "settlement_adjustment_fen": adjustment,
            **{name: employee[name] for name in fields},
        },
        "personal_labor": {
            "has_activity": flags["labor_has_activity"],
            "breakdown_available": True,
            "reason": None,
            "total_fen": saved["workforce_cost"]["personal_labor_fen"],
            "gross_remuneration_fen": flags["labor_gross_fen"],
            "withholding_note": _withholding(flags["labor_tax_states"]),
        },
    }


def build(snapshot):
    financial = snapshot.components["financial"]["financial"]
    funds = snapshot.components["funds"]["data"]
    assets = snapshot.components["assets"]
    position = {
        name: amount(financial[source])
        for name, source in (
            ("month_revenue_fen", "revenue"),
            ("month_expense_fen", "expense"),
            ("month_result_fen", "net_profit"),
        )
    }
    position["complete"] = all(value is not None for value in position.values())
    return {
        "position": position,
        "financial_position": _financial(financial, funds),
        "workforce_cost": workforce(snapshot.components["employees"]),
        "long_term_assets": {
            "net_fen": amount(financial["long_term_net"]),
            "fixed_active_count": assets["fixed"]["active_count"],
            "intangible_active_count": assets["intangible"]["active_count"],
        },
        "funds_overview": {
            name: funds[name]
            for name in (
                "total_fen",
                "bank_fen",
                "cash_fen",
                "payment_platform_fen",
                "inflow_fen",
                "outflow_fen",
                "net_change_fen",
                "internal_transfer_fen",
            )
        },
    }
