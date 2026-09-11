"""Company-local, password-reauthenticated close grants consumed with the close."""

import re
import uuid

from ..types import YearMonth
from .primitives import IdentityError
from .service import SECOND, Authority

APPROVAL_LIFETIME = 30 * 60 * SECOND


def _binding(connection, authority, company_id, database_id, period, preview_digest, epochs):
    if not connection.in_transaction:
        raise IdentityError("IDENTITY_APPROVAL_TRANSACTION_REQUIRED")
    if not isinstance(authority, Authority):
        raise IdentityError("IDENTITY_SESSION_INVALID")
    row = connection.execute("SELECT company_id,database_id FROM identity WHERE id=1").fetchone()
    if row is None or row[0] != company_id or row[1] != database_id:
        raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
    if not isinstance(preview_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", preview_digest):
        raise IdentityError("IDENTITY_APPROVAL_INVALID")
    if not isinstance(epochs, dict) or set(epochs) != {"accounting", "material", "management"}:
        raise IdentityError("IDENTITY_APPROVAL_INVALID")
    if any(type(value) is not int or value < 0 for value in epochs.values()):
        raise IdentityError("IDENTITY_APPROVAL_INVALID")
    return (
        authority.catalog_instance_id,
        company_id,
        database_id,
        YearMonth(period).ordinal,
        bytes.fromhex(preview_digest),
        epochs["accounting"],
        epochs["material"],
        epochs["management"],
        authority.owner_id,
        authority.session_id,
        authority.credential_version,
    )


def insert_close_approval(
    connection, *, authority, company_id, database_id, period, preview_digest, epochs, now
):
    """Internal primitive; the security service first reauthenticates the password.

    The host checks the current close preview under authorization_gate before
    invoking this function, and commits the caller-owned company transaction.
    """
    binding = _binding(
        connection, authority, company_id, database_id, period, preview_digest, epochs
    )
    approval_id = uuid.uuid4().hex
    connection.execute(
        """INSERT INTO security_close_approval(
        id,catalog_instance_id,company_id,database_id,period,preview_digest,
        accounting_epoch,material_epoch,management_epoch,owner_id,session_id,credential_version,
        confirmed_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (approval_id, *binding, now, now + APPROVAL_LIFETIME),
    )
    return {"approval_id": approval_id, "expires_at": now + APPROVAL_LIFETIME}


def consume_close_approval(
    connection,
    approval_id,
    *,
    authority,
    company_id,
    database_id,
    period,
    preview_digest,
    epochs,
    now,
):
    """Called only after live authorization, inside the company's close transaction.

    Consumption rolls back if close publication or COMMIT fails. The host must
    retain SecurityService.authorization_gate to order this commit with logout
    and credential rotation. A closed-request idempotency replay bypasses this
    consumption only after the host has authenticated the current request.
    """
    binding = _binding(
        connection, authority, company_id, database_id, period, preview_digest, epochs
    )
    row = connection.execute(
        "SELECT * FROM security_close_approval WHERE id=?", (approval_id,)
    ).fetchone()
    fields = (
        "catalog_instance_id",
        "company_id",
        "database_id",
        "period",
        "preview_digest",
        "accounting_epoch",
        "material_epoch",
        "management_epoch",
        "owner_id",
        "session_id",
        "credential_version",
    )
    if (
        row is None
        or tuple(row[name] for name in fields) != binding
        or row["consumed_at"] is not None
        or not row["confirmed_at"] <= now < row["expires_at"]
    ):
        raise IdentityError("IDENTITY_CLOSE_APPROVAL_INVALID")
    changed = connection.execute(
        "UPDATE security_close_approval SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
        (now, approval_id),
    ).rowcount
    if changed != 1:
        raise IdentityError("IDENTITY_CLOSE_APPROVAL_INVALID")
    return {
        "approval_id": approval_id,
        "owner_id": authority.owner_id,
        "catalog_instance_id": authority.catalog_instance_id,
        "credential_version": authority.credential_version,
        "confirmed_at": row["confirmed_at"],
        "method": "local_password_reauthentication",
    }
