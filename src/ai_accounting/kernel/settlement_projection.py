"""Rebuildable posting-period settlement contributions.

The publication chain is authoritative.  These rows only normalize the exact
obligations and settlement relations of each active publication tranche so a
summary read never has to decode unrelated historical outcomes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from .contracts import KernelError
from .query_reads import SETTLEMENT_KINDS, QueryReads
from .types import YearMonth, canonical, checked, digest

_STANDARD_QUERY_READS = QueryReads

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


@dataclass(frozen=True)
class _VerifiedProjection:
    connection: sqlite3.Connection
    expected: tuple[tuple, ...]
    lease: object


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


def _active_tranches(connection, *, subject_ids=None, publication_highwater=None):
    from .publication import active_tranches

    return active_tranches(
        connection, subject_ids=subject_ids, publication_highwater=publication_highwater
    )


def _may_contribute_settlement(calculation, known_kinds):
    """Conservatively retain every root that can yield its own relation row."""

    kind = calculation.get("kind")
    if kind in SETTLEMENT_KINDS or kind == "opening_identity_binding" or kind not in known_kinds:
        return True
    outcome = calculation.get("outcome")
    if not isinstance(outcome, dict) or not isinstance(outcome.get("values"), dict):
        return True
    values = outcome["values"]
    if "obligations" not in values:
        return False
    obligations = values["obligations"]
    return not isinstance(obligations, list) or bool(obligations)


def _projection_calculations(reads, identifiers, verified_calculations):
    """Read every root identity; reuse only a complete same-call verified source map."""

    if verified_calculations is None:
        return reads.raw_calculations(identifiers), True
    if not identifiers <= verified_calculations.keys():
        return reads.prime_raw_calculations(
            identifiers, verified_calculations=verified_calculations
        ), False
    metadata = reads.metadata(identifiers, state=False)
    result = {}
    for ident in identifiers:
        source = verified_calculations[ident]
        header = metadata[ident]
        if (
            header["result_digest"] != bytes(source["digest"]).hex()
            or header["kind"] != source["kind"]
            or header["fact_id"] != source["fact_id"]
            or header["subject_id"] != source["subject_id"]
        ):
            raise KernelError("content_integrity_failed", "已核验清偿核算身份不一致")
        result[ident] = {
            "id": ident,
            "kind": source["kind"],
            "outcome": source["decoded"],
            "result_digest": header["result_digest"],
        }
    return result, True


def expected_settlement_projection(
    engine,
    connection,
    *,
    periods=None,
    subject_ids=None,
    verified_calculations=None,
    publication_highwater=None,
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
        for item in _active_tranches(
            connection, subject_ids=subject_ids, publication_highwater=publication_highwater
        )
        if selected_periods is None or item["posting_period"] in selected_periods
    ]
    calculation_ids = {
        ident
        for item in tranches
        for ident in (item.get("calculation_id"), item.get("baseline_calculation_id"))
        if ident is not None
    }
    reads = QueryReads(engine, connection)
    calculations, filter_relations = (
        _projection_calculations(reads, calculation_ids, verified_calculations)
        if calculation_ids
        else ({}, True)
    )
    relation_ids = {
        ident
        for ident, calculation in calculations.items()
        if not filter_relations
        or type(reads) is not _STANDARD_QUERY_READS
        or _may_contribute_settlement(calculation, reads.store.registry.models)
    }
    if relation_ids:
        calculations.update(
            reads.prime_raw_calculations(
                relation_ids, verified_calculations=verified_calculations
            )
        )
    relations = reads.relations_many(relation_ids, raw=True) if relation_ids else {}
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
                for item in _relation_rows(
                    calculation, relations.get(ident, {"obligations": (), "settlements": ()}), sign
                )
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


def _read_seals(connection, periods):
    """Verify the original seal bytes with bounded per-period Python objects.

    Every contribution and publication identity is included in the same order
    as ``_sealed``; the SQL JSON encoder avoids decoding and rebuilding each
    fourteen-column row in Python. The independent rebuild path is unchanged.
    """
    encoded = canonical(sorted(periods))
    publications = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT posting_period,json_group_array(id ORDER BY sequence) "
            "FROM calculation_publication WHERE posting_period IN "
            "(SELECT value FROM json_each(?)) GROUP BY posting_period",
            (encoded,),
        )
    }
    fields = ["s." + name for name in _COLUMNS[:-1]] + ["lower(hex(s.source_digest))"]
    rows = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT s.posting_period,count(*),json_group_array(json_array("
            + ",".join(fields)
            + ") ORDER BY s.publication_id,s.item_no) "
            "FROM settlement_change s WHERE s.posting_period IN "
            "(SELECT value FROM json_each(?)) GROUP BY s.posting_period",
            (encoded,),
        )
    }
    return {
        period: (
            rows.get(period, (0, "[]"))[0],
            hashlib.sha256(
                (
                    '{"publications":'
                    + publications.get(period, "[]")
                    + ',"rows":'
                    + rows.get(period, (0, "[]"))[1]
                    + "}"
                ).encode("utf-8")
            ).digest(),
        )
        for period in periods
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


def require_settlement_projection(
    engine, connection, *, verified_calculations=None, _return_verified=False
):
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
    if _return_verified:
        from .verified_source_lease import current_verified_lease

        return _VerifiedProjection(
            connection, tuple(compared["expected"]), current_verified_lease(connection)
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
    bad_source = connection.execute(
        "SELECT 1 FROM settlement_change s LEFT JOIN calculation c "
        "ON c.id=s.source_calculation_id WHERE s.posting_period IN "
        "(SELECT value FROM json_each(?)) "
        "AND (c.id IS NULL OR c.digest IS NOT s.source_digest) LIMIT 1",
        (encoded,),
    ).fetchone()
    if bad_source is not None:
        raise KernelError(
            "content_integrity_failed",
            "清偿读取投影的正式结果依据不匹配",
            component="settlement_projection",
            record_id="*",
            reason="source_digest_mismatch",
        )
    actual = _read_seals(connection, periods)
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


def _summary_scope(connection, period, *, subject_ids, current, reads):
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
    # Current knowledge must cover later publications even when a damaged
    # projection has omitted their only settlement change for a scoped key.
    # The projection itself cannot decide the range of its completeness check.
    from .posting_period_reads import posting_periods

    periods = posting_periods(
        connection,
        ("calculation_publication", "settlement_projection_seal", "settlement_change"),
        through=None if current else through,
    )
    if reads is None:
        verify_settlement_periods(connection, periods)
    else:
        reads.verify_settlement_periods(periods)
    return cutoff, through, source_keys, scope_parameters


def _summary_relation(source_keys, *, include_metadata=True):
    grouped = (
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
        "FROM selected GROUP BY obligation_key)"
    )
    remaining = (
        "CASE WHEN g.bad_source=1 OR g.bad_paid=1 OR g.bad_other=1 THEN NULL "
        "ELSE g.source_amount-g.paid-g.other_settled END remaining "
    )
    if not include_metadata:
        return grouped + ", obligations AS (SELECT g.*," + remaining + "FROM grouped g) "
    return (
        grouped
        + ", latest AS (SELECT s.* FROM selected s "
        "JOIN calculation_publication p ON p.id=s.publication_id "
        "WHERE s.change_kind='source' AND s.source_calculation_id=p.calculation_id "
        "AND NOT EXISTS(SELECT 1 FROM selected n JOIN calculation_publication np "
        "ON np.id=n.publication_id WHERE n.obligation_key=s.obligation_key "
        "AND n.change_kind='source' AND n.source_calculation_id=np.calculation_id "
        "AND (np.sequence,n.item_no)>(p.sequence,s.item_no))), "
        "obligations AS (SELECT g.*,l.source_subject_id,l.category,l.account,"
        "l.counterparty_id,l.component,l.source_calculation_id,"
        "c.kind source_kind,c.fact_id source_fact_id,"
        + remaining
        + "FROM grouped g LEFT JOIN latest l USING(obligation_key) "
        "LEFT JOIN calculation c ON c.id=l.source_calculation_id) "
    )


def settlement_position_rows(connection, period, accounts, *, reads=None):
    """Read only account/party balances needed to classify the financial position."""

    from .settlement_freeze import frozen_position_rows

    frozen = frozen_position_rows(connection, period, set(accounts), reads=reads)
    if frozen is not None:
        return frozen

    cutoff, through, source_keys, scope_parameters = _summary_scope(
        connection, period, subject_ids=None, current=False, reads=reads
    )
    return [
        dict(row)
        for row in connection.execute(
            _summary_relation(source_keys) + "SELECT account,category,counterparty_id,"
            "sum(remaining) remaining,max(remaining IS NULL) unknown "
            "FROM obligations WHERE account IN (SELECT value FROM json_each(?)) "
            "GROUP BY account,category,counterparty_id",
            [*scope_parameters, through, cutoff, cutoff, canonical(sorted(accounts))],
        )
    ]


def _obligation_view(row):
    source_amount = None if row["bad_source"] else row["source_amount"]
    paid = None if row["bad_paid"] else row["paid"]
    other = None if row["bad_other"] else row["other_settled"]
    remaining = (
        None
        if source_amount is None or paid is None or other is None
        else checked(source_amount - paid - other)
    )
    return {
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


def settlement_dashboard_open(
    connection,
    period,
    *,
    current=False,
    after=None,
    limit=100,
    page_keys=None,
    summary_only=False,
    include_settled_page=False,
    reads=None,
):
    """Aggregate all open obligations in SQL and hydrate only displayed rows."""

    from .settlement_freeze import frozen_dashboard_open

    frozen = frozen_dashboard_open(
        connection, period, current=current, after=after, limit=limit,
        page_keys=page_keys, summary_only=summary_only,
        include_settled_page=include_settled_page, reads=reads,
    )
    if frozen is not None:
        return frozen

    cutoff, through, source_keys, scope_parameters = _summary_scope(
        connection, period, subject_ids=None, current=current, reads=reads
    )
    if page_keys is not None and not page_keys:
        # The brief page asks for current values of its historical page keys.
        # With no keys there is no current row to hydrate; scope verification
        # above still checks the complete applicable projection history.
        return {
            "cutoff_period": str(YearMonth.from_ordinal(through)),
            "status": "not_established",
            "complete": True,
            "issues": [],
            "categories": {},
            "obligations": [],
            "page": {
                "total_count": 0,
                "filtered_count": 0,
                "returned_count": 0,
                "has_more": False,
                "next_cursor": None,
            },
            **({"current_cutoff_period": str(YearMonth.from_ordinal(through))} if current else {}),
        }
    category = (
        "CASE WHEN category='receivable' THEN "
        "CASE WHEN account='1122' THEN 'customer_receivables' "
        "WHEN account='1123' THEN 'supplier_advances' "
        "WHEN instr(coalesce(source_kind,''),'deposit')>0 "
        "THEN 'refundable_deposit_receivables' ELSE 'other_receivables' END "
        "WHEN source_kind IN ('payroll','payroll_bounded','annual_bonus',"
        "'opening_payroll_payable') "
        "THEN 'payroll_payables' "
        "WHEN source_kind IN ('labor','labor_accrual') THEN 'labor_payables' "
        "WHEN account='2202' THEN 'supplier_payables' "
        "WHEN source_kind IN ('employee_advance','reimbursement_acceptance',"
        "'reimbursed_asset','reimbursed_asset_batch') THEN 'employee_payables' "
        "ELSE 'other_payables' END"
    )
    page_condition = (
        "obligation_key IN (SELECT value FROM json_each(?))"
        if page_keys is not None
        else "(? IS NULL OR obligation_key>?)"
    )
    page_parameters = [canonical(sorted(page_keys))] if page_keys is not None else [after, after]
    fields = (
        "obligation_key",
        "source_event_count",
        "source_amount",
        "bad_source",
        "paid",
        "other_settled",
        "period_paid",
        "period_other",
        "bad_paid",
        "bad_other",
        "source_subject_id",
        "category",
        "account",
        "counterparty_id",
        "component",
        "source_calculation_id",
        "source_kind",
        "source_fact_id",
    )
    if summary_only:
        fields = ("obligation_key",)
    row_json = "json_object(" + ",".join(f"'{field}',{field}" for field in fields) + ")"
    page_relation = (
        f"(SELECT *,{category} category_key FROM obligations)" if include_settled_page else "open"
    )
    rows = connection.execute(
        _summary_relation(source_keys)
        + f", open AS MATERIALIZED (SELECT *,{category} category_key FROM obligations "
        "WHERE remaining IS NULL OR remaining<>0), "
        f"page AS (SELECT * FROM {page_relation} WHERE {page_condition} "
        "ORDER BY obligation_key LIMIT ?) "
        "SELECT -1 row_type,NULL category_key,count(*) item_count,NULL amount,"
        "max(remaining IS NULL) unknown,NULL cursor_matches,NULL item FROM obligations "
        "UNION ALL SELECT 0,category_key,count(*),sum(remaining),"
        "max(remaining IS NULL),sum(obligation_key=?),NULL FROM open GROUP BY category_key "
        "UNION ALL SELECT 1,category_key,NULL,NULL,NULL,NULL," + row_json + " FROM page",
        [*scope_parameters, through, cutoff, cutoff, *page_parameters, limit + 1, after],
    )
    obligation_count = 0
    unknown = False
    cursor_matches = 0
    category_rows = {}
    page_rows = []
    for row in rows:
        if row["row_type"] == -1:
            obligation_count = row["item_count"]
            unknown = bool(row["unknown"])
        elif row["row_type"] == 0:
            category_rows[row["category_key"]] = {
                "count": row["item_count"],
                "amount": None if row["unknown"] else row["amount"],
            }
            cursor_matches += row["cursor_matches"] or 0
        else:
            value = json.loads(row["item"])
            page_rows.append(value if summary_only else _obligation_view(value))
    if after is not None and page_keys is None and not cursor_matches:
        raise KernelError("dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。")
    total = sum(row["count"] for row in category_rows.values())
    items = page_rows[:limit]
    more = len(page_rows) > limit
    return {
        "cutoff_period": str(YearMonth.from_ordinal(through)),
        "status": "partially_established"
        if unknown
        else "established"
        if obligation_count
        else "not_established",
        "complete": not unknown,
        "issues": (
            [{"field": "settlements", "message": "存在尚未确立的清偿关系"}] if unknown else []
        ),
        "categories": category_rows,
        "obligations": [] if summary_only else items,
        "page": {
            "total_count": total,
            "filtered_count": total,
            "returned_count": len(items),
            "has_more": more,
            "next_cursor": (items[-1].get("key") or items[-1]["obligation_key"]) if more else None,
        },
        **({"current_cutoff_period": str(YearMonth.from_ordinal(through))} if current else {}),
    }


def settlement_summary(
    connection, period, *, subject_ids=None, current=False, reads=None, include_history_counts=True
):
    """Aggregate obligations through a posting cutoff from normalized rows."""

    if subject_ids is not None:
        from .settlement_freeze import frozen_subject_summary

        frozen = frozen_subject_summary(
            connection, period, subject_ids=set(subject_ids), current=current, reads=reads,
            include_history_counts=include_history_counts,
        )
        if frozen is not None:
            return frozen

    history_cache = (
        reads._settlement_history_summaries
        if reads is not None and getattr(reads, "_snapshot_active", False)
        else None
    )
    history_key = (
        str(YearMonth(period)), None if subject_ids is None else frozenset(subject_ids),
        include_history_counts,
    )
    cutoff, through, source_keys, scope_parameters = _summary_scope(
        connection, period, subject_ids=subject_ids, current=current, reads=reads
    )
    if current and through == cutoff and history_cache is not None:
        historical = history_cache.get(history_key)
        if historical is not None:
            # Current scope was independently checked against every authoritative
            # publication period above. With no later applicable contribution,
            # its reducer has the same exact keys and amounts as this snapshot's
            # already checked historical result.
            return {
                **historical,
                "scope_period": period,
                "current_cutoff_period": str(YearMonth.from_ordinal(through)),
                "cutoff_semantics": "current_published_relations_independent_of_as_of",
            }
    parameters = [*scope_parameters, through, cutoff, cutoff]
    rows = connection.execute(
        _summary_relation(source_keys) + "SELECT * FROM obligations ORDER BY obligation_key",
        parameters,
    ).fetchall()
    obligations = [_obligation_view(row) for row in rows]
    unresolved = any(row["remaining_fen"] is None for row in obligations)
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
    ).fetchone()[0] if include_history_counts else None
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
    ).fetchone()[0] if include_history_counts else None
    result = {
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
        **({"business_count": business_count, "movement_count": movement_count,
            "line_relation_count": 0} if include_history_counts else {}),
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
    if history_cache is not None and not current:
        history_cache[history_key] = result
    return result


def settlement_followup_summary(connection, period, *, current=False, reads=None):
    """Compute the preparation-page totals without constructing every obligation view."""

    from .settlement_freeze import frozen_followup_summary

    frozen = frozen_followup_summary(connection, period, current=current, reads=reads)
    if frozen is not None:
        return frozen

    cutoff, through, source_keys, scope_parameters = _summary_scope(
        connection, period, subject_ids=None, current=current, reads=reads
    )
    try:
        row = connection.execute(
            _summary_relation(source_keys, include_metadata=False)
            + ", movements AS (SELECT count(*) movement_count,"
            "max(state='unresolved') unknown FROM selected WHERE change_kind!='source') "
            "SELECT count(*) obligation_count,"
            "coalesce(sum(remaining IS NULL OR remaining<>0),0) followup_count,"
            "max(remaining IS NULL) unknown_remaining,"
            "max(bad_source) bad_source,max(bad_paid) bad_paid,max(bad_other) bad_other,"
            "max(remaining IS NOT NULL AND typeof(remaining)!='integer') bad_integer,"
            "coalesce(sum(source_amount),0) source_amount,"
            "coalesce(sum(paid),0) paid,coalesce(sum(other_settled),0) other_settled,"
            "coalesce(sum(remaining),0) remaining,"
            "(SELECT movement_count FROM movements) movement_count,"
            "(SELECT unknown FROM movements) unknown_movement FROM obligations",
            [*scope_parameters, through, cutoff, cutoff],
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "integer overflow" not in str(exc):
            raise
        row = None
    if row is None or row["bad_integer"]:
        # SQLite promotes overflowing per-obligation arithmetic to REAL and
        # raises on overflowing SUM. Preserve the original exact-integer and
        # checked-range behavior for these exceptional inputs.
        from .query_semantics import project_settlement_followup

        return project_settlement_followup(
            settlement_summary(connection, period, current=current, reads=reads)
        )
    obligation_count = row["obligation_count"]
    unresolved = bool(row["unknown_remaining"] or row["unknown_movement"])
    cutoff_label = str(YearMonth.from_ordinal(through))
    return {
        "status": (
            "partially_established" if unresolved else "established"
            if obligation_count else "not_established"
        ),
        "cutoff_period": cutoff_label,
        "current_cutoff_period": cutoff_label if current else None,
        "issues": (
            [{"field": "settlements", "message": "存在尚未确立的清偿关系"}]
            if unresolved else []
        ),
        "obligation_count": obligation_count,
        "followup_count": row["followup_count"],
        "complete": not unresolved,
        "unestablished_state_selection_count": 0,
        "movement_count": row["movement_count"],
        "source_amount_fen": None if row["bad_source"] else row["source_amount"],
        "paid_fen": None if row["bad_paid"] else row["paid"],
        "other_settled_fen": None if row["bad_other"] else row["other_settled"],
        "remaining_fen": None if row["unknown_remaining"] else row["remaining"],
    }
