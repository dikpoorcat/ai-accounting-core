"""Verified, self-contained backups of the new SQLite company format.

Evidence bytes live inside the database, so the SQLite Online Backup API captures
the accounting state and its evidence in the same snapshot. No live ``.sqlite``
file is copied, and no archive member is extracted using a caller-supplied path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import KernelError
from .permissions import (
    PrivatePathError,
    adopt_private_file,
    assert_private_file,
    create_private_file,
    ensure_private_file,
    private_temporary_directory,
    reject_reparse_path,
)
from .runtime import connect, require_supported_runtime
from .schema_bundle import DATABASE_FORMAT_KEYS, production_bundle, valid_database_format
from .versions import database_format, upgrade, verify_schema

FORMAT = "ai-accounting-kernel/company-backup"
FORMAT_VERSION = 2
DATABASE_MEMBER = "company.sqlite"
MANIFEST_MEMBER = "manifest.json"
MAX_MANIFEST_BYTES = 64 * 1024
DEFAULT_MAX_DATABASE_BYTES = 32 * 1024**3


class BackupError(ValueError):
    """A backup failed verification or would replace an unapproved target."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_MANIFEST_KEYS = frozenset(
    {
        "format",
        "format_version",
        "database_format",
        "identity",
        "database_member",
        "database_bytes",
        "database_sha256",
        "evidence_count",
        "latest_closed_period",
        "created_at",
        "request_id",
    }
)
_DATABASE_FORMAT_KEYS = DATABASE_FORMAT_KEYS
_IDENTITY_KEYS = frozenset({"company_id", "taxpayer_id", "database_id"})


def _resolve_bundle(value):
    return production_bundle() if value is None else value


def _error(code: str, message: str) -> BackupError:
    return BackupError(code, message)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _identity(connection: sqlite3.Connection) -> dict[str, str]:
    tables = {row[1]: row for row in connection.execute("PRAGMA table_list")}
    for name in ("identity", "evidence", "period_close"):
        if name not in tables or tables[name][2] != "table" or tables[name][5] != 1:
            raise _error("backup_content_invalid", f"Required STRICT table is missing: {name}")
    rows = connection.execute(
        "SELECT id,company_id,taxpayer_id,database_id FROM identity"
    ).fetchall()
    if len(rows) != 1 or rows[0]["id"] != 1:
        raise _error(
            "backup_content_invalid", "Company identity must contain exactly one singleton row"
        )
    identity = dict(rows[0])
    identity.pop("id")
    if any(
        not isinstance(identity[field], str) or not identity[field].strip()
        for field in ("company_id", "taxpayer_id", "database_id")
    ):
        raise _error("backup_content_invalid", "Company identity is incomplete")
    return identity


def _check_identity(
    identity: dict[str, Any],
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
) -> None:
    for field, expected in (
        ("company_id", expected_company_id),
        ("taxpayer_id", expected_taxpayer_id),
        ("database_id", expected_database_id),
    ):
        if expected is not None and identity[field] != expected:
            raise _error("backup_identity_mismatch", f"Company identity mismatch: {field}")


def _company_header(connection, bundle, *, allow_previous: bool, **identity_checks):
    format_value = database_format(
        connection, bundle=bundle, kind="company", allow_previous=allow_previous
    )
    identity = _identity(connection)
    _check_identity(identity, **identity_checks)
    return format_value, identity


def _latest_closed_period(connection):
    latest_ordinal = connection.execute("SELECT max(period) FROM period_close").fetchone()[0]
    # Format 2 fixes the period encoding independently of future active models.
    if latest_ordinal is None:
        return None
    if type(latest_ordinal) is not int or not 0 <= latest_ordinal < 9999 * 12:
        raise _error("backup_content_invalid", "Company close period is invalid")
    year, month = divmod(latest_ordinal, 12)
    return f"{year + 1:04d}-{month + 1:02d}"


def _verify_connection(
    connection: sqlite3.Connection,
    bundle,
    *,
    allow_previous: bool,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
) -> dict[str, Any]:
    format_value, identity = _company_header(
        connection,
        bundle,
        allow_previous=allow_previous,
        expected_company_id=expected_company_id,
        expected_taxpayer_id=expected_taxpayer_id,
        expected_database_id=expected_database_id,
    )
    verifier = bundle.company_verifiers.get(format_value["version"])
    if verifier is None:
        raise _error(
            "backup_schema_unsupported",
            "No content verifier is installed for this company database version",
        )
    try:
        verification = verifier(connection, bundle)
    except KernelError as exc:
        raise _error("backup_content_invalid", f"{exc.code}: {exc}") from exc
    try:
        evidence_count = verification["counts"]["evidence"]
    except (KeyError, TypeError) as exc:
        raise _error(
            "backup_content_invalid", "Company verifier returned an invalid result"
        ) from exc
    if type(evidence_count) is not int or evidence_count < 0:
        raise _error("backup_content_invalid", "Company evidence count is invalid")
    return {
        "identity": identity,
        "database_format": format_value,
        "evidence_count": evidence_count,
        "latest_closed_period": _latest_closed_period(connection),
        "verification": verification,
    }


_RETAINED_HISTORY_ROWS = (
    # Stable identity, original evidence metadata, and typed source identities.
    ("identity", "id,company_id,taxpayer_id,database_id", "id"),
    ("evidence", "digest,media_type,name", "digest"),
    ("subject", "id,kind", "id"),
    ("fact_revision", "id,subject_id,revision,period,digest", "id"),
    ("fact_evidence", "fact_id,evidence_digest", "fact_id,evidence_digest"),
    ("fact_scope", "fact_id,kind,scope_key", "fact_id,scope_key"),
    # The calculation ID seals its input digest, but the saved source edges and
    # selection scope are also retained history, including unused old versions.
    ("calculation", "id,subject_id,fact_id,kind,period,digest,program_version", "id"),
    ("calculation_scope", "calculation_id,kind,scope_key", "calculation_id,scope_key"),
    (
        "dependency_scope",
        "calculation_id,source,kind,scope_key,before_period",
        "calculation_id,source,kind,scope_key,before_period",
    ),
    ("dependency_fact", "calculation_id,fact_id", "calculation_id,fact_id"),
    ("dependency_calculation", "calculation_id,upstream_id", "calculation_id,upstream_id"),
    (
        "disposition",
        "id,subject_id,cause_id,action,calculation_id,explanation",
        "id",
    ),
    ("voucher", "id,number", "id"),
    ("voucher_version", "id,voucher_id,calculation_id,period,reverses_id,total", "id"),
    ("voucher_line", "version_id,line_no,account,debit,credit,cashflow", "version_id,line_no"),
    (
        "calculation_publication",
        "id,sequence,subject_id,previous_publication_id,calculation_id,mode,"
        "posting_period,baseline_calculation_id,voucher_id",
        "id",
    ),
    ("period_close", "period,digest", "period"),
    ("material_close_rule", "period,rule_digest", "period"),
    # Original management records must survive even when still open and not
    # referenced by a period-close manifest.
    (
        "management_revision",
        "id,subject_id,revision,note,payment_period,payment_category",
        "id",
    ),
    ("payee_revision", "id,party_id,revision,name,account,evidence_digest", "id"),
    (
        "material_revision",
        "id,period,category,expected,received,no_business,evidence_digest",
        "id",
    ),
    ("material_item", "inventory_id,evidence_digest", "inventory_id,evidence_digest"),
    ("company_note_revision", "id,revision,text,digest,evidence_digest", "id"),
    (
        "display_profile_revision",
        "id,kind,entity_id,revision,display_name,display_number,purpose,note,"
        "counterparty_id,beneficiary_id,handler_id,source,evidence_digest,digest",
        "id",
    ),
    (
        "period_commentary_revision",
        "id,period,revision,text,context_digest,close_digest,source,evidence_digest,digest",
        "id",
    ),
    ("period_commentary_basis", "commentary_id,contract,basis,content_digest", "commentary_id"),
    ("entity", "id,kind,account_type", "id"),
    (
        "entity_profile_revision",
        "id,entity_id,revision,content,source,evidence_digest,digest",
        "id",
    ),
    (
        "entity_resolution",
        "id,correction_id,source_entity_id,target_entity_id,digest",
        "id",
    ),
    ("identity_correction", "id,plan,digest", "id"),
    (
        "identity_correction_item",
        "id,correction_id,subject_id,action,before_fact_id,after_fact_id,"
        "replacement_subject_id,calculation_id",
        "id",
    ),
    # Append-only decisions and receipts are not replaceable by a fresh valid
    # decision with the same count or by its current projection.
    ("source_change", "sequence,source,target_id,before_ref,after_ref", "sequence"),
    (
        "business_duplicate_check",
        "id,contract,contract_version,proposed_subject_id,proposed_revision,proposed_digest,"
        "candidate_digest,action,result_fact_id,selected_fact_id,manifest,review_basis,"
        "explanation,record_digest,created_at",
        "id",
    ),
    (
        "asset_batch_member",
        "owner_calculation_id,position,asset_id,member_subject_id,member_fact_id,"
        "member_calculation_id,result_digest,summary,line_start,line_count",
        "owner_calculation_id,position",
    ),
    ("request", "id,digest,result", "id"),
    ("audit", "id,request_id,action,payload,created_at", "id"),
    # Worker lifecycle columns are mutable during normal operation, but an
    # offline migration has no authority to rewrite an old delivery receipt or
    # reset a failed attempt. Retry/recovery happens outside this comparison.
    ("jobs", "id,kind,payload,status,attempts,last_error,error_code,result", "id"),
    (
        "security_close_approval",
        "id,catalog_instance_id,company_id,database_id,period,preview_digest,"
        "accounting_epoch,material_epoch,management_epoch,owner_id,session_id,"
        "credential_version,confirmed_at,expires_at,consumed_at",
        "id",
    ),
)


def _retained_history_digest(connection: sqlite3.Connection) -> str:
    """Bind immutable source identities and saved results across a restore migration."""
    hashed = hashlib.sha256()
    for table, columns, order in _RETAINED_HISTORY_ROWS:
        hashed.update(table.encode("ascii") + b"\0")
        for row in connection.execute(f"SELECT {columns} FROM {table} ORDER BY {order}"):
            values = [
                ["blob", bytes(value).hex()] if isinstance(value, bytes) else ["value", value]
                for value in row
            ]
            encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
            hashed.update(len(encoded).to_bytes(8, "big"))
            hashed.update(encoded)
    return hashed.hexdigest()


def _require_retained_history(source, target, source_digest, connection) -> None:
    stable = ("facts", "calculations", "vouchers", "closes", "evidence")
    if (
        any(
            source[key] != target[key]
            for key in ("identity", "evidence_count", "latest_closed_period")
        )
        or any(
            source["verification"]["counts"][key] != target["verification"]["counts"][key]
            for key in stable
        )
        or target["verification"]["status"] != "verified"
        or target["verification"]["limitations"]
        or _retained_history_digest(connection) != source_digest
    ):
        raise _error("backup_content_invalid", "Restore migration changed retained history")


def verify_file(
    path: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    _bundle=None,
    _allow_previous: bool = False,
) -> dict[str, Any]:
    """Verify structure, saved accounting sources and derived data without upgrading."""
    bundle = _resolve_bundle(_bundle)
    database = reject_reparse_path(path)
    if not database.is_file():
        raise _error("backup_content_invalid", "Company database does not exist")
    connection = None
    try:

        def validate(candidate):
            verify_schema(candidate, bundle=bundle, kind="company", allow_previous=_allow_previous)

        connection = connect(database, read_only=True, validator=validate)
        connection.execute("BEGIN")
        return _verify_connection(
            connection,
            bundle,
            allow_previous=_allow_previous,
            expected_company_id=expected_company_id,
            expected_taxpayer_id=expected_taxpayer_id,
            expected_database_id=expected_database_id,
        )
    except BackupError:
        raise
    except KernelError as exc:
        raise _error("backup_schema_unsupported", f"{exc.code}: {exc}") from exc
    except sqlite3.Error as exc:
        raise _error(
            "backup_content_invalid", "The company file is not a valid supported SQLite database"
        ) from exc
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def _publish_new(temporary: Path, destination: Path) -> None:
    """Publish a completed file without ever replacing an existing destination."""
    ensure_private_file(temporary)
    if os.name == "nt":
        # Windows rename fails if the destination exists; POSIX rename replaces it.
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)
        temporary.unlink()
    ensure_private_file(destination)
    _sync_file(destination)
    _sync_directory(destination.parent)


def _sync_file(path: Path) -> None:
    # Windows _commit (used by os.fsync) requires a writable file descriptor.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_ROLLOVER_WAIT_SECONDS = 5.0
_ROLLOVER_WAIT_STEP_SECONDS = 0.1


def _archive_state(path: Path) -> tuple[int, int, int, int, str]:
    assert_private_file(path)
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, _digest(path)


def _same_archive(
    path: Path, expected: tuple[int, int, int, int, str] | None, *, digest: bool
) -> bool:
    if expected is None:
        return not path.exists()
    if not path.exists():
        return False
    assert_private_file(path)
    info = path.stat()
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != expected[:4]:
        return False
    return not digest or _digest(path) == expected[-1]


def _replace_archive(
    source: Path,
    destination: Path,
    *,
    source_state: tuple[int, int, int, int, str],
    destination_state: tuple[int, int, int, int, str] | None,
) -> None:
    """Replace only the verified files; wait briefly for Windows ZIP readers."""
    deadline = time.monotonic() + _ROLLOVER_WAIT_SECONDS
    failures = 0
    while True:
        # A full checksum after the first denial catches content changes. Reopening
        # a just-written ZIP on every 100 ms retry can itself prolong scanner locks.
        check_digest = failures == 1
        if not _same_archive(source, source_state, digest=check_digest) or not _same_archive(
            destination, destination_state, digest=check_digest
        ):
            raise _error(
                "backup_target_changed", "Backup publication files changed during rollover"
            )
        try:
            os.replace(source, destination)
        except PermissionError as exc:
            if os.name != "nt" or exc.winerror not in (5, 32):
                raise
            if time.monotonic() >= deadline:
                raise
            failures += 1
            time.sleep(min(_ROLLOVER_WAIT_STEP_SECONDS, max(0, deadline - time.monotonic())))
            continue
        if _archive_state(destination)[-1] != source_state[-1]:
            raise _error("backup_target_changed", "Published backup digest changed")
        return


def _rollover_paths(output: Path, taxpayer_id: str) -> tuple[Path, Path]:
    return (
        output / f".{taxpayer_id}.rollover.json",
        output / f".{taxpayer_id}.rollover-previous.zip",
    )


def _clear_rollover(marker: Path, pending: Path) -> None:
    if pending.exists():
        assert_private_file(pending)
        pending.unlink()
    if marker.exists():
        assert_private_file(marker)
        marker.unlink()
    _sync_directory(marker.parent)


def _verify_rollover_archive(path, bundle, identity_checks):
    """Verify a retained source ZIP only for an explicitly declared draft rollover.

    Normal restore and verification still require their exact active contract.
    This fallback does not rewrite the archive or relabel its original format.
    """
    try:
        return verify_portable(path, _bundle=bundle, **identity_checks)
    except BackupError as original_error:
        if original_error.code != "backup_schema_unsupported" or bundle.status != "draft":
            raise
        from .offline_development_upgrade import SOURCE_FINGERPRINT, source_bundle

        if (SOURCE_FINGERPRINT, bundle.current("company")["sha256"]) not in (
            bundle.draft_transitions.get("company", ())
        ):
            raise
        historical = source_bundle(bundle)
        # The first verifier already checked archive paths, sizes and manifest
        # shape before rejecting its historical format. Recheck with the exact
        # source contract, including content, evidence and all identity fields.
        try:
            return verify_portable(path, _bundle=historical, **identity_checks)
        except BackupError as source_error:
            if source_error.code == "backup_schema_unsupported":
                raise original_error from source_error
            raise


def _recover_rollover(output: Path, taxpayer_id: str, bundle, identity_checks) -> None:
    marker, pending = _rollover_paths(output, taxpayer_id)
    if not marker.exists():
        if pending.exists():
            raise _error("backup_target_changed", "Backup rollover has an orphaned previous file")
        return
    assert_private_file(marker)
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _error("backup_target_changed", "Backup rollover marker is invalid") from exc
    if (
        not isinstance(state, dict)
        or set(state) != {"version", "old_current", "new_current", "old_previous"}
        or type(state.get("version")) is not int
        or state["version"] != 1
        or state.get("old_current") == state.get("new_current")
        or any(
            not isinstance(state.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", state[key]) is None
            for key in ("old_current", "new_current")
        )
        or (
            state.get("old_previous") is not None
            and (
                not isinstance(state["old_previous"], str)
                or re.fullmatch(r"[0-9a-f]{64}", state["old_previous"]) is None
            )
        )
    ):
        raise _error("backup_target_changed", "Backup rollover marker is invalid")
    final = output / _taxpayer_filename(taxpayer_id)
    previous = output / f"{taxpayer_id}.previous.finance-company.zip"
    if not final.exists():
        raise _error("backup_target_changed", "Current backup disappeared during rollover")
    current_digest = _archive_state(final)[-1]
    previous_digest = _archive_state(previous)[-1] if previous.exists() else None
    if current_digest == state["old_current"]:
        if previous_digest != state["old_previous"]:
            raise _error("backup_target_changed", "Previous backup changed during rollover")
        if pending.exists() and _archive_state(pending)[-1] != state["old_current"]:
            raise _error("backup_target_changed", "Pending previous backup changed")
        _clear_rollover(marker, pending)
        return
    if current_digest != state["new_current"]:
        raise _error("backup_target_changed", "Current backup changed during rollover")
    verify_portable(final, _bundle=bundle, **identity_checks)
    if pending.exists():
        pending_state = _archive_state(pending)
        if pending_state[-1] != state["old_current"] or previous_digest != state["old_previous"]:
            raise _error("backup_target_changed", "Previous backup changed during rollover")
        _verify_rollover_archive(pending, bundle, identity_checks)
        _replace_archive(
            pending,
            previous,
            source_state=pending_state,
            destination_state=_archive_state(previous) if previous.exists() else None,
        )
        _sync_file(previous)
    elif previous_digest != state["old_current"]:
        raise _error("backup_target_changed", "Pending previous backup disappeared")
    _clear_rollover(marker, pending)


def _copy_sqlite_snapshot(source_connection, temporary: Path, *, timeout_seconds: float) -> None:
    destination = None
    try:
        destination = sqlite3.connect(temporary, isolation_level=None)
        destination.execute("PRAGMA synchronous=FULL")
        started = time.monotonic()

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() - started >= timeout_seconds:
                raise _error("backup_content_invalid", "SQLite backup timed out")

        source_connection.backup(destination, pages=256, progress=progress, sleep=0.01)
        if destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise _error(
                "backup_content_invalid", "Could not finalize the standalone database snapshot"
            )
    finally:
        if destination is not None:
            destination.close()


@contextmanager
def _current_file_connection(path: str | Path, bundle):
    database = reject_reparse_path(path)
    if not database.is_file():
        raise _error("backup_content_invalid", "Company database does not exist")
    connection = None
    try:

        def validate(candidate):
            verify_schema(candidate, bundle=bundle, kind="company")

        connection = connect(database, read_only=True, validator=validate)
        connection.execute("BEGIN")
        yield connection
    except BackupError:
        raise
    except KernelError as exc:
        raise _error("backup_schema_unsupported", f"{exc.code}: {exc}") from exc
    except sqlite3.Error as exc:
        raise _error(
            "backup_content_invalid", "The company file is not a valid supported SQLite database"
        ) from exc
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def _file_header(path: str | Path, bundle, **identity_checks) -> dict[str, Any]:
    """Read only current structure and identity; never certify accounting content."""
    with _current_file_connection(path, bundle) as connection:
        format_value, identity = _company_header(
            connection, bundle, allow_previous=False, **identity_checks
        )
        return {"database_format": format_value, "identity": identity}


def _snapshot_manifest_fields(path: Path, bundle, **identity_checks) -> dict[str, Any]:
    """Read manifest facts from the exact SQLite snapshot about to be packaged."""
    with _current_file_connection(path, bundle) as connection:
        format_value, identity = _company_header(
            connection, bundle, allow_previous=False, **identity_checks
        )
        return {
            "database_format": format_value,
            "identity": identity,
            "evidence_count": connection.execute("SELECT count(*) FROM evidence").fetchone()[0],
            "latest_closed_period": _latest_closed_period(connection),
        }


def backup_to_file(
    source: str | Path,
    target: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    timeout_seconds: float = 120.0,
    _bundle=None,
) -> dict[str, Any]:
    """Create and verify one standalone snapshot; the target must not exist."""
    bundle = _resolve_bundle(_bundle)
    require_supported_runtime()
    source_path, target_path = reject_reparse_path(source), reject_reparse_path(target)
    if source_path == target_path or target_path.exists():
        raise _error("backup_target_exists", "Backup target must not exist")
    if not source_path.is_file():
        raise _error("backup_content_invalid", "Source company database does not exist")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    identity_checks = {
        "expected_company_id": expected_company_id,
        "expected_taxpayer_id": expected_taxpayer_id,
        "expected_database_id": expected_database_id,
    }
    with private_temporary_directory(target_path.parent, prefix=".company-backup-") as directory:
        temporary = directory / DATABASE_MEMBER
        create_private_file(temporary)

        def validate(candidate):
            verify_schema(candidate, bundle=bundle, kind="company")

        try:
            source_connection = connect(source_path, read_only=True, validator=validate)
        except KernelError as exc:
            raise _error("backup_schema_unsupported", f"{exc.code}: {exc}") from exc
        try:
            source_connection.execute("BEGIN")
            _verify_connection(source_connection, bundle, allow_previous=False, **identity_checks)
            _copy_sqlite_snapshot(source_connection, temporary, timeout_seconds=timeout_seconds)
        finally:
            source_connection.rollback()
            source_connection.close()
        result = verify_file(temporary, _bundle=bundle, **identity_checks)
        _sync_file(temporary)
        try:
            _publish_new(temporary, target_path)
        except FileExistsError as exc:
            raise _error("backup_target_exists", "Backup target must not exist") from exc
    return {**result, "path": str(target_path), "sha256": _digest(target_path)}


def _capture_portable_snapshot(source: Path, target: Path, bundle, **identity_checks):
    """Copy a private SQLite snapshot; only the packaged bytes certify its content."""
    require_supported_runtime()
    source_path, target_path = reject_reparse_path(source), reject_reparse_path(target)
    if source_path == target_path or target_path.exists():
        raise _error("backup_target_exists", "Backup target must not exist")
    if not source_path.is_file():
        raise _error("backup_content_invalid", "Source company database does not exist")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with private_temporary_directory(target_path.parent, prefix=".company-backup-") as directory:
        temporary = directory / DATABASE_MEMBER
        create_private_file(temporary)

        def validate(candidate):
            verify_schema(candidate, bundle=bundle, kind="company")

        try:
            source_connection = connect(source_path, read_only=True, validator=validate)
        except KernelError as exc:
            raise _error("backup_schema_unsupported", f"{exc.code}: {exc}") from exc
        try:
            source_connection.execute("BEGIN")
            _company_header(source_connection, bundle, allow_previous=False, **identity_checks)
            _copy_sqlite_snapshot(source_connection, temporary, timeout_seconds=120.0)
        finally:
            source_connection.rollback()
            source_connection.close()
        snapshot = _snapshot_manifest_fields(temporary, bundle, **identity_checks)
        _sync_file(temporary)
        try:
            _publish_new(temporary, target_path)
        except FileExistsError as exc:
            raise _error("backup_target_exists", "Backup target must not exist") from exc
    return {**snapshot, "path": str(target_path), "sha256": _digest(target_path)}


def _taxpayer_filename(taxpayer_id: str) -> str:
    if re.fullmatch(r"[0-9A-Z]{18}", taxpayer_id) is None:
        raise _error(
            "backup_identity_mismatch",
            "Portable company filenames require an 18-character taxpayer ID",
        )
    return f"{taxpayer_id}.finance-company.zip"


def _manifest_invalid(message: str) -> BackupError:
    return _error("backup_manifest_invalid", message)


def _validate_manifest(manifest: Any, *, member_size: int) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise _manifest_invalid("Portable manifest must be a JSON object")
    if (
        manifest.get("format") != FORMAT
        or type(manifest.get("format_version")) is not int
        or manifest["format_version"] != FORMAT_VERSION
    ):
        raise _error("backup_format_unsupported", "Unsupported portable backup format")
    if set(manifest) != _MANIFEST_KEYS:
        raise _manifest_invalid("Portable manifest fields do not match the version 2 contract")

    format_value = manifest["database_format"]
    if not valid_database_format(format_value) or format_value["kind"] != "company":
        raise _manifest_invalid("Portable database format metadata is invalid")

    identity = manifest["identity"]
    if not isinstance(identity, dict) or set(identity) != _IDENTITY_KEYS:
        raise _manifest_invalid("Portable company identity is invalid")
    if any(
        not isinstance(identity[field], str) or not identity[field].strip() for field in identity
    ):
        raise _manifest_invalid("Portable company identity is incomplete")
    if re.fullmatch(r"[0-9A-Z]{18}", identity["taxpayer_id"]) is None:
        raise _manifest_invalid("Portable taxpayer identity is invalid")

    if manifest["database_member"] != DATABASE_MEMBER:
        raise _manifest_invalid("Unexpected portable database path")
    if (
        type(manifest["database_bytes"]) is not int
        or manifest["database_bytes"] <= 0
        or manifest["database_bytes"] != member_size
    ):
        raise _manifest_invalid("Portable database size is invalid")
    if (
        not isinstance(manifest["database_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", manifest["database_sha256"]) is None
    ):
        raise _manifest_invalid("Portable database digest is invalid")
    if type(manifest["evidence_count"]) is not int or manifest["evidence_count"] < 0:
        raise _manifest_invalid("Portable evidence count is invalid")
    latest = manifest["latest_closed_period"]
    if latest is not None and (
        not isinstance(latest, str) or re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", latest) is None
    ):
        raise _manifest_invalid("Portable latest closed period is invalid")
    try:
        created = datetime.fromisoformat(manifest["created_at"])
    except (TypeError, ValueError) as exc:
        raise _manifest_invalid("Portable creation time is invalid") from exc
    if created.tzinfo is None or created.utcoffset() != UTC.utcoffset(created):
        raise _manifest_invalid("Portable creation time must be UTC")
    request_id = manifest["request_id"]
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise _manifest_invalid("Portable request identity is invalid")
    return manifest


def portable_result_verified(result: Any) -> bool:
    """Validate the saved completion proof, without checking today's file availability.

    The manifest digest identifies the SQLite member, not the ZIP itself. A
    portable job therefore does not use the export jobs' top-level sha256 field.
    """
    if not isinstance(result, dict) or not isinstance(result.get("path"), str):
        return False
    if not result["path"].strip():
        return False
    manifest = result.get("manifest")
    try:
        _validate_manifest(manifest, member_size=manifest["database_bytes"])
    except (BackupError, KeyError, TypeError, ValueError):
        return False
    for field in ("identity", "database_format", "evidence_count", "latest_closed_period"):
        if field not in result or result[field] != manifest[field]:
            return False
    if type(result["evidence_count"]) is not int:
        return False
    if not valid_database_format(result["database_format"]):
        return False
    verification = result.get("verification")
    if not isinstance(verification, dict):
        return False
    if verification.get("status") != "verified" or verification.get("limitations") != []:
        return False
    if verification.get("coverage") != {
        "sources": "verified",
        "historical_adoption": "verified",
        "projections": "verified",
        "read_indexes": "verified",
    }:
        return False
    counts = verification.get("counts")
    if not isinstance(counts, dict) or set(counts) != {
        "facts",
        "calculations",
        "vouchers",
        "closes",
        "evidence",
    }:
        return False
    if any(type(value) is not int or value < 0 for value in counts.values()):
        return False
    return counts["evidence"] == result["evidence_count"]


def _assert_supported_format(format_value: dict[str, Any], bundle) -> None:
    if format_value["family"] != bundle.family:
        raise _error("backup_format_unsupported", "Portable backup belongs to another family")
    if format_value["status"] != bundle.status:
        raise _error(
            "backup_schema_unsupported", "Portable backup has an incompatible release status"
        )
    version = format_value["version"]
    current = bundle.current_versions["company"]
    if bundle.status == "draft" and version != current:
        raise _error(
            "backup_schema_unsupported", "Draft backups require the exact active draft contract"
        )
    if bundle.status == "released" and version > current:
        raise _error("backup_schema_unsupported", "Portable backup is newer than this package")
    contract = bundle.contracts["company"].get(version)
    if (
        contract is None
        or contract["status"] != format_value["status"]
        or contract["sha256"] != format_value["fingerprint"]
        or version not in bundle.company_verifiers
    ):
        raise _error(
            "backup_schema_unsupported", "Portable backup schema is not supported by this package"
        )


def _read_manifest(archive: zipfile.ZipFile, maximum: int, bundle) -> dict[str, Any]:
    entries = archive.infolist()
    if len(entries) != 2 or {entry.filename for entry in entries} != {
        DATABASE_MEMBER,
        MANIFEST_MEMBER,
    }:
        raise _manifest_invalid(
            "Portable archive must contain only manifest.json and company.sqlite"
        )
    for entry in entries:
        if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
            raise _manifest_invalid("Portable archive contains an unsupported member")
    if archive.getinfo(MANIFEST_MEMBER).file_size > MAX_MANIFEST_BYTES:
        raise _manifest_invalid("Portable manifest is too large")
    if archive.getinfo(DATABASE_MEMBER).file_size > maximum:
        raise _manifest_invalid("Portable database exceeds the configured size limit")
    try:
        manifest = json.loads(archive.read(MANIFEST_MEMBER))
    except (ValueError, UnicodeError) as exc:
        raise _manifest_invalid("Portable manifest is invalid JSON") from exc
    result = _validate_manifest(manifest, member_size=archive.getinfo(DATABASE_MEMBER).file_size)
    _assert_supported_format(result["database_format"], bundle)
    return result


def _unpack_verified(
    path: Path,
    directory: Path,
    *,
    max_database_bytes: int,
    bundle,
    upgrade_to_current: bool = False,
    **identity_checks: Any,
) -> tuple[dict[str, Any], Path]:
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = _read_manifest(archive, max_database_bytes, bundle)
            database = directory / DATABASE_MEMBER
            actual_size = 0
            create_private_file(database)
            with archive.open(DATABASE_MEMBER) as source, database.open("wb") as destination:
                while chunk := source.read(1024 * 1024):
                    actual_size += len(chunk)
                    if actual_size > max_database_bytes:
                        raise _manifest_invalid(
                            "Portable database exceeds the configured size limit"
                        )
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        if _digest(database) != manifest["database_sha256"]:
            raise _error("backup_content_invalid", "Portable database digest mismatch")
        result = verify_file(
            database,
            _bundle=bundle,
            _allow_previous=True,
            **identity_checks,
        )
        if any(
            manifest.get(field) != result[field]
            for field in (
                "database_format",
                "identity",
                "evidence_count",
                "latest_closed_period",
            )
        ):
            raise _error(
                "backup_content_invalid",
                "Portable manifest does not match the contained company database",
            )
        _taxpayer_filename(result["identity"]["taxpayer_id"])
        source_result = result
        source_format = result["database_format"]
        if upgrade_to_current and source_format["version"] != bundle.current_versions["company"]:

            def validate_source(connection):
                verify_schema(
                    connection,
                    bundle=bundle,
                    kind="company",
                    allow_previous=True,
                )

            connection = connect(database, validator=validate_source)
            try:
                retained_digest = _retained_history_digest(connection)

                def verify_source(candidate):
                    checked = _verify_connection(
                        candidate, bundle, allow_previous=True, **identity_checks
                    )
                    _require_retained_history(result, checked, retained_digest, candidate)

                def verify_target(candidate):
                    checked = _verify_connection(
                        candidate, bundle, allow_previous=False, **identity_checks
                    )
                    _require_retained_history(result, checked, retained_digest, candidate)

                upgrade(
                    connection,
                    bundle=bundle,
                    kind="company",
                    verify_source_content=verify_source,
                    verify_target=verify_target,
                )
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
                    raise _error(
                        "backup_content_invalid",
                        "Restored company database could not be finalized",
                    )
            except KernelError as exc:
                raise _error("backup_schema_unsupported", f"{exc.code}: {exc}") from exc
            finally:
                connection.close()
            result = verify_file(database, _bundle=bundle, **identity_checks)
            with closing(connect(database, read_only=True)) as connection:
                connection.execute("BEGIN")
                _require_retained_history(source_result, result, retained_digest, connection)
            result["source_database_format"] = source_format
            _sync_file(database)
        return {**result, "manifest": manifest, "path": str(path)}, database
    except PrivatePathError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise _error("backup_content_invalid", "Portable archive cannot be read") from exc


def verify_portable(
    path: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    max_database_bytes: int = DEFAULT_MAX_DATABASE_BYTES,
    _bundle=None,
) -> dict[str, Any]:
    """Verify member paths, archive hashes, company identity and database contents."""
    bundle = _resolve_bundle(_bundle)
    require_supported_runtime()
    with private_temporary_directory(
        Path(tempfile.gettempdir()), prefix="company-verify-"
    ) as directory:
        result, _database = _unpack_verified(
            reject_reparse_path(path),
            directory,
            max_database_bytes=max_database_bytes,
            bundle=bundle,
            expected_company_id=expected_company_id,
            expected_taxpayer_id=expected_taxpayer_id,
            expected_database_id=expected_database_id,
        )
        return result


def create_portable(
    source: str | Path,
    directory: str | Path,
    *,
    rollover: bool = False,
    request_id: str | None = None,
    _bundle=None,
) -> dict[str, Any]:
    """Publish a verified company ZIP; rollover retains a verified previous ZIP.

    request_id makes publication retryable if the process exits after publishing
    but before its durable job can record success.
    """
    bundle = _resolve_bundle(_bundle)
    output = reject_reparse_path(directory)
    output.mkdir(parents=True, exist_ok=True)
    source_result = _file_header(source, bundle)
    taxpayer_id = source_result["identity"]["taxpayer_id"]
    _taxpayer_filename(taxpayer_id)
    with _worker_lock(output / f".{taxpayer_id}.publication") as acquired:
        if not acquired:
            raise _error("backup_target_changed", "Backup publication is already running")
        return _create_portable_unlocked(
            source,
            output,
            rollover=rollover,
            request_id=request_id,
            bundle=bundle,
            source_result=source_result,
        )


def _create_portable_unlocked(
    source: str | Path,
    output: Path,
    *,
    rollover: bool,
    request_id: str | None,
    bundle,
    source_result: dict[str, Any],
) -> dict[str, Any]:
    identity = source_result["identity"]
    identity_checks = {
        f"expected_{key}": identity[key] for key in ("company_id", "taxpayer_id", "database_id")
    }
    checks = {**identity_checks, "_bundle": bundle}
    final = output / _taxpayer_filename(identity["taxpayer_id"])
    _recover_rollover(output, identity["taxpayer_id"], bundle, identity_checks)
    existing = None
    if final.exists():
        ensure_private_file(final)
        existing = _verify_rollover_archive(final, bundle, identity_checks)
    if existing is not None:
        if (
            request_id is not None
            and existing["manifest"].get("request_id") == request_id
            and existing["database_format"] == source_result["database_format"]
        ):
            # Replay publishes no new snapshot. Retain the prior requirement
            # that the current live source itself passes full verification.
            verify_file(source, _bundle=bundle, **identity_checks)
            return {**existing, "idempotent_replay": True}
        if not rollover:
            raise _error(
                "backup_target_exists",
                "Current company backup already exists; rollover was not requested",
            )
    with private_temporary_directory(output, prefix=".company-package-") as staging:
        snapshot = _capture_portable_snapshot(
            source, staging / DATABASE_MEMBER, bundle, **identity_checks
        )
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "database_format": snapshot["database_format"],
            "identity": snapshot["identity"],
            "evidence_count": snapshot["evidence_count"],
            "latest_closed_period": snapshot["latest_closed_period"],
            "database_member": DATABASE_MEMBER,
            "database_bytes": (staging / DATABASE_MEMBER).stat().st_size,
            "database_sha256": snapshot["sha256"],
            "created_at": datetime.now(UTC).isoformat(),
            "request_id": request_id,
        }
        candidate = staging / "package.zip"
        with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_MEMBER, _json(manifest))
            archive.write(staging / DATABASE_MEMBER, DATABASE_MEMBER)
        adopt_private_file(candidate)
        verified = verify_portable(candidate, **checks)
        _sync_file(candidate)
        if existing is not None:
            previous = output / f"{identity['taxpayer_id']}.previous.finance-company.zip"
            marker, pending = _rollover_paths(output, identity["taxpayer_id"])
            previous_stage = staging / "previous.zip"
            candidate_state = _archive_state(candidate)
            final_state = _archive_state(final)
            previous_state = _archive_state(previous) if previous.exists() else None
            marker_stage = staging / "rollover.json"
            create_private_file(marker_stage)
            marker_stage.write_text(
                _json(
                    {
                        "version": 1,
                        "old_current": final_state[-1],
                        "new_current": candidate_state[-1],
                        "old_previous": previous_state[-1] if previous_state else None,
                    }
                ),
                encoding="utf-8",
            )
            _sync_file(marker_stage)
            _publish_new(marker_stage, marker)
            shutil.copyfile(final, previous_stage)
            adopt_private_file(previous_stage)
            _sync_file(previous_stage)
            pending_state = _archive_state(previous_stage)
            if pending_state[-1] != final_state[-1]:
                raise _error("backup_target_changed", "Current backup changed during rollover")
            _publish_new(previous_stage, pending)
            pending_state = _archive_state(pending)
            _replace_archive(
                candidate,
                final,
                source_state=candidate_state,
                destination_state=final_state,
            )
            _sync_file(final)
            _sync_directory(output)
            _replace_archive(
                pending,
                previous,
                source_state=pending_state,
                destination_state=previous_state,
            )
            _sync_file(previous)
            _sync_directory(output)
            _clear_rollover(marker, pending)
        else:
            try:
                _publish_new(candidate, final)
            except FileExistsError as exc:
                raise _error(
                    "backup_target_exists", "Company backup was published concurrently"
                ) from exc
    return {**verified, "path": str(final), "idempotent_replay": False}


def restore_portable(
    archive: str | Path,
    target: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    max_database_bytes: int = DEFAULT_MAX_DATABASE_BYTES,
    _bundle=None,
) -> dict[str, Any]:
    """Restore into an absent company file, never merge or replace an old file."""
    bundle = _resolve_bundle(_bundle)
    require_supported_runtime()
    destination = reject_reparse_path(target)
    if destination.exists() or any(
        reject_reparse_path(Path(str(destination) + suffix)).exists()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise _error(
            "restore_target_exists", "Restore target and its SQLite sidecars must not exist"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with private_temporary_directory(destination.parent, prefix=".company-restore-") as temporary:
        result, database = _unpack_verified(
            reject_reparse_path(archive),
            temporary,
            max_database_bytes=max_database_bytes,
            bundle=bundle,
            upgrade_to_current=True,
            expected_company_id=expected_company_id,
            expected_taxpayer_id=expected_taxpayer_id,
            expected_database_id=expected_database_id,
        )
        try:
            _publish_new(database, destination)
        except FileExistsError as exc:
            raise _error("restore_target_exists", "Restore target must not exist") from exc
    return {**result, "path": str(destination)}


@contextmanager
def _worker_lock(database: Path):
    """Keep one backup worker per file; OS locks automatically release on crash."""
    lock_path = Path(str(database) + ".backup-worker.lock")
    ensure_private_file(lock_path, create=True)
    with lock_path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run_backup_jobs(database: str | Path, *, limit: int = 1, _bundle=None) -> list[dict[str, Any]]:
    """Run pending/failed jobs and recover interrupted running backup jobs.

    The separate OS worker lock covers the slow packaging work while SQLite's
    write transaction covers only claiming a job and recording its result.
    Backup failure changes no accounting state and never undoes a closed month.
    Each job is attempted at most once in this invocation.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be positive")
    bundle = _resolve_bundle(_bundle)
    path = reject_reparse_path(database)
    if not path.is_file():
        raise _error("backup_content_invalid", "Source company database does not exist")
    outcomes: list[dict[str, Any]] = []
    attempted: list[str] = []
    with _worker_lock(path) as acquired:
        if not acquired:
            return outcomes

        def validate(candidate):
            verify_schema(candidate, bundle=bundle, kind="company")

        connection = connect(path, validator=validate)
        try:
            verify_schema(connection, bundle=bundle, kind="company")
            for _ in range(limit):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    exclude = (
                        f"AND id NOT IN ({','.join('?' for _ in attempted)})" if attempted else ""
                    )
                    job = connection.execute(
                        "SELECT id,payload FROM jobs WHERE kind='portable_backup' "
                        "AND status IN ('pending','running','failed') AND attempts<3 "
                        f"{exclude} ORDER BY attempts,id LIMIT 1",
                        attempted,
                    ).fetchone()
                    if job is None:
                        connection.rollback()
                        break
                    connection.execute(
                        "UPDATE jobs SET status='running',attempts=attempts+1,"
                        "last_error=NULL,error_code=NULL "
                        "WHERE id=?",
                        (job["id"],),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                attempted.append(job["id"])
                try:
                    payload = json.loads(job["payload"])
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("directory"), str
                    ):
                        raise _error(
                            "backup_manifest_invalid",
                            "Backup job requires an output directory",
                        )
                    rollover = payload.get("rollover", False)
                    if type(rollover) is not bool:
                        raise _error(
                            "backup_manifest_invalid", "Backup job rollover must be boolean"
                        )
                    result = create_portable(
                        path,
                        payload["directory"],
                        rollover=rollover,
                        request_id=job["id"],
                        _bundle=bundle,
                    )
                    status, error, error_code = "succeeded", None, None
                except Exception as exc:
                    from .diagnostics import job_error_code

                    result = None
                    status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
                    error_code = job_error_code(exc)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "UPDATE jobs SET status=?,last_error=?,error_code=?,result=? WHERE id=?",
                        (
                            status,
                            error,
                            error_code,
                            _json(result) if result is not None else None,
                            job["id"],
                        ),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                outcomes.append(
                    {"id": job["id"], "status": status, "result": result, "error_code": error_code}
                )
        finally:
            connection.close()
    return outcomes
