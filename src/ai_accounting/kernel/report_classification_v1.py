"""Released v1 report-classification references for historical source checks.

The open-month comparison must not acquire future report selection rules.  Old
closed facts come from verified logical closes; current heads are considered
only for months that were still open in the v1 source database.
"""

import json

from .content_history_context import close_reader
from .report_party_v1 import RECLASS

_CASH_ACCOUNTS = frozenset({"1001", "1002", "1012"})
_PROFIT_DETAIL_ACCOUNTS = frozenset({"5403", "5601", "5602", "5603"})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _issue(field, message, **details):
    return {"field": field, "message": message, "semantics": "accounting", **details}


def _closed_sources(connection, end):
    references, vouchers = set(), set()
    for row in connection.execute(
        "SELECT * FROM period_close WHERE period<=? ORDER BY period", (end,)
    ):
        manifest = close_reader().decode_close(connection, row)
        references.update(manifest["readiness"].get("financial_reports", {}).get("facts", ()))
        vouchers.update(item["id"] for item in manifest["vouchers"])
    return references, vouchers


def selected_classifications(connection, reads, end, source_vouchers, problems):
    """Select and check the complete v1 open-report classification scope."""
    closed_references, selected_vouchers = _closed_sources(connection, end)
    references = set(closed_references)
    references.update(
        row[0]
        for row in connection.execute(
            "SELECT f.id FROM fact_current h JOIN fact_revision f ON f.id=h.fact_id "
            "JOIN subject s ON s.id=f.subject_id WHERE s.kind='report_classification' "
            "AND f.period<=? AND NOT EXISTS(SELECT 1 FROM period_close p WHERE p.period=f.period)",
            (end,),
        )
    )
    selected_vouchers.update(
        row[0]
        for row in connection.execute(
            "SELECT v.id FROM voucher_current h JOIN voucher_version v ON v.id=h.version_id "
            "WHERE v.period<=? AND NOT EXISTS("
            "SELECT 1 FROM period_close p WHERE p.period=v.period)",
            (end,),
        )
    )
    headers, conflicts = {}, set()
    if references:
        for row in connection.execute(
            "SELECT c.revision_id,c.period,c.voucher_version_id,v.period AS voucher_period "
            "FROM json_each(?) ids JOIN fact_report_classification c ON c.revision_id=ids.value "
            "LEFT JOIN voucher_version v ON v.id=c.voucher_version_id",
            (_json(sorted(references)),),
        ):
            key = row["voucher_version_id"]
            if key in headers or key in conflicts:
                problems.append(
                    _issue(
                        "report_classification",
                        "同一凭证版本存在多个分类来源",
                        voucher_version_id=key,
                    )
                )
                headers.pop(key, None)
                conflicts.add(key)
            else:
                headers[key] = row
    if not headers:
        return {}

    candidates = set(headers)
    candidates.update(
        row[0]
        for row in connection.execute(
            "SELECT v.id FROM json_each(?) ids JOIN voucher_version v ON v.reverses_id=ids.value",
            (_json(sorted(headers)),),
        )
    )
    selected_periods = {}
    for row in connection.execute(
        "SELECT v.id,v.period,v.reverses_id FROM json_each(?) ids "
        "JOIN voucher_version v ON v.id=ids.value",
        (_json(sorted(candidates & selected_vouchers)),),
    ):
        periods = selected_periods.setdefault(row["reverses_id"] or row["id"], set())
        if row["reverses_id"] is None:
            periods.add(row["period"])

    detail_ids = []
    for key, row in headers.items():
        if key not in selected_periods:
            if row["voucher_period"] != row["period"]:
                problems.append(
                    _issue(
                        "report_classification.voucher_version_id",
                        "分类必须引用本月已经存在的凭证版本",
                        voucher_version_id=key,
                    )
                )
            continue
        if row["period"] not in selected_periods[key]:
            problems.append(
                _issue(
                    "report_classification.period",
                    "分类月份必须等于原凭证记账月份",
                    voucher_version_id=key,
                )
            )
        detail_ids.append(row["revision_id"])
    for kind, accounts in (
        ("profit_details", _PROFIT_DETAIL_ACCOUNTS),
        ("counterparties", RECLASS),
        ("cash_details", _CASH_ACCOUNTS),
    ):
        for row in connection.execute(
            "SELECT c.voucher_version_id,l.account FROM json_each(?) ids "
            "JOIN fact_report_classification c ON c.revision_id=ids.value "
            f"JOIN fact_report_classification_{kind} d ON d.revision_id=c.revision_id "
            "LEFT JOIN voucher_line l ON l.version_id=c.voucher_version_id AND l.line_no=d.line_no",
            (_json(detail_ids),),
        ):
            message = (
                "分类引用不存在的凭证行"
                if row["account"] is None
                else "该凭证行不接受此类报表分类"
                if row["account"] not in accounts
                else None
            )
            if message is not None:
                problems.append(
                    _issue(
                        "report_classification.line_no",
                        message,
                        voucher_version_id=row["voucher_version_id"],
                    )
                )
    selected_ids = {row["revision_id"] for key, row in headers.items() if key in source_vouchers}
    return {
        version.fact.voucher_version_id: version.fact
        for version in reads.fact_versions(selected_ids).values()
    }
