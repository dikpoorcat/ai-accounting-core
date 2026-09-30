"""Read-only v1 financial-position classification used by frozen owner review.

The snapshot supplies exact stored sources; this module retains the v1 rule.
"""

import json
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from itertools import islice
from typing import Any

from .report_party_v1 import report_party_splits

CASH_ACCOUNTS = {"1001", "1002", "1012"}
PROFIT_ACCOUNTS = {
    "5001": (1, -1),
    "5111": (20, -1),
    "5401": (2, 1),
    "540101": (2, 1),
    "540104": (2, 1),
    "540102": (2, 1),
    "540103": (2, 1),
    "5403": (3, 1),
    "5601": (11, 1),
    "560101": (11, 1),
    "560102": (11, 1),
    "560103": (11, 1),
    "560104": (11, 1),
    "5602": (14, 1),
    "560201": (14, 1),
    "560202": (14, 1),
    "560203": (14, 1),
    "560204": (14, 1),
    "5603": (18, 1),
    "560301": (18, 1),
    "6301": (22, -1),
    "630101": (22, -1),
    "571101": (24, 1),
    "571102": (24, 1),
    "571103": (24, 1),
    "571104": (24, 1),
    "5801": (31, 1),
}
DEBIT_BALANCE = {
    "1101": 2,
    "1121": 3,
    "1131": 6,
    "1132": 7,
    "1403": 10,
    "1405": 12,
    "1411": 13,
    "4301": 9,
    "1501": 16,
    "1511": 17,
    "1601": 18,
    "1604": 21,
    "1605": 22,
    "1606": 23,
    "1621": 24,
    "1701": 25,
    "1801": 27,
    "189901": 28,
}
CREDIT_BALANCE = {
    "1602": 19,
    "1702": 25,
    "2001": 31,
    "2201": 32,
    "221101": 35,
    "221102": 35,
    "221103": 35,
    "2231": 37,
    "2232": 38,
    "2501": 42,
    "2701": 43,
    "2401": 44,
    "3001": 48,
    "3002": 49,
    "3101": 50,
    "3103": 51,
    "3104": 51,
}
TAX_ACCOUNTS = {"222101", "222102", "222103", "222104", "222105", "222106"}
RECLASS = {
    "1122": (4, 34),
    "1123": (5, 33),
    "1221": (8, 39),
    "122101": (8, 39),
    "122105": (8, 39),
    "2202": (5, 33),
    "2203": (4, 34),
    "2241": (8, 39),
    "224101": (8, 39),
    "224102": (8, 39),
    "224103": (8, 39),
    "224104": (8, 39),
    "224105": (8, 39),
}

KNOWN_POSITION_ACCOUNTS = (
    CASH_ACCOUNTS
    | set(PROFIT_ACCOUNTS)
    | set(DEBIT_BALANCE)
    | set(CREDIT_BALANCE)
    | TAX_ACCOUNTS
    | set(RECLASS)
)


@dataclass(frozen=True)
class _VerifiedClosePrefix:
    connection: object
    lease: object
    closes: tuple


def _verified_close_prefix(connection, closes):
    """Carry closes already proved by the integrity loop into its owner review."""
    from .verified_source_lease import current_verified_lease

    return _VerifiedClosePrefix(connection, current_verified_lease(connection), tuple(closes))


def _require_close_prefix(connection, prefix, month):
    from .verified_source_lease import require_verified_lease

    if type(prefix) is not _VerifiedClosePrefix or prefix.connection is not connection:
        raise ValueError("verified closes belong to another connection")
    require_verified_lease(connection, prefix.lease)
    if not prefix.closes or prefix.closes[-1][0]["period"] != month:
        raise ValueError("verified closes do not end at the owner review period")
    saved = tuple(
        (row["period"], row["digest"])
        for row in connection.execute(
            "SELECT period,digest FROM period_close WHERE period<=? ORDER BY period", (month,)
        )
    )
    if saved != tuple((row["period"], row["digest"]) for row, _ in prefix.closes):
        raise ValueError("verified closes omit or alter a saved close")
    return prefix.closes


class PositionInputsV1:
    """Only the stored sources needed to replay the v1 close position."""

    def __init__(self, engine, connection, manifest, *, _verified_closes=None):
        from .content_v1 import _V1YearMonth
        from .history_reads_v1 import V1Reads as QueryReads

        self.engine = engine
        self.connection = connection
        self.period = manifest["period"]
        self.month = _V1YearMonth(self.period).ordinal
        self.verified_closes = (
            None
            if _verified_closes is None
            else _require_close_prefix(connection, _verified_closes, self.month)
        )
        self.reads = QueryReads(engine, connection)
        self.accounts = defaultdict(int)
        self.accounts.update(
            {row["account"]: row["debit"] - row["credit"] for row in manifest["trial_balance"]}
        )
        self.month_accounts = defaultdict(int)
        month_lines = self.reads.voucher_lines(voucher["id"] for voucher in manifest["vouchers"])
        for voucher in manifest["vouchers"]:
            for line in month_lines[voucher["id"]]:
                self.month_accounts[line["account"]] += line["debit"] - line["credit"]
        self.opening_selection = {"unestablished_state_selections": []}
        if self.verified_closes is None:
            first = connection.execute(
                "SELECT * FROM period_close ORDER BY period LIMIT 1"
            ).fetchone()
            from .close_storage_v1 import decode_close

            first_manifest = (
                manifest if first["period"] == self.month else decode_close(connection, first)
            )
        else:
            first_manifest = self.verified_closes[0][1]
        opening_id = first_manifest["opening_calculation_id"]
        self.openings = [self.reads.calculation(opening_id)] if opening_id else []
        self.journal = _V1SelectedJournal(self)

    def fact(self, ident):
        return self.reads.fact(ident)


class _V1SelectedJournal:
    def __init__(self, inputs):
        self.inputs = inputs

    def select(self, *, accounts):
        from .close_storage_v1 import decode_close
        from .report_projection_v1 import selected_voucher_sql

        inputs = self.inputs
        candidate_ids = {
            row[0]
            for row in inputs.connection.execute(
                "SELECT DISTINCT l.version_id FROM voucher_line l "
                "JOIN voucher_version v ON v.id=l.version_id "
                "WHERE l.account IN (SELECT value FROM json_each(?)) AND v.period<=?",
                (json.dumps(sorted(accounts)), inputs.month),
            )
        }
        if not candidate_ids:
            return []
        adopted = {}
        if inputs.verified_closes is None:
            closes = (
                (row, decode_close(inputs.connection, row))
                for row in inputs.connection.execute(
                    "SELECT * FROM period_close WHERE period<=? ORDER BY period", (inputs.month,)
                )
            )
        else:
            closes = inputs.verified_closes
        for row, close_manifest in closes:
            for voucher in close_manifest["vouchers"]:
                if voucher["id"] in candidate_ids:
                    adopted[voucher["id"]] = row["period"]
        source, parameters = selected_voucher_sql(
            inputs.period, voucher_ids=candidate_ids, authoritative_vouchers=adopted
        )
        result = []
        cursor = inputs.connection.execute(source + " ORDER BY period,number,id", parameters)
        while selected_rows := list(islice(cursor, 512)):
            selected_lines = inputs.reads.voucher_lines(row["id"] for row in selected_rows)
            for row in selected_rows:
                lines = selected_lines[row["id"]]
                if any(line["account"] in accounts for line in lines):
                    result.append({**dict(row), "lines": lines})
        return result


def _issue(field, message, **details):
    return {"field": field, "message": message, "semantics": "accounting", **details}


def classify_financial_position(rows: Iterable[Mapping[str, Any]]) -> dict:
    """Classify signed account rows after netting each (account, stable party key)."""

    direct: dict[str, int] = defaultdict(int)
    parties: dict[tuple[str, Hashable], int] = defaultdict(int)
    unknown_lines: set[int] = set()
    unknown_account = False
    issues: list[dict] = []
    known = KNOWN_POSITION_ACCOUNTS
    for index, row in enumerate(rows):
        account = row.get("account")
        amount = row.get("amount_fen", row.get("amount"))
        if not isinstance(account, str) or type(amount) is not int:
            issues.append(
                _issue("financial_position.row", "财务位置行缺少有效科目或整数分金额", row=index)
            )
            unknown_account = True
            continue
        if not amount:
            continue
        if account not in known:
            issues.append(_issue("account_mapping", "存在未映射的非零账户余额", account=account))
            unknown_account = True
            continue
        if account not in RECLASS:
            direct[account] += amount
            continue
        splits = row.get("party_splits")
        if (
            splits is None
            and row.get("party_key") is not None
            and row.get("party_state") != "unresolved"
        ):
            splits = ((row["party_key"], amount),)
        if splits is None and row.get("party") is not None:
            party = row["party"]
            party_key = party if isinstance(party, tuple) else ("party", party)
            splits = ((party_key, amount),)
        valid = splits is not None
        normalized = []
        if valid:
            for split in splits:
                if isinstance(split, Mapping):
                    party, part = split.get("party_key"), split.get("amount_fen")
                else:
                    try:
                        party, part = split
                    except (TypeError, ValueError):
                        valid = False
                        break
                if party is None or not isinstance(party, Hashable) or type(part) is not int:
                    valid = False
                    break
                normalized.append((party, part))
            valid = valid and sum(part for _, part in normalized) == amount
        if not valid:
            affected = RECLASS[account]
            unknown_lines.update(affected)
            issues.append(
                _issue(
                    "financial_position.party",
                    "往来余额缺少精确稳定归属，不能跨未知对象抵销",
                    account=account,
                    version_id=row.get("reverses_id") or row.get("version_id"),
                    line_no=row.get("line_no"),
                    affected_lines=list(affected),
                )
            )
            continue
        for party, part in normalized:
            parties[(account, party)] += part

    result: dict[int, int | None] = {line: 0 for line in range(1, 54)}
    for (account, _party_key), amount in parties.items():
        result[RECLASS[account][0 if amount >= 0 else 1]] += abs(amount)
    for account, amount in direct.items():
        if account in CASH_ACCOUNTS:
            result[1] += amount
        elif account in PROFIT_ACCOUNTS:
            result[51] -= amount
        elif account in DEBIT_BALANCE:
            result[DEBIT_BALANCE[account]] += amount
        elif account in CREDIT_BALANCE:
            result[CREDIT_BALANCE[account]] += amount if account == "1702" else -amount
        elif account in TAX_ACCOUNTS:
            result[14 if amount > 0 else 36] += abs(amount)
    for line in unknown_lines:
        result[line] = None

    def total(lines: Iterable[int], *, subtract=()) -> int | None:
        selected = tuple(lines)
        if any(result[line] is None for line in (*selected, *subtract)):
            return None
        return sum(result[line] for line in selected) - sum(result[line] for line in subtract)

    inventory_total = total((10, 11, 12, 13))
    result[9] = None if inventory_total is None else result[9] + inventory_total
    result[20] = total((18,), subtract=(19,))
    result[15] = total((*range(1, 10), 14))
    result[29] = total((16, 17, 20, 21, 22, 23, 24, 25, 26, 27, 28))
    result[30] = total((15, 29))
    result[41] = total(range(31, 41))
    result[46] = total(range(42, 46))
    result[47] = total((41, 46))
    result[52] = total(range(48, 52))
    result[53] = total((47, 52))
    if unknown_account:
        for line in (30, 47, 52, 53):
            result[line] = None
    equation_valid = None if result[30] is None or result[53] is None else result[30] == result[53]
    return {
        "lines": result,
        "assets_fen": result[30],
        "liabilities_fen": result[47],
        "capital_fen": total((48, 49, 50)),
        "cumulative_result_fen": result[51],
        "equity_fen": result[52],
        "equation_valid": equation_valid,
        "complete": not issues,
        "issues": issues,
    }


def _position_classification_ids(connection, month):
    """Select only party-relevant v1 facts from the exact historical reference set."""

    # A released v1 bundle can predate the report-classification fact type and
    # therefore have no typed classification tables. The historical reader
    # only queried those tables when the authoritative fact set was nonempty.
    # Keep that gate; a missing table with an actual candidate still fails.
    closed_candidates = (
        "SELECT f.id FROM subject s "
        "CROSS JOIN fact_revision f ON f.subject_id=s.id "
        "WHERE s.kind='report_classification' AND EXISTS("
        "SELECT 1 FROM close_reference r "
        "WHERE r.reference_type='fact' AND r.reference_id=f.id "
        "AND r.path='readiness.financial_reports.facts[*]' AND r.close_period<=?) "
    )
    open_candidates = (
        "SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
        "JOIN subject s ON s.id=f.subject_id WHERE s.kind='report_classification' "
        "AND f.period<=? "
        "AND NOT EXISTS(SELECT 1 FROM period_close p WHERE p.period=f.period)"
    )
    if connection.execute(
        "SELECT 1 WHERE EXISTS(" + closed_candidates + ") OR EXISTS(" + open_candidates + ")",
        (month, month),
    ).fetchone() is None:
        return set()

    return {
        row[0]
        for row in connection.execute(
            "WITH selected(id) AS MATERIALIZED ("
            + closed_candidates
            + "UNION "
            + open_candidates
            + "), duplicate_vouchers AS (SELECT c.voucher_version_id "
            "FROM selected ids JOIN fact_report_classification c ON c.revision_id=ids.id "
            "GROUP BY c.voucher_version_id HAVING count(*)>1) "
            "SELECT ids.id FROM selected ids LEFT JOIN fact_report_classification c "
            "ON c.revision_id=ids.id WHERE c.revision_id IS NULL "
            "OR c.voucher_version_id IN (SELECT voucher_version_id FROM duplicate_vouchers) "
            "OR EXISTS(SELECT 1 FROM fact_report_classification_counterparties child "
            "WHERE child.revision_id=ids.id)",
            (month, month),
        )
    }


def position_v1(snap):
    balances = snap.accounts
    source_issues = []
    known = KNOWN_POSITION_ACCOUNTS
    detailed_accounts = set(RECLASS) | (set(balances) - known)
    rows = [
        {"account": account, "amount": value}
        for account, value in balances.items()
        if account not in detailed_accounts
    ]
    projected = defaultdict(int)
    from .settlement_freeze_v1 import frozen_position_rows

    position_obligations = frozen_position_rows(snap.connection, snap.period, set(RECLASS))
    if position_obligations is None:
        raise ValueError("v1 owner review requires a sealed settlement source")
    fallback_accounts = {
        obligation["account"]
        for obligation in position_obligations
        if obligation["account"] in RECLASS
        and (obligation["counterparty_id"] is None or obligation["unknown"])
    }
    # Report classifications may contain only profit or cash details. Those
    # facts do not affect a financial-position party split. Keep duplicate
    # voucher classifications and missing typed rows in the checked set, but
    # load party facts only when they actually contain party detail.
    position_classification_ids = _position_classification_ids(snap.connection, snap.month)
    classifications, ambiguous = {}, set()
    has_explicit_party = False
    for ident in sorted(position_classification_ids):
        data = snap.fact(ident)["data"]
        key = data["voucher_version_id"]
        has_explicit_party = has_explicit_party or bool(data["counterparties"])
        if key in classifications:
            ambiguous.add(key)
            source_issues.append(
                {
                    "field": "report_classification",
                    "message": "同一凭证版本存在多个分类来源",
                    "voucher_version_id": key,
                }
            )
        classifications[key] = {
            item["line_no"]: item["counterparty_id"] for item in data["counterparties"]
        }
    for key in ambiguous:
        classifications[key] = {}
    if classifications:
        classified_accounts = {
            row[0]
            for row in snap.connection.execute(
                "SELECT DISTINCT l.account FROM json_each(?) ids "
                "JOIN voucher_line l ON l.version_id=ids.value "
                "WHERE l.account IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(classifications)), json.dumps(sorted(RECLASS))),
            )
        }
        # A stale party classification still takes the exact line path. A
        # profit-only classification cannot force all reclassified accounts
        # through historical journal hydration.
        fallback_accounts.update(
            classified_accounts or (RECLASS if has_explicit_party or ambiguous else ())
        )
    journal_accounts = fallback_accounts | (set(balances) - known) if fallback_accounts else set()
    if journal_accounts:
        events = list(snap.journal.select(accounts=journal_accounts))
        from .query_relations_v1 import resolve_calculation_relations

        resolutions = snap.reads.relations_many(
            (event["basis_calculation_id"] for event in events),
            resolver=resolve_calculation_relations,
        )
        for event in events:
            resolution = resolutions[event["basis_calculation_id"]]
            for line in event["lines"]:
                if line["account"] not in journal_accounts:
                    continue
                row = {
                    **line,
                    "amount": line["debit"] - line["credit"],
                    "version_id": event["id"],
                    "reverses_id": event["reverses_id"],
                }
                if line["account"] in RECLASS:
                    party = report_party_splits(
                        row,
                        resolution,
                        explicit_party_id=classifications.get(
                            event["reverses_id"] or event["id"], {}
                        ).get(line["line_no"]),
                    )
                    row["party_splits"] = party["splits"]
                    source_issues.extend(party["issues"])
                rows.append(row)
        for calculation in snap.openings:
            members = calculation["outcome"]["values"].get("members")
            if members is None:
                members = [calculation["outcome"]]
            for member in members:
                for line in member.get("opening_lines", ()):
                    if line["account"] not in journal_accounts:
                        continue
                    parties = {
                        item.get("counterparty_id")
                        for item in member.get("values", {}).get("obligations", ())
                        if item["account"] == line["account"]
                    }
                    party = next(iter(parties)) if len(parties) == 1 else None
                    rows.append(
                        {
                            **line,
                            "amount": line["debit"] - line["credit"],
                            "party_key": ("party", party) if party else None,
                        }
                    )
    for obligation in position_obligations:
        account = obligation.get("account")
        if account not in RECLASS or account in fallback_accounts:
            continue
        remaining = None if obligation["unknown"] else obligation["remaining"]
        category = obligation.get("category")
        amount = (
            remaining
            if category == "receivable" and type(remaining) is int
            else -remaining
            if category == "payable" and type(remaining) is int
            else None
        )
        if type(amount) is int:
            projected[account] += amount
        rows.append(
            {
                "account": account,
                "amount": amount,
                "party_key": (
                    ("party", obligation["counterparty_id"])
                    if obligation.get("counterparty_id")
                    else None
                ),
            }
        )
    for account in detailed_accounts - journal_accounts:
        residual = balances[account] - projected[account]
        if residual:
            rows.append({"account": account, "amount": residual})
    position = classify_financial_position(rows)
    if snap.opening_selection["unestablished_state_selections"]:
        source_issues.append(
            {
                "field": "opening.selection",
                "message": "期初冻结采用依据未能建立，保留精确追溯。",
                "selections": snap.opening_selection["unestablished_state_selections"],
            }
        )
        position.update(assets_fen=None, liabilities_fen=None, equation_valid=None, complete=False)
    assets, liabilities = position["assets_fen"], position["liabilities_fen"]

    def result(values):
        revenue = -sum(
            value
            for account, value in values.items()
            if PROFIT_ACCOUNTS.get(account, (0, 0))[1] == -1
        )
        expense = sum(
            value
            for account, value in values.items()
            if PROFIT_ACCOUNTS.get(account, (0, 0))[1] == 1
        )
        # An account outside every mapping table reaches neither sum, so its amount would
        # silently understate revenue or expense; collect it instead of dropping it.
        for account, value in values.items():
            if value and account not in known:
                source_issues.append(
                    {
                        "field": "account_mapping",
                        "message": "存在未映射的非零账户余额，本月收入、费用不含该金额",
                        "semantics": "accounting",
                        "account": account,
                        "amount_fen": value,
                    }
                )
        return revenue, expense, revenue - expense

    revenue, expense, monthly = result(snap.month_accounts)
    fixed, intangible = balances["1601"] + balances["1602"], balances["1701"] + balances["1702"]
    return {
        "assets_fen": assets,
        "liabilities_fen": liabilities,
        "capital_fen": position["capital_fen"],
        "equity_fen": position["equity_fen"],
        "bank_fen": balances["1002"],
        "liability_calculation": {
            "current_fen": position["lines"][41],
            "non_current_fen": position["lines"][46],
        },
        "fixed_asset_cost_fen": balances["1601"],
        "accumulated_depreciation_fen": -balances["1602"],
        "fixed_asset_net_fen": fixed,
        "intangible_asset_cost_fen": balances["1701"],
        "accumulated_amortization_fen": -balances["1702"],
        "intangible_asset_net_fen": intangible,
        "other_assets_fen": None
        if assets is None
        else assets - balances["1002"] - fixed - intangible,
        "month_revenue_fen": revenue,
        "month_expense_fen": expense,
        "month_result_fen": monthly,
        "cumulative_result_fen": position["cumulative_result_fen"],
        "equation_valid": position["equation_valid"],
        "complete": position["complete"] and not source_issues,
        "issues": [*source_issues, *position["issues"]],
    }
