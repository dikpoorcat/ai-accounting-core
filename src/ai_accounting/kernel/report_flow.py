"""Root-bound monthly report contributions, derived from the normal row calculator.

This is a repairable read projection. The private close root, not this table,
commits the result. Full comparison rebuilds from the close's original sources.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .account_definitions import CASH_ACCOUNTS, PROFIT_ACCOUNTS, RECLASS
from .content_history_context import close_reader
from .contracts import KernelError
from .types import canonical, digest

REPORT_FLOW_DDL = """
CREATE TABLE report_period_flow(
 posting_period INTEGER PRIMARY KEY REFERENCES period_close(period),
 content TEXT NOT NULL CHECK(json_valid(content)),
 close_digest BLOB NOT NULL CHECK(length(close_digest)=32),
 root_digest BLOB NOT NULL CHECK(length(root_digest)=32)
) STRICT;
"""


@dataclass(frozen=True)
class PreparedReportFlow:
    period: int
    close_digest: bytes
    content: str
    root_digest: bytes


def _classification_refs(connection, period, source_vouchers, *, closed):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_report_classification'"
    ).fetchone():
        return ()
    if closed:
        # Drive the two exact candidate ranges independently. The voucher
        # branch uses the typed index, while the current-month branch uses
        # fact_period; neither scans all historic classifications. The reverse
        # reference still proves each frozen adoption.
        return tuple(
            (row[0], row[1].hex())
            for row in connection.execute(
                "WITH candidate AS MATERIALIZED ("
                "SELECT f.id,f.digest FROM json_each(?) ids "
                "CROSS JOIN fact_report_classification c "
                "INDEXED BY report_classification_voucher_revision "
                "ON c.voucher_version_id=ids.value "
                "CROSS JOIN fact_revision f ON f.id=c.revision_id "
                "CROSS JOIN subject s ON s.id=f.subject_id "
                "WHERE s.kind='report_classification' UNION "
                "SELECT f.id,f.digest FROM fact_revision f INDEXED BY fact_period "
                "CROSS JOIN fact_report_classification c ON c.revision_id=f.id "
                "CROSS JOIN subject s ON s.id=f.subject_id "
                "WHERE f.period=? AND s.kind='report_classification'"
                ") SELECT candidate.id,candidate.digest FROM candidate "
                "WHERE EXISTS(SELECT 1 FROM close_reference r INDEXED BY close_reference_lookup "
                "WHERE r.reference_type='fact' AND r.reference_id=candidate.id "
                "AND r.close_period<=? AND r.path='readiness.financial_reports.facts[*]') "
                "ORDER BY candidate.id",
                (canonical(sorted(source_vouchers)), period, period),
            )
        )
    return tuple(
        (row[0], row[1].hex())
        for row in connection.execute(
            "WITH candidate AS MATERIALIZED ("
            "SELECT f.id,f.digest,f.period FROM json_each(?) ids "
            "CROSS JOIN fact_report_classification c "
            "INDEXED BY report_classification_voucher_revision "
            "ON c.voucher_version_id=ids.value "
            "CROSS JOIN fact_revision f ON f.id=c.revision_id "
            "CROSS JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind='report_classification' UNION "
            "SELECT f.id,f.digest,f.period FROM fact_revision f INDEXED BY fact_period "
            "CROSS JOIN fact_report_classification c ON c.revision_id=f.id "
            "CROSS JOIN subject s ON s.id=f.subject_id "
            "WHERE f.period=? AND s.kind='report_classification'"
            ") SELECT candidate.id,candidate.digest FROM candidate WHERE "
            "EXISTS(SELECT 1 FROM close_reference r INDEXED BY close_reference_lookup "
            "WHERE r.reference_type='fact' AND r.reference_id=candidate.id "
            "AND r.close_period<=? AND r.path='readiness.financial_reports.facts[*]') "
            "OR (candidate.period<=? AND NOT EXISTS(SELECT 1 FROM period_close p "
            "WHERE p.period=candidate.period) AND EXISTS(SELECT 1 FROM fact_current h "
            "WHERE h.fact_id=candidate.id)) ORDER BY candidate.id",
            (canonical(sorted(source_vouchers)), period, period, period),
        )
    )


def _root(period, close_digest, source_root, semantic_root, content):
    return digest(
        {
            "format": "ai-accounting-kernel/2/report-period-flow/1",
            "period": period,
            "close_digest": close_digest.hex(),
            "source_root": source_root.hex(),
            "semantic_root": semantic_root.hex(),
            "content": content,
        }
    )


def _refs_from_ids(connection, period, identifiers, source_vouchers):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fact_report_classification'"
    ).fetchone():
        return ()
    return tuple(
        (row[0], row[1].hex())
        for row in connection.execute(
            "SELECT DISTINCT f.id,f.digest FROM json_each(?) ids "
            "CROSS JOIN fact_revision f ON f.id=ids.value "
            "CROSS JOIN subject s ON s.id=f.subject_id "
            "CROSS JOIN fact_report_classification c ON c.revision_id=f.id "
            "WHERE s.kind='report_classification' AND "
            "(c.voucher_version_id IN (SELECT value FROM json_each(?)) OR f.period=?) "
            "ORDER BY f.id",
            (canonical(sorted(identifiers)), canonical(sorted(source_vouchers)), period),
        )
    )


def _content(
    engine,
    connection,
    period,
    source_rows,
    semantic_rows,
    prepared_report,
    *,
    classification_refs=None,
):
    from .query_reads import QueryReads
    from .report_projection import SOURCE_COLUMNS
    from .report_semantics import expand_line_fields
    from .reports import _cash_rows, _profit_rows, issue

    semantics = {(ident, line): json.loads(body) for _, ident, line, body in semantic_rows}
    selected = [
        dict(zip(SOURCE_COLUMNS, source, strict=True))
        for source in source_rows
        if source[8] in CASH_ACCOUNTS | set(PROFIT_ACCOUNTS)
    ]
    reads = QueryReads(engine, connection)
    problems = []
    selected_vouchers = {row["reverses_id"] or row["version_id"] for row in selected}
    refs = (
        _classification_refs(connection, period, selected_vouchers, closed=False)
        if classification_refs is None
        else classification_refs
    )
    reference_ids = [ident for ident, _ in refs]
    versions = reads.fact_versions(reference_ids)
    classifications = {}
    conflicts = set()
    for ident in sorted(reference_ids):
        fact = versions[ident].fact
        target = fact.voucher_version_id
        if target in classifications or target in conflicts:
            problems.append(
                issue(
                    "report_classification",
                    "同一凭证版本存在多个分类来源",
                    voucher_version_id=target,
                )
            )
            classifications.pop(target, None)
            conflicts.add(target)
            continue
        voucher = connection.execute(
            "SELECT period FROM voucher_version WHERE id=?", (target,)
        ).fetchone()
        if target not in selected_vouchers:
            if voucher is None or voucher["period"] != fact.period.ordinal:
                problems.append(
                    issue(
                        "report_classification.voucher_version_id",
                        "分类必须引用本月已经存在的凭证版本",
                        voucher_version_id=target,
                    )
                )
            continue
        if voucher is None or voucher["period"] != fact.period.ordinal:
            problems.append(
                issue(
                    "report_classification.period",
                    "分类月份必须等于原凭证记账月份",
                    voucher_version_id=target,
                )
            )
        for details, accounts in (
            (fact.profit_details, {"5403", "5601", "5602", "5603"}),
            (fact.counterparties, RECLASS),
            (fact.cash_details, CASH_ACCOUNTS),
        ):
            for detail in details:
                line = connection.execute(
                    "SELECT account FROM voucher_line WHERE version_id=? AND line_no=?",
                    (target, detail.line_no),
                ).fetchone()
                if line is None or line["account"] not in accounts:
                    problems.append(
                        issue(
                            "report_classification.line_no",
                            "分类引用不存在的凭证行"
                            if line is None
                            else "该凭证行不接受此类报表分类",
                            voucher_version_id=target,
                        )
                    )
        classifications[target] = fact
    rows = []
    related = set()
    for source in selected:
        key = source["version_id"], source["line_no"]
        compact = semantics.get(key)
        if compact is None:
            raise KernelError("content_integrity_failed", "报表月度汇总缺少冻结语义行")
        row = {
            "period": period,
            "version_id": source["version_id"],
            "line_no": source["line_no"],
            "account": source["account"],
            "amount": source["debit"] - source["credit"],
            "reverses_id": source["reverses_id"],
            "cashflow": source["cashflow"],
            "kind": source["kind"],
            "classification": classifications.get(source["reverses_id"] or source["version_id"]),
        }
        row.update(expand_line_fields(compact))
        source_id = compact["source_calculation_id"]
        if source_id not in related:
            problems.extend(compact["relation_issues"])
            related.add(source_id)
        rows.append(row)
    if len(rows) != len(semantics):
        raise KernelError("content_integrity_failed", "报表月度汇总存在额外冻结语义行")
    profit = _profit_rows(rows, period, period, problems)
    cash = _cash_rows(rows, period, period, problems)
    assessed = {}
    for row in rows:
        if row["account"] == "5801":
            tax_year = getattr(row["fact"], "tax_year", period // 12 + 1)
            assessed[str(tax_year)] = assessed.get(str(tax_year), 0) + row["amount"]
    return canonical(
        {
            "classification_refs": refs,
            "source_vouchers": sorted(selected_vouchers),
            "profit": {str(key): amount for key, amount in profit.items()},
            "cash": {str(key): amount for key, amount in cash.items() if key not in (21, 22)},
            "assessed": assessed,
            "issues": list({canonical(item): item for item in problems}.values()),
            "party_rows": prepared_report.party_rows,
            "party_usable": prepared_report.party_usable,
            "checkpoint_rows": prepared_report.checkpoint_rows,
            "checkpoint_usable": prepared_report.checkpoint_usable,
            "checkpoint_inputs": prepared_report.checkpoint_inputs,
        }
    )


def prepare_report_flow(engine, connection, period, prepared_report, prepared_semantics):
    """Prepare in the close transaction, before the private root is written."""

    if prepared_report.period != period or prepared_semantics.period != period:
        raise KernelError("content_integrity_failed", "报表月度汇总月份不一致")
    if prepared_report.close_digest != prepared_semantics.close_digest:
        raise KernelError("content_integrity_failed", "报表月度汇总关账摘要不一致")
    content = _content(
        engine,
        connection,
        period,
        prepared_report.rows,
        prepared_semantics.rows,
        prepared_report,
    )
    root = _root(
        period,
        prepared_report.close_digest,
        prepared_report.root_digest,
        prepared_semantics.root_digest,
        content,
    )
    return PreparedReportFlow(period, prepared_report.close_digest, content, root)


def persist_report_flow(connection, prepared):
    """Persist only after close_storage has bound the prepared digest."""

    close = connection.execute(
        "SELECT * FROM period_close WHERE period=?", (prepared.period,)
    ).fetchone()
    if close is None or close["digest"] != prepared.close_digest:
        raise KernelError("content_integrity_failed", "报表月度汇总缺少匹配的关账")
    reader = close_reader()
    header = reader.verified_header(connection, close, require_marker=True)
    if reader.derived_root(header, "report_flow") != prepared.root_digest:
        raise KernelError("content_integrity_failed", "报表月度汇总未绑定不可变关账根")
    connection.execute(
        "INSERT INTO report_period_flow VALUES(?,?,?,?)",
        (prepared.period, prepared.content, prepared.close_digest, prepared.root_digest),
    )


def read_report_flow(connection, period, *, reads=None):
    """Return authenticated monthly data, or None if classification selection changed."""

    cache = (
        reads._report_snapshot_cache
        if reads is not None and reads._snapshot_active and reads.connection is connection
        else None
    )
    cache_key = ("report_period_flow", period)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    close = connection.execute("SELECT * FROM period_close WHERE period=?", (period,)).fetchone()
    if close is None:
        raise KernelError("frozen_snapshot_unavailable", "报表月度汇总月份尚未关账")
    reader = close_reader()
    header = reader.verified_header(connection, close, require_marker=True)
    bound = reader.derived_root(header, "report_flow")
    source_root = reader.derived_root(header, "report")
    semantic_root = reader.derived_root(header, "report_semantics")
    stored = connection.execute(
        "SELECT content,close_digest,root_digest FROM report_period_flow WHERE posting_period=?",
        (period,),
    ).fetchone()
    if (
        bound is None
        or source_root is None
        or semantic_root is None
        or stored is None
        or stored["close_digest"] != close["digest"]
        or stored["root_digest"] != bound
        or _root(period, close["digest"], source_root, semantic_root, stored["content"]) != bound
    ):
        raise KernelError("content_integrity_failed", "报表月度汇总与冻结来源不一致")
    content = json.loads(stored["content"])
    if canonical(content) != stored["content"]:
        raise KernelError("content_integrity_failed", "报表月度汇总内容编码不一致")
    expected_refs = tuple(tuple(item) for item in content["classification_refs"])
    # The authenticated month root gives the exact adopted reference IDs.
    # Check every named immutable header and typed voucher binding, including
    # facts that belong to this month but not to a source voucher. Reading the
    # cumulative readiness directory here would materialize all prior years'
    # classifications once for each month in a report.
    identifiers = [ident for ident, _ in expected_refs]
    selected = list(connection.execute(
        "SELECT f.id,f.digest,f.period,s.kind,c.voucher_version_id "
        "FROM json_each(?) ids LEFT JOIN fact_revision f ON f.id=ids.value "
        "LEFT JOIN subject s ON s.id=f.subject_id "
        "LEFT JOIN fact_report_classification c ON c.revision_id=f.id "
        "ORDER BY ids.value",
        (canonical(identifiers),),
    ))
    if len(selected) != len(expected_refs) or any(
        row["id"] is None
        or row["kind"] != "report_classification"
        or row["voucher_version_id"] is None
        for row in selected
    ):
        raise KernelError("content_integrity_failed", "冻结报表分类来源缺失")
    vouchers = set(content["source_vouchers"])
    actual_refs = tuple(
        (row["id"], row["digest"].hex())
        for row in selected
        if row["period"] == period or row["voucher_version_id"] in vouchers
    )
    if expected_refs != actual_refs:
        actual_digests = dict(actual_refs)
        if any(
            ident in actual_digests and actual_digests[ident] != expected_digest
            for ident, expected_digest in expected_refs
        ):
            raise KernelError("content_integrity_failed", "冻结报表分类来源摘要不一致")
        return None
    content["root"] = source_root
    if cache is not None:
        cache[cache_key] = content
    return content


def compare_report_flow(
    engine,
    connection,
    *,
    through_period=None,
    _verified_closes=None,
    _verified_reports=None,
    _verified_semantics=None,
):
    """Rebuild from original close sources, never from the repairable flow row."""

    from .report_projection import (
        PreparedReport,
        _expected_checkpoint,
        _manifest_rows,
        _party_delta,
        _report_root,
        _VerifiedReports,
    )
    from .report_semantics import _prepared_from_source, _VerifiedSemantics

    restriction = "" if through_period is None else " WHERE period<=?"
    params = () if through_period is None else (through_period,)
    closes = list(connection.execute("SELECT * FROM period_close" + restriction, params))
    if (_verified_reports is None) != (_verified_semantics is None):
        raise KernelError("content_integrity_failed", "报表月度汇总缺少配对的已核验来源")
    reused = _verified_reports is not None
    if reused:
        from .verified_source_lease import require_verified_lease

        if (
            type(_verified_reports) is not _VerifiedReports
            or type(_verified_semantics) is not _VerifiedSemantics
            or _verified_reports.connection is not connection
            or _verified_semantics.connection is not connection
            or not connection.in_transaction
        ):
            raise KernelError("content_integrity_failed", "报表月度汇总复用不属于同一读取事务")
        try:
            require_verified_lease(connection, _verified_reports.lease)
            require_verified_lease(connection, _verified_semantics.lease)
        except ValueError as exc:
            raise KernelError(
                "content_integrity_failed", "报表月度汇总复用不属于同一读取事务"
            ) from exc
        reports = dict(_verified_reports.closes)
        semantics = dict(_verified_semantics.closes)
        selected_periods = {row["period"] for row in closes}
        if (
            len(reports) != len(_verified_reports.closes)
            or len(semantics) != len(_verified_semantics.closes)
            or set(reports) != selected_periods
            or set(semantics) != selected_periods
            or any(
                reports[row["period"]].period != row["period"]
                or semantics[row["period"]].period != row["period"]
                or reports[row["period"]].close_digest != row["digest"]
                or semantics[row["period"]].close_digest != row["digest"]
                for row in closes
            )
        ):
            raise KernelError("content_integrity_failed", "报表月度汇总复用关账集合不一致")
    decoded = None
    if _verified_closes is not None:
        if not connection.in_transaction:
            raise KernelError("content_integrity_failed", "报表月度汇总复用须处于同一事务")
        from .verified_close_archive import VerifiedCloseArchive

        if isinstance(_verified_closes, VerifiedCloseArchive):
            decoded = _verified_closes.lookup(connection, closes)
        else:
            decoded = {row["period"]: (row, manifest) for row, manifest in _verified_closes}
            if (
                len(decoded) != len(_verified_closes)
                or set(decoded) != {row["period"] for row in closes}
                or any(tuple(decoded[row["period"]][0]) != tuple(row) for row in closes)
            ):
                raise KernelError("content_integrity_failed", "报表月度汇总复用关账集合不一致")
    expected = []
    expected_months = {}
    frozen_classification_refs = set()
    for close in sorted(closes, key=lambda item: item["period"]):
        period = close["period"]
        reader = close_reader()
        header = reader.verified_header(connection, close, require_marker=True)
        manifest = reader.decode_close(connection, close) if decoded is None else decoded[period][1]
        frozen_classification_refs.update(
            manifest["readiness"].get("financial_reports", {}).get("facts", ())
        )
        if reused:
            prepared_report = reports[period]
            prepared_semantics = semantics[period]
            source_rows = prepared_report.rows
        else:
            source_rows = _manifest_rows(connection, period, manifest)
            party_rows, party_usable = _party_delta(
                engine,
                connection,
                period,
                source_rows,
                source="closed",
                classification_refs=frozen_classification_refs,
            )
            checkpoint_rows, checkpoint_usable, checkpoint_inputs = _expected_checkpoint(
                connection, period, party_rows, party_usable, expected_months
            )
            prepared_report = PreparedReport(
                period,
                close["digest"],
                source_rows,
                party_rows,
                party_usable,
                checkpoint_rows,
                checkpoint_usable,
                checkpoint_inputs,
                _report_root(
                    period,
                    close["digest"],
                    source_rows,
                    party_rows,
                    party_usable,
                    checkpoint_rows,
                    checkpoint_usable,
                    checkpoint_inputs,
                ),
            )
            prepared_semantics = _prepared_from_source(
                engine, connection, period, source_rows, close["digest"]
            )
        source_vouchers = {
            row[7] or row[2]
            for row in source_rows
            if row[8] in CASH_ACCOUNTS | set(PROFIT_ACCOUNTS)
        }
        refs = _refs_from_ids(connection, period, frozen_classification_refs, source_vouchers)
        content = _content(
            engine,
            connection,
            period,
            source_rows,
            prepared_semantics.rows,
            prepared_report,
            classification_refs=refs,
        )
        root = _root(
            period,
            close["digest"],
            prepared_report.root_digest,
            prepared_semantics.root_digest,
            content,
        )
        if reader.derived_root(header, "report_flow") != root:
            raise KernelError("content_integrity_failed", "报表月度汇总与权威来源不一致")
        if (
            reader.derived_root(header, "report") != prepared_report.root_digest
            or reader.derived_root(header, "report_semantics") != prepared_semantics.root_digest
        ):
            raise KernelError("content_integrity_failed", "报表月度汇总复用的来源根不一致")
        expected.append((period, content, close["digest"], root))
        expected_months[period] = {
            "root": prepared_report.root_digest,
            "party_rows": prepared_report.party_rows,
            "party_usable": prepared_report.party_usable,
            "checkpoint_rows": prepared_report.checkpoint_rows,
            "checkpoint_usable": prepared_report.checkpoint_usable,
        }
    suffix = "" if through_period is None else " WHERE posting_period<=?"
    actual = [
        tuple(row)
        for row in connection.execute(
            "SELECT posting_period,content,close_digest,root_digest FROM report_period_flow"
            + suffix
            + " ORDER BY posting_period",
            params,
        )
    ]
    return {"changed": actual != expected, "expected_rows": expected, "rows": len(expected)}


def require_report_flow(
    engine,
    connection,
    *,
    through_period=None,
    _verified_closes=None,
    _verified_reports=None,
    _verified_semantics=None,
):
    result = compare_report_flow(
        engine,
        connection,
        through_period=through_period,
        _verified_closes=_verified_closes,
        _verified_reports=_verified_reports,
        _verified_semantics=_verified_semantics,
    )
    if result["changed"]:
        raise KernelError("content_integrity_failed", "报表月度汇总投影与权威来源不一致")
    return result


def repair_report_flow(engine, connection):
    result = compare_report_flow(engine, connection)
    if result["changed"]:
        connection.execute("DELETE FROM report_period_flow")
        connection.executemany(
            "INSERT INTO report_period_flow VALUES(?,?,?,?)", result["expected_rows"]
        )
    return result
