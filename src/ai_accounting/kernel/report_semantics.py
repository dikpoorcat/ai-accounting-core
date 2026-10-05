"""Immutable report-line drivers shared by full reads and a bounded projection.

Management classifications remain a separate, version-selected read.  This
module stores only the calculation and relation fields used by report formulas;
it never derives statement amounts or freezes a management classification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

from .account_definitions import CASH_ACCOUNTS, PROFIT_ACCOUNTS
from .content_history_context import close_reader
from .contracts import KernelError
from .types import YearMonth, canonical, digest

# Kept outside schema.py until both final private derived roots are installed
# in a single development-contract change. These rows are repairable; only the
# private close-storage root is immutable authority.
REPORT_SEMANTICS_DDL = """
CREATE TABLE report_semantic_line(
 posting_period INTEGER NOT NULL REFERENCES period_close(period),
 version_id TEXT NOT NULL REFERENCES voucher_version(id),
 line_no INTEGER NOT NULL CHECK(line_no>0),
 content TEXT NOT NULL CHECK(json_valid(content)),
 PRIMARY KEY(posting_period,version_id,line_no)
) STRICT;
CREATE TABLE report_semantic_seal(
 posting_period INTEGER PRIMARY KEY REFERENCES period_close(period),
 row_count INTEGER NOT NULL CHECK(row_count>=0),
 close_digest BLOB NOT NULL CHECK(length(close_digest)=32),
 root_digest BLOB NOT NULL CHECK(length(root_digest)=32)
) STRICT;
"""

_COMPACT_KEYS = frozenset(
    {
        "source_calculation_id",
        "source_fact_id",
        "source_result_digest",
        "kind",
        "values",
        "fact",
        "cash_source",
        "cash_source_calculation_id",
        "cash_obligation_account",
        "relation_issues",
    }
)


@dataclass(frozen=True)
class PreparedReportSemantics:
    period: int
    close_digest: bytes
    rows: tuple[tuple[int, str, int, str], ...]
    root_digest: bytes


@dataclass(frozen=True)
class _VerifiedSemantics:
    connection: object
    closes: tuple[tuple[int, PreparedReportSemantics], ...]
    lease: object


_FACT_FIELDS = ("expense_class", "income_kind", "funding_kind", "tax_year")
_TAX_VALUE_FIELDS = (
    "surtax_fen",
    "urban_tax_fen",
    "education_tax_fen",
    "local_education_tax_fen",
)


def _values(record):
    return (record.get("decoded") or record["outcome"])["values"]


def report_source_fact(record):
    """Keep the existing typed outcome attributes used by report formulas."""

    values = dict(_values(record))
    if record["kind"] in {"funding", "cash_funding", "platform_funding"}:
        values["funding_kind"] = record["fact_data"]["funding_kind"]
    elif record["kind"] == "income_tax_assessment":
        values["tax_year"] = record["fact_data"]["year"]
    return SimpleNamespace(**values)


def immutable_line_fields(row, source_id, *, calculation, source_fact, relations):
    """Resolve exactly the existing immutable source used by one report line."""

    record = calculation(source_id)
    fact = source_fact(source_id)
    resolution = relations(source_id)
    fields = {
        "source_calculation_id": source_id,
        "source_fact_id": record["fact_id"],
        "source_result_digest": record["result_digest"],
        "kind": record["kind"],
        "values": _values(record),
        "fact": fact,
        "cash_source": fact,
        "relation_issues": resolution["issues"],
    }
    related = [
        relation
        for relation in resolution["line_relations"]
        if relation["line_no"] == row["line_no"] and relation["state"] == "resolved"
    ]
    funds = next((relation for relation in related if relation["role"] == "funds"), None)
    if funds is not None:
        fields["cash_source"] = source_fact(funds["source_calculation_id"])
        fields["cash_source_calculation_id"] = funds["source_calculation_id"]
        fields["cash_obligation"] = next(
            item
            for item in resolution["obligations"]
            if item["source_calculation_id"] == funds["source_calculation_id"]
            and item["key"] == funds["obligation_key"]
        )
    return fields


def compact_line_fields(fields):
    """Keep the minimum immutable inputs consumed by existing report formulas."""

    return {
        "source_calculation_id": fields["source_calculation_id"],
        "source_fact_id": fields["source_fact_id"],
        "source_result_digest": fields["source_result_digest"],
        "kind": fields["kind"],
        "values": {
            key: fields["values"][key] for key in _TAX_VALUE_FIELDS if key in fields["values"]
        },
        "fact": {
            key: getattr(fields["fact"], key)
            for key in _FACT_FIELDS
            if hasattr(fields["fact"], key)
        },
        "cash_source": {
            key: getattr(fields["cash_source"], key)
            for key in _FACT_FIELDS
            if hasattr(fields["cash_source"], key)
        },
        "cash_source_calculation_id": fields.get("cash_source_calculation_id"),
        "cash_obligation_account": fields.get("cash_obligation", {}).get("account"),
        "relation_issues": fields["relation_issues"],
    }


def expand_line_fields(compact):
    """Restore only the values the unchanged statement calculator reads."""

    result = {
        "source_calculation_id": compact["source_calculation_id"],
        "source_fact_id": compact["source_fact_id"],
        "source_result_digest": compact["source_result_digest"],
        "kind": compact["kind"],
        "values": compact["values"],
        "fact": SimpleNamespace(**compact["fact"]),
        "cash_source": SimpleNamespace(**compact["cash_source"]),
        "relation_issues": compact["relation_issues"],
    }
    if compact["cash_obligation_account"] is not None:
        result["cash_obligation"] = {"account": compact["cash_obligation_account"]}
    return result


def project_immutable_lines(rows, *, calculation, relations):
    """Prepare only profit/cash drivers from a verified voucher-line selection.

    ``calculation`` supplies the original basis, including ancestry for a
    reversed voucher; ``relations`` uses that same basis.  The caller owns
    authoritative selection and source verification before freezing the rows.
    """

    facts = {}

    def source_fact(ident):
        if ident not in facts:
            facts[ident] = report_source_fact(calculation(ident))
        return facts[ident]

    result = []
    for row in rows:
        if row["account"] not in CASH_ACCOUNTS | set(PROFIT_ACCOUNTS):
            continue
        source_id = row["basis_calculation_id"]
        fields = immutable_line_fields(
            row,
            source_id,
            calculation=calculation,
            source_fact=source_fact,
            relations=relations,
        )
        result.append(
            {
                "posting_period": row["posting_period"],
                "version_id": row["version_id"],
                "line_no": row["line_no"],
                "semantic": compact_line_fields(fields),
            }
        )
    return result


def _root(period, close_digest, rows):
    return digest(
        {
            "format": "ai-accounting-kernel/2/report-semantics/1",
            "period": period,
            "close_digest": close_digest.hex(),
            "rows": rows,
        }
    )


def _prepared_from_source(
    engine, connection, period, source_rows, close_digest, *, _verified_source=None
):
    from .query_reads import QueryReads
    from .report_projection import SOURCE_COLUMNS

    if type(close_digest) is not bytes or len(close_digest) != 32:
        raise KernelError("content_integrity_failed", "报表语义缺少有效的关账摘要")
    sources = [dict(zip(SOURCE_COLUMNS, row, strict=True)) for row in source_rows]
    if any(row["scope"] != "closed" or row["posting_period"] != period for row in sources):
        raise KernelError("content_integrity_failed", "报表语义来源不属于当前冻结月")
    selected = [row for row in sources if row["account"] in CASH_ACCOUNTS | set(PROFIT_ACCOUNTS)]
    if _verified_source is not None:
        from .verified_source_lease import require_verified_calculation_source

        verified_source = require_verified_calculation_source(connection, _verified_source)
    identifiers = {row["basis_calculation_id"] for row in selected}
    if identifiers:
        if _verified_source is None:
            reads = QueryReads(engine, connection)
            reads.prime_calculations(identifiers)
            resolutions = reads.relations_many(identifiers)
            calculation = reads.calculation
        else:
            from .query_semantics import resolve_calculation_relations

            source = verified_source
            records = {}

            def calculation(ident):
                if ident not in records:
                    row = source["calculations"].get(ident)
                    if row is None:
                        raise KernelError("content_integrity_failed", "报表语义缺少已核验核算来源")
                    publication = source["publications"].get(ident)
                    records[ident] = {
                        **row,
                        "outcome": row["decoded"],
                        "period": str(YearMonth.from_ordinal(row["period"])),
                        "posting_period": (
                            str(YearMonth.from_ordinal(publication["posting_period"]))
                            if publication is not None
                            else None
                        ),
                        "result_digest": row["digest"].hex(),
                        "fact_data": source["facts"][row["fact_id"]]["data"],
                    }
                return records[ident]

            cache = {}
            resolutions = {
                ident: resolve_calculation_relations(
                    calculation(ident),
                    load_calculation=calculation,
                    load_parents=lambda key: tuple(sorted(source["dependencies"][key])),
                    source_cache=cache,
                )
                for ident in sorted(identifiers)
            }
        projected = project_immutable_lines(
            selected,
            calculation=calculation,
            relations=resolutions.__getitem__,
        )
    else:
        projected = []
    rows = tuple(
        (
            item["posting_period"],
            item["version_id"],
            item["line_no"],
            canonical(item["semantic"]),
        )
        for item in projected
    )
    return PreparedReportSemantics(period, close_digest, rows, _root(period, close_digest, rows))


def prepare_report_semantics(engine, connection, period, source_rows, logical_close_digest):
    """Prepare a second derived root before close_storage writes its physical root.

    ``source_rows`` are the verified logical-manifest rows produced by
    ``prepare_report_projection`` in the same close transaction.
    """

    month = period if type(period) is int else YearMonth(period).ordinal
    return _prepared_from_source(engine, connection, month, source_rows, logical_close_digest)


def _close(connection, period):
    row = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
    if row is None:
        raise KernelError("frozen_snapshot_unavailable", "报表语义月份尚未关账")
    return row


def _bound_root(connection, row):
    reader = close_reader()
    header = reader.verified_header(connection, row, require_marker=True)
    root = reader.derived_root(header, "report_semantics")
    if root is None:
        raise KernelError("content_integrity_failed", "关账缺少报表语义冻结根")
    return root


def _decode(content):
    try:
        value = json.loads(content)
        if (
            type(value) is not dict
            or set(value) != _COMPACT_KEYS
            or type(value["source_calculation_id"]) is not str
            or not value["source_calculation_id"]
            or type(value["source_fact_id"]) is not str
            or not value["source_fact_id"]
            or type(value["source_result_digest"]) is not str
            or len(value["source_result_digest"]) != 64
            or bytes.fromhex(value["source_result_digest"]).hex() != value["source_result_digest"]
            or type(value["kind"]) is not str
            or not value["kind"]
            or type(value["values"]) is not dict
            or not set(value["values"]) <= set(_TAX_VALUE_FIELDS)
            or any(type(item) is not int for item in value["values"].values())
            or type(value["fact"]) is not dict
            or type(value["cash_source"]) is not dict
            or not set(value["fact"]) <= set(_FACT_FIELDS)
            or not set(value["cash_source"]) <= set(_FACT_FIELDS)
            or any(
                type(item) is not int if key == "tax_year" else type(item) is not str
                for fields in (value["fact"], value["cash_source"])
                for key, item in fields.items()
            )
            or value["cash_source_calculation_id"] is not None
            and type(value["cash_source_calculation_id"]) is not str
            or value["cash_obligation_account"] is not None
            and type(value["cash_obligation_account"]) is not str
            or type(value["relation_issues"]) is not list
            or any(type(item) is not dict for item in value["relation_issues"])
            or canonical(value) != content
        ):
            raise ValueError("invalid report semantic shape")
        return value
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise KernelError("content_integrity_failed", "报表语义行合同不一致") from exc


def _source_keys(source_rows):
    return {
        (row[2], row[3]): (row[5], row[13], row[16].hex())
        for row in source_rows
        if row[8] in CASH_ACCOUNTS | set(PROFIT_ACCOUNTS)
    }


def _check_source_keys(rows, source_rows, *, authenticated=False):
    expected = _source_keys(source_rows)
    actual = {}
    semantics = {}
    for _, version_id, line_no, content in rows:
        # A normal read has already matched the complete, ordered row payload
        # to the immutable close root. Its JSON was constructed by _decode's
        # canonical writer contract at close time, so another per-line encode
        # cannot add an integrity check. Preparation/persistence still take
        # the strict path before that root is committed.
        semantic = json.loads(content) if authenticated else _decode(content)
        semantics[version_id, line_no] = semantic
        actual[version_id, line_no] = (
            semantic["source_calculation_id"],
            semantic["source_fact_id"],
            semantic["source_result_digest"],
        )
    if actual != expected or len(actual) != len(rows):
        raise KernelError("content_integrity_failed", "报表语义与已选冻结行来源不一致")
    return semantics


def persist_report_semantics(connection, prepared):
    """Store a prepared projection only after its root is in the private close."""

    from .close_storage import derived_root, verified_header
    from .report_projection import verify_report_lines

    row = _close(connection, prepared.period)
    if row["digest"] != prepared.close_digest:
        raise KernelError("content_integrity_failed", "报表语义关账摘要不一致")
    header = verified_header(connection, row, require_marker=True)
    if (
        derived_root(header, "report_semantics") != prepared.root_digest
        or _root(prepared.period, prepared.close_digest, prepared.rows) != prepared.root_digest
    ):
        raise KernelError("content_integrity_failed", "报表语义与冻结根不一致")
    sources = verify_report_lines(connection, "closed", prepared.period)
    _check_source_keys(prepared.rows, sources)
    if connection.execute(
        "SELECT 1 FROM report_semantic_seal WHERE posting_period=?", (prepared.period,)
    ).fetchone():
        raise KernelError("content_integrity_failed", "报表语义投影已存在")
    connection.executemany("INSERT INTO report_semantic_line VALUES(?,?,?,?)", prepared.rows)
    connection.execute(
        "INSERT INTO report_semantic_seal VALUES(?,?,?,?)",
        (
            prepared.period,
            len(prepared.rows),
            prepared.close_digest,
            prepared.root_digest,
        ),
    )


def read_report_semantics(connection, period, *, include_sources=False, reads=None):
    """Read every selected closed driver against its committed root and source."""

    from .report_projection import _stored_month

    month = period if type(period) is int else YearMonth(period).ordinal
    row = _close(connection, month)
    root = _bound_root(connection, row)
    rows = tuple(
        tuple(item)
        for item in connection.execute(
            "SELECT posting_period,version_id,line_no,content FROM report_semantic_line "
            "WHERE posting_period=? ORDER BY version_id,line_no",
            (month,),
        )
    )
    seal = connection.execute(
        "SELECT row_count,close_digest,root_digest FROM report_semantic_seal "
        "WHERE posting_period=?",
        (month,),
    ).fetchone()
    if (
        seal is None
        or tuple(seal) != (len(rows), row["digest"], root)
        or _root(month, row["digest"], rows) != root
    ):
        raise KernelError("content_integrity_failed", "报表语义投影与冻结根不一致")
    # The older report root already commits the complete selected source rows.
    # Reusing its verified rows avoids another JournalIndex selection over the
    # same frozen month; the consumer still matches every displayed row below.
    cache = (
        reads._report_snapshot_cache
        if reads is not None and reads._snapshot_active and reads.connection is connection
        else None
    )
    source_key = ("report_party_month", month)
    if cache is not None and source_key in cache and "source_rows" in cache[source_key]:
        stored = cache[source_key]
    else:
        stored = _stored_month(connection, month)
        if cache is not None:
            cache[source_key] = stored
    source_rows = stored["source_rows"]
    semantics = _check_source_keys(rows, source_rows, authenticated=True)
    return (semantics, source_rows) if include_sources else semantics


def compare_report_semantics(
    engine,
    connection,
    *,
    through_period=None,
    _verified_closes=None,
    _verified_source=None,
    _verified_reports=None,
):
    """Rebuild expected derived rows from each actual logical frozen close."""

    from .report_projection import _manifest_rows, _VerifiedReports

    restriction = "" if through_period is None else " WHERE period<=?"
    params = () if through_period is None else (through_period,)
    expected_rows, expected_seals = [], []
    verified_closed = []
    periods = 0
    closes = list(
        connection.execute("SELECT * FROM period_close" + restriction + " ORDER BY period", params)
    )
    reports = None
    if _verified_reports is not None:
        from .verified_source_lease import require_verified_lease

        if (
            type(_verified_reports) is not _VerifiedReports
            or _verified_reports.connection is not connection
            or not connection.in_transaction
        ):
            raise KernelError("content_integrity_failed", "报表语义来源复用不属于同一读取事务")
        try:
            require_verified_lease(connection, _verified_reports.lease)
        except ValueError as exc:
            raise KernelError(
                "content_integrity_failed", "报表语义来源复用不属于同一读取事务"
            ) from exc
        reports = dict(_verified_reports.closes)
        if (
            len(reports) != len(_verified_reports.closes)
            or set(reports) != {row["period"] for row in closes}
            or any(
                reports[row["period"]].period != row["period"]
                or reports[row["period"]].close_digest != row["digest"]
                for row in closes
            )
        ):
            raise KernelError("content_integrity_failed", "报表语义来源复用的关账集合不一致")
    decoded = None
    if _verified_closes is not None:
        if not connection.in_transaction:
            raise KernelError("content_integrity_failed", "报表语义复用须处于同一事务")
        from .verified_close_archive import VerifiedCloseArchive

        if isinstance(_verified_closes, VerifiedCloseArchive):
            decoded = _verified_closes.lookup(connection, closes)
        else:
            decoded = {row["period"]: (row, manifest) for row, manifest in _verified_closes}
            if (
                len(decoded) != len(_verified_closes)
                or set(decoded) != {row["period"] for row in closes}
                or any(tuple(decoded[close["period"]][0]) != tuple(close) for close in closes)
            ):
                raise KernelError("content_integrity_failed", "报表语义复用的关账集合不一致")
    for close in closes:
        period = close["period"]
        reader = close_reader()
        header = reader.verified_header(connection, close, require_marker=True)
        manifest = reader.decode_close(connection, close) if decoded is None else decoded[period][1]
        # These are the original manifest-selected rows authenticated by report
        # verification in this lease, never rows from the repairable projection.
        sources = (
            _manifest_rows(connection, period, manifest)
            if reports is None
            else reports[period].rows
        )
        prepared = _prepared_from_source(
            engine,
            connection,
            period,
            sources,
            close["digest"],
            _verified_source=_verified_source,
        )
        if reader.derived_root(header, "report_semantics") != prepared.root_digest:
            raise KernelError(
                "content_integrity_failed",
                "报表语义与不可变冻结来源不一致",
                component="report_semantics",
                record_id=str(period),
                reason="frozen_root_mismatch",
            )
        verified_closed.append((period, prepared))
        expected_rows.extend(prepared.rows)
        expected_seals.append((period, len(prepared.rows), close["digest"], prepared.root_digest))
        periods += 1
    suffix = "" if through_period is None else " WHERE posting_period<=?"
    actual_rows = [
        tuple(row)
        for row in connection.execute(
            "SELECT posting_period,version_id,line_no,content FROM report_semantic_line"
            + suffix
            + " ORDER BY posting_period,version_id,line_no",
            params,
        )
    ]
    actual_seals = [
        tuple(row)
        for row in connection.execute(
            "SELECT posting_period,row_count,close_digest,root_digest "
            "FROM report_semantic_seal" + suffix + " ORDER BY posting_period",
            params,
        )
    ]
    return {
        "changed": actual_rows != expected_rows or actual_seals != expected_seals,
        "expected_rows": expected_rows,
        "expected_seals": expected_seals,
        "rows": len(expected_rows),
        "periods": periods,
        "_verified_closed": tuple(verified_closed),
    }


def require_report_semantics(
    engine,
    connection,
    *,
    through_period=None,
    _verified_closes=None,
    _verified_source=None,
    _verified_reports=None,
    _return_verified=False,
):
    compared = compare_report_semantics(
        engine,
        connection,
        through_period=through_period,
        _verified_closes=_verified_closes,
        _verified_source=_verified_source,
        _verified_reports=_verified_reports,
    )
    if compared["changed"]:
        raise KernelError(
            "content_integrity_failed",
            "报表语义投影与已核验冻结来源不一致，需要显式维修",
            component="report_semantics",
            record_id="*",
            reason="projection_mismatch",
        )
    if _return_verified:
        from .verified_source_lease import current_verified_lease

        return _VerifiedSemantics(
            connection, compared["_verified_closed"], current_verified_lease(connection)
        )
    return {"rows": compared["rows"], "periods": compared["periods"]}


def repair_report_semantics(engine, connection, *, fault=None):
    """Rebuild only mutable derived rows inside the caller's repair transaction."""

    if not connection.in_transaction:
        raise ValueError("report semantics repair requires a write transaction")
    compared = compare_report_semantics(engine, connection)
    if compared["changed"]:
        connection.execute("DELETE FROM report_semantic_line")
        connection.execute("DELETE FROM report_semantic_seal")
        if fault:
            fault("report_semantics_cleared", connection)
        connection.executemany(
            "INSERT INTO report_semantic_line VALUES(?,?,?,?)", compared["expected_rows"]
        )
        connection.executemany(
            "INSERT INTO report_semantic_seal VALUES(?,?,?,?)", compared["expected_seals"]
        )
        if fault:
            fault("report_semantics_rebuilt", connection)
    require_report_semantics(engine, connection)
    return {
        "changed": compared["changed"],
        "rows": compared["rows"],
        "periods": compared["periods"],
    }
