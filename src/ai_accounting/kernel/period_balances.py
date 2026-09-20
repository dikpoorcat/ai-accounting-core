"""Repairable, posting-month balance contributions derived from publication chains."""

import json

from .contracts import KernelError
from .publication import active_tranches, verify_publication_chain, verify_record
from .types import canonical, checked, digest

COLUMNS = (
    "publication_id",
    "posting_period",
    "category",
    "balance_key",
    "component",
    "amount",
    "calculation_id",
    "source_digest",
    "baseline_calculation_id",
    "baseline_digest",
)


def _error(reason):
    raise KernelError(
        "content_integrity_failed",
        "期间余额投影与正式发布来源不一致，需要显式维修",
        component="period_balance",
        record_id="*",
        reason=reason,
    )


def _wire(row):
    return [value.hex() if isinstance(value, bytes) else value for value in row]


def expected_period_balances(connection, subject_ids=None, *, verified_calculations=None):
    """Derive rows, optionally reusing source checks from this same transaction."""
    segments = active_tranches(connection, subject_ids)
    ids = {
        r[key] for r in segments for key in ("calculation_id", "baseline_calculation_id") if r[key]
    }
    verified_calculations = verified_calculations or {}
    outcomes = {
        ident: (verified_calculations[ident]["decoded"], verified_calculations[ident]["digest"])
        for ident in ids & verified_calculations.keys()
    }
    missing = ids - outcomes.keys()
    if missing:
        for row in connection.execute(
            "SELECT id,outcome,digest FROM calculation "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(sorted(missing)),),
        ):
            outcome = json.loads(row["outcome"])
            if digest(outcome) != row["digest"]:
                _error("source_digest")
            outcomes[row["id"]] = (outcome, row["digest"])
    result = []
    for segment in segments:
        cid, baseline = segment["calculation_id"], segment["baseline_calculation_id"]
        if cid not in outcomes or (baseline and baseline not in outcomes):
            _error("missing_source")
        totals = {}
        for ident, sign in ((cid, 1), (baseline, -1)):
            if not ident:
                continue
            outcome = outcomes[ident][0]
            component = "opening" if outcome.get("opening") else "activity"
            for effect in outcome["balances"]:
                key = effect["category"], effect["key"], component
                totals[key] = checked(totals.get(key, 0) + sign * effect["amount"])
        for (category, key, component), amount in sorted(totals.items()):
            if amount:
                result.append(
                    (
                        segment["id"],
                        segment["posting_period"],
                        category,
                        key,
                        component,
                        amount,
                        cid,
                        outcomes[cid][1],
                        baseline,
                        outcomes[baseline][1] if baseline else None,
                    )
                )
    return sorted(result)


def expected_seals(rows):
    groups = {}
    for row in rows:
        groups.setdefault((row[1], row[2]), []).append(_wire(row))
    return [
        (period, category, len(items), digest(sorted(items)))
        for (period, category), items in sorted(groups.items())
    ]


def _period_seals(connection, category_seals, periods=None, *, verified_publications=None):
    """Bind every category seal, including an empty period, to its publications."""
    if verified_publications is None:
        query = "SELECT * FROM calculation_publication"
        parameters = ()
        if periods is not None:
            query += " WHERE posting_period IN (SELECT value FROM json_each(?))"
            parameters = (canonical(sorted(periods)),)
        publications = list(
            connection.execute(query + " ORDER BY posting_period,sequence", parameters)
        )
        for row in publications:
            verify_record(row)
    else:
        publications = sorted(
            verified_publications, key=lambda row: (row["posting_period"], row["sequence"])
        )
    published = {}
    for row in publications:
        published.setdefault(row["posting_period"], []).append(row["id"])
    categories = {}
    for period, category, count, checksum in category_seals:
        categories.setdefault(period, []).append([category, count, checksum.hex()])
    return [
        (
            period,
            "",
            sum(item[1] for item in categories.get(period, [])),
            digest({"publications": ids, "categories": sorted(categories.get(period, []))}),
        )
        for period, ids in sorted(published.items())
    ]


def _all_seals(connection, rows, periods=None):
    categories = expected_seals(rows)
    return sorted([*categories, *_period_seals(connection, categories, periods)])


def compare_period_balances(connection, *, verified_calculations=None):
    expected = expected_period_balances(connection, verified_calculations=verified_calculations)
    actual = [
        tuple(r)
        for r in connection.execute(
            "SELECT "
            + ",".join(COLUMNS)
            + " FROM period_balance ORDER BY publication_id,category,balance_key,component"
        )
    ]
    seals = [
        tuple(r)
        for r in connection.execute(
            "SELECT posting_period,category,row_count,digest FROM period_balance_seal ORDER BY 1,2"
        )
    ]
    expected_seal = _all_seals(connection, expected)
    return {
        "changed": actual != expected or seals != expected_seal,
        "expected": expected,
        "seals": expected_seal,
    }


def require_period_balances(connection, *, verified_calculations=None):
    verify_publication_chain(connection)
    compared = compare_period_balances(connection, verified_calculations=verified_calculations)
    if compared["changed"]:
        _error("projection_mismatch")
    totals = {}
    for row in compared["expected"]:
        key = row[2], row[3]
        totals[key] = checked(totals.get(key, 0) + row[5])
    actual = {(r[0], r[1]): r[2] for r in connection.execute("SELECT * FROM balance") if r[2]}
    if {key: value for key, value in totals.items() if value} != actual:
        _error("current_balance_mismatch")
    return {"rows": len(compared["expected"])}


def _insert(connection, rows):
    connection.executemany(
        "INSERT INTO period_balance("
        + ",".join(COLUMNS)
        + ") VALUES("
        + ",".join("?" for _ in COLUMNS)
        + ")",
        rows,
    )


def sync_period_balances(connection, subject_ids):
    subjects = canonical(sorted(set(subject_ids)))
    old = [
        r[0]
        for r in connection.execute(
            "SELECT DISTINCT b.posting_period FROM period_balance b JOIN calculation_publication p "
            "ON p.id=b.publication_id WHERE p.subject_id IN (SELECT value FROM json_each(?))",
            (subjects,),
        )
    ]
    rows = expected_period_balances(connection, subject_ids)
    periods = (
        set(old)
        | {row[1] for row in rows}
        | {
            r[0]
            for r in connection.execute(
                "SELECT DISTINCT posting_period FROM calculation_publication "
                "WHERE subject_id IN (SELECT value FROM json_each(?))",
                (subjects,),
            )
        }
    )
    connection.execute(
        "DELETE FROM period_balance WHERE publication_id IN "
        "(SELECT id FROM calculation_publication WHERE subject_id IN "
        "(SELECT value FROM json_each(?)))",
        (subjects,),
    )
    _insert(connection, rows)
    payload = canonical(sorted(periods))
    selected = [
        tuple(r)
        for r in connection.execute(
            "SELECT " + ",".join(COLUMNS) + " FROM period_balance WHERE posting_period IN "
            "(SELECT value FROM json_each(?))",
            (payload,),
        )
    ]
    connection.execute(
        "DELETE FROM period_balance_seal WHERE posting_period IN (SELECT value FROM json_each(?))",
        (payload,),
    )
    connection.executemany(
        "INSERT INTO period_balance_seal VALUES(?,?,?,?)", _all_seals(connection, selected, periods)
    )
    return sorted(periods)


def repair_period_balances(connection, *, fault=None, verified_calculations=None):
    compared = compare_period_balances(connection, verified_calculations=verified_calculations)
    if compared["changed"]:
        connection.execute("DELETE FROM period_balance")
        connection.execute("DELETE FROM period_balance_seal")
        if fault:
            fault("period_balance_cleared", connection)
        _insert(connection, compared["expected"])
        connection.executemany("INSERT INTO period_balance_seal VALUES(?,?,?,?)", compared["seals"])
    return {"changed": compared["changed"], "rows": len(compared["expected"])}


def verify_selected_balances(
    connection, through_period, category=None, *, periods=None, reads=None
):
    scope = (
        "posting_period<=?"
        if periods is None
        else "posting_period IN (SELECT value FROM json_each(?))"
    )
    scope_params = (through_period,) if periods is None else (canonical(sorted(periods)),)
    all_seals = [
        tuple(r)
        for r in connection.execute(
            "SELECT posting_period,category,row_count,digest FROM period_balance_seal "
            "WHERE " + scope + " ORDER BY 1,2",
            scope_params,
        )
    ]
    periods = {
        r[0]
        for r in connection.execute(
            "SELECT DISTINCT posting_period FROM calculation_publication WHERE " + scope,
            scope_params,
        )
    }
    if [r for r in all_seals if not r[1]] != _period_seals(
        connection,
        [r for r in all_seals if r[1]],
        periods,
        verified_publications=(
            reads.verify_publication_periods(periods) if reads is not None else None
        ),
    ):
        _error("period_coverage")
    clauses, params = ["b." + scope], list(scope_params)
    if category is not None:
        clauses.append("b.category=?")
        params.append(category)
    where = " AND ".join(clauses)
    rows = [
        tuple(r)
        for r in connection.execute(
            "SELECT "
            + ",".join("b." + col for col in COLUMNS)
            + " FROM period_balance b WHERE "
            + where,
            params,
        )
    ]
    seals = [
        tuple(r)
        for r in connection.execute(
            "SELECT b.posting_period,b.category,b.row_count,b.digest "
            "FROM period_balance_seal b WHERE b.category<>'' AND " + where + " ORDER BY 1,2",
            params,
        )
    ]
    if seals != expected_seals(rows):
        _error("content_checksum")
    bad = connection.execute(
        "SELECT 1 FROM period_balance b LEFT JOIN calculation_publication p "
        "ON p.id=b.publication_id "
        "LEFT JOIN calculation c ON c.id=b.calculation_id "
        "LEFT JOIN calculation base ON base.id=b.baseline_calculation_id WHERE "
        + where
        + " AND (p.id IS NULL OR p.calculation_id<>b.calculation_id "
        "OR p.posting_period<>b.posting_period "
        "OR p.baseline_calculation_id IS NOT b.baseline_calculation_id OR c.id IS NULL "
        "OR c.digest<>b.source_digest OR base.digest IS NOT b.baseline_digest) LIMIT 1",
        params,
    ).fetchone()
    if bad:
        _error("source_reference")


def _totals(connection, period, category, keys, movement, reads):
    if reads is None:
        verify_selected_balances(connection, period, category)
    else:
        reads.verify_balance_scope(period, category)
    where = ["posting_period=?" if movement else "posting_period<=?"]
    params = [period]
    if movement:
        where.append("component='activity'")
    if category is not None:
        where.append("category=?")
        params.append(category)
    if keys is not None:
        where.append("balance_key IN (SELECT value FROM json_each(?))")
        params.append(canonical(sorted(keys)))
    return [
        {"category": row[0], "key": row[1], "amount": checked(row[2])}
        for row in connection.execute(
            "SELECT category,balance_key,sum(amount) FROM period_balance WHERE "
            + " AND ".join(where)
            + " GROUP BY category,balance_key ORDER BY category,balance_key",
            params,
        )
    ]


def balance_totals(connection, through_period, category=None, keys=None, *, reads=None):
    return _totals(connection, through_period, category, keys, False, reads)


def balance_movements(connection, period, category=None, keys=None, *, reads=None):
    return _totals(connection, period, category, keys, True, reads)
