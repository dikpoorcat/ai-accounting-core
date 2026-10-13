"""Company-local real objects and their immutable management profiles.

Object IDs are generated here. Business IDs, payment instructions and correction
scope remain independent; a name change never rewrites an accounting fact.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .content_history_context import source_canonical as canonical
from .content_history_context import source_digest as digest
from .content_history_context import source_json_loads
from .contracts import KernelError, NeedsInformation
from .employee_employment import (
    WorkforceChange,
    WorkforceDate,
    WorkforceEvent,
    active_events,
    decode_cursor,
    encode_cursor,
    monthly_projection,
    recorded_event_times,
    require_occurred,
    verify_history,
)
from .employee_employment import (
    issue as employment_issue,
)
from .employee_employment import (
    projection as workforce_projection,
)
from .employee_employment import (
    records as workforce_records,
)
from .fund_accounts import (
    IDENTITY_FIELDS,
    account_identity,
    correct_fund_identity,
    existing_fund_account,
    has_fund_definitions,
    identity_issue,
    install_fund_account_definition,
    require_fund_account,
    require_fund_source,
    verify_fund_accounts,
)
from .text_sort import pinyin_key
from .types import EvidenceDigest, YearMonth, evidence_digest_bytes

EntityKind = Literal["person", "organization", "fund_account", "asset", "project", "fund_product"]
ENTITY_KINDS = ("person", "organization", "fund_account", "asset", "project", "fund_product")


class EntityProfile(BaseModel):
    """Shared management vocabulary; subtype models own all specific fields."""

    model_config = ConfigDict(extra="forbid", strict=True)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=50000)


class NumberedEntityProfile(EntityProfile):
    display_number: str | None = Field(default=None, min_length=1, max_length=200)
    external_identifiers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_external_identifiers(self):
        if any(
            not key.strip() or not value.strip() or len(value) > 500
            for key, value in self.external_identifiers.items()
        ):
            raise ValueError("external identifiers require a name and an explicit value")
        return self


class WorkerRegistration(NumberedEntityProfile):
    action: Literal["hire", "start_labor"]
    effective_date: WorkforceDate
    initial_unpaid_leave: bool = False
    after_event_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_initial_state(self):
        if self.initial_unpaid_leave and self.action != "hire":
            raise ValueError("initial_unpaid_leave requires hire")
        return self


class PersonProfile(NumberedEntityProfile):
    workforce_event: WorkforceEvent | None = None


class OrganizationProfile(NumberedEntityProfile):
    pass


class FundAccountProfile(EntityProfile):
    active: bool = True


class AssetProfile(NumberedEntityProfile):
    category_label: str | None = Field(default=None, min_length=1, max_length=200)


def entity_profile_models():
    """Stored metadata and the single event asserted by a workforce operation."""
    return {
        "person": PersonProfile,
        "organization": OrganizationProfile,
        "fund_account": FundAccountProfile,
        "asset": AssetProfile,
        "project": NumberedEntityProfile,
        "fund_product": NumberedEntityProfile,
    }


def entity_input_models():
    """Metadata write vocabulary; personnel lifecycle belongs to typed commands."""
    return dict(entity_profile_models(), person=NumberedEntityProfile)


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
    if row["kind"] == "fund_account" and has_fund_definitions(connection):
        require_fund_account(connection, entity_id, row["account_type"])
    return dict(row)


def _profile_record(row):
    try:
        content = source_json_loads(row["content"])
    except ValueError as exc:
        raise KernelError(
            "content_integrity_failed", "对象档案 JSON 有重复字段或格式错误", profile_id=row["id"]
        ) from exc
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


def display_profile(profile, kind, *, fund_account=None):
    """Project management metadata and separately sourced formal fund identity."""
    result = {
        key: profile.get(key)
        for key in (
            "id",
            "entity_id",
            "revision",
            "display_name",
            "display_number",
            "note",
            "active",
            "category_label",
            "source",
            "evidence_digest",
            "digest",
        )
    }
    if kind == "fund_account":
        result.pop("display_number")
        result["account_number"] = None
        if fund_account is not None:
            if profile["entity_id"] != fund_account["entity_id"]:
                raise KernelError("entity_kind_mismatch", "资金账户身份与档案不一致")
            field = {"bank": "bank_account_number", "platform": "account_identifier"}.get(
                fund_account["account_type"]
            )
            if field is not None:
                result["account_number"] = fund_account["identity"][field]
                result["account_number_source"] = {
                    "source_type": "fund_account_identity",
                    "id": fund_account["digest"],
                    "entity_id": fund_account["entity_id"],
                    "revision": None,
                    "field": "identity." + field,
                    "source": fund_account["source"],
                    "evidence_digest": fund_account["evidence_digest"],
                }
    return dict(result, kind=kind)


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


def profiles_for_entities(connection, entity_ids):
    """Authenticate latest profiles for an exact, caller-selected object set."""
    if not entity_ids:
        return {}
    rows = list(
        connection.execute(
            "SELECT p.*,e.kind,e.account_type FROM json_each(?) ids "
            "JOIN entity e ON e.id=ids.value JOIN entity_profile_revision p ON p.entity_id=e.id "
            "WHERE p.revision=(SELECT max(q.revision) FROM entity_profile_revision q "
            "WHERE q.entity_id=e.id) ORDER BY e.id",
            (canonical(sorted(set(entity_ids))),),
        )
    )
    result = {
        row["entity_id"]: dict(
            _profile_record(row), entity_kind=row["kind"], account_type=row["account_type"]
        )
        for row in rows
    }
    if len(result) != len(set(entity_ids)):
        raise KernelError("content_integrity_failed", "已引用对象缺少档案来源")
    return result


def workforce_timelines(connection, entity_ids=None):
    """Authenticate all revisions once, then derive person-scoped timelines."""
    requested = None if entity_ids is None else sorted(set(entity_ids))
    if requested == []:
        return {}
    histories = {}
    sql = (
        "SELECT p.* FROM entity e JOIN entity_profile_revision p ON p.entity_id=e.id "
        "WHERE e.kind='person'"
    )
    params = ()
    if requested is not None:
        sql += " AND e.id IN(SELECT value FROM json_each(?))"
        params = (canonical(requested),)
    sql += " ORDER BY e.id,p.revision"
    for row in connection.execute(sql, params):
        histories.setdefault(row["entity_id"], []).append(_profile_record(row))
    from .entity_profile_history import retained_profile_ids

    retained = retained_profile_ids(connection, histories.values())
    result = {ident: verify_history(history, retained_ids=retained)
              for ident, history in histories.items()}
    recorded_event_times(
        connection,
        [event for timeline in result.values() for event in timeline["event_references"]],
    )
    return result


def employee_entities(connection, period, *, registry, entity_ids=None):
    """Past employee relationships plus independently authenticated payroll facts."""
    cutoff = YearMonth(period).ordinal
    requested = None if entity_ids is None else tuple(sorted(set(entity_ids)))
    if requested == ():
        return []
    timelines = workforce_timelines(connection, requested)
    candidates = list(
        connection.execute(
            "SELECT p.*,EXISTS(SELECT 1 FROM entity_reference_current r "
            "INDEXED BY entity_current_entity_role "
            "CROSS JOIN fact_current c ON c.fact_id=r.fact_id WHERE r.entity_id=e.id "
            "AND r.role='employee' AND r.period<=?) AS employee_reference "
            "FROM entity e JOIN entity_profile_revision p ON p.entity_id=e.id "
            "WHERE e.kind='person' AND p.revision=(SELECT max(q.revision) "
            "FROM entity_profile_revision q WHERE q.entity_id=e.id)"
            + (" AND e.id IN(SELECT value FROM json_each(?))" if requested is not None else "")
            + " ORDER BY e.id",
            [cutoff, *([canonical(requested)] if requested is not None else [])],
        )
    )
    selected = []
    for profile in candidates:
        _profile_record(profile)
        timeline = timelines[profile["entity_id"]]
        # Correcting one financial identity binding cannot end this person's
        # separately confirmed workforce relationship; resolutions are not aliases.
        if profile["employee_reference"] or any(
            segment["state"] in {"regular", "unpaid_leave"}
            and segment["start"][:7] <= period
            for segment in timeline["segments"]
        ):
            selected.append(profile)
    if selected:
        witnesses = connection.execute(
            "SELECT ids.value entity_id,(SELECT r.fact_id FROM entity_reference_current r "
            "INDEXED BY entity_current_entity_role "
            "CROSS JOIN fact_current c ON c.fact_id=r.fact_id WHERE r.entity_id=ids.value "
            "AND r.role='employee' AND r.period<=? "
            "ORDER BY r.period DESC,r.fact_id DESC LIMIT 1) fact_id "
            "FROM json_each(?) ids",
            (cutoff, canonical([row["entity_id"] for row in selected])),
        ).fetchall()
        hits = [{"fact_id": row["fact_id"]} for row in witnesses if row["fact_id"] is not None]
        if hits:
            from .entity_references import verify_hits

            verify_hits(connection, hits, registry=registry)
    return [row["entity_id"] for row in selected]


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
    verify_fund_accounts(connection)
    for row in connection.execute("SELECT * FROM entity_profile_revision"):
        _profile_record(row)
    from .entity_profile_history import retained_profile_ids

    histories = [workforce_records(connection, row["id"])
                 for row in connection.execute("SELECT id FROM entity WHERE kind='person'")]
    retained = retained_profile_ids(connection, histories)
    events = []
    for history in histories:
        verify_history(history, retained_ids=retained)
        events.extend(item for item in history if item.get("workforce_event") is not None)
    recorded_event_times(connection, events)
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
    def _profile(kind, data, source, evidence_digest):
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="json")
        selected = entity_profile_models()[kind].model_validate_json(canonical(data))
        profile = selected.model_dump(mode="json")
        if not isinstance(source, str) or not source.strip() or len(source) > 2000:
            raise NeedsInformation([employment_issue("source", "须明确提供本次管理资料的来源")])
        evidence = None if evidence_digest is None else evidence_digest_bytes(evidence_digest)
        return profile, evidence

    @staticmethod
    def _explicit(model, data):
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="json", exclude_unset=True)
        try:
            return model.model_validate_json(canonical(data)).model_dump(
                mode="json", exclude_unset=True
            )
        except ValidationError as exc:
            if model not in {WorkerRegistration, WorkforceChange}:
                raise
            missing = [
                employment_issue(str(error["loc"][0]), "缺少必需用工管理事实")
                for error in exc.errors(include_input=False)
                if error["type"] == "missing" and len(error["loc"]) == 1
            ]
            if missing:
                raise NeedsInformation(missing) from exc
            raise

    @staticmethod
    def _current(connection, entity_id, expected_revision):
        row = connection.execute(
            "SELECT * FROM entity_profile_revision WHERE entity_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
        if (
            type(expected_revision) is not int
            or row is None
            or row["revision"] != expected_revision
        ):
            raise KernelError("entity_profile_version_conflict", "对象档案已变化，请重新读取")
        return _profile_record(row)

    @staticmethod
    def _content(profile):
        return {
            key: value
            for key, value in profile.items()
            if key
            not in {
                "id",
                "entity_id",
                "revision",
                "source",
                "digest",
                "evidence_digest",
                "entity_kind",
                "account_type",
            }
        }

    @staticmethod
    def _insert_profile(
        connection, entity_id, revision, profile, source, evidence, *, profile_id=None
    ):
        if (
            evidence is not None
            and not connection.execute(
                "SELECT 1 FROM evidence WHERE digest=?", (evidence,)
            ).fetchone()
        ):
            raise NeedsInformation([employment_issue("evidence_digest", "档案依据尚未登记")])
        identifier = profile_id or uuid.uuid4().hex
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
        evidence_digest: EvidenceDigest | None = None,
        account_type: Literal["bank", "cash", "platform"] | None = None,
    ):
        if kind not in ENTITY_KINDS or (kind == "fund_account") != (account_type is not None):
            raise ValueError(
                "fund accounts require an explicit account_type; other entities do not"
            )
        if account_type is not None and account_type not in ("bank", "cash", "platform"):
            raise ValueError("invalid account type")
        identity = None
        if kind == "fund_account":
            selected, identity = account_identity(account_type, data)
            require_fund_source(source, evidence_digest)
            data = selected.model_dump(mode="json")
            for key in IDENTITY_FIELDS[account_type]:
                data.pop(key)
        else:
            data = self._explicit(entity_input_models()[kind], data)
        profile, evidence = self._profile(kind, data, source, evidence_digest)

        def operation(connection):
            if identity is not None:
                if not connection.execute(
                    "SELECT 1 FROM evidence WHERE digest=?", (evidence,)
                ).fetchone():
                    raise NeedsInformation(
                        [identity_issue("evidence_digest", "资金账户身份来源尚未登记")]
                    )
                existing = existing_fund_account(connection, account_type, identity)
                if existing is not None:
                    current = connection.execute(
                        "SELECT id,revision FROM entity_profile_revision WHERE entity_id=? "
                        "ORDER BY revision DESC LIMIT 1",
                        (existing["entity_id"],),
                    ).fetchone()
                    return {
                        "status": "existing",
                        "entity_id": existing["entity_id"],
                        "profile_id": current["id"],
                        "revision": current["revision"],
                        "fund_account": existing,
                    }
            identifier = "entity_" + uuid.uuid4().hex
            connection.execute("INSERT INTO entity VALUES(?,?,?)", (identifier, kind, account_type))
            saved = self._insert_profile(connection, identifier, 1, profile, source, evidence)
            if identity is not None:
                saved["fund_account"] = install_fund_account_definition(
                    connection,
                    identifier,
                    account_type,
                    dict(profile, **identity),
                    source=source,
                    evidence_digest=evidence_digest,
                )
                self.engine.fault("fund_account_registered", connection)
            return saved

        return self.engine._write(
            request_id,
            digest(
                ["register_entity", kind, account_type, profile, source, evidence_digest, identity]
            )
            if identity is not None
            else digest(["register_entity", kind, account_type, profile, source, evidence_digest]),
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
        evidence_digest: EvidenceDigest | None = None,
    ):
        with self.store.connection(read_only=True) as connection:
            entity = require_entity(connection, entity_id)
        explicit = self._explicit(entity_input_models()[entity["kind"]], data)
        _, evidence = self._profile(entity["kind"], explicit, source, evidence_digest)

        def operation(connection):
            require_entity(connection, entity_id)
            current = self._current(connection, entity_id, expected_revision)
            if entity["kind"] == "person":
                from .entity_profile_history import current_person_metadata, retained_profile_ids

                history = workforce_records(connection, entity_id)
                verify_history(history, retained_ids=retained_profile_ids(connection, [history]))
                merged = current_person_metadata(connection, current, history=history)
            else:
                merged = self._content(current)
            merged.update(explicit)
            # A metadata revision makes no assertion about the relationship.
            merged.pop("workforce_event", None)
            profile, _ = self._profile(entity["kind"], merged, source, evidence_digest)
            return self._insert_profile(
                connection, entity_id, current["revision"] + 1, profile, source, evidence
            )

        return self.engine._write(
            request_id,
            digest(
                [
                    "update_entity_profile",
                    entity_id,
                    explicit,
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

    def register_worker(
        self,
        data: WorkerRegistration,
        *,
        source: str,
        request_id: str,
        entity_id: str | None = None,
        expected_revision: int | None = None,
        evidence_digest: EvidenceDigest | None = None,
    ):
        explicit = self._explicit(WorkerRegistration, data)
        if entity_id is None and expected_revision is not None:
            raise ValueError("expected_revision requires an existing person")
        if entity_id is not None and expected_revision is None:
            raise NeedsInformation(
                [employment_issue("expected_revision", "复用人员身份须提供当前档案版本", entity_id)]
            )

        def operation(connection):
            require_occurred(explicit["effective_date"])
            if entity_id is None:
                identifier = "entity_" + uuid.uuid4().hex
                history, profile, revision = [], {}, 1
                connection.execute("INSERT INTO entity VALUES(?, 'person', NULL)", (identifier,))
            else:
                identifier = entity_id
                require_entity(connection, identifier, kinds=("person",))
                current = self._current(connection, identifier, expected_revision)
                history = workforce_records(connection, identifier)
                from .entity_profile_history import current_person_metadata, retained_profile_ids

                verify_history(history, retained_ids=retained_profile_ids(connection, [history]))
                profile = current_person_metadata(connection, current, history=history)
                revision = current["revision"] + 1
            metadata = {
                key: value
                for key, value in explicit.items()
                if key not in {"action", "effective_date", "after_event_id", "initial_unpaid_leave"}
            }
            profile.update(metadata)
            event = WorkforceEvent(
                action=explicit["action"],
                effective_date=explicit["effective_date"],
                initial_unpaid_leave=explicit.get("initial_unpaid_leave", False),
                after_event_id=explicit.get("after_event_id"),
            ).model_dump(mode="json")
            return self._workforce_append(
                connection, identifier, revision, profile, history, event, source, evidence_digest
            )

        return self.engine._write(
            request_id,
            digest(
                [
                    "register_worker",
                    entity_id,
                    expected_revision,
                    explicit,
                    source,
                    evidence_digest,
                ]
            ),
            None,
            ("management",),
            "register_entity" if entity_id is None else "update_entity_profile",
            operation,
        )

    def change_workforce_relationship(
        self,
        entity_id: str,
        data: WorkforceChange,
        *,
        source: str,
        expected_revision: int,
        request_id: str,
        evidence_digest: EvidenceDigest | None = None,
    ):
        explicit = self._explicit(WorkforceChange, data)
        event = WorkforceEvent.model_validate_json(canonical(explicit)).model_dump(mode="json")

        def operation(connection):
            require_entity(connection, entity_id, kinds=("person",))
            current = self._current(connection, entity_id, expected_revision)
            require_occurred(event["effective_date"])
            history = workforce_records(connection, entity_id)
            from .entity_profile_history import current_person_metadata, retained_profile_ids

            verify_history(history, retained_ids=retained_profile_ids(connection, [history]))
            return self._workforce_append(
                connection,
                entity_id,
                current["revision"] + 1,
                current_person_metadata(connection, current, history=history),
                history,
                event,
                source,
                evidence_digest,
            )

        return self.engine._write(
            request_id,
            digest(
                [
                    "change_workforce_relationship",
                    entity_id,
                    expected_revision,
                    explicit,
                    source,
                    evidence_digest,
                ]
            ),
            None,
            ("management",),
            "update_entity_profile",
            operation,
        )

    def _workforce_append(
        self, connection, entity_id, revision, profile, history, event, source, evidence_digest
    ):
        profile = dict(profile)
        profile["workforce_event"] = event
        # The inserted profile ID is the event identity. Allocate once, derive
        # before inserting, then save the checked content under that exact ID.
        profile_id = uuid.uuid4().hex
        candidate = dict(
            profile,
            id=profile_id,
            entity_id=entity_id,
            revision=revision,
        )
        workforce_projection([*history, candidate])
        profile, evidence = self._profile("person", profile, source, evidence_digest)
        saved = self._insert_profile(
            connection, entity_id, revision, profile, source, evidence, profile_id=profile_id
        )
        from .dashboard_model_runtime import record_period

        record_period(connection, event["effective_date"][:7])
        return saved

    def workforce_relationship_history(
        self,
        entity_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
        expected_revision: int | None = None,
        period: YearMonth | None = None,
    ):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        period = str(YearMonth(period)) if period is not None else None
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            require_entity(connection, entity_id, kinds=("person",))
            history = workforce_records(connection, entity_id)
            from .entity_profile_history import retained_profile_ids

            timeline = verify_history(
                history, retained_ids=retained_profile_ids(connection, [history]),
            )
            if not history:
                raise KernelError("entity_profile_corrupt", "对象缺少初始档案", entity_id=entity_id)
            revision = history[-1]["revision"]
            if expected_revision is not None and (
                type(expected_revision) is not int or expected_revision != revision
            ):
                raise KernelError("entity_profile_version_conflict", "对象档案已变化，请重新读取")
            event_version = timeline["event_version"]
            scope = [
                self.store.company_id, self.store.database_id, entity_id, event_version, period,
            ]
            offset = 0 if cursor is None else decode_cursor(cursor, scope)
            active, _, _ = active_events(history)
            events = [
                item for item in reversed(history) if item.get("workforce_event") is not None
            ]
            selected = events[offset : offset + limit]
            times = recorded_event_times(connection, events)
            items = [
                dict(
                    item["workforce_event"],
                    event_id=item["id"],
                    revision=item["revision"],
                    source=item["source"],
                    evidence_digest=item["evidence_digest"],
                    recorded_at=times.get(("display_profile", item["id"])),
                    active=item["id"] in active,
                )
                for item in selected
            ]
            more = offset + len(items) < len(events)
            return {
                "entity_id": entity_id,
                "revision": revision,
                "event_version": event_version,
                "period": period,
                "monthly": monthly_projection(timeline, period) if period is not None else None,
                "items": items,
                "page": {
                    "total_count": len(events),
                    "returned_count": len(items),
                    "has_more": more,
                    "next_cursor": encode_cursor(scope, offset + len(items)) if more else None,
                },
            }

    def correct_fund_account_identity(
        self,
        entity_id: str,
        *,
        account_type: Literal["bank", "platform"],
        data: dict,
        expected_identity_digest: EvidenceDigest,
        recording_error_confirmed: Literal[True],
        source: str,
        evidence_digest: EvidenceDigest,
        request_id: str,
    ):
        if recording_error_confirmed is not True:
            raise ValueError("recording_error_confirmed must be true")
        if account_type not in ("bank", "platform"):
            raise ValueError("cash account names are changed through update_entity_profile")
        require_fund_source(source, evidence_digest)
        _, identity = account_identity(account_type, data)

        def operation(connection):
            result = correct_fund_identity(
                connection,
                entity_id,
                account_type,
                identity,
                expected_identity_digest=expected_identity_digest,
                source=source,
                evidence_digest=evidence_digest,
            )
            self.engine.fault("fund_account_identity_corrected", connection)
            return {
                "status": "corrected",
                "entity_id": entity_id,
                "previous_identity_digest": expected_identity_digest,
                "fund_account": result,
            }

        return self.engine._write(
            request_id,
            digest(
                [
                    "correct_fund_account_identity",
                    entity_id,
                    account_type,
                    identity,
                    expected_identity_digest,
                    recording_error_confirmed,
                    source,
                    evidence_digest,
                ]
            ),
            None,
            ("management",),
            "correct_fund_account_identity",
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
            needle = query.casefold().strip() if query else None
            candidates = {}
            for entity_id, profile in all_profiles.items():
                if kind is not None and kind != profile["entity_kind"]:
                    continue
                values = [
                    entity_id,
                    profile.get("display_name"),
                    profile.get("display_number"),
                    *profile.get("external_identifiers", {}).values(),
                ]
                account = (
                    require_fund_account(connection, entity_id)
                    if profile["entity_kind"] == "fund_account"
                    else None
                )
                if account is not None:
                    values.extend((*account["identity"].values(), account["detail_code"]))
                exact = needle is not None and any(
                    needle == value.casefold() for value in values if value
                )
                if needle and not any(needle in value.casefold() for value in values if value):
                    continue
                candidates[entity_id] = profile, exact, account

            # Only the latest current references can decide recent_period and
            # its range filter. Prove every tied source before using that
            # indexed value, including candidates excluded by used_to; a
            # missing index row still belongs to full integrity verification.
            usage = {}
            if candidates:
                latest_rows = list(
                    connection.execute(
                        "WITH latest AS (SELECT r.entity_id,max(r.period) last_period "
                        "FROM entity_reference_current r JOIN fact_current c "
                        "ON c.fact_id=r.fact_id "
                        "WHERE r.entity_id IN (SELECT value FROM json_each(?)) "
                        "GROUP BY r.entity_id) "
                        "SELECT l.entity_id,l.last_period,r.fact_id FROM latest l "
                        "JOIN entity_reference_current r ON r.entity_id=l.entity_id "
                        "AND r.period=l.last_period JOIN fact_current c ON c.fact_id=r.fact_id",
                        (canonical(sorted(candidates)),),
                    )
                )
                if latest_rows:
                    from .entity_references import verify_hits

                    verify_hits(
                        connection,
                        [
                            {"fact_id": ident}
                            for ident in sorted({row["fact_id"] for row in latest_rows})
                        ],
                        registry=self.store.registry,
                    )
                    usage = {row["entity_id"]: row["last_period"] for row in latest_rows}
            relationships = list(connection.execute("SELECT * FROM entity_resolution"))
            timelines = workforce_timelines(
                connection,
                [ident for ident, (profile, _, _) in candidates.items()
                 if profile["entity_kind"] == "person"],
            )
            items = []
            for entity_id, (profile, exact, account) in candidates.items():
                recent = usage.get(entity_id)
                if (lower is not None and (recent is None or recent < lower)) or (
                    upper is not None and (recent is None or recent > upper)
                ):
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
                        **({
                            "employment_state": timelines[entity_id]["employment_state"],
                            "event_version": timelines[entity_id]["event_version"],
                        } if entity_id in timelines else {}),
                        **({"fund_account": account} if account is not None else {}),
                    }
                )
            items.sort(
                key=lambda row: (
                    row["match"] != "exact",
                    pinyin_key(row["profile"]["display_name"] or ""),
                    row["entity_id"],
                )
            )
            return {"schema_version": 1, "items": items[:limit], "has_more": len(items) > limit}
