"""Entity-scoped dashboard metadata on the caller's existing read transaction.

These mappings delay records, not display policy. Snapshot.profile, party_details
and source retain responsibility for frozen values and current supplements.
"""

from __future__ import annotations

from collections.abc import Mapping, Set

from .display import _KINDS, Display
from .read_indexes import CLOSE_MANAGEMENT, CLOSE_PAYEES, CLOSE_PROFILES, verify_close_references
from .types import canonical

_REFERENCE_FIELDS = (
    "close_period",
    "path",
    "position",
    "reference_type",
    "reference_id",
    "related_id",
)
_RECORDS = {
    "profile": ("display_profile_revision", "entity_id", CLOSE_PROFILES, "display_profile"),
    "payee": ("payee_revision", "party_id", CLOSE_PAYEES, "payee"),
    "management": ("management_revision", "subject_id", CLOSE_MANAGEMENT, "management"),
}


class Records(Mapping):
    """Keys are cheap identities; get/prime load only requested latest or frozen rows."""

    def __init__(self, snapshot, record_type, *, kind=None, frozen=False):
        self.snapshot, self.connection = snapshot, snapshot.connection
        self.record_type, self.kind, self.frozen = record_type, kind, frozen
        self.table, self.identity, self.path, self.reference_type = _RECORDS[record_type]
        self.cache, self.missing = {}, set()
        self._identities = None

    def _query(self, *, records=False, identifiers=None):
        columns = "p.*" if records else f"p.{self.identity}"
        query, parameters = f"SELECT {columns}", []
        if self.frozen:
            query += "," + ",".join("r." + field for field in _REFERENCE_FIELDS)
        query += f" FROM {self.table} p"
        if self.frozen:
            query += (
                " JOIN close_reference r ON r.reference_id=p.id "
                "AND r.close_period=? AND r.path=? AND r.reference_type=?"
            )
            parameters.extend((self.snapshot.month, self.path, self.reference_type))
        query += " WHERE 1=1"
        if self.kind is not None:
            query += " AND p.kind=?"
            parameters.append(self.kind)
        if identifiers is not None:
            query += f" AND p.{self.identity} IN (SELECT value FROM json_each(?))"
            parameters.append(canonical(sorted(identifiers)))
        if not self.frozen:
            query += (
                f" AND p.revision=(SELECT max(q.revision) FROM {self.table} q "
                f"WHERE q.{self.identity}=p.{self.identity}"
                + (" AND q.kind=p.kind" if self.kind is not None else "")
                + ")"
            )
        query += f" ORDER BY p.{self.identity},p.revision"
        return query, parameters

    def prime(self, identifiers):
        missing = set(identifiers) - self.cache.keys() - self.missing
        if not missing:
            return
        query, parameters = self._query(records=True, identifiers=missing)
        rows = self.connection.execute(query, parameters).fetchall()
        if self.frozen:
            verify_close_references(self.connection, rows)
        for row in rows:
            record = {
                key: row[key]
                for key in row.keys()
                if not self.frozen or key not in _REFERENCE_FIELDS
            }
            self.cache[row[self.identity]] = (
                Display._record(record) if self.record_type == "profile" else record
            )
        self.missing.update(missing - self.cache.keys())

    def __getitem__(self, key):
        self.prime((key,))
        return self.cache[key]

    def __iter__(self):
        if self._identities is None:
            query, parameters = self._query()
            rows = self.connection.execute(query, parameters).fetchall()
            if self.frozen:
                verify_close_references(self.connection, rows)
            self._identities = tuple(dict.fromkeys(row[self.identity] for row in rows))
        return iter(self._identities)

    def __len__(self):
        return sum(1 for _ in self)

    def values(self):
        keys = list(self)
        self.prime(keys)
        return [self.cache[key] for key in keys]

    def items(self):
        keys = list(self)
        self.prime(keys)
        return [(key, self.cache[key]) for key in keys]


class ProfileCollection(Mapping):
    def __init__(self, snapshot, *, frozen=False):
        self.maps = {
            kind: Records(snapshot, "profile", kind=kind, frozen=frozen) for kind in _KINDS
        }

    def __getitem__(self, kind):
        return self.maps[kind]

    def __iter__(self):
        return iter(self.maps)

    def __len__(self):
        return len(self.maps)

    def prime(self, kind, identifiers):
        self.maps[kind].prime(identifiers)


class Projection(Mapping):
    def __init__(self, records, project):
        self.records, self.project = records, project

    def __getitem__(self, key):
        return self.project(self.records[key])

    def __iter__(self):
        return iter(self.records)

    def __len__(self):
        return len(self.records)


class FrozenManagementIds(Set):
    """Exact reference membership without constructing all management records."""

    def __init__(self, snapshot):
        self.snapshot, self.cache = snapshot, {}

    def __contains__(self, ident):
        if not self.snapshot.close:
            return False
        key = str(ident)
        if key not in self.cache:
            rows = self.snapshot.connection.execute(
                "SELECT r.* FROM close_reference r WHERE r.close_period=? AND r.path=? "
                "AND r.reference_type='management' AND r.reference_id=?",
                (self.snapshot.month, CLOSE_MANAGEMENT, key),
            ).fetchall()
            verify_close_references(self.snapshot.connection, rows)
            self.cache[key] = bool(rows)
        return self.cache[key]

    def __iter__(self):
        if not self.snapshot.close:
            return iter(())
        rows = self.snapshot.connection.execute(
            "SELECT r.* FROM close_reference r WHERE r.close_period=? AND r.path=? "
            "AND r.reference_type='management'",
            (self.snapshot.month, CLOSE_MANAGEMENT),
        ).fetchall()
        verify_close_references(self.snapshot.connection, rows)
        return iter(dict.fromkeys(int(row["reference_id"]) for row in rows))

    def __len__(self):
        return sum(1 for _ in self)


class Management(Mapping):
    """The original snapshot field projection, evaluated only for named subjects."""

    def __init__(self, snapshot, selected, current):
        self.snapshot, self.selected, self.current = snapshot, selected, current
        self.cache = {}

    def prime(self, identifiers):
        missing = set(identifiers) - self.cache.keys()
        self.selected.prime(missing)
        self.current.prime(missing)
        for ident in missing:
            frozen = self.selected.get(ident, {})
            current = self.current.get(ident)
            if current is None:
                if frozen:
                    self.cache[ident] = frozen
                continue
            selected = dict(frozen or current)
            selected["field_sources"] = {}
            for field in ("note", "payment_period", "payment_category"):
                record = frozen if frozen.get(field) not in (None, "") else current
                selected[field] = record.get(field)
                if selected[field] not in (None, ""):
                    selected["field_sources"][field] = self.snapshot.management_source(
                        record, field
                    )
            self.cache[ident] = selected

    def __getitem__(self, key):
        self.prime((key,))
        return self.cache[key]

    def __iter__(self):
        return iter(sorted(set(self.selected) | set(self.current)))

    def __len__(self):
        return sum(1 for _ in self)

    def values(self):
        keys = list(self)
        self.prime(keys)
        return [self.cache[key] for key in keys]

    def items(self):
        keys = list(self)
        self.prime(keys)
        return [(key, self.cache[key]) for key in keys]


class TaxIdentityCandidates(Mapping):
    def __init__(self, snapshot):
        self.snapshot, self.cache, self.missing = snapshot, {}, set()
        self.available = "tax_import_identity_v2" in snapshot.store.registry.models

    def prime(self, employee_ids):
        missing = set(employee_ids) - self.cache.keys() - self.missing
        if not missing:
            return
        if not self.available:
            self.missing.update(missing)
            return
        # The existing exact scope index locates each employee before normalized
        # identity columns are checked; no all-company tax fact payload is read.
        rows = self.snapshot.connection.execute(
            "SELECT t.employee_id,f.id FROM json_each(?) ids JOIN fact_scope s "
            "ON s.kind='tax_import_identity_v2' AND s.scope_key='tax-identity:'||ids.value "
            "JOIN fact_current a ON a.fact_id=s.fact_id JOIN fact_revision f ON f.id=a.fact_id "
            "JOIN fact_tax_import_identity_v2 t ON t.revision_id=f.id AND t.employee_id=ids.value "
            "ORDER BY f.period,f.revision,f.id",
            (canonical(sorted(missing)),),
        ).fetchall()
        facts = self.snapshot.reads.facts(row["id"] for row in rows)
        for row in rows:
            self.cache.setdefault(row["employee_id"], []).append(facts[row["id"]])
        self.missing.update(missing - self.cache.keys())

    def __getitem__(self, key):
        self.prime((key,))
        return self.cache[key]

    def __iter__(self):
        if not self.available:
            return iter(())
        return iter(
            row[0]
            for row in self.snapshot.connection.execute(
                "SELECT DISTINCT t.employee_id FROM fact_current a "
                "JOIN fact_tax_import_identity_v2 t "
                "ON t.revision_id=a.fact_id ORDER BY t.employee_id"
            )
        )

    def __len__(self):
        return sum(1 for _ in self)


class DashboardMetadata:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.current_profiles = ProfileCollection(snapshot)
        self.profiles = (
            ProfileCollection(snapshot, frozen=True) if snapshot.close else self.current_profiles
        )
        self.current_payees = Records(snapshot, "payee")
        self.payee_records = (
            Records(snapshot, "payee", frozen=True) if snapshot.close else self.current_payees
        )
        self.payees = Projection(self.payee_records, lambda row: row["name"])
        current_management = Records(snapshot, "management")
        selected_management = (
            Records(snapshot, "management", frozen=True) if snapshot.close else current_management
        )
        self.frozen_management_ids = FrozenManagementIds(snapshot)
        self.management = Management(snapshot, selected_management, current_management)
        self.tax_candidates = TaxIdentityCandidates(snapshot)
        self.tax_current = Projection(self.tax_candidates, lambda rows: rows[-1])

    def prime_profiles(self, kind, identifiers):
        identifiers = set(identifiers)
        self.profiles.prime(kind, identifiers)
        self.current_profiles.prime(kind, identifiers)


def initialize_metadata(snapshot):
    """Install compatible mappings without issuing a metadata query at construction."""
    metadata = DashboardMetadata(snapshot)
    for name in (
        "profiles",
        "current_profiles",
        "payee_records",
        "payees",
        "current_payees",
        "management",
        "frozen_management_ids",
    ):
        setattr(snapshot, name, getattr(metadata, name))
    snapshot._tax_identity_records = metadata.tax_current, metadata.tax_candidates
    return metadata
