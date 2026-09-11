"""One immutable catalogue grant; each exact company range consumes it locally."""

from __future__ import annotations

import json
import re
from contextlib import closing

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..runtime import connect
from ..types import YearMonth, canonical, digest
from .approval import APPROVAL_LIFETIME
from .primitives import IdentityError


class CloseBatchTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    company_id: str = Field(min_length=1, max_length=200)
    database_id: str = Field(min_length=1, max_length=200)
    from_period: YearMonth
    through_period: YearMonth
    calculation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    epochs: dict[str, int]

    @model_validator(mode="after")
    def exact_range(self):
        if self.from_period > self.through_period:
            raise ValueError("close range must be increasing")
        if set(self.epochs) != {"accounting", "material", "management"} or any(
            type(value) is not int or value < 0 for value in self.epochs.values()
        ):
            raise ValueError("invalid state versions")
        return self


def targets_payload(targets):
    if not 1 <= len(targets) <= 32:
        raise IdentityError("IDENTITY_APPROVAL_INVALID")
    checked = [CloseBatchTarget.model_validate_json(canonical(target)) for target in targets]
    if len({target.company_id for target in checked}) != len(checked):
        raise IdentityError("IDENTITY_APPROVAL_INVALID")
    return [
        target.model_dump(mode="json") for target in sorted(checked, key=lambda t: t.company_id)
    ]


def _validate_grant(row, authority, *, now):
    if (
        row is None
        or any(
            row[field] != getattr(authority, field)
            for field in ("catalog_instance_id", "owner_id", "session_id", "credential_version")
        )
        or not row["confirmed_at"] <= now < row["expires_at"]
    ):
        raise IdentityError("IDENTITY_CLOSE_APPROVAL_INVALID")
    targets = json.loads(row["targets"])
    if digest(targets) != row["digest"]:
        raise IdentityError("IDENTITY_CLOSE_APPROVAL_INVALID")
    return targets


def _result(row):
    return {
        "batch_id": row["id"],
        "expires_at": row["expires_at"],
        "confirmed_at": row["confirmed_at"],
        "approvals": [
            {**target, "approval_id": row["id"]} for target in json.loads(row["targets"])
        ],
    }


def issue_batch_approval(service, *, token, password, batch_id, targets):
    """Called after all previews were rechecked under the owner's authorization gate.

    Password verification and the single immutable manifest share one catalogue
    transaction. A retry of the same native request recovers its committed grant.
    """
    if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        raise IdentityError("IDENTITY_REQUEST_INVALID")
    targets = targets_payload(targets)
    with service._transaction() as connection:
        authority = service._authorize(connection, token, request_id=batch_id)
        previous = connection.execute(
            "SELECT * FROM security_close_batch WHERE id=?", (batch_id,)
        ).fetchone()
        now = service.now()
        if previous is not None:
            existing = _validate_grant(previous, authority, now=now)
            if existing != targets:
                raise IdentityError("IDENTITY_APPROVAL_INVALID")
            return _result(previous)
        service._password_check(
            connection, service._owner(connection), password, now, request_id=batch_id
        )
        connection.execute(
            "INSERT INTO security_close_batch VALUES(?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                authority.catalog_instance_id,
                authority.owner_id,
                authority.session_id,
                authority.credential_version,
                canonical(targets),
                digest(targets),
                now,
                now + APPROVAL_LIFETIME,
            ),
        )
        service._audit(
            connection,
            "close_batches_approved",
            now,
            owner=authority.owner_id,
            session=authority.session_id,
            request_id=batch_id,
        )
        return _result(
            connection.execute(
                "SELECT * FROM security_close_batch WHERE id=?", (batch_id,)
            ).fetchone()
        )


def batch_approval_exists(service, batch_id):
    with closing(connect(service.path, read_only=True)) as connection:
        service._check_catalog(connection)
        return (
            connection.execute(
                "SELECT 1 FROM security_close_batch WHERE id=?", (batch_id,)
            ).fetchone()
            is not None
        )


def read_batch_approval(service, *, batch_id, authority):
    with service.authorization_gate:
        service.validate_authority(authority)
        return _read_batch_approval(service, batch_id, authority)


def _read_batch_approval(service, batch_id, authority):
    with closing(connect(service.path, read_only=True)) as connection:
        service._check_catalog(connection)
        row = connection.execute(
            "SELECT * FROM security_close_batch WHERE id=?", (batch_id,)
        ).fetchone()
        _validate_grant(row, authority, now=service.now())
        return _result(row)


def consume_batch_approval(
    connection,
    approval_id,
    *,
    service,
    authority,
    company_id,
    database_id,
    from_period,
    through_period,
    preview_digest,
    epochs,
):
    """Inside the company's guarded range transaction; insert rolls back with close."""
    if not connection.in_transaction:
        raise IdentityError("IDENTITY_APPROVAL_TRANSACTION_REQUIRED")
    identity = connection.execute(
        "SELECT company_id,database_id FROM identity WHERE id=1"
    ).fetchone()
    if identity is None or tuple(identity) != (company_id, database_id):
        raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
    expected = CloseBatchTarget(
        company_id=company_id,
        database_id=database_id,
        from_period=from_period,
        through_period=through_period,
        calculation_hash=preview_digest,
        epochs=epochs,
    ).model_dump(mode="json")
    grant = read_batch_approval(service, batch_id=approval_id, authority=authority)
    targets = [
        {key: value for key, value in target.items() if key != "approval_id"}
        for target in grant["approvals"]
    ]
    matching = next((target for target in targets if _same_target(expected, target)), None)
    if matching is None:
        raise IdentityError("IDENTITY_CLOSE_APPROVAL_INVALID")
    if connection.execute(
        "SELECT 1 FROM security_close_batch_receipt WHERE batch_id=?", (approval_id,)
    ).fetchone():
        raise IdentityError("IDENTITY_CLOSE_APPROVAL_INVALID")
    now = service.now()
    connection.execute(
        "INSERT INTO security_close_batch_receipt VALUES(?,?,?,?,?,?,?,?,?)",
        (
            approval_id,
            authority.catalog_instance_id,
            company_id,
            database_id,
            YearMonth(from_period).ordinal,
            YearMonth(through_period).ordinal,
            bytes.fromhex(preview_digest),
            digest(matching),
            now,
        ),
    )
    return {
        "approval_id": approval_id,
        "owner_id": authority.owner_id,
        "batch_id": approval_id,
        "company_id": company_id,
        "database_id": database_id,
        "from_period": from_period,
        "through_period": through_period,
        "preview_digest": preview_digest,
        "confirmed_at": grant["confirmed_at"],
        "catalog_instance_id": authority.catalog_instance_id,
        "credential_version": authority.credential_version,
        "method": "local_password_batch_reauthentication",
    }


def _same_epochs(first, second):
    return all(first[key] == second[key] for key in ("accounting", "material"))


def _same_target(first, second):
    return all(
        first[key] == second[key]
        for key in (
            "company_id",
            "database_id",
            "from_period",
            "through_period",
            "calculation_hash",
        )
    ) and _same_epochs(first["epochs"], second["epochs"])


class CloseBatchHost:
    """Resident adapter: native display and live range verification share one plan."""

    def __init__(self, service):
        self.service = service

    def inspect(self, request):
        targets = targets_payload(request["batches"])
        companies = {item["id"]: item for item in self.service.catalog.companies()}
        display = []
        for target in targets:
            company = companies.get(target["company_id"])
            if company is None or company["database_id"] != target["database_id"]:
                raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
            key = target["company_id"], target["database_id"], target["calculation_hash"]
            preview = self.service.close_range_previews.get(key)
            if (
                preview is None
                or any(
                    preview[field] != target[field] for field in ("from_period", "through_period")
                )
                or not _same_epochs(preview["epochs"], target["epochs"])
            ):
                raise IdentityError("ACCOUNTING_PERIOD_CALCULATION_STALE")
            if preview["status"] != "preview" or not preview["manifests"]:
                raise IdentityError("ACCOUNTING_PERIOD_NOT_OPEN")
            display.append({**target, "company_name": company["name"]})
        return {"company_name": "所列历史关账公司", "batches": display}

    def issue(self, request, token, password, batch_id):
        service = self.service
        authority = service.security.authorize(token)
        targets = targets_payload(request["batches"])
        # Recover a durable committed result without reissuing or extending it.
        with closing(connect(service.security.path, read_only=True)) as connection:
            service.security._check_catalog(connection)
            exists = connection.execute(
                "SELECT 1 FROM security_close_batch WHERE id=?", (batch_id,)
            ).fetchone()
        if exists:
            result = read_batch_approval(service.security, batch_id=batch_id, authority=authority)
            if [
                {k: v for k, v in item.items() if k != "approval_id"}
                for item in result["approvals"]
            ] != targets:
                raise IdentityError("IDENTITY_APPROVAL_INVALID")
            return result
        self.inspect(request)
        # Only the public preview command can populate this process-local cache.
        # Its exact digest is trusted while all accounting/material inputs retain
        # their epochs. A daemon restart discards the cache; new approval requires
        # a fresh preview. The company close transaction still rebuilds every month.
        engines = [service.engine(target["company_id"]) for target in targets]
        # Every production writer uses this gate. No multi-database transaction:
        # merely recheck each immutable identity/state before one catalogue commit.
        with service.security.authorization_gate:
            service.security.validate_authority(authority)
            for engine, target in zip(engines, targets, strict=True):
                with engine.store.connection(read_only=True) as connection:
                    if not _same_epochs(engine.store.epochs(connection), target["epochs"]):
                        raise IdentityError("ACCOUNTING_PERIOD_CALCULATION_STALE")
            return issue_batch_approval(
                service.security, token=token, password=password, batch_id=batch_id, targets=targets
            )
