"""Rebuildable, period-scoped source directory for quarterly reports.

The directory is deliberately narrower than a report: management classifications,
party decisions, statement amounts and readiness are never stored here.  A reader
must still verify the selected authoritative rows and frozen close references.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .content_history_context import close_reader
from .content_v1 import _V1YearMonth as YearMonth
from .contracts import KernelError


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).digest()


SOURCE_COLUMNS = (
    "scope",
    "posting_period",
    "version_id",
    "line_no",
    "voucher_calculation_id",
    "basis_calculation_id",
    "publication_id",
    "reverses_id",
    "account",
    "debit",
    "credit",
    "cashflow",
    "kind",
    "fact_id",
    "calculation_period",
    "calculation_subject_id",
    "result_digest",
)


@dataclass(frozen=True)
class PreparedReport:
    period: int
    close_digest: bytes
    rows: tuple[tuple, ...]
    party_rows: tuple[tuple, ...]
    party_usable: bool
    checkpoint_rows: tuple[tuple, ...]
    checkpoint_usable: bool | None
    checkpoint_inputs: tuple[tuple[int, str], ...]
    root_digest: bytes


@dataclass(frozen=True)
class _VerifiedReports:
    connection: object
    closes: tuple[tuple[int, PreparedReport], ...]
    lease: object


def _ordinal(period):
    return period.ordinal if isinstance(period, YearMonth) else YearMonth(period).ordinal


def _authoritative_rows(connection, scope, period):
    month = YearMonth.from_ordinal(period)
    selected, params = selected_voucher_sql(month, posting_period=month)
    if scope == "closed":
        selected = "SELECT * FROM (" + selected + ") WHERE selection_source='close_manifest'"
    query = (
        "WITH selected AS ("
        + selected
        + ") SELECT ?,v.period,v.id,l.line_no,v.voucher_calculation_id,"
        "v.basis_calculation_id,p.id,v.reverses_id,l.account,l.debit,l.credit,l.cashflow,"
        "c.kind,c.fact_id,c.period,c.subject_id,c.digest "
        "FROM selected v JOIN voucher_line l ON l.version_id=v.id "
        "JOIN calculation c ON c.id=v.basis_calculation_id "
        "LEFT JOIN calculation_publication p ON p.calculation_id=v.basis_calculation_id "
        "ORDER BY v.id,l.line_no"
    )
    return [tuple(row) for row in connection.execute(query, (*params, scope))]


def _seal(connection, scope, period, rows, *, publication_sequence=None):
    if scope == "closed" and publication_sequence is None:
        close = connection.execute(
            "SELECT * FROM period_close WHERE period=?", (period,)
        ).fetchone()
        if close is None:
            raise KernelError("frozen_snapshot_unavailable", "关账报表来源月份尚未冻结")
        publication_sequence = (
            close_reader().verified_header(connection, close).root["small"]["publication_sequence"]
        )
    restriction = "" if publication_sequence is None else " AND sequence<=?"
    parameters = (period,) if publication_sequence is None else (period, publication_sequence)
    publications = [
        [row["id"], row["sequence"], row["mode"], row["calculation_id"]]
        for row in connection.execute(
            "SELECT id,sequence,mode,calculation_id FROM calculation_publication "
            "WHERE posting_period=?" + restriction + " ORDER BY sequence",
            parameters,
        )
    ]
    source = [list(row[:-1]) + [row[-1].hex()] for row in rows]
    return len(rows), digest(
        {"scope": scope, "period": period, "publications": publications, "rows": source}
    )


def _manifest_rows(connection, period, manifest):
    """Prepare the closed selection before close_reference has been written."""

    vouchers = {item["id"]: item for item in manifest["vouchers"]}
    if not vouchers:
        return ()
    selected = list(
        connection.execute(
            "SELECT v.id,v.voucher_id,v.calculation_id,v.reverses_id,v.total,n.number,"
            "l.line_no,l.account,l.debit,l.credit,l.cashflow,c.kind,c.fact_id,"
            "c.period calculation_period,c.subject_id,c.digest,p.id publication_id,"
            "c.id basis_calculation_id "
            "FROM json_each(?) ids JOIN voucher_version v ON v.id=ids.value "
            "JOIN voucher n ON n.id=v.voucher_id "
            "JOIN voucher_line l ON l.version_id=v.id "
            "LEFT JOIN voucher_version original ON original.id=v.reverses_id "
            "JOIN calculation c ON c.id=coalesce(original.calculation_id,v.calculation_id) "
            "LEFT JOIN calculation_publication p ON p.calculation_id=c.id "
            "WHERE v.period=? ORDER BY v.id,l.line_no",
            (canonical(sorted(vouchers)), period),
        )
    )
    covered = set()
    rows = []
    for row in selected:
        item = vouchers[row["id"]]
        if any(
            row[column] != item[column]
            for column in ("voucher_id", "calculation_id", "reverses_id", "total", "number")
        ):
            raise KernelError("content_integrity_failed", "关账报表来源与冻结凭证不一致")
        covered.add(row["id"])
        rows.append(
            (
                "closed",
                period,
                row["id"],
                row["line_no"],
                row["calculation_id"],
                row["basis_calculation_id"],
                row["publication_id"],
                row["reverses_id"],
                row["account"],
                row["debit"],
                row["credit"],
                row["cashflow"],
                row["kind"],
                row["fact_id"],
                row["calculation_period"],
                row["subject_id"],
                row["digest"],
            )
        )
    if covered != set(vouchers):
        raise KernelError("content_integrity_failed", "关账报表来源缺少冻结凭证行")
    return tuple(rows)


def _report_root(
    period,
    close_digest,
    rows,
    party_rows,
    party_usable,
    checkpoint_rows=(),
    checkpoint_usable=None,
    checkpoint_inputs=(),
):
    return digest(
        {
            "format": "ai-accounting-kernel/2/report-projection/1",
            "period": period,
            "close_digest": close_digest.hex(),
            "source_rows": [list(row[:-1]) + [row[-1].hex()] for row in rows],
            "party_rows": party_rows,
            "party_usable": party_usable,
            "checkpoint_rows": checkpoint_rows,
            "checkpoint_usable": checkpoint_usable,
            "checkpoint_inputs": checkpoint_inputs,
        }
    )


def _party_delta(
    engine, connection, period, source_rows, *, source="open", classification_refs=None
):
    """Resolve only the closing month's parties; unresolved months use the old read."""

    from collections import defaultdict

    from .history_reads_v1 import V1Reads as QueryReads
    from .report_classification_v1 import selected_classifications
    from .report_party_v1 import RECLASS, report_party_splits

    relevant = [row for row in source_rows if row[8] in RECLASS]
    if not relevant:
        return (), True
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_report_classification'"
        ).fetchone()
        is None
    ):
        return (), False
    reads = QueryReads(engine, connection)
    end = YearMonth.from_ordinal(period)
    source_vouchers = {row[7] or row[2] for row in relevant}
    problems = []
    if classification_refs is None:
        classifications = selected_classifications(
            connection, reads, end.ordinal, source_vouchers, problems
        )
    else:
        headers = list(
            connection.execute(
                "SELECT c.revision_id,c.period,c.voucher_version_id,v.period voucher_period "
                "FROM json_each(?) ids JOIN fact_report_classification c "
                "ON c.revision_id=ids.value LEFT JOIN voucher_version v "
                "ON v.id=c.voucher_version_id",
                (canonical(sorted(classification_refs)),),
            )
        )
        keys = [row["voucher_version_id"] for row in headers]
        if len(keys) != len(set(keys)) or any(
            row["voucher_period"] is None or row["period"] != row["voucher_period"]
            for row in headers
        ):
            return (), False
        selected_ids = {
            row["revision_id"] for row in headers if row["voucher_version_id"] in source_vouchers
        }
        classifications = {
            item.fact.voucher_version_id: item.fact
            for item in reads.fact_versions(selected_ids).values()
        }
    originals = reads.vouchers({row[7] for row in relevant if row[7]})
    source_ids = {originals[row[7]]["calculation_id"] if row[7] else row[5] for row in relevant}
    from .query_relations_v1 import resolve_calculation_relations

    relations = reads.relations_many(source_ids, resolver=resolve_calculation_relations)
    totals = defaultdict(int)
    for item in relevant:
        source_id = originals[item[7]]["calculation_id"] if item[7] else item[5]
        resolution = relations[source_id]
        problems.extend(resolution["issues"])
        selected = classifications.get(item[7] or item[2])
        explicit = (
            [entry.counterparty_id for entry in selected.counterparties if entry.line_no == item[3]]
            if selected
            else []
        )
        row = {
            "version_id": item[2],
            "line_no": item[3],
            "account": item[8],
            "amount": item[9] - item[10],
            "reverses_id": item[7],
        }
        party = report_party_splits(
            row, resolution, explicit_party_id=explicit[0] if len(explicit) == 1 else None
        )
        problems.extend(party["issues"])
        if party["splits"] is None:
            return (), False
        for key, amount in party["splits"]:
            totals[item[8], canonical(key)] += amount
    if problems:
        return (), False
    return tuple(
        (period, account, key, amount)
        for (account, key), amount in sorted(totals.items())
        if amount
    ), True


def _checkpoint_seal(period, rows, usable, inputs):
    return digest({"period": period, "rows": rows, "usable": usable, "inputs": inputs})


def _stored_month(connection, period):
    """Verify one derived month against its immutable private report root."""

    close = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
    if close is None:
        raise KernelError("frozen_snapshot_unavailable", "报表往来月份尚未关账")
    source_rows = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT " + ",".join(SOURCE_COLUMNS) + " FROM report_line_source "
            "WHERE scope='closed' AND posting_period=? ORDER BY version_id,line_no",
            (period,),
        )
    )
    source_seal = connection.execute(
        "SELECT row_count,digest FROM report_line_source_seal "
        "WHERE scope='closed' AND posting_period=?",
        (period,),
    ).fetchone()
    if source_seal is None or tuple(source_seal) != _seal(
        connection, "closed", period, source_rows
    ):
        raise KernelError("content_integrity_failed", "关账报表来源封签不一致")
    party_rows = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT posting_period,account,party_key,amount FROM report_party_delta "
            "WHERE posting_period=? ORDER BY account,party_key",
            (period,),
        )
    )
    party_seal = connection.execute(
        "SELECT row_count,usable,digest FROM report_party_month_seal WHERE posting_period=?",
        (period,),
    ).fetchone()
    if party_seal is None:
        raise KernelError("content_integrity_failed", "关账报表往来增量封签缺失")
    party_usable = bool(party_seal["usable"])
    if tuple(party_seal) != (
        len(party_rows),
        int(party_usable),
        _party_seal(period, party_rows, party_usable),
    ):
        raise KernelError("content_integrity_failed", "关账报表往来增量封签不一致")
    checkpoint_rows = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT posting_period,account,party_key,amount FROM report_party_checkpoint "
            "WHERE posting_period=? ORDER BY account,party_key",
            (period,),
        )
    )
    checkpoint_seal = connection.execute(
        "SELECT row_count,usable,inputs,digest FROM report_party_checkpoint_seal "
        "WHERE posting_period=?",
        (period,),
    ).fetchone()
    if period % 12 == 11:
        if checkpoint_seal is None:
            raise KernelError("content_integrity_failed", "年末报表往来快照缺失")
        import json

        checkpoint_usable = bool(checkpoint_seal["usable"])
        checkpoint_inputs = tuple(tuple(item) for item in json.loads(checkpoint_seal["inputs"]))
        if (
            checkpoint_seal["row_count"],
            checkpoint_seal["digest"],
        ) != (
            len(checkpoint_rows),
            _checkpoint_seal(period, checkpoint_rows, checkpoint_usable, checkpoint_inputs),
        ):
            raise KernelError("content_integrity_failed", "年末报表往来快照封签不一致")
    else:
        if checkpoint_seal is not None or checkpoint_rows:
            raise KernelError("content_integrity_failed", "非年末存在报表往来快照")
        checkpoint_usable, checkpoint_inputs = None, ()
    root = _report_root(
        period,
        close["digest"],
        source_rows,
        party_rows,
        party_usable,
        checkpoint_rows,
        checkpoint_usable,
        checkpoint_inputs,
    )
    reader = close_reader()
    header = reader.verified_header(connection, close, require_marker=True)
    if reader.derived_root(header, "report") != root:
        raise KernelError("content_integrity_failed", "报表往来快照与不可变关账根不一致")
    return {
        "root": root,
        "party_rows": party_rows,
        "party_usable": party_usable,
        "checkpoint_rows": checkpoint_rows,
        "checkpoint_usable": checkpoint_usable,
    }


def _expected_checkpoint(connection, period, party_rows, party_usable, months):
    """Independent rebuild using earlier authoritative months, never stored projection."""

    if period % 12 != 11:
        return (), None, ()
    year_start = period - 11
    previous_period = year_start - 1
    usable, totals, inputs = party_usable, {}, []
    previous = months.get(previous_period)
    if previous is not None:
        inputs.append((previous_period, previous["root"].hex()))
        usable = usable and bool(previous["checkpoint_usable"])
        if usable:
            totals.update({(row[1], row[2]): row[3] for row in previous["checkpoint_rows"]})
    elif connection.execute(
        "SELECT 1 FROM calculation_publication WHERE posting_period<? LIMIT 1",
        (year_start,),
    ).fetchone():
        usable = False
    for month in range(year_start, period):
        selected = months.get(month)
        if selected is None:
            usable = False
            continue
        inputs.append((month, selected["root"].hex()))
        usable = usable and selected["party_usable"]
        if usable:
            for row in selected["party_rows"]:
                key = row[1], row[2]
                totals[key] = totals.get(key, 0) + row[3]
    if usable:
        for row in party_rows:
            key = row[1], row[2]
            totals[key] = totals.get(key, 0) + row[3]
    rows = (
        tuple(
            (period, account, key, amount)
            for (account, key), amount in sorted(totals.items())
            if amount
        )
        if usable
        else ()
    )
    return rows, bool(usable), tuple(inputs)


def _party_seal(period, rows, usable):
    return digest({"period": period, "rows": rows, "usable": usable})


def party_balance_rows(engine, connection, cutoff, *, source="open"):
    """Return exact party net rows from one rooted year-end and bounded later months.

    None means the optimized source cannot establish the complete report; the
    caller must retain its original voucher-by-voucher read and issue path.
    """

    import json

    from .report_party_v1 import RECLASS

    if cutoff < 0:
        return []
    if connection.execute(
        "SELECT 1 FROM opening_account WHERE period<=? AND account IN "
        "(SELECT value FROM json_each(?)) AND debit<>credit LIMIT 1",
        (cutoff, canonical(sorted(RECLASS))),
    ).fetchone():
        return None
    if (
        connection.execute(
            "SELECT 1 FROM calculation_publication WHERE posting_period<=? LIMIT 1", (cutoff,)
        ).fetchone()
        is None
    ):
        return []
    last = connection.execute(
        "SELECT posting_period FROM report_party_checkpoint_seal "
        "WHERE posting_period<=? AND usable=1 ORDER BY posting_period DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    if last:
        start = last[0] + 1
        checkpoint = _stored_month(connection, last[0])
        if not checkpoint["checkpoint_usable"]:
            return None
        totals = {(row[1], row[2]): row[3] for row in checkpoint["checkpoint_rows"]}
    else:
        start = cutoff - cutoff % 12
        if connection.execute(
            "SELECT 1 FROM calculation_publication WHERE posting_period<? LIMIT 1", (start,)
        ).fetchone():
            return None
        totals = {}
    for month in range(start, cutoff + 1):
        closed = connection.execute(
            "SELECT 1 FROM period_close WHERE period=?", (month,)
        ).fetchone()
        if closed:
            selected = _stored_month(connection, month)
            if not selected["party_usable"]:
                return None
            rows = selected["party_rows"]
        elif source == "closed":
            continue
        else:
            rows = tuple(_authoritative_rows(connection, "open", month))
            rows, usable = _party_delta(engine, connection, month, rows)
            if not usable:
                return None
        for row in rows:
            key = row[1], row[2]
            totals[key] = totals.get(key, 0) + row[3]

    def freeze_key(value):
        if isinstance(value, list):
            return tuple(freeze_key(item) for item in value)
        return value

    return [
        {
            "account": account,
            "amount": amount,
            "party_splits": ((freeze_key(json.loads(party_key)), amount),),
        }
        for (account, party_key), amount in sorted(totals.items())
        if amount
    ]


def verify_report_lines(connection, scope, period, *, authoritative=True):
    """Check the seal and, when reading, all selected source identities and values."""

    period = _ordinal(period) if not isinstance(period, int) else period
    rows = [
        tuple(row)
        for row in connection.execute(
            "SELECT " + ",".join(SOURCE_COLUMNS) + " FROM report_line_source "
            "WHERE scope=? AND posting_period=? ORDER BY version_id,line_no",
            (scope, period),
        )
    ]
    expected = connection.execute(
        "SELECT row_count,digest FROM report_line_source_seal WHERE scope=? AND posting_period=?",
        (scope, period),
    ).fetchone()
    count, checksum = _seal(connection, scope, period, rows)
    if expected is None or (count, checksum) != tuple(expected):
        raise KernelError("content_integrity_failed", "报表来源投影封签不一致")
    if authoritative and rows != _authoritative_rows(connection, scope, period):
        raise KernelError("content_integrity_failed", "报表来源投影与权威凭证不一致")
    return rows


def source_rows(connection, scope, periods):
    """Return only verified requested months; callers still check close leaves."""

    return [
        row
        for period in sorted(set(periods))
        for row in verify_report_lines(connection, scope, period)
    ]


def compare_report_projection(engine, connection, *, through_period=None, _verified_closes=None):
    """Rebuild expected derived rows from authority, verifying every frozen root."""

    restriction = "" if through_period is None else " WHERE posting_period<=?"
    parameters = () if through_period is None else (through_period,)
    publication_periods = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT posting_period FROM calculation_publication" + restriction,
            parameters,
        )
    }
    close_restriction = "" if through_period is None else " WHERE period<=?"
    closed = {
        row["period"]: row
        for row in connection.execute("SELECT * FROM period_close" + close_restriction, parameters)
    }
    decoded = None
    if _verified_closes is not None:
        if not connection.in_transaction:
            raise KernelError("content_integrity_failed", "报表来源复用须处于同一事务")
        from .verified_close_archive import VerifiedCloseArchive

        if isinstance(_verified_closes, VerifiedCloseArchive):
            decoded = _verified_closes.lookup(connection, list(closed.values()))
        else:
            decoded = {row["period"]: (row, manifest) for row, manifest in _verified_closes}
            if (
                len(decoded) != len(_verified_closes)
                or decoded.keys() != closed.keys()
                or any(tuple(pair[0]) != tuple(closed[period]) for period, pair in decoded.items())
            ):
                raise KernelError("content_integrity_failed", "报表来源复用的关账集合不一致")
    wanted = {
        ("closed" if period in closed else "open", period)
        for period in publication_periods | closed.keys()
    }
    expected_rows, expected_seals, expected_party_rows, expected_party_seals = [], [], [], []
    expected_checkpoint_rows, expected_checkpoint_seals, expected_months = [], [], {}
    verified_closed = []
    frozen_classification_refs = set()
    for scope, period in sorted(wanted):
        if scope == "closed":
            reader = close_reader()
            row = closed[period]
            header = reader.verified_header(connection, row, require_marker=True)
            manifest = (
                reader.decode_close(connection, row) if decoded is None else decoded[period][1]
            )
            frozen_classification_refs.update(
                manifest["readiness"].get("financial_reports", {}).get("facts", ())
            )
            rows = _manifest_rows(connection, period, manifest)
            party_rows, party_usable = _party_delta(
                engine,
                connection,
                period,
                rows,
                source="closed",
                classification_refs=frozen_classification_refs,
            )
            checkpoint_rows, checkpoint_usable, checkpoint_inputs = _expected_checkpoint(
                connection, period, party_rows, party_usable, expected_months
            )
            expected_root = _report_root(
                period,
                row["digest"],
                rows,
                party_rows,
                party_usable,
                checkpoint_rows,
                checkpoint_usable,
                checkpoint_inputs,
            )
            if reader.derived_root(header, "report") != expected_root:
                raise KernelError(
                    "content_integrity_failed",
                    "关账报表来源与不可变冻结投影根不一致",
                    component="report_projection",
                    record_id=str(period),
                    reason="frozen_root_mismatch",
                )
            verified_closed.append(
                (
                    period,
                    PreparedReport(
                        period,
                        row["digest"],
                        rows,
                        party_rows,
                        party_usable,
                        checkpoint_rows,
                        checkpoint_usable,
                        checkpoint_inputs,
                        expected_root,
                    ),
                )
            )
            expected_party_rows.extend(party_rows)
            expected_party_seals.append(
                (
                    period,
                    len(party_rows),
                    int(party_usable),
                    _party_seal(period, party_rows, party_usable),
                )
            )
            expected_months[period] = {
                "root": expected_root,
                "party_rows": party_rows,
                "party_usable": party_usable,
                "checkpoint_rows": checkpoint_rows,
                "checkpoint_usable": checkpoint_usable,
            }
            if checkpoint_usable is not None:
                expected_checkpoint_rows.extend(checkpoint_rows)
                expected_checkpoint_seals.append(
                    (
                        period,
                        len(checkpoint_rows),
                        int(checkpoint_usable),
                        canonical(checkpoint_inputs),
                        _checkpoint_seal(
                            period, checkpoint_rows, checkpoint_usable, checkpoint_inputs
                        ),
                    )
                )
        else:
            rows = _authoritative_rows(connection, scope, period)
        expected_rows.extend(rows)
        count, checksum = _seal(connection, scope, period, rows)
        expected_seals.append((scope, period, count, checksum))
    suffix = "" if through_period is None else " WHERE posting_period<=?"
    actual_rows = [
        tuple(row)
        for row in connection.execute(
            "SELECT "
            + ",".join(SOURCE_COLUMNS)
            + " FROM report_line_source"
            + suffix
            + " ORDER BY scope,posting_period,version_id,line_no",
            parameters,
        )
    ]
    actual_seals = [
        tuple(row)
        for row in connection.execute(
            "SELECT scope,posting_period,row_count,digest FROM report_line_source_seal"
            + suffix
            + " ORDER BY scope,posting_period",
            parameters,
        )
    ]
    party_suffix = "" if through_period is None else " WHERE posting_period<=?"
    actual_party_rows = [
        tuple(row)
        for row in connection.execute(
            "SELECT posting_period,account,party_key,amount FROM report_party_delta"
            + party_suffix
            + " ORDER BY posting_period,account,party_key",
            parameters,
        )
    ]
    actual_party_seals = [
        tuple(row)
        for row in connection.execute(
            "SELECT posting_period,row_count,usable,digest FROM report_party_month_seal"
            + party_suffix
            + " ORDER BY posting_period",
            parameters,
        )
    ]
    actual_checkpoint_rows = [
        tuple(row)
        for row in connection.execute(
            "SELECT posting_period,account,party_key,amount FROM report_party_checkpoint"
            + party_suffix
            + " ORDER BY posting_period,account,party_key",
            parameters,
        )
    ]
    actual_checkpoint_seals = [
        tuple(row)
        for row in connection.execute(
            "SELECT posting_period,row_count,usable,inputs,digest "
            "FROM report_party_checkpoint_seal" + party_suffix + " ORDER BY posting_period",
            parameters,
        )
    ]
    expected_rows.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    expected_seals.sort(key=lambda row: (row[0], row[1]))
    expected_party_rows.sort(key=lambda row: (row[0], row[1], row[2]))
    expected_party_seals.sort(key=lambda row: row[0])
    expected_checkpoint_rows.sort(key=lambda row: (row[0], row[1], row[2]))
    expected_checkpoint_seals.sort(key=lambda row: row[0])
    return {
        "changed": (
            actual_rows != expected_rows
            or actual_seals != expected_seals
            or actual_party_rows != expected_party_rows
            or actual_party_seals != expected_party_seals
            or actual_checkpoint_rows != expected_checkpoint_rows
            or actual_checkpoint_seals != expected_checkpoint_seals
        ),
        "expected_rows": expected_rows,
        "expected_seals": expected_seals,
        "expected_party_rows": expected_party_rows,
        "expected_party_seals": expected_party_seals,
        "expected_checkpoint_rows": expected_checkpoint_rows,
        "expected_checkpoint_seals": expected_checkpoint_seals,
        "row_count": len(expected_rows),
        "period_count": len(wanted),
        "_verified_closed": tuple(verified_closed),
    }


def require_report_projection(
    engine, connection, *, through_period=None, _verified_closes=None, _return_verified=False
):
    compared = compare_report_projection(
        engine,
        connection,
        through_period=through_period,
        _verified_closes=_verified_closes,
    )
    if compared["changed"]:
        raise KernelError(
            "content_integrity_failed",
            "报表来源投影与已核验来源不一致，需要显式维修",
            component="report_projection",
            record_id="*",
            reason="projection_mismatch",
        )
    if _return_verified:
        from .verified_source_lease import current_verified_lease

        return _VerifiedReports(
            connection, compared["_verified_closed"], current_verified_lease(connection)
        )
    return {"rows": compared["row_count"], "periods": compared["period_count"]}


def selected_voucher_sql(
    period,
    *,
    current_heads=False,
    subject_ids=None,
    kinds=None,
    posting_period=None,
    after_number=None,
    posting_start=None,
    voucher_ids=None,
    authoritative_vouchers=None,
):
    """Composable exact selection driven by bounded voucher candidates.

    Outer predicates and LIMIT may be applied without materializing a global
    close-reference grouping. Hit close_period values require local verification.
    """
    cutoff = period.ordinal if isinstance(period, YearMonth) else YearMonth(period).ordinal
    # Bound the driving voucher identities before looking up close/current state.
    # Keeping the predicates below also preserves the original exact-selection
    # contract: this CTE is only an indexed candidate directory, not a new rule.
    prefix, driver, parameters = "WITH ", "voucher_version v", []
    if voucher_ids is not None:
        prefix += "scoped_vouchers AS (SELECT value AS id FROM json_each(?)), "
        parameters.append(canonical(sorted(set(voucher_ids))))
        driver = "scoped_vouchers scoped CROSS JOIN voucher_version v ON v.id=scoped.id"
    elif subject_ids is not None:
        subjects = canonical(sorted({subject_ids} if isinstance(subject_ids, str) else subject_ids))
        source = (
            " FROM json_each(?) ids CROSS JOIN calculation sc INDEXED BY calculation_subject "
            "ON sc.subject_id=ids.value CROSS JOIN voucher_version original "
            "INDEXED BY voucher_calculation ON original.calculation_id=sc.id"
        )
        prefix += (
            "scoped_vouchers AS (SELECT original.id"
            + source
            + " UNION SELECT reversal.id"
            + source
            + " CROSS JOIN voucher_version reversal INDEXED BY voucher_version_reverses "
            "ON reversal.reverses_id=original.id), "
        )
        parameters.extend((subjects, subjects))
        driver = "scoped_vouchers scoped CROSS JOIN voucher_version v ON v.id=scoped.id"
    elif kinds is not None:
        prefix += (
            "scoped_vouchers AS (SELECT original.id FROM json_each(?) selected_kinds "
            "CROSS JOIN calculation sc INDEXED BY calculation_kind_period "
            "ON sc.kind=selected_kinds.value CROSS JOIN voucher_version original "
            "INDEXED BY voucher_calculation ON original.calculation_id=sc.id), "
        )
        parameters.append(canonical(sorted(set(kinds))))
        driver = "scoped_vouchers scoped CROSS JOIN voucher_version v ON v.id=scoped.id"
    if authoritative_vouchers is not None:
        prefix += (
            "frozen_vouchers AS MATERIALIZED (SELECT json_extract(value,'$[0]') AS id,"
            "json_extract(value,'$[1]') AS close_period FROM json_each(?)), "
        )
        parameters.append(
            canonical(
                sorted(
                    [ident, close_period] for ident, close_period in authoritative_vouchers.items()
                )
            )
        )
        close_period = "frozen.close_period"
        frozen_join = " LEFT JOIN frozen_vouchers frozen ON frozen.id=v.id"
    else:
        close_period = (
            "(SELECT min(r.close_period) FROM close_reference r WHERE r.reference_type='voucher' "
            "AND r.reference_id=v.id AND r.close_period<=?)"
        )
        frozen_join = ""
    frozen_driven = (
        authoritative_vouchers is not None
        and voucher_ids is not None
        and subject_ids is None
        and kinds is None
        and posting_period is None
        and posting_start is None
        and after_number is None
    )
    if frozen_driven:
        # A materialized JSON CTE has no id index. Joining every scoped
        # voucher to it scans the entire frozen list for each candidate.
        # Drive sealed rows from the frozen list once, then add only scoped
        # identities absent from that list. The selected CTE below still
        # applies its exact current/closed and reversal rules to both arms.
        sql = (
            prefix + "candidates AS ("
            "SELECT v.*,n.number,vc.subject_id AS voucher_subject_id,"
            "frozen.close_period AS close_period FROM frozen_vouchers frozen "
            "CROSS JOIN voucher_version v ON v.id=frozen.id "
            "CROSS JOIN voucher n ON n.id=v.voucher_id "
            "CROSS JOIN calculation vc ON vc.id=v.calculation_id "
            "WHERE v.period<=? AND v.id IN (SELECT id FROM scoped_vouchers) "
            "UNION ALL SELECT v.*,n.number,vc.subject_id AS voucher_subject_id,"
            "NULL AS close_period FROM scoped_vouchers scoped "
            "CROSS JOIN voucher_version v ON v.id=scoped.id "
            "CROSS JOIN voucher n ON n.id=v.voucher_id "
            "CROSS JOIN calculation vc ON vc.id=v.calculation_id "
            "WHERE v.period<=? AND v.id NOT IN "
            "(SELECT id FROM frozen_vouchers WHERE id IS NOT NULL)"
        )
        parameters.extend((cutoff, cutoff))
    else:
        sql = (
            prefix + "candidates AS (SELECT v.*,n.number,vc.subject_id AS voucher_subject_id,"
            f"{close_period} AS close_period "
            f"FROM {driver}{frozen_join} CROSS JOIN voucher n ON n.id=v.voucher_id "
            "CROSS JOIN calculation vc ON vc.id=v.calculation_id WHERE v.period<=?"
        )
        parameters.extend((cutoff, cutoff) if authoritative_vouchers is None else (cutoff,))
        if posting_period is not None:
            sql += " AND v.period=?"
            parameters.append(YearMonth(posting_period).ordinal)
        if posting_start is not None:
            sql += " AND v.period>=?"
            parameters.append(YearMonth(posting_start).ordinal)
        if voucher_ids is not None:
            sql += " AND v.id IN (SELECT value FROM json_each(?))"
            parameters.append(canonical(sorted(voucher_ids)))
        if after_number is not None:
            sql += " AND n.number>?"
            parameters.append(after_number)
        if subject_ids is not None:
            sql += (
                " AND (vc.subject_id IN (SELECT value FROM json_each(?)) OR EXISTS("
                "SELECT 1 FROM voucher_version original JOIN calculation oc "
                "ON oc.id=original.calculation_id WHERE original.id=v.reverses_id "
                "AND oc.subject_id IN (SELECT value FROM json_each(?))))"
            )
            subjects = canonical(
                sorted({subject_ids} if isinstance(subject_ids, str) else subject_ids)
            )
            parameters.extend((subjects, subjects))
        if kinds is not None:
            sql += " AND vc.kind IN (SELECT value FROM json_each(?))"
            parameters.append(canonical(sorted(kinds)))
    sql += (
        "), selected AS (SELECT v.*,CASE WHEN v.close_period IS NOT NULL "
        "THEN 'close_manifest' ELSE 'current_publication' END AS selection_source,"
        "v.calculation_id AS voucher_calculation_id,CASE WHEN v.reverses_id IS NOT NULL "
        "THEN original.calculation_id WHEN (? OR v.close_period IS NULL) "
        "THEN coalesce((SELECT a.calculation_id FROM calculation_current a "
        "JOIN calculation_publication p ON p.calculation_id=a.calculation_id "
        "WHERE a.subject_id=v.voucher_subject_id AND p.voucher_id=v.voucher_id "
        "AND p.posting_period=v.period),v.calculation_id) ELSE v.calculation_id END "
        "AS basis_calculation_id FROM candidates v LEFT JOIN voucher_version original "
        "ON original.id=v.reverses_id WHERE v.close_period IS NOT NULL OR (EXISTS("
        "SELECT 1 FROM voucher_current a WHERE a.version_id=v.id) AND NOT EXISTS("
        "SELECT 1 FROM period_close p WHERE p.period=v.period))) SELECT s.*,"
        "c.subject_id AS basis_subject_id,c.kind AS basis_kind,c.fact_id AS basis_fact_id,"
        "c.period AS calculation_period,c.digest AS result_digest,EXISTS("
        "SELECT 1 FROM voucher_version x WHERE x.calculation_id=s.voucher_calculation_id "
        "AND x.reverses_id IS NOT NULL) AS replacement FROM selected s "
        "JOIN calculation c ON c.id=s.basis_calculation_id"
    )
    parameters.append(bool(current_heads))
    return sql, parameters
