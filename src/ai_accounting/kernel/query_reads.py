"""Exact, request-local batch reads shared by business queries and page projections.

The caller owns the SQLite read transaction.  Caches never cross connections and
hold immutable versions only; selecting current or frozen versions stays in the
business query selector.
"""

from __future__ import annotations

import json

from .contracts import KernelError
from .query_semantics import SETTLEMENT_SOURCE_SLOTS, resolve_calculation_relations
from .types import YearMonth, canonical

SETTLEMENT_KINDS = frozenset(
    {
        *SETTLEMENT_SOURCE_SLOTS,
        "settlement",
        "overpayment",
        "opening_package",
    }
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
    sql = (
        prefix + "candidates AS (SELECT v.*,n.number,vc.subject_id AS voucher_subject_id,"
        "(SELECT min(r.close_period) FROM close_reference r WHERE r.reference_type='voucher' "
        "AND r.reference_id=v.id AND r.close_period<=?) AS close_period "
        f"FROM {driver} CROSS JOIN voucher n ON n.id=v.voucher_id "
        "CROSS JOIN calculation vc ON vc.id=v.calculation_id WHERE v.period<=?"
    )
    parameters.extend((cutoff, cutoff))
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
        subjects = canonical(sorted({subject_ids} if isinstance(subject_ids, str) else subject_ids))
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


class QueryReads:
    def __init__(self, engine, connection):
        self.engine, self.store, self.connection = engine, engine.store, connection
        self._fact_versions = {}
        self._facts = {}
        self._metadata = {}
        self._calculations = {}
        self._parents = {}
        self._vouchers = {}
        self._lines = {}
        self._relations = {}
        self._relation_sources = {}
        self._ancestor_roots = set()
        self._closes = {}
        self._close_manifests = {}
        self.closed_accounting_contexts = {}
        self._selections = {}
        self._typed_calculations = {}
        self._jobs = {}
        self.job_plans = {}

    def fact_versions(self, identifiers):
        identifiers = set(identifiers)
        missing = identifiers - self._fact_versions.keys()
        if missing:
            self._fact_versions.update(self.store.facts(self.connection, missing))
        return {ident: self._fact_versions[ident] for ident in identifiers}

    def fact_version(self, ident):
        return self.fact_versions((ident,))[ident]

    def prime_select(self, reads):
        """Batch the existing Read contract without changing scope or version choice."""
        requested = list(dict.fromkeys(read for read in reads if read not in self._selections))
        for source in ("fact", "calculation"):
            group = [read for read in requested if read.source == source]
            if not group:
                continue
            specifications = [
                [
                    index,
                    read.kind,
                    read.key,
                    read.before_period.ordinal if read.before_period else 119988,
                ]
                for index, read in enumerate(group)
            ]
            q = (
                "WITH requests AS (SELECT json_extract(value,'$[0]') AS slot,"
                "json_extract(value,'$[1]') AS kind,json_extract(value,'$[2]') AS key,"
                "json_extract(value,'$[3]') AS cutoff FROM json_each(?)), ids AS ("
            )
            if source == "fact":
                q += (
                    "SELECT q.slot,f.id FROM requests q JOIN fact_revision f "
                    "ON f.id=substr(q.key,2) JOIN subject s ON s.id=f.subject_id "
                    "JOIN fact_seal z ON z.fact_id=f.id WHERE substr(q.key,1,1)='#' "
                    "AND (q.kind='*' OR q.kind=s.kind) AND f.period<q.cutoff UNION ALL "
                    "SELECT q.slot,f.id FROM requests q JOIN subject s ON s.kind=q.kind "
                    "JOIN fact_current a ON a.subject_id=s.id JOIN fact_revision f "
                    "ON f.id=a.fact_id WHERE q.key='*' AND f.period<q.cutoff UNION ALL "
                    "SELECT q.slot,f.id FROM requests q JOIN fact_scope x ON x.scope_key=q.key "
                    "JOIN fact_current a ON a.fact_id=x.fact_id "
                    "JOIN fact_revision f ON f.id=a.fact_id "
                    "WHERE q.key<>'*' AND substr(q.key,1,1)<>'#' "
                    "AND (q.kind='*' OR q.kind=x.kind) AND f.period<q.cutoff) "
                    "SELECT ids.slot,f.id,f.subject_id,f.period FROM ids "
                    "JOIN fact_revision f ON f.id=ids.id "
                    "ORDER BY ids.slot,f.subject_id"
                )
            else:
                q += (
                    "SELECT q.slot,c.id FROM requests q JOIN calculation c ON c.id=substr(q.key,2) "
                    "JOIN calculation_seal z ON z.calculation_id=c.id WHERE substr(q.key,1,1)='#' "
                    "AND (q.kind='*' OR q.kind=c.kind) AND c.period<q.cutoff UNION ALL "
                    "SELECT q.slot,c.id FROM requests q JOIN calculation c ON c.kind=q.kind "
                    "JOIN calculation_current a ON a.calculation_id=c.id "
                    "WHERE q.key='*' AND c.period<q.cutoff UNION ALL "
                    "SELECT q.slot,c.id FROM requests q "
                    "JOIN calculation_scope x ON x.scope_key=q.key "
                    "JOIN calculation_current a ON a.calculation_id=x.calculation_id "
                    "JOIN calculation c ON c.id=a.calculation_id WHERE q.key<>'*' "
                    "AND substr(q.key,1,1)<>'#' AND (q.kind='*' OR q.kind=x.kind) "
                    "AND c.period<q.cutoff) SELECT ids.slot,c.* FROM ids "
                    "JOIN calculation c ON c.id=ids.id ORDER BY ids.slot,c.period,c.subject_id"
                )
            rows = list(self.connection.execute(q, (canonical(specifications),)))
            if source == "fact":
                versions = self.fact_versions({row["id"] for row in rows})
            else:
                for row in rows:
                    if row["id"] not in self._typed_calculations:
                        self._typed_calculations[row["id"]] = self.store.calculation(row)
                versions = self._typed_calculations
            grouped = [[] for _ in group]
            for row in rows:
                grouped[row["slot"]].append(versions[row["id"]])
            self._selections.update(
                (read, tuple(grouped[index])) for index, read in enumerate(group)
            )

    def select(self, read):
        if read.key == "*" and read.kind == "*":
            raise KernelError("unbounded_read", "whole-company wildcard reads are not supported")
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

    def metadata(self, identifiers, *, state=True):
        identifiers = set(identifiers)
        missing = {
            ident
            for ident in identifiers
            if ident not in self._metadata or (state and "line_count" not in self._metadata[ident])
        }
        if missing:
            expressions = (
                "json_array_length(c.outcome,'$.lines') AS line_count,"
                "coalesce(json_extract(c.outcome,'$.opening'),0) AS opening "
                if state
                else "NULL AS line_count,NULL AS opening "
            )
            for row in self.connection.execute(
                "SELECT c.id,c.subject_id,c.kind,c.period,c.fact_id,c.digest,c.program_version,"
                "f.revision AS fact_revision,"
                "p.posting_period,p.voucher_id,"
                + expressions
                + "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                "JOIN calculation_publication p ON p.calculation_id=c.id "
                "JOIN fact_revision f ON f.id=c.fact_id",
                (canonical(sorted(missing)),),
            ):
                value = dict(row)
                value["result_digest"] = value.pop("digest").hex()
                value["period"] = str(YearMonth.from_ordinal(value["period"]))
                value["posting_period"] = str(YearMonth.from_ordinal(value["posting_period"]))
                if not state:
                    value.pop("line_count")
                    value.pop("opening")
                self._metadata[value["id"]] = value
            if missing - self._metadata.keys():
                raise KernelError("unknown_calculation", "计算版本不存在")
        return {ident: self._metadata[ident] for ident in identifiers}

    def calculations(self, identifiers):
        identifiers = set(identifiers)
        missing = identifiers - self._calculations.keys()
        if missing:
            metadata = self.metadata(missing)
            facts = self.facts({row["fact_id"] for row in metadata.values()})
            for row in self.connection.execute(
                "SELECT c.id,c.outcome FROM json_each(?) ids JOIN calculation c ON c.id=ids.value",
                (canonical(sorted(missing)),),
            ):
                value = metadata[row["id"]]
                self._calculations[row["id"]] = {
                    key: item
                    for key, item in value.items()
                    if key not in {"line_count", "opening", "fact_revision"}
                } | {
                    "fact_data": facts[value["fact_id"]]["data"],
                    "outcome": json.loads(row["outcome"]),
                }
        return {ident: self._calculations[ident] for ident in identifiers}

    def calculation(self, ident):
        return self.calculations((ident,))[ident]

    def prime_parents(self, identifiers):
        missing = set(identifiers) - self._parents.keys()
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
            # Frozen slots can retain an exact pointer even when a legacy graph
            # lacks its edge. Batch those immutable records without treating the
            # pointer as proof of dependency or of frozen adoption.
            pointers = set()
            for ident in closure:
                values = self._calculations[ident]["outcome"].get("values", {})
                for field, pointer in (
                    ("settlements", "source_calculation"),
                    ("accepted_sources", "source_calculation_id"),
                    ("tax_transfers", "source_calculation"),
                ):
                    pointers.update(
                        item[pointer]
                        for item in values.get(field, ())
                        if isinstance(item.get(pointer), str)
                    )
            pointers -= self._calculations.keys()
            if pointers:
                existing = {
                    row[0]
                    for row in self.connection.execute(
                        "SELECT c.id FROM json_each(?) ids JOIN calculation c ON c.id=ids.value",
                        (canonical(sorted(pointers)),),
                    )
                }
                self.calculations(existing)
            self._ancestor_roots.update(closure)
        else:
            self.calculations(identifiers)
        return {ident: self._calculations[ident] for ident in identifiers}

    prefetch = prime_calculations

    def relations(self, calculation, *, resolver=resolve_calculation_relations):
        ident = calculation if isinstance(calculation, str) else calculation["id"]
        key = (ident, resolver)
        if key not in self._relations:
            self.prime_calculations((ident,))
            self._relations[key] = resolver(
                self.calculation(ident),
                load_calculation=self.calculation,
                load_parents=self.parents,
                source_cache=self._relation_sources,
            )
        return self._relations[key]

    def vouchers(self, identifiers):
        identifiers = set(identifiers)
        missing = identifiers - self._vouchers.keys()
        if missing:
            for row in self.connection.execute(
                "SELECT v.*,n.number FROM json_each(?) ids "
                "JOIN voucher_version v ON v.id=ids.value "
                "JOIN voucher n ON n.id=v.voucher_id",
                (canonical(sorted(missing)),),
            ):
                self._vouchers[row["id"]] = dict(row)
            if missing - self._vouchers.keys():
                raise KernelError("selected_voucher_missing", "冻结期间引用的凭证版本不存在")
        return {ident: self._vouchers[ident] for ident in identifiers}

    def voucher(self, ident):
        return self.vouchers((ident,))[ident]

    def voucher_lines(self, identifiers):
        identifiers = set(identifiers)
        missing = identifiers - self._lines.keys()
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

        # The source verifier and full membership are shared across selectors in
        # this response.  No close is verified again for another page entity.
        rows = close_rows(self.connection, verified_periods=self._closes.keys(), **scope)
        for row in rows:
            self._closes[row["period"]] = row
        return rows

    def close_manifest(self, row):
        period = row["period"]
        if period not in self._close_manifests:
            self._close_manifests[period] = json.loads(row["manifest"])
        return self._close_manifests[period]

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
        query = (
            "SELECT c.subject_id FROM calculation c INDEXED BY calculation_obligations WHERE "
            "json_array_length(c.outcome,'$.values.obligations')>0 "
            "UNION SELECT c.subject_id FROM calculation c WHERE "
            "c.kind IN (SELECT value FROM json_each(?))"
        )
        parameters = [canonical(sorted(SETTLEMENT_KINDS))]
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
