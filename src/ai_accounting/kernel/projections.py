"""Compare and restore the four amount projections from published records.

The caller owns the transaction, source-content verification and repair revision.
Historical calculations are never evaluated or rewritten here.
"""

from __future__ import annotations

import json

from .contracts import KernelError
from .types import YearMonth, canonical, checked

TABLE_COLUMNS = {
    "monthly_account": ("period", "account", "debit", "credit"),
    "monthly_cashflow": ("period", "category", "amount"),
    "balance": ("category", "balance_key", "amount"),
    "opening_account": ("period", "account", "debit", "credit"),
}


def _add(totals, key, values):
    previous = totals.setdefault(key, [0] * len(values))
    for index, value in enumerate(values):
        previous[index] = checked(previous[index] + checked(value))


def expected_projections(connection, *, through_period=None, verified_calculations=None):
    """Build amounts using optional source checks from this same transaction.

    Reusing decoded immutable input does not reuse any derived expected rows.
    Voucher and calculation heads still select their own effective amounts.
    """
    accounts, cashflows, balances, openings = {}, {}, {}, {}
    restriction = " WHERE v.period<=?" if through_period is not None else ""
    parameters = (through_period,) if through_period is not None else ()
    for row in connection.execute(
        "SELECT v.period,l.account,l.debit,l.credit,l.cashflow "
        "FROM voucher_current h JOIN voucher_version v ON v.id=h.version_id "
        "JOIN voucher_line l ON l.version_id=v.id" + restriction,
        parameters,
    ):
        _add(accounts, (row["period"], row["account"]), (row["debit"], row["credit"]))
        if row["cashflow"] is not None:
            _add(cashflows, (row["period"], row["cashflow"]), (row["debit"] - row["credit"],))
    selected = list(
        connection.execute(
            "SELECT c.id,c.period FROM calculation_current h "
            "JOIN calculation c ON c.id=h.calculation_id "
            "JOIN calculation_seal s ON s.calculation_id=c.id "
            "JOIN calculation_publication p ON p.calculation_id=c.id"
        )
    )
    verified_calculations = verified_calculations or {}
    outcomes = {ident: row["decoded"] for ident, row in verified_calculations.items()}
    missing = {row["id"] for row in selected} - outcomes.keys()
    if missing:
        for row in connection.execute(
            "SELECT c.id,c.outcome FROM json_each(?) ids JOIN calculation c ON c.id=ids.value",
            (canonical(sorted(missing)),),
        ):
            outcomes[row["id"]] = json.loads(row["outcome"])
    for row in selected:
        outcome = outcomes[row["id"]]
        for effect in outcome["balances"]:
            _add(balances, (effect["category"], effect["key"]), (effect["amount"],))
        if through_period is None or row["period"] <= through_period:
            for line in outcome.get("opening_lines", ()):
                _add(openings, (row["period"], line["account"]), (line["debit"], line["credit"]))
    return {
        table: [(*key, *values) for key, values in sorted(totals.items()) if any(values)]
        for table, totals in (
            ("monthly_account", accounts),
            ("monthly_cashflow", cashflows),
            ("balance", balances),
            ("opening_account", openings),
        )
    }


def compare_projections(connection, *, through_period=None, verified_calculations=None):
    expected = expected_projections(
        connection, through_period=through_period, verified_calculations=verified_calculations
    )
    differences = []
    for table, columns in TABLE_COLUMNS.items():
        restriction = (
            " WHERE period<=?" if through_period is not None and table != "balance" else ""
        )
        parameters = (through_period,) if restriction else ()
        actual = [
            tuple(row)
            for row in connection.execute(
                f"SELECT {','.join(columns)} FROM {table}" + restriction + " ORDER BY 1,2",
                parameters,
            )
        ]
        if actual != expected[table]:
            differences.append(
                {
                    "table": table,
                    "actual_rows": len(actual),
                    "expected_rows": len(expected[table]),
                }
            )
    return {"changed": bool(differences), "differences": differences, "expected": expected}


def require_projections(connection, *, through_period=None, verified_calculations=None):
    from .period_balances import require_period_balances

    compared = compare_projections(
        connection, through_period=through_period, verified_calculations=verified_calculations
    )
    if compared["changed"]:
        raise KernelError(
            "content_integrity_failed",
            "金额投影与正式发布来源不一致，需要显式维修",
            component="projections",
            record_id="*",
            reason="projection_mismatch",
            differences=compared["differences"],
        )
    require_period_balances(connection, verified_calculations=verified_calculations)
    return {"tables": len(TABLE_COLUMNS)}


def repair_projections(connection, *, fault=None, verified_calculations=None):
    """Restore only fixed projection tables, after the caller verifies sources."""
    if not connection.in_transaction:
        raise ValueError("projection repair requires the caller's write transaction")
    from .period_balances import repair_period_balances

    compared = compare_projections(connection, verified_calculations=verified_calculations)
    if compared["changed"]:
        for table in TABLE_COLUMNS:
            connection.execute(f"DELETE FROM {table}")
            if fault:
                fault("projection_cleared", connection)
            columns = TABLE_COLUMNS[table]
            connection.executemany(
                f"INSERT INTO {table}({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                compared["expected"][table],
            )
        if fault:
            fault("projection_rebuilt", connection)
    periods = repair_period_balances(
        connection, fault=fault, verified_calculations=verified_calculations
    )
    require_projections(connection, verified_calculations=verified_calculations)
    return {
        "changed": compared["changed"] or periods["changed"],
        "differences": compared["differences"],
        "period_balances": periods,
    }


def _subject_contributions(connection, subjects):
    """Current contributions of exact affected subjects, including their reversals."""
    result = {table: {} for table in TABLE_COLUMNS}
    payload = canonical(sorted(subjects))
    for row in connection.execute(
        "SELECT v.period,l.account,l.debit,l.credit,l.cashflow FROM json_each(?) ids "
        "JOIN calculation c ON c.subject_id=ids.value "
        "JOIN voucher_version v ON v.calculation_id=c.id "
        "JOIN voucher_current h ON h.version_id=v.id "
        "JOIN voucher_line l ON l.version_id=v.id",
        (payload,),
    ):
        _add(
            result["monthly_account"],
            (row["period"], row["account"]),
            (row["debit"], row["credit"]),
        )
        if row["cashflow"] is not None:
            _add(
                result["monthly_cashflow"],
                (row["period"], row["cashflow"]),
                (row["debit"] - row["credit"],),
            )
    for row in connection.execute(
        "SELECT c.period,c.outcome FROM json_each(?) ids "
        "JOIN calculation_current h ON h.subject_id=ids.value "
        "JOIN calculation c ON c.id=h.calculation_id "
        "JOIN calculation_publication p ON p.calculation_id=c.id",
        (payload,),
    ):
        outcome = json.loads(row["outcome"])
        for effect in outcome["balances"]:
            _add(result["balance"], (effect["category"], effect["key"]), (effect["amount"],))
        for line in outcome.get("opening_lines", ()):
            _add(
                result["opening_account"],
                (row["period"], line["account"]),
                (line["debit"], line["credit"]),
            )
    return result


def _selected_projection_rows(connection, keys):
    result = {}
    for table, columns in TABLE_COLUMNS.items():
        found = {key: [0] * (len(columns) - 2) for key in keys[table]}
        for row in connection.execute(
            f"SELECT {','.join('p.' + column for column in columns)} FROM json_each(?) ids "
            f"JOIN {table} p ON p.{columns[0]}=json_extract(ids.value,'$[0]') "
            f"AND p.{columns[1]}=json_extract(ids.value,'$[1]')",
            (canonical(sorted(keys[table])),),
        ):
            found[tuple(row)[:2]] = list(tuple(row)[2:])
        result[table] = found
    return result


def prepare_projection_check(connection, prepared, *, posting_period=None):
    """Capture exact affected keys before publication, within the write lock.

    This proves the transaction's changes, not the pre-existing whole ledger.
    Full independent reconstruction remains mandatory for close and verification.
    """
    if not connection.in_transaction:
        raise ValueError("projection change checks require a write transaction")
    prepared = tuple(prepared)
    subjects = {item.version.subject_id for item in prepared}
    before = _subject_contributions(connection, subjects)
    keys = {table: set(values) for table, values in before.items()}
    if posting_period is not None:
        posting = YearMonth(posting_period).ordinal
        # A correction to zero still publishes the old voucher's reversal.
        # Its accounts/cashflow do not occur in the new (empty) outcome.
        for table in ("monthly_account", "monthly_cashflow"):
            keys[table].update((posting, key[1]) for key in before[table])
    previous_periods = {
        row[0]
        for row in connection.execute(
            "SELECT p.posting_period FROM json_each(?) ids JOIN calculation_current h "
            "ON h.subject_id=ids.value JOIN calculation_publication p "
            "ON p.calculation_id=h.calculation_id",
            (canonical(sorted(subjects)),),
        )
    }
    for item in prepared:
        outcome = item.outcome
        if outcome is None:
            continue
        if item.publication:
            actual_posting = item.publication["posting_period"]
            for table in ("monthly_account", "monthly_cashflow"):
                keys[table].update((actual_posting, key[1]) for key in before[table])
        periods = {*previous_periods, item.version.fact.period.ordinal}
        if posting_period is not None:
            periods.add(YearMonth(posting_period).ordinal)
        for line in outcome["lines"]:
            keys["monthly_account"].update((period, line["account"]) for period in periods)
            if line.get("cashflow") is not None:
                keys["monthly_cashflow"].update((period, line["cashflow"]) for period in periods)
        for line in outcome.get("opening_lines", ()):
            keys["opening_account"].add((item.version.fact.period.ordinal, line["account"]))
        keys["balance"].update(
            (effect["category"], effect["key"]) for effect in outcome["balances"]
        )
    return {
        "subjects": subjects,
        "before": before,
        "keys": keys,
        "projection": _selected_projection_rows(connection, keys),
    }


def verify_projection_change(connection, snapshot):
    """Check the four projection deltas after publication, before committing."""
    after = _subject_contributions(connection, snapshot["subjects"])
    for table in TABLE_COLUMNS:
        if not after[table].keys() <= snapshot["keys"][table]:
            raise KernelError(
                "content_integrity_failed",
                "本次发布产生未声明的投影变化",
                component="projections",
                record_id=table,
                reason="unexpected_projection_key",
            )
    actual = _selected_projection_rows(connection, snapshot["keys"])
    for table, keys in snapshot["keys"].items():
        width = len(TABLE_COLUMNS[table]) - 2
        zero = [0] * width
        for key in keys:
            previous = snapshot["projection"][table][key]
            removed = snapshot["before"][table].get(key, zero)
            added = after[table].get(key, zero)
            expected = [
                checked(checked(previous[index] - removed[index]) + added[index])
                for index in range(width)
            ]
            if actual[table][key] != expected:
                raise KernelError(
                    "content_integrity_failed",
                    "本次发布的投影变化与正式来源不一致",
                    component="projections",
                    record_id=table,
                    reason="projection_change_mismatch",
                )
    return {"status": "verified", "tables": len(TABLE_COLUMNS)}
