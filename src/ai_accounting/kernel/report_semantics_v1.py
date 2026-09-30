"""Released v1 report semantic source and derived-root verification rules.

Management classifications remain a separate, version-selected read.  This
module stores only the calculation and relation fields used by report formulas;
it never derives statement amounts or freezes a management classification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

from .close_storage_v1 import decode_close, derived_root, verified_header
from .content_v1 import _V1YearMonth as YearMonth
from .contracts import KernelError
from .history_encoding_v1 import canonical, digest
from .query_relations_v1 import resolve_calculation_relations
from .report_projection_v1 import SOURCE_COLUMNS, _manifest_rows, verify_report_lines

CASH_ACCOUNTS = frozenset({"1001", "1002", "1012"})
PROFIT_ACCOUNTS = frozenset(
    {
        "5001",
        "5111",
        "5401",
        "540101",
        "540104",
        "540102",
        "540103",
        "5403",
        "5601",
        "560101",
        "560102",
        "560103",
        "560104",
        "5602",
        "560201",
        "560202",
        "560203",
        "560204",
        "5603",
        "560301",
        "6301",
        "630101",
        "571101",
        "571102",
        "571103",
        "571104",
        "5801",
    }
)

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
    from .content_v1 import _V1YearMonth
    from .history_reads_v1 import V1Reads as QueryReads

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
            resolutions = reads.relations_many(identifiers, resolver=resolve_calculation_relations)
            calculation = reads.calculation
        else:
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
                        "period": str(_V1YearMonth.from_ordinal(row["period"])),
                        "posting_period": (
                            str(_V1YearMonth.from_ordinal(publication["posting_period"]))
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
    header = verified_header(connection, row, require_marker=True)
    root = derived_root(header, "report_semantics")
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


def _check_source_keys(rows, source_rows):
    expected = _source_keys(source_rows)
    actual = {}
    for _, version_id, line_no, content in rows:
        semantic = _decode(content)
        actual[version_id, line_no] = (
            semantic["source_calculation_id"],
            semantic["source_fact_id"],
            semantic["source_result_digest"],
        )
    if actual != expected or len(actual) != len(rows):
        raise KernelError("content_integrity_failed", "报表语义与已选冻结行来源不一致")


def persist_report_semantics(connection, prepared):
    """Store a prepared projection only after its root is in the private close."""

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


def read_report_semantics(connection, period, *, source_rows=None):
    """Read every selected closed driver against its committed root and source."""

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
    if source_rows is None:
        source_rows = verify_report_lines(connection, "closed", month)
    _check_source_keys(rows, source_rows)
    return {(version_id, line_no): _decode(content) for _, version_id, line_no, content in rows}


def compare_report_semantics(
    engine, connection, *, through_period=None, _verified_closes=None, _verified_source=None
):
    """Rebuild expected derived rows from each actual logical frozen close."""

    restriction = "" if through_period is None else " WHERE period<=?"
    params = () if through_period is None else (through_period,)
    expected_rows, expected_seals = [], []
    verified_closed = []
    periods = 0
    closes = list(
        connection.execute("SELECT * FROM period_close" + restriction + " ORDER BY period", params)
    )
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
        header = verified_header(connection, close, require_marker=True)
        manifest = decode_close(connection, close) if decoded is None else decoded[period][1]
        sources = _manifest_rows(connection, period, manifest)
        prepared = _prepared_from_source(
            engine,
            connection,
            period,
            sources,
            close["digest"],
            _verified_source=_verified_source,
        )
        if derived_root(header, "report_semantics") != prepared.root_digest:
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
    _return_verified=False,
):
    compared = compare_report_semantics(
        engine,
        connection,
        through_period=through_period,
        _verified_closes=_verified_closes,
        _verified_source=_verified_source,
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
