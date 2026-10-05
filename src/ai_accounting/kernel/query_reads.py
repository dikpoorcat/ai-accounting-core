"""Exact, request-local batch reads shared by business queries and page projections.

Ordinary callers own their transaction. snapshot() owns a read-only transaction
and scopes successful reference checks to that lifetime. Consumers read shared
source objects without modifying them; selection rules stay in the business query.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import ChainMap
from contextlib import contextmanager, nullcontext

from . import stored_json
from .contracts import KernelError
from .domains.money import SETTLEMENT_PAYMENT_KINDS
from .query_semantics import (
    SETTLEMENT_SOURCE_SLOTS,
    report_party_needs_ancestry,
    resolve_calculation_relations,
)
from .storage import _active_fact_reads
from .stored_json import (
    DuplicateStoredKey,
    loads_unique,
    verify_sql_outcomes,
)
from .types import YearMonth, canonical, digest

SETTLEMENT_KINDS = frozenset(
    {
        *SETTLEMENT_SOURCE_SLOTS,
        "settlement",
        "overpayment",
        "opening_package",
    }
)

# Seek each distinct fact month through the index, rather than scanning every
# revision to discover months. A later close does not stand in for a missing
# earlier close. Both placeholders are the exact requested cutoff.
OPEN_FACT_PERIODS_CTE = (
    "WITH RECURSIVE periods(period) AS ("
    "SELECT min(period) FROM fact_revision WHERE period<=? UNION ALL "
    "SELECT (SELECT min(period) FROM fact_revision WHERE period>p.period AND period<=?) "
    "FROM periods p WHERE p.period IS NOT NULL), "
    "open_periods AS MATERIALIZED (SELECT period FROM periods p "
    "WHERE p.period IS NOT NULL AND NOT EXISTS("
    "SELECT 1 FROM period_close c WHERE c.period=p.period)) "
)


class _MaterialSqlOutcomeBatch:
    """Stage exact material outcome checks until the whole checker succeeds."""

    def __init__(self, reads, connection):
        self._reads = reads
        self._connection = connection
        self._snapshot_token = reads._snapshot_token
        self._identifiers = set()
        self._committed = False

    def _require_active(self):
        reads = self._reads
        if (
            self._committed
            or not reads._snapshot_active
            or reads.connection is not self._connection
            or reads._snapshot_token is not self._snapshot_token
        ):
            raise KernelError("content_integrity_failed", "资料结果核验快照已失效")

    def verify(self, row):
        self._require_active()
        ident = row["id"]
        if ident not in self._identifiers and ident not in self._reads._verified_sql_outcomes:
            # No caller-provided success flag: an exact earlier check in this
            # read snapshot, or this batch's own strict check, proves the row.
            stored_json.verify_outcome_bytes(row["stored_outcome"], row["result_digest"], ident)
        self._identifiers.add(ident)

    def commit(self):
        self._require_active()
        self._reads._verified_sql_outcomes.update(self._identifiers)
        self._committed = True


class _OpenVoucherScopeHeads:
    """Physical rows carried only from this owned scope's actual SQL read."""

    def __init__(self, reads, rows):
        self.reads = reads
        self.connection = reads.connection
        self.token = reads._snapshot_token
        self.rows = tuple(rows)

    def require(self, connection, publications, reads):
        if (
            reads is not self.reads
            or connection is not self.connection
            or reads._snapshot_token is not self.token
            or not _owns_current_selector_snapshot(reads, connection)
            or len(publications) != len(self.rows)
            or any(not isinstance(row, sqlite3.Row) for row in self.rows)
            or any(a is not b for a, b in zip(publications, self.rows, strict=True))
        ):
            raise KernelError("content_integrity_failed", "开放凭证头读取快照已失效")
        return self.rows


def _current_voucher_publication_headers(connection, identifiers):
    """Keep the independent ID lookup's missing/current/closed semantics."""
    if not identifiers:
        return []
    selected = list(connection.execute(
        "SELECT ids.value requested_id,v.id,v.voucher_id,v.calculation_id,"
        "v.reverses_id,v.period,c.subject_id,h.version_id current_version_id,"
        "closes.closed_through "
        "FROM json_each(?) ids LEFT JOIN voucher_version v ON v.id=ids.value "
        "LEFT JOIN calculation c ON c.id=v.calculation_id "
        "LEFT JOIN voucher_current h ON h.version_id=v.id "
        "CROSS JOIN (SELECT coalesce(max(period),-1) closed_through "
        "FROM period_close) closes",
        (canonical(sorted(identifiers)),),
    ))
    if len(selected) != len(identifiers) or any(row["id"] is None for row in selected):
        raise KernelError("content_integrity_failed", "选中凭证版本缺少权威来源")
    return selected


def verify_current_voucher_publications(connection, voucher_ids):
    """Prove formal adoption for selected, still-current open vouchers.

    A no-impact review keeps the old voucher version, so both its original
    publication and the subject's current reviewed publication must exist.
    Closed vouchers use their separate frozen adoption proof. Asset batch
    members have no independent voucher and never enter this selected set.
    """

    identifiers = set(voucher_ids)
    if not identifiers:
        return {}
    reads = _active_fact_reads.get()
    owned = _owns_current_selector_snapshot(reads, connection)
    cached = reads._verified_current_voucher_publications if owned else {}
    missing = {ident for ident in identifiers if ident not in cached}
    reusable = {}
    for ident in identifiers - missing:
        reusable.update(cached[ident][1])
    if not missing:
        return reusable
    selected = _current_voucher_publication_headers(connection, missing)
    current = [
        row
        for row in selected
        if row["current_version_id"] is not None and row["period"] > row["closed_through"]
    ]
    publications = _verify_current_voucher_publication_rows(connection, current)
    if owned:
        # The ID lookup also checked absent/current/closed filtering. Preserve
        # that exact empty result, without claiming a frozen adoption proof.
        active_ids = {row["id"] for row in current}
        cached.update((row["id"], (None, {})) for row in selected
                      if row["id"] not in active_ids)
    return reusable | publications


def verify_current_publication_voucher_heads(connection, publications):
    """Prove open publications' expected heads before candidate selection.

    This is a forward relationship check, not an outcome/adoption proof. A
    cleared result can keep its reserved voucher number without a current
    version; only that exceptional source requires its complete saved body.
    """
    publications = tuple(publications)
    if not publications:
        return
    reads = _active_fact_reads.get()
    owned = _owns_current_selector_snapshot(reads, connection)
    _verify_publication_voucher_head_batch(connection, publications, reads if owned else None)


def _verify_publication_voucher_head_batch(connection, publications, reads, scope_heads=None):
    """Publish record proofs only after this entire head batch succeeds."""
    pending_records = (
        {row["id"] for row in publications
         if row["id"] not in reads._verified_publication_ids}
        if reads is not None else set()
    )
    try:
        _verify_current_publication_voucher_heads(
            connection, publications, reads, scope_heads,
        )
    except Exception:
        # Authentication is provisional until every original/current head and
        # correction relationship in this selected batch succeeds. Only this
        # batch's new record proofs are withdrawn; earlier independent success
        # survives. The checkpoint is bounded by the input, not the whole cache.
        for ident in pending_records:
            reads._verified_publication_ids.pop(ident, None)
        raise


def _verify_current_publication_voucher_heads(connection, publications, reads, scope_heads=None):
    from .content_history_context import publication_reader

    owned = reads is not None
    if owned:
        reads.verify_publication_records(publications)
    else:
        for publication in publications:
            publication_reader().verify_record(publication)
    by_id = {row["id"]: row for row in publications}
    expected_calculations = {row["id"]: row["calculation_id"] for row in publications}
    review_ids = {row["id"] for row in publications if row["mode"] == "review_no_impact"}
    if review_ids:
        chains = list(connection.execute(
            "WITH RECURSIVE reviewed(root,id,previous_publication_id,mode) AS ("
            "SELECT p.id,p.id,p.previous_publication_id,p.mode FROM json_each(?) ids "
            "JOIN calculation_publication p ON p.id=ids.value UNION "
            "SELECT r.root,p.id,p.previous_publication_id,p.mode FROM reviewed r "
            "JOIN calculation_publication p ON p.id=r.previous_publication_id "
            "WHERE r.mode='review_no_impact') SELECT r.root,p.* FROM reviewed r "
            "JOIN calculation_publication p ON p.id=r.id",
            (canonical(sorted(review_ids)),),
        ))
        resolved = set()
        for row in chains:
            publication_reader().verify_record(row)
            current = by_id[row["root"]]
            if (row["subject_id"], row["posting_period"], row["voucher_id"]) != (
                current["subject_id"], current["posting_period"], current["voucher_id"]
            ):
                raise KernelError("content_integrity_failed", "无影响复核改变了凭证归属")
            if row["mode"] != "review_no_impact":
                expected_calculations[row["root"]] = row["calculation_id"]
                resolved.add(row["root"])
        if resolved != review_ids:
            raise KernelError("content_integrity_failed", "无影响复核缺少原始发布")
    selected = (
        scope_heads.require(connection, publications, reads)
        if scope_heads is not None else list(connection.execute(
            "SELECT p.id,p.calculation_id,p.voucher_id,p.posting_period,"
            "h.version_id,v.id version_exists,v.voucher_id version_voucher,"
            "v.period version_period,v.reverses_id,v.calculation_id version_calculation "
            "FROM json_each(?) ids LEFT JOIN calculation_publication p ON p.id=ids.value "
            "LEFT JOIN voucher_current h ON h.voucher_id=p.voucher_id "
            "LEFT JOIN voucher_version v ON v.id=h.version_id",
            (canonical(sorted(by_id)),),
        ))
    )
    if {row["id"] for row in selected} != by_id.keys():
        raise KernelError("content_integrity_failed", "正式发布来源缺失")
    versions, cleared = set(), set()
    for row in selected:
        publication = by_id[row["id"]]
        if (row["calculation_id"], row["voucher_id"], row["posting_period"]) != (
            publication["calculation_id"], publication["voucher_id"],
            publication["posting_period"],
        ):
            raise KernelError("content_integrity_failed", "正式发布与读取依据不一致")
        if row["voucher_id"] is None:
            continue
        if row["version_id"] is None:
            cleared.add(row["calculation_id"])
        elif (row["version_exists"] is None
              or row["version_voucher"] != row["voucher_id"]
              or row["version_period"] != row["posting_period"]
              or row["reverses_id"] is not None
              or row["version_calculation"] != expected_calculations[row["id"]]):
            raise KernelError("content_integrity_failed", "当前正式发布凭证头不匹配")
        else:
            versions.add(row["version_id"])
    if cleared:
        rows = list(connection.execute(
            "SELECT c.id,c.outcome,c.digest FROM json_each(?) ids "
            "LEFT JOIN calculation c ON c.id=ids.value",
            (canonical(sorted(cleared)),),
        ))
        if {row["id"] for row in rows} != cleared:
            raise KernelError("content_integrity_failed", "当前正式发布核算来源缺失")
        for row in rows:
            outcome = stored_json.verify_outcome_bytes(row["outcome"], row["digest"], row["id"])
            if (not isinstance(outcome, dict) or not isinstance(outcome.get("lines"), list)
                    or outcome["lines"]):
                raise KernelError("content_integrity_failed", "非零正式发布缺少当前凭证头")
    # A missing reversal version cannot define its own absence. The authenticated
    # correction segment independently determines the versions that must exist.
    # Same-month replacements may legitimately retain an earlier reversal.
    versions.update(publication_reader().verify_open_correction_publication_heads(
        connection, publications
    ))
    # No-impact review can adopt a version from the prior calculation. Retain
    # the original/current publication proof rather than equating their IDs.
    if scope_heads is None:
        verify_current_voucher_publications(connection, versions)
    else:
        # Only ordinary normal heads reuse the actual physical lookup above.
        # Review owners and independent correction reversals keep their ID read.
        normal, pending = _stage_open_scope_normal_relations(
            connection, publications, reads, scope_heads, versions,
        )
        independent = _current_voucher_publication_headers(
            connection, versions - normal,
        )
        # One relationship batch: a failed review/reversal must not leave the
        # ordinary heads newly marked successful before it is checked.
        _verify_current_voucher_publication_rows(connection, [
            row for row in independent
            if row["current_version_id"] is not None
            and row["period"] > row["closed_through"]
        ])
        reads._verified_current_voucher_publications.update(pending)


def _stage_open_scope_normal_relations(connection, publications, reads, scope_heads, versions):
    """Stage normal reverse pairs proved by this exact terminal/current SQL.

    The carrier owns actual rows and their terminal selection in this snapshot.
    Record authentication and the opposite missing-source guard precede this
    call; neither a caller flag nor a source-content proof supplies the relation.
    """
    from .publication import CONTENT_FIELDS

    normal, pending = set(), {}
    for row in scope_heads.require(connection, publications, reads):
        if row["version_id"] not in versions or row["mode"] == "review_no_impact":
            continue
        if (
            row["subject_id"] is None
            or row["version_subject"] != row["subject_id"]
            or row["current_id"] != row["calculation_id"]
            or row["version_calculation"] != row["calculation_id"]
            or row["version_exists"] != row["version_id"]
            or row["version_voucher"] != row["voucher_id"]
            or row["version_period"] != row["posting_period"]
            or row["reverses_id"] is not None
            or reads._verified_publication_ids.get(row["id"]) != row["posting_period"]
        ):
            raise KernelError("content_integrity_failed", "当前凭证缺少正式发布采用")
        signature = (
            row["version_id"], row["version_voucher"], row["version_calculation"],
            row["reverses_id"], row["version_period"], row["version_subject"],
        )
        cached = reads._verified_current_voucher_publications.get(row["version_id"])
        if cached is not None:
            if cached[0] != signature:
                raise KernelError("content_integrity_failed", "凭证头与本次已核内容不一致")
        else:
            # Match the independent p.*,has_successor contract exactly. Physical
            # source/head support columns belong only to the private carrier.
            publication = {key: row[key] for key in ("id", *CONTENT_FIELDS)}
            publication["has_successor"] = 0
            pending[row["version_id"]] = (
                signature, {row["calculation_id"]: publication},
            )
        normal.add(row["version_id"])
    return normal, pending


def verify_open_voucher_scope(connection, through_period, *, posting_period=None,
                              subject_ids=None):
    """Independently locate open terminal sources before narrowing vouchers."""
    # Fixed released readers own their historical rules. The corresponding v1
    # source boundary must use its fixed implementation, never current models.
    from . import publication
    from .content_history_context import publication_reader

    if publication_reader() is not publication:
        return
    if subject_ids is not None:
        # An explicit empty selector has no sources in either direction. Keep
        # None as the independent whole-prefix check, and consume an iterable
        # once so both publication and voucher queries see the same IDs.
        subject_ids = frozenset(subject_ids)
        if not subject_ids:
            return
    reads = _active_fact_reads.get()
    owned = subject_ids is None and _owns_current_selector_snapshot(reads, connection)
    scopes = reads._verified_open_voucher_scopes if owned else {}
    if any(scopes.get(key, -1) >= through_period for key in (None, posting_period)):
        return
    closed_through = connection.execute(
        "SELECT coalesce(max(period),-1) FROM period_close"
    ).fetchone()[0]
    restrictions = [
        "p.posting_period<=?",
        "p.posting_period>?",
        "NOT EXISTS(SELECT 1 FROM calculation_publication later "
        "WHERE later.previous_publication_id=p.id)",
    ]
    parameters = [through_period, closed_through]
    if posting_period is not None:
        restrictions.append("p.posting_period=?")
        parameters.append(posting_period)
    if subject_ids is not None:
        restrictions.append("p.subject_id IN (SELECT value FROM json_each(?))")
        parameters.append(canonical(sorted(set(subject_ids))))
    rows = list(connection.execute(
        "SELECT p.*,a.calculation_id current_id,c.id calculation_exists,"
        "c.subject_id calculation_subject,c.kind calculation_kind,c.period source_period,"
        "f.id fact_exists,f.subject_id fact_subject,f.period fact_period,s.kind fact_kind "
        + (",h.version_id,v.id version_exists,v.voucher_id version_voucher,"
           "v.period version_period,v.reverses_id,v.calculation_id version_calculation,"
           "vc.subject_id version_subject " if owned else "")
        + "FROM calculation_publication p "
        "LEFT JOIN calculation_current a ON a.subject_id=p.subject_id "
        "LEFT JOIN calculation c ON c.id=p.calculation_id "
        "LEFT JOIN fact_revision f ON f.id=c.fact_id LEFT JOIN subject s ON s.id=f.subject_id "
        + ("LEFT JOIN voucher_current h ON h.voucher_id=p.voucher_id "
           "LEFT JOIN voucher_version v ON v.id=h.version_id "
           "LEFT JOIN calculation vc ON vc.id=v.calculation_id " if owned else "")
        + "WHERE " + " AND ".join(restrictions), parameters,
    ))
    publications = []
    for row in rows:
        # The heads checker authenticates the complete owned non-withdrawn
        # batch below. Withdrawals never enter it and need their own digest.
        # Failed source selection cannot publish a record or relationship proof.
        if not owned or row["mode"] == "withdrawn":
            publication_reader().verify_record(row)
        if row["mode"] == "withdrawn":
            if (row["calculation_id"] is not None or row["voucher_id"] is not None
                    or row["current_id"] is not None):
                raise KernelError("content_integrity_failed", "撤去发布与当前业务头不一致")
            continue
        if (row["calculation_exists"] is None or row["fact_exists"] is None
                or row["current_id"] != row["calculation_id"]
                or row["calculation_subject"] != row["subject_id"]
                or (row["calculation_subject"], row["calculation_kind"], row["source_period"])
                != (row["fact_subject"], row["fact_kind"], row["fact_period"])):
            raise KernelError("content_integrity_failed", "正式发布缺少精确当前核算来源")
        publications.append(row)
    # The opposite direction keeps a removed publication/current calculation
    # from disappearing out of the terminal-publication driver. This bounded
    # physical open-voucher range is independent of the consumer's account or
    # business filter, and reads no historical result bodies.
    voucher_restrictions = [
        "v.period<=?",
        "v.period>?",
        "(c.id IS NULL OR a.calculation_id IS NULL OR p.id IS NULL "
        "OR p.subject_id IS NOT c.subject_id OR p.posting_period IS NOT v.period)",
    ]
    voucher_parameters = [through_period, closed_through]
    if posting_period is not None:
        voucher_restrictions.append("v.period=?")
        voucher_parameters.append(posting_period)
    if subject_ids is not None:
        voucher_restrictions.append("c.subject_id IN (SELECT value FROM json_each(?))")
        voucher_parameters.append(canonical(sorted(set(subject_ids))))
    missing = connection.execute(
        "SELECT v.id FROM voucher_version v INDEXED BY voucher_period "
        "CROSS JOIN voucher_current h ON h.version_id=v.id "
        "LEFT JOIN calculation c ON c.id=v.calculation_id "
        "LEFT JOIN calculation_current a ON a.subject_id=c.subject_id "
        "LEFT JOIN calculation_publication p ON p.calculation_id=a.calculation_id "
        "WHERE " + " AND ".join(voucher_restrictions) + " LIMIT 1", voucher_parameters,
    ).fetchone()
    if missing is not None:
        raise KernelError("content_integrity_failed", "当前开放凭证缺少正式发布来源")
    if owned:
        publications = tuple(publications)
        _verify_publication_voucher_head_batch(
            connection, publications, reads, _OpenVoucherScopeHeads(reads, publications),
        )
    else:
        verify_current_publication_voucher_heads(connection, publications)
    if owned:
        scopes[posting_period] = max(through_period, scopes.get(posting_period, -1))
        if posting_period == through_period and closed_through == through_period - 1:
            # The actual closed boundary makes the exact-month range equal to
            # the entire open prefix. Request labels alone cannot prove this.
            scopes[None] = max(through_period, scopes.get(None, -1))


def _owns_current_selector_snapshot(reads, connection):
    """Limit selector-header reuse to the active current reader and connection."""
    from . import close_storage, publication
    from .content_history_context import close_reader, publication_reader

    return (
        isinstance(reads, QueryReads)
        and reads.connection is connection
        and reads._snapshot_active
        and connection.in_transaction
        and _active_fact_reads.get() is reads
        and getattr(reads.store.registry, "content_version", None) != 1
        and close_reader() is close_storage
        and publication_reader() is publication
    )


def _selected_current_voucher_publications(reads, rows, *, period, cutoff):
    """Reuse actual open-month selector headers in their owned current snapshot.

    The selector's checked absence of all frozen references and its actual
    current-version predicate prove the omitted header lookup. Other scopes
    retain the independent ID entry point; publication relationships below
    still come from their own physical records.
    """
    if (
        not _owns_current_selector_snapshot(reads, getattr(reads, "connection", None))
        or reads._report_snapshot_cache.get(("open_voucher_period", cutoff, period)) is not True
        or any(
            not isinstance(row, sqlite3.Row)
            or row["period"] != period
            or row["close_period"] is not None
            for row in rows
        )
    ):
        return None
    closed_through = reads.connection.execute(
        "SELECT coalesce(max(period),-1) FROM period_close"
    ).fetchone()[0]
    current = [
        {"id": row["id"], "voucher_id": row["voucher_id"],
         "calculation_id": row["calculation_id"], "reverses_id": row["reverses_id"],
         "period": row["period"], "subject_id": row["voucher_subject_id"]}
        for row in rows if row["period"] > closed_through
    ]
    return _verify_current_voucher_publication_rows(reads.connection, current)


def _verify_current_voucher_publication_rows(connection, current):
    """Prove original/current publication relations for physical current heads."""
    if not current:
        return {}
    reads = _active_fact_reads.get()
    owned = _owns_current_selector_snapshot(reads, connection)
    cached = reads._verified_current_voucher_publications if owned else {}
    reusable, pending = {}, []
    pending_signatures = []
    for row in current:
        proof = cached.get(row["id"])
        signature = _current_voucher_signature(row)
        if proof is None:
            pending.append(row)
            if owned:
                pending_signatures.append(signature)
        elif proof[0] != signature:
            raise KernelError("content_integrity_failed", "凭证头与本次已核内容不一致")
        else:
            reusable.update(proof[1])
    current = pending
    if not current:
        return reusable
    subjects = {row["subject_id"] for row in current}
    if None in subjects:
        raise KernelError("content_integrity_failed", "当前凭证缺少业务身份")
    heads = dict(
        connection.execute(
            "SELECT a.subject_id,a.calculation_id FROM json_each(?) ids "
            "CROSS JOIN calculation_current a ON a.subject_id=ids.value",
            (canonical(sorted(subjects)),),
        )
    )
    calculation_ids = {row["calculation_id"] for row in current} | set(heads.values())
    publications = {
        row["calculation_id"]: row
        for row in connection.execute(
            "SELECT p.*,EXISTS(SELECT 1 FROM calculation_publication later "
            "WHERE later.previous_publication_id=p.id) has_successor "
            "FROM json_each(?) ids CROSS JOIN calculation_publication p "
            "ON p.calculation_id=ids.value",
            (canonical(sorted(calculation_ids)),),
        )
    }
    from .publication import verify_record

    # A balance or settlement read may already have authenticated the complete
    # publication rows for these periods. Reuse only that controlled read
    # snapshot's successful digest proof; selection and head checks still run.
    verified_publications = (
        {
            item["id"]
            for period in {row["period"] for row in current}
            for item in reads._verified_publications.get(period, ())
        }
        if reads is not None
        and reads.connection is connection
        and reads._snapshot_active
        and connection.in_transaction
        else set()
    )
    if (
        reads is not None
        and reads.connection is connection
        and reads._snapshot_active
        and connection.in_transaction
    ):
        verified_publications.update(
            publication["id"]
            for publication in publications.values()
            if publication["id"] in reads._verified_publication_ids
            and reads._verified_publication_ids[publication["id"]] == publication["posting_period"]
        )
    for row in current:
        head_id = heads.get(row["subject_id"])
        original = publications.get(row["calculation_id"])
        head = publications.get(head_id)
        if (
            head_id is None
            or original is None
            or head is None
            or any(
                publication["subject_id"] != row["subject_id"]
                or publication["posting_period"] != row["period"]
                for publication in (original, head)
            )
            or head["has_successor"]
            or (
                row["reverses_id"] is None
                and (
                    original["voucher_id"] != row["voucher_id"]
                    or head["voucher_id"] != row["voucher_id"]
                )
            )
        ):
            raise KernelError("content_integrity_failed", "当前凭证缺少正式发布采用")
        for publication in (original, head):
            if publication["id"] not in verified_publications:
                verify_record(publication)
                verified_publications.add(publication["id"])
    if (
        reads is not None
        and reads.connection is connection
        and reads._snapshot_active
        and connection.in_transaction
    ):
        # Selection and every record succeeded. The exact identity is reusable,
        # while later callers still check their own source/head relationships.
        reads._verified_publication_ids.update(
            (publication["id"], publication["posting_period"])
            for publication in publications.values()
            if publication["id"] in verified_publications
        )
    if owned:
        # Each version owns only its actual original/current pair, not a copy
        # of the complete batch. Publish after every relationship and digest
        # succeeds; a failed batch never publishes partial relationship proof.
        cached.update((row["id"], (
            signature,
            {ident: publications[ident] for ident in
             {row["calculation_id"], heads[row["subject_id"]]}},
        )) for row, signature in zip(current, pending_signatures, strict=True))
    return reusable | publications


def _current_voucher_signature(row):
    return (
        row["id"], row["voucher_id"], row["calculation_id"],
        row["reverses_id"], row["period"], row["subject_id"],
    )


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
    no_close_references=False,
):
    """Composable exact selection driven by bounded voucher candidates.

    Outer predicates and LIMIT may be applied without materializing a global
    close-reference grouping. Hit close_period values require local verification.
    no_close_references requires a same-transaction absence check for every
    candidate voucher through the cutoff; callers must not infer it from an
    incomplete derived directory.
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
            "CROSS JOIN subject ss INDEXED BY subject_kind ON ss.kind=selected_kinds.value "
            "CROSS JOIN calculation sc INDEXED BY calculation_subject "
            "ON sc.subject_id=ss.id CROSS JOIN voucher_version original "
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
    elif no_close_references:
        close_period = "NULL"
        frozen_join = ""
    else:
        close_period = (
            "(SELECT min(r.close_period) FROM close_reference r WHERE r.reference_type='voucher' "
            "AND r.reference_id=v.id AND r.close_period<=?)"
        )
        frozen_join = ""
    sql = (
        prefix + "candidates AS (SELECT v.*,n.number,vc.subject_id AS voucher_subject_id,"
        f"{close_period} AS close_period "
        f"FROM {driver}{frozen_join} CROSS JOIN voucher n ON n.id=v.voucher_id "
        "CROSS JOIN calculation vc ON vc.id=v.calculation_id WHERE v.period<=?"
    )
    parameters.extend(
        (cutoff, cutoff)
        if authoritative_vouchers is None and not no_close_references
        else (cutoff,)
    )
    if posting_period is not None:
        sql += " AND v.period=?"
        parameters.append(YearMonth(posting_period).ordinal)
    if posting_start is not None:
        sql += " AND v.period>=?"
        parameters.append(YearMonth(posting_start).ordinal)
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
        subjects = canonical(sorted({subject_ids} if isinstance(subject_ids, str) else subject_ids))
        parameters.extend((subjects, subjects))
    if kinds is not None:
        # A calculation header has not yet been authenticated. Its kind must
        # not hide a voucher from the consumer that proves that header.
        sql += (
            " AND EXISTS(SELECT 1 FROM subject selected_subject "
            "WHERE selected_subject.id=vc.subject_id "
            "AND selected_subject.kind IN (SELECT value FROM json_each(?)))"
        )
        parameters.append(canonical(sorted(kinds)))
    # A proven open scope or explicit current-head read needs this lookup for
    # every selected voucher.  Resolve it once per candidate instead of
    # repeating the correlated scalar lookup when the CTE is flattened into
    # downstream line and calculation joins.  Both joined keys are unique.
    current_basis_join = current_heads or no_close_references
    basis = (
        "coalesce(current_publication.calculation_id,v.calculation_id)"
        if current_basis_join
        else "coalesce((SELECT a.calculation_id FROM calculation_current a "
        "JOIN calculation_publication p ON p.calculation_id=a.calculation_id "
        "WHERE a.subject_id=v.voucher_subject_id AND p.voucher_id=v.voucher_id "
        "AND p.posting_period=v.period),v.calculation_id)"
    )
    current_join = (
        " LEFT JOIN calculation_current current_basis "
        "ON current_basis.subject_id=v.voucher_subject_id "
        "LEFT JOIN calculation_publication current_publication "
        "ON current_publication.calculation_id=current_basis.calculation_id "
        "AND current_publication.voucher_id=v.voucher_id "
        "AND current_publication.posting_period=v.period"
        if current_basis_join
        else ""
    )
    sql += (
        "), selected AS (SELECT v.*,CASE WHEN v.close_period IS NOT NULL "
        "THEN 'close_manifest' ELSE 'current_publication' END AS selection_source,"
        "v.calculation_id AS voucher_calculation_id,CASE WHEN v.reverses_id IS NOT NULL "
        "THEN original.calculation_id WHEN (? OR v.close_period IS NULL) "
        "THEN " + basis + " ELSE v.calculation_id END "
        "AS basis_calculation_id FROM candidates v LEFT JOIN voucher_version original "
        "ON original.id=v.reverses_id"
        + current_join
        + " WHERE v.close_period IS NOT NULL OR (EXISTS("
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


def _selected_voucher_path_references(connection, frozen):
    """Keep malformed type prefixes visible at the exact voucher adoption path."""
    return connection.execute(
        "WITH RECURSIVE types(value) AS ("
        "SELECT min(reference_type) FROM close_reference UNION ALL "
        "SELECT (SELECT min(reference_type) FROM close_reference "
        "WHERE reference_type>types.value) FROM types WHERE types.value IS NOT NULL"
        "), wanted(id,close_period) AS MATERIALIZED ("
        "SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)"
        ") SELECT r.* FROM types CROSS JOIN wanted "
        "CROSS JOIN close_reference r INDEXED BY close_reference_lookup "
        "ON r.reference_type=types.value AND r.reference_id=wanted.id "
        "AND r.close_period=wanted.close_period WHERE r.path='vouchers[*].id' "
        "AND r.reference_type<>'voucher'",
        (canonical(sorted([ident, period] for ident, period in frozen.items())),),
    ).fetchall()


class QueryReads:
    @staticmethod
    def _stored_outcome(raw):
        try:
            return loads_unique(raw)
        except DuplicateStoredKey as exc:
            raise KernelError(
                "content_integrity_failed", "已保存的核算结果 JSON 有重复字段"
            ) from exc

    def __init__(self, engine, connection):
        self.engine, self.store, self.connection = engine, engine.store, connection
        self._reset()

    def _reset(self):
        self._snapshot_active = False
        self._snapshot_token = object()
        self._verified_close_references = set()
        self._verified_close_storage_parts = {}
        self._verified_frozen_voucher_headers = {}
        self._verified_selected_voucher_adoptions = set()
        self._verified_settlement_periods = set()
        self._settlement_history_summaries = {}
        self._frozen_settlement_scopes = {}
        self._frozen_settlement_roots = {}
        self._frozen_settlement_blocks = {}
        self._frozen_settlement_states = {}
        self._frozen_settlement_tails = {}
        self._verified_balance_scopes = set()
        self._verified_balance_period_scopes = set()
        self._verified_publications = {}
        self._verified_publication_ids = {}
        self._verified_open_voucher_scopes = {}
        self._verified_current_voucher_publications = {}
        self._frozen_asset_owner_discovery = None
        self._fact_versions = {}
        self._raw_fact_data = {}
        self._facts = {}
        self._metadata = {}
        self._calculations = {}
        self._raw_calculation_outcomes = {}
        self._raw_calculations = {}
        self._parents = {}
        self._vouchers = {}
        self._lines = {}
        self._relations = {}
        self._relation_sources = {}
        self._report_direct_relations = {}
        self._verified_source_contents = {}
        self._verified_saved_input_identities = set()
        self._anchored_source_bytes = {}
        self._verified_sql_outcomes = set()
        self._raw_relation_sources = {}
        self._ancestor_roots = set()
        self._raw_ancestor_roots = set()
        self._closes = {}
        self._authoritative_closes = {}
        self._close_headers = {}
        self._close_manifests = {}
        self._close_accounting_slices = {}
        self._close_adopted_result_slices = {}
        self._close_accounting_positions = {}
        self._close_sections = {}
        self._report_snapshot_cache = {}
        self._selections = {}
        self._typed_calculations = {}
        self._jobs = {}
        self._asset_members = {}
        self.job_plans = {}

    @classmethod
    @contextmanager
    def snapshot(cls, engine):
        """Own one read-only transaction; no memo survives an exit or transaction change."""
        from .runtime import _OwnedReadConnection, _owned_read_transaction

        with engine.store._snapshot_connection() as connection:
            owned = type(connection) is _OwnedReadConnection
            if not owned:
                connection.execute("BEGIN")

            def keep_snapshot(action, *_):
                # in_transaction alone cannot distinguish COMMIT followed by BEGIN.
                # This connection is private to this scope; its consumers only read.
                return (
                    sqlite3.SQLITE_DENY
                    if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT)
                    else sqlite3.SQLITE_OK
                )

            transaction = _owned_read_transaction(connection) if owned else nullcontext()
            with transaction:
                reads = cls(engine, connection)
                if not owned:
                    connection.set_authorizer(keep_snapshot)
                reads._snapshot_active = True
                token = _active_fact_reads.set(reads)
                try:
                    yield reads
                finally:
                    _active_fact_reads.reset(token)
                    reads._reset()
                    if not owned:
                        connection.set_authorizer(None)

    def verify_close_references(self, references):
        """Reuse exact successful leaf checks, never an independent adoption proof."""
        from .read_indexes import verify_close_references

        references = list(references)
        if not self._snapshot_active:
            return verify_close_references(self.connection, references)
        fields = (
            "close_period",
            "path",
            "position",
            "reference_type",
            "reference_id",
            "related_id",
        )
        pending, keys = [], set()
        for reference in references:
            # Types are part of identity too: e.g. False must not hit a verified 0.
            values = tuple(reference[field] for field in fields)
            # One flat key retains the same type-sensitive identity without six
            # extra tuples per historical reference in the snapshot cache.
            key = tuple(map(type, values)) + values
            if key not in self._verified_close_references and key not in keys:
                pending.append(reference)
                keys.add(key)
        if pending:
            verified = {
                period: self._close_manifests[period]
                for period in {reference["close_period"] for reference in pending}
                if period in self._close_manifests
                and (period in self._closes or period in self._authoritative_closes)
            }
            verify_close_references(
                self.connection,
                pending,
                _verified_manifests=verified,
                _verified_headers=self._close_headers,
                _verified_storage_parts=self._verified_close_storage_parts,
            )
            self._verified_close_references.update(keys)

    def verify_selected_voucher_adoptions(self, rows, *, through_period):
        """Bind selected live voucher headers to their exact frozen adoption.

        Narrow projections supply id/close_period; their actual complete heads
        are read in one batch. A basis calculation is never the original voucher
        calculation. This proves returned heads, not a caller's selection scope.
        """
        from . import close_storage
        from .content_history_context import close_reader
        from .read_indexes import selected_voucher_references, verify_close_references

        rows = tuple(rows)
        frozen = {row["id"]: row["close_period"] for row in rows if row["close_period"] is not None}
        if not frozen:
            return
        active = _owns_current_selector_snapshot(self, self.connection)
        scope_keys = {
            ident: (ident, period, type(through_period), through_period)
            for ident, period in frozen.items()
        }
        requested = {
            ident: period for ident, period in frozen.items()
            if not active or scope_keys[ident] not in self._verified_selected_voucher_adoptions
            or (period, ident) not in self._verified_frozen_voucher_headers
        }
        references = selected_voucher_references(
            self.connection, sorted(requested), through_period=through_period
        )
        if requested:
            references.extend(_selected_voucher_path_references(self.connection, requested))
        fresh_parts, fresh_headers = {}, {}
        parts = ChainMap(fresh_parts, self._verified_close_storage_parts if active else {})
        headers = ChainMap(fresh_headers, self._close_headers if active else {})
        fields = (
            "close_period", "path", "position", "reference_type", "reference_id", "related_id",
        )
        keys, pending = set(), []
        for reference in references:
            values = tuple(reference[field] for field in fields)
            key = tuple(map(type, values)) + values
            if not active or key not in self._verified_close_references:
                if key not in keys:
                    pending.append(reference)
                    keys.add(key)
        # Stage mirror, bucket and header success until every live head binds.
        verify_close_references(
            self.connection, pending,
            _verified_manifests=self._close_manifests if active else None,
            _verified_headers=headers, _verified_storage_parts=parts,
        )
        found = {(row["reference_id"], row["close_period"]) for row in references}
        if any((ident, period) not in found for ident, period in requested.items()):
            raise KernelError("content_integrity_failed", "冻结凭证采用来源缺失")
        cached = self._verified_frozen_voucher_headers if active else {}
        missing = {
            ident: period for ident, period in frozen.items() if (period, ident) not in cached
        }
        staged = {}
        if missing:
            actual = {row["id"]: dict(row) for row in self.connection.execute(
                "SELECT v.id,v.voucher_id,v.calculation_id,v.period,v.reverses_id,v.total,n.number "
                "FROM json_each(?) ids JOIN voucher_version v ON v.id=ids.value "
                "JOIN voucher n ON n.id=v.voucher_id",
                (canonical(sorted(missing)),),
            )}
            if actual.keys() != missing.keys():
                raise KernelError("content_integrity_failed", "冻结凭证实际来源缺失")
            reader = close_reader()
            periods = {period for period in missing.values() if period not in headers}
            close_rows = list(self.connection.execute(
                "SELECT p.* FROM json_each(?) periods "
                "JOIN period_close p ON p.period=periods.value",
                (canonical(sorted(periods)),),
            )) if periods else []
            if {row["period"] for row in close_rows} != periods:
                raise KernelError("content_integrity_failed", "冻结凭证关账来源缺失")
            checked = (
                close_storage.verified_headers(self.connection, close_rows)
                if reader is close_storage else
                tuple(reader.verified_header(self.connection, row) for row in close_rows)
            )
            headers.update((header.period, header) for header in checked)
            selected = {}
            for reference in references:
                ident, period = reference["reference_id"], reference["close_period"]
                if (ident in missing and period == missing[ident]
                        and reference["path"] == "vouchers[*].id"):
                    selected.setdefault(period, []).append(
                        (reference["path"], int(reference["position"]), ident)
                    )
            if {(period, ident) for period, refs in selected.items() for _, _, ident in refs} != {
                (period, ident) for ident, period in missing.items()
            }:
                raise KernelError("content_integrity_failed", "冻结凭证精确采用来源缺失")
            if reader is close_storage:
                leaves = close_storage.voucher_reference_headers(
                    self.connection, [(headers[period], refs) for period, refs in selected.items()],
                    parts=parts,
                )
            else:
                # Fixed readers retain their decoder and complete section rules.
                leaves = {}
                for period, refs in selected.items():
                    vouchers = reader.read_section(self.connection, headers[period], "vouchers")
                    for _, position, ident in refs:
                        item = vouchers[position]
                        if item["id"] != ident:
                            raise KernelError("content_integrity_failed", "冻结凭证精确采用不匹配")
                        leaves[period, ident] = item
            for ident, period in missing.items():
                live, leaf = actual[ident], leaves[period, ident]
                if live["period"] != period or any(
                    live[field] != leaf[field]
                    for field in (
                        "id", "voucher_id", "calculation_id", "number", "total", "reverses_id",
                    )
                ):
                    raise KernelError(
                        "content_integrity_failed", "实际凭证头与冻结采用不一致",
                        component="voucher", record_id=ident,
                        reason="frozen_voucher_header_mismatch",
                    )
                staged[period, ident] = live
        for row in rows:
            if row["close_period"] is None:
                continue
            live = staged.get((row["close_period"], row["id"])) or cached.get(
                (row["close_period"], row["id"])
            )
            if live is None or any(
                row[field] != live[field]
                for field in ("voucher_id", "number", "total", "reverses_id", "period")
                if field in row.keys()
            ) or (
                "voucher_calculation_id" in row.keys()
                and row["voucher_calculation_id"] != live["calculation_id"]
            ):
                raise KernelError("content_integrity_failed", "所选凭证头与实际来源不一致")
        if active:
            self._verified_close_references.update(keys)
            self._verified_close_storage_parts.update(fresh_parts)
            self._close_headers.update(fresh_headers)
            self._verified_frozen_voucher_headers.update(staged)
            self._verified_selected_voucher_adoptions.update(scope_keys.values())

    def verify_settlement_periods(self, periods):
        """Reuse successful period seals only within this read snapshot."""
        from .settlement_projection import verify_settlement_periods

        periods = set(periods)
        pending = periods - self._verified_settlement_periods
        if not pending:
            return
        verify_settlement_periods(self.connection, pending, reads=self)
        if self._snapshot_active:
            self._verified_settlement_periods.update(pending)

    def verify_publication_periods(self, periods):
        """Verify immutable publication identities once in this read snapshot."""
        from . import close_storage, publication
        from .content_history_context import close_reader, publication_reader

        periods = set(periods)
        active = (
            self._snapshot_active and self.connection.in_transaction
            and _active_fact_reads.get() is self
            and getattr(self.store.registry, "content_version", None) != 1
            and close_reader() is close_storage and publication_reader() is publication
        )
        cached = self._verified_publications if active else {}
        pending = {period for period in periods if period not in cached}
        grouped = {period: [] for period in pending}
        if pending:
            headers = (
                publication._verified_period_headers(
                    self.connection, pending, self._verified_publication_ids
                ) if active else publication.verified_period_headers(self.connection, pending)
            )
            for value in headers:
                grouped[value["posting_period"]].append(value)
            if active:
                self._verified_publications.update(
                    {period: tuple(rows) for period, rows in grouped.items()}
                )
                self._verified_publication_ids.update(
                    (row["id"], row["posting_period"]) for rows in grouped.values() for row in rows
                )
        return [
            row
            for period in sorted(periods)
            for row in cached.get(period, grouped.get(period, ()))
        ]

    def verify_publication_records(self, rows):
        """Reuse an exact record digest, never publication selection or adoption."""
        from .content_history_context import publication_reader

        rows = tuple(rows)
        active = self._snapshot_active and self.connection.in_transaction
        verified = self._verified_publication_ids if active else {}
        pending = {}
        for row in rows:
            ident, period = row["id"], row["posting_period"]
            if ident in verified:
                if verified[ident] != period:
                    raise KernelError("content_integrity_failed", "正式发布期间与已核内容不一致")
                continue
            publication_reader().verify_record(row)
            pending[ident] = period
        if active:
            self._verified_publication_ids.update(pending)

    def verify_open_voucher_scope(self, through_period, *, posting_period=None):
        """Reuse only a complete scope check in this owned read-only snapshot."""
        verify_open_voucher_scope(
            self.connection, through_period, posting_period=posting_period
        )

    def verify_balance_scope(self, through_period, category=None):
        """Cache only a successful seal/source check in this snapshot."""
        from .period_balances import verify_selected_balances

        key = (through_period, category)
        if key in self._verified_balance_scopes or any(
            verified_category == category and verified_period >= through_period
            for verified_period, verified_category in self._verified_balance_scopes
        ):
            return
        verify_selected_balances(self.connection, through_period, category, reads=self)
        if self._snapshot_active:
            self._verified_balance_scopes.add(key)

    def verify_balance_periods(self, through_period, category, periods):
        """Reuse only this exact, successfully verified open-tail scope."""
        from .period_balances import verify_selected_balances

        selected = frozenset(periods)
        key = (through_period, category, selected)
        if self._snapshot_active and key in self._verified_balance_period_scopes:
            return
        verify_selected_balances(
            self.connection, through_period, category, periods=selected, reads=self
        )
        if self._snapshot_active:
            self._verified_balance_period_scopes.add(key)

    def fact_versions(self, identifiers):
        identifiers = set(identifiers)
        missing = {ident for ident in identifiers if ident not in self._fact_versions}
        if missing:
            self._fact_versions.update(self.store.facts(self.connection, missing))
        return {ident: self._fact_versions[ident] for ident in identifiers}

    def fact_version(self, ident):
        return self.fact_versions((ident,))[ident]

    def prime_select(self, reads):
        """Batch the existing Read contract without changing scope or version choice."""
        requested = list(dict.fromkeys(read for read in reads if read not in self._selections))
        selections = self.store.select_many(
            self.connection, requested, fact_loader=self.fact_versions
        )
        self._selections.update(selections)
        for read, values in selections.items():
            if read.source == "fact":
                self._fact_versions.update((value.id, value) for value in values)
            else:
                self._typed_calculations.update((value.id, value) for value in values)

    def select(self, read):
        self.prime_select((read,))
        return self._selections[read]

    def facts(self, identifiers):
        identifiers = set(identifiers)
        for ident, version in self.fact_versions(identifiers).items():
            if ident not in self._facts:
                self._facts[ident] = {
                    "id": version.id,
                    "subject_id": version.subject_id,
                    "revision": version.revision,
                    "kind": version.fact.kind,
                    "period": str(version.fact.period),
                    "data": version.fact.model_dump(mode="json"),
                    "evidence": list(version.evidence),
                }
        return {ident: self._facts[ident] for ident in identifiers}

    def fact(self, ident):
        return self.facts((ident,))[ident]

    def verify_fact_versions(self, identifiers):
        """Authenticate exact raw facts, then reuse this call's checked inputs.

        Typed presentation is built only after the original stored data, child
        order, identity, seals and evidence have passed the ordinary verifier.
        A model dump is never used to authenticate the stored source. Released
        v1 and unowned reads retain their existing storage adapter path.
        """
        from .contracts import FactVersion
        from .integrity import verify_sources
        from .storage import _validation_json

        identifiers = set(identifiers)
        checked = verify_sources(
            self.engine, self.connection, fact_ids=identifiers, _return_facts=True
        )
        if (
            not self._snapshot_active or not self.connection.in_transaction
            or getattr(self.store.registry, "content_version", None) == 1
        ):
            return
        staged = {}
        for ident, row in checked.items():
            model = self.store.registry.models[row["kind"]]
            staged[ident] = FactVersion(
                ident, row["subject_id"], row["revision"],
                model.model_validate_json(_validation_json(row["data"])),
                tuple(sorted(row["evidence"])),
            )
        # Typed construction can fail even after a raw proof succeeds. No
        # half-batch typed cache is published in that case.
        self._fact_versions.update(staged)

    def calculation_identity_headers(self, identifiers):
        """Read exact calculation/fact/publication identities in one batch.

        This does not prove result contents or frozen adoption. The identity
        consumer still authenticates the publication and its precise frozen
        leaf or current terminal; no successful body marker is populated here.
        """
        identifiers = set(identifiers)
        if not identifiers:
            return {}
        cursor = self.connection.execute(
            "SELECT p.*,c.id source_id,c.subject_id source_subject,c.kind source_kind,"
            "c.period source_period,c.fact_id source_fact_id,c.digest source_digest,"
            "c.program_version source_program_version,f.revision fact_revision,"
            "f.subject_id fact_subject,f.period fact_period,s.kind fact_kind,"
            "EXISTS(SELECT 1 FROM calculation_seal z WHERE z.calculation_id=c.id) cs,"
            "EXISTS(SELECT 1 FROM fact_seal z WHERE z.fact_id=f.id) fs,"
            "h.calculation_id current_id,"
            "EXISTS(SELECT 1 FROM calculation_publication n "
            "WHERE n.previous_publication_id=p.id) has_successor "
            "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
            "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id "
            "LEFT JOIN calculation_publication p ON p.calculation_id=c.id "
            "LEFT JOIN calculation_current h ON h.subject_id=p.subject_id",
            (canonical(sorted(identifiers)),),
        )
        names = tuple(item[0] for item in cursor.description)
        headers = {}
        for row in cursor:
            value = dict(zip(names, row, strict=True))
            if value["source_id"] in headers or (
                value["source_subject"], value["source_kind"], value["source_period"]
            ) != (value["fact_subject"], value["fact_kind"], value["fact_period"]):
                raise KernelError("content_integrity_failed", "核算元数据与来源事实身份不一致")
            if value["id"] is None:
                raise KernelError("content_integrity_failed", "采用来源缺少正式发布")
            headers[value["source_id"]] = value
        if headers.keys() != identifiers:
            raise KernelError("content_integrity_failed", "核算元数据与来源事实身份不一致")
        return headers

    def metadata(self, identifiers, *, state=True, outcomes=None, _decoded_outcomes=None):
        # A consumer needing this exact body can receive the strictly checked
        # object during the same metadata read. It lives only in the caller's
        # local batch, never in a second snapshot body cache or an identity proof.
        if _decoded_outcomes is not None and not state:
            raise ValueError("decoded metadata requires result-state verification")
        if _decoded_outcomes is not None and outcomes is not None:
            raise ValueError("choose decoded or raw metadata output")
        decode_selected = _decoded_outcomes is not None
        identifiers = set(identifiers)
        reusable = (
            identifiers & self._verified_source_contents.keys()
            if decode_selected and self._snapshot_active and self.connection.in_transaction
            else set()
        )
        missing = {
            ident
            for ident in identifiers
            if ident not in self._metadata
            or (state and "line_count" not in self._metadata[ident])
            or outcomes is not None
            or decode_selected
        }
        if missing:
            if state and not decode_selected:
                self.verify_sql_outcomes(missing)
            expressions = (
                "json_array_length(c.outcome,'$.lines') AS line_count,"
                "coalesce(json_extract(c.outcome,'$.opening'),0) AS opening "
                if state and not decode_selected
                else "NULL AS line_count,NULL AS opening "
            )
            # An existing complete-source proof can supply this exact body,
            # but metadata and new bodies still form one staged batch. Neither
            # half publishes success before all identities and bodies pass.
            body_expression = (
                "CASE WHEN c.id IN (SELECT value FROM json_each(?)) "
                "THEN NULL ELSE c.outcome END AS outcome,"
                if reusable
                else "c.outcome AS outcome,"
            ) if outcomes is not None or decode_selected else ""
            parameters = (
                (canonical(sorted(reusable)), canonical(sorted(missing)))
                if reusable else (canonical(sorted(missing)),)
            )
            rows = list(
                self.connection.execute(
                    "SELECT c.id,c.subject_id,c.kind,c.period,c.fact_id,c.digest,c.program_version,"
                    "f.revision AS fact_revision,"
                    "f.subject_id fact_subject,f.period fact_period,s.kind fact_kind,"
                    "p.id AS publication_id,p.mode AS publication_mode,"
                    "p.posting_period,p.voucher_id,"
                    + body_expression
                    + expressions
                    + "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                    "LEFT JOIN calculation_publication p ON p.calculation_id=c.id "
                    "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id",
                    parameters,
                )
            )
            if {row["id"] for row in rows} != missing or any(
                (row["subject_id"], row["kind"], row["period"])
                != (row["fact_subject"], row["fact_kind"], row["fact_period"])
                for row in rows
            ):
                raise KernelError("content_integrity_failed", "核算元数据与来源事实身份不一致")
            staged, staged_outcomes, staged_decoded = {}, {}, {}
            for row in rows:
                value = dict(row)
                if decode_selected:
                    decoded = (
                        self._verified_source_contents[value["id"]]
                        if value["id"] in reusable else stored_json.verify_outcome_bytes(
                            value["outcome"], value["digest"], value["id"]
                        )
                    )
                    if (
                        not isinstance(decoded, dict)
                        or not isinstance(decoded.get("lines"), list)
                        or type(decoded.get("opening")) is not bool
                    ):
                        raise KernelError("content_integrity_failed", "已保存的核算结果结构不一致")
                    # Do not let JSON1 interpret unchecked bytes. These two
                    # fields have fixed Outcome types; this is the same state
                    # projection as JSON1 for every valid stored result.
                    value["line_count"] = len(decoded["lines"])
                    value["opening"] = int(decoded["opening"])
                    staged_decoded[value["id"]] = decoded
                for field in ("fact_subject", "fact_kind", "fact_period"):
                    value.pop(field)
                if outcomes is not None or decode_selected:
                    raw = value.pop("outcome")
                    if outcomes is not None:
                        staged_outcomes[value["id"]] = raw
                if value["posting_period"] is None and value["kind"] not in {
                    "asset_activation",
                    "asset_consumption",
                }:
                    raise KernelError(
                        "unknown_calculation", "计算版本没有正式发布或资产批次采用记录"
                    )
                value["result_digest"] = value.pop("digest").hex()
                value["period"] = str(YearMonth.from_ordinal(value["period"]))
                value["publication_role"] = (
                    "independent" if value["posting_period"] is not None else "asset_member"
                )
                value["posting_period"] = (
                    str(YearMonth.from_ordinal(value["posting_period"]))
                    if value["posting_period"] is not None
                    else None
                )
                if not state:
                    value.pop("line_count")
                    value.pop("opening")
                    prior = self._metadata.get(value["id"], {})
                    for field in ("line_count", "opening"):
                        if field in prior:
                            value[field] = prior[field]
                staged[value["id"]] = value
            self._metadata.update(staged)
            if outcomes is not None:
                outcomes.update(staged_outcomes)
            if decode_selected:
                _decoded_outcomes.update(staged_decoded)
                if self._snapshot_active and self.connection.in_transaction:
                    self._verified_sql_outcomes.update(missing - reusable)
            if any(ident not in self._metadata for ident in missing) or (
                outcomes is not None and any(ident not in outcomes for ident in missing)
            ):
                raise KernelError("unknown_calculation", "计算版本不存在")
        return {ident: self._metadata[ident] for ident in identifiers}

    def verify_sql_outcomes(self, identifiers):
        """Reuse only exact successful JSON/content proof in this owned snapshot."""
        identifiers = set(identifiers)
        active = self._snapshot_active and self.connection.in_transaction
        missing = (
            {
                ident
                for ident in identifiers
                if ident not in self._verified_sql_outcomes
                and ident not in self._verified_source_contents
                and ident not in self._anchored_source_bytes
            }
            if active
            else identifiers
        )
        if missing:
            verify_sql_outcomes(self.connection, missing)
            if active:
                self._verified_sql_outcomes.update(missing)

    def _material_sql_outcome_batch(self, connection):
        """Share material syntax/digest proof only with this owned read snapshot."""
        if not self._snapshot_active or connection is not self.connection:
            return None
        return _MaterialSqlOutcomeBatch(self, connection)

    def asset_members(self, owner_calculation_id):
        """Read the exact immutable adoption, never a current member replacement."""
        self.asset_members_many((owner_calculation_id,))
        return self._asset_members[owner_calculation_id]

    def asset_members_many(self, owner_calculation_ids, *, _decoded_owners=None):
        """Validate selected frozen owners together and retain successful snapshot reads."""
        owner_ids = tuple(dict.fromkeys(owner_calculation_ids))
        missing = tuple(ident for ident in owner_ids if ident not in self._asset_members)
        if missing:
            from .asset_batches import frozen_members_many

            reusable = {
                ident: _decoded_owners[ident] for ident in missing
                if ident in (_decoded_owners or {}) and ident in self._verified_sql_outcomes
            } if _owns_current_selector_snapshot(self, self.connection) else {}
            verified = frozen_members_many(self.connection, missing, _owner_outcomes=reusable)
            self._asset_members.update(
                {ident: tuple(members) for ident, members in verified.items()}
            )
        return {ident: self._asset_members[ident] for ident in owner_ids}

    def calculations(self, identifiers):
        identifiers = set(identifiers)
        missing = {ident for ident in identifiers if ident not in self._calculations}
        if missing:
            # Read the full result alongside its publication and fact identity.
            # State-only JSON scalars are not needed by this complete decoder.
            reusable = (
                {ident for ident in missing if ident in self._verified_source_contents}
                if self._snapshot_active and self.connection.in_transaction
                else set()
            )
            outcomes = {}
            metadata = self.metadata(missing - reusable, state=False, outcomes=outcomes)
            metadata.update(self.metadata(reusable, state=False))
            facts = self.facts({row["fact_id"] for row in metadata.values()})
            prepared = {}
            for ident in missing:
                value = metadata[ident]
                prepared[ident] = {
                    key: item
                    for key, item in value.items()
                    if key not in {"line_count", "opening", "fact_revision"}
                } | {
                    "fact_data": facts[value["fact_id"]]["data"],
                    "outcome": (
                        self._verified_source_contents[ident]
                        if ident in reusable
                        else self._stored_outcome(outcomes[ident])
                    ),
                }
            # Retain the exact text returned by the same SELECT only after the
            # complete typed batch succeeds. It is input for a later hash check,
            # never a verified-content or adoption marker.
            self._calculations.update(prepared)
            if self._snapshot_active and self.connection.in_transaction:
                self._raw_calculation_outcomes.update(
                    (ident, outcome.encode("utf-8")) for ident, outcome in outcomes.items()
                )
        return {ident: self._calculations[ident] for ident in identifiers}

    def calculation(self, ident):
        if ident in self._calculations:
            return self._calculations[ident]
        return self.calculations((ident,))[ident]

    def raw_calculations(self, identifiers):
        """Load stored fact shapes without applying today's model validation."""
        identifiers = set(identifiers)
        missing = {ident for ident in identifiers if ident not in self._raw_calculations}
        if missing:
            combined = (
                self._snapshot_active and self.connection.in_transaction
                and getattr(self.store.registry, "content_version", None) != 1
            )
            decoded = {}
            metadata = (
                self.metadata(missing, _decoded_outcomes=decoded)
                if combined else self.metadata(missing)
            )
            facts = self.store.fact_data_many(
                self.connection, {row["fact_id"] for row in metadata.values()}
            )
            reusable = (
                missing if combined else
                {ident for ident in missing if ident in self._verified_source_contents}
                if self._snapshot_active and self.connection.in_transaction
                else set()
            )
            outcomes = (
                {
                    row["id"]: row["outcome"]
                    for row in self.connection.execute(
                        "SELECT c.id,c.outcome FROM json_each(?) ids "
                        "JOIN calculation c ON c.id=ids.value",
                        (canonical(sorted(missing - reusable)),),
                    )
                }
                if missing - reusable
                else {}
            )
            prepared = {}
            for ident in missing:
                value = metadata[ident]
                prepared[ident] = {
                    key: item
                    for key, item in value.items()
                    if key not in {"line_count", "opening", "fact_revision"}
                } | {
                    "fact_data": facts[value["fact_id"]],
                    "outcome": (
                        decoded[ident] if combined else self._verified_source_contents[ident]
                        if ident in reusable
                        else self._stored_outcome(outcomes[ident])
                    ),
                }
            self._raw_calculations.update(prepared)
        return {ident: self._raw_calculations[ident] for ident in identifiers}

    def prime_parents(self, identifiers):
        missing = {ident for ident in identifiers if ident not in self._parents}
        if not missing:
            return
        values = {ident: [] for ident in missing}
        for row in self.connection.execute(
            "SELECT d.calculation_id,d.upstream_id FROM json_each(?) ids "
            "JOIN dependency_calculation d ON d.calculation_id=ids.value "
            "ORDER BY d.calculation_id,d.upstream_id",
            (canonical(sorted(missing)),),
        ):
            values[row["calculation_id"]].append(row["upstream_id"])
        self._parents.update({key: tuple(value) for key, value in values.items()})

    def parents(self, ident):
        # Membership first: prime_parents rebuilds a set difference against the
        # whole cache, so calling it per identity makes a cache hit ~50x costlier
        # than a lookup once the parent graph is warm.
        if ident not in self._parents:
            self.prime_parents((ident,))
        return self._parents[ident]

    def prime_calculations(self, identifiers, *, ancestors=True):
        identifiers = set(identifiers)
        missing_roots = identifiers - self._ancestor_roots
        if ancestors and missing_roots:
            closure = {
                row[0]
                for row in self.connection.execute(
                    "WITH RECURSIVE selected(id) AS (SELECT value FROM json_each(?) UNION "
                    "SELECT d.upstream_id FROM dependency_calculation d JOIN selected s "
                    "ON d.calculation_id=s.id) SELECT id FROM selected",
                    (canonical(sorted(missing_roots)),),
                )
            }
            self.prime_parents(closure)
            self.calculations(closure)
            self._ancestor_roots.update(closure)
        else:
            self.calculations(identifiers)
        return {ident: self._calculations[ident] for ident in identifiers}

    prefetch = prime_calculations

    def prime_raw_calculations(self, identifiers, *, verified_calculations=None):
        """Prefetch an ancestry closure using raw fact adapters for integrity work."""
        identifiers = set(identifiers)
        missing_roots = identifiers - self._raw_ancestor_roots
        if missing_roots:
            closure = {
                row[0]
                for row in self.connection.execute(
                    "WITH RECURSIVE selected(id) AS (SELECT value FROM json_each(?) UNION "
                    "SELECT d.upstream_id FROM dependency_calculation d JOIN selected s "
                    "ON d.calculation_id=s.id) SELECT id FROM selected",
                    (canonical(sorted(missing_roots)),),
                )
            }
            verified = verified_calculations or {}
            reusable = closure & verified.keys()
            if reusable:
                facts = self.store.fact_data_many(
                    self.connection, {verified[ident]["fact_id"] for ident in reusable}
                )
                publications = {
                    row["calculation_id"]: dict(row)
                    for row in self.connection.execute(
                        "SELECT p.calculation_id,p.id publication_id,p.mode publication_mode,"
                        "p.posting_period,p.voucher_id FROM json_each(?) ids "
                        "JOIN calculation_publication p ON p.calculation_id=ids.value",
                        (canonical(sorted(reusable)),),
                    )
                }
                for ident in reusable:
                    source = verified[ident]
                    publication = publications.get(ident, {})
                    posting_period = publication.get("posting_period")
                    self._raw_calculations[ident] = {
                        "id": ident,
                        "subject_id": source["subject_id"],
                        "kind": source["kind"],
                        "period": str(YearMonth.from_ordinal(source["period"])),
                        "fact_id": source["fact_id"],
                        "result_digest": bytes(source["digest"]).hex(),
                        "program_version": source["program_version"],
                        "publication_id": publication.get("publication_id"),
                        "publication_mode": publication.get("publication_mode"),
                        "posting_period": (
                            str(YearMonth.from_ordinal(posting_period))
                            if posting_period is not None
                            else None
                        ),
                        "voucher_id": publication.get("voucher_id"),
                        "publication_role": (
                            "independent" if posting_period is not None else "asset_member"
                        ),
                        "fact_data": facts[source["fact_id"]],
                        "outcome": source["decoded"],
                    }
            self.prime_parents(closure)
            self.raw_calculations(closure)
            self._raw_ancestor_roots.update(closure)
        else:
            self.raw_calculations(identifiers)
        return {ident: self._raw_calculations[ident] for ident in identifiers}

    def relations(self, calculation, *, resolver=resolve_calculation_relations):
        ident = calculation if isinstance(calculation, str) else calculation["id"]
        return self.relations_many((ident,), resolver=resolver)[ident]

    def relations_many(self, calculations, *, resolver=resolve_calculation_relations, raw=False):
        """Resolve several roots after one shared ancestry and payload prefetch.

        The semantic resolver is intentionally unchanged.  Batching only moves
        database work ahead of the per-root pure-Python walk, and the existing
        relation/source caches remain scoped to this managed read snapshot.
        """

        identifiers = {item if isinstance(item, str) else item["id"] for item in calculations}
        missing = {ident for ident in identifiers if (ident, resolver, raw) not in self._relations}
        if missing:
            if raw:
                self.prime_raw_calculations(missing)
                calculations_by_id = self._raw_calculations
                sources = self._raw_relation_sources
            else:
                self.prime_calculations(missing)
                calculations_by_id = self._calculations
                sources = self._relation_sources
            for ident in sorted(missing):
                self._relations[ident, resolver, raw] = resolver(
                    calculations_by_id[ident],
                    load_calculation=(
                        (lambda key: self.raw_calculations((key,))[key])
                        if raw
                        else self.calculation
                    ),
                    load_parents=self.parents,
                    source_cache=sources,
                )
        return {ident: self._relations[ident, resolver, raw] for ident in identifiers}

    def report_line_relations_many(self, rows_by_calculation):
        """Resolve exact report lines without expanding unrelated obligation ancestry.

        A line with no usable direct relation may need candidates from the
        complete ancestry. Such roots retain the public relations_many path.
        """
        identifiers = set(rows_by_calculation)
        if not identifiers:
            return {}
        roots = self.calculations(identifiers)
        self.prime_parents(identifiers)
        direct_sources = set()
        for ident, record in roots.items():
            kind = record.get("kind")
            if kind in {
                *SETTLEMENT_PAYMENT_KINDS,
                "settlement",
                "employee_advance",
                "reimbursement_acceptance",
                "overpayment",
            }:
                direct_sources.update(self._parents[ident])
        if direct_sources:
            self.calculations(direct_sources)
        resolved, fallback, pending = {}, set(), {}
        used_sources = set()
        for ident in sorted(identifiers):
            cached = self._report_direct_relations.get(ident) if self._snapshot_active else None
            if cached is None:
                loaded_sources = set()

                def load_direct(key, _loaded_sources=loaded_sources):
                    _loaded_sources.add(key)
                    return self.calculation(key)

                relation = resolve_calculation_relations(
                    roots[ident],
                    load_calculation=load_direct,
                    load_parents=self.parents,
                    source_cache=self._relation_sources,
                    collect_ancestry=False,
                )
                pending[ident] = relation, frozenset(loaded_sources)
            else:
                relation, loaded_sources = cached
            used_sources.update(loaded_sources)
            if not loaded_sources.issubset(self._parents[ident]):
                raise KernelError("content_integrity_failed", "报表命中来源缺少精确核算依赖")
            if any(
                report_party_needs_ancestry(row, relation) for row in rows_by_calculation[ident]
            ):
                fallback.add(ident)
            else:
                resolved[ident] = relation
        if fallback:
            resolved.update(self.relations_many(fallback))
            closure = {
                row[0]
                for row in self.connection.execute(
                    "WITH RECURSIVE selected(id) AS (SELECT value FROM json_each(?) UNION "
                    "SELECT d.upstream_id FROM dependency_calculation d JOIN selected s "
                    "ON d.calculation_id=s.id) SELECT id FROM selected",
                    (canonical(sorted(fallback)),),
                )
            }
            used_sources.update(closure)
        self.verify_selected_content(identifiers | used_sources)
        if self._snapshot_active:
            self._report_direct_relations.update(pending)
        return resolved

    def verify_selected_content(self, identifiers):
        """Check used result/fact bodies, without expanding unused ancestors.

        This proves saved content, not adoption or a complete dependency graph.
        Callers still select exact publications and verify their used relations.
        """
        identifiers = set(identifiers)
        missing = {ident for ident in identifiers if ident not in self._verified_source_contents}
        if not missing:
            return {ident: self._verified_source_contents[ident] for ident in identifiers}
        reusable = (
            missing & self._raw_calculation_outcomes.keys()
            if self._snapshot_active and self.connection.in_transaction
            else set()
        )
        headers = {}
        for selected, include_outcome in ((reusable, False), (missing - reusable, True)):
            if not selected:
                continue
            headers.update(
                (row["id"], dict(row))
                for row in self.connection.execute(
                    "SELECT c.id,c.subject_id,c.kind,c.period,c.fact_id,c.digest,"
                    + ("c.outcome," if include_outcome else "")
                    + "f.subject_id fact_subject,f.period fact_period,"
                    "f.digest fact_digest,s.kind fact_kind,"
                    "EXISTS(SELECT 1 FROM calculation_seal z WHERE z.calculation_id=c.id) "
                    "calculation_sealed,"
                    "EXISTS(SELECT 1 FROM fact_seal z WHERE z.fact_id=f.id) fact_sealed "
                    "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                    "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id",
                    (canonical(sorted(selected)),),
                )
            )
        if headers.keys() != missing:
            raise KernelError("content_integrity_failed", "本次读取的核算来源缺失")
        from .integrity import _object

        for header in headers.values():
            if (
                not header["calculation_sealed"]
                or not header["fact_sealed"]
                or (header["subject_id"], header["kind"], header["period"])
                != (header["fact_subject"], header["fact_kind"], header["fact_period"])
            ):
                raise KernelError("content_integrity_failed", "本次读取的核算来源封签不一致")
        self._verify_selected_fact_bodies(headers.values())
        verified = {}
        for ident, header in headers.items():
            raw_outcome = (
                self._raw_calculation_outcomes[ident]
                if ident in reusable
                else header["outcome"].encode("utf-8")
            )
            raw_hash_matches = hashlib.sha256(raw_outcome).digest() == header["digest"]
            # The fallback must decode these exact stored bytes, not a typed or
            # normalized calculation object that merely looks equivalent.
            outcome = _object(raw_outcome, "calculation", ident)
            try:
                outcome_digest_matches = raw_hash_matches or digest(outcome) == header["digest"]
            except (TypeError, ValueError, OverflowError) as error:
                raise KernelError(
                    "content_integrity_failed",
                    "本次读取的核算来源无法解码",
                    component="calculation",
                    record_id=ident,
                    reason="invalid_json",
                ) from error
            # The writer stores canonical JSON; equivalent noncanonical
            # content still uses the strict decoded comparison above.
            if not outcome_digest_matches:
                raise KernelError("content_integrity_failed", "本次读取的核算来源封签不一致")
            verified[ident] = outcome
        if self._snapshot_active:
            self._verified_source_contents.update(verified)
        return {
            ident: verified[ident] if ident in verified else self._verified_source_contents[ident]
            for ident in identifiers
        }

    def _verify_selected_fact_bodies(self, headers):
        """Prove exact physical facts without decoding unused scalar values.

        Scalar SQL encodes the same stored fields and child order as the writer.
        A byte hash is only an equality fast path: composite, noncanonical and
        fixed-version historical facts retain the original complete decoder.
        Header identity/seals are checked by each caller before reaching here.
        """
        from .integrity import verify_fact_child_order
        from .schema import table_name
        from .storage import _scalar_fact_hashes, _snapshot_fact_hashes, _snapshot_fact_raws

        headers = tuple(headers)
        kinds, fact_periods, fact_digests = {}, {}, {}
        for header in headers:
            ident = header["fact_id"]
            kinds.setdefault(header["fact_kind"], set()).add(ident)
            fact_periods[ident] = header["fact_period"]
            fact_digests[ident] = header["fact_digest"]
        verify_fact_child_order(self.engine, self.connection, kinds)
        for kind, identifiers in kinds.items():
            physical = dict(
                self.connection.execute(
                    f"SELECT f.revision_id,f.period FROM json_each(?) ids "
                    f"CROSS JOIN {table_name(kind)} f ON f.revision_id=ids.value",
                    (canonical(sorted(identifiers)),),
                )
            )
            if physical.keys() != identifiers or any(
                physical[ident] != fact_periods[ident] for ident in identifiers
            ):
                raise KernelError("content_integrity_failed", "本次读取的事实期间不一致")
        fact_ids = set(fact_periods)
        raw_hashes = _snapshot_fact_hashes(self.store, self.connection, fact_ids)
        raw_hashes.update(
            _scalar_fact_hashes(
                self.store,
                self.connection,
                {kind: ids - raw_hashes.keys() for kind, ids in kinds.items()},
            )
        )
        fallback_ids = {ident for ident in fact_ids if raw_hashes.get(ident) != fact_digests[ident]}
        if not fallback_ids:
            return
        facts = _snapshot_fact_raws(self.store, self.connection, fallback_ids)
        try:
            facts.update(self.store.fact_data_many(self.connection, fallback_ids - facts.keys()))
        except (KeyError, ValueError) as error:
            raise KernelError("content_integrity_failed", "本次读取的事实内容无法解码") from error
        except KernelError as error:
            if error.code != "unknown_fact":
                raise
            raise KernelError("content_integrity_failed", "本次读取的事实内容缺失") from error
        if facts.keys() != fallback_ids or any(
            fact.get("period") != str(YearMonth.from_ordinal(fact_periods[ident]))
            or digest(fact) != fact_digests[ident]
            for ident, fact in facts.items()
        ):
            raise KernelError("content_integrity_failed", "本次读取的事实内容封签不一致")

    def verify_saved_input_identity(self, identifiers):
        """Bind consumed result bodies to their saved calculation identities.

        Read only the requested IDs' saved scopes and dependency version IDs,
        never ancestor result bodies. This is the existing core's two exact
        input-digest variants, not a current-head or adoption assertion.
        """
        from collections import defaultdict
        from dataclasses import asdict

        from .content_history_context import month_type, read_type, source_digest
        from .integrity import _invalid

        identifiers = set(identifiers)
        outcomes = self.verify_selected_content(identifiers)
        missing = (
            identifiers - self._verified_saved_input_identities
            if self._snapshot_active
            else identifiers
        )
        if not missing:
            return outcomes
        encoded = canonical(sorted(missing))
        headers = {
            row["id"]: row
            for row in self.connection.execute(
                "SELECT c.id,c.fact_id,c.program_version FROM json_each(?) ids "
                "JOIN calculation c ON c.id=ids.value",
                (encoded,),
            )
        }
        if headers.keys() != missing:
            _invalid("calculation", "*", "referenced_calculation_missing")
        dependencies, dependency_facts, saved_reads = (
            defaultdict(set),
            defaultdict(set),
            defaultdict(list),
        )
        for row in self.connection.execute(
            "SELECT d.calculation_id,d.upstream_id,c.id found FROM json_each(?) ids "
            "JOIN dependency_calculation d ON d.calculation_id=ids.value "
            "LEFT JOIN calculation c ON c.id=d.upstream_id",
            (encoded,),
        ):
            if row["found"] is None or row["upstream_id"] == row["calculation_id"]:
                _invalid("calculation", row["calculation_id"], "invalid_calculation_dependency")
            dependencies[row["calculation_id"]].add(row["upstream_id"])
        for row in self.connection.execute(
            "SELECT d.calculation_id,d.fact_id,f.id found FROM json_each(?) ids "
            "JOIN dependency_fact d ON d.calculation_id=ids.value "
            "LEFT JOIN fact_revision f ON f.id=d.fact_id",
            (encoded,),
        ):
            if row["found"] is None:
                _invalid("fact", row["fact_id"], "referenced_fact_missing")
            dependency_facts[row["calculation_id"]].add(row["fact_id"])
        for row in self.connection.execute(
            "SELECT d.* FROM json_each(?) ids "
            "JOIN dependency_scope d ON d.calculation_id=ids.value",
            (encoded,),
        ):
            try:
                saved_reads[row["calculation_id"]].append(
                    read_type()(
                        row["source"],
                        row["kind"],
                        row["scope_key"],
                        None
                        if row["before_period"] == 119988
                        else month_type().from_ordinal(row["before_period"]),
                    )
                )
            except (ValueError, TypeError) as exc:
                raise KernelError(
                    "content_integrity_failed",
                    "已保存的读取作用域格式错误",
                    component="calculation",
                    record_id=row["calculation_id"],
                ) from exc
        for ident, row in headers.items():
            if row["fact_id"] not in dependency_facts[ident]:
                _invalid("calculation", ident, "own_fact_dependency_missing")
            versions = dependencies[ident] | (dependency_facts[ident] - {row["fact_id"]})
            payload = {
                "fact": row["fact_id"],
                "outcome": outcomes[ident],
                "reads": [asdict(read) for read in sorted(saved_reads[ident], key=repr)],
                "program": row["program_version"],
            }
            first_key = "c_" + source_digest({**payload, "versions": sorted(versions)}).hex()
            if ident != first_key:
                second_key = (
                    "c_"
                    + source_digest(
                        {**payload, "versions": sorted(versions | {row["fact_id"]})}
                    ).hex()
                )
                if ident != second_key:
                    _invalid("calculation", ident, "calculation_input_digest_mismatch")
        if self._snapshot_active:
            self._verified_saved_input_identities.update(missing)
        return outcomes

    def _verify_anchored_source_bytes(self, proof):
        """Check an already authenticated report anchor's exact saved sources.

        The report reader alone mints this private proof after checking the
        immutable anchor against its saved body. No decoded result is returned
        or inserted into the ordinary decoded-content cache.
        """
        from .integrity import _object
        from .report_open_contribution import _require_source_proof

        bindings = _require_source_proof(proof, self)
        expected = {}
        for binding in bindings:
            if len(binding) != 4:
                raise KernelError("content_integrity_failed", "报表命中来源绑定格式不一致")
            ident = binding[0]
            if ident in expected and expected[ident] != binding:
                raise KernelError("content_integrity_failed", "报表命中来源绑定相互冲突")
            expected[ident] = binding
        if not expected:
            raise KernelError("content_integrity_failed", "报表命中来源绑定为空")
        actual = {}
        pending_bindings = {}
        for ident, binding in expected.items():
            verified = self._anchored_source_bytes.get(ident)
            if verified is None:
                pending_bindings[ident] = binding
            elif verified != binding:
                raise KernelError("content_integrity_failed", "报表命中来源绑定与已核原文冲突")
            else:
                actual[ident] = list(binding)
        if not pending_bindings:
            return actual
        body_ids = {
            ident for ident in pending_bindings if ident not in self._verified_source_contents
        }
        headers = {
            row["id"]: dict(row)
            for row in self.connection.execute(
                "SELECT c.id,c.subject_id,c.kind,c.period,c.fact_id,c.digest,"
                "CASE WHEN c.id IN (SELECT value FROM json_each(?)) "
                "THEN c.outcome END outcome,"
                "f.subject_id fact_subject,f.period fact_period,f.digest fact_digest,"
                "s.kind fact_kind,"
                "EXISTS(SELECT 1 FROM calculation_seal z WHERE z.calculation_id=c.id) "
                "calculation_sealed,"
                "EXISTS(SELECT 1 FROM fact_seal z WHERE z.fact_id=f.id) fact_sealed "
                "FROM json_each(?) ids CROSS JOIN calculation c ON c.id=ids.value "
                "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id",
                (canonical(sorted(body_ids)), canonical(sorted(pending_bindings))),
            )
        }
        if headers.keys() != pending_bindings.keys():
            raise KernelError("content_integrity_failed", "本次读取的核算来源缺失")
        pending = {}
        for ident, header in headers.items():
            binding = [
                ident,
                header["digest"].hex(),
                header["fact_id"],
                header["fact_digest"].hex(),
            ]
            actual[ident] = binding
            if tuple(binding) != pending_bindings[ident]:
                raise KernelError("content_integrity_failed", "报表命中来源绑定与原文不一致")
            if (
                not header["calculation_sealed"]
                or not header["fact_sealed"]
                or (header["subject_id"], header["kind"], header["period"])
                != (header["fact_subject"], header["fact_kind"], header["fact_period"])
            ):
                raise KernelError("content_integrity_failed", "本次读取的核算来源封签不一致")
            if ident not in self._verified_source_contents:
                pending[ident] = header
        if not pending:
            self._anchored_source_bytes.update(pending_bindings)
            return actual

        self._verify_selected_fact_bodies(pending.values())

        for ident, header in pending.items():
            raw = (
                self._raw_calculation_outcomes[ident]
                if ident in self._raw_calculation_outcomes
                else header["outcome"].encode("utf-8")
            )
            if hashlib.sha256(raw).digest() == header["digest"]:
                continue
            # Equivalent noncanonical JSON remains valid under the original
            # content rule, but only after decoding these exact stored bytes.
            outcome = _object(raw, "calculation", ident)
            try:
                valid = digest(outcome) == header["digest"]
            except (TypeError, ValueError, OverflowError) as error:
                raise KernelError(
                    "content_integrity_failed", "本次读取的核算来源无法解码"
                ) from error
            if not valid:
                raise KernelError("content_integrity_failed", "本次读取的核算来源封签不一致")
        self._anchored_source_bytes.update(pending_bindings)
        return actual

    def vouchers(self, identifiers):
        identifiers = set(identifiers)
        missing = {ident for ident in identifiers if ident not in self._vouchers}
        if missing:
            for row in self.connection.execute(
                "SELECT v.*,n.number FROM json_each(?) ids "
                "JOIN voucher_version v ON v.id=ids.value "
                "JOIN voucher n ON n.id=v.voucher_id",
                (canonical(sorted(missing)),),
            ):
                self._vouchers[row["id"]] = dict(row)
            if any(ident not in self._vouchers for ident in missing):
                raise KernelError("selected_voucher_missing", "冻结期间引用的凭证版本不存在")
        return {ident: self._vouchers[ident] for ident in identifiers}

    def voucher(self, ident):
        return self.vouchers((ident,))[ident]

    def voucher_lines(self, identifiers):
        identifiers = set(identifiers)
        missing = {ident for ident in identifiers if ident not in self._lines}
        if missing:
            self._lines.update({ident: [] for ident in missing})
            for row in self.connection.execute(
                "SELECT l.* FROM json_each(?) ids JOIN voucher_line l ON l.version_id=ids.value "
                "ORDER BY l.version_id,l.line_no",
                (canonical(sorted(missing)),),
            ):
                self._lines[row["version_id"]].append(
                    {key: row[key] for key in ("line_no", "account", "debit", "credit", "cashflow")}
                )
        return {ident: self._lines[ident] for ident in identifiers}

    def close_rows(self, **scope):
        from .read_indexes import close_rows

        # A managed snapshot retains only successfully checked sources and the
        # manifest that the complete source/reference check already parsed.
        # Other callers may replace their transaction, so they recheck each read.
        if self._snapshot_active:
            parsed = {}
            rows = close_rows(
                self.connection,
                verified_periods=self._closes.keys(),
                parsed_manifests=parsed,
                **scope,
            )
            for row in rows:
                self._closes[row["period"]] = row
            self._close_manifests.update(parsed)
        else:
            rows = close_rows(self.connection, **scope)
        return rows

    def close_manifest(self, row):
        period = row["period"]
        if self._snapshot_active and period in self._close_manifests:
            return self._close_manifests[period]
        from .content_history_context import close_reader

        manifest = close_reader().decode_close(self.connection, row)
        if self._snapshot_active:
            self._close_manifests[period] = manifest
        return manifest

    def close_header(self, row):
        from .content_history_context import close_reader

        period = row["period"]
        if self._snapshot_active and period in self._close_headers:
            return self._close_headers[period]
        header = close_reader().verified_header(self.connection, row)
        if self._snapshot_active:
            self._close_headers[period] = header
        return header

    def close_accounting(self, row, *, subjects):
        from . import close_storage
        from .content_history_context import close_reader

        key = (row["period"], frozenset(subjects))
        if self._snapshot_active and key in self._close_accounting_slices:
            return self._close_accounting_slices[key]
        reader = close_reader()
        options = {
            "_verified_parts": (
                self._verified_close_storage_parts if self._snapshot_active else None
            )
        }
        if self._snapshot_active and reader is close_storage:
            options["_positions_cache"] = self._close_accounting_positions
        result = reader.read_accounting(
            self.connection,
            self.close_header(row),
            key[1],
            **options,
        )
        if self._snapshot_active:
            self._close_accounting_slices[key] = result
        return result

    def close_accounting_many(self, rows, *, subjects, subjects_by_period=None):
        return self._close_accounting_many(
            rows, subjects=subjects, adopted_only=False, subjects_by_period=subjects_by_period
        )

    def close_asset_owner_accounting_many(self, rows, *, subjects):
        """Read frozen owner slices using their independently committed full roster."""
        return self._close_accounting_many(
            rows, subjects=subjects, adopted_only=False,
            asset_owners=_owns_current_selector_snapshot(self, self.connection),
        )

    def frozen_bank_account_identities(
        self, frozen_periods, *, through_period, _decoded_outcomes=None
    ):
        """Authenticate historical bank identities, not complete source contents.

        Frozen statement/reconciliation results contain the bank identity without
        original row children. The independent adoption anchors these exact result
        bytes; only the consumed scalar fact and registered identity are read.
        Nothing here enters the complete-content or decoded-calculation caches.
        """
        from . import close_storage
        from .content_history_context import close_reader
        from .publication import verify_record

        if close_reader() is not close_storage:
            raise ValueError("historical bank identity reads require the current close reader")
        identifiers = set(frozen_periods)
        if not identifiers:
            return {}
        # The funds selector can carry its exact strict metadata decode. Bind
        # the entire object to that result digest before omitting stored bytes;
        # the independent frozen leaf is checked below before any scalar is
        # consumed. This local reuse never publishes a body or identity proof.
        reusable = {}
        if (
            _decoded_outcomes is not None and self._snapshot_active
            and self.connection.in_transaction
            and getattr(self.store.registry, "content_version", None) != 1
        ):
            for ident in identifiers & _decoded_outcomes.keys() & self._verified_sql_outcomes:
                metadata = self._metadata.get(ident)
                if metadata is None:
                    continue
                try:
                    result_digest = digest(_decoded_outcomes[ident]).hex()
                except (TypeError, ValueError, OverflowError):
                    continue
                if result_digest == metadata["result_digest"]:
                    reusable[ident] = (_decoded_outcomes[ident], result_digest)
        outcome_expression = (
            "CASE WHEN c.id IN (SELECT value FROM json_each(?)) "
            "THEN NULL ELSE c.outcome END outcome,"
            if reusable else "c.outcome,"
        )
        parameters = (
            (canonical(sorted(reusable)), canonical(sorted(identifiers)))
            if reusable else (canonical(sorted(identifiers)),)
        )
        headers = {}
        for row in self.connection.execute(
            "SELECT p.*,c.id calc_id,c.subject_id calc_subject,c.kind calc_kind,"
            "c.period calc_period,c.fact_id calc_fact,c.digest calc_digest,"
            + outcome_expression
            + "f.subject_id fact_subject,f.period fact_period,s.kind fact_kind,"
            "CASE s.kind WHEN 'bank_statement' THEN b.bank_account_id "
            "WHEN 'bank_reconciliation' THEN r.bank_account_id END bank_account_id,"
            "CASE s.kind WHEN 'bank_statement' THEN b.period "
            "WHEN 'bank_reconciliation' THEN r.period END scalar_period,"
            "e.kind entity_kind,e.account_type,"
            "EXISTS(SELECT 1 FROM calculation_seal z WHERE z.calculation_id=c.id) cs,"
            "EXISTS(SELECT 1 FROM fact_seal z WHERE z.fact_id=c.fact_id) fs "
            "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
            "LEFT JOIN calculation_publication p ON p.calculation_id=c.id "
            "LEFT JOIN fact_revision f ON f.id=c.fact_id "
            "LEFT JOIN subject s ON s.id=f.subject_id "
            "LEFT JOIN fact_bank_statement b ON b.revision_id=f.id "
            "LEFT JOIN fact_bank_reconciliation r ON r.revision_id=f.id "
            "LEFT JOIN entity e ON e.id=CASE s.kind "
            "WHEN 'bank_statement' THEN b.bank_account_id "
            "WHEN 'bank_reconciliation' THEN r.bank_account_id END",
            parameters,
        ):
            ident = row["calc_id"]
            if (
                ident in headers
                or row["id"] is None
                or not row["cs"]
                or not row["fs"]
                or row["calc_kind"] not in {"bank_statement", "bank_reconciliation"}
                or (row["calc_subject"], row["calc_kind"], row["calc_period"])
                != (row["fact_subject"], row["fact_kind"], row["fact_period"])
                or row["scalar_period"] != row["fact_period"]
                or row["calc_subject"] != row["subject_id"]
                or row["calc_id"] != row["calculation_id"]
                or row["posting_period"] != frozen_periods[ident]
                or not row["calc_period"] < through_period
                or not row["posting_period"] <= through_period
                or row["entity_kind"] != "fund_account"
                or row["account_type"] != "bank"
            ):
                raise KernelError("content_integrity_failed", "历史银行账户来源身份不一致")
            verify_record(row)
            headers[ident] = row
        if headers.keys() != identifiers:
            raise KernelError("content_integrity_failed", "历史银行账户来源缺失")
        closes = self.authoritative_close_rows(
            periods=set(frozen_periods.values()), through_period=through_period
        )
        if {row["period"] for row in closes} != set(frozen_periods.values()):
            raise KernelError("content_integrity_failed", "历史银行账户缺少冻结采用")
        scopes = {row["period"]: set() for row in closes}
        for ident, period in frozen_periods.items():
            scopes[period].add(headers[ident]["calc_subject"])
        adopted = {
            (part.period, item["calculation_id"]): item
            for part in self.close_adopted_results_many(
                closes,
                subjects={row["calc_subject"] for row in headers.values()},
                subjects_by_period=scopes,
            )
            for item in part.adopted_results
        }
        result = {}
        for ident, row in headers.items():
            item = adopted.get((frozen_periods[ident], ident))
            if item is None or any(
                item[field] != expected
                for field, expected in (
                    ("publication_id", row["id"]),
                    ("subject_id", row["calc_subject"]),
                    ("fact_id", row["calc_fact"]),
                    ("source_period", str(YearMonth.from_ordinal(row["calc_period"]))),
                    ("posting_period", str(YearMonth.from_ordinal(row["posting_period"]))),
                    ("result_digest", row["calc_digest"].hex()),
                )
            ):
                raise KernelError("content_integrity_failed", "历史银行账户冻结采用不一致")
            if ident in reusable:
                outcome, result_digest = reusable[ident]
                if result_digest != item["result_digest"]:
                    raise KernelError("content_integrity_failed", "历史银行账户采用正文不一致")
            else:
                outcome = stored_json.verify_outcome_bytes(
                    row["outcome"], bytes.fromhex(item["result_digest"]), ident
                )
            if (
                not isinstance(outcome, dict)
                or not isinstance(outcome.get("values"), dict)
                or outcome.get("lines") != []
                or outcome.get("opening") is not False
                or outcome["values"].get("bank_account_id") != row["bank_account_id"]
            ):
                raise KernelError("content_integrity_failed", "历史银行账户标量与采用来源不一致")
            result[ident] = row["bank_account_id"]
        return result

    def close_adopted_results_many(self, rows, *, subjects, subjects_by_period=None):
        """Read adopted leaves; released v1 keeps its complete accounting proof."""
        return self._close_accounting_many(
            rows, subjects=subjects, adopted_only=True, subjects_by_period=subjects_by_period
        )

    def _close_accounting_many(
        self, rows, *, subjects, adopted_only, subjects_by_period=None, asset_owners=False
    ):
        """Stage current-reader groups; retain v1's per-month proof semantics."""
        from . import close_storage
        from .content_history_context import close_reader

        rows = tuple(rows)
        if not rows:
            return ()
        subjects = frozenset(subjects)
        if subjects_by_period is not None:
            subjects_by_period = {
                period: frozenset(scope) for period, scope in subjects_by_period.items()
            }
            if (
                set(subjects_by_period) != {row["period"] for row in rows}
                or any(not scope <= subjects for scope in subjects_by_period.values())
            ):
                raise ValueError("accounting scopes must match the requested closes and subjects")
        reader = close_reader()
        if reader is not close_storage:
            # The released v1 reader retains its own historical single-month
            # rule and is never handed a current-version authority package.
            return tuple(self.close_accounting(row, subjects=subjects) for row in rows)
        cache = self._close_adopted_result_slices if adopted_only else self._close_accounting_slices
        pending = {}
        requested_keys = []
        for row in rows:
            scope = subjects if subjects_by_period is None else subjects_by_period[row["period"]]
            # Complete scoped slices prove absence against the entire requested
            # authority universe. A smaller universe's success cannot hide an
            # omitted publication when the same bucket scope is requested later.
            key = (
                (row["period"], scope, subjects)
                if subjects_by_period is not None and not adopted_only and scope != subjects
                else (row["period"], scope)
            )
            if asset_owners:
                # The committed owner roster proves this narrower read scope.
                # It is distinct from the ordinary whole-universe proof, even
                # though both return the same full subject selection.
                key = (*key, "asset_owners")
            requested_keys.append(key)
            if not self._snapshot_active or key not in cache:
                pending.setdefault(key, row)
        staged = {}
        staged_headers = {}
        if pending:
            missing_rows = {
                row["period"]: row for row in pending.values()
                if not self._snapshot_active or row["period"] not in self._close_headers
            }
            verified = close_storage.verified_headers(self.connection, missing_rows.values())
            staged_headers.update((header.period, header) for header in verified)
            headers = []
            for key in pending:
                period = key[0]
                header = (
                    self._close_headers[period]
                    if self._snapshot_active and period in self._close_headers
                    else staged_headers[period]
                )
                staged_headers[period] = header
                headers.append(header)
            read_many = (
                reader.read_adopted_results_many if adopted_only else reader.read_accounting_many
            )
            if asset_owners:
                read_many = reader.read_asset_owner_accounting_many
            results = read_many(
                self.connection,
                headers,
                subjects,
                _verified_parts=(
                    self._verified_close_storage_parts if self._snapshot_active else None
                ),
                _positions_cache=(
                    self._close_accounting_positions if self._snapshot_active else None
                ),
                **(
                    {"subjects_by_period": {key[0]: key[1] for key in pending}}
                    if subjects_by_period is not None
                    else {}
                ),
            )
            staged.update(zip(pending, results, strict=True))
        if self._snapshot_active:
            self._close_headers.update(staged_headers)
            cache.update(staged)
        return tuple((cache if self._snapshot_active else staged)[key] for key in requested_keys)

    def close_material_sources(self, row, *, source_ids):
        from .content_history_context import close_reader

        return close_reader().read_material_sources(
            self.connection, self.close_header(row), source_ids
        )

    def close_material_source_summaries(self, row, *, source_ids):
        from .content_history_context import close_reader

        return close_reader().material_source_summaries(
            self.connection, self.close_header(row), source_ids
        )

    def close_section(self, row, name):
        from .content_history_context import close_reader

        key = (row["period"], name)
        if self._snapshot_active and key in self._close_sections:
            return self._close_sections[key]
        result = close_reader().read_section(self.connection, self.close_header(row), name)
        if self._snapshot_active:
            self._close_sections[key] = result
        return result

    def close_readiness_check(self, row, name):
        from .content_history_context import close_reader

        key = (row["period"], "readiness:" + name)
        if self._snapshot_active and key in self._close_sections:
            return self._close_sections[key]
        result = close_reader().read_readiness_check(self.connection, self.close_header(row), name)
        if self._snapshot_active:
            self._close_sections[key] = result
        return result

    def close_readiness_checks_many(self, rows, name):
        """Read one checker across exact closes without changing its proof scope."""
        from . import close_storage

        rows = tuple(rows)
        if not _owns_current_selector_snapshot(self, self.connection):
            return tuple(self.close_readiness_check(row, name) for row in rows)
        periods = [row["period"] for row in rows]
        if any(type(period) is not int for period in periods) or len(set(periods)) != len(rows):
            raise ValueError("readiness closes must have distinct integer periods")
        if type(name) is not str or not name:
            raise ValueError("readiness checker name must be nonempty")
        keys = [(period, "readiness:" + name) for period in periods]
        pending = [
            row for row, key in zip(rows, keys, strict=True) if key not in self._close_sections
        ]
        if pending:
            fresh = [row for row in pending if row["period"] not in self._close_headers]
            staged_headers = dict(zip(
                (row["period"] for row in fresh),
                close_storage.verified_headers(self.connection, fresh), strict=True,
            ))
            headers = tuple(
                self._close_headers[row["period"]]
                if row["period"] in self._close_headers else staged_headers[row["period"]]
                for row in pending
            )
            values = close_storage.read_readiness_checks_many(self.connection, headers, name)
            # A late damaged month must not publish even earlier successful
            # checker or header proofs from this batch.
            staged_sections = dict(zip(
                ((row["period"], "readiness:" + name) for row in pending), values, strict=True,
            ))
            self._close_headers.update(staged_headers)
            self._close_sections.update(staged_sections)
        return tuple(self._close_sections[key] for key in keys)

    def authoritative_close_rows(self, *, periods, through_period=None):
        """Read exact publication periods without trusting reverse directory discovery.

        A source/marker-checked row does not enter _closes, which is reserved for
        full close_reference multiset checks. Managed snapshots may reuse either.
        """
        from .read_indexes import authoritative_close_rows

        requested = tuple(sorted(set(periods)))
        if any(type(period) is not int for period in requested):
            raise ValueError("authoritative close periods must be integer ordinals")
        if not self._snapshot_active:
            rows, _ = authoritative_close_rows(
                self.connection, periods=requested, through_period=through_period
            )
            return rows
        missing = [
            period for period in requested
            if period not in self._authoritative_closes and period not in self._closes
        ]
        if missing:
            rows, headers = authoritative_close_rows(
                self.connection, periods=missing, through_period=through_period
            )
            self._authoritative_closes.update({row["period"]: row for row in rows})
            self._close_headers.update(headers)
        return [
            cached_row
            for period in requested
            if (through_period is None or period <= through_period)
            if (cached_row := (
                self._authoritative_closes[period]
                if period in self._authoritative_closes else self._closes.get(period)
            )) is not None
        ]

    def job_rows(self, *, subject_id=None, period):
        from .read_indexes import job_rows

        key = (
            None
            if subject_id is None
            else frozenset({subject_id} if isinstance(subject_id, str) else subject_id),
            period,
        )
        if key not in self._jobs:
            self._jobs[key] = job_rows(self.connection, subject_id=subject_id, period=period)
        return self._jobs[key]

    def related_subjects(self, subjects):
        """Candidate closure from immutable edges and declared exact read scopes.

        Scope declarations remain candidates when a dependency edge cannot prove
        the source.  The relation resolver, never this query, establishes payment.
        """
        subjects = set(subjects)
        if not subjects:
            return set()
        return {
            row[0]
            for row in self.connection.execute(
                "WITH RECURSIVE related(id) AS (SELECT c.id FROM json_each(?) ids "
                "JOIN calculation c ON c.subject_id=ids.value UNION "
                "SELECT d.calculation_id FROM dependency_calculation d JOIN related r "
                "ON r.id=d.upstream_id UNION SELECT d.calculation_id FROM related r "
                "JOIN calculation c ON c.id=r.id JOIN dependency_scope d "
                "ON d.source='calculation' AND d.scope_key IN ('@'||c.subject_id,'#'||c.id)) "
                "SELECT DISTINCT c.subject_id FROM related r CROSS JOIN calculation c ON c.id=r.id",
                (canonical(sorted(subjects)),),
            )
        } | subjects

    def settlement_subjects(self, period=None):
        """Select raw declared obligation/relationship candidates, not paid states."""
        kinds = canonical(sorted(SETTLEMENT_KINDS))
        # The obligations index is a negative filter. Check only the object
        # members that decide that filter before JSON1 chooses its first key.
        # json_each exposes escaped-equivalent keys too. This is not a proof of
        # unrelated outcome fields and must not populate the full-body cache.
        # The other UNION arm is independent of outcome JSON.
        candidate_sql = (
            "SELECT c.id FROM calculation c WHERE c.kind NOT IN (SELECT value FROM json_each(?))"
        )
        candidate_parameters = [kinds]
        if period is not None:
            candidate_sql += (
                " AND EXISTS(SELECT 1 FROM calculation_publication p "
                "WHERE p.subject_id=c.subject_id AND p.posting_period<=?)"
            )
            candidate_parameters.append(YearMonth(period).ordinal)
        ambiguous = self.connection.execute(
            candidate_sql + " AND (json_type(c.outcome) IS NOT 'object' "
            "OR (SELECT count(*) FROM json_each(c.outcome) j WHERE j.key='values')!=1 "
            "OR json_type(c.outcome,'$.values') IS NOT 'object' "
            "OR (SELECT count(*) FROM json_each(c.outcome,'$.values') j "
            "WHERE j.key='obligations')>1 "
            "OR (json_type(c.outcome,'$.values.obligations') IS NOT NULL "
            "AND json_type(c.outcome,'$.values.obligations')!='array')) LIMIT 1",
            candidate_parameters,
        ).fetchone()
        if ambiguous is not None:
            raise KernelError(
                "content_integrity_failed",
                "已保存的核算结果义务字段解释不唯一或格式错误",
                component="calculation",
                record_id=ambiguous[0],
            )
        query = (
            "SELECT c.subject_id FROM calculation c INDEXED BY calculation_obligations WHERE "
            "json_array_length(c.outcome,'$.values.obligations')>0 "
            "UNION SELECT c.subject_id FROM calculation c WHERE "
            "c.kind IN (SELECT value FROM json_each(?))"
        )
        parameters = [kinds]
        if period is not None:
            query = (
                "SELECT candidate.subject_id FROM (" + query + ") candidate WHERE EXISTS("
                "SELECT 1 FROM calculation c JOIN calculation_publication p "
                "ON p.calculation_id=c.id WHERE c.subject_id=candidate.subject_id "
                "AND p.posting_period<=?)"
            )
            parameters.append(YearMonth(period).ordinal)
        return {row[0] for row in self.connection.execute(query, parameters)}

    @staticmethod
    def declared_sources(calculation):
        data = calculation["fact_data"]
        kind = calculation["kind"]
        if kind in SETTLEMENT_SOURCE_SLOTS:
            references = data.get(SETTLEMENT_SOURCE_SLOTS[kind][0], ())
        elif kind == "settlement":
            references = (data.get("first", {}), data.get("second", {}))
        elif kind == "overpayment":
            references = (data,)
        else:
            references = ()
        return references

    @classmethod
    def declared_subjects(cls, calculation):
        return {
            item.get("source_id")
            for item in cls.declared_sources(calculation)
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        }
