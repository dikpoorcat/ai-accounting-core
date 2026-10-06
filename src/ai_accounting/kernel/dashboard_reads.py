"""SQL ranges and lazy display records for the existing dashboard projection.

The business selector owns version choice. These helpers only restrict a selected
relation and delay expensive presentation fields until a consumer needs them.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from itertools import islice

from .query_reads import _owns_current_selector_snapshot, selected_voucher_sql
from .types import YearMonth, canonical, checked


def page_keys(keys, after=None, limit=100, *, total_count=None):
    """Choose stable presentation keys before expanding their facts or sources."""
    from .contracts import KernelError

    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("每页数量必须为 1 至 500")
    ordered = list(keys)
    start = 0
    if after is not None:
        try:
            start = ordered.index(after) + 1
        except ValueError as exc:
            raise KernelError(
                "dashboard_snapshot_changed", "分页位置已变化，请重新加载明细。"
            ) from exc
    picked = ordered[start : start + limit]
    more = start + len(picked) < len(ordered)
    return picked, {
        "total_count": len(ordered) if total_count is None else total_count,
        "filtered_count": len(ordered),
        "returned_count": len(picked),
        "has_more": more,
        "next_cursor": picked[-1] if more else None,
    }


def scalar_facts(snapshot, calculations):
    """Read normalized scalar columns without loading any growing child collection."""
    from .schema import table_name
    from .storage import decode_fields

    by_kind = {}
    for calc in calculations:
        by_kind.setdefault(calc["kind"], set()).add(calc["fact_id"])
    result = {}
    for kind, identifiers in by_kind.items():
        for row in snapshot.connection.execute(
            f"SELECT f.* FROM {table_name(kind)} f JOIN json_each(?) ids "
            "ON f.revision_id=ids.value",
            (canonical(sorted(identifiers)),),
        ):
            result[row["revision_id"]] = decode_fields(
                snapshot.store.registry.models[kind], dict(row)
            )
    return result


def verified_scalar_facts(snapshot, calculations):
    """Verify every candidate source before its scalar columns define a page scope."""
    from .integrity import verify_sources

    calculations = list(calculations)
    if not calculations:
        return {}
    # Authenticate the exact header before its kind chooses a typed fact table.
    # An owned snapshot's successful full content proof already checked these
    # bindings; otherwise this reads headers and seals, never result bodies.
    pending = {
        calc["id"]
        for calc in calculations
        if not snapshot.reads._snapshot_active
        or calc["id"] not in snapshot.reads._verified_source_contents
    }
    headers = (
        {
            row["id"]: row
            for row in snapshot.connection.execute(
                "SELECT c.id,c.subject_id,c.kind,c.period,c.fact_id,"
                "f.subject_id fact_subject,f.period fact_period,s.kind fact_kind,"
                "EXISTS(SELECT 1 FROM calculation_seal z WHERE z.calculation_id=c.id) cs,"
                "EXISTS(SELECT 1 FROM fact_seal z WHERE z.fact_id=c.fact_id) fs "
                "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                "LEFT JOIN fact_revision f ON f.id=c.fact_id "
                "LEFT JOIN subject s ON s.id=f.subject_id",
                (canonical(sorted(pending)),),
            )
        }
        if pending
        else {}
    )
    from .contracts import KernelError

    if headers.keys() != pending:
        raise KernelError("content_integrity_failed", "本次读取的业务来源缺失")
    for calc in calculations:
        if calc["id"] not in pending:
            continue
        row = headers[calc["id"]]
        if (
            not row["cs"]
            or not row["fs"]
            or (row["subject_id"], row["kind"], row["period"])
            != (row["fact_subject"], row["fact_kind"], row["fact_period"])
            or any(row[field] != calc[field] for field in ("subject_id", "kind", "fact_id"))
        ):
            raise KernelError("content_integrity_failed", "业务核算结果与原事实身份不匹配")
    verify_sources(
        snapshot.engine,
        snapshot.connection,
        fact_ids={calc["fact_id"] for calc in calculations},
    )
    return scalar_facts(snapshot, calculations)


def _adopted_heads_sql(snapshot, kinds, *, posting_period=None, subjects=None):
    """Locate possible heads before missing mutable rows can hide required sources.

    Frozen references and current rows are candidates, never adoption authority.
    Consumers still authenticate the selected header/publication/close leaf.
    A verified withdrawal is an absence marker, not a calculation with NULL ID.
    """
    selected_month = YearMonth(posting_period).ordinal if posting_period else None
    periods = [
        row[0]
        for row in snapshot.connection.execute(
            "SELECT period FROM period_close WHERE period<=?"
            + (" AND period=?" if selected_month is not None else "")
            + " ORDER BY period",
            (snapshot.month, selected_month) if selected_month is not None else (snapshot.month,),
        )
    ]
    closes = snapshot.reads.authoritative_close_rows(periods=periods, through_period=snapshot.month)
    from .contracts import KernelError

    limits = []
    for row in closes:
        highwater = snapshot.reads.close_header(row).root["small"]["publication_sequence"]
        if type(row["period"]) is not int or type(highwater) is not int:
            raise KernelError("content_integrity_failed", "已冻结的发布序号格式错误")
        limits.append([row["period"], highwater])
    subject_locator = (
        "SELECT s.id FROM requested k CROSS JOIN subject s INDEXED BY subject_kind "
        "ON s.kind=k.kind UNION SELECT c.subject_id FROM requested k "
        "CROSS JOIN calculation c INDEXED BY calculation_kind_period ON c.kind=k.kind"
        if subjects is None else "SELECT value FROM json_each(?)"
    )
    query = (
        "WITH RECURSIVE requested(kind) AS (SELECT value FROM json_each(?)), "
        "limits(period,highwater) AS MATERIALIZED ("
        "SELECT CAST(json_extract(value,'$[0]') AS INTEGER),"
        "CAST(json_extract(value,'$[1]') AS INTEGER) FROM json_each(?)), "
        "selection(month,posting_period) AS (SELECT ?,?), "
        # Both identity lanes are locators. A missing subject or calculation
        # must not remove the surviving source's candidate from this scope.
        "scoped_subjects(subject_id) AS MATERIALIZED ("
        + subject_locator + "), "
        # Enumerate real lookup prefixes by seeks, including damaged types.
        # Hard-coding valid types would silently omit a malformed exact hit.
        "reference_types(value) AS (SELECT min(reference_type) FROM close_reference "
        "UNION ALL SELECT (SELECT min(reference_type) FROM close_reference "
        "WHERE reference_type>reference_types.value) FROM reference_types "
        "WHERE reference_types.value IS NOT NULL), "
        "frozen_locators AS MATERIALIZED ("
        "SELECT r.close_period,r.position,r.reference_id calculation_id,a.reference_id fact_id,"
        "l.highwater "
        "FROM scoped_subjects ss CROSS JOIN fact_revision f ON f.subject_id=ss.subject_id "
        "CROSS JOIN reference_types t "
        "CROSS JOIN close_reference a INDEXED BY close_reference_direct_adoption "
        "ON a.reference_type=t.value AND a.reference_id=f.id "
        "JOIN limits l ON l.period=a.close_period "
        "JOIN close_reference r ON r.close_period=a.close_period AND r.position=a.position "
        "AND r.path='adopted_results[*].calculation_id' "
        "WHERE a.path='adopted_results[*].fact_id' UNION "
        "SELECT r.close_period,r.position,r.reference_id,a.reference_id,l.highwater "
        "FROM scoped_subjects ss "
        "CROSS JOIN calculation c INDEXED BY calculation_subject ON c.subject_id=ss.subject_id "
        "CROSS JOIN reference_types t "
        "CROSS JOIN close_reference r INDEXED BY close_reference_direct_adoption "
        "ON r.reference_type=t.value AND r.reference_id=c.id "
        "JOIN limits l ON l.period=r.close_period "
        "JOIN close_reference a ON a.close_period=r.close_period AND a.position=r.position "
        "AND a.path='adopted_results[*].fact_id' "
        "WHERE r.path='adopted_results[*].calculation_id'), "
        "eligible AS (SELECT p.subject_id,p.calculation_id,p.posting_period,p.sequence,"
        "coalesce(c.fact_id,f.id) fact_id,coalesce(c.kind,s.kind) kind,"
        "coalesce(c.period,f.period) period,p.mode,p.id publication_id,"
        "0 locator_priority,0 current_missing "
        "FROM scoped_subjects ss "
        "CROSS JOIN calculation_publication p INDEXED BY publication_subject "
        "ON p.subject_id=ss.subject_id LEFT JOIN subject s ON s.id=p.subject_id "
        "LEFT JOIN calculation c ON c.id=p.calculation_id "
        "LEFT JOIN fact_current fc ON fc.subject_id=p.subject_id "
        "LEFT JOIN fact_revision f ON f.id=fc.fact_id "
        "LEFT JOIN limits l ON l.period=p.posting_period "
        "LEFT JOIN calculation_current cc ON cc.subject_id=p.subject_id "
        "CROSS JOIN selection q WHERE p.posting_period<=q.month "
        "AND (q.posting_period IS NULL OR p.posting_period=q.posting_period) "
        # The frozen locator already supplies this same physical publication
        # source. Keep its independent candidate, not a second window row.
        # Header/publication/leaf proof still consumes the real source below.
        "AND NOT EXISTS(SELECT 1 FROM frozen_locators x "
        "WHERE x.calculation_id=p.calculation_id AND x.close_period=p.posting_period) "
        "AND ((l.period IS NOT NULL AND p.sequence<=l.highwater "
        "AND NOT EXISTS(SELECT 1 FROM calculation_publication later "
        "WHERE later.subject_id=p.subject_id AND later.posting_period=p.posting_period "
        "AND later.sequence>p.sequence AND later.sequence<=l.highwater)) "
        "OR (l.period IS NULL AND cc.calculation_id=p.calculation_id)) UNION ALL "
        # Frozen calculation/fact references keep a source visible when its
        # publication or calculation is missing. These are only locators;
        # their actual source and independently frozen leaf must still agree.
        "SELECT coalesce(f.subject_id,c.subject_id,p.subject_id),r.calculation_id,r.close_period,"
        "coalesce(p.sequence,r.highwater),r.fact_id,coalesce(s.kind,c.kind,ps.kind),"
        "coalesce(f.period,c.period),'frozen',p.id,1,0 "
        "FROM frozen_locators r "
        "LEFT JOIN calculation c ON c.id=r.calculation_id "
        "LEFT JOIN fact_revision f ON f.id=r.fact_id "
        "LEFT JOIN subject s ON s.id=f.subject_id "
        "LEFT JOIN calculation_publication p ON p.calculation_id=r.calculation_id "
        "LEFT JOIN subject ps ON ps.id=p.subject_id "
        "UNION ALL "
        # A current source without publication is a damaged candidate. Its
        # fact month bounds the locator, never establishes a posting month.
        "SELECT h.subject_id,h.calculation_id,coalesce(c.period,f.period),0,"
        "coalesce(c.fact_id,f.id),coalesce(s.kind,c.kind),coalesce(c.period,f.period),"
        "NULL,NULL,0,1 FROM scoped_subjects ss "
        "CROSS JOIN calculation_current h ON h.subject_id=ss.subject_id "
        "LEFT JOIN calculation c ON c.id=h.calculation_id "
        "LEFT JOIN subject s ON s.id=h.subject_id "
        "LEFT JOIN fact_current fc ON fc.subject_id=h.subject_id "
        "LEFT JOIN fact_revision f ON f.id=fc.fact_id "
        "LEFT JOIN calculation_publication p ON p.calculation_id=h.calculation_id "
        "CROSS JOIN selection q WHERE p.id IS NULL "
        "AND coalesce(c.period,f.period)<=q.month), "
        # A complete frozen locator wins same-ID ties, without making the
        # missing/redirected mutable header valid. Strict consumption follows.
        "ranked_heads AS (SELECT *,row_number() OVER(PARTITION BY subject_id "
        "ORDER BY posting_period DESC,sequence DESC,"
        "((fact_id IS NOT NULL)+(kind IS NOT NULL)+(period IS NOT NULL)) DESC,"
        "locator_priority DESC,calculation_id DESC) rn FROM eligible), "
        "heads AS (SELECT * FROM ranked_heads) "
    )
    parameters = canonical(sorted(kinds)), canonical(limits), snapshot.month, selected_month
    if subjects is not None:
        parameters += (canonical(sorted(subjects)),)
    return query, parameters


def _exclude_verified_head_withdrawals(snapshot, heads):
    """Authenticate located absence markers without evaluating candidates twice."""
    withdrawn = {head["publication_id"]: head for head in heads if head["mode"] == "withdrawn"}
    if not withdrawn:
        return heads
    from .contracts import KernelError

    publications = list(snapshot.connection.execute(
        "SELECT p.*,a.calculation_id current_id,"
        "EXISTS(SELECT 1 FROM calculation_publication n "
        "WHERE n.previous_publication_id=p.id) has_successor "
        "FROM json_each(?) ids CROSS JOIN calculation_publication p ON p.id=ids.value "
        "LEFT JOIN calculation_current a ON a.subject_id=p.subject_id",
        (canonical(sorted(withdrawn)),),
    ))
    if {row["id"] for row in publications} != withdrawn.keys():
        raise KernelError("content_integrity_failed", "业务撤去缺少精确发布")
    snapshot.reads.verify_publication_records(publications)
    for row in publications:
        head = withdrawn[row["id"]]
        if (row["calculation_id"] is not None or head["id"] is not None
                or row["voucher_id"] is not None or row["mode"] != "withdrawn"
                or row["subject_id"] != head["subject_id"]
                or row["posting_period"] != head["posting_period"]
                or row["sequence"] != head["sequence"]
                or not row["has_successor"] and row["current_id"] is not None):
            raise KernelError("content_integrity_failed", "业务撤去发布身份不匹配")
    return [head for head in heads if head["mode"] != "withdrawn"]


def _verify_head_sources(heads):
    missing_subject = next((head for head in heads if head["source_subject_id"] is None), None)
    if missing_subject is not None:
        from .contracts import KernelError

        # This subject is required by a located adoption, not a caller's
        # optional lookup. Its absence is damaged source identity.
        raise KernelError(
            "content_integrity_failed", "已定位的采用来源缺少业务对象",
            component="subject", record_id=missing_subject["subject_id"],
        )
    missing_fact = next((head for head in heads if head["source_fact_id"] is None), None)
    if missing_fact is not None:
        from .contracts import KernelError

        raise KernelError(
            "content_integrity_failed", "已定位的采用来源缺少事实版本",
            component="fact", record_id=missing_fact["fact_id"],
        )


def _head_line_counts(snapshot, heads, *, line_count_period=None, read_line_counts=True):
    line_month = YearMonth(line_count_period).ordinal if line_count_period else None
    line_ids = {
        row["id"] for row in heads
        if read_line_counts and (line_month is None or row["posting_period"] == line_month)
    }
    snapshot.reads.verify_sql_outcomes(line_ids)
    line_counts = {
        row["id"]: row["line_count"]
        for row in snapshot.connection.execute(
            "SELECT c.id,json_array_length(c.outcome,'$.lines') line_count "
            "FROM json_each(?) ids CROSS JOIN calculation c ON c.id=ids.value",
            (canonical(sorted(line_ids)),),
        )
    }
    for row in heads:
        row["line_count"] = line_counts.get(row["id"])
    return heads


def _head_close_locator_sql(*, exact_period=False):
    requested = (
        "(SELECT json_extract(value,'$[0]') id,"
        "CAST(json_extract(value,'$[1]') AS INTEGER) period FROM json_each(?))"
        if exact_period else "json_each(?)"
    )
    ident = "ids.id" if exact_period else "ids.value"
    period = "r.close_period=ids.period" if exact_period else "r.close_period<=?"
    return (
        "WITH RECURSIVE types(value) AS (SELECT min(reference_type) FROM close_reference "
        "UNION ALL SELECT (SELECT min(reference_type) FROM close_reference "
        "WHERE reference_type>types.value) FROM types WHERE types.value IS NOT NULL) "
        "SELECT r.* FROM types CROSS JOIN " + requested + " ids "
        "CROSS JOIN close_reference r INDEXED BY close_reference_lookup "
        "ON r.reference_type=types.value AND r.reference_id=" + ident + " "
        "WHERE r.path='adopted_results[*].calculation_id' AND " + period
    )


def _verify_head_close_locators(snapshot, heads):
    heads = tuple(heads)
    exact_period = _owns_current_selector_snapshot(snapshot.reads, snapshot.connection) and all(
        isinstance(head["id"], str) and bool(head["id"])
        and type(head["posting_period"]) is int
        and 0 <= head["posting_period"] <= snapshot.month
        for head in heads
    )
    # A calculation has one immutable publication and one posting month. Only
    # that node can directly adopt it. Other months' readiness/voucher mentions
    # are not this head's direct adoption; full index verification still checks
    # the complete multiset. The original dual-ID candidate discovery remains
    # independent, including redirected or missing mutable sources.
    parameters = (
        (canonical(sorted({(head["id"], head["posting_period"]) for head in heads})),)
        if exact_period else
        (canonical(sorted({head["id"] for head in heads})), snapshot.month)
    )
    references = list(snapshot.connection.execute(
        _head_close_locator_sql(exact_period=exact_period), parameters,
    ))
    snapshot.reads.verify_close_references(references)


def adopted_head_metadata(
    snapshot, kinds, *, posting_period=None, line_count_period=None, read_line_counts=True
):
    """Enumerate exact adopted heads; only requested line counts interpret bodies.

    Frozen publications use their authenticated close high-water mark, while
    open publications retain the exact current head. Unread line counts are
    unknown, never zero. Explicit history consumers retain the complete scope.
    """
    query, parameters = _adopted_heads_sql(snapshot, kinds, posting_period=posting_period)
    heads = [dict(row) for row in snapshot.connection.execute(
        query + "SELECT h.subject_id,h.calculation_id id,h.posting_period,h.fact_id,"
        "h.kind,h.period,h.current_missing,h.mode,h.publication_id,h.sequence,"
        "s.id source_subject_id,f.id source_fact_id "
        "FROM heads h LEFT JOIN subject s ON s.id=h.subject_id "
        "LEFT JOIN fact_revision f ON f.id=h.fact_id WHERE h.rn=1", parameters,
    )]
    heads = _exclude_verified_head_withdrawals(snapshot, heads)
    _verify_head_sources(heads)
    members = {
        head["subject_id"] for head in heads
        if head["current_missing"] and head["kind"] in {"asset_activation", "asset_consumption"}
    }
    if members:
        # Members have no independent publication. Their exact current owner
        # and immutable batch adoption must be proved before excluding them
        # from independent heads; asset consumers hydrate that member lane.
        snapshot.queries._current_accounting_heads(
            snapshot.connection, members, cutoff=snapshot.month,
            posting_period=YearMonth(posting_period).ordinal if posting_period else None,
            current_heads=True,
        )
        heads = [head for head in heads if head["subject_id"] not in members]
    missing = next((head for head in heads if head["current_missing"]), None)
    if missing is not None:
        from .contracts import KernelError

        raise KernelError(
            "content_integrity_failed", "当前核算头缺少正式发布记录",
            component="publication", record_id=missing["id"],
        )
    _verify_head_close_locators(snapshot, heads)
    return _head_line_counts(
        snapshot, heads, line_count_period=line_count_period, read_line_counts=read_line_counts
    )


def payroll_head_metadata(snapshot, kinds, *, line_count_period=None):
    return adopted_head_metadata(snapshot, kinds, line_count_period=line_count_period)


def _payroll_identity_subjects(snapshot):
    """Locate real correction scopes from retained plans, never from role aliases.

    This only authenticates the scope locator. Exact before/after fact roles
    and ordered supersession/restoration transitions are still checked by
    ``current_role_matches`` for the selected wage facts.
    """
    from .content_history_context import source_digest, source_json_loads
    from .contracts import KernelError

    subjects = set()
    by_receipt = {}
    for item in snapshot.connection.execute("SELECT * FROM identity_correction_item"):
        by_receipt.setdefault(item["correction_id"], []).append(item)
    for receipt in snapshot.connection.execute("SELECT * FROM identity_correction"):
        try:
            plan = source_json_loads(receipt["plan"])
            items = plan["items"]
            expected = {item["subject_id"]: item for item in items}
            if (source_digest(plan) != receipt["digest"]
                    or plan["correction_id"] != receipt["id"]
                    or len(expected) != len(items)):
                raise ValueError("correction scope mismatch")
            actual = by_receipt.get(receipt["id"], [])
            if len(actual) != len(expected) or any(
                item["subject_id"] not in expected or any(
                    item[field] != expected[item["subject_id"]][field]
                    for field in (
                        "action", "before_fact_id", "after_fact_id", "replacement_subject_id"
                    )
                ) for item in actual
            ):
                raise ValueError("correction item scope mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            raise KernelError(
                "content_integrity_failed", "工资归属的身份纠错范围不匹配",
                component="identity_correction", record_id=receipt["id"],
            ) from exc
        subjects.update(expected)
        subjects.update(item["replacement_subject_id"] for item in items
                        if item["replacement_subject_id"] is not None)
    return subjects


def _payroll_roster_candidates(snapshot, kinds, correction_subjects):
    """Separate whole-person discovery from required monthly/source candidates.

    Role rows locate one fact per person/kind. They never prove adoption or
    directory completeness. Monthly/open/withdrawn/corrected publications and
    missing current publications locate sources independently of those roles.
    Unresolved candidates use the existing complete head selector.
    """
    table = "entity_reference_recorded" if snapshot.close else "entity_reference_current"
    prefix = "entity_recorded" if snapshot.close else "entity_current"
    kinds_json = canonical(sorted(kinds))
    seeds = list(snapshot.connection.execute(
        "WITH requested(kind) AS (SELECT value FROM json_each(?)), "
        "objects AS MATERIALIZED (SELECT DISTINCT entity_id "
        f"FROM {table} INDEXED BY {prefix}_role "
        "WHERE role='employee' AND period<=? AND kind IN(SELECT kind FROM requested)), "
        "candidate_facts AS MATERIALIZED (SELECT o.entity_id,k.kind,(SELECT r.fact_id "
        f"FROM {table} r INDEXED BY {prefix}_entity_role_kind "
        "WHERE r.entity_id=o.entity_id AND r.role='employee' AND r.kind=k.kind AND r.period<=? "
        "ORDER BY r.period DESC,r.fact_id DESC LIMIT 1) fact_id "
        "FROM objects o CROSS JOIN requested k) "
        "SELECT c.*,coalesce(f.subject_id,fc.subject_id) subject_id "
        "FROM candidate_facts c LEFT JOIN fact_revision f ON f.id=c.fact_id "
        "LEFT JOIN fact_current fc ON fc.fact_id=c.fact_id WHERE c.fact_id IS NOT NULL",
        (kinds_json, snapshot.month, snapshot.month),
    ))
    if any(seed["subject_id"] is None for seed in seeds):
        return None
    located = list(snapshot.connection.execute(
        "WITH RECURSIVE requested(kind) AS (SELECT value FROM json_each(?)), "
        "selection(month) AS (SELECT ?), "
        "publication_periods(period) AS (SELECT min(posting_period) "
        "FROM calculation_publication WHERE posting_period<=(SELECT month FROM selection) "
        "UNION ALL SELECT (SELECT min(posting_period) FROM calculation_publication "
        "WHERE posting_period>publication_periods.period "
        "AND posting_period<=(SELECT month FROM selection)) "
        "FROM publication_periods WHERE period IS NOT NULL), "
        # Filter publication scope before joining wide calculation headers.
        # A withdrawn record is still authenticated by its ordinary consumer.
        "publications AS MATERIALIZED (SELECT p.subject_id,p.calculation_id "
        "FROM publication_periods q CROSS JOIN calculation_publication p "
        "INDEXED BY publication_posting ON p.posting_period=q.period "
        "WHERE q.period=(SELECT month FROM selection) "
        "OR NOT EXISTS(SELECT 1 FROM period_close z WHERE z.period=q.period) UNION "
        "SELECT p.subject_id,p.calculation_id FROM requested k "
        "CROSS JOIN subject s INDEXED BY subject_kind ON s.kind=k.kind "
        "CROSS JOIN calculation_publication p INDEXED BY publication_subject "
        "ON p.subject_id=s.id WHERE p.mode='withdrawn' "
        "AND p.posting_period<=(SELECT month FROM selection) UNION "
        "SELECT p.subject_id,p.calculation_id FROM json_each(?) ids "
        "CROSS JOIN calculation_publication p INDEXED BY publication_subject "
        "ON p.subject_id=ids.value WHERE p.posting_period<=(SELECT month FROM selection)), "
        "candidate_publications AS MATERIALIZED (SELECT p.* FROM publications p "
        "LEFT JOIN subject s ON s.id=p.subject_id "
        "WHERE s.kind IN(SELECT kind FROM requested) OR s.id IS NULL), "
        # A current pointer is an independent locator when its publication has
        # vanished. Inspect its source only after this narrow absence search.
        "missing_current AS MATERIALIZED (SELECT h.subject_id,h.calculation_id "
        "FROM requested k CROSS JOIN subject s INDEXED BY subject_kind ON s.kind=k.kind "
        "CROSS JOIN calculation_current h ON h.subject_id=s.id "
        "LEFT JOIN calculation_publication p "
        "ON p.calculation_id=h.calculation_id WHERE p.id IS NULL), "
        # The whole frozen month, including zero-line results, is independent
        # of publication and role hints. Both source identities can recover a
        # required subject when the other mutable source has disappeared.
        "frozen_period_heads AS MATERIALIZED (SELECT r.reference_id calculation_id,"
        "a.reference_id fact_id FROM close_reference r JOIN close_reference a "
        "ON a.close_period=r.close_period AND a.position=r.position "
        "AND a.path='adopted_results[*].fact_id' "
        "LEFT JOIN fact_revision f ON f.id=a.reference_id "
        "LEFT JOIN subject s ON s.id=f.subject_id "
        "WHERE r.close_period=(SELECT month FROM selection) "
        "AND r.path='adopted_results[*].calculation_id' "
        "AND (s.kind IN(SELECT kind FROM requested) OR s.id IS NULL)) "
        "SELECT p.subject_id,0 current_missing FROM candidate_publications p "
        "LEFT JOIN subject s ON s.id=p.subject_id "
        "LEFT JOIN calculation c ON c.id=p.calculation_id "
        "WHERE coalesce(s.kind,c.kind) IN(SELECT kind FROM requested) UNION "
        "SELECT h.subject_id,1 FROM missing_current h "
        "LEFT JOIN subject s ON s.id=h.subject_id "
        "LEFT JOIN calculation c ON c.id=h.calculation_id "
        "LEFT JOIN fact_current fc ON fc.subject_id=h.subject_id "
        "LEFT JOIN fact_revision f ON f.id=fc.fact_id "
        "WHERE coalesce(s.kind,c.kind) IN(SELECT kind FROM requested) "
        "AND coalesce(c.period,f.period)<=(SELECT month FROM selection) UNION "
        "SELECT coalesce(f.subject_id,c.subject_id,p.subject_id),0 "
        "FROM frozen_period_heads r LEFT JOIN calculation c ON c.id=r.calculation_id "
        "LEFT JOIN fact_revision f ON f.id=r.fact_id LEFT JOIN subject s ON s.id=f.subject_id "
        "LEFT JOIN calculation_publication p ON p.calculation_id=r.calculation_id "
        "LEFT JOIN subject ps ON ps.id=p.subject_id "
        "WHERE coalesce(s.kind,c.kind,ps.kind) IN(SELECT kind FROM requested)",
        (kinds_json, snapshot.month, canonical(sorted(correction_subjects))),
    ))
    # Missing publication lacks the actual posting scope. Preserve the old
    # independent-current locator rather than guessing the business month.
    if any(row["current_missing"] or row["subject_id"] is None for row in located):
        return None
    subjects = {seed["subject_id"] for seed in seeds}
    subjects.update(row["subject_id"] for row in located)
    # The retained correction plan is independently authenticated by the caller.
    # Only its wage subjects belong in this consumer's optional precise scope.
    if correction_subjects:
        subjects.update(row[0] for row in snapshot.connection.execute(
            "SELECT ids.value FROM json_each(?) ids LEFT JOIN subject s ON s.id=ids.value "
            "WHERE s.kind IN(SELECT value FROM json_each(?)) OR EXISTS("
            "SELECT 1 FROM calculation c INDEXED BY calculation_subject "
            "WHERE c.subject_id=ids.value AND c.kind IN(SELECT value FROM json_each(?)))",
            (canonical(sorted(correction_subjects)), kinds_json, kinds_json),
        ))
    return {seed["entity_id"] for seed in seeds}, subjects


def _payroll_roster_sql(snapshot, kinds, subjects, *, head_subjects=None):
    """Locate employee roles by each candidate's exact fact, not a role-wide scan."""
    query, parameters = _adopted_heads_sql(snapshot, kinds, subjects=head_subjects)
    table = "entity_reference_recorded" if snapshot.close else "entity_reference_current"
    query += (
        ", witnesses AS (SELECT h.*,r.entity_id roster_employee_id,"
        "row_number() OVER(PARTITION BY r.entity_id "
        "ORDER BY h.posting_period DESC,h.calculation_id DESC) employee_rank "
        f"FROM heads h CROSS JOIN {table} r INDEXED BY sqlite_autoindex_{table}_1 "
        "ON r.fact_id=h.fact_id "
        "WHERE h.rn=1 AND h.mode IS NOT 'withdrawn' AND r.role='employee'), "
        "selected_heads AS ("
        "SELECT subject_id,calculation_id id,posting_period,fact_id,kind,period,"
        "roster_employee_id,current_missing,mode,publication_id,sequence "
        "FROM witnesses WHERE employee_rank=1 UNION ALL "
        "SELECT subject_id,calculation_id id,posting_period,fact_id,kind,period,"
        "NULL roster_employee_id,current_missing,mode,publication_id,sequence "
        "FROM heads WHERE rn=1 AND (mode='withdrawn' OR posting_period=? "
        "OR NOT EXISTS(SELECT 1 FROM period_close z WHERE z.period=heads.posting_period) "
        "OR subject_id IN(SELECT value FROM json_each(?)))) "
        "SELECT h.*,s.id source_subject_id,f.id source_fact_id FROM selected_heads h "
        "LEFT JOIN subject s ON s.id=h.subject_id LEFT JOIN fact_revision f ON f.id=h.fact_id"
    )
    return query, (*parameters, snapshot.month, canonical(sorted(subjects)))


def payroll_list_head_metadata(snapshot, kinds, *, line_count_period=None):
    """Locate all employee identities without authenticating every frozen wage.

    One exact adopted wage witnesses each distinct employee, including old
    zero-line and retired employees. All monthly and open heads remain in the
    amount scope. Real correction scopes are separate from the roster: only
    those old heads can change current attribution of a frozen creditor.
    Directory completeness remains the full verifier/backup/repair's duty.
    """
    from . import close_storage, settlement_freeze
    from .content_history_context import close_reader, settlement_reader
    from .entity_references import declarations_for

    if (close_reader() is not close_storage or settlement_reader() is not settlement_freeze
            or getattr(snapshot.store.registry, "content_version", None) == 1
            or any(not any(item["role"] == "employee" and item["reference_type"] == "entity"
                           for item in declarations_for(kind, registry=snapshot.store.registry))
                   for kind in kinds)):
        return None
    scope = settlement_freeze._scope(
        snapshot.connection, snapshot.period, current=False, reads=snapshot.reads
    )
    if scope is None:
        return None
    subjects = _payroll_identity_subjects(snapshot) if not snapshot.close else set()
    candidates = _payroll_roster_candidates(snapshot, kinds, subjects)
    if candidates is None:
        return None
    query, parameters = _payroll_roster_sql(
        snapshot, kinds, subjects, head_subjects=candidates[1]
    )
    by_id = {}
    heads = [dict(row) for row in snapshot.connection.execute(query, parameters)]
    if candidates[0] != {
        head["roster_employee_id"] for head in heads if head["roster_employee_id"] is not None
    }:
        # A directory fact can be a draft, withdrawn or outside the requested
        # adoption month. Its absence from the precise selector is not a proof
        # that the employee has no adopted wage; retain complete discovery.
        return None
    heads = _exclude_verified_head_withdrawals(snapshot, heads)
    _verify_head_sources(heads)
    for head in heads:
        by_id.setdefault(head["id"], head)
    _verify_head_close_locators(snapshot, by_id.values())
    # A changed period payment can consume an older wage that is not the roster
    # witness. Its state already commits the digest independently; additionally
    # retain strict source/fact/seal/publication identity before using the money.
    states = settlement_freeze._payroll_period_payment_states(
        snapshot.connection, scope, reads=snapshot.reads,
        keys=settlement_freeze._payroll_period_keys(scope),
    )
    headers = snapshot.reads.calculation_identity_headers(
        state["source_calculation_id"] for state, _paid, _other in states
    )
    snapshot.reads.verify_publication_records(headers.values())
    from .contracts import KernelError

    if any(
        not (header := headers[state["source_calculation_id"]])["cs"] or not header["fs"]
        or header["source_subject"] != state["source_subject_id"]
        or header["subject_id"] != state["source_subject_id"]
        for state, _paid, _other in states
    ):
        raise KernelError("content_integrity_failed", "工资实付来源的精确身份不匹配")
    return _head_line_counts(snapshot, list(by_id.values()), line_count_period=line_count_period)


def payroll_cohort_identities_match(snapshot, heads, employee_by_fact):
    """Check whether verified current role attribution still matches cohort creditors.

    Explicit identity correction can reassign an older frozen wage to a current
    employee role without rewriting that frozen cohort's original creditor.
    The caller has already authenticated these precise facts and role mappings.
    """
    from .schema import table_name

    by_kind = {}
    for head in heads:
        employee = employee_by_fact.get(head["fact_id"])
        if employee is None:
            return False
        by_kind.setdefault(head["kind"], []).append([head["fact_id"], employee])
    for kind, pairs in by_kind.items():
        if snapshot.connection.execute(
            "SELECT 1 FROM json_each(?) requested "
            f"LEFT JOIN {table_name(kind)} f ON f.revision_id=json_extract(requested.value,'$[0]') "
            "WHERE f.employee_id IS NOT json_extract(requested.value,'$[1]') LIMIT 1",
            (canonical(pairs),),
        ).fetchone() is not None:
            return False
    return True


def verified_adopted_head_identities(snapshot, heads, *, kinds=None):
    """Authenticate exact adopted identities without proving unused result bodies.

    Frozen digests bind to independent adopted leaves; open heads bind to their
    exact current publication. This proves no full source/outcome content and
    never marks those proof sets. Consumers of amounts or state still check the
    corresponding body through the ordinary strict read path.
    """
    from .contracts import KernelError

    heads = list(heads)
    identifiers = {head["id"] for head in heads}
    # Missing or redirected current rows must not silently remove an open
    # subject from the enumerator before it can be authenticated. Inspect only
    # publication identities; a legitimate later publication outside this
    # month has a successor and stays outside this requested source scope.
    open_terminals = list(snapshot.connection.execute(
        "SELECT p.*,c.id calculation_exists,h.calculation_id current_id "
        "FROM json_each(?) requested CROSS JOIN subject s INDEXED BY subject_kind "
        "ON s.kind=requested.value "
        "CROSS JOIN calculation_publication p INDEXED BY publication_subject "
        "ON p.subject_id=s.id "
        "LEFT JOIN calculation c ON c.id=p.calculation_id "
        "LEFT JOIN calculation_current h ON h.subject_id=p.subject_id "
        "WHERE p.posting_period<=? "
        "AND NOT EXISTS(SELECT 1 FROM period_close z WHERE z.period=p.posting_period) "
        "AND NOT EXISTS(SELECT 1 FROM calculation_publication n "
        "WHERE n.previous_publication_id=p.id)",
        (canonical(sorted(
            kinds if kinds is not None else {head["kind"] for head in heads}
        )), snapshot.month),
    ))
    # A withdrawal is a real authoritative publication with no calculation.
    # Prove its bounded record before its mode can remove a subject from the
    # identity scope; a forged mode must never hide a missing current head.
    snapshot.reads.verify_publication_records(open_terminals)
    for row in open_terminals:
        if row["mode"] == "withdrawn":
            if (row["calculation_id"] is not None or row["voucher_id"] is not None
                    or row["current_id"] is not None):
                raise KernelError("content_integrity_failed", "业务撤去发布身份不匹配")
            continue
        if row["calculation_exists"] is None or row["current_id"] != row["calculation_id"]:
            raise KernelError("content_integrity_failed", "开放采用来源缺少精确当前头")
    from . import close_storage
    from .content_history_context import close_reader

    identity_headers = (
        snapshot.reads.calculation_identity_headers(identifiers)
        if close_reader() is close_storage
        and getattr(snapshot.store.registry, "content_version", None) != 1 else None
    )
    month_strings = {}

    def month_string(period):
        if type(period) is not int or period not in month_strings:
            month_strings[period] = str(YearMonth.from_ordinal(period))
        return month_strings[period]

    metadata = (
        {
            ident: {
                "id": ident,
                "subject_id": row["source_subject"],
                "kind": row["source_kind"],
                "period": month_string(row["source_period"]),
                "fact_id": row["source_fact_id"],
                "result_digest": row["source_digest"].hex(),
                "program_version": row["source_program_version"],
                "fact_revision": row["fact_revision"],
                "publication_id": row["id"],
                "publication_mode": row["mode"],
                "posting_period": month_string(row["posting_period"]),
                "voucher_id": row["voucher_id"],
                "publication_role": "independent",
            }
            for ident, row in identity_headers.items()
        }
        if identity_headers is not None else snapshot.reads.metadata(identifiers, state=False)
    )
    periods = {head["posting_period"] for head in heads}
    closes = snapshot.reads.authoritative_close_rows(periods=periods, through_period=snapshot.month)
    adopted = {}
    subjects_by_period = {row["period"]: set() for row in closes}
    for head in heads:
        if head["posting_period"] in subjects_by_period:
            subjects_by_period[head["posting_period"]].add(head["subject_id"])
    for selection in snapshot.reads.close_adopted_results_many(
        closes, subjects={head["subject_id"] for head in heads},
        subjects_by_period=subjects_by_period,
    ):
        for item in selection.adopted_results:
            adopted[item["calculation_id"]] = item
    closed_periods = {row["period"] for row in closes}
    for head in heads:
        for period in (head["period"], head["posting_period"]):
            month_string(period)
    frozen = set()
    for head in heads:
        calc = metadata[head["id"]]
        if (
            calc["subject_id"], calc["fact_id"], calc["kind"], calc["period"],
            calc["posting_period"],
        ) != (
            head["subject_id"], head["fact_id"], head["kind"], month_strings[head["period"]],
            month_strings[head["posting_period"]],
        ):
            raise KernelError("content_integrity_failed", "业务采用身份不匹配")
        if head["posting_period"] not in closed_periods:
            continue
        item = adopted.get(head["id"])
        if item is None or any(
            item[adopted_field] != calc[field]
            for adopted_field, field in (
                ("publication_id", "publication_id"),
                ("subject_id", "subject_id"),
                ("fact_id", "fact_id"),
                ("source_period", "period"),
                ("posting_period", "posting_period"),
                ("result_digest", "result_digest"),
            )
        ):
            raise KernelError("content_integrity_failed", "冻结业务采用身份不匹配")
        frozen.add(head["id"])
    # The current identity batch carries publication, seals and source identity
    # together. Released readers retain the prior metadata/publication path.
    # Neither path records a successful result-body or source-content proof.
    publications = identity_headers if identity_headers is not None else {
        row["calculation_id"]: row
        for row in snapshot.connection.execute(
            "SELECT p.*,h.calculation_id current_id,"
            "EXISTS(SELECT 1 FROM calculation_publication n "
            "WHERE n.previous_publication_id=p.id) has_successor,"
            "EXISTS(SELECT 1 FROM calculation_seal z "
            "WHERE z.calculation_id=p.calculation_id) cs,"
            "EXISTS(SELECT 1 FROM fact_seal z WHERE z.fact_id=c.fact_id) fs "
            "FROM json_each(?) ids JOIN calculation_publication p ON p.calculation_id=ids.value "
            "JOIN calculation c ON c.id=p.calculation_id "
            "LEFT JOIN calculation_current h ON h.subject_id=p.subject_id",
            (canonical(sorted(identifiers)),),
        )
    }
    if publications.keys() != identifiers:
        raise KernelError("content_integrity_failed", "采用来源缺少正式发布")
    snapshot.reads.verify_publication_records(publications.values())
    for head in heads:
        publication = publications[head["id"]]
        if not publication["cs"] or not publication["fs"]:
            raise KernelError("content_integrity_failed", "业务来源未封存")
        if (
            publication["subject_id"] != head["subject_id"]
            or publication["posting_period"] != head["posting_period"]
            or publication["id"] != metadata[head["id"]]["publication_id"]
            or head["id"] not in frozen and (
                publication["current_id"] != head["id"] or publication["has_successor"]
            )
        ):
            raise KernelError("content_integrity_failed", "采用来源的当前发布身份不匹配")
    return {calc["subject_id"]: calc for calc in metadata.values()}


def verified_payroll_heads(snapshot, heads):
    """Prove every wage head's adoption identity; content is consumer scoped."""
    from .dashboard import PAYROLL_KINDS

    return verified_adopted_head_identities(
        snapshot, heads, kinds=PAYROLL_KINDS | {"opening_payroll_payable"}
    )


def payroll_head_identities(snapshot, heads):
    """Locate each adopted wage's employee through its exact source reference."""
    if not heads:
        return {}
    from .entity_references import verify_hits

    # Every head contributes to the employee list, including old no-line
    # publications absent from the month journal and heads later collapsed to
    # another representative employee. Authenticate the complete recorded role
    # set before any indexed employee identity influences that selection.
    verified = verify_hits(
        snapshot.connection,
        [{"fact_id": ident} for ident in sorted({head["fact_id"] for head in heads})],
        identity_match="recorded" if snapshot.close else "current",
        registry=snapshot.store.registry,
    )
    by_fact = {head["fact_id"]: head for head in heads}
    found = {}
    for fact_id, _path, entity_id, role, kind, period, _digest in verified:
        if role != "employee":
            continue
        head = by_fact[fact_id]
        if (
            entity_id is None
            or fact_id in found and found[fact_id] != entity_id
            or kind != head["kind"]
            or period != head["period"]
        ):
            from .contracts import KernelError

            raise KernelError(
                "content_integrity_failed",
                "工资人员来源目录不匹配",
                component="employee",
                record_id=fact_id,
            )
        found[fact_id] = entity_id
    if set(found) != set(by_fact):
        from .contracts import KernelError

        raise KernelError("content_integrity_failed", "工资人员来源目录缺行", component="employee")
    return found


def metric_rows(journal, fields, *, cost_accounts=()):
    """Project only scalar facts and named metric values for whole-domain totals."""
    snapshot = journal.snapshot
    query, parameters = journal.sql()
    snapshot.reads.verify_sql_outcomes(
        row[0]
        for row in snapshot.connection.execute(
            "SELECT DISTINCT basis_calculation_id FROM (" + query + ")", parameters
        )
    )
    pairs = ",".join(f"'{field}',json_extract(c.outcome,'$.values.{field}')" for field in fields)
    cost_sql = (
        "coalesce((SELECT sum(l.debit-l.credit) FROM voucher_line l "
        "WHERE l.version_id=j.id AND l.account IN (SELECT value FROM json_each(?))),0)"
        if cost_accounts else "0"
    )
    rows = snapshot.connection.execute(
        f"SELECT j.*,json_object({pairs}) AS metric_values,{cost_sql} AS metric_cost "
        f"FROM ({query}) j "
        "JOIN calculation c ON c.id=j.basis_calculation_id ORDER BY j.period,j.number,j.id",
        [*([canonical(sorted(cost_accounts))] if cost_accounts else []), *parameters],
    ).fetchall()
    journal._verify_frozen_headers(rows=rows)
    metadata = snapshot.reads.metadata({row["basis_calculation_id"] for row in rows})
    scalar = scalar_facts(snapshot, metadata.values())
    result = []
    for row in rows:
        record = dict(row)
        meta = metadata[row["basis_calculation_id"]]
        values = {
            key: value
            for key, value in json.loads(row["metric_values"]).items()
            if value is not None
        }
        record["basis"] = dict(snapshot.calculation(meta["id"])) | {
            "fact": {
                "id": meta["fact_id"],
                "revision": meta["fact_revision"],
                "data": scalar[meta["fact_id"]],
            },
            "outcome": {"values": values},
        }
        record["sign"] = -1 if record["reverses_id"] else 1
        record["calculation_id"] = meta["id"]
        result.append(record)
    return result


def _checked_posted_lines(row, actual_lines, expected):
    """Compare exact saved voucher lines with an authenticated result."""
    from .integrity import _invalid, _lines
    from .types import sum_fen

    ident = row["id"]
    if any(item["line_no"] != index for index, item in enumerate(actual_lines, 1)):
        _invalid("voucher", ident, "voucher_line_order_mismatch")
    actual = [(item["account"], item["debit"], item["credit"], item["cashflow"])
              for item in actual_lines]
    if any(type(item[1]) is not int or type(item[2]) is not int for item in actual):
        _invalid("voucher", ident, "invalid_line")
    if not actual or sum_fen(line[1] for line in actual) != row["total"]:
        _invalid("voucher", ident, "voucher_total_mismatch")
    if actual != expected:
        _lines(
            [{key: item[key] for key in ("account", "debit", "credit", "cashflow")}
             for item in actual_lines], "voucher", ident,
        )
        _invalid("voucher", ident, "voucher_lines_mismatch")
    return actual


class CalculationView(dict):
    def __init__(self, snapshot, metadata):
        from .types import YearMonth

        super().__init__(metadata)
        self.snapshot = snapshot
        self["digest"] = bytes.fromhex(self["result_digest"])
        self["period"] = YearMonth(self["period"]).ordinal
        self["posting_period"] = (
            YearMonth(self["posting_period"]).ordinal
            if self["posting_period"] is not None
            else None
        )

    def __getitem__(self, key):
        if key == "fact" and key not in self:
            self[key] = self.snapshot.fact(super().__getitem__("fact_id"))
        if key == "outcome" and key not in self:
            self[key] = self.snapshot.reads.calculation(super().__getitem__("id"))["outcome"]
        return super().__getitem__(key)

    def get(self, key, default=None):
        return self[key] if key in self or key in {"fact", "outcome"} else default

    def __or__(self, other):
        self["fact"]
        self["outcome"]
        return dict(self) | other


class JournalRow(dict):
    def __init__(self, snapshot, metadata):
        super().__init__(metadata)
        self.snapshot = snapshot
        self["calculation_id"] = self["basis_calculation_id"]
        self["sign"] = -1 if self["reverses_id"] else 1

    def __getitem__(self, key):
        if key == "basis" and key not in self:
            self[key] = self.snapshot.calculation(super().__getitem__("basis_calculation_id"))
        if key == "lines" and key not in self:
            self[key] = self.snapshot.reads.voucher_lines((super().__getitem__("id"),))[
                super().__getitem__("id")
            ]
        return super().__getitem__(key)

    def get(self, key, default=None):
        return self[key] if key in self or key in {"basis", "lines"} else default


def _account_voucher_ids(connection, accounts, cutoff, *, posting_period=None):
    """Locate account candidates within the requested posting scope.

    A single month starts with its period index; an all-history request keeps
    the account index. This only narrows candidates, before the existing
    publication/adoption selector and content checks.
    """
    if posting_period is not None:
        query = (
            "SELECT v.id FROM voucher_version v INDEXED BY voucher_period "
            "WHERE v.period=? AND v.period<=? AND EXISTS(SELECT 1 FROM voucher_line l "
            "WHERE l.version_id=v.id AND l.account IN (SELECT value FROM json_each(?)))"
        )
        parameters = (posting_period, cutoff, canonical(sorted(accounts)))
    else:
        query = (
            "SELECT DISTINCT l.version_id FROM voucher_line l INDEXED BY voucher_line_account "
            "JOIN voucher_version v ON v.id=l.version_id WHERE l.account IN "
            "(SELECT value FROM json_each(?)) AND v.period<=?"
        )
        parameters = (canonical(sorted(accounts)), cutoff)
    return {row[0] for row in connection.execute(query, parameters)}


class Journal:
    def __init__(self, snapshot, *, month=None, kinds=None, accounts=None, subjects=None):
        self.snapshot, self.month = snapshot, month
        self.kinds, self.accounts, self.subjects = kinds, accounts, subjects

    def select(self, *, kinds=None, accounts=None, subjects=None):
        return Journal(
            self.snapshot,
            month=self.month,
            kinds=kinds if kinds is not None else self.kinds,
            accounts=accounts if accounts is not None else self.accounts,
            subjects=subjects if subjects is not None else self.subjects,
        )

    def _cache(self):
        reads = self.snapshot.reads
        return reads._report_snapshot_cache if reads._snapshot_active else None

    def _key(self, operation):
        return (
            operation,
            self.snapshot.month,
            self.month,
            *(
                None if value is None else frozenset((value,) if isinstance(value, str) else value)
                for value in (self.kinds, self.accounts, self.subjects)
            ),
        )

    def sql(self):
        cache, key = self._cache(), self._key("journal_sql")
        if cache is not None and key in cache:
            query, parameters = cache[key]
            return query, list(parameters)
        query, parameters = self._sql()
        if cache is not None:
            cache[key] = query, tuple(parameters)
        return query, parameters

    def _sql(self):
        self.snapshot.reads.verify_open_voucher_scope(
            self.snapshot.month, posting_period=self.month
        )
        voucher_ids = None
        if self.accounts is not None:
            voucher_ids = _account_voucher_ids(
                self.snapshot.connection,
                self.accounts,
                self.snapshot.month,
                posting_period=self.month,
            )
        no_close_references = False
        if self.month is not None:
            cache, key = self._cache(), ("open_voucher_period", self.snapshot.month, self.month)
            if cache is not None and key in cache:
                no_close_references = cache[key]
            else:
                connection = self.snapshot.connection
                no_close_references = (
                    connection.execute(
                        "SELECT 1 FROM period_close WHERE period=?", (self.month,)
                    ).fetchone()
                    is None
                    and connection.execute(
                        "SELECT 1 FROM voucher_version v INDEXED BY voucher_period "
                        "CROSS JOIN close_reference r INDEXED BY close_reference_lookup "
                        "ON r.reference_type='voucher' AND r.reference_id=v.id "
                        "AND r.close_period<=? WHERE v.period=? LIMIT 1",
                        (self.snapshot.month, self.month),
                    ).fetchone()
                    is None
                )
                if cache is not None:
                    cache[key] = no_close_references
        source, parameters = selected_voucher_sql(
            self.snapshot.period,
            kinds=self.kinds,
            subject_ids=self.subjects,
            posting_period=str(YearMonth.from_ordinal(self.month))
            if self.month is not None
            else None,
            voucher_ids=voucher_ids,
            no_close_references=no_close_references,
        )
        query = f"SELECT j.* FROM ({source}) j WHERE 1=1"
        if self.month is not None:
            query += " AND j.period=?"
            parameters.append(self.month)
        if self.accounts is not None:
            query += (
                " AND EXISTS(SELECT 1 FROM voucher_line l WHERE l.version_id=j.id "
                "AND l.account IN (SELECT value FROM json_each(?)))"
            )
            parameters.append(canonical(sorted(self.accounts)))
        return query, parameters

    def __len__(self):
        cache, key = self._cache(), self._key("journal_count")
        if cache is not None and key in cache:
            return cache[key]
        query, parameters = self.sql()
        if self.month is not None and self.kinds is None and self.accounts is None:
            self._verify_frozen_headers()
        result = self.snapshot.connection.execute(
            f"SELECT count(*) FROM ({query})", parameters
        ).fetchone()[0]
        if cache is not None:
            cache[key] = result
        return result

    def prime_summary(self):
        """Read whole-month counts and line totals through one exact selection.

        Pages that only need a total still use the narrower ``__len__`` query.
        A caller needing all three summaries opts in before asking for a page.
        """
        cache, key = self._cache(), self._key("journal_summary")
        if cache is not None and key in cache:
            return cache[key]
        query, parameters = self.sql()
        self._verify_frozen_headers()
        # The selector has one row per voucher version. DISTINCT counts that
        # row once after the line join, including a voucher with no lines.
        groups = tuple(
            (
                row["kind"],
                row["reversal"],
                row["count"],
                row["line_count"],
                row["debit"],
                row["credit"],
            )
            for row in self.snapshot.connection.execute(
                "SELECT j.basis_kind kind,j.reverses_id IS NOT NULL reversal,"
                "count(DISTINCT j.id) count,count(l.version_id) line_count,"
                "coalesce(sum(l.debit),0) debit,coalesce(sum(l.credit),0) credit "
                f"FROM ({query}) j LEFT JOIN voucher_line l ON l.version_id=j.id "
                "GROUP BY j.basis_kind,j.reverses_id IS NOT NULL",
                parameters,
            )
        )
        summary = {
            "count": sum(row[2] for row in groups),
            "groups": groups,
            "totals": {
                "line_count": sum(row[3] for row in groups),
                "debit": checked(sum(row[4] for row in groups)),
                "credit": checked(sum(row[5] for row in groups)),
            },
        }
        if cache is not None:
            cache[key] = summary
            cache[self._key("journal_count")] = summary["count"]
        return summary

    def __iter__(self):
        query, parameters = self.sql()
        cursor = self.snapshot.connection.execute(query + " ORDER BY period,number,id", parameters)
        while rows := list(islice(cursor, 100)):
            yield from self.hydrate(rows)

    def hydrate(self, rows, *, include_lines=True):
        self.snapshot.reads.verify_selected_voucher_adoptions(
            rows, through_period=self.snapshot.month,
        )
        self.snapshot.reads.metadata({row["basis_calculation_id"] for row in rows})
        # Lines are prefetched only for an actual returned batch, never summaries.
        if include_lines:
            self.snapshot.reads.voucher_lines({row["id"] for row in rows})
        return [JournalRow(self.snapshot, dict(row)) for row in rows]

    def page(
        self,
        after_number,
        limit,
        *,
        voucher_number=None,
        voucher_version_id=None,
        include_lines=True,
    ):
        total = len(self)
        rows = self._verified_page_rows(
            after_number, limit, voucher_number=voucher_number,
            voucher_version_id=voucher_version_id,
        )
        if rows is None:
            rows = self._sql_page_rows(
                after_number, limit, voucher_number=voucher_number,
                voucher_version_id=voucher_version_id,
            )
        more = len(rows) > limit
        records = self.hydrate(rows[:limit], include_lines=include_lines)
        return records, {
            "total_count": total,
            "filtered_count": total,
            "returned_count": len(records),
            "has_more": more,
            "next_cursor": records[-1]["number"] if more else None,
        }

    def business_page(self, after, limit, *, include_lines=True):
        """Order verified scalar identities before loading this page's bodies."""
        from .dashboard_sort import business_sort_metadata, date_object_key

        rows = self.verified_rows()
        if rows is None:
            query, parameters = self.sql()
            rows = list(self.snapshot.connection.execute(query, parameters))
            self.snapshot.reads.verify_selected_voucher_adoptions(
                rows, through_period=self.snapshot.month,
            )
        metadata = business_sort_metadata(
            self.snapshot, {row["basis_calculation_id"] for row in rows}
        )
        ordered = sorted(rows, key=lambda row: date_object_key(
            metadata[row["basis_calculation_id"]], str(YearMonth.from_ordinal(row["period"])),
            (row["subject_id"] if "subject_id" in row.keys() else "", row["id"]),
        ))
        by_id = {row["id"]: row for row in ordered}
        selected, page = page_keys(by_id, after, limit)
        return self.hydrate([by_id[key] for key in selected], include_lines=include_lines), page

    def _verified_page_rows(
        self, after_number, limit, *, voucher_number=None, voucher_version_id=None,
    ):
        """Seek headers already proved for this exact month and read snapshot."""
        reads, connection = self.snapshot.reads, self.snapshot.connection
        if (
            self.month is None or any(value is not None for value in (
                self.kinds, self.accounts, self.subjects,
            ))
            or not _owns_current_selector_snapshot(reads, connection)
            or type(limit) is not int or not 1 <= limit <= 500
            or type(after_number) is not int
            or voucher_number is not None and type(voucher_number) is not int
            or voucher_version_id is not None and type(voucher_version_id) is not str
        ):
            return None
        if not -(1 << 63) <= after_number < (1 << 63) or (
            voucher_number is not None and not -(1 << 63) <= voucher_number < (1 << 63)
        ):
            # Keep SQLite's original integer binding rejection for malformed
            # cursors and exact-number requests, rather than returning no rows.
            return None
        verified = self.verified_rows()
        if verified is None:
            return None
        cache, key = self._cache(), self._key("journal_page_headers")
        if key not in cache:
            ordered = tuple(sorted(verified, key=lambda row: (row["number"], row["id"])))
            cache[key] = (
                ordered, tuple(row["number"] for row in ordered),
                {row["id"]: row for row in ordered},
            )
        ordered, numbers, by_id = cache[key]
        if voucher_version_id is not None:
            row = by_id.get(voucher_version_id)
            return [] if row is None else [row]
        if voucher_number is not None:
            start, end = bisect_left(numbers, voucher_number), bisect_right(numbers, voucher_number)
            return ordered[start:min(end, start + limit + 1)]
        start = bisect_right(numbers, after_number)
        return ordered[start:start + limit + 1]

    def _sql_page_rows(
        self, after_number, limit, *, voucher_number=None, voucher_version_id=None,
    ):
        query, parameters = self.sql()
        query += (
            " AND j.id=?"
            if voucher_version_id is not None
            else " AND j.number=?"
            if voucher_number is not None
            else " AND j.number>?"
        )
        parameters.append(
            voucher_version_id
            if voucher_version_id is not None
            else voucher_number
            if voucher_number is not None
            else after_number
        )
        return self.snapshot.connection.execute(
            query + " ORDER BY j.number,j.id LIMIT ?", [*parameters, limit + 1]
        ).fetchall()

    def totals(self):
        cache, key = self._cache(), self._key("journal_summary")
        if cache is not None and key in cache:
            return dict(cache[key]["totals"])
        query, parameters = self.sql()
        self._verify_frozen_headers()
        return dict(
            self.snapshot.connection.execute(
                "SELECT count(*) AS line_count,coalesce(sum(l.debit),0) AS debit,"
                f"coalesce(sum(l.credit),0) AS credit FROM ({query}) j "
                "JOIN voucher_line l ON l.version_id=j.id",
                parameters,
            ).fetchone()
        )

    def verified_rows(self):
        """Exact headers from a successful whole-scope money proof, if available."""
        cache = self._cache()
        return None if cache is None else cache.get(self._key("journal_verified_rows"))

    def _verify_frozen_headers(self, *, rows=None):
        """Bind selected live headers before aggregating their saved directions.

        This reads scalar headers only, never result bodies or upstream facts.
        A whole-scope money proof already covers these exact headers.
        """
        if self.verified_rows() is not None:
            return
        cache, key = self._cache(), self._key("journal_frozen_headers")
        if cache is not None and key in cache:
            return
        query, parameters = self.sql()
        expected = None
        if self.month is not None and self.kinds is None and self.accounts is None:
            closes = self.snapshot.reads.authoritative_close_rows(periods=(self.month,))
            if closes:
                close = closes[0]
                vouchers = (
                    self.snapshot.reads.close_section(close, "vouchers")
                    if self.subjects is None else
                    self.snapshot.reads.close_accounting(
                        close,
                        subjects=(
                            {self.subjects} if isinstance(self.subjects, str) else self.subjects
                        ),
                    ).vouchers
                )
                expected = {voucher["id"] for voucher in vouchers}
            elif cache is not None and cache.get(
                ("open_voucher_period", self.snapshot.month, self.month)
            ):
                cache[key] = True
                return
        if rows is None:
            rows = list(self.snapshot.connection.execute(
                "SELECT j.id,j.voucher_id,j.voucher_calculation_id,j.period,j.number,j.total,"
                f"j.reverses_id,j.close_period FROM ({query}) j WHERE j.close_period IS NOT NULL",
                parameters,
            ))
        if expected is not None and {
            row["id"] for row in rows if row["close_period"] is not None
        } != expected:
            from .contracts import KernelError

            raise KernelError("content_integrity_failed", "冻结月份凭证选择与原采用范围不一致")
        self.snapshot.reads.verify_selected_voucher_adoptions(
            rows, through_period=self.snapshot.month,
        )
        if cache is not None:
            cache[key] = True

    def account_amounts(self):
        """Authenticate the exact month's posted lines before aggregating money.

        This consumes the saved owner and adopted result, not their ancestors
        or a second financial-position calculation. Full historical verification
        retains its independent complete dependency and evidence checks.
        """
        if self.month is None:
            raise ValueError("posted account amounts require one month")
        from .integrity import _invalid, _lines
        from .query_reads import (
            _selected_current_voucher_publications,
            verify_current_voucher_publications,
        )
        from .types import checked

        reads, connection = self.snapshot.reads, self.snapshot.connection
        query, parameters = self.sql()
        rows = list(connection.execute(query, parameters))
        reads.verify_selected_voucher_adoptions(rows, through_period=self.snapshot.month)
        publications = (
            _selected_current_voucher_publications(
                reads, rows, period=self.month, cutoff=self.snapshot.month
            )
            if self.kinds is None and self.accounts is None and self.subjects is None
            else None
        )
        if publications is None:
            publications = verify_current_voucher_publications(
                connection, {row["id"] for row in rows}
            )
        close_rows = reads.authoritative_close_rows(periods=(self.month,))
        adopted, frozen_vouchers = {}, {}
        if close_rows:
            close = close_rows[0]
            adopted = {
                item["calculation_id"]: item
                for item in reads.close_section(close, "adopted_results")
            }
            frozen_vouchers = {item["id"]: item for item in reads.close_section(close, "vouchers")}
            if {row["id"] for row in rows} != frozen_vouchers.keys():
                _invalid("voucher", self.month, "frozen_month_selection_mismatch")
        else:
            current_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT v.id FROM voucher_current h JOIN voucher_version v "
                    "ON v.id=h.version_id WHERE v.period=?",
                    (self.month,),
                )
            }
            if {row["id"] for row in rows} != current_ids:
                _invalid("voucher", self.month, "current_month_selection_mismatch")
        originals = reads.vouchers(row["reverses_id"] for row in rows if row["reverses_id"])
        identifiers = {
            row[key] for row in rows for key in ("voucher_calculation_id", "basis_calculation_id")
        }
        identifiers.update(row["calculation_id"] for row in originals.values())
        if originals:
            original_metadata = reads.metadata(
                {row["calculation_id"] for row in originals.values()}, state=False
            )
            original_closes = reads.authoritative_close_rows(
                periods={row["period"] for row in originals.values()}
            )
            slices = reads.close_accounting_many(
                original_closes,
                subjects={record["subject_id"] for record in original_metadata.values()},
            )
            for selection in slices:
                adopted.update((item["calculation_id"], item) for item in selection.adopted_results)
                frozen_vouchers.update((item["id"], item) for item in selection.vouchers)
            if not originals.keys() <= frozen_vouchers.keys():
                _invalid("voucher", self.month, "reversal_frozen_source_missing")
        identifiers.update(
            frozen_vouchers[row["id"]]["adopted_calculation_id"]
            for row in (*originals.values(), *rows) if row["id"] in frozen_vouchers
        )
        outcomes = reads.verify_selected_content(identifiers)
        metadata = reads.metadata(
            {frozen_vouchers[row["id"]]["adopted_calculation_id"]
             for row in (*originals.values(), *rows) if row["id"] in frozen_vouchers},
            state=False,
        )
        lines = reads.voucher_lines({row["id"] for row in rows} | originals.keys())
        missing_publications = identifiers - publications.keys()
        if missing_publications:
            publications.update(
                (row["calculation_id"], row) for row in connection.execute(
                "SELECT p.* FROM json_each(?) ids CROSS JOIN calculation_publication p "
                "ON p.calculation_id=ids.value",
                (canonical(sorted(missing_publications)),),
            )
            )
        reads.verify_publication_records(publications.values())
        expected_lines = {}

        def result_lines(calculation_id):
            if calculation_id not in expected_lines:
                expected_lines[calculation_id] = _lines(
                    outcomes[calculation_id]["lines"], "calculation", calculation_id
                )
            return expected_lines[calculation_id]

        amounts, authenticated_lines, groups = {}, {}, {}
        for row in [*originals.values(), *rows]:
            ident = row["id"]
            actual_lines = lines[ident]
            publication = publications.get(row["calculation_id"])
            if publication is None or publication["posting_period"] != row["period"]:
                _invalid("voucher", ident, "voucher_publication_mismatch")
            if row["reverses_id"]:
                original = originals[row["reverses_id"]]
                if original["period"] >= row["period"] or original["reverses_id"] is not None:
                    _invalid("voucher", ident, "invalid_reversal_source")
                expected = [
                    (account, credit, debit, cashflow)
                    for account, debit, credit, cashflow in authenticated_lines[original["id"]]
                ]
            else:
                if publication["voucher_id"] != row["voucher_id"]:
                    _invalid("voucher", ident, "voucher_publication_identity_mismatch")
                expected = result_lines(row["calculation_id"])
            actual = _checked_posted_lines(row, actual_lines, expected)
            frozen = frozen_vouchers.get(ident)
            if frozen:
                if any(
                    row[field] != frozen[field]
                    for field in ("voucher_id", "calculation_id", "reverses_id", "total", "number")
                ):
                    _invalid("voucher", ident, "frozen_voucher_header_mismatch")
                adopted_id = frozen["adopted_calculation_id"]
                entry, meta = adopted.get(adopted_id), metadata[adopted_id]
                if entry is None or any(
                    entry[key] != meta[field]
                    for key, field in (
                        ("publication_id", "publication_id"),
                        ("subject_id", "subject_id"),
                        ("fact_id", "fact_id"),
                        ("source_period", "period"),
                        ("posting_period", "posting_period"),
                        ("result_digest", "result_digest"),
                    )
                ):
                    _invalid("voucher", ident, "frozen_adopted_result_mismatch")
                if not row["reverses_id"] and actual != result_lines(adopted_id):
                    _invalid("voucher", ident, "frozen_adopted_lines_mismatch")
            if (
                ident not in originals
                and not row["reverses_id"]
                and actual != result_lines(row["basis_calculation_id"])
            ):
                _invalid("voucher", ident, "adopted_lines_mismatch")
            authenticated_lines[ident] = actual
            if ident in originals:
                continue
            group = groups.setdefault((row["basis_kind"], row["reverses_id"] is not None),
                                      [0, 0, 0, 0])
            group[0] += 1
            group[1] += len(actual)
            # The complete actual lines equal a balanced, strictly checked
            # result, and their debit sum equals this exact voucher total.
            group[2] += row["total"]
            group[3] += row["total"]
            for account, debit, credit, _ in actual:
                previous = amounts.setdefault(account, [0, 0])
                previous[0] = checked(previous[0] + debit)
                previous[1] = checked(previous[1] + credit)
        summary = {
            "count": len(rows),
            "groups": tuple((kind, reversal, *values)
                            for (kind, reversal), values in sorted(groups.items())),
            "totals": {
                "line_count": sum(values[1] for values in groups.values()),
                "debit": checked(sum(values[2] for values in groups.values())),
                "credit": checked(sum(values[3] for values in groups.values())),
            },
        }
        cache = self._cache()
        if cache is not None:
            cache.update(
                (("authenticated_voucher_lines", row["id"], row["voucher_calculation_id"],
                  row["basis_calculation_id"]), True) for row in rows
            )
            cache[self._key("journal_summary")] = summary
            cache[self._key("journal_count")] = summary["count"]
            cache[self._key("journal_verified_rows")] = tuple(rows)
        return amounts

    def authenticated_selected_lines(self, rows):
        """Prove only selected financial-position vouchers and reversal originals.

        Whole-month money proofs publish the same successful line binding, so
        the current month can reuse it without decoding a result a second time.
        """
        from .integrity import _invalid, _lines

        rows = tuple(rows)
        self._verify_frozen_headers(rows=rows)
        reads, cache = self.snapshot.reads, self._cache()
        keys = {
            row["id"]: ("authenticated_voucher_lines", row["id"],
                        row["voucher_calculation_id"], row["basis_calculation_id"])
            for row in rows
        }
        pending = [row for row in rows if cache is None or keys[row["id"]] not in cache]
        originals = reads.vouchers(row["reverses_id"] for row in pending if row["reverses_id"])
        lines = reads.voucher_lines({row["id"] for row in rows} | originals.keys())
        if not pending:
            return lines
        original_headers = []
        if originals:
            query, parameters = selected_voucher_sql(
                self.snapshot.period, voucher_ids=originals,
            )
            original_headers = list(self.snapshot.connection.execute(query, parameters))
            if {row["id"] for row in original_headers} != originals.keys() or any(
                row["close_period"] is None for row in original_headers
            ):
                _invalid("voucher", self.snapshot.month, "reversal_frozen_source_missing")
            reads.verify_selected_voucher_adoptions(
                original_headers, through_period=self.snapshot.month,
            )
        used_rows = (*original_headers, *pending)
        identifiers = {
            row[field] for row in used_rows
            for field in ("voucher_calculation_id", "basis_calculation_id")
        }
        outcomes = reads.verify_selected_content(identifiers)
        owner_ids = {row["voucher_calculation_id"] for row in used_rows}
        publications = {
            row["calculation_id"]: row for row in self.snapshot.connection.execute(
                "SELECT p.* FROM json_each(?) ids JOIN calculation_publication p "
                "ON p.calculation_id=ids.value", (canonical(sorted(owner_ids)),),
            )
        }
        reads.verify_publication_records(publications.values())
        verified_originals = {}
        for row in used_rows:
            ident = row["id"]
            owner = row["voucher_calculation_id"]
            publication = publications.get(owner)
            if publication is None or publication["posting_period"] != row["period"]:
                _invalid("voucher", ident, "voucher_publication_mismatch")
            if row["reverses_id"]:
                original = originals[row["reverses_id"]]
                if original["period"] >= row["period"] or original["reverses_id"] is not None:
                    _invalid("voucher", ident, "invalid_reversal_source")
                expected = [
                    (account, credit, debit, cashflow)
                    for account, debit, credit, cashflow in verified_originals[original["id"]]
                ]
            else:
                if publication["voucher_id"] != row["voucher_id"]:
                    _invalid("voucher", ident, "voucher_publication_identity_mismatch")
                expected = _lines(outcomes[owner]["lines"], "calculation", owner)
            actual = _checked_posted_lines(row, lines[ident], expected)
            if ident in originals:
                verified_originals[ident] = actual
            if not row["reverses_id"]:
                adopted_id = row["basis_calculation_id"]
                if actual != _lines(outcomes[adopted_id]["lines"], "calculation", adopted_id):
                    _invalid("voucher", ident, "adopted_lines_mismatch")
        if cache is not None:
            cache.update((keys[row["id"]], True) for row in pending)
        return lines

    def kind_counts(self):
        cache, key = self._cache(), self._key("journal_summary")
        if cache is not None and key in cache:
            return [
                {"kind": kind, "reversal": reversal, "count": count}
                for kind, reversal, count, *_ in cache[key]["groups"]
            ]
        query, parameters = self.sql()
        self._verify_frozen_headers()
        return [
            dict(row)
            for row in self.snapshot.connection.execute(
                "SELECT basis_kind AS kind,reverses_id IS NOT NULL AS reversal,count(*) AS count "
                f"FROM ({query}) GROUP BY basis_kind,reverses_id IS NOT NULL",
                parameters,
            )
        ]


class Calculations(Mapping):
    """Selected business heads, loaded by the exact domain or subject requested."""

    def __init__(self, snapshot):
        self.snapshot, self.cache = snapshot, {}

    def selected(self, *, kinds=None, subjects=None, posting_period=None):
        key = (
            tuple(sorted(kinds)) if kinds is not None else None,
            tuple(sorted(subjects)) if subjects is not None else None,
            posting_period,
        )
        if key not in self.cache:
            selected = self.snapshot.queries._selected_accounting(
                self.snapshot.connection,
                subjects,
                self.snapshot.period,
                kinds=kinds,
                include_lines=False,
                posting_period=posting_period,
            )
            events = selected["through_period"]["voucher_events"]
            states = selected["through_period"]["state_results"]
            identifiers = {item["calculation_id"] for item in [*events, *states]}
            metadata = self.snapshot.reads.metadata(identifiers)
            heads = {}
            for record in metadata.values():
                old = heads.get(record["subject_id"])
                if old is None or (record["posting_period"], record["fact_revision"]) > (
                    old["posting_period"],
                    old["fact_revision"],
                ):
                    heads[record["subject_id"]] = record
            member_events = self.snapshot.asset_member_events(kinds=kinds, subjects=subjects)
            member_metadata = self.snapshot.reads.metadata(
                item["calculation_id"] for item in member_events
            )
            member_heads = {}
            for event in member_events:
                if event["direction"] < 0:
                    # A reversal removes exactly its adopted member, not a later
                    # replacement already selected through another batch.
                    old = member_heads.get(event["subject_id"])
                    if old and old["calculation_id"] == event["calculation_id"]:
                        member_heads.pop(event["subject_id"])
                    continue
                member_heads[event["subject_id"]] = event
            for subject, event in member_heads.items():
                record = member_metadata[event["calculation_id"]]
                old = heads.get(subject)
                if old is None or event["adoption_period"] >= old["posting_period"]:
                    heads[subject] = record
            views = {
                subject: self.snapshot.calculation(record["id"])
                for subject, record in heads.items()
            }
            self.cache[key] = views
        return self.cache[key]

    def __getitem__(self, key):
        if key is None:
            raise KeyError(key)
        return self.selected(subjects={key})[key]

    def __iter__(self):
        return iter(self.selected())

    def __len__(self):
        return len(self.selected())

    def values(self):
        return self.selected().values()


class FrozenCloseView:
    """A verified close handle; it does not imply all frozen bodies were decoded."""

    def __init__(self, reads, row):
        self.reads, self.row = reads, row
        self.header = reads.close_header(row)

    def section(self, name):
        return self.reads.close_section(self.row, name)


class ClosedPeriods(Mapping):
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.periods = tuple(
            row[0]
            for row in snapshot.connection.execute(
                "SELECT period FROM period_close WHERE period<=? ORDER BY period", (snapshot.month,)
            )
        )
        self.cache = {}

    def __getitem__(self, period):
        if period not in self.periods:
            raise KeyError(period)
        if period not in self.cache:
            reads = self.snapshot.reads
            row = reads.authoritative_close_rows(periods=(period,))[0]
            self.cache[period] = FrozenCloseView(reads, row)
        return self.cache[period]

    def __iter__(self):
        return iter(self.periods)

    def __len__(self):
        return len(self.periods)


class FrozenFacts:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def __contains__(self, ident):
        return (
            self.snapshot.connection.execute(
                "SELECT 1 FROM close_reference r WHERE r.close_period<=? AND "
                "((r.reference_type='fact' AND r.reference_id=?) OR "
                "(r.reference_type='calculation' AND EXISTS(SELECT 1 FROM calculation c "
                "WHERE c.id=r.reference_id AND c.fact_id=?)) OR "
                "(r.reference_type='calculation' AND EXISTS(SELECT 1 FROM dependency_fact d "
                "WHERE d.calculation_id=r.reference_id AND d.fact_id=?))) LIMIT 1",
                (self.snapshot.month, ident, ident, ident),
            ).fetchone()
            is not None
        )


def posted_account_totals(connection, cutoff, *, source="open", reads=None):
    """Shared account totals from a frozen baseline and stored posting increments."""
    row = connection.execute(
        "SELECT period,manifest,digest FROM period_close "
        "WHERE period<=? ORDER BY period DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    boundary = -1
    totals = {}
    if row is not None:
        boundary = row["period"]
        if reads is not None:
            close_row = reads.authoritative_close_rows(periods=[boundary])[0]
            trial_balance = reads.close_section(close_row, "trial_balance")
        else:
            from .close_storage import read_section, verified_header

            trial_balance = read_section(
                connection, verified_header(connection, row), "trial_balance"
            )
        for item in trial_balance:
            totals[item["account"]] = item["debit"] - item["credit"]
    if source == "open":
        for item in connection.execute(
            "SELECT account,sum(debit-credit) amount FROM ("
            "SELECT account,debit,credit FROM monthly_account WHERE period>? AND period<=? "
            "UNION ALL SELECT account,debit,credit FROM opening_account "
            "WHERE period>? AND period<=?) GROUP BY account",
            (boundary, cutoff, boundary, cutoff),
        ):
            totals[item["account"]] = totals.get(item["account"], 0) + item["amount"]
    return totals


def account_totals(snapshot):
    return posted_account_totals(snapshot.connection, snapshot.month, reads=snapshot.reads)
