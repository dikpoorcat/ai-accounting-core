"""Typed company fund identities and their immutable, kernel-assigned subjects."""

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import Field, ValidationError, field_validator

from .content_history_context import source_canonical as canonical
from .content_history_context import source_digest as digest
from .content_history_context import source_json_loads
from .contracts import KernelError, NeedsInformation
from .types import evidence_digest_bytes

PARENTS = {"bank": "1002", "cash": "1001", "platform": "1012"}
IDENTITY_FIELDS = {
    "bank": ("bank_name", "bank_account_number", "account_holder_name"),
    "cash": (),
    "platform": ("provider_name", "account_identifier", "account_holder_name"),
}
SOURCES = [
    "bank_account_document",
    "bank_statement",
    "platform_account_document",
    "owner_confirmation",
]


def _identity_field(meaning):
    return Field(
        min_length=1,
        max_length=200,
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": meaning,
                "reusable_sources": SOURCES,
                "allowed_precision": ["exact"],
            }
        },
    )


@lru_cache(maxsize=1)
def fund_profile_models():
    # Fund identities and management profiles have separate persisted sources.
    from .entities import FundAccountProfile

    class BankAccountProfile(FundAccountProfile):
        bank_name: str = _identity_field("fund_account_bank_name")
        bank_account_number: str = _identity_field("complete_company_bank_account_number")
        account_holder_name: str = _identity_field("explicit_account_holder")

        @field_validator("bank_account_number")
        @classmethod
        def complete_number(cls, value):
            return normalize_identifier(value, bank=True)

        @field_validator("bank_name", "account_holder_name")
        @classmethod
        def named(cls, value):
            return require_text(value)

    class PlatformAccountProfile(FundAccountProfile):
        provider_name: str = _identity_field("payment_platform_provider")
        account_identifier: str = _identity_field("complete_platform_account_identifier")
        account_holder_name: str = _identity_field("explicit_account_holder")

        @field_validator("account_identifier")
        @classmethod
        def complete_number(cls, value):
            return normalize_identifier(value)

        @field_validator("provider_name", "account_holder_name")
        @classmethod
        def named(cls, value):
            return require_text(value)

    class CashAccountProfile(FundAccountProfile):
        display_name: str = Field(
            min_length=1, max_length=200,
            json_schema_extra={"x-accounting-fact": {
                "role": "management", "meaning": "explicit_managed_cash_account_label",
                "reusable_sources": ["owner_confirmation"], "allowed_precision": ["exact"],
            }},
        )

        @field_validator("display_name")
        @classmethod
        def named(cls, value):
            return require_text(value)

    return {
        "bank": BankAccountProfile,
        "cash": CashAccountProfile,
        "platform": PlatformAccountProfile,
    }


def require_text(value):
    value = value.strip()
    if not value:
        raise ValueError("account identity must be explicitly provided")
    return value


def normalize_identifier(value, *, bank=False):
    if not isinstance(value, str):
        raise ValueError("account number must be an exact string")
    result = re.sub(r"\s+", "", value) if bank else value.strip()
    if not result or any(char in result for char in "*＊•●…"):
        raise ValueError("a complete, unmasked account identifier is required")
    if bank and (not result.isascii() or not result.isdigit()):
        raise ValueError("bank account number must contain exact digits, not a numeric conversion")
    return result


def identity_issue(field, message, *, source=None):
    return {
        "field": field,
        "message": message,
        "semantics": "accounting",
        "reusable_sources": [source] if source else SOURCES,
        "allowed_precision": ["exact"],
    }


def require_fund_source(source, evidence_digest):
    issues = []
    if not isinstance(source, str) or not source.strip() or len(source) > 2000:
        issues.append(identity_issue("source", "资金账户登记必须明确开户资料或确认来源"))
    if evidence_digest is None:
        issues.append(identity_issue("evidence_digest", "资金账户登记必须引用已保全来源"))
    if issues:
        raise NeedsInformation(issues)


def validate_fund_profile(account_type, data):
    models = fund_profile_models()
    if account_type not in models:
        raise ValueError("invalid account type")
    try:
        return models[account_type].model_validate_json(canonical(data))
    except ValidationError as exc:
        issues = [
            identity_issue(
                "data." + ".".join(map(str, error["loc"])), "资金账户身份资料缺失或格式无效"
            )
            for error in exc.errors(include_input=False, include_url=False)
            if error["loc"] and error["loc"][0] in IDENTITY_FIELDS[account_type]
        ]
        if account_type == "cash":
            issues.extend(
                {**identity_issue("data.display_name", "现金账户登记需要明确管理名称"),
                 "semantics": "management", "reusable_sources": ["owner_confirmation"]}
                for error in exc.errors(include_input=False, include_url=False)
                if error["loc"] and error["loc"][0] == "display_name"
            )
        if issues:
            raise NeedsInformation(issues) from exc
        raise


FUND_ACCOUNTS_DDL = """
CREATE TABLE fund_account_definition(
 entity_id TEXT PRIMARY KEY REFERENCES entity(id),
 account_type TEXT NOT NULL CHECK(account_type IN('bank','cash','platform')),
 parent_account TEXT NOT NULL CHECK(parent_account IN('1001','1002','1012')),
 detail_code TEXT NOT NULL UNIQUE,sequence INTEGER NOT NULL CHECK(sequence>0),
 identity TEXT NOT NULL CHECK(json_valid(identity)),identity_key TEXT NOT NULL,
 source TEXT NOT NULL,evidence_digest BLOB NOT NULL REFERENCES evidence(digest),
 digest BLOB NOT NULL CHECK(length(digest)=32),
 UNIQUE(parent_account,sequence),
 CHECK((account_type='bank' AND parent_account='1002') OR
 (account_type='cash' AND parent_account='1001') OR
 (account_type='platform' AND parent_account='1012'))) STRICT;
CREATE TRIGGER immutable_fund_account_definition_UPDATE BEFORE UPDATE ON fund_account_definition
 BEGIN SELECT RAISE(ABORT,'immutable fund account definition'); END;
CREATE TRIGGER immutable_fund_account_definition_DELETE BEFORE DELETE ON fund_account_definition
 BEGIN SELECT RAISE(ABORT,'immutable fund account definition'); END;
CREATE TABLE fund_account_identity_revision(
 entity_id TEXT NOT NULL REFERENCES fund_account_definition(entity_id),
 revision INTEGER NOT NULL CHECK(revision>0),
 parent_digest BLOB NOT NULL CHECK(length(parent_digest)=32),
 identity TEXT NOT NULL CHECK(json_valid(identity)),identity_key TEXT NOT NULL,
 source TEXT NOT NULL,evidence_digest BLOB NOT NULL REFERENCES evidence(digest),
 digest BLOB NOT NULL UNIQUE CHECK(length(digest)=32),
 PRIMARY KEY(entity_id,revision)) STRICT;
CREATE TRIGGER immutable_fund_account_identity_revision_UPDATE
 BEFORE UPDATE ON fund_account_identity_revision
 BEGIN SELECT RAISE(ABORT,'immutable fund account identity revision'); END;
CREATE TRIGGER immutable_fund_account_identity_revision_DELETE
 BEFORE DELETE ON fund_account_identity_revision
 BEGIN SELECT RAISE(ABORT,'immutable fund account identity revision'); END;
"""


def has_fund_definitions(connection):
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='fund_account_definition'"
        ).fetchone()
        is not None
    )


def _payload(row):
    return [
        row["entity_id"],
        row["account_type"],
        row["parent_account"],
        row["detail_code"],
        row["sequence"],
        source_json_loads(row["identity"]),
        row["identity_key"],
        row["source"],
        bytes(row["evidence_digest"]).hex(),
    ]


def _record(row):
    try:
        identity = source_json_loads(row["identity"])
        expected_code = f"{PARENTS[row['account_type']]}.{row['sequence']:04d}"
        if (
            set(identity) != set(IDENTITY_FIELDS[row["account_type"]])
            or row["parent_account"] != PARENTS[row["account_type"]]
            or row["detail_code"] != expected_code
            or digest(_payload(row)) != row["digest"]
        ):
            raise ValueError("invalid definition")
        for value in identity.values():
            if not isinstance(value, str) or not value.strip():
                raise ValueError("invalid identity")
        identifier = identity.get("bank_account_number", identity.get("account_identifier"))
        if identifier is not None:
            normalize_identifier(identifier, bank=row["account_type"] == "bank")
    except (ValueError, KeyError, TypeError) as exc:
        raise KernelError(
            "content_integrity_failed", "资金账户定义或明细科目校验失败", entity_id=row["entity_id"]
        ) from exc
    return dict(row) | {
        "identity": identity,
        "evidence_digest": bytes(row["evidence_digest"]).hex(),
        "digest": bytes(row["digest"]).hex(),
    }


def require_fund_account(connection, entity_id, account_type=None):
    row = (
        connection.execute(
            "SELECT * FROM fund_account_definition WHERE entity_id=?", (entity_id,)
        ).fetchone()
        if has_fund_definitions(connection)
        else None
    )
    if row is None:
        raise NeedsInformation(
            [
                identity_issue(
                    "fund_account_id", "资金账户尚未完成身份资料与明细科目登记", source=entity_id
                )
            ]
        )
    result = _identity_history(connection, _record(row))[-1]
    entity = connection.execute(
        "SELECT kind,account_type FROM entity WHERE id=?", (entity_id,)
    ).fetchone()
    if (
        entity is None
        or entity["kind"] != "fund_account"
        or entity["account_type"] != result["account_type"]
        or (account_type and result["account_type"] != account_type)
    ):
        raise KernelError(
            "entity_kind_mismatch", "资金账户身份或类型与业务不一致", entity_id=entity_id
        )
    return result


subject_for_account = require_fund_account


def fund_account_definitions(connection, entity_ids=None):
    if not has_fund_definitions(connection):
        return []
    identifiers = None if entity_ids is None else sorted(set(entity_ids))
    if identifiers == []:
        return []
    sql = "SELECT * FROM fund_account_definition"
    parameters = ()
    if identifiers is not None:
        sql += " WHERE entity_id IN(SELECT value FROM json_each(?))"
        parameters = (canonical(identifiers),)
    rows = connection.execute(sql + " ORDER BY entity_id", parameters)
    return [_identity_history(connection, _record(row))[-1] for row in rows]


def _revision_payload(entity_id, revision, parent_digest, identity, key, source, evidence):
    return [entity_id, revision, parent_digest, identity, key, source, evidence]


def _identity_history(connection, original):
    """Verify the immutable identity chain without altering its subject or original basis."""
    history = [original]
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='fund_account_identity_revision'"
        ).fetchone()
        is None
    ):
        return history
    for row in connection.execute(
        "SELECT * FROM fund_account_identity_revision WHERE entity_id=? ORDER BY revision",
        (original["entity_id"],),
    ):
        try:
            identity = source_json_loads(row["identity"])
            if original["account_type"] == "cash":
                raise ValueError("cash labels do not belong to identity history")
            _, normalized = account_identity(original["account_type"], identity)
            evidence = bytes(row["evidence_digest"]).hex()
            parent = bytes(row["parent_digest"]).hex()
            key = identity_key(original["account_type"], identity, original["entity_id"])
            payload = _revision_payload(
                original["entity_id"],
                row["revision"],
                parent,
                identity,
                key,
                row["source"],
                evidence,
            )
            if (
                row["revision"] != len(history)
                or parent != history[-1]["digest"]
                or normalized != identity
                or row["identity_key"] != key
                or digest(payload) != row["digest"]
                or not row["source"].strip()
                or not connection.execute(
                    "SELECT 1 FROM evidence WHERE digest=?", (row["evidence_digest"],)
                ).fetchone()
            ):
                raise ValueError("invalid identity history")
        except (ValueError, KeyError, TypeError) as exc:
            raise KernelError(
                "content_integrity_failed",
                "资金账户身份更正历史校验失败",
                entity_id=original["entity_id"],
            ) from exc
        history.append(
            original
            | {
                "identity": identity,
                "identity_key": key,
                "source": row["source"],
                "evidence_digest": evidence,
                "digest": bytes(row["digest"]).hex(),
            }
        )
    return history


def historical_fund_account(connection, entity_id, identity_digest):
    """Return exactly the adopted identity snapshot, including the original definition."""
    wanted = evidence_digest_bytes(identity_digest).hex()
    row = (
        connection.execute(
            "SELECT * FROM fund_account_definition WHERE entity_id=?", (entity_id,)
        ).fetchone()
        if has_fund_definitions(connection)
        else None
    )
    if row is not None:
        for snapshot in _identity_history(connection, _record(row)):
            if snapshot["digest"] == wanted:
                return snapshot
    raise KernelError(
        "content_integrity_failed", "未找到精确采用的资金账户身份版本", entity_id=entity_id
    )


def account_identity(account_type, data):
    profile = validate_fund_profile(account_type, data)
    values = profile.model_dump(mode="json")
    identity = {key: values[key] for key in IDENTITY_FIELDS[account_type]}
    return profile, identity


def identity_key(account_type, identity, entity_id=None):
    if account_type == "bank":
        return digest([identity["bank_name"].casefold(), identity["bank_account_number"]]).hex()
    if account_type == "platform":
        return digest([identity["provider_name"].casefold(), identity["account_identifier"]]).hex()
    return entity_id


def existing_fund_account(connection, account_type, identity):
    if account_type == "cash":
        return None
    key = identity_key(account_type, identity)
    matches = [
        row
        for row in fund_account_definitions(connection)
        if row["account_type"] == account_type and row["identity_key"] == key
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise KernelError("content_integrity_failed", "同一实际资金身份存在多个受管账户")
    result = matches[0]
    if identity["account_holder_name"] != result["identity"]["account_holder_name"]:
        raise NeedsInformation(
            [
                identity_issue(
                    "data.account_holder_name",
                    "同一资金账号的开户归属资料冲突",
                    source=result["entity_id"],
                )
            ]
        )
    return result


def correct_fund_identity(
    connection, entity_id, account_type, data, *, expected_identity_digest, source, evidence_digest
):
    """Append a sourced recording correction; all business and subject identities stay fixed."""
    if account_type not in ("bank", "platform"):
        raise ValueError("cash account names are changed through update_entity_profile")
    require_fund_source(source, evidence_digest)
    _, identity = account_identity(account_type, data)
    evidence = evidence_digest_bytes(evidence_digest)
    if not connection.execute("SELECT 1 FROM evidence WHERE digest=?", (evidence,)).fetchone():
        raise NeedsInformation([identity_issue("evidence_digest", "资金账户身份来源尚未登记")])
    current = require_fund_account(connection, entity_id, account_type)
    expected = evidence_digest_bytes(expected_identity_digest).hex()
    if current["digest"] != expected:
        raise KernelError("fund_account_identity_version_conflict", "账户身份已经变化，请重新读取")
    if account_type != "cash":
        key = identity_key(account_type, identity)
        if any(
            row["entity_id"] != entity_id
            and row["account_type"] == account_type
            and row["identity_key"] == key
            for row in fund_account_definitions(connection)
        ):
            raise NeedsInformation(
                [
                    identity_issue(
                        "data.bank_account_number"
                        if account_type == "bank"
                        else "data.account_identifier",
                        "更正后的实际资金账号已由另一账户管理",
                    )
                ]
            )
    if identity == current["identity"]:
        return current
    revision = connection.execute(
        "SELECT coalesce(max(revision),0)+1 FROM fund_account_identity_revision WHERE entity_id=?",
        (entity_id,),
    ).fetchone()[0]
    key = identity_key(account_type, identity, entity_id)
    payload = _revision_payload(
        entity_id, revision, expected, identity, key, source, evidence.hex()
    )
    connection.execute(
        "INSERT INTO fund_account_identity_revision VALUES(?,?,?,?,?,?,?,?)",
        (
            entity_id,
            revision,
            bytes.fromhex(expected),
            canonical(identity),
            key,
            source,
            evidence,
            digest(payload),
        ),
    )
    return require_fund_account(connection, entity_id, account_type)


def install_fund_account_definition(
    connection, entity_id, account_type, data, *, source, evidence_digest
):
    _, identity = account_identity(account_type, data)
    require_fund_source(source, evidence_digest)
    evidence = evidence_digest_bytes(evidence_digest)
    if connection.execute("SELECT 1 FROM evidence WHERE digest=?", (evidence,)).fetchone() is None:
        raise NeedsInformation([identity_issue("evidence_digest", "资金账户身份来源尚未登记")])
    present = connection.execute(
        "SELECT * FROM fund_account_definition WHERE entity_id=?", (entity_id,)
    ).fetchone()
    if present is not None:
        result = require_fund_account(connection, entity_id, account_type)
        if result["identity"] != identity:
            raise KernelError(
                "fund_account_identity_immutable",
                "账户身份不能通过档案修改，请使用有来源的身份纠错",
            )
        return result
    duplicate = existing_fund_account(connection, account_type, identity)
    if duplicate is not None:
        raise KernelError(
            "fund_account_duplicate", "该实际资金账户已经登记", entity_id=duplicate["entity_id"]
        )
    entity = connection.execute(
        "SELECT kind,account_type FROM entity WHERE id=?", (entity_id,)
    ).fetchone()
    if entity is None or entity["kind"] != "fund_account" or entity["account_type"] != account_type:
        raise KernelError("entity_kind_mismatch", "资金账户定义与实体不一致")
    parent = PARENTS[account_type]
    sequence = connection.execute(
        "SELECT coalesce(max(sequence),0)+1 FROM fund_account_definition WHERE parent_account=?",
        (parent,),
    ).fetchone()[0]
    code = f"{parent}.{sequence:04d}"
    key = identity_key(account_type, identity, entity_id)
    payload = [
        entity_id,
        account_type,
        parent,
        code,
        sequence,
        identity,
        key,
        source,
        evidence.hex(),
    ]
    connection.execute(
        "INSERT INTO fund_account_definition VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            entity_id,
            account_type,
            parent,
            code,
            sequence,
            canonical(identity),
            key,
            source,
            evidence,
            digest(payload),
        ),
    )
    return require_fund_account(connection, entity_id, account_type)


def verify_fund_accounts(connection):
    if not has_fund_definitions(connection):
        return
    for row in connection.execute("SELECT id,account_type FROM entity WHERE kind='fund_account'"):
        require_fund_account(connection, row["id"], row["account_type"])
    seen = set()
    for definition in fund_account_definitions(connection):
        if (
            identity_key(
                definition["account_type"], definition["identity"], definition["entity_id"]
            )
            != definition["identity_key"]
        ):
            raise KernelError("content_integrity_failed", "资金账户身份键校验失败")
        key = (definition["account_type"], definition["identity_key"])
        if key in seen:
            raise KernelError("content_integrity_failed", "同一实际资金身份存在多个受管账户")
        seen.add(key)
