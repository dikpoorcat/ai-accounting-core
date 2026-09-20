"""Rebuildable posting-period settlement contributions.

The publication chain is authoritative.  These rows only normalize the exact
obligations and settlement relations of each active publication tranche so a
summary read never has to decode unrelated historical outcomes.
"""

from __future__ import annotations

import json

from .contracts import KernelError
from .query_reads import QueryReads
from .types import YearMonth, canonical, checked, digest

_COLUMNS = (
    "publication_id",
    "item_no",
    "posting_period",
    "obligation_key",
    "source_subject_id",
    "category",
    "account",
    "counterparty_id",
    "component",
    "change_kind",
    "amount",
    "state",
    "source_calculation_id",
    "source_digest",
)


def _relation_rows(calculation, relation, sign):
    """Return stable semantic contributions made by one calculation."""

    obligations = {
        item.get("key"): item
        for item in relation.get("obligations", ())
        if isinstance(item.get("key"), str) and item.get("key")
    }
    result = []
    for item in obligations.values():
        if item.get("source_calculation_id") != calculation["id"]:
            continue
        amount = item.get("amount_fen")
        result.append(
            {
                "obligation_key": item["key"],
                "source_subject_id": (item.get("source_business") or {}).get("subject_id"),
                "category": item.get("category"),
                "account": item.get("account"),
                "counterparty_id": item.get("creditor_id") or item.get("counterparty_id"),
                "component": item.get("name"),
                "change_kind": "source",
                "amount": checked(sign * amount) if type(amount) is int else None,
                "state": item.get("state", "unresolved"),
            }
        )
    for item in relation.get("settlements", ()):
        key = item.get("obligation_key")
        source = obligations.get(key, {})
        amount = item.get("amount_fen")
        result.append(
            {
                "obligation_key": key if isinstance(key, str) and key else None,
                "source_subject_id": (item.get("source_business") or {}).get("subject_id"),
                "category": source.get("category"),
                "account": source.get("account"),
                "counterparty_id": (
                    item.get("creditor_id")
                    or source.get("creditor_id")
                    or source.get("counterparty_id")
                ),
                "component": item.get("obligation_name") or source.get("name"),
                "change_kind": "payment" if item.get("mode") == "payment" else "other",
                "amount": checked(sign * amount) if type(amount) is int else None,
                "state": item.get("state", "unresolved"),
            }
        )
    return result


def _active_tranches(connection, *, subject_ids=None):
    from .publication import active_tranches

    return active_tranches(connection, subject_ids=subject_ids)


def expected_settlement_projection(
    engine,
    connection,
    *,
    periods=None,
    subject_ids=None,
    verified_calculations=None,
):
    """Derive exact contribution rows without consulting current projections."""

    selected_periods = None if periods is None else set(periods)
    if selected_periods is not None and subject_ids is None:
        subject_ids = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT subject_id FROM calculation_publication "
                "WHERE posting_period IN (SELECT value FROM json_each(?))",
                (canonical(sorted(selected_periods)),),
            )
        }
    tranches = [
        item
        for item in _active_tranches(connection, subject_ids=subject_ids)
        if selected_periods is None or item["posting_period"] in selected_periods
    ]
    calculation_ids = {
        ident
        for item in tranches
        for ident in (item.get("calculation_id"), item.get("baseline_calculation_id"))
        if ident is not None
    }
    reads = QueryReads(engine, connection)
    calculations = (
        reads.prime_raw_calculations(calculation_ids, verified_calculations=verified_calculations)
        if calculation_ids
        else {}
    )
    relations = reads.relations_many(calculation_ids, raw=True) if calculation_ids else {}
    binding_source_ids = {
        calculation["outcome"]["values"]["source_calculation_id"]
        for calculation in calculations.values()
        if calculation["kind"] == "opening_identity_binding"
    }
    binding_sources = reads.raw_calculations(binding_source_ids) if binding_source_ids else {}
    rows = []
    for tranche in tranches:
        contributions = []
        for field, sign in (("baseline_calculation_id", -1), ("calculation_id", 1)):
            ident = tranche.get(field)
            if ident is None:
                continue
            calculation = calculations[ident]
            if calculation["kind"] == "opening_identity_binding":
                values = calculation["outcome"]["values"]
                original = binding_sources[values["source_calculation_id"]]["outcome"]["values"]
                selected_values = [(original, -sign)]
                if not values.get("superseded"):
                    selected_values.append((values["basis_values"], sign))
                for source_values, direction in selected_values:
                    for obligation in source_values.get("obligations", ()):
                        amount = obligation.get("amount_fen")
                        contributions.append(
                            (
                                {
                                    "obligation_key": obligation["key"],
                                    "source_subject_id": values["source_subject_id"],
                                    "category": obligation.get("category"),
                                    "account": obligation.get("account"),
                                    "counterparty_id": obligation.get("counterparty_id"),
                                    "component": obligation.get("name"),
                                    "change_kind": "source",
                                    "amount": checked(direction * amount)
                                    if type(amount) is int
                                    else None,
                                    "state": "resolved"
                                    if obligation.get("counterparty_id")
                                    else "unresolved",
                                },
                                ident,
                                bytes.fromhex(calculation["result_digest"]),
                            )
                        )
                continue
            contributions.extend(
                (
                    item,
                    ident,
                    bytes.fromhex(calculation["result_digest"]),
                )
                for item in _relation_rows(calculation, relations[ident], sign)
            )
        contributions.sort(
            key=lambda item: canonical(
                {
                    **item[0],
                    "source_calculation_id": item[1],
                    "source_digest": item[2].hex(),
                }
            )
        )
        for item_no, (item, calculation_id, result_digest) in enumerate(contributions, 1):
            rows.append(
                (
                    tranche["id"],
                    item_no,
                    tranche["posting_period"],
                    item["obligation_key"],
                    item["source_subject_id"],
                    item["category"],
                    item["account"],
                    item["counterparty_id"],
                    item["component"],
                    item["change_kind"],
                    item["amount"],
                    item["state"],
                    calculation_id,
                    result_digest,
                )
            )
    return sorted(rows, key=lambda row: (row[2], row[0], row[1]))


def _publication_periods(connection, periods=None):
    query = "SELECT DISTINCT posting_period FROM calculation_publication"
    parameters = []
    if periods is not None:
        query += " WHERE posting_period IN (SELECT value FROM json_each(?))"
        parameters.append(json.dumps(sorted(set(periods))))
    return {row[0] for row in connection.execute(query, parameters)}


def _sealed(connection, rows, periods):
    grouped = {period: [] for period in periods}
    for row in rows:
        grouped.setdefault(row[2], []).append(row)
    publications = {period: [] for period in periods}
    for row in connection.execute(
        "SELECT posting_period,id FROM calculation_publication WHERE posting_period IN "
        "(SELECT value FROM json_each(?)) ORDER BY posting_period,sequence",
        (canonical(sorted(periods)),),
    ):
        publications.setdefault(row["posting_period"], []).append(row["id"])
    return {
        period: (
            len(values),
            digest(
                {
                    "publications": publications.get(period, []),
                    "rows": [list(value[:-1]) + [value[-1].hex()] for value in values],
                }
            ),
        )
        for period, values in grouped.items()
    }


def sync_settlement_publications(
    engine, connection, affected_publication_ids=(), affected_periods=()
):
    """Replace affected posting periods inside the caller's publication transaction."""

    del affected_publication_ids  # Period replacement is complete and cannot leave stale rows.
    if not connection.in_transaction:
        raise ValueError("settlement projection sync requires a write transaction")
    periods = set(affected_periods)
    if not periods:
        return {"changed": False, "periods": 0}
    expected = expected_settlement_projection(engine, connection, periods=periods)
    encoded = json.dumps(sorted(periods))
    before = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {','.join(_COLUMNS)} FROM settlement_change "
            "WHERE posting_period IN (SELECT value FROM json_each(?)) ORDER BY 3,1,2",
            (encoded,),
        )
    ]
    expected_seals = _sealed(
        connection, expected, periods | _publication_periods(connection, periods)
    )
    actual_seals = {
        row[0]: (row[1], bytes(row[2]))
        for row in connection.execute(
            "SELECT posting_period,row_count,digest FROM settlement_projection_seal "
            "WHERE posting_period IN (SELECT value FROM json_each(?))",
            (encoded,),
        )
    }
    changed = before != expected or actual_seals != expected_seals
    if changed:
        connection.execute(
            "DELETE FROM settlement_change WHERE posting_period IN "
            "(SELECT value FROM json_each(?))",
            (encoded,),
        )
        connection.execute(
            "DELETE FROM settlement_projection_seal WHERE posting_period IN "
            "(SELECT value FROM json_each(?))",
            (encoded,),
        )
        connection.executemany(
            f"INSERT INTO settlement_change({','.join(_COLUMNS)}) "
            f"VALUES({','.join('?' for _ in _COLUMNS)})",
            expected,
        )
        connection.executemany(
            "INSERT INTO settlement_projection_seal VALUES(?,?,?)",
            [(period, *expected_seals[period]) for period in sorted(expected_seals)],
        )
    return {"changed": changed, "periods": len(periods)}


def compare_settlement_projection(engine, connection, *, verified_calculations=None):
    periods = _publication_periods(connection)
    expected = expected_settlement_projection(
        engine, connection, verified_calculations=verified_calculations
    )
    actual = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {','.join(_COLUMNS)} FROM settlement_change ORDER BY 3,1,2"
        )
    ]
    expected_seals = _sealed(connection, expected, periods)
    actual_seals = {
        row[0]: (row[1], bytes(row[2]))
        for row in connection.execute(
            "SELECT posting_period,row_count,digest FROM settlement_projection_seal"
        )
    }
    return {
        "changed": actual != expected or actual_seals != expected_seals,
        "expected": expected,
        "expected_seals": expected_seals,
        "actual_rows": len(actual),
    }


def require_settlement_projection(engine, connection, *, verified_calculations=None):
    compared = compare_settlement_projection(
        engine, connection, verified_calculations=verified_calculations
    )
    if compared["changed"]:
        raise KernelError(
            "content_integrity_failed",
            "清偿读取投影与正式发布来源不一致，需要显式维修",
            component="settlement_projection",
            record_id="*",
            reason="projection_mismatch",
        )
    return {"rows": compared["actual_rows"], "periods": len(compared["expected_seals"])}


def repair_settlement_projection(engine, connection, *, verified_calculations=None):
    if not connection.in_transaction:
        raise ValueError("settlement projection repair requires a write transaction")
    compared = compare_settlement_projection(
        engine, connection, verified_calculations=verified_calculations
    )
    if compared["changed"]:
        connection.execute("DELETE FROM settlement_change")
        connection.execute("DELETE FROM settlement_projection_seal")
        connection.executemany(
            f"INSERT INTO settlement_change({','.join(_COLUMNS)}) "
            f"VALUES({','.join('?' for _ in _COLUMNS)})",
            compared["expected"],
        )
        connection.executemany(
            "INSERT INTO settlement_projection_seal VALUES(?,?,?)",
            [
                (period, *compared["expected_seals"][period])
                for period in sorted(compared["expected_seals"])
            ],
        )
        require_settlement_projection(
            engine, connection, verified_calculations=verified_calculations
        )
    return {"changed": compared["changed"], "rows": len(compared["expected"])}


def verify_settlement_periods(connection, periods, *, reads=None):
    """Verify hit completeness and source identity without decoding outcomes."""

    periods = sorted(set(periods))
    if not periods:
        return
    encoded = json.dumps(periods)
    if reads is not None:
        reads.verify_publication_periods(periods)
    else:
        from .publication import verify_record

        for publication in connection.execute(
            "SELECT * FROM calculation_publication WHERE posting_period IN "
            "(SELECT value FROM json_each(?)) ORDER BY sequence",
            (encoded,),
        ):
            verify_record(dict(publication))
    rows = [
        tuple(row)
        for row in connection.execute(
            f"SELECT {','.join('s.' + column for column in _COLUMNS)} "
            "FROM settlement_change s JOIN calculation c ON c.id=s.source_calculation_id "
            "WHERE s.posting_period IN (SELECT value FROM json_each(?)) "
            "AND c.digest=s.source_digest ORDER BY s.posting_period,s.publication_id,s.item_no",
            (encoded,),
        )
    ]
    all_rows = connection.execute(
        "SELECT count(*) FROM settlement_change WHERE posting_period IN "
        "(SELECT value FROM json_each(?))",
        (encoded,),
    ).fetchone()[0]
    if len(rows) != all_rows:
        raise KernelError(
            "content_integrity_failed",
            "清偿读取投影的正式结果依据不匹配",
            component="settlement_projection",
            record_id="*",
            reason="source_digest_mismatch",
        )
    actual = _sealed(connection, rows, periods)
    seals = {
        row[0]: (row[1], bytes(row[2]))
        for row in connection.execute(
            "SELECT posting_period,row_count,digest FROM settlement_projection_seal "
            "WHERE posting_period IN (SELECT value FROM json_each(?))",
            (encoded,),
        )
    }
    if actual != seals:
        raise KernelError(
            "content_integrity_failed",
            "清偿读取投影的期间摘要不匹配",
            component="settlement_projection",
            record_id="*",
            reason="period_seal_mismatch",
        )


def settlement_summary(connection, period, *, subject_ids=None, current=False, reads=None):
    """Aggregate obligations through a posting cutoff from normalized rows."""

    cutoff = YearMonth(period).ordinal
    scope_parameters = [cutoff]
    scope = "posting_period<=?"
    if subject_ids is not None:
        scope += " AND source_subject_id IN (SELECT value FROM json_each(?))"
        scope_parameters.append(json.dumps(sorted(set(subject_ids))))
    source_keys = (
        "SELECT obligation_key FROM settlement_change WHERE "
        + scope
        + " AND change_kind='source' AND obligation_key IS NOT NULL "
        "GROUP BY obligation_key"
    )
    through = cutoff
    if current:
        direct_scope = (
            " OR source_subject_id IN (SELECT value FROM json_each(?))"
            if subject_ids is not None
            else ""
        )
        direct_parameters = (
            [json.dumps(sorted(set(subject_ids)))] if subject_ids is not None else []
        )
        latest = connection.execute(
            "SELECT max(posting_period) FROM settlement_change WHERE (obligation_key IN ("
            + source_keys
            + ")"
            + direct_scope
            + ")",
            [*scope_parameters, *direct_parameters],
        ).fetchone()[0]
        if latest is not None:
            through = max(through, latest)
    periods = {
        row[0]
        for row in connection.execute(
            "SELECT posting_period FROM calculation_publication WHERE posting_period<=? "
            "UNION SELECT posting_period FROM settlement_projection_seal WHERE posting_period<=? "
            "UNION SELECT posting_period FROM settlement_change WHERE posting_period<=?",
            (through, through, through),
        )
    }
    if reads is None:
        verify_settlement_periods(connection, periods)
    else:
        reads.verify_settlement_periods(periods)
    parameters = [*scope_parameters, through, cutoff, cutoff]
    rows = connection.execute(
        "WITH scoped_keys AS ("
        + source_keys
        + "), selected AS (SELECT s.* FROM settlement_change s JOIN scoped_keys k USING("
        "obligation_key) WHERE s.posting_period<=?), grouped AS (SELECT obligation_key,"
        "sum(CASE WHEN change_kind='source' THEN 1 ELSE 0 END) source_event_count,"
        "sum(CASE WHEN change_kind='source' THEN coalesce(amount,0) ELSE 0 END) source_amount,"
        "max(CASE WHEN change_kind='source' AND amount IS NULL THEN 1 ELSE 0 END) bad_source,"
        "sum(CASE WHEN change_kind='payment' AND state='resolved' THEN coalesce(amount,0) "
        "ELSE 0 END) paid,sum(CASE WHEN change_kind='other' AND state='resolved' THEN "
        "coalesce(amount,0) ELSE 0 END) other_settled,"
        "sum(CASE WHEN change_kind='payment' AND state='resolved' AND posting_period=? THEN "
        "coalesce(amount,0) ELSE 0 END) period_paid,"
        "sum(CASE WHEN change_kind='other' AND state='resolved' AND posting_period=? THEN "
        "coalesce(amount,0) ELSE 0 END) period_other,"
        "max(CASE WHEN change_kind='payment' AND state='unresolved' THEN 1 ELSE 0 END) bad_paid,"
        "max(CASE WHEN change_kind='other' AND state='unresolved' THEN 1 ELSE 0 END) bad_other "
        "FROM selected GROUP BY obligation_key), latest AS (SELECT s.* FROM selected s "
        "JOIN calculation_publication p ON p.id=s.publication_id "
        "WHERE s.change_kind='source' AND s.source_calculation_id=p.calculation_id "
        "AND NOT EXISTS(SELECT 1 FROM selected n JOIN calculation_publication np "
        "ON np.id=n.publication_id WHERE n.obligation_key=s.obligation_key "
        "AND n.change_kind='source' AND n.source_calculation_id=np.calculation_id "
        "AND (np.sequence,n.item_no)>(p.sequence,s.item_no))) "
        "SELECT g.*,l.source_subject_id,l.category,l.account,l.counterparty_id,l.component,"
        "l.source_calculation_id,c.kind source_kind,c.fact_id source_fact_id FROM grouped g "
        "LEFT JOIN latest l USING(obligation_key) LEFT JOIN calculation c "
        "ON c.id=l.source_calculation_id ORDER BY g.obligation_key",
        parameters,
    ).fetchall()
    obligations = []
    unresolved = False
    for row in rows:
        source_amount = None if row["bad_source"] else row["source_amount"]
        paid = None if row["bad_paid"] else row["paid"]
        other = None if row["bad_other"] else row["other_settled"]
        remaining = (
            None
            if source_amount is None or paid is None or other is None
            else checked(source_amount - paid - other)
        )
        unresolved = unresolved or remaining is None
        obligations.append(
            {
                "key": row["obligation_key"],
                "name": row["component"],
                "category": row["category"],
                "account": row["account"],
                "counterparty_id": row["counterparty_id"],
                "creditor_id": row["counterparty_id"],
                "source_business": {
                    "kind": row["source_kind"],
                    "subject_id": row["source_subject_id"],
                },
                "source_calculation_id": row["source_calculation_id"],
                "source_fact_id": row["source_fact_id"],
                "source_amount_fen": source_amount,
                "paid_fen": paid,
                "other_settled_fen": other,
                "period_paid_fen": None if row["bad_paid"] else row["period_paid"],
                "period_other_settled_fen": None if row["bad_other"] else row["period_other"],
                "remaining_fen": remaining,
                "settlement_status": (
                    "unestablished"
                    if remaining is None
                    else "settled"
                    if remaining == 0
                    else "over_settled"
                    if remaining < 0
                    else "open"
                    if remaining == source_amount
                    else "partial"
                ),
                "source_events": [],
                "source_event_count": row["source_event_count"],
            }
        )
    subject_scope = (
        " OR s.source_subject_id IN (SELECT value FROM json_each(?))"
        if subject_ids is not None
        else ""
    )
    outer_parameters = [json.dumps(sorted(set(subject_ids)))] if subject_ids is not None else []
    movement_count = connection.execute(
        "WITH scoped_keys AS (" + source_keys + ") SELECT count(*) FROM settlement_change s "
        "LEFT JOIN scoped_keys k USING(obligation_key) WHERE s.posting_period<=? "
        "AND s.change_kind!='source' AND (k.obligation_key IS NOT NULL" + subject_scope + ")",
        [*scope_parameters, through, *outer_parameters],
    ).fetchone()[0]
    scoped_unresolved = connection.execute(
        "WITH scoped_keys AS ("
        + source_keys
        + ") SELECT 1 FROM settlement_change s LEFT JOIN scoped_keys k USING(obligation_key) "
        "WHERE s.posting_period<=? AND s.change_kind!='source' AND s.state='unresolved' "
        "AND (k.obligation_key IS NOT NULL" + subject_scope + ") LIMIT 1",
        [*scope_parameters, through, *outer_parameters],
    ).fetchone()
    unresolved = unresolved or scoped_unresolved is not None
    business_count = connection.execute(
        "WITH scoped_keys AS ("
        + source_keys
        + ") SELECT count(DISTINCT s.publication_id) FROM settlement_change s "
        "LEFT JOIN scoped_keys k USING(obligation_key) WHERE s.posting_period<=? "
        "AND (k.obligation_key IS NOT NULL" + subject_scope + ")",
        [*scope_parameters, through, *outer_parameters],
    ).fetchone()[0]
    return {
        "cutoff_period": str(YearMonth.from_ordinal(through)),
        "status": (
            "partially_established" if unresolved else "established" if rows else "not_established"
        ),
        "business": [],
        "obligations": obligations,
        "movements": [],
        "line_relations": [],
        "issues": (
            [{"field": "settlements", "message": "存在尚未确立的清偿关系"}] if unresolved else []
        ),
        "business_count": business_count,
        "movement_count": movement_count,
        "line_relation_count": 0,
        "unestablished_state_selections": [],
        "complete": not unresolved,
        **(
            {
                "scope_period": period,
                "current_cutoff_period": str(YearMonth.from_ordinal(through)),
                "cutoff_semantics": "current_published_relations_independent_of_as_of",
            }
            if current
            else {}
        ),
    }
