"""Company-local real objects and their immutable management profiles.

Object IDs are generated here. Business IDs, payment instructions and correction
scope remain independent; a name change never rewrites an accounting fact.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

from .contracts import KernelError, NeedsInformation
from .types import ActualDate, YearMonth, canonical, digest

EntityKind = Literal["person", "organization", "fund_account", "asset", "project", "fund_product"]
ENTITY_KINDS = ("person", "organization", "fund_account", "asset", "project", "fund_product")
DisplayDate = Annotated[
    YearMonth | ActualDate,
    WithJsonSchema(
        {
            "type": "string",
            "anyOf": [
                {"pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"},
                {"pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
            ],
            "x-accounting-fact": {
                "role": "management",
                "meaning": "explicitly_provided_employment_date",
                "allowed_precision": ["month", "day"],
                "reusable_sources": ["owner_confirmation", "employment_document"],
            },
        }
    ),
]


class EntityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    display_number: str | None = Field(default=None, min_length=1, max_length=200)
    external_identifiers: dict[str, str] = Field(default_factory=dict)
    active: bool = True
    purpose: str | None = Field(default=None, max_length=50000)
    note: str | None = Field(default=None, max_length=50000)
    employment_start: DisplayDate | None = None
    employment_end: DisplayDate | None = None
    employment_status: Literal["active", "inactive", "unknown"] = "unknown"
    category_label: str | None = None
    rights_description: str | None = None
    useful_life_basis: str | None = None

    @model_validator(mode="after")
    def validate_explicit_details(self):
        if any(
            not key.strip() or not value.strip() or len(value) > 500
            for key, value in self.external_identifiers.items()
        ):
            raise ValueError("external identifiers require a name and an explicit value")
        if self.employment_start and self.employment_end:
            start, end = self.employment_start, self.employment_end
            if start[:7] > end[:7] or (len(start) == len(end) == 10 and start > end):
                raise ValueError("employment end precedes start")
        return self


ENTITY_DDL = """
CREATE TABLE entity(id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN
 ('person','organization','fund_account','asset','project','fund_product')),
 account_type TEXT CHECK(account_type IN('bank','cash','platform')),
 CHECK((kind='fund_account')=(account_type IS NOT NULL))) STRICT;
CREATE TABLE entity_profile_revision(id TEXT PRIMARY KEY,entity_id TEXT NOT NULL
 REFERENCES entity(id),revision INTEGER NOT NULL CHECK(revision>0),
 content TEXT NOT NULL,source TEXT NOT NULL,evidence_digest BLOB REFERENCES evidence(digest),
 digest BLOB NOT NULL CHECK(length(digest)=32),UNIQUE(entity_id,revision)) STRICT;
CREATE TABLE entity_resolution(id TEXT PRIMARY KEY,correction_id TEXT NOT NULL
 REFERENCES identity_correction(id),source_entity_id TEXT NOT NULL REFERENCES entity(id),
 target_entity_id TEXT NOT NULL REFERENCES entity(id),digest BLOB NOT NULL CHECK(length(digest)=32),
 CHECK(source_entity_id<>target_entity_id)) STRICT;
CREATE INDEX entity_kind ON entity(kind,id);
CREATE INDEX entity_resolution_source ON entity_resolution(source_entity_id);
CREATE INDEX entity_resolution_target ON entity_resolution(target_entity_id);
"""


def require_entity(connection, entity_id, *, kinds=(), account_type=None):
    row = connection.execute("SELECT * FROM entity WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        raise NeedsInformation(
            "entity_id", "对象尚未登记，请先查找并复用或登记对象", sources=(entity_id,)
        )
    if kinds and row["kind"] not in kinds:
        raise KernelError("entity_kind_mismatch", "对象类型与业务引用不一致", entity_id=entity_id)
    if account_type and row["account_type"] != account_type:
        raise KernelError("entity_kind_mismatch", "资金账户类型与业务不一致", entity_id=entity_id)
    return dict(row)


def _profile_record(row):
    content = json.loads(row["content"])
    evidence = row["evidence_digest"].hex() if row["evidence_digest"] else None
    if (
        digest([row["entity_id"], row["revision"], content, row["source"], evidence])
        != row["digest"]
    ):
        raise KernelError("entity_profile_corrupt", "对象档案内容校验失败", profile_id=row["id"])
    return dict(
        content,
        id=row["id"],
        entity_id=row["entity_id"],
        revision=row["revision"],
        source=row["source"],
        digest=row["digest"].hex(),
        evidence_digest=row["evidence_digest"].hex() if row["evidence_digest"] else None,
    )


def display_profile(profile, kind):
    """Read-only page vocabulary; the entity profile remains the only source."""
    result = {
        key: profile.get(key)
        for key in (
            "id",
            "entity_id",
            "revision",
            "display_name",
            "display_number",
            "purpose",
            "note",
            "employment_start",
            "employment_end",
            "employment_status",
            "active",
            "category_label",
            "rights_description",
            "useful_life_basis",
            "source",
            "evidence_digest",
            "digest",
        )
    }
    return dict(result, kind=kind, counterparty_id=None, beneficiary_id=None, handler_id=None)


def profiles(connection, *, profile_ids=None):
    sql = (
        "SELECT p.*,e.kind,e.account_type FROM entity_profile_revision p "
        "JOIN entity e ON e.id=p.entity_id "
    )
    if profile_ids is None:
        sql += (
            "WHERE p.revision=(SELECT max(q.revision) FROM entity_profile_revision q "
            "WHERE q.entity_id=p.entity_id)"
        )
        params = ()
    else:
        sql += "WHERE p.id IN(SELECT value FROM json_each(?))"
        params = (canonical(list(profile_ids)),)
    return {
        row["entity_id"]: dict(
            _profile_record(row), entity_kind=row["kind"], account_type=row["account_type"]
        )
        for row in connection.execute(sql, params)
    }


def employee_entities(connection, period):
    """Explicit employment records or profiles, with scoped corrections applied."""
    return [
        row[0]
        for row in connection.execute(
            "SELECT e.id FROM entity e JOIN entity_profile_revision p ON p.entity_id=e.id "
            "WHERE e.kind='person' AND p.revision=(SELECT max(q.revision) "
            "FROM entity_profile_revision q WHERE q.entity_id=e.id) AND ("
            "EXISTS(SELECT 1 FROM entity_reference_current r "
            "JOIN fact_current c ON c.fact_id=r.fact_id "
            "WHERE r.entity_id=e.id AND r.role='employee' AND r.period<=?) OR ("
            "(json_extract(p.content,'$.employment_start') IS NOT NULL OR "
            "json_extract(p.content,'$.employment_status')<>'unknown') AND NOT EXISTS "
            "(SELECT 1 FROM entity_resolution x WHERE x.source_entity_id=e.id))) ORDER BY e.id",
            (YearMonth(period).ordinal,),
        )
    ]


def validate_resolution(connection, changes, entity_resolution):
    if entity_resolution is None:
        return
    if set(entity_resolution) != {"source_entity_id", "target_entity_id"}:
        raise KernelError("invalid_entity_resolution", "身份纠错必须明确原对象和目标对象")
    source, target = (entity_resolution[key] for key in ("source_entity_id", "target_entity_id"))
    original, destination = require_entity(connection, source), require_entity(connection, target)
    if source == target or (original["kind"], original["account_type"]) != (
        destination["kind"],
        destination["account_type"],
    ):
        raise KernelError("entity_kind_mismatch", "纠错双方必须是同类的不同对象")
    if not any(
        item.get("before") == source and item.get("after") == target
        for change in changes
        for item in change.get("entity_changes", ())
    ):
        raise KernelError("invalid_entity_resolution", "名单归并必须来自本次实际纠正的对象引用")


def apply_resolution(connection, correction_id, changes, entity_resolution):
    validate_resolution(connection, changes, entity_resolution)
    if entity_resolution is None:
        return
    source, target = (entity_resolution[key] for key in ("source_entity_id", "target_entity_id"))
    # This records the checked relationship, not an alias. Exact fact bindings
    # decide current ownership; another correction can restore any chosen scope.
    payload = dict(entity_resolution, correction_id=correction_id)
    connection.execute(
        "INSERT INTO entity_resolution VALUES(?,?,?,?,?)",
        (uuid.uuid4().hex, correction_id, source, target, digest(payload)),
    )


def verify_entities(connection):
    for row in connection.execute("SELECT * FROM entity_profile_revision"):
        _profile_record(row)
    for row in connection.execute("SELECT * FROM entity_resolution"):
        payload = {
            key: row[key] for key in ("correction_id", "source_entity_id", "target_entity_id")
        }
        if digest(payload) != row["digest"]:
            raise KernelError("entity_resolution_corrupt", "身份纠错关系校验失败")
    missing = connection.execute(
        "SELECT e.id FROM entity e WHERE NOT EXISTS "
        "(SELECT 1 FROM entity_profile_revision p WHERE p.entity_id=e.id)"
    ).fetchone()
    if missing:
        raise KernelError("entity_profile_corrupt", "对象缺少初始档案", entity_id=missing[0])


class Entities:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    @staticmethod
    def _profile(data, source, evidence_digest):
        profile = EntityProfile.model_validate_json(canonical(data)).model_dump(mode="json")
        if not isinstance(source, str) or not source.strip() or len(source) > 2000:
            raise ValueError("source must describe the explicit source of the profile")
        evidence = None if evidence_digest is None else bytes.fromhex(evidence_digest)
        if evidence is not None and len(evidence) != 32:
            raise ValueError("invalid evidence digest")
        return profile, evidence

    @staticmethod
    def _insert_profile(connection, entity_id, revision, profile, source, evidence):
        if (
            evidence is not None
            and not connection.execute(
                "SELECT 1 FROM evidence WHERE digest=?", (evidence,)
            ).fetchone()
        ):
            raise NeedsInformation("evidence_digest", "档案依据尚未登记")
        identifier = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO entity_profile_revision VALUES(?,?,?,?,?,?,?)",
            (
                identifier,
                entity_id,
                revision,
                canonical(profile),
                source,
                evidence,
                digest(
                    [entity_id, revision, profile, source, evidence.hex() if evidence else None]
                ),
            ),
        )
        return {
            "status": "registered",
            "entity_id": entity_id,
            "profile_id": identifier,
            "revision": revision,
        }

    def register_entity(
        self,
        kind: EntityKind,
        data: dict,
        *,
        source: str,
        request_id: str,
        evidence_digest: str | None = None,
        account_type: Literal["bank", "cash", "platform"] | None = None,
    ):
        if kind not in ENTITY_KINDS or (kind == "fund_account") != (account_type is not None):
            raise ValueError(
                "fund accounts require an explicit account_type; other entities do not"
            )
        if account_type is not None and account_type not in ("bank", "cash", "platform"):
            raise ValueError("invalid account type")
        profile, evidence = self._profile(data, source, evidence_digest)

        def operation(connection):
            identifier = "entity_" + uuid.uuid4().hex
            connection.execute("INSERT INTO entity VALUES(?,?,?)", (identifier, kind, account_type))
            return self._insert_profile(connection, identifier, 1, profile, source, evidence)

        return self.engine._write(
            request_id,
            digest(["register_entity", kind, account_type, profile, source, evidence_digest]),
            None,
            ("management",),
            "register_entity",
            operation,
        )

    def update_entity_profile(
        self,
        entity_id: str,
        data: dict,
        *,
        source: str,
        expected_revision: int,
        request_id: str,
        evidence_digest: str | None = None,
    ):
        profile, evidence = self._profile(data, source, evidence_digest)

        def operation(connection):
            require_entity(connection, entity_id)
            current = connection.execute(
                "SELECT max(revision) FROM entity_profile_revision WHERE entity_id=?", (entity_id,)
            ).fetchone()[0]
            if type(expected_revision) is not int or current != expected_revision:
                raise KernelError("entity_profile_version_conflict", "对象档案已变化，请重新读取")
            return self._insert_profile(
                connection, entity_id, current + 1, profile, source, evidence
            )

        return self.engine._write(
            request_id,
            digest(
                [
                    "update_entity_profile",
                    entity_id,
                    profile,
                    source,
                    evidence_digest,
                    expected_revision,
                ]
            ),
            None,
            ("management",),
            "update_entity_profile",
            operation,
        )

    def find_entities(
        self,
        *,
        query: str | None = None,
        kind: EntityKind | None = None,
        used_from: str | None = None,
        used_to: str | None = None,
        limit: int = 100,
    ):
        if kind is not None and kind not in ENTITY_KINDS:
            raise ValueError("invalid entity kind")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        lower, upper = (
            YearMonth(value).ordinal if value is not None else None
            for value in (used_from, used_to)
        )
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("inverted period range")
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            all_profiles = profiles(connection)
            usage = {
                row["entity_id"]: row["last_period"]
                for row in connection.execute(
                    "SELECT r.entity_id,max(r.period) last_period FROM entity_reference_current r "
                    "JOIN fact_current c ON c.fact_id=r.fact_id GROUP BY r.entity_id"
                )
            }
            relationships = list(connection.execute("SELECT * FROM entity_resolution"))
            items = []
            needle = query.casefold().strip() if query else None
            for entity_id, profile in all_profiles.items():
                if kind is not None and kind != profile["entity_kind"]:
                    continue
                recent = usage.get(entity_id)
                if (lower is not None and (recent is None or recent < lower)) or (
                    upper is not None and (recent is None or recent > upper)
                ):
                    continue
                values = [
                    entity_id,
                    profile["display_name"],
                    profile["display_number"],
                    *profile["external_identifiers"].values(),
                ]
                exact = needle is not None and any(
                    needle == value.casefold() for value in values if value
                )
                if needle and not any(needle in value.casefold() for value in values if value):
                    continue
                linked = [
                    dict(
                        source_entity_id=row["source_entity_id"],
                        target_entity_id=row["target_entity_id"],
                        correction_id=row["correction_id"],
                    )
                    for row in relationships
                    if entity_id in (row["source_entity_id"], row["target_entity_id"])
                ]
                items.append(
                    {
                        "entity_id": entity_id,
                        "kind": profile["entity_kind"],
                        "account_type": profile["account_type"],
                        "profile": profile,
                        "match": "exact" if exact else "similar" if needle else "unfiltered",
                        "recent_period": str(YearMonth.from_ordinal(recent))
                        if recent is not None
                        else None,
                        "corrections": linked,
                    }
                )
            items.sort(key=lambda row: (row["match"] != "exact", row["entity_id"]))
            return {"schema_version": 1, "items": items[:limit], "has_more": len(items) > limit}
