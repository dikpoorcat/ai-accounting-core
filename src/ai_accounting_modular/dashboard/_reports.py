"""The old three-table response maps the ledger snapshot without new arithmetic."""

import calendar

from ai_accounting_modular.contracts.public import parse_fen

_TABLES = (
    (
        "balance_sheet",
        "资产负债表",
        (("ending", "ending_fen", "期末余额"), ("beginning", "beginning_fen", "年初余额")),
    ),
    (
        "profit_statement",
        "利润表",
        (
            ("current", "current_fen", "本季金额"),
            ("year_to_date", "year_to_date_fen", "本年累计"),
        ),
    ),
    (
        "cash_flow_statement",
        "现金流量表",
        (
            ("current", "current_fen", "本季金额"),
            ("year_to_date", "year_to_date_fen", "本年累计"),
        ),
    ),
)
_TOTALS = {
    "balance_sheet": {15, 29, 30, 41, 47, 52, 53},
    "profit_statement": {19, 21, 30, 32},
    "cash_flow_statement": {7, 13, 19, 20, 22},
}


def amount(cell):
    return cell["amount_fen"] if cell["state"] == "known" else None


def response(delivery, now):
    snapshot = delivery.payload["snapshot"]
    first, last = snapshot["scope"]["period_from"], snapshot["scope"]["period_to"]
    year, quarter = int(first[:4]), (int(first[5:]) - 1) // 3 + 1
    closed = all(
        f"{year:04d}-{month:02d}" in snapshot["selection"]["closed_periods"]
        for month in range(int(first[5:]), int(last[5:]) + 1)
    )
    partial = snapshot["status"] != "ready"
    statements = []
    for key, label, columns in _TABLES:
        rows = []
        for number, row in snapshot[key].items():
            values = {target: amount(row[source]) for source, target, _ in columns}
            rows.append(
                {
                    "line": int(number),
                    "name": row["name"],
                    "values": values,
                    "is_total": int(number) in _TOTALS[key],
                    "has_amount": any(
                        value is not None and parse_fen(value) != 0 for value in values.values()
                    ),
                }
            )
        statements.append(
            {
                "key": key,
                "label": label,
                "columns": [
                    {"key": target, "label": column_label} for _, target, column_label in columns
                ],
                "rows": sorted(rows, key=lambda row: row["line"]),
            }
        )
    bs, pl, cf = (
        snapshot[key] for key in ("balance_sheet", "profit_statement", "cash_flow_statement")
    )
    organization = delivery.payload["organization"]
    messages = list(dict.fromkeys(issue["message"] for issue in snapshot["issues"]))
    failed = [check["code"] for check in snapshot["checks"] if check["status"] == "fail"]
    if failed:
        messages.append("报表勾稽需要核对：" + "、".join(failed))
    return {
        "schema_version": 5,
        "close_state": "closed" if closed else "open",
        "readiness_state": "blocked" if partial else "ready",
        "status": "blocked" if partial else "ready",
        "status_label": "资料待核对" if partial else "已准备好",
        "headline": "部分报表资料待核对" if partial else "季度财务报表",
        "message": "；".join(messages) if partial else "报表按正式账务结果编制。",
        "checked_at": now.isoformat(),
        "organization": {
            "name": organization["name"],
            "taxpayer_identification_number": organization["taxpayer_id"],
        },
        "period": {
            "year": year,
            "quarter": quarter,
            "quarter_start": first + "-01",
            "quarter_end": last + f"-{calendar.monthrange(year, int(last[5:]))[1]:02d}",
            "label": f"{year}年第{quarter}季度",
        },
        "summary": {
            "assets_total_fen": amount(bs["30"]["ending"]),
            "liabilities_total_fen": amount(bs["47"]["ending"]),
            "liabilities_equity_total_fen": amount(bs["53"]["ending"]),
            "current_net_profit_fen": amount(pl["32"]["current"]),
            "year_to_date_net_profit_fen": amount(pl["32"]["year_to_date"]),
            "current_cash_change_fen": amount(cf["20"]["current"]),
            "ending_cash_fen": amount(cf["22"]["current"]),
        },
        "statements": statements,
        "draft": partial or not closed,
        # Workbook export needs an installed producer and a real captured plan.
        "export": {
            "available": False,
            "file_name": f"{year}年第{quarter}季度财务报表.xlsx",
            "preview_digest": None,
            "epochs": None,
        },
        "read_context": {
            "company_id": snapshot["company_id"],
            "database_id": snapshot["database_id"],
            "as_of": now.date().isoformat(),
            "read_version": delivery.version,
        },
    }
