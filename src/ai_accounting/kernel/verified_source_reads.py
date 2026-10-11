"""Small current-content reader for a completed full source check.

The source checker remains the authority. This adapter only presents its exact
facts, outcomes and saved parents to later independent semantic comparisons.
It never owns a selector snapshot or marks any close adoption verified.
"""

from contextlib import contextmanager

from .contracts import KernelError
from .query_reads import QueryReads
from .query_semantics import resolve_calculation_relations
from .types import YearMonth
from .verified_source_lease import require_verified_calculation_source


@contextmanager
def readonly_verification(connection):
    """Protect a caller's writer while verifying, preserving its transaction."""
    original = connection.execute("PRAGMA query_only").fetchone()[0]
    if not original:
        connection.execute("PRAGMA query_only=ON")
    try:
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise ValueError("verification requires a read-only connection")
        yield
    finally:
        try:
            connection.execute(f"PRAGMA query_only={int(original)}")
            if connection.execute("PRAGMA query_only").fetchone()[0] != original:
                raise ValueError("verification read-only protection was not restored")
        except BaseException:
            connection.close()
            raise


class VerifiedSourceReads(QueryReads):
    """Transaction-bound facts/results/parents, with normal relation semantics."""

    def __init__(self, engine, connection, token):
        if getattr(engine.store.registry, "content_version", None) == 1:
            raise ValueError("current source reader cannot interpret released v1 content")
        super().__init__(engine, connection)
        self._source_token = token
        self._source = require_verified_calculation_source(connection, token)
        self._source_changes = connection.total_changes
        self._require_source()

    def _require_source(self):
        require_verified_calculation_source(self.connection, self._source_token)
        if (
            self.connection.execute("PRAGMA query_only").fetchone()[0] != 1
            or self.connection.total_changes != self._source_changes
        ):
            raise KernelError("content_integrity_failed", "已核验来源的只读事务已失效")

    def facts(self, identifiers):
        self._require_source()
        requested = set(identifiers)
        if not requested <= self._source["facts"].keys():
            raise KernelError("content_integrity_failed", "已核验来源缺少精确事实")
        for ident in requested - self._facts.keys():
            row = self._source["facts"][ident]
            version = self._fact_version(ident)
            self._facts[ident] = {
                "id": ident, "subject_id": row["subject_id"], "revision": row["revision"],
                "kind": row["kind"], "period": str(YearMonth.from_ordinal(row["period"])),
                "data": version.fact.model_dump(mode="json"), "evidence": list(version.evidence),
            }
        return {ident: self._facts[ident] for ident in requested}

    def raw_facts(self, identifiers):
        """Present exact checked stored data without applying the current fact model."""
        self._require_source()
        requested = set(identifiers)
        if not requested <= self._source["facts"].keys():
            raise KernelError("content_integrity_failed", "已核验来源缺少精确事实")
        return {
            ident: {
                "id": ident,
                "subject_id": row["subject_id"],
                "revision": row["revision"],
                "kind": row["kind"],
                "period": str(YearMonth.from_ordinal(row["period"])),
                "data": row["data"],
                "evidence": list(row["evidence"]),
            }
            for ident in requested
            for row in (self._source["facts"][ident],)
        }

    def fact_versions(self, identifiers):
        self._require_source()
        requested = set(identifiers)
        if not requested <= self._source["facts"].keys():
            raise KernelError("content_integrity_failed", "已核验来源缺少精确事实")
        for ident in requested - self._fact_versions.keys():
            self._fact_version(ident)
        return {ident: self._fact_versions[ident] for ident in requested}

    def _fact_version(self, ident):
        from .contracts import FactVersion
        from .storage import _validation_json

        if ident not in self._fact_versions:
            row = self._source["facts"][ident]
            model = self.store.registry.models[row["kind"]]
            self._fact_versions[ident] = FactVersion(
                ident, row["subject_id"], row["revision"],
                model.model_validate_json(_validation_json(row["data"])),
                tuple(sorted(row["evidence"])),
            )
        return self._fact_versions[ident]

    def _calculation(self, ident, *, raw=False):
        cache = self._raw_calculations if raw else self._calculations
        if ident not in cache:
            row = self._source["calculations"].get(ident)
            if row is None:
                raise KernelError("content_integrity_failed", "已核验来源缺少精确核算")
            publication = self._source["publications"].get(ident, {})
            posting = publication.get("posting_period")
            fact = self._source["facts"][row["fact_id"]]
            cache[ident] = {
                "id": ident, "subject_id": row["subject_id"], "kind": row["kind"],
                "period": str(YearMonth.from_ordinal(row["period"])),
                "fact_id": row["fact_id"], "result_digest": row["digest"].hex(),
                "program_version": row["program_version"],
                "publication_id": publication.get("id"),
                "publication_mode": publication.get("mode"),
                "posting_period": (
                    str(YearMonth.from_ordinal(posting)) if posting is not None else None
                ),
                "voucher_id": publication.get("voucher_id"),
                "publication_role": "independent" if posting is not None else "asset_member",
                "fact_data": (
                    fact["data"] if raw else self._fact_version(row["fact_id"]).fact.model_dump(
                        mode="json",
                    )
                ),
                "outcome": row["decoded"],
            }
        return cache[ident]

    def calculations(self, identifiers):
        self._require_source()
        return {ident: self._calculation(ident) for ident in set(identifiers)}

    def calculation(self, ident):
        return self.calculations((ident,))[ident]

    def raw_calculations(self, identifiers):
        self._require_source()
        return {ident: self._calculation(ident, raw=True) for ident in set(identifiers)}

    def metadata(self, identifiers, *, state=True, outcomes=None, _decoded_outcomes=None):
        self._require_source()
        if outcomes is not None and _decoded_outcomes is not None:
            raise ValueError("choose decoded or raw metadata output")
        result = {}
        for ident in set(identifiers):
            calculation = self._calculation(ident, raw=True)
            row = self._source["calculations"][ident]
            value = {key: value for key, value in calculation.items()
                     if key not in {"fact_data", "outcome"}}
            value["fact_revision"] = self._source["facts"][row["fact_id"]]["revision"]
            if state:
                value.update(line_count=len(row["decoded"]["lines"]),
                             opening=int(row["decoded"]["opening"]))
            if outcomes is not None:
                outcomes[ident] = row["outcome"]
            if _decoded_outcomes is not None:
                _decoded_outcomes[ident] = row["decoded"]
            result[ident] = value
        return result

    def _parents_for(self, ident):
        if ident not in self._source["calculations"]:
            raise KernelError("content_integrity_failed", "已核验来源缺少保存依赖")
        if ident not in self._parents:
            self._parents[ident] = tuple(sorted(self._source["dependencies"].get(ident, ())))
        return self._parents[ident]

    def parents(self, ident):
        self._require_source()
        return self._parents_for(ident)

    def prime_calculations(self, identifiers, *, ancestors=True):
        self._require_source()
        return self._prime_calculations(identifiers, ancestors=ancestors)

    def _prime_calculations(self, identifiers, *, ancestors=True, raw=False):
        requested = set(identifiers)
        completed = self._raw_ancestor_roots if raw else self._ancestor_roots
        pending = list(requested - completed) if ancestors else list(requested)
        while pending:
            ident = pending.pop()
            self._calculation(ident, raw=raw)
            if ancestors and ident not in completed:
                completed.add(ident)
                pending.extend(self._parents_for(ident))
        return {ident: self._calculation(ident, raw=raw) for ident in requested}

    prefetch = prime_calculations

    def prime_raw_calculations(self, identifiers, *, verified_calculations=None):
        self._require_source()
        return self._prime_calculations(identifiers, raw=True)

    def relations_many(self, calculations, *, resolver=resolve_calculation_relations, raw=False):
        self._require_source()
        identifiers = {item if isinstance(item, str) else item["id"] for item in calculations}
        sources = self._raw_relation_sources if raw else self._relation_sources
        def calculation(ident):
            return self._calculation(ident, raw=raw)

        for ident in sorted(identifiers):
            key = ident, resolver, raw
            if key not in self._relations:
                self._relations[key] = resolver(
                    calculation(ident), load_calculation=calculation,
                    load_parents=self._parents_for, source_cache=sources,
                )
        return {ident: self._relations[ident, resolver, raw] for ident in identifiers}

    def verify_selected_content(self, identifiers):
        self._require_source()
        return {ident: self._calculation(ident)["outcome"] for ident in identifiers}

    def verify_fact_versions(self, identifiers):
        self.facts(identifiers)

    def verify_sql_outcomes(self, identifiers):
        self.verify_selected_content(identifiers)
