"""The small stored-source reader used by released v1 content verification.

This is deliberately limited to the fact, calculation, voucher, and parent
records needed by v1 close proofs. It does not select current business state.
"""

import json

from .contracts import KernelError
from .query_relations_v1 import resolve_calculation_relations


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class V1Reads:
    @staticmethod
    def _stored_outcome(raw):
        from .stored_json_v1 import DuplicateStoredKey, loads_unique

        try:
            return loads_unique(raw)
        except DuplicateStoredKey as exc:
            raise KernelError("content_integrity_failed", "已保存的 v1 核算结果有重复字段") from exc

    def __init__(self, engine, connection):
        self.engine = engine
        self.connection = connection
        self._versions = {}
        self._facts = {}
        self._calculations = {}
        self._raw_calculations = {}
        self._parents = {}
        self._vouchers = {}
        self._lines = {}
        self._relations = {}
        self._relation_sources = {}
        self._raw_relation_sources = {}

    def fact_versions(self, identifiers):
        from .content_v1 import load_v1_fact_versions

        requested = set(identifiers)
        missing = requested - self._versions.keys()
        if missing:
            self._versions.update(
                load_v1_fact_versions(self.connection, self.engine.store.registry, missing)
            )
        return {ident: self._versions[ident] for ident in requested}

    def facts(self, identifiers):
        requested = set(identifiers)
        for ident, version in self.fact_versions(requested).items():
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
        return {ident: self._facts[ident] for ident in requested}

    def fact(self, ident):
        return self.facts((ident,))[ident]

    def prime_parents(self, identifiers):
        missing = set(identifiers) - self._parents.keys()
        if not missing:
            return
        parents = {ident: [] for ident in missing}
        for row in self.connection.execute(
            "SELECT d.calculation_id,d.upstream_id FROM json_each(?) ids "
            "JOIN dependency_calculation d ON d.calculation_id=ids.value "
            "ORDER BY d.calculation_id,d.upstream_id",
            (_json(sorted(missing)),),
        ):
            parents[row["calculation_id"]].append(row["upstream_id"])
        self._parents.update({ident: tuple(values) for ident, values in parents.items()})

    def parents(self, ident):
        if ident not in self._parents:
            self.prime_parents((ident,))
        return self._parents[ident]

    def _closure(self, identifiers):
        roots = set(identifiers)
        if not roots:
            return set()
        closure = {
            row[0]
            for row in self.connection.execute(
                "WITH RECURSIVE selected(id) AS (SELECT value FROM json_each(?) UNION "
                "SELECT d.upstream_id FROM dependency_calculation d JOIN selected s "
                "ON d.calculation_id=s.id) SELECT id FROM selected",
                (_json(sorted(roots)),),
            )
        }
        self.prime_parents(closure)
        return closure

    def _load_calculations(self, identifiers, *, raw, verified_calculations=None):
        from .content_v1 import _V1YearMonth, load_v1_fact_data_many

        requested = set(identifiers)
        cache = self._raw_calculations if raw else self._calculations
        missing = {ident for ident in requested if ident not in cache}
        if missing:
            records = {}
            for row in self.connection.execute(
                "SELECT c.id,c.subject_id,c.kind,c.period,c.fact_id,c.digest,"
                "c.program_version,c.outcome,f.revision fact_revision,"
                "p.id publication_id,p.mode publication_mode,p.posting_period,p.voucher_id "
                "FROM json_each(?) ids JOIN calculation c ON c.id=ids.value "
                "JOIN fact_revision f ON f.id=c.fact_id "
                "LEFT JOIN calculation_publication p ON p.calculation_id=c.id",
                (_json(sorted(missing)),),
            ):
                records[row["id"]] = dict(row)
            if set(records) != missing:
                raise KernelError("unknown_calculation", "历史核算版本不存在")
            fact_ids = {row["fact_id"] for row in records.values()}
            facts = (
                load_v1_fact_data_many(self.connection, self.engine.store.registry, fact_ids)
                if raw
                else {ident: value["data"] for ident, value in self.facts(fact_ids).items()}
            )
            for ident, row in records.items():
                if row["posting_period"] is None and row["kind"] not in {
                    "asset_activation",
                    "asset_consumption",
                }:
                    raise KernelError("unknown_calculation", "历史核算未正式发布或采用")
                verified = (verified_calculations or {}).get(ident)
                if verified is not None and (
                    verified["digest"] != row["digest"]
                    or verified["fact_id"] != row["fact_id"]
                    or verified["kind"] != row["kind"]
                ):
                    raise KernelError("content_integrity_failed", "已核验历史核算身份不一致")
                outcome = (
                    verified["decoded"]
                    if verified is not None
                    else self._stored_outcome(row["outcome"])
                )
                cache[ident] = {
                    "id": ident,
                    "subject_id": row["subject_id"],
                    "kind": row["kind"],
                    "period": str(_V1YearMonth.from_ordinal(row["period"])),
                    "fact_id": row["fact_id"],
                    "result_digest": row["digest"].hex(),
                    "program_version": row["program_version"],
                    "publication_id": row["publication_id"],
                    "publication_mode": row["publication_mode"],
                    "posting_period": (
                        str(_V1YearMonth.from_ordinal(row["posting_period"]))
                        if row["posting_period"] is not None
                        else None
                    ),
                    "voucher_id": row["voucher_id"],
                    "publication_role": (
                        "independent" if row["posting_period"] is not None else "asset_member"
                    ),
                    "fact_data": facts[row["fact_id"]],
                    "outcome": outcome,
                }
        return {ident: cache[ident] for ident in requested}

    def calculations(self, identifiers):
        return self._load_calculations(identifiers, raw=False)

    def calculation(self, ident):
        return self.calculations((ident,))[ident]

    def prime_calculations(self, identifiers, *, ancestors=True):
        closure = self._closure(identifiers) if ancestors else set(identifiers)
        self.calculations(closure)
        return self.calculations(identifiers)

    def raw_calculations(self, identifiers):
        return self._load_calculations(identifiers, raw=True)

    def prime_raw_calculations(self, identifiers, *, verified_calculations=None):
        closure = self._closure(identifiers)
        self._load_calculations(closure, raw=True, verified_calculations=verified_calculations)
        return self.raw_calculations(identifiers)

    def relations_many(self, calculations, *, resolver=resolve_calculation_relations, raw=False):
        identifiers = {item if isinstance(item, str) else item["id"] for item in calculations}
        missing = {ident for ident in identifiers if (ident, resolver, raw) not in self._relations}
        if missing:
            if raw:
                self.prime_raw_calculations(missing)
                calculations_by_id = self._raw_calculations
                sources = self._raw_relation_sources

                def load(ident):
                    return self.raw_calculations((ident,))[ident]
            else:
                self.prime_calculations(missing)
                calculations_by_id = self._calculations
                sources = self._relation_sources
                load = self.calculation
            for ident in sorted(missing):
                self._relations[ident, resolver, raw] = resolver(
                    calculations_by_id[ident],
                    load_calculation=load,
                    load_parents=self.parents,
                    source_cache=sources,
                )
        return {ident: self._relations[ident, resolver, raw] for ident in identifiers}

    def vouchers(self, identifiers):
        requested = set(identifiers)
        missing = requested - self._vouchers.keys()
        if missing:
            for row in self.connection.execute(
                "SELECT v.*,n.number FROM json_each(?) ids JOIN voucher_version v "
                "ON v.id=ids.value JOIN voucher n ON n.id=v.voucher_id",
                (_json(sorted(missing)),),
            ):
                self._vouchers[row["id"]] = dict(row)
            if requested - self._vouchers.keys():
                raise KernelError("selected_voucher_missing", "历史凭证版本不存在")
        return {ident: self._vouchers[ident] for ident in requested}

    def voucher_lines(self, identifiers):
        requested = set(identifiers)
        missing = requested - self._lines.keys()
        if missing:
            self._lines.update({ident: [] for ident in missing})
            for row in self.connection.execute(
                "SELECT l.* FROM json_each(?) ids JOIN voucher_line l "
                "ON l.version_id=ids.value ORDER BY l.version_id,l.line_no",
                (_json(sorted(missing)),),
            ):
                self._lines[row["version_id"]].append(
                    {key: row[key] for key in ("line_no", "account", "debit", "credit", "cashflow")}
                )
        return {ident: self._lines[ident] for ident in requested}
