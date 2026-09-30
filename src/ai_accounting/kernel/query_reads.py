"""Exact, request-local batch reads shared by business queries and page projections.

Ordinary callers own their transaction. snapshot() owns a read-only transaction
and scopes successful reference checks to that lifetime. Consumers read shared
source objects without modifying them; selection rules stay in the business query.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager

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
            stored_json.verify_outcome_bytes(
                row["stored_outcome"], row["result_digest"], ident
            )
        self._identifiers.add(ident)

    def commit(self):
        self._require_active()
        self._reads._verified_sql_outcomes.update(self._identifiers)
        self._committed = True


def verify_current_voucher_publications(connection, voucher_ids):
    """Prove formal adoption for selected, still-current open vouchers.

    A no-impact review keeps the old voucher version, so both its original
    publication and the subject's current reviewed publication must exist.
    Closed vouchers use their separate frozen adoption proof. Asset batch
    members have no independent voucher and never enter this selected set.
    """

    identifiers = set(voucher_ids)
    if not identifiers:
        return
    selected = list(
        connection.execute(
            "SELECT ids.value requested_id,v.id,v.voucher_id,v.calculation_id,"
            "v.reverses_id,v.period,"
            "c.subject_id,h.version_id current_version_id,"
            "closes.closed_through "
            "FROM json_each(?) ids LEFT JOIN voucher_version v ON v.id=ids.value "
            "LEFT JOIN calculation c ON c.id=v.calculation_id "
            "LEFT JOIN voucher_current h ON h.version_id=v.id "
            "CROSS JOIN (SELECT coalesce(max(period),-1) closed_through "
            "FROM period_close) closes",
            (canonical(sorted(identifiers)),),
        )
    )
    if len(selected) != len(identifiers) or any(row["id"] is None for row in selected):
        raise KernelError("content_integrity_failed", "选中凭证版本缺少权威来源")
    current = [
        row
        for row in selected
        if row["current_version_id"] is not None and row["period"] > row["closed_through"]
    ]
    if not current:
        return
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
    reads = _active_fact_reads.get()
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
        sql += " AND vc.kind IN (SELECT value FROM json_each(?))"
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
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            reads = cls(engine, connection)

            def keep_snapshot(action, *_):
                # in_transaction alone cannot distinguish COMMIT followed by BEGIN.
                # This connection is private to this scope; its consumers only read.
                return (
                    sqlite3.SQLITE_DENY
                    if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT)
                    else sqlite3.SQLITE_OK
                )

            connection.set_authorizer(keep_snapshot)
            reads._snapshot_active = True
            token = _active_fact_reads.set(reads)
            try:
                yield reads
            finally:
                _active_fact_reads.reset(token)
                reads._reset()
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
                for period in self._closes.keys() | self._authoritative_closes.keys()
                if period in self._close_manifests
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
        """Check each selected frozen voucher hit before consuming its result."""
        from .read_indexes import selected_voucher_references

        frozen = {row["id"]: row["close_period"] for row in rows if row["close_period"] is not None}
        if not frozen:
            return
        references = selected_voucher_references(
            self.connection, sorted(frozen), through_period=through_period
        )
        found = {(row["reference_id"], row["close_period"]) for row in references}
        if any((ident, period) not in found for ident, period in frozen.items()):
            raise KernelError("content_integrity_failed", "冻结凭证采用来源缺失")
        self.verify_close_references(references)

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
        from .publication import verified_period_headers

        periods = set(periods)
        pending = periods - self._verified_publications.keys()
        grouped = {period: [] for period in pending}
        if pending:
            for value in verified_period_headers(self.connection, pending):
                grouped[value["posting_period"]].append(value)
            if self._snapshot_active:
                self._verified_publications.update(
                    {period: tuple(rows) for period, rows in grouped.items()}
                )
        return [
            row
            for period in sorted(periods)
            for row in self._verified_publications.get(period, grouped.get(period, ()))
        ]

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

    def metadata(self, identifiers, *, state=True, outcomes=None):
        identifiers = set(identifiers)
        missing = {
            ident
            for ident in identifiers
            if ident not in self._metadata
            or (state and "line_count" not in self._metadata[ident])
            or outcomes is not None
        }
        if missing:
            if state:
                self.verify_sql_outcomes(missing)
            expressions = (
                "json_array_length(c.outcome,'$.lines') AS line_count,"
                "coalesce(json_extract(c.outcome,'$.opening'),0) AS opening "
                if state
                else "NULL AS line_count,NULL AS opening "
            )
            for row in self.connection.execute(
                "SELECT c.id,c.subject_id,c.kind,c.period,c.fact_id,c.digest,c.program_version,"
                "f.revision AS fact_revision,"
                "p.id AS publication_id,p.mode AS publication_mode,"
                "p.posting_period,p.voucher_id,"
                + ("c.outcome AS outcome," if outcomes is not None else "")
                + expressions
                + "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                "LEFT JOIN calculation_publication p ON p.calculation_id=c.id "
                "JOIN fact_revision f ON f.id=c.fact_id",
                (canonical(sorted(missing)),),
            ):
                value = dict(row)
                if outcomes is not None:
                    outcomes[value["id"]] = value.pop("outcome")
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
                self._metadata[value["id"]] = value
            if any(ident not in self._metadata for ident in missing) or (
                outcomes is not None and any(ident not in outcomes for ident in missing)
            ):
                raise KernelError("unknown_calculation", "计算版本不存在")
        return {ident: self._metadata[ident] for ident in identifiers}

    def verify_sql_outcomes(self, identifiers):
        identifiers = set(identifiers)
        missing = (
            identifiers - self._verified_sql_outcomes
            if self._snapshot_active else identifiers
        )
        if missing:
            verify_sql_outcomes(self.connection, missing)
            if self._snapshot_active:
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

    def asset_members_many(self, owner_calculation_ids):
        """Validate selected frozen owners together and retain successful snapshot reads."""
        owner_ids = tuple(dict.fromkeys(owner_calculation_ids))
        missing = tuple(ident for ident in owner_ids if ident not in self._asset_members)
        if missing:
            from .asset_batches import frozen_members_many

            verified = frozen_members_many(self.connection, missing)
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
            outcomes = {}
            metadata = self.metadata(missing, state=False, outcomes=outcomes)
            facts = self.facts({row["fact_id"] for row in metadata.values()})
            prepared = {}
            for ident, outcome in outcomes.items():
                value = metadata[ident]
                prepared[ident] = {
                    key: item
                    for key, item in value.items()
                    if key not in {"line_count", "opening", "fact_revision"}
                } | {
                    "fact_data": facts[value["fact_id"]]["data"],
                    "outcome": self._stored_outcome(outcome),
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
            metadata = self.metadata(missing)
            facts = self.store.fact_data_many(
                self.connection, {row["fact_id"] for row in metadata.values()}
            )
            for row in self.connection.execute(
                "SELECT c.id,c.outcome FROM json_each(?) ids JOIN calculation c ON c.id=ids.value",
                (canonical(sorted(missing)),),
            ):
                value = metadata[row["id"]]
                self._raw_calculations[row["id"]] = {
                    key: item
                    for key, item in value.items()
                    if key not in {"line_count", "opening", "fact_revision"}
                } | {
                    "fact_data": facts[value["fact_id"]],
                    "outcome": self._stored_outcome(row["outcome"]),
                }
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
        missing = identifiers - self._verified_source_contents.keys()
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
        from .integrity import _object, verify_fact_child_order
        from .storage import _snapshot_fact_hashes, _snapshot_fact_raws

        fact_ids = {row["fact_id"] for row in headers.values()}
        raw_hashes = _snapshot_fact_hashes(self.store, self.connection, fact_ids)
        facts = _snapshot_fact_raws(self.store, self.connection, fact_ids)
        try:
            facts.update(self.store.fact_data_many(self.connection, fact_ids - facts.keys()))
        except (KeyError, ValueError) as error:
            raise KernelError("content_integrity_failed", "本次读取的事实内容无法解码") from error
        except KernelError as error:
            if error.code != "unknown_fact":
                raise
            raise KernelError("content_integrity_failed", "本次读取的事实内容缺失") from error
        if facts.keys() != fact_ids:
            raise KernelError("content_integrity_failed", "本次读取的事实内容缺失")
        kinds = {}
        for header in headers.values():
            kinds.setdefault(header["fact_kind"], set()).add(header["fact_id"])
        verify_fact_child_order(self.engine, self.connection, kinds)
        verified = {}
        for ident, header in headers.items():
            fact_id = header["fact_id"]
            fact = facts[fact_id]
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
            if (
                not header["calculation_sealed"]
                or not header["fact_sealed"]
                or (header["subject_id"], header["kind"], header["period"])
                != (header["fact_subject"], header["fact_kind"], header["fact_period"])
                or fact.get("period") != str(YearMonth.from_ordinal(header["fact_period"]))
                or (
                    raw_hashes.get(fact_id) != header["fact_digest"]
                    and digest(fact) != header["fact_digest"]
                )
                # The writer already stores canonical JSON. Hash those bytes
                # directly; retain canonical comparison for an equivalent
                # stored representation, rather than changing its semantics.
                or not outcome_digest_matches
            ):
                raise KernelError("content_integrity_failed", "本次读取的核算来源封签不一致")
            verified[ident] = outcome
        if self._snapshot_active:
            self._verified_source_contents.update(verified)
        return {
            ident: verified[ident] if ident in verified else self._verified_source_contents[ident]
            for ident in identifiers
        }

    def _verify_anchored_source_bytes(self, proof):
        """Check an already authenticated report anchor's exact saved sources.

        The report reader alone mints this private proof after checking the
        immutable anchor against its saved body. No decoded result is returned
        or inserted into the ordinary decoded-content cache.
        """
        from .integrity import _object, verify_fact_child_order
        from .report_open_contribution import _require_source_proof
        from .schema import table_name
        from .storage import _scalar_fact_hashes, _snapshot_fact_hashes, _snapshot_fact_raws

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
        headers = {
            row["id"]: dict(row)
            for row in self.connection.execute(
                "SELECT c.id,c.subject_id,c.kind,c.period,c.fact_id,c.digest,c.outcome,"
                "f.subject_id fact_subject,f.period fact_period,f.digest fact_digest,"
                "s.kind fact_kind,"
                "EXISTS(SELECT 1 FROM calculation_seal z WHERE z.calculation_id=c.id) "
                "calculation_sealed,"
                "EXISTS(SELECT 1 FROM fact_seal z WHERE z.fact_id=f.id) fact_sealed "
                "FROM json_each(?) ids CROSS JOIN calculation c ON c.id=ids.value "
                "JOIN fact_revision f ON f.id=c.fact_id JOIN subject s ON s.id=f.subject_id",
                (canonical(sorted(pending_bindings)),),
            )
        }
        if headers.keys() != pending_bindings.keys():
            raise KernelError("content_integrity_failed", "本次读取的核算来源缺失")
        pending = {}
        kinds = {}
        fact_periods = {}
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
                kinds.setdefault(header["fact_kind"], set()).add(header["fact_id"])
                fact_periods[header["fact_id"]] = header["fact_period"]
        if not pending:
            self._anchored_source_bytes.update(pending_bindings)
            return actual

        verify_fact_child_order(self.engine, self.connection, kinds)
        for kind, fact_ids in kinds.items():
            physical = {
                row["revision_id"]: row["period"]
                for row in self.connection.execute(
                    f"SELECT f.revision_id,f.period FROM json_each(?) ids "
                    f"CROSS JOIN {table_name(kind)} f ON f.revision_id=ids.value",
                    (canonical(sorted(fact_ids)),),
                )
            }
            if physical.keys() != fact_ids or any(
                physical[ident] != fact_periods[ident] for ident in fact_ids
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
        fact_digests = {header["fact_id"]: header["fact_digest"] for header in pending.values()}
        fallback_ids = {ident for ident in fact_ids if raw_hashes.get(ident) != fact_digests[ident]}
        if fallback_ids:
            facts = _snapshot_fact_raws(self.store, self.connection, fallback_ids)
            try:
                facts.update(
                    self.store.fact_data_many(self.connection, fallback_ids - facts.keys())
                )
            except (KeyError, ValueError) as error:
                raise KernelError(
                    "content_integrity_failed", "本次读取的事实内容无法解码"
                ) from error
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

    def close_accounting_many(self, rows, *, subjects):
        """Stage current-reader groups; retain v1's per-month proof semantics."""
        from . import close_storage
        from .content_history_context import close_reader

        rows = tuple(rows)
        if not rows:
            return ()
        subjects = frozenset(subjects)
        reader = close_reader()
        if reader is not close_storage:
            # The released v1 reader retains its own historical single-month
            # rule and is never handed a current-version authority package.
            return tuple(self.close_accounting(row, subjects=subjects) for row in rows)
        pending = {}
        for row in rows:
            key = (row["period"], subjects)
            if not self._snapshot_active or key not in self._close_accounting_slices:
                pending.setdefault(key, row)
        staged = {}
        staged_headers = {}
        if pending:
            headers = []
            for key, row in pending.items():
                period = key[0]
                header = (
                    self._close_headers[period]
                    if self._snapshot_active and period in self._close_headers
                    else reader.verified_header(self.connection, row)
                )
                staged_headers[period] = header
                headers.append(header)
            results = reader.read_accounting_many(
                self.connection, headers, subjects,
                _verified_parts=(
                    self._verified_close_storage_parts if self._snapshot_active else None
                ),
                _positions_cache=(
                    self._close_accounting_positions if self._snapshot_active else None
                ),
            )
            staged.update(zip(pending, results, strict=True))
        if self._snapshot_active:
            self._close_headers.update(staged_headers)
            self._close_accounting_slices.update(staged)
        return tuple(
            self._close_accounting_slices[(row["period"], subjects)]
            if self._snapshot_active
            else staged[(row["period"], subjects)]
            for row in rows
        )

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
        result = close_reader().read_readiness_check(
            self.connection, self.close_header(row), name
        )
        if self._snapshot_active:
            self._close_sections[key] = result
        return result

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
        cached = self._closes | self._authoritative_closes
        missing = [period for period in requested if period not in cached]
        if missing:
            rows, headers = authoritative_close_rows(
                self.connection, periods=missing, through_period=through_period
            )
            self._authoritative_closes.update({row["period"]: row for row in rows})
            self._close_headers.update(headers)
        cached = self._closes | self._authoritative_closes
        return [
            cached_row
            for period in requested
            if (through_period is None or period <= through_period)
            if (cached_row := cached.get(period)) is not None
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
            "SELECT c.id FROM calculation c WHERE "
            "c.kind NOT IN (SELECT value FROM json_each(?))"
        )
        candidate_parameters = [kinds]
        if period is not None:
            candidate_sql += (
                " AND EXISTS(SELECT 1 FROM calculation_publication p "
                "WHERE p.subject_id=c.subject_id AND p.posting_period<=?)"
            )
            candidate_parameters.append(YearMonth(period).ordinal)
        ambiguous = self.connection.execute(
            candidate_sql
            + " AND (json_type(c.outcome) IS NOT 'object' "
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
