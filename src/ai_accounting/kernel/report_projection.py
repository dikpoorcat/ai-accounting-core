"""Rebuildable, period-scoped source directory for quarterly reports.

The directory is deliberately narrower than a report: management classifications,
party decisions, statement amounts and readiness are never stored here.  A reader
must still verify the selected authoritative rows and frozen close references.
"""

from __future__ import annotations

from dataclasses import dataclass

from .content_history_context import close_reader
from .contracts import KernelError
from .query_reads import selected_voucher_sql, verify_current_voucher_publications
from .types import YearMonth, canonical, digest

# The release schema owner incorporates this constant in the one frozen DDL.
REPORT_SOURCE_DDL = """
CREATE TABLE report_line_source(
 scope TEXT NOT NULL CHECK(scope IN('open','closed')),
 posting_period INTEGER NOT NULL CHECK(posting_period BETWEEN 0 AND 119987),
 version_id TEXT NOT NULL REFERENCES voucher_version(id),
 line_no INTEGER NOT NULL CHECK(line_no>0),
 voucher_calculation_id TEXT NOT NULL REFERENCES calculation(id),
 basis_calculation_id TEXT NOT NULL REFERENCES calculation(id),
 publication_id TEXT REFERENCES calculation_publication(id),
 reverses_id TEXT REFERENCES voucher_version(id),
 account TEXT NOT NULL,
 debit INTEGER NOT NULL CHECK(debit>=0),
 credit INTEGER NOT NULL CHECK(credit>=0),
 cashflow TEXT,
 kind TEXT NOT NULL,
 fact_id TEXT NOT NULL REFERENCES fact_revision(id),
 calculation_period INTEGER NOT NULL,
 calculation_subject_id TEXT NOT NULL REFERENCES subject(id),
 result_digest BLOB NOT NULL CHECK(length(result_digest)=32),
 PRIMARY KEY(scope,posting_period,version_id,line_no)
) STRICT;
CREATE INDEX report_line_source_account ON report_line_source(
 scope,account,posting_period,version_id,line_no);
CREATE TABLE report_line_source_seal(
 scope TEXT NOT NULL CHECK(scope IN('open','closed')),
 posting_period INTEGER NOT NULL CHECK(posting_period BETWEEN 0 AND 119987),
 row_count INTEGER NOT NULL CHECK(row_count>=0),
 digest BLOB NOT NULL CHECK(length(digest)=32),
 PRIMARY KEY(scope,posting_period)
) STRICT;
CREATE TABLE report_party_delta(
 posting_period INTEGER NOT NULL CHECK(posting_period BETWEEN 0 AND 119987),
 account TEXT NOT NULL,party_key TEXT NOT NULL CHECK(json_valid(party_key)),
 amount INTEGER NOT NULL,PRIMARY KEY(posting_period,account,party_key)
) STRICT;
CREATE TABLE report_party_month_seal(
 posting_period INTEGER PRIMARY KEY CHECK(posting_period BETWEEN 0 AND 119987),
 row_count INTEGER NOT NULL CHECK(row_count>=0),
 usable INTEGER NOT NULL CHECK(usable IN(0,1)),
 digest BLOB NOT NULL CHECK(length(digest)=32)
) STRICT;
CREATE TABLE report_party_checkpoint(
 posting_period INTEGER NOT NULL CHECK(posting_period BETWEEN 0 AND 119987),
 account TEXT NOT NULL,party_key TEXT NOT NULL CHECK(json_valid(party_key)),
 amount INTEGER NOT NULL,PRIMARY KEY(posting_period,account,party_key)
) STRICT;
CREATE TABLE report_party_checkpoint_seal(
 posting_period INTEGER PRIMARY KEY CHECK(posting_period BETWEEN 0 AND 119987),
 row_count INTEGER NOT NULL CHECK(row_count>=0),
 usable INTEGER NOT NULL CHECK(usable IN(0,1)),
 inputs TEXT NOT NULL CHECK(json_valid(inputs)),
 digest BLOB NOT NULL CHECK(length(digest)=32)
) STRICT;
"""

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


def _authoritative_rows(connection, scope, period, *, with_absence=False):
    month = YearMonth.from_ordinal(period)
    no_close_references = False
    if scope == "open" and connection.execute(
        "SELECT 1 FROM period_close WHERE period=?", (period,)
    ).fetchone() is None:
        # Prove this exact month has no voucher adoption before omitting the
        # otherwise repeated per-voucher close-reference lookup. An unexpected
        # reference keeps the complete selection path and its mismatch check.
        no_close_references = connection.execute(
            "SELECT 1 FROM voucher_version v INDEXED BY voucher_period "
            "CROSS JOIN close_reference r INDEXED BY close_reference_lookup "
            "ON r.reference_type='voucher' AND r.reference_id=v.id AND r.close_period<=? "
            "WHERE v.period=? LIMIT 1",
            (period, period),
        ).fetchone() is None
    selected, params = selected_voucher_sql(
        month, posting_period=month, no_close_references=no_close_references
    )
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
    rows = [tuple(row) for row in connection.execute(query, (*params, scope))]
    if scope == "open":
        verify_current_voucher_publications(connection, {row[2] for row in rows})
    return (rows, no_close_references) if with_absence else rows


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
    engine,
    connection,
    period,
    source_rows,
    *,
    source="open",
    classification_refs=None,
    reads=None,
):
    """Resolve only the closing month's parties; unresolved months use the old read."""

    from collections import defaultdict

    from .query_reads import QueryReads
    from .query_semantics import RECLASS, report_party_splits
    from .reports import _report_classifications, _verify_report_fact_sources

    cache = (
        reads._report_snapshot_cache
        if reads is not None and reads._snapshot_active and reads.connection is connection
        else None
    )
    cache_key = (
        "report_party_delta",
        period,
        source,
        None if classification_refs is None else tuple(sorted(classification_refs)),
    )
    if cache is not None and cache_key in cache:
        return cache[cache_key]
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
    reads = reads or QueryReads(engine, connection)
    end = YearMonth.from_ordinal(period)
    source_vouchers = {row[7] or row[2] for row in relevant}
    problems = []
    if classification_refs is None:
        from .report_classification_directory import classification_directory_scope

        # Party resolution only consumes classifications of the selected
        # vouchers. The close-bound key path proves exact membership and
        # absence without constructing every historical classification head.
        rooted = classification_directory_scope(
            connection, period, source_vouchers, reads=reads
        )
        if rooted is None:
            references = set()
        else:
            references = {
                ident
                for entries in rooted["membership"].values()
                for ident, _, _, _ in entries
            }
            frozen_expected = {
                ident: (key, digest_hex)
                for key, entries in rooted["membership"].items()
                for ident, digest_hex, _, _ in entries
            }
            actual = {
                row["id"]: (row["voucher_version_id"], row["digest"].hex())
                for row in connection.execute(
                    "SELECT f.id,f.digest,c.voucher_version_id FROM json_each(?) ids "
                    "CROSS JOIN fact_revision f ON f.id=ids.value "
                    "JOIN fact_report_classification c ON c.revision_id=f.id",
                    (canonical(sorted(references)),),
                )
            }
            if actual != frozen_expected:
                raise KernelError(
                    "content_integrity_failed", "冻结报表往来分类来源身份或摘要不一致"
                )
        references.update(
            row[0]
            for row in connection.execute(
                "SELECT f.id FROM fact_report_classification c "
                "CROSS JOIN fact_revision f ON f.id=c.revision_id "
                "CROSS JOIN fact_current a ON a.fact_id=f.id "
                "WHERE f.period<=? AND NOT EXISTS("
                "SELECT 1 FROM period_close p WHERE p.period=f.period) "
                "AND c.voucher_version_id IN (SELECT value FROM json_each(?))",
                (period, canonical(sorted(source_vouchers))),
            )
        )
        classifications = _report_classifications(
            connection, reads, references, source_vouchers, end, source, problems
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
        _verify_report_fact_sources(connection, reads, selected_ids)
        classifications = {
            item.fact.voucher_version_id: item.fact
            for item in reads.fact_versions(selected_ids).values()
        }
    originals = reads.vouchers({row[7] for row in relevant if row[7]})
    prepared_rows = []
    lines_by_source = defaultdict(list)
    for item in relevant:
        source_id = originals[item[7]]["calculation_id"] if item[7] else item[5]
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
            "debit": item[9],
            "credit": item[10],
            "cashflow": item[11],
            "reverses_id": item[7],
        }
        prepared_rows.append((item, source_id, explicit, row))
        lines_by_source[source_id].append(row)
    if source == "open" and reads._snapshot_active:
        from .report_open_contribution import read_open_contributions, selected_open_line

        contributions = read_open_contributions(
            engine, connection, lines_by_source, reads=reads
        )
        relations = {}
        fallback = {}
        for source_id, source_lines in lines_by_source.items():
            content = contributions.get(source_id)
            selected = (
                [selected_open_line(content, line) for line in source_lines]
                if content is not None
                else []
            )
            if selected and all(item is not None for item in selected):
                relations[source_id] = selected[0][1]
            else:
                fallback[source_id] = source_lines
        if fallback:
            relations.update(reads.report_line_relations_many(fallback))
    else:
        relations = reads.relations_many(lines_by_source)
    totals = defaultdict(int)
    for item, source_id, explicit, row in prepared_rows:
        resolution = relations[source_id]
        problems.extend(resolution["issues"])
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
    result = (
        tuple(
            (period, account, key, amount)
            for (account, key), amount in sorted(totals.items())
            if amount
        ),
        True,
    )
    if cache is not None:
        cache[cache_key] = result
    return result


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
        "source_rows": source_rows,
        "party_rows": party_rows,
        "party_usable": party_usable,
        "checkpoint_rows": checkpoint_rows,
        "checkpoint_usable": checkpoint_usable,
    }


def _prepare_checkpoint(connection, period, party_rows, party_usable):
    if period % 12 != 11:
        return (), None, ()
    year_start = period - 11
    previous_period = year_start - 1
    inputs = []
    totals = {}
    previous = connection.execute(
        "SELECT 1 FROM period_close WHERE period=?", (previous_period,)
    ).fetchone()
    usable = party_usable
    if previous:
        stored = _stored_month(connection, previous_period)
        inputs.append((previous_period, stored["root"].hex()))
        usable = usable and bool(stored["checkpoint_usable"])
        if usable:
            totals.update({(row[1], row[2]): row[3] for row in stored["checkpoint_rows"]})
    elif connection.execute(
        "SELECT 1 FROM calculation_publication WHERE posting_period<? LIMIT 1",
        (year_start,),
    ).fetchone():
        usable = False
    for month in range(year_start, period):
        if (
            connection.execute("SELECT 1 FROM period_close WHERE period=?", (month,)).fetchone()
            is None
        ):
            usable = False
            continue
        stored = _stored_month(connection, month)
        inputs.append((month, stored["root"].hex()))
        usable = usable and stored["party_usable"]
        if usable:
            for row in stored["party_rows"]:
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


def prepare_report_projection(engine, connection, period, logical_manifest, logical_close_digest):
    """Bind this month's exact source lines into the immutable private close root."""

    period = _ordinal(period) if not isinstance(period, int) else period
    if digest(logical_manifest) != logical_close_digest:
        raise KernelError("content_integrity_failed", "关账报表投影逻辑摘要不一致")
    if logical_manifest["period"] != str(YearMonth.from_ordinal(period)):
        raise KernelError("content_integrity_failed", "关账报表投影期间不一致")
    rows = _manifest_rows(connection, period, logical_manifest)
    party_rows, party_usable = _party_delta(engine, connection, period, rows)
    checkpoint_rows, checkpoint_usable, checkpoint_inputs = _prepare_checkpoint(
        connection, period, party_rows, party_usable
    )
    return PreparedReport(
        period,
        logical_close_digest,
        rows,
        party_rows,
        party_usable,
        checkpoint_rows,
        checkpoint_usable,
        checkpoint_inputs,
        _report_root(
            period,
            logical_close_digest,
            rows,
            party_rows,
            party_usable,
            checkpoint_rows,
            checkpoint_usable,
            checkpoint_inputs,
        ),
    )


def persist_report_projection(connection, prepared):
    """Persist only a projection already committed by the private close root."""

    from .close_storage import derived_root, verified_header

    row = connection.execute(
        "SELECT * FROM period_close WHERE period=?", (prepared.period,)
    ).fetchone()
    if row is None or row["digest"] != prepared.close_digest:
        raise KernelError("content_integrity_failed", "关账报表投影缺少匹配的关账来源")
    header = verified_header(connection, row, require_marker=True)
    if derived_root(header, "report") != prepared.root_digest:
        raise KernelError("content_integrity_failed", "关账报表投影根与冻结来源不一致")
    if (
        _report_root(
            prepared.period,
            prepared.close_digest,
            prepared.rows,
            prepared.party_rows,
            prepared.party_usable,
            prepared.checkpoint_rows,
            prepared.checkpoint_usable,
            prepared.checkpoint_inputs,
        )
        != prepared.root_digest
    ):
        raise KernelError("content_integrity_failed", "关账报表投影准备内容不一致")
    if tuple(_authoritative_rows(connection, "closed", prepared.period)) != prepared.rows:
        raise KernelError("content_integrity_failed", "关账报表投影与权威凭证不一致")
    if connection.execute(
        "SELECT 1 FROM report_line_source_seal WHERE scope='closed' AND posting_period=?",
        (prepared.period,),
    ).fetchone():
        raise KernelError("content_integrity_failed", "关账报表投影已存在")
    connection.executemany(
        "INSERT INTO report_line_source("
        + ",".join(SOURCE_COLUMNS)
        + ") VALUES("
        + ",".join("?" for _ in SOURCE_COLUMNS)
        + ")",
        prepared.rows,
    )
    count, checksum = _seal(connection, "closed", prepared.period, prepared.rows)
    connection.execute(
        "INSERT INTO report_line_source_seal VALUES('closed',?,?,?)",
        (prepared.period, count, checksum),
    )
    connection.executemany("INSERT INTO report_party_delta VALUES(?,?,?,?)", prepared.party_rows)
    connection.execute(
        "INSERT INTO report_party_month_seal VALUES(?,?,?,?)",
        (
            prepared.period,
            len(prepared.party_rows),
            int(prepared.party_usable),
            _party_seal(prepared.period, prepared.party_rows, prepared.party_usable),
        ),
    )
    if prepared.checkpoint_usable is not None:
        connection.executemany(
            "INSERT INTO report_party_checkpoint VALUES(?,?,?,?)",
            prepared.checkpoint_rows,
        )
        connection.execute(
            "INSERT INTO report_party_checkpoint_seal VALUES(?,?,?,?,?)",
            (
                prepared.period,
                len(prepared.checkpoint_rows),
                int(prepared.checkpoint_usable),
                canonical(prepared.checkpoint_inputs),
                _checkpoint_seal(
                    prepared.period,
                    prepared.checkpoint_rows,
                    prepared.checkpoint_usable,
                    prepared.checkpoint_inputs,
                ),
            ),
        )
    connection.execute(
        "DELETE FROM report_line_source WHERE scope='open' AND posting_period=?",
        (prepared.period,),
    )
    connection.execute(
        "DELETE FROM report_line_source_seal WHERE scope='open' AND posting_period=?",
        (prepared.period,),
    )
    return count


def _party_seal(period, rows, usable):
    return digest({"period": period, "rows": rows, "usable": usable})


def party_balance_rows(engine, connection, cutoff, *, source="open", reads=None):
    """Return exact party net rows from one rooted year-end and bounded later months.

    None means the optimized source cannot establish the complete report; the
    caller must retain its original voucher-by-voucher read and issue path.
    """

    cache = (
        reads._report_snapshot_cache
        if reads is not None and reads._snapshot_active and reads.connection is connection
        else None
    )
    key = ("party_balance_rows", cutoff, source)
    if cache is not None and key in cache:
        return cache[key]
    result = _party_balance_rows_uncached(engine, connection, cutoff, source=source, reads=reads)
    if cache is not None and result is not None:
        cache[key] = result
    return result


def _freeze_party_key(value):
    if isinstance(value, list):
        return tuple(_freeze_party_key(item) for item in value)
    return value


def _party_balance_rows_uncached(engine, connection, cutoff, *, source, reads):
    """Compute one party balance; only the public wrapper caches success."""

    import json

    from .account_definitions import RECLASS

    cache = (
        reads._report_snapshot_cache
        if reads is not None and reads._snapshot_active and reads.connection is connection
        else None
    )

    def stored_month(period):
        key = ("report_party_month", period)
        if cache is not None and key in cache:
            return cache[key]
        from .report_flow import read_report_flow

        selected = (
            read_report_flow(connection, period, reads=reads)
            if engine is not None
            else _stored_month(connection, period)
        )
        if selected is None:
            return None
        if cache is not None:
            cache[key] = selected
        return selected

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
        checkpoint = stored_month(last[0])
        if checkpoint is None or not checkpoint["checkpoint_usable"]:
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
            selected = stored_month(month)
            if selected is None or not selected["party_usable"]:
                return None
            rows = selected["party_rows"]
        elif source == "closed":
            continue
        else:
            source_rows, no_close_references = _authoritative_rows(
                connection, "open", month, with_absence=True
            )
            rows, usable = _party_delta(
                engine, connection, month, tuple(source_rows), reads=reads
            )
            if not usable:
                return None
            if cache is not None and no_close_references:
                cache["report_open_source_rows", month] = tuple(source_rows)
        for row in rows:
            key = row[1], row[2]
            totals[key] = totals.get(key, 0) + row[3]

    return [
        {
            "account": account,
            "amount": amount,
            "party_splits": ((_freeze_party_key(json.loads(party_key)), amount),),
        }
        for (account, party_key), amount in sorted(totals.items())
        if amount
    ]


def sync_report_lines(connection, periods):
    """Replace only selected open-period rows inside the publication transaction."""

    completed = []
    for value in sorted(set(periods)):
        period = _ordinal(value) if not isinstance(value, int) else value
        if connection.execute("SELECT 1 FROM period_close WHERE period=?", (period,)).fetchone():
            continue
        rows = _authoritative_rows(connection, "open", period)
        connection.execute(
            "DELETE FROM report_line_source WHERE scope='open' AND posting_period=?", (period,)
        )
        connection.executemany(
            "INSERT INTO report_line_source("
            + ",".join(SOURCE_COLUMNS)
            + ") VALUES("
            + ",".join("?" for _ in SOURCE_COLUMNS)
            + ")",
            rows,
        )
        count, checksum = _seal(connection, "open", period, rows)
        connection.execute(
            "INSERT INTO report_line_source_seal VALUES('open',?,?,?) "
            "ON CONFLICT(scope,posting_period) DO UPDATE SET "
            "row_count=excluded.row_count,digest=excluded.digest",
            (period, count, checksum),
        )
        completed.append(period)
    return completed


def freeze_report_lines(connection, period):
    """Capture a closed month's exact selected rows after its close references exist."""

    period = _ordinal(period) if not isinstance(period, int) else period
    if not connection.execute("SELECT 1 FROM period_close WHERE period=?", (period,)).fetchone():
        raise KernelError("frozen_snapshot_unavailable", "报表来源月份尚未关账")
    if connection.execute(
        "SELECT 1 FROM report_line_source_seal WHERE scope='closed' AND posting_period=?",
        (period,),
    ).fetchone():
        raise KernelError("content_integrity_failed", "关账报表来源已经冻结")
    rows = _authoritative_rows(connection, "closed", period)
    connection.executemany(
        "INSERT INTO report_line_source("
        + ",".join(SOURCE_COLUMNS)
        + ") VALUES("
        + ",".join("?" for _ in SOURCE_COLUMNS)
        + ")",
        rows,
    )
    count, checksum = _seal(connection, "closed", period, rows)
    connection.execute(
        "INSERT INTO report_line_source_seal VALUES('closed',?,?,?)",
        (period, count, checksum),
    )
    return count


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


def repair_report_projection(engine, connection, *, fault=None):
    """Rebuild only derived rows inside the caller's verified repair transaction."""

    if not connection.in_transaction:
        raise ValueError("report projection repair requires a write transaction")
    compared = compare_report_projection(engine, connection)
    if compared["changed"]:
        connection.execute("DELETE FROM report_line_source")
        connection.execute("DELETE FROM report_line_source_seal")
        connection.execute("DELETE FROM report_party_delta")
        connection.execute("DELETE FROM report_party_month_seal")
        connection.execute("DELETE FROM report_party_checkpoint")
        connection.execute("DELETE FROM report_party_checkpoint_seal")
        if fault:
            fault("report_projection_cleared", connection)
        connection.executemany(
            "INSERT INTO report_line_source("
            + ",".join(SOURCE_COLUMNS)
            + ") VALUES("
            + ",".join("?" for _ in SOURCE_COLUMNS)
            + ")",
            compared["expected_rows"],
        )
        connection.executemany(
            "INSERT INTO report_line_source_seal VALUES(?,?,?,?)",
            compared["expected_seals"],
        )
        connection.executemany(
            "INSERT INTO report_party_delta VALUES(?,?,?,?)",
            compared["expected_party_rows"],
        )
        connection.executemany(
            "INSERT INTO report_party_month_seal VALUES(?,?,?,?)",
            compared["expected_party_seals"],
        )
        connection.executemany(
            "INSERT INTO report_party_checkpoint VALUES(?,?,?,?)",
            compared["expected_checkpoint_rows"],
        )
        connection.executemany(
            "INSERT INTO report_party_checkpoint_seal VALUES(?,?,?,?,?)",
            compared["expected_checkpoint_seals"],
        )
        if fault:
            fault("report_projection_rebuilt", connection)
    require_report_projection(engine, connection)
    return {
        "changed": compared["changed"],
        "rows": compared["row_count"],
        "periods": compared["period_count"],
    }
