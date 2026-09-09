"""Deterministic, typed full-system export and empty-database replay utility."""

from __future__ import annotations

import argparse
import base64
import calendar
import csv
import hashlib
import inspect
import json
import re
import shutil
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, get_type_hints

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel
from sqlalchemy import create_engine, func, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from alembic import command

from .coa import get_business_class_template, seed_organization
from .company_router import grant_runtime_database_access
from .config import get_settings
from .models import (
    Account,
    CatalogMetadata,
    CompanyRegistry,
    Evidence,
    Organization,
    OrganizationDatabaseMetadata,
    OrganizationProfileVersion,
    OwnerAccount,
)

_ROOT = Path(__file__).resolve().parents[2]
_FORMAT_VERSION = "ai-accounting-composition-replay-v3"
_BUSINESS_REVISION = "0001_business_baseline_v3"
_CATALOG_REVISION = "0001_catalog_baseline_v2"


def _current_schema_revision(*, catalog: bool) -> str:
    config = Config(str(_ROOT / ("catalog_alembic.ini" if catalog else "alembic.ini")))
    config.set_main_option(
        "script_location", str(_ROOT / ("catalog_alembic" if catalog else "alembic"))
    )
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise ReplayError("REPLAY_SCHEMA_HEAD_MISSING")
    return head


_MANIFEST = "MANIFEST.sha256"
_STATE_VERSION = "ai-accounting-composition-replay-state-v3"
_HEX_64 = frozenset("0123456789abcdef")
_UUID_TEXT = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SENSITIVE_TABLES = frozenset(
    {"owner_accounts", "owner_recovery_codes", "owner_sessions", "identity_audit_events"}
)
_TECHNICAL_UUID_KEYS = frozenset(
    {
        "id",
        "event_id",
        "voucher_id",
        "batch_id",
        "payroll_batch_id",
        "payout_run_id",
        "execution_attribution_id",
        "owner_approval_id",
        "confirmation_id",
        "action_id",
    }
)
_DIRECT_SPECIALIZED_TOOLS = {
    "fixed_asset_acquisition": "finance_acquire_fixed_asset",
    "fixed_asset_activation": "finance_activate_fixed_asset",
    "fixed_asset_disposal": "finance_dispose_fixed_asset",
    "intangible_asset_acquisition": "finance_acquire_intangible_asset",
    "intangible_asset_retirement": "finance_retire_intangible_asset",
    "borrowing_drawdown": "finance_draw_borrowing",
}
_PREVIEW_CONFIRM_WORKFLOWS = {
    "tax_relief": ("finance_calculate_tax_period", "finance_confirm_tax_period"),
    "payroll_accrual": ("finance_preview_payroll", "finance_confirm_payroll"),
    "labor_remuneration_accrual": (
        "finance_preview_labor_remuneration_batch",
        "finance_confirm_labor_remuneration_batch",
    ),
    "fixed_asset_depreciation": (
        "finance_preview_fixed_asset_depreciation_batch",
        "finance_confirm_fixed_asset_depreciation_batch",
    ),
    "intangible_asset_amortization": (
        "finance_preview_intangible_asset_amortization",
        "finance_confirm_intangible_asset_amortization",
    ),
    "borrowing_interest_accrual": (
        "finance_preview_borrowing_interest",
        "finance_confirm_borrowing_interest",
    ),
}


class ReplayError(ValueError):
    """Stable CLI failure without database URLs, SQL text, or private content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _semantic_replay_key(source_key: str) -> str:
    """Keep business-readable idempotency while removing embedded source UUIDs."""

    return _UUID_TEXT.sub("source-id", source_key)


def _semantic_file_name(source_name: str) -> str:
    return _UUID_TEXT.sub("source-id", source_name)


def _sanitize_evidence_metadata(value: Any, *, key: str | None = None) -> Any:
    """Retain descriptive metadata but remove source-database technical identities."""

    if isinstance(value, Mapping):
        return {
            str(child_key): _sanitize_evidence_metadata(child, key=str(child_key))
            for child_key, child in value.items()
            if not str(child_key).lower().endswith("_id")
            and str(child_key).lower()
            not in {"session", "session_token", "password_hash", "recovery_code"}
        }
    if isinstance(value, list):
        return [_sanitize_evidence_metadata(item, key=key) for item in value]
    if isinstance(value, str) and _UUID_TEXT.fullmatch(value):
        return None
    return _jsonable(value)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for value in values:
            stream.write(
                json.dumps(
                    _jsonable(value),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayError("REPLAY_PACKAGE_JSON_INVALID") from exc


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_package_file(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative:
        raise ReplayError("REPLAY_PACKAGE_PATH_INVALID")
    candidate = (root / relative).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReplayError("REPLAY_PACKAGE_PATH_INVALID") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ReplayError("REPLAY_PACKAGE_FILE_INVALID")
    return candidate


def _package_files(root: Path) -> list[Path]:
    files: list[Path] = []
    root_manifest = root / _MANIFEST
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReplayError("REPLAY_PACKAGE_SYMLINK_FORBIDDEN")
        if path.is_file() and path != root_manifest:
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _seal_package(root: Path) -> str:
    lines = []
    for path in _package_files(root):
        digest, _ = _sha256_file(path)
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    manifest = root / _MANIFEST
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return _sha256_file(manifest)[0]


def _parse_manifest(root: Path) -> dict[str, str]:
    manifest = _safe_package_file(root, _MANIFEST)
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if len(line) < 67 or line[64:66] != "  ":
            raise ReplayError("REPLAY_PACKAGE_MANIFEST_INVALID")
        digest = line[:64]
        relative = line[66:]
        if set(digest) - _HEX_64 or relative in expected:
            raise ReplayError("REPLAY_PACKAGE_MANIFEST_INVALID")
        _safe_package_file(root, relative)
        expected[relative] = digest
    actual = {
        path.relative_to(root).as_posix(): _sha256_file(path)[0] for path in _package_files(root)
    }
    if actual != expected:
        raise ReplayError("REPLAY_PACKAGE_MANIFEST_MISMATCH")
    return expected


_REFERENCE_FIELDS: dict[str, frozenset[str]] = {
    "operation_result": frozenset({"operation_key", "field"}),
    "evidence": frozenset({"sha256"}),
    "employee": frozenset({"employee_code"}),
    "bank_transaction": frozenset({"bank_account_code", "source_fingerprint"}),
    "bank_transaction_reference": frozenset({"bank_account_code", "source_fingerprint"}),
    "counterparty": frozenset({"kind", "name"}),
    "event": frozenset({"replay_key"}),
    "component": frozenset({"source_replay_key", "component_key"}),
    "component_open_item": frozenset({"source_replay_key", "component_key", "open_item_key"}),
    "component_income_tax": frozenset({"source_replay_key", "component_key", "result_kind"}),
    "prepared_line_key": frozenset({"operation_key", "identity_code", "line_kind"}),
    "open_item": frozenset(
        {
            "source_replay_key",
            "item_type",
            "original_amount_fen",
            "counterparty_kind",
            "counterparty_name",
        }
    ),
    "asset": frozenset({"code"}),
    "intangible": frozenset({"code"}),
    "labor_person": frozenset({"code"}),
    "borrowing": frozenset({"code"}),
    "period": frozenset({"period_month"}),
    "bank_import_actions_for_period": frozenset(
        {"bank_account_code", "coverage_start_date", "coverage_end_date"}
    ),
    "voucher_line": frozenset(
        {
            "source_replay_key",
            "component_key",
            "account_code",
            "debit_fen",
            "credit_fen",
        }
    ),
}


def _walk_package_values(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk_package_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_package_values(child)


def _validate_composed_replay_request(request: Mapping[str, Any]) -> None:
    """Validate current typed facts offline; stable references stand in for new IDs."""
    from pydantic import ValidationError

    from .component_schemas import RecordEventRequest

    placeholder = "11111111-1111-4111-8111-111111111111"

    def materialize_shape(value: Any) -> Any:
        if value == "${ORG_ID}":
            return placeholder
        if isinstance(value, list):
            return [materialize_shape(item) for item in value]
        if isinstance(value, Mapping):
            if value.get("$ref") == "bank_transaction_reference":
                return {"id": placeholder, "fingerprint": "0" * 64}
            if value.get("$ref") == "operation_result" and value.get("field") == (
                "calculation_hash"
            ):
                return "0" * 64
            if "$ref" in value:
                return placeholder
            return {key: materialize_shape(item) for key, item in value.items()}
        return value

    try:
        RecordEventRequest.model_validate(materialize_shape(request))
    except ValidationError as exc:
        raise ReplayError("REPLAY_COMPONENT_REQUEST_INVALID") from exc


def _verify_operation_references(
    operations: Sequence[Mapping[str, Any]],
    *,
    evidence_hashes: set[str],
    org_id: str,
) -> None:
    keys = [str(operation.get("key", "")) for operation in operations]
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise ReplayError("REPLAY_PACKAGE_OPERATION_KEY_INVALID")
    positions = {key: index for index, key in enumerate(keys)}
    allowed_kinds = {
        "tool",
        "preview_confirm",
        "evidence",
        "bank_import",
        "owner_control",
        "period_close",
        "composite_event",
    }
    for index, operation in enumerate(operations):
        if operation.get("kind") not in allowed_kinds:
            raise ReplayError("REPLAY_PACKAGE_OPERATION_KIND_INVALID")
        if operation.get("tool") == "finance_record_event" or operation.get("kind") == (
            "composite_event"
        ):
            request = operation.get("request")
            if not isinstance(request, Mapping):
                raise ReplayError("REPLAY_COMPONENT_REQUEST_INVALID")
            _validate_composed_replay_request(request)
        preparation_keys = {
            str(item.get("key"))
            for item in operation.get("preparations", [])
            if isinstance(item, Mapping) and item.get("key")
        }
        for preparation_key in preparation_keys:
            if preparation_key in positions:
                raise ReplayError("REPLAY_PACKAGE_OPERATION_KEY_INVALID")
            positions[preparation_key] = index
        preparation_order = {
            str(item["key"]): preparation_index
            for preparation_index, item in enumerate(operation.get("preparations", []))
            if isinstance(item, Mapping) and item.get("key")
        }
        for preparation_index, preparation in enumerate(operation.get("preparations", [])):
            if not isinstance(preparation, Mapping):
                raise ReplayError("REPLAY_PACKAGE_OPERATION_KIND_INVALID")
            for value in _walk_package_values(preparation.get("preview_request", {})):
                if not isinstance(value, Mapping) or value.get("$ref") != "operation_result":
                    continue
                target_preparation = preparation_order.get(str(value.get("operation_key")))
                if target_preparation is not None and target_preparation >= preparation_index:
                    raise ReplayError("REPLAY_PACKAGE_OPERATION_REFERENCE_MISSING")
        for value in _walk_package_values(operation):
            if isinstance(value, str):
                matches = _UUID_TEXT.findall(value)
                if any(match.lower() != org_id.lower() for match in matches):
                    raise ReplayError("REPLAY_PACKAGE_TECHNICAL_UUID_FORBIDDEN")
            if not isinstance(value, Mapping) or "$ref" not in value:
                continue
            ref_type = str(value["$ref"])
            required = _REFERENCE_FIELDS.get(ref_type)
            if required is None:
                raise ReplayError("REPLAY_PACKAGE_REFERENCE_TYPE_INVALID")
            if not required.issubset(value):
                raise ReplayError("REPLAY_PACKAGE_REFERENCE_FIELDS_MISSING")
            if ref_type == "evidence" and str(value["sha256"]) not in evidence_hashes:
                raise ReplayError("REPLAY_PACKAGE_EVIDENCE_REFERENCE_MISSING")
            target_key = value.get("operation_key")
            if target_key is None:
                target_key = value.get("replay_key") or value.get("source_replay_key")
            if target_key is not None:
                target_position = positions.get(str(target_key))
                if (
                    target_position is None
                    or target_position > index
                    or (target_position == index and str(target_key) not in preparation_keys)
                ):
                    raise ReplayError("REPLAY_PACKAGE_OPERATION_REFERENCE_MISSING")


def verify_package(package: Path) -> dict[str, Any]:
    root = package.resolve(strict=True)
    if not root.is_dir():
        raise ReplayError("REPLAY_PACKAGE_DIRECTORY_REQUIRED")
    files = _parse_manifest(root)
    system = _load_json(_safe_package_file(root, "system.json"))
    if system.get("format_version") != _FORMAT_VERSION:
        raise ReplayError("REPLAY_PACKAGE_FORMAT_UNSUPPORTED")
    companies = system.get("companies")
    if not isinstance(companies, list) or not companies:
        raise ReplayError("REPLAY_PACKAGE_COMPANIES_REQUIRED")
    seen_orgs: set[str] = set()
    evidence_count = 0
    evidence_bytes = 0
    event_count = 0
    for company in companies:
        org_id = str(uuid.UUID(str(company["org_id"])))
        if org_id in seen_orgs:
            raise ReplayError("REPLAY_PACKAGE_ORGANIZATION_DUPLICATE")
        seen_orgs.add(org_id)
        directory = str(company["directory"])
        descriptor = _load_json(_safe_package_file(root, f"{directory}/company.json"))
        if (
            descriptor.get("format_version") != _FORMAT_VERSION
            or descriptor.get("org_id") != org_id
        ):
            raise ReplayError("REPLAY_PACKAGE_ORGANIZATION_MISMATCH")
        evidence_rows = _read_jsonl(
            _safe_package_file(root, f"{directory}/evidence-manifest.jsonl")
        )
        evidence_hashes: set[str] = set()
        for row in evidence_rows:
            digest = str(row.get("sha256", ""))
            if len(digest) != 64 or set(digest) - _HEX_64 or digest in evidence_hashes:
                raise ReplayError("REPLAY_PACKAGE_EVIDENCE_HASH_INVALID")
            evidence_hashes.add(digest)
            evidence_path = _safe_package_file(root, f"{directory}/{row['relative_path']}")
            actual_digest, actual_size = _sha256_file(evidence_path)
            if actual_digest != digest or actual_size != int(row["size_bytes"]):
                raise ReplayError("REPLAY_PACKAGE_EVIDENCE_MISMATCH")
            evidence_count += 1
            evidence_bytes += actual_size
        events = _read_jsonl(_safe_package_file(root, f"{directory}/typed-events.jsonl"))
        keys = [str(item.get("replay_key", "")) for item in events]
        if any(not item for item in keys) or len(keys) != len(set(keys)):
            raise ReplayError("REPLAY_PACKAGE_STABLE_REFERENCE_INVALID")
        event_count += len(events)
        operations = _read_jsonl(_safe_package_file(root, f"{directory}/operations.jsonl"))
        if int(descriptor.get("operation_count", -1)) != len(operations):
            raise ReplayError("REPLAY_PACKAGE_OPERATION_COUNT_MISMATCH")
        operation_keys = {str(item.get("key", "")) for item in operations}
        if not set(keys).issubset(operation_keys):
            raise ReplayError("REPLAY_PACKAGE_EVENT_OPERATION_MISSING")
        _verify_operation_references(
            operations,
            evidence_hashes=evidence_hashes,
            org_id=org_id,
        )
        expected = descriptor.get("checkpoints", {})
        if int(expected.get("effective_event_count", -1)) != len(events):
            raise ReplayError("REPLAY_PACKAGE_EVENT_COUNT_MISMATCH")
        if int(expected.get("evidence_count", -1)) != len(evidence_rows):
            raise ReplayError("REPLAY_PACKAGE_EVIDENCE_COUNT_MISMATCH")
    expected_system = system.get("checkpoints", {})
    if int(expected_system.get("effective_event_count", -1)) != event_count:
        raise ReplayError("REPLAY_PACKAGE_SYSTEM_COUNT_MISMATCH")
    return {
        "status": "verified",
        "format_version": _FORMAT_VERSION,
        "company_count": len(companies),
        "effective_event_count": event_count,
        "evidence_count": evidence_count,
        "evidence_bytes": evidence_bytes,
        "file_count": len(files),
        "manifest_sha256": _sha256_file(root / _MANIFEST)[0],
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for _line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError
                rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReplayError(f"REPLAY_PACKAGE_JSONL_INVALID:{path.name}") from exc
    return rows


def _database_url_for_name(base_url: str | URL, database_name: str) -> URL:
    return make_url(base_url).set(database=database_name)


def _migration_url_for_runtime_database(migration_base: str | URL, runtime_url: URL) -> URL:
    if not runtime_url.database:
        raise ReplayError("REPLAY_TARGET_DATABASE_NAME_INVALID")
    return _database_url_for_name(migration_base, runtime_url.database)


def _render_url(url: str | URL) -> str:
    return make_url(url).render_as_string(hide_password=False)


def _query_rows(session: Session, sql: str, **parameters: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in session.execute(text(sql), parameters).mappings()]


def _effective_events(session: Session, org_id: uuid.UUID) -> list[dict[str, Any]]:
    return _query_rows(
        session,
        """
        SELECT id, idempotency_key, event_type, description, facts,
               business_date, posting_date, created_at
          FROM business_events
         WHERE org_id = :org_id
           AND status = 'posted'
           AND event_type <> 'reversal'
           AND reversed_by_event_id IS NULL
         ORDER BY created_at, id
        """,
        org_id=org_id,
    )


def _stable_maps(session: Session, org_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    maps: dict[str, dict[str, Any]] = {
        "evidence": {},
        "bank": {},
        "employee": {},
        "counterparty": {},
        "event": {},
        "component": {},
        "open_item": {},
        "asset": {},
        "intangible": {},
        "labor_person": {},
        "borrowing": {},
        "income_tax": {},
    }
    for row in _query_rows(
        session,
        "SELECT id, sha256 FROM evidence WHERE org_id=:org_id",
        org_id=org_id,
    ):
        maps["evidence"][str(row["id"])] = {"$ref": "evidence", "sha256": row["sha256"]}
    for row in _query_rows(
        session,
        "SELECT id, bank_account_code, external_id, fingerprint "
        "FROM bank_transactions WHERE org_id=:org_id",
        org_id=org_id,
    ):
        maps["bank"][str(row["id"])] = {
            "$ref": "bank_transaction",
            "bank_account_code": row["bank_account_code"],
            "external_id": row["external_id"],
            "source_fingerprint": row["fingerprint"],
        }
    for row in _query_rows(
        session,
        "SELECT id, employee_code FROM employees WHERE org_id=:org_id",
        org_id=org_id,
    ):
        maps["employee"][str(row["id"])] = {
            "$ref": "employee",
            "employee_code": row["employee_code"],
        }
    for row in _query_rows(
        session,
        "SELECT id, kind, name, external_ref FROM counterparties WHERE org_id=:org_id",
        org_id=org_id,
    ):
        maps["counterparty"][str(row["id"])] = {
            "$ref": "counterparty",
            "kind": row["kind"],
            "name": row["name"],
            "external_ref": row["external_ref"],
        }
    for row in _effective_events(session, org_id):
        maps["event"][str(row["id"])] = {
            "$ref": "event",
            "replay_key": _semantic_replay_key(str(row["idempotency_key"])),
        }
    for row in _query_rows(
        session,
        """
        SELECT item.id, item.item_type, item.original_amount_fen, item.due_date,
               item.payable_category, item.payable_agency_code, item.insurance_kind,
               event.idempotency_key, counterparty.kind AS counterparty_kind,
               counterparty.name AS counterparty_name,
               counterparty.external_ref AS counterparty_external_ref
          FROM open_items AS item
          JOIN business_events AS event
            ON event.org_id=item.org_id AND event.id=item.source_event_id
          LEFT JOIN counterparties AS counterparty
            ON counterparty.org_id=item.org_id AND counterparty.id=item.counterparty_id
         WHERE item.org_id=:org_id
        """,
        org_id=org_id,
    ):
        maps["open_item"][str(row["id"])] = {
            "$ref": "open_item",
            "source_replay_key": _semantic_replay_key(str(row["idempotency_key"])),
            "item_type": row["item_type"],
            "original_amount_fen": row["original_amount_fen"],
            "due_date": row["due_date"],
            "payable_category": row["payable_category"],
            "payable_agency_code": row["payable_agency_code"],
            "insurance_kind": row["insurance_kind"],
            "counterparty_kind": row["counterparty_kind"],
            "counterparty_name": row["counterparty_name"],
            "counterparty_external_ref": row["counterparty_external_ref"],
        }
    for table_name, target, code_name in (
        ("fixed_assets", "asset", "asset_code"),
        ("intangible_assets", "intangible", "asset_code"),
        ("labor_service_persons", "labor_person", "person_code"),
        ("borrowings", "borrowing", "borrowing_code"),
    ):
        for row in _query_rows(
            session,
            f'SELECT id, "{code_name}" AS code FROM "{table_name}" WHERE org_id=:org_id',
            org_id=org_id,
        ):
            maps[target][str(row["id"])] = {"$ref": target, "code": row["code"]}
    income_tax_event_ids: set[str] = set()
    for row in _query_rows(
        session,
        "SELECT confirmation.id, confirmation.calendar_year, "
        "confirmation.calendar_quarter, confirmation.treatment, "
        "confirmation.business_event_id, confirmation.component_id, "
        "component.key AS component_key, event.idempotency_key "
        "FROM enterprise_income_tax_quarter_confirmations AS confirmation "
        "LEFT JOIN business_event_components AS component "
        "ON component.org_id=confirmation.org_id AND component.id=confirmation.component_id "
        "LEFT JOIN business_events AS event ON event.org_id=component.org_id "
        "AND event.id=component.event_id WHERE confirmation.org_id=:org_id",
        org_id=org_id,
    ):
        key = (
            f"enterprise-income-tax:{row['calendar_year']}-"
            f"Q{row['calendar_quarter']}:{row['treatment']}"
        )
        if row["component_id"]:
            maps["income_tax"][str(row["id"])] = {
                "$ref": "component_income_tax",
                "source_replay_key": _semantic_replay_key(row["idempotency_key"]),
                "component_key": row["component_key"],
                "result_kind": "confirmation",
            }
        else:
            maps["income_tax"][str(row["id"])] = {
                "$ref": "operation_result",
                "operation_key": key,
                "field": "enterprise_income_tax_confirmation_id",
            }
        if row["business_event_id"] and not row["component_id"]:
            income_tax_event_ids.add(str(row["business_event_id"]))
            maps["event"][str(row["business_event_id"])] = {
                "$ref": "operation_result",
                "operation_key": key,
                "field": "event_id",
            }
    for row in _query_rows(
        session,
        "SELECT result.id, result.idempotency_key, result.business_event_id, "
        "result.component_id, component.key AS component_key, event.idempotency_key "
        "AS event_idempotency_key FROM enterprise_income_tax_results AS result "
        "LEFT JOIN business_event_components AS component ON component.org_id=result.org_id "
        "AND component.id=result.component_id LEFT JOIN business_events AS event "
        "ON event.org_id=component.org_id AND event.id=component.event_id "
        "WHERE result.org_id=:org_id ORDER BY result.created_at, result.revision",
        org_id=org_id,
    ):
        key = "cit-result:" + _semantic_replay_key(row["idempotency_key"])
        maps["income_tax"][str(row["id"])] = (
            {
                "$ref": "component_income_tax",
                "source_replay_key": _semantic_replay_key(row["event_idempotency_key"]),
                "component_key": row["component_key"],
                "result_kind": "result",
            }
            if row["component_id"]
            else {
                "$ref": "operation_result",
                "operation_key": key,
                "field": "result_id",
            }
        )
        event_id = row["business_event_id"]
        if event_id and not row["component_id"] and str(event_id) not in income_tax_event_ids:
            income_tax_event_ids.add(str(event_id))
            maps["event"][str(event_id)] = {
                "$ref": "operation_result",
                "operation_key": key,
                "field": "event_id",
            }
    for row in _query_rows(
        session,
        "SELECT component.id, component.key, event.idempotency_key "
        "FROM business_event_components AS component "
        "JOIN business_events AS event ON event.org_id=component.org_id "
        "AND event.id=component.event_id WHERE component.org_id=:org_id",
        org_id=org_id,
    ):
        maps["component"][str(row["id"])] = {
            "$ref": "component",
            "source_replay_key": _semantic_replay_key(str(row["idempotency_key"])),
            "component_key": row["key"],
        }
    for row in _query_rows(
        session,
        "SELECT item.id, item.component_key AS open_item_key, component.key, event.idempotency_key "
        "FROM open_items AS item "
        "JOIN business_event_components AS component ON component.org_id=item.org_id "
        "AND component.id=item.source_component_id "
        "JOIN business_events AS event ON event.org_id=component.org_id "
        "AND event.id=component.event_id WHERE item.org_id=:org_id",
        org_id=org_id,
    ):
        maps["open_item"][str(row["id"])] = {
            "$ref": "component_open_item",
            "source_replay_key": _semantic_replay_key(str(row["idempotency_key"])),
            "component_key": row["key"],
            "open_item_key": row["open_item_key"],
        }
    return maps


def _replace_stable_references(
    value: Any,
    *,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
    key: str | None = None,
) -> Any:
    if isinstance(value, dict):
        if set(value) == {"id", "fingerprint"} and value.get("id") is not None:
            replacement = maps["bank"].get(str(value["id"]))
            if replacement is not None:
                return {**replacement, "$ref": "bank_transaction_reference"}
        # CounterpartyRef is an embedded business reference.  Its ``id`` is a
        # source-database UUID and must never leak into the portable package.
        if value.get("id") is not None:
            replacement = maps["counterparty"].get(str(value["id"]))
            if replacement is not None:
                return {
                    "id": None,
                    "kind": replacement["kind"],
                    "name": replacement["name"],
                    "external_ref": replacement["external_ref"],
                }
        return {
            str(child_key): _replace_stable_references(
                child,
                org_id=org_id,
                maps=maps,
                key=str(child_key),
            )
            for child_key, child in value.items()
            if not str(child_key).startswith("_")
            and child_key not in {"derived", "calculation", "calculation_hash"}
        }
    if isinstance(value, list):
        return [
            _replace_stable_references(item, org_id=org_id, maps=maps, key=key) for item in value
        ]
    if isinstance(value, uuid.UUID):
        value = str(value)
    if isinstance(value, str):
        if value == str(org_id):
            return "${ORG_ID}"
        lookup_order = {
            "source_id": ("income_tax",),
            "previous_result_id": ("income_tax",),
            "original_confirmation_id": ("income_tax",),
            "evidence_references": ("evidence",),
            "bank_transaction_id": ("bank",),
            "bank_transaction_ids": ("bank",),
            "bank_transaction_references": ("bank",),
            "employee_id": ("employee",),
            "prior_labor_person_id": ("labor_person",),
            "labor_person_id": ("labor_person",),
            "source_open_item_id": ("open_item",),
            "open_item_id": ("open_item",),
            "component_id": ("component",),
            "asset_id": ("asset", "intangible"),
            "borrowing_id": ("borrowing",),
            "original_event_id": ("event",),
            "regular_payroll_batch_id": ("event",),
        }.get(key, ())
        for map_name in lookup_order:
            if value in maps[map_name]:
                return maps[map_name][value]
        for map_name in (
            "income_tax",
            "evidence",
            "bank",
            "employee",
            "open_item",
            "component",
            "asset",
            "intangible",
            "labor_person",
            "borrowing",
            "event",
            "counterparty",
        ):
            if value in maps[map_name] and key not in _TECHNICAL_UUID_KEYS:
                return maps[map_name][value]
    return _jsonable(value)


def _table_request(
    row: Mapping[str, Any],
    fields: Sequence[str],
    *,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    value = {field: row.get(field) for field in fields}
    value["org_id"] = str(org_id)
    return _replace_stable_references(value, org_id=org_id, maps=maps)


def _evidence_refs_for(
    session: Session,
    *,
    table: str,
    owner_column: str,
    owner_id: uuid.UUID,
    evidence_by_id: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = _query_rows(
        session,
        f'SELECT evidence_id FROM "{table}" WHERE "{owner_column}"=:owner_id ORDER BY evidence_id',
        owner_id=owner_id,
    )
    return [evidence_by_id[str(row["evidence_id"])] for row in rows]


def _setup_operations(
    session: Session,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    labor_people = _query_rows(
        session,
        """
        SELECT id, person_code, name, relationship_start_date, relationship_end_date,
               status, idempotency_key, created_at
          FROM labor_service_persons
         WHERE org_id=:org_id ORDER BY created_at, person_code
        """,
        org_id=org_id,
    )
    for row in labor_people:
        request = _table_request(
            row,
            (
                "person_code",
                "name",
                "relationship_start_date",
                "relationship_end_date",
                "status",
            ),
            org_id=org_id,
            maps=maps,
        )
        request["idempotency_key"] = _replay_idempotency(f"labor-person:{row['person_code']}")
        request["evidence_references"] = _evidence_refs_for(
            session,
            table="labor_service_person_evidence",
            owner_column="labor_person_id",
            owner_id=row["id"],
            evidence_by_id=maps["evidence"],
        )
        operations.append(
            {
                "key": f"labor-person:{row['person_code']}",
                "kind": "tool",
                "tool": "finance_register_labor_service_person",
                "request": request,
                "allowed_statuses": ["registered"],
            }
        )

    employees = _query_rows(
        session,
        """
        SELECT id, employee_code, name, employment_start_date,
               tax_withholding_start_date, employment_end_date, status,
               prior_labor_person_id, created_at
          FROM employees WHERE org_id=:org_id ORDER BY created_at, employee_code
        """,
        org_id=org_id,
    )
    for row in employees:
        request = _table_request(
            row,
            (
                "employee_code",
                "name",
                "employment_start_date",
                "tax_withholding_start_date",
                "employment_end_date",
                "status",
                "prior_labor_person_id",
            ),
            org_id=org_id,
            maps=maps,
        )
        operations.append(
            {
                "key": f"employee:{row['employee_code']}",
                "kind": "tool",
                "tool": "finance_register_employee",
                "request": request,
                "allowed_statuses": ["registered"],
            }
        )

    profiles = _query_rows(
        session,
        """
        SELECT id, employee_id, supersedes_id, effective_from, effective_to,
               expense_role, social_insurance_base_fen, housing_fund_base_fen,
               social_insurance_participating, housing_fund_participating,
               resident_employee, created_at
          FROM employee_payroll_profile_versions
         WHERE org_id=:org_id ORDER BY created_at, id
        """,
        org_id=org_id,
    )
    profile_keys = {
        str(row["id"]): (
            f"employee-profile:{maps['employee'][str(row['employee_id'])]['employee_code']}:"
            f"{row['effective_from']}"
        )
        for row in profiles
    }
    for row in profiles:
        request = _table_request(
            row,
            (
                "employee_id",
                "effective_from",
                "effective_to",
                "expense_role",
                "social_insurance_base_fen",
                "housing_fund_base_fen",
                "social_insurance_participating",
                "housing_fund_participating",
                "resident_employee",
            ),
            org_id=org_id,
            maps=maps,
        )
        if row["supersedes_id"] is not None:
            request["supersedes_profile_version_id"] = {
                "$ref": "operation_result",
                "operation_key": profile_keys[str(row["supersedes_id"])],
                "field": "profile_version_id",
            }
        operations.append(
            {
                "key": profile_keys[str(row["id"])],
                "kind": "tool",
                "tool": "finance_register_employee_profile_version",
                "request": request,
                "allowed_statuses": ["registered"],
            }
        )

    policies = _query_rows(
        session,
        """
        SELECT id, region, effective_from, effective_to, version, source_url,
               parameters, supersedes_id, created_at
          FROM payroll_policy_versions
         WHERE org_id=:org_id ORDER BY created_at, id
        """,
        org_id=org_id,
    )
    policy_keys = {
        str(row["id"]): (f"payroll-policy:{row['region']}:{row['version']}:{row['effective_from']}")
        for row in policies
    }
    for row in policies:
        request = _table_request(
            row,
            ("region", "effective_from", "effective_to", "version", "source_url", "parameters"),
            org_id=org_id,
            maps=maps,
        )
        if row["supersedes_id"] is not None:
            request["supersedes_policy_version_id"] = {
                "$ref": "operation_result",
                "operation_key": policy_keys[str(row["supersedes_id"])],
                "field": "policy_version_id",
            }
        operations.append(
            {
                "key": policy_keys[str(row["id"])],
                "kind": "tool",
                "tool": "finance_register_payroll_policy_version",
                "request": request,
                "allowed_statuses": ["registered"],
            }
        )

    return operations


def _payroll_fact_operations(
    session: Session,
    *,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Export final effective payroll inputs, excluding superseded audit history."""

    operations: list[dict[str, Any]] = []
    opening_rows = _query_rows(
        session,
        """
        SELECT state.*
          FROM payroll_opening_states AS state
         WHERE state.org_id=:org_id
           AND NOT EXISTS (
               SELECT 1 FROM payroll_opening_states AS successor
                WHERE successor.org_id=state.org_id
                  AND successor.supersedes_id=state.id)
         ORDER BY state.tax_year, state.through_month, state.employee_id
        """,
        org_id=org_id,
    )
    opening_fields = (
        "employee_id",
        "tax_year",
        "through_month",
        "cumulative_income_fen",
        "cumulative_tax_exempt_income_fen",
        "cumulative_basic_deduction_fen",
        "cumulative_employee_social_insurance_fen",
        "cumulative_employee_housing_fund_fen",
        "cumulative_special_additional_deduction_fen",
        "cumulative_other_legal_deduction_fen",
        "cumulative_tax_relief_fen",
        "cumulative_tax_withheld_fen",
    )
    for row in opening_rows:
        employee_code = maps["employee"][str(row["employee_id"])]["employee_code"]
        operations.append(
            {
                "key": (
                    f"payroll-opening:{employee_code}:{row['tax_year']}:{row['through_month']:02d}"
                ),
                "kind": "tool",
                "tool": "finance_register_payroll_opening_state",
                "request": _table_request(row, opening_fields, org_id=org_id, maps=maps),
                "allowed_statuses": ["registered"],
            }
        )

    treatment_rows = _query_rows(
        session,
        """
        SELECT treatment.*
          FROM payroll_first_wage_tax_treatments AS treatment
         WHERE treatment.org_id=:org_id
           AND NOT EXISTS (
               SELECT 1 FROM payroll_first_wage_tax_treatments AS successor
                WHERE successor.org_id=treatment.org_id
                  AND successor.supersedes_id=treatment.id)
         ORDER BY treatment.tax_year, treatment.employee_id
        """,
        org_id=org_id,
    )
    for row in treatment_rows:
        employee_code = maps["employee"][str(row["employee_id"])]["employee_code"]
        request = _table_request(
            row,
            (
                "employee_id",
                "tax_year",
                "first_wage_month",
                "treatment_state",
                "declaration_date",
                "confirmation_description",
            ),
            org_id=org_id,
            maps=maps,
        )
        request["idempotency_key"] = _replay_idempotency(
            f"first-wage:{employee_code}:{row['tax_year']}"
        )
        request["evidence_references"] = _evidence_refs_for(
            session,
            table="payroll_first_wage_tax_treatment_evidence",
            owner_column="treatment_id",
            owner_id=row["id"],
            evidence_by_id=maps["evidence"],
        )
        operations.append(
            {
                "key": f"first-wage:{employee_code}:{row['tax_year']}",
                "kind": "tool",
                "tool": "finance_register_payroll_first_wage_tax_treatment",
                "request": request,
                "allowed_statuses": ["registered"],
            }
        )

    active_items = _query_rows(
        session,
        """
        SELECT item.*, actual.declaration_date, actual.reason_code,
               actual.reason_description
          FROM payroll_contribution_actual_items AS item
          JOIN payroll_contribution_actual_sets AS actual
            ON actual.org_id=item.org_id AND actual.id=item.actual_set_id
         WHERE item.org_id=:org_id
           AND NOT EXISTS (
               SELECT 1 FROM payroll_contribution_actual_items AS successor
                WHERE successor.org_id=item.org_id
                  AND successor.supersedes_id=item.id)
         ORDER BY item.contribution_period, item.employee_id,
                  item.actual_set_id, item.contribution_group, item.insurance_kind
        """,
        org_id=org_id,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in active_items:
        grouped.setdefault(str(row["actual_set_id"]), []).append(row)
    for set_id, rows in grouped.items():
        first = rows[0]
        employee_code = maps["employee"][str(first["employee_id"])]["employee_code"]
        stable_key = (
            f"contribution-actual:{first['contribution_period']}:"
            f"{employee_code}:{first['declaration_date']}"
        )
        request = {
            "org_id": "${ORG_ID}",
            "idempotency_key": _replay_idempotency(stable_key),
            "employee_id": maps["employee"][str(first["employee_id"])],
            "contribution_period": first["contribution_period"],
            "declaration_date": first["declaration_date"],
            "reason_code": first["reason_code"],
            "reason_description": first["reason_description"],
            "items": [
                {
                    "contribution_group": item["contribution_group"],
                    "insurance_kind": item["insurance_kind"],
                    "actual_state": item["actual_state"],
                    "employee_amount_fen": item["employee_amount_fen"],
                    "employer_amount_fen": item["employer_amount_fen"],
                }
                for item in rows
            ],
            "evidence_references": _evidence_refs_for(
                session,
                table="payroll_contribution_actual_evidence",
                owner_column="actual_set_id",
                owner_id=uuid.UUID(set_id),
                evidence_by_id=maps["evidence"],
            ),
            "supersedes_actual_ids": [],
        }
        operations.append(
            {
                "key": stable_key,
                "kind": "tool",
                "tool": "finance_register_payroll_contribution_actual",
                "request": request,
                "allowed_statuses": ["registered"],
            }
        )
    return operations


def _replay_idempotency(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"semantic-replay:{digest[:40]}"


def _event_workflow_request(
    session: Session,
    event: Mapping[str, Any],
) -> dict[str, Any] | None:
    event_id = event["id"]
    query_by_type = {
        "payroll_accrual": (
            "SELECT calculation_input FROM payroll_batches WHERE business_event_id=:event_id"
        ),
        "labor_remuneration_accrual": (
            "SELECT calculation_input FROM labor_remuneration_batches "
            "WHERE business_event_id=:event_id"
        ),
    }
    sql = query_by_type.get(str(event["event_type"]))
    if sql is None:
        return None
    value = session.execute(text(sql), {"event_id": event_id}).scalar_one_or_none()
    if not isinstance(value, dict) or not isinstance(value.get("request"), dict):
        raise ReplayError("REPLAY_SOURCE_WORKFLOW_REQUEST_MISSING")
    return dict(value["request"])


def _filter_request_for_tool(name: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Remove result-only fields that services append to persisted event facts."""

    from . import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool(name)
    if tool is None:
        raise ReplayError(f"REPLAY_TOOL_NOT_ALLOWED:{name}")
    model = _tool_request_model(tool)
    return {key: value for key, value in request.items() if key in model.model_fields}


def _composite_replay_request(
    session: Session,
    event: Mapping[str, Any],
    *,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
    replay_key: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replace frozen calculated component identities with replay-time previews."""

    raw_request = dict(event["facts"] or {})
    raw_request["description"] = event.get("description", "")
    request = _replace_stable_references(raw_request, org_id=org_id, maps=maps)
    components = request.get("components")
    raw_components = raw_request.get("components")
    if not isinstance(components, list) or not isinstance(raw_components, list):
        raise ReplayError("REPLAY_SOURCE_COMPONENT_FACTS_MISSING")
    by_key = {str(item.get("key")): item for item in components if isinstance(item, dict)}
    raw_by_key = {
        str(item.get("key")): item for item in raw_components if isinstance(item, Mapping)
    }
    preparations_by_component: dict[str, dict[str, Any]] = {}
    preparation_dependencies: dict[str, set[str]] = {}
    calculated_line_refs: dict[str, dict[str, Any]] = {}
    workflows = {
        "payroll_accrual": (
            "payroll_batches",
            "finance_preview_payroll",
        ),
        "labor_remuneration_accrual": (
            "labor_remuneration_batches",
            "finance_preview_labor_remuneration_batch",
        ),
    }
    for raw_component in raw_components:
        if not isinstance(raw_component, Mapping):
            raise ReplayError("REPLAY_SOURCE_COMPONENT_FACTS_MISSING")
        key = str(raw_component.get("key", ""))
        kind = str(raw_component.get("kind", ""))
        exported = by_key.get(key)
        if exported is None:
            raise ReplayError("REPLAY_SOURCE_COMPONENT_FACTS_MISSING")
        if kind in {"tax_relief", "enterprise_income_tax_result"}:
            # The secured whole-event preview recalculates these against all
            # preceding and sibling components and returns the reviewed hash.
            exported["calculation_hash"] = "0" * 64
        if kind in {"fixed_asset_depreciation", "fixed_asset_depreciation_batch"}:
            depreciation_facts = exported.get("facts")
            if not isinstance(depreciation_facts, dict):
                raise ReplayError("REPLAY_SOURCE_COMPONENT_FACTS_MISSING")
            # Local activation sources are rebuilt by the whole-event compiler.
            # The exported request carries only stable component keys; the
            # secured preview supplies a new calculation hash and formal source
            # proof for the target database.
            depreciation_facts["calculation_hash"] = "0" * 64
        workflow = workflows.get(kind)
        if workflow is None:
            continue
        batch_id = raw_component.get("batch_id")
        if batch_id is None:
            raise ReplayError("REPLAY_SOURCE_WORKFLOW_REQUEST_MISSING")
        table_name, preview_tool = workflow
        calculation_input = session.scalar(
            text(f'SELECT calculation_input FROM "{table_name}" WHERE org_id=:org_id AND id=:id'),
            {"org_id": org_id, "id": batch_id},
        )
        if not isinstance(calculation_input, Mapping) or not isinstance(
            calculation_input.get("request"), Mapping
        ):
            raise ReplayError("REPLAY_SOURCE_WORKFLOW_REQUEST_MISSING")
        preparation_key = f"{replay_key}:prepare:{key}"
        preview_request = _replace_stable_references(
            dict(calculation_input["request"]), org_id=org_id, maps=maps
        )
        preview_request["org_id"] = "${ORG_ID}"
        preview_request["idempotency_key"] = _replay_idempotency(preparation_key)
        preview_request = _filter_request_for_tool(preview_tool, preview_request)
        preparation = {
            "key": preparation_key,
            "preview_tool": preview_tool,
            "preview_request": preview_request,
            "allowed_statuses": ["calculated"],
        }
        dependencies: set[str] = set()
        regular_component_keys = raw_component.get("regular_payroll_component_keys") or []
        if regular_component_keys:
            if kind != "payroll_accrual":
                raise ReplayError("REPLAY_SOURCE_PAYROLL_DEPENDENCY_INVALID")
            if not isinstance(regular_component_keys, list):
                raise ReplayError("REPLAY_SOURCE_PAYROLL_DEPENDENCY_INVALID")
            parent_by_batch_id: dict[str, tuple[str, str]] = {}
            for value in regular_component_keys:
                regular_component_key = str(value)
                regular_component = raw_by_key.get(regular_component_key)
                if (
                    regular_component is None
                    or regular_component.get("kind") != "payroll_accrual"
                    or regular_component.get("batch_id") is None
                ):
                    raise ReplayError("REPLAY_SOURCE_PAYROLL_DEPENDENCY_INVALID")
                regular_batch_id = str(regular_component["batch_id"])
                parent_preparation_key = f"{replay_key}:prepare:{regular_component_key}"
                if regular_batch_id in parent_by_batch_id:
                    raise ReplayError("REPLAY_SOURCE_PAYROLL_DEPENDENCY_INVALID")
                parent_by_batch_id[regular_batch_id] = (
                    regular_component_key,
                    parent_preparation_key,
                )
            employee_items = preview_request.get("employee_items")
            if not isinstance(employee_items, list) or not employee_items:
                raise ReplayError("REPLAY_SOURCE_PAYROLL_DEPENDENCY_INVALID")
            used_parent_keys: set[str] = set()
            for item in employee_items:
                if not isinstance(item, dict):
                    raise ReplayError("REPLAY_SOURCE_PAYROLL_DEPENDENCY_INVALID")
                parent = parent_by_batch_id.get(str(item.get("regular_payroll_batch_id")))
                if parent is None:
                    raise ReplayError("REPLAY_SOURCE_PAYROLL_DEPENDENCY_INVALID")
                regular_component_key, parent_preparation_key = parent
                item["regular_payroll_batch_id"] = {
                    "$ref": "operation_result",
                    "operation_key": parent_preparation_key,
                    "field": "batch_id",
                }
                used_parent_keys.add(regular_component_key)
            dependencies.update(used_parent_keys)
            if used_parent_keys != {str(value) for value in regular_component_keys}:
                raise ReplayError("REPLAY_SOURCE_PAYROLL_DEPENDENCY_INVALID")
            preparation["depends_on"] = [
                f"{replay_key}:prepare:{component_key}" for component_key in regular_component_keys
            ]
        preparations_by_component[key] = preparation
        preparation_dependencies[key] = dependencies
        exported["batch_id"] = {
            "$ref": "operation_result",
            "operation_key": preparation_key,
            "field": "batch_id",
        }
        exported["calculation_hash"] = {
            "$ref": "operation_result",
            "operation_key": preparation_key,
            "field": "calculation_hash",
        }
        if kind == "payroll_accrual":
            rows = _query_rows(
                session,
                "SELECT line.id, employee.employee_code FROM payroll_lines AS line "
                "JOIN employees AS employee ON employee.org_id=line.org_id "
                "AND employee.id=line.employee_id WHERE line.org_id=:org_id "
                "AND line.payroll_batch_id=:batch_id",
                org_id=org_id,
                batch_id=batch_id,
            )
            calculated_line_refs.update(
                {
                    f"salary:{row['id']}": {
                        "$ref": "prepared_line_key",
                        "operation_key": preparation_key,
                        "identity_code": row["employee_code"],
                        "line_kind": "payroll",
                    }
                    for row in rows
                }
            )
        else:
            rows = _query_rows(
                session,
                "SELECT line.id, person.person_code FROM labor_remuneration_lines AS line "
                "JOIN labor_service_persons AS person ON person.org_id=line.org_id "
                "AND person.id=line.labor_person_id WHERE line.org_id=:org_id "
                "AND line.batch_id=:batch_id",
                org_id=org_id,
                batch_id=batch_id,
            )
            calculated_line_refs.update(
                {
                    str(row["id"]): {
                        "$ref": "prepared_line_key",
                        "operation_key": preparation_key,
                        "identity_code": row["person_code"],
                        "line_kind": "labor",
                    }
                    for row in rows
                }
            )

    preparations: list[dict[str, Any]] = []
    emitted: set[str] = set()
    while len(emitted) < len(preparations_by_component):
        ready = [
            key
            for key in preparations_by_component
            if key not in emitted and preparation_dependencies[key].issubset(emitted)
        ]
        if not ready:
            raise ReplayError("REPLAY_SOURCE_PAYROLL_DEPENDENCY_INVALID")
        for key in ready:
            preparations.append(preparations_by_component[key])
            emitted.add(key)

    def replace_line_keys(value: Any) -> Any:
        if isinstance(value, list):
            return [replace_line_keys(item) for item in value]
        if isinstance(value, dict):
            return {name: replace_line_keys(item) for name, item in value.items()}
        return calculated_line_refs.get(str(value), value)

    request = replace_line_keys(request)
    return request, preparations


def _event_operation(
    session: Session,
    event: Mapping[str, Any],
    *,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    event_type = str(event["event_type"])
    replay_key = _semantic_replay_key(str(event["idempotency_key"]))
    facts = dict(event["facts"] or {})
    workflow_request = _event_workflow_request(session, event)
    request = workflow_request or facts
    request["org_id"] = "${ORG_ID}"
    request["idempotency_key"] = _replay_idempotency(replay_key + ":preview")
    common = {
        "key": replay_key,
        "replay_key": replay_key,
        "source_business_date": event["business_date"],
        "source_posting_date": event["posting_date"],
        "source_event_type": event_type,
        "source_created_at": str(event["created_at"]),
        "source_fact_sha256": hashlib.sha256(
            json.dumps(_jsonable(facts), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }
    if isinstance(facts.get("components"), list):
        request, preparations = _composite_replay_request(
            session,
            event,
            org_id=org_id,
            maps=maps,
            replay_key=replay_key,
        )
        if not isinstance(request.get("components"), list) or not request["components"]:
            raise ReplayError("REPLAY_SOURCE_COMPONENT_FACTS_MISSING")
        request["idempotency_key"] = _replay_idempotency(replay_key)
        request = _filter_request_for_tool("finance_record_event", request)
        return {
            **common,
            "kind": "composite_event",
            "tool": "finance_record_event",
            "request": request,
            "preparations": preparations,
            "allowed_statuses": ["posted"],
        }
    request = _replace_stable_references(request, org_id=org_id, maps=maps)
    if event_type in _DIRECT_SPECIALIZED_TOOLS:
        request["idempotency_key"] = _replay_idempotency(replay_key)
        tool_name = _DIRECT_SPECIALIZED_TOOLS[event_type]
        request = _filter_request_for_tool(tool_name, request)
        return {
            **common,
            "kind": "tool",
            "tool": tool_name,
            "request": request,
            "allowed_statuses": ["posted"],
        }
    if event_type not in _PREVIEW_CONFIRM_WORKFLOWS:
        raise ReplayError(f"REPLAY_SOURCE_EVENT_TYPE_UNSUPPORTED:{event_type}")
    preview_tool, confirm_tool = _PREVIEW_CONFIRM_WORKFLOWS[event_type]
    if event_type == "fixed_asset_depreciation":
        request = {
            "org_id": "${ORG_ID}",
            "depreciation_period": facts["depreciation_period"],
            "posting_date": facts["posting_date"],
        }
    elif event_type == "intangible_asset_amortization":
        request = {
            "org_id": "${ORG_ID}",
            "asset_id": _replace_stable_references(
                facts["asset_id"], org_id=org_id, maps=maps, key="asset_id"
            ),
            "amortization_period": facts["amortization_period"],
            "posting_date": facts["posting_date"],
        }
    elif event_type == "borrowing_interest_accrual":
        request = {
            key: value
            for key, value in request.items()
            if key not in {"idempotency_key", "calculation_hash", "confirmation_note"}
        }
    elif event_type == "tax_relief":
        tax_period = facts["tax_period"]
        request = {
            "org_id": "${ORG_ID}",
            "start_date": tax_period["start_date"],
            "end_date": tax_period["end_date"],
            "adjustment_posting_date": tax_period["adjustment_posting_date"],
        }
    request = _filter_request_for_tool(preview_tool, request)
    confirmation_note = str(
        facts.get("confirmation_note") or event.get("description") or "依据已确认业务事实确认。"
    )
    confirm_request = {
        "idempotency_key": _replay_idempotency(replay_key),
        "confirmation_note": confirmation_note,
    }
    if event_type == "tax_relief":
        confirm_request.pop("confirmation_note")
    return {
        **common,
        "kind": "preview_confirm",
        "preview_tool": preview_tool,
        "confirm_tool": confirm_tool,
        "preview_request": request,
        "confirm_request": confirm_request,
        "allowed_preview_statuses": ["calculated"],
        "allowed_confirm_statuses": ["posted"],
    }


def _company_checkpoints(session: Session, org_id: uuid.UUID) -> dict[str, Any]:
    def count(table_name: str, predicate: str = "") -> int:
        suffix = f" AND {predicate}" if predicate else ""
        return int(
            session.scalar(
                text(f'SELECT COUNT(*) FROM "{table_name}" WHERE org_id=:org_id{suffix}'),
                {"org_id": org_id},
            )
            or 0
        )

    debit, credit = session.execute(
        text(
            "SELECT COALESCE(SUM(line.debit_fen),0), COALESCE(SUM(line.credit_fen),0) "
            "FROM voucher_lines AS line JOIN vouchers AS voucher "
            "ON voucher.org_id=line.org_id AND voucher.id=line.voucher_id "
            "WHERE line.org_id=:org_id AND voucher.status='posted'"
        ),
        {"org_id": org_id},
    ).one()
    periods = _query_rows(
        session,
        "SELECT calendar_year, calendar_month, status FROM accounting_periods "
        "WHERE org_id=:org_id ORDER BY calendar_year, calendar_month",
        org_id=org_id,
    )
    return {
        "effective_event_count": len(_effective_events(session, org_id)),
        "effective_voucher_count": count("vouchers", "status='posted'"),
        "voucher_debit_total_fen": int(debit),
        "voucher_credit_total_fen": int(credit),
        "bank_transaction_count": count("bank_transactions"),
        "matched_bank_transaction_count": count(
            "bank_transactions", "matched_event_id IS NOT NULL"
        ),
        "evidence_count": count("evidence"),
        "payroll_batch_count": count(
            "payroll_batches", "status='posted' AND reversal_of_batch_id IS NULL"
        ),
        "bank_reconciliation_count": count("bank_reconciliations"),
        "fixed_asset_count": count("fixed_assets"),
        "intangible_asset_count": count("intangible_assets"),
        "labor_remuneration_batch_count": count("labor_remuneration_batches", "status='posted'"),
        "financial_statement_classification_count": count("financial_statement_classifications"),
        "enterprise_income_tax_confirmation_count": count(
            "enterprise_income_tax_quarter_confirmations"
        ),
        "enterprise_income_tax_result_count": count("enterprise_income_tax_results"),
        "enterprise_income_tax_settlement_count": count("enterprise_income_tax_settlements"),
        "closed_period_count": sum(item["status"] == "closed" for item in periods),
        "closed_periods": [
            f"{item['calendar_year']:04d}-{item['calendar_month']:02d}"
            for item in periods
            if item["status"] == "closed"
        ],
        "open_periods": [
            f"{item['calendar_year']:04d}-{item['calendar_month']:02d}"
            for item in periods
            if item["status"] == "open"
        ],
        "open_item_balance_fen": int(
            session.scalar(
                text(
                    "SELECT COALESCE(SUM(original_amount_fen-settled_amount_fen),0) "
                    "FROM open_items "
                    "WHERE org_id=:org_id"
                ),
                {"org_id": org_id},
            )
            or 0
        ),
    }


def _account_balance_projection(session: Session, org_id: uuid.UUID) -> list[dict[str, Any]]:
    return _query_rows(
        session,
        """
        SELECT account.code AS account_code, account.name AS account_name,
               account.category, account.normal_side, account.system_role,
               COALESCE(SUM(CASE WHEN voucher.id IS NOT NULL
                                 THEN line.debit_fen ELSE 0 END),0) AS debit_total_fen,
               COALESCE(SUM(CASE WHEN voucher.id IS NOT NULL
                                 THEN line.credit_fen ELSE 0 END),0) AS credit_total_fen,
               CASE WHEN account.normal_side='debit'
                    THEN COALESCE(SUM(CASE WHEN voucher.id IS NOT NULL
                              THEN line.debit_fen-line.credit_fen ELSE 0 END),0)
                    ELSE COALESCE(SUM(CASE WHEN voucher.id IS NOT NULL
                              THEN line.credit_fen-line.debit_fen ELSE 0 END),0)
                END AS ending_balance_fen
          FROM accounts AS account
          LEFT JOIN voucher_lines AS line
            ON line.org_id=account.org_id AND line.account_id=account.id
          LEFT JOIN vouchers AS voucher
            ON voucher.org_id=line.org_id AND voucher.id=line.voucher_id
           AND voucher.status='posted'
         WHERE account.org_id=:org_id
         GROUP BY account.code, account.name, account.category,
                  account.normal_side, account.system_role
        HAVING COALESCE(SUM(CASE WHEN voucher.id IS NOT NULL
                                 THEN line.debit_fen ELSE 0 END),0)<>0
            OR COALESCE(SUM(CASE WHEN voucher.id IS NOT NULL
                                 THEN line.credit_fen ELSE 0 END),0)<>0
         ORDER BY account.code
        """,
        org_id=org_id,
    )


def _open_item_projection(
    session: Session,
    *,
    org_id: uuid.UUID,
) -> list[dict[str, Any]]:
    rows = _query_rows(
        session,
        """
        SELECT item.id, item.item_type, item.original_amount_fen,
               item.settled_amount_fen, item.status, item.due_date,
               item.payable_category, item.payable_agency_code, item.insurance_kind,
               event.idempotency_key, counterparty.kind AS counterparty_kind,
               counterparty.name AS counterparty_name,
               counterparty.external_ref AS counterparty_external_ref
          FROM open_items AS item
          JOIN business_events AS event
            ON event.org_id=item.org_id AND event.id=item.source_event_id
          LEFT JOIN counterparties AS counterparty
            ON counterparty.org_id=item.org_id AND counterparty.id=item.counterparty_id
         WHERE item.org_id=:org_id AND item.status IN ('open','partial')
         ORDER BY event.idempotency_key, item.item_type, item.original_amount_fen,
                  item.payable_category, item.insurance_kind
        """,
        org_id=org_id,
    )
    projection = [
        {
            "counterparty": {
                "kind": row["counterparty_kind"],
                "name": row["counterparty_name"],
                "external_ref": row["counterparty_external_ref"],
            },
            "item_type": row["item_type"],
            "original_amount_fen": row["original_amount_fen"],
            "settled_amount_fen": row["settled_amount_fen"],
            "remaining_amount_fen": (row["original_amount_fen"] - row["settled_amount_fen"]),
            "status": row["status"],
            "due_date": row["due_date"],
            "payable_category": row["payable_category"],
            "payable_agency_code": row["payable_agency_code"],
            "insurance_kind": row["insurance_kind"],
        }
        for row in rows
    ]
    return sorted(
        projection,
        key=lambda item: json.dumps(
            _jsonable(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )


def _export_evidence(
    session: Session,
    *,
    org_id: uuid.UUID,
    company_dir: Path,
) -> list[dict[str, Any]]:
    evidence_dir = company_dir / "evidence"
    evidence_dir.mkdir()
    rows = _query_rows(
        session,
        """
        SELECT id, sha256, original_name, media_type, source, size_bytes,
               storage_path, metadata AS metadata_json
          FROM evidence WHERE org_id=:org_id ORDER BY sha256
        """,
        org_id=org_id,
    )
    manifest: list[dict[str, Any]] = []
    for row in rows:
        source = Path(str(row["storage_path"])).resolve(strict=True)
        digest, size = _sha256_file(source)
        if digest != row["sha256"] or size != int(row["size_bytes"]):
            raise ReplayError("REPLAY_SOURCE_EVIDENCE_MISMATCH")
        relative = f"evidence/{digest}"
        shutil.copyfile(source, company_dir / relative)
        manifest.append(
            {
                "sha256": digest,
                "size_bytes": size,
                "relative_path": relative,
                "original_name": _semantic_file_name(str(row["original_name"])),
                "media_type": row["media_type"],
                "source": row["source"],
                "metadata": _sanitize_evidence_metadata(row["metadata_json"] or {}),
            }
        )
    _write_jsonl(company_dir / "evidence-manifest.jsonl", manifest)
    return manifest


def _export_bank_transactions(
    session: Session,
    *,
    org_id: uuid.UUID,
    company_dir: Path,
) -> list[dict[str, Any]]:
    target_dir = company_dir / "bank-transactions"
    target_dir.mkdir()
    rows = _query_rows(
        session,
        """
        SELECT bank_account_code, external_id, booking_date, amount_fen, currency,
               counterparty_name, memo, fingerprint, source_sha256
          FROM bank_transactions
         WHERE org_id=:org_id
         ORDER BY bank_account_code, booking_date, external_id, id
        """,
        org_id=org_id,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["bank_account_code"]), []).append(row)
    exports: list[dict[str, Any]] = []
    for account_code, account_rows in sorted(grouped.items()):
        file_name = f"{account_code}.csv"
        path = target_dir / file_name
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=(
                    "external_id",
                    "booking_date",
                    "amount",
                    "currency",
                    "counterparty_name",
                    "memo",
                ),
            )
            writer.writeheader()
            for row in account_rows:
                writer.writerow(
                    {
                        field: (
                            format(Decimal(int(row["amount_fen"])) / Decimal(100), ".2f")
                            if field == "amount"
                            else _jsonable(row[field])
                        )
                        for field in writer.fieldnames
                    }
                )
        digest, size = _sha256_file(path)
        exports.append(
            {
                "bank_account_code": account_code,
                "relative_path": f"bank-transactions/{file_name}",
                "row_count": len(account_rows),
                "sha256": digest,
                "size_bytes": size,
                "column_mapping": {
                    "booking_date": "booking_date",
                    "amount": "amount",
                    "counterparty": "counterparty_name",
                    "memo": "memo",
                    "external_id": "external_id",
                    "currency": "currency",
                },
            }
        )
    _write_json(company_dir / "bank-imports.json", exports)
    return exports


def _account_controls(session: Session, org_id: uuid.UUID) -> list[dict[str, Any]]:
    return _query_rows(
        session,
        """
        SELECT code, name, category, normal_side, system_role, business_class, active,
               requires_bank_reconciliation, bank_reconciliation_start_date,
               bank_reconciliation_end_date
          FROM accounts WHERE org_id=:org_id ORDER BY code
        """,
        org_id=org_id,
    )


def _period_operations(
    session: Session,
    *,
    org_id: uuid.UUID,
    support_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = _query_rows(
        session,
        "SELECT calendar_year, calendar_month FROM accounting_periods "
        "WHERE org_id=:org_id ORDER BY calendar_year, calendar_month",
        org_id=org_id,
    )
    return [
        {
            "key": f"period:{row['calendar_year']:04d}-{row['calendar_month']:02d}",
            "kind": "tool",
            "tool": "finance_generate_accounting_period",
            "request": {
                "org_id": "${ORG_ID}",
                "period_month": f"{row['calendar_year']:04d}-{row['calendar_month']:02d}",
                "idempotency_key": _replay_idempotency(
                    f"period:{row['calendar_year']:04d}-{row['calendar_month']:02d}"
                ),
                "confirmation_note": "依据企业已确认的连续会计期间范围建立期间。",
                "evidence_references": [support_evidence],
            },
            "allowed_statuses": ["posted"],
        }
        for row in rows
    ]


def _evidence_operations(manifest: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "key": f"evidence:{row['sha256']}",
            "kind": "evidence",
            "relative_path": row["relative_path"],
            "request": {
                "org_id": "${ORG_ID}",
                "source": row["source"],
                "original_name": row["original_name"],
                "media_type": row["media_type"],
                "metadata": row["metadata"],
                "expected_sha256": row["sha256"],
            },
            "allowed_statuses": ["registered"],
        }
        for row in manifest
    ]


def _bank_operations(
    bank_exports: Sequence[Mapping[str, Any]],
    *,
    accounts: Sequence[Mapping[str, Any]],
    support_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    bank_accounts = [
        {
            "bank_account_code": row["code"],
            "account_name": row["name"],
            "start_date": row["bank_reconciliation_start_date"],
            "end_date": row["bank_reconciliation_end_date"],
        }
        for row in accounts
        if row["requires_bank_reconciliation"]
    ]
    operations: list[dict[str, Any]] = [
        {
            "key": "bank-reconciliation-scope:initial",
            "kind": "preview_confirm",
            "preview_tool": "finance_preview_bank_reconciliation_scope",
            "confirm_tool": "finance_confirm_bank_reconciliation_scope",
            "preview_request": {
                "org_id": "${ORG_ID}",
                "action_type": "initial_confirmation",
                "previous_action_id": None,
                "accounts": bank_accounts,
                "confirm_zero_accounts": not bank_accounts,
                "explanation": "依据完整银行账户清单确认对账范围。",
                "evidence_references": [support_evidence],
            },
            "confirm_request": {
                "idempotency_key": _replay_idempotency("bank-reconciliation-scope:initial")
            },
            "allowed_preview_statuses": ["calculated"],
            "allowed_confirm_statuses": ["posted"],
        }
    ]
    for row in bank_exports:
        operations.append(
            {
                "key": f"bank-import:{row['bank_account_code']}",
                "kind": "bank_import",
                "relative_path": row["relative_path"],
                "bank_account_code": row["bank_account_code"],
                "column_mapping": row["column_mapping"],
                "expected_sha256": row["sha256"],
                "expected_row_count": row["row_count"],
            }
        )
    return operations


def _bank_reconciliation_operations(
    session: Session,
    *,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = _query_rows(
        session,
        """
        SELECT reconciliation.id, period.calendar_year, period.calendar_month,
               reconciliation.bank_account_code,
               reconciliation.coverage_start_date,
               reconciliation.coverage_end_date,
               reconciliation.statement_opening_balance_fen,
               reconciliation.statement_closing_balance_fen,
               reconciliation.version
          FROM bank_reconciliations AS reconciliation
          JOIN accounting_periods AS period
            ON period.org_id=reconciliation.org_id
           AND period.id=reconciliation.period_id
         WHERE reconciliation.org_id=:org_id
           AND reconciliation.version=(
               SELECT MAX(newer.version)
                 FROM bank_reconciliations AS newer
                WHERE newer.org_id=reconciliation.org_id
                  AND newer.period_id=reconciliation.period_id
                  AND newer.bank_account_code=reconciliation.bank_account_code)
         ORDER BY period.calendar_year, period.calendar_month,
                  reconciliation.bank_account_code
        """,
        org_id=org_id,
    )
    operations: list[dict[str, Any]] = []
    for row in rows:
        month = f"{row['calendar_year']:04d}-{row['calendar_month']:02d}"
        evidence = _evidence_refs_for(
            session,
            table="bank_reconciliation_evidence",
            owner_column="reconciliation_id",
            owner_id=row["id"],
            evidence_by_id=maps["evidence"],
        )
        request = {
            "org_id": "${ORG_ID}",
            "period_id": {"$ref": "period", "period_month": month},
            "bank_account_code": row["bank_account_code"],
            "coverage_start_date": row["coverage_start_date"],
            "coverage_end_date": row["coverage_end_date"],
            "statement_opening_balance_fen": row["statement_opening_balance_fen"],
            "statement_closing_balance_fen": row["statement_closing_balance_fen"],
            "statement_import_action_ids": {
                "$ref": "bank_import_actions_for_period",
                "bank_account_code": row["bank_account_code"],
                "coverage_start_date": row["coverage_start_date"],
                "coverage_end_date": row["coverage_end_date"],
            },
            "statement_evidence_references": evidence,
            "difference_explanations": [],
        }
        stable_key = f"bank-reconciliation:{month}:{row['bank_account_code']}"
        operations.append(
            {
                "key": stable_key,
                "kind": "preview_confirm",
                "preview_tool": "finance_preview_bank_reconciliation",
                "confirm_tool": "finance_confirm_bank_reconciliation",
                "preview_request": request,
                "confirm_request": {"idempotency_key": _replay_idempotency(stable_key)},
                "allowed_preview_statuses": ["calculated"],
                "allowed_confirm_statuses": ["posted"],
            }
        )
    return operations


def _financial_statement_operations(
    session: Session,
    *,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    opening_rows = _query_rows(
        session,
        "SELECT establishment_date, treatment, confirmation_note, evidence_references "
        "FROM financial_statement_opening_balance_confirmations "
        "WHERE org_id=:org_id ORDER BY created_at",
        org_id=org_id,
    )
    for row in opening_rows:
        operations.append(
            {
                "key": "financial-statement-opening:zero-on-establishment",
                "kind": "tool",
                "tool": "finance_confirm_financial_statement_opening_balance",
                "request": {
                    "org_id": "${ORG_ID}",
                    "establishment_date": row["establishment_date"],
                    "treatment": row["treatment"],
                    "idempotency_key": _replay_idempotency(
                        "financial-statement-opening:zero-on-establishment"
                    ),
                    "confirmation_note": row["confirmation_note"],
                    "evidence_references": [
                        maps["evidence"][str(item)] for item in row["evidence_references"]
                    ],
                },
                "allowed_statuses": ["posted"],
            }
        )

    classification_rows = _query_rows(
        session,
        """
        SELECT classification.id, classification.allocations,
               classification.confirmation_note,
               classification.evidence_references,
               line.line_number, component.key AS component_key, account.code AS account_code,
               line.debit_fen, line.credit_fen, event.idempotency_key
          FROM financial_statement_classifications AS classification
          JOIN voucher_lines AS line ON line.id=classification.voucher_line_id
          JOIN accounts AS account ON account.id=line.account_id
          JOIN business_event_components AS component ON component.id=line.component_id
          JOIN vouchers AS voucher ON voucher.id=line.voucher_id
          JOIN business_events AS event ON event.id=voucher.event_id
         WHERE classification.org_id=:org_id
           AND NOT EXISTS (
               SELECT 1 FROM financial_statement_classifications AS successor
                WHERE successor.org_id=classification.org_id
                  AND successor.supersedes_id=classification.id)
         ORDER BY event.posting_date, event.created_at, line.line_number
        """,
        org_id=org_id,
    )
    for row in classification_rows:
        replay_key = _semantic_replay_key(str(row["idempotency_key"]))
        stable_key = f"financial-classification:{replay_key}:{row['line_number']}"
        operations.append(
            {
                "key": stable_key,
                "kind": "tool",
                "tool": "finance_confirm_financial_statement_classification",
                "request": {
                    "org_id": "${ORG_ID}",
                    "voucher_line_id": {
                        "$ref": "voucher_line",
                        "source_replay_key": replay_key,
                        "component_key": row["component_key"],
                        "account_code": row["account_code"],
                        "debit_fen": row["debit_fen"],
                        "credit_fen": row["credit_fen"],
                    },
                    "allocations": row["allocations"],
                    "supersedes_classification_id": None,
                    "idempotency_key": _replay_idempotency(stable_key),
                    "confirmation_note": row["confirmation_note"],
                    "evidence_references": [
                        maps["evidence"][str(item)] for item in row["evidence_references"]
                    ],
                },
                "allowed_statuses": ["posted"],
            }
        )

    tax_rows = _query_rows(
        session,
        """
        SELECT calendar_year, calendar_quarter, treatment, amount_fen,
               posting_date, confirmation_note, evidence_references
          FROM enterprise_income_tax_quarter_confirmations
         WHERE org_id=:org_id ORDER BY calendar_year, calendar_quarter
        """,
        org_id=org_id,
    )
    for row in tax_rows:
        stable_key = (
            f"enterprise-income-tax:{row['calendar_year']}-"
            f"Q{row['calendar_quarter']}:{row['treatment']}"
        )
        operations.append(
            {
                "key": stable_key,
                "kind": "tool",
                "tool": "finance_confirm_enterprise_income_tax_quarter",
                "request": {
                    "org_id": "${ORG_ID}",
                    "year": row["calendar_year"],
                    "quarter": row["calendar_quarter"],
                    "treatment": row["treatment"],
                    "amount_fen": row["amount_fen"],
                    "posting_date": row["posting_date"],
                    "idempotency_key": _replay_idempotency(stable_key),
                    "confirmation_note": row["confirmation_note"],
                    "evidence_references": [
                        maps["evidence"][str(item)] for item in row["evidence_references"]
                    ],
                },
                "allowed_statuses": ["posted"],
            }
        )
    return operations


def _income_tax_operations(
    session: Session,
    *,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replay the complete CIT chain, including zero facts and replaced assessments."""
    operations = []
    roots = _query_rows(
        session,
        "SELECT * FROM enterprise_income_tax_quarter_confirmations WHERE org_id=:org_id "
        "AND business_event_id IS NULL "
        "ORDER BY created_at, calendar_year, calendar_quarter",
        org_id=org_id,
    )
    for row in roots:
        key = (
            f"enterprise-income-tax:{row['calendar_year']}-"
            f"Q{row['calendar_quarter']}:{row['treatment']}"
        )
        operations.append(
            {
                "key": key,
                "kind": "tool",
                "source_created_at": str(row["created_at"]),
                "tool": "finance_confirm_enterprise_income_tax_quarter",
                "request": {
                    "org_id": "${ORG_ID}",
                    "year": row["calendar_year"],
                    "quarter": row["calendar_quarter"],
                    "treatment": row["treatment"],
                    "amount_fen": row["amount_fen"],
                    "posting_date": row["posting_date"],
                    "confirmation_note": row["confirmation_note"],
                    "evidence_references": [
                        maps["evidence"][str(v)] for v in row["evidence_references"]
                    ],
                    "idempotency_key": _replay_idempotency(key),
                },
                "allowed_statuses": ["posted"],
            }
        )
    results = _query_rows(
        session,
        "SELECT * FROM enterprise_income_tax_results WHERE org_id=:org_id "
        "AND business_event_id IS NULL "
        "ORDER BY created_at, revision",
        org_id=org_id,
    )
    for row in results:
        key = "cit-result:" + _semantic_replay_key(row["idempotency_key"])
        request = _replace_stable_references(row["input_facts"], org_id=org_id, maps=maps)
        request.pop("idempotency_key", None)
        request.pop("calculation_hash", None)
        operations.append(
            {
                "key": key,
                "kind": "preview_confirm",
                "source_created_at": str(row["created_at"]),
                "preview_tool": "finance_preview_enterprise_income_tax_result",
                "confirm_tool": "finance_confirm_enterprise_income_tax_result",
                "preview_request": request,
                "confirm_request": {"idempotency_key": _replay_idempotency(key)},
                "allowed_preview_statuses": ["calculated"],
                "allowed_confirm_statuses": ["posted"],
            }
        )
    return sorted(operations, key=lambda v: v["source_created_at"])


def _income_tax_event_inventory(
    events: list[dict[str, Any]], maps: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep effective CIT events in the package's independently checked inventory."""
    inventory = []
    for event in events:
        components = event["facts"].get("components", [])
        if event["event_type"] not in {
            "enterprise_income_tax_assessment",
            "enterprise_income_tax_result",
        } and not any(
            c.get("kind") in {"enterprise_income_tax_assessment", "enterprise_income_tax_result"}
            or (c.get("kind") == "tax_settlement" and c.get("tax_type") == "enterprise_income_tax")
            for c in components
        ):
            continue
        reference = maps["event"].get(str(event["id"]), {})
        key = reference.get("operation_key") or _semantic_replay_key(event["idempotency_key"])
        inventory.append(
            {
                "replay_key": key,
                "source_event_type": event["event_type"],
                "source_business_date": event["business_date"],
                "source_posting_date": event["posting_date"],
                "source_fact_sha256": hashlib.sha256(
                    json.dumps(_jsonable(event["facts"]), sort_keys=True).encode("utf-8")
                ).hexdigest(),
            }
        )
    return inventory


def _period_close_operations(
    session: Session,
    *,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = _query_rows(
        session,
        """
        SELECT period.calendar_year, period.calendar_month, period.end_date,
               action.id AS action_id, action.input_facts,
               action.confirmation_note, commentary.commentary
          FROM accounting_periods AS period
          JOIN accounting_period_closes AS close
            ON close.org_id=period.org_id AND close.id=period.close_id
          JOIN accounting_period_actions AS action
            ON action.org_id=close.org_id AND action.id=close.action_id
          JOIN accounting_period_close_commentaries AS commentary
            ON commentary.org_id=close.org_id AND commentary.close_id=close.id
         WHERE period.org_id=:org_id AND period.status='closed'
         ORDER BY period.calendar_year, period.calendar_month
        """,
        org_id=org_id,
    )
    operations: list[dict[str, Any]] = []
    for row in rows:
        month = f"{row['calendar_year']:04d}-{row['calendar_month']:02d}"
        evidence = _evidence_refs_for(
            session,
            table="accounting_period_action_evidence",
            owner_column="action_id",
            owner_id=row["action_id"],
            evidence_by_id=maps["evidence"],
        )
        if not evidence:
            evidence = [next(iter(maps["evidence"].values()))]
        review_facts = (row["input_facts"] or {}).get("review_facts") or {
            "voucher_completeness_reviewed": True,
            "bank_reconciliation_reviewed": True,
            "open_items_reviewed": True,
            "payroll_and_statutory_items_reviewed": True,
            "tax_items_reviewed": True,
            "asset_and_borrowing_schedules_reviewed": True,
        }
        operations.append(
            {
                "key": f"period-close:{month}",
                "kind": "period_close",
                "preview_request": {
                    "org_id": "${ORG_ID}",
                    "period_id": {"$ref": "period", "period_month": month},
                    "closing_date": row["end_date"],
                },
                "management_commentary": row["commentary"],
                "review_facts": review_facts,
                "confirmation_note": row["confirmation_note"],
                "evidence_references": evidence,
            }
        )
    return operations


def _snapshot_evidence_refs(
    snapshot: Sequence[Mapping[str, Any]],
    maps: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    by_sha = {str(item["sha256"]): item for item in maps["evidence"].values()}
    for item in snapshot:
        sha = str(item.get("sha256", ""))
        if sha and sha in by_sha:
            result.append(by_sha[sha])
    return result


def _external_obligation_identity(
    session: Session,
    *,
    org_id: uuid.UUID,
    obligation_id: uuid.UUID,
    code: str,
    scope: str,
) -> str:
    from .owner_workflow import _OBLIGATION_NAMESPACE

    identities: list[str]
    if scope == "month":
        identities = [
            f"{row['calendar_year']:04d}-{row['calendar_month']:02d}"
            for row in _query_rows(
                session,
                "SELECT calendar_year, calendar_month FROM accounting_periods "
                "WHERE org_id=:org_id ORDER BY calendar_year, calendar_month",
                org_id=org_id,
            )
        ]
    elif scope == "quarter":
        identities = sorted(
            {
                f"{row['calendar_year']:04d}-Q{(row['calendar_month'] - 1) // 3 + 1}"
                for row in _query_rows(
                    session,
                    "SELECT calendar_year, calendar_month FROM accounting_periods "
                    "WHERE org_id=:org_id",
                    org_id=org_id,
                )
            }
        )
    elif scope == "year":
        identities = [str(year) for year in range(1900, datetime.now(UTC).year + 1)]
    else:
        raise ReplayError("REPLAY_SOURCE_OBLIGATION_SCOPE_UNSUPPORTED")
    for identity in identities:
        candidate = uuid.uuid5(_OBLIGATION_NAMESPACE, f"{org_id}:{code}:{scope}:{identity}")
        if candidate == obligation_id:
            return identity
    raise ReplayError("REPLAY_SOURCE_OBLIGATION_IDENTITY_UNRESOLVED")


def _owner_control_operations(
    session: Session,
    *,
    org_id: uuid.UUID,
    maps: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    period_rows = _query_rows(
        session,
        """
        SELECT confirmation.fact_type, confirmation.confirmation_state,
               confirmation.confirmation_note, confirmation.evidence_snapshot,
               period.calendar_year, period.calendar_month
          FROM owner_period_confirmations AS confirmation
          JOIN accounting_periods AS period
            ON period.org_id=confirmation.org_id
           AND period.id=confirmation.period_id
         WHERE confirmation.org_id=:org_id
           AND NOT EXISTS (
               SELECT 1 FROM owner_period_confirmations AS successor
                WHERE successor.org_id=confirmation.org_id
                  AND successor.supersedes_id=confirmation.id)
         ORDER BY period.calendar_year, period.calendar_month,
                  confirmation.fact_type
        """,
        org_id=org_id,
    )
    for row in period_rows:
        month = f"{row['calendar_year']:04d}-{row['calendar_month']:02d}"
        fact_type = str(row["fact_type"])
        stable_key = f"owner-control:{fact_type}:{month}"
        operations.append(
            {
                "key": stable_key,
                "kind": "owner_control",
                "control": fact_type,
                "period_month": month,
                "confirmation_state": row["confirmation_state"],
                "confirmation_note": row["confirmation_note"],
                "evidence_references": _snapshot_evidence_refs(
                    row["evidence_snapshot"] or [], maps
                ),
                "idempotency_key": _replay_idempotency(stable_key),
            }
        )

    assessment_rows = _query_rows(
        session,
        """
        SELECT confirmation.declaration_status, confirmation.declaration_date,
               confirmation.external_reference, confirmation.confirmation_note,
               confirmation.evidence_snapshot,
               period.calendar_year, period.calendar_month
          FROM payroll_contribution_assessment_confirmations AS confirmation
          JOIN accounting_periods AS period
            ON period.org_id=confirmation.org_id
           AND period.id=confirmation.period_id
         WHERE confirmation.org_id=:org_id
           AND NOT EXISTS (
               SELECT 1 FROM payroll_contribution_assessment_confirmations AS successor
                WHERE successor.org_id=confirmation.org_id
                  AND successor.supersedes_id=confirmation.id)
         ORDER BY period.calendar_year, period.calendar_month
        """,
        org_id=org_id,
    )
    for row in assessment_rows:
        month = f"{row['calendar_year']:04d}-{row['calendar_month']:02d}"
        stable_key = f"owner-control:contribution-assessment:{month}"
        operations.append(
            {
                "key": stable_key,
                "kind": "owner_control",
                "control": "contribution_assessment",
                "period_month": month,
                "declaration_status": row["declaration_status"],
                "declaration_date": row["declaration_date"],
                "external_reference": row["external_reference"],
                "confirmation_note": row["confirmation_note"],
                "evidence_references": _snapshot_evidence_refs(
                    row["evidence_snapshot"] or [], maps
                ),
                "idempotency_key": _replay_idempotency(stable_key),
            }
        )

    external_rows = _query_rows(
        session,
        """
        SELECT obligation_id, obligation_code, obligation_scope,
               completion_status, completion_date, external_reference,
               confirmation_note, evidence_snapshot
          FROM external_obligation_confirmations AS confirmation
         WHERE confirmation.org_id=:org_id
           AND NOT EXISTS (
               SELECT 1 FROM external_obligation_confirmations AS successor
                WHERE successor.org_id=confirmation.org_id
                  AND successor.supersedes_id=confirmation.id)
         ORDER BY obligation_code, obligation_id
        """,
        org_id=org_id,
    )
    for row in external_rows:
        identity = _external_obligation_identity(
            session,
            org_id=org_id,
            obligation_id=row["obligation_id"],
            code=str(row["obligation_code"]),
            scope=str(row["obligation_scope"]),
        )
        stable_key = f"owner-control:external:{row['obligation_code']}:{identity}"
        operations.append(
            {
                "key": stable_key,
                "kind": "owner_control",
                "control": "external_obligation",
                "obligation_code": row["obligation_code"],
                "obligation_scope": row["obligation_scope"],
                "scope_identity": identity,
                "completion_status": row["completion_status"],
                "completion_date": row["completion_date"],
                "external_reference": row["external_reference"],
                "confirmation_note": row["confirmation_note"],
                "evidence_references": _snapshot_evidence_refs(
                    row["evidence_snapshot"] or [], maps
                ),
                "idempotency_key": _replay_idempotency(stable_key),
            }
        )

    historical_rows = _query_rows(
        session,
        """
        SELECT obligation_code, completion_through_identity,
               completion_date_status, confirmation_note, evidence_snapshot
          FROM historical_obligation_completion_confirmations AS confirmation
         WHERE confirmation.org_id=:org_id
           AND NOT EXISTS (
               SELECT 1 FROM historical_obligation_completion_confirmations AS successor
                WHERE successor.org_id=confirmation.org_id
                  AND successor.supersedes_id=confirmation.id)
         ORDER BY obligation_code
        """,
        org_id=org_id,
    )
    for row in historical_rows:
        stable_key = f"owner-control:history:{row['obligation_code']}"
        operations.append(
            {
                "key": stable_key,
                "kind": "owner_control",
                "control": "historical_obligation",
                "obligation_code": row["obligation_code"],
                "completion_through_identity": row["completion_through_identity"],
                "completion_date_status": row["completion_date_status"],
                "confirmation_note": row["confirmation_note"],
                "evidence_references": _snapshot_evidence_refs(
                    row["evidence_snapshot"] or [], maps
                ),
                "idempotency_key": _replay_idempotency(stable_key),
            }
        )
    return operations


def _metadata_operations(session, org_id, maps):
    """Replay management history separately, after the accounting snapshot is closed."""
    from .models import BusinessEvent, BusinessMetadataVersion

    rows = session.execute(
        select(BusinessMetadataVersion, BusinessEvent.idempotency_key)
        .join(
            BusinessEvent,
            BusinessEvent.id == BusinessMetadataVersion.event_id,
        )
        .where(
            BusinessMetadataVersion.org_id == org_id,
            BusinessEvent.status == "posted",
            BusinessEvent.reversed_by_event_id.is_(None),
        )
        .order_by(
            BusinessMetadataVersion.event_id,
            BusinessMetadataVersion.component_key,
            BusinessMetadataVersion.version,
        )
    ).all()
    previous = {}
    operations = []
    for row, event_key in rows:
        identity = (row.event_id, row.component_key)
        values = _replace_stable_references(row.metadata_values, org_id=org_id, maps=maps)
        patch = {key: None for key in previous.get(identity, {}) if key not in values} | values
        previous[identity] = values
        key = f"metadata:{_semantic_replay_key(event_key)}:{row.component_key}:{row.version}"
        operations.append(
            {
                "key": key,
                "kind": "tool",
                "tool": "finance_update_business_metadata",
                "request": {
                    "org_id": "${ORG_ID}",
                    "source": {
                        "event_key": _replay_idempotency(_semantic_replay_key(event_key)),
                        "component_key": row.component_key,
                    },
                    "metadata": patch,
                    "expected_version": row.version - 1,
                    "idempotency_key": _replay_idempotency(key),
                },
                "allowed_statuses": ["updated"],
            }
        )
    return operations


def _export_company(
    *,
    engine: sa.Engine,
    registry: Mapping[str, Any],
    package_root: Path,
) -> dict[str, Any]:
    org_id = uuid.UUID(str(registry["org_id"]))
    directory = f"companies/{org_id}"
    company_dir = package_root / directory
    company_dir.mkdir(parents=True)
    with Session(engine) as session:
        revision = session.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != _current_schema_revision(catalog=False):
            raise ReplayError("REPLAY_SOURCE_COMPONENT_BASELINE_REQUIRED")
        organization = session.get(Organization, org_id)
        if organization is None:
            raise ReplayError("REPLAY_SOURCE_ORGANIZATION_MISSING")
        profile = session.scalar(
            select(OrganizationProfileVersion)
            .where(OrganizationProfileVersion.org_id == org_id)
            .order_by(OrganizationProfileVersion.effective_from.asc())
            .limit(1)
        )
        if profile is None:
            raise ReplayError("REPLAY_SOURCE_PROFILE_MISSING")
        maps = _stable_maps(session, org_id)
        evidence = _export_evidence(session, org_id=org_id, company_dir=company_dir)
        if not evidence:
            raise ReplayError("REPLAY_SOURCE_EVIDENCE_REQUIRED")
        bank_exports = _export_bank_transactions(session, org_id=org_id, company_dir=company_dir)
        accounts = _account_controls(session, org_id)
        events = _effective_events(session, org_id)
        typed_events = [
            _event_operation(session, event, org_id=org_id, maps=maps)
            for event in events
            if maps["event"][str(event["id"])].get("$ref") == "event"
        ]
        checkpoints = _company_checkpoints(session, org_id)
        account_balances = _account_balance_projection(session, org_id)
        open_items = _open_item_projection(session, org_id=org_id)
        support_evidence = maps["evidence"][
            str(
                session.scalar(
                    select(Evidence.id)
                    .where(Evidence.org_id == org_id)
                    .order_by(Evidence.created_at, Evidence.id)
                    .limit(1)
                )
            )
        ]
        setup_operations = _setup_operations(session, org_id, maps)
        deferred_policy_successors = [
            operation
            for operation in setup_operations
            if operation.get("tool") == "finance_register_payroll_policy_version"
            and operation.get("request", {}).get("supersedes_policy_version_id") is not None
        ]
        initial_setup_operations = [
            operation
            for operation in setup_operations
            if operation not in deferred_policy_successors
        ]
        payroll_fact_operations = _payroll_fact_operations(session, org_id=org_id, maps=maps)
        undated_payroll_facts = [
            operation
            for operation in payroll_fact_operations
            if not str(operation.get("key", "")).startswith("contribution-actual:")
        ]
        dated_payroll_facts = [
            operation
            for operation in payroll_fact_operations
            if operation not in undated_payroll_facts
        ]
        successors = [
            (
                date.fromisoformat(str(operation["request"]["effective_from"])),
                operation,
            )
            for operation in sorted(
                deferred_policy_successors,
                key=lambda item: (
                    str(item["request"]["effective_from"]),
                    str(item["key"]),
                ),
            )
        ]
        actual_buckets: dict[int, list[dict[str, Any]]] = {
            index: [] for index in range(-1, len(successors))
        }
        for operation in dated_payroll_facts:
            year, month = (
                int(part) for part in str(operation["request"]["contribution_period"]).split("-")
            )
            period_end = date(year, month, calendar.monthrange(year, month)[1])
            bucket = max(
                (
                    index
                    for index, (effective_from, _operation) in enumerate(successors)
                    if effective_from <= period_end
                ),
                default=-1,
            )
            actual_buckets[bucket].append(operation)
        business_timeline = list(actual_buckets[-1])
        next_successor = 0
        for operation in typed_events:
            if operation["source_event_type"] == "payroll_accrual":
                posting_date = date.fromisoformat(str(operation["source_posting_date"]))
                while (
                    next_successor < len(successors)
                    and successors[next_successor][0] <= posting_date
                ):
                    business_timeline.append(successors[next_successor][1])
                    business_timeline.extend(actual_buckets[next_successor])
                    next_successor += 1
            business_timeline.append(operation)
        while next_successor < len(successors):
            business_timeline.append(successors[next_successor][1])
            business_timeline.extend(actual_buckets[next_successor])
            next_successor += 1
        # Preserve existing setup/event order while inserting CIT facts where they
        # were originally confirmed. Financial classifications still follow events.
        anchored = []
        anchor = ""
        for index, operation in enumerate(business_timeline):
            anchor = str(operation.get("source_created_at", anchor))
            anchored.append((anchor, 1, index, operation))
        for index, operation in enumerate(
            _income_tax_operations(session, org_id=org_id, maps=maps)
        ):
            anchored.append((operation["source_created_at"], 0, index, operation))
        business_timeline = [item[3] for item in sorted(anchored, key=lambda v: v[:3])]
        operations = [
            *_evidence_operations(evidence),
            *_period_operations(session, org_id=org_id, support_evidence=support_evidence),
            *initial_setup_operations,
            *undated_payroll_facts,
            *_bank_operations(bank_exports, accounts=accounts, support_evidence=support_evidence),
            *business_timeline,
            *(
                operation
                for operation in _financial_statement_operations(session, org_id=org_id, maps=maps)
                if operation.get("tool") != "finance_confirm_enterprise_income_tax_quarter"
            ),
            *_bank_reconciliation_operations(session, org_id=org_id, maps=maps),
            *_owner_control_operations(session, org_id=org_id, maps=maps),
            *_period_close_operations(session, org_id=org_id, maps=maps),
            *_metadata_operations(session, org_id, maps),
        ]
        descriptor = {
            "format_version": _FORMAT_VERSION,
            "org_id": str(org_id),
            "organization": {
                "name": organization.name,
                "taxpayer_identification_number": organization.taxpayer_identification_number,
                "taxpayer_type": organization.taxpayer_type,
                "filing_cycle": organization.filing_cycle,
                "jurisdiction": organization.jurisdiction,
                "urban_maintenance_rate": str(organization.urban_maintenance_rate),
                "accounting_standard": organization.accounting_standard,
                "profile_effective_from": profile.effective_from.isoformat(),
                "profile_confirmation_note": profile.confirmation_note,
            },
            "source_projection": {
                "status": registry["status"],
                "is_primary": bool(registry["is_primary"]),
                "close_backup_directory": registry["close_backup_directory"],
            },
            "accounts": _jsonable(accounts),
            "checkpoints": checkpoints,
            "verification_files": {
                "account_balances": "account-balances.json",
                "open_items": "open-items.json",
                "report_preconditions": "report-preconditions.json",
            },
            "operation_count": len(operations),
        }
        _write_json(company_dir / "company.json", descriptor)
        _write_jsonl(
            company_dir / "typed-events.jsonl",
            typed_events + _income_tax_event_inventory(events, maps),
        )
        _write_jsonl(company_dir / "operations.jsonl", operations)
        _write_json(company_dir / "checkpoints.json", checkpoints)
        _write_json(company_dir / "account-balances.json", account_balances)
        _write_json(company_dir / "open-items.json", open_items)
        _write_json(
            company_dir / "report-preconditions.json",
            {
                "closed_periods": checkpoints["closed_periods"],
                "open_periods": checkpoints["open_periods"],
                "bank_reconciliation_count": checkpoints["bank_reconciliation_count"],
                "financial_statement_classification_count": checkpoints[
                    "financial_statement_classification_count"
                ],
                "enterprise_income_tax_confirmation_count": checkpoints[
                    "enterprise_income_tax_confirmation_count"
                ],
            },
        )
        return {
            "org_id": str(org_id),
            "directory": directory,
            "display_name": organization.name,
            "taxpayer_identification_number": organization.taxpayer_identification_number,
            "is_primary": bool(registry["is_primary"]),
            "checkpoints": checkpoints,
        }


def _readonly_source_engine(url: str | URL) -> sa.Engine:
    if make_url(url).get_backend_name() != "postgresql":
        raise ReplayError("REPLAY_SOURCE_POSTGRESQL_REQUIRED")
    return create_engine(
        url,
        connect_args={"options": "-c default_transaction_read_only=on"},
        isolation_level="REPEATABLE READ",
    )


def export_system(output: Path) -> dict[str, Any]:
    target = output.resolve()
    if target.exists():
        raise ReplayError("REPLAY_EXPORT_TARGET_ALREADY_EXISTS")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    settings = get_settings()
    catalog_engine = _readonly_source_engine(settings.database_url)
    try:
        with Session(catalog_engine) as session:
            tables = set(sa_inspect(session.bind).get_table_names())
            if not _SENSITIVE_TABLES <= tables:
                raise ReplayError("REPLAY_SOURCE_CATALOG_INVALID")
            registries = _query_rows(
                session,
                """
                SELECT registry.org_id, registry.database_name, registry.status,
                       registry.display_name, registry.taxpayer_identification_number,
                       registry.is_primary,
                       (SELECT location.backup_directory
                          FROM close_backup_location_versions AS location
                         WHERE location.org_id=registry.org_id
                         ORDER BY location.version DESC, location.created_at DESC,
                                  location.id DESC
                         LIMIT 1) AS close_backup_directory
                  FROM company_registry AS registry
                 WHERE status IN ('active','archived')
                 ORDER BY registry.is_primary DESC, registry.display_name, registry.org_id
                """,
            )
        if not registries:
            raise ReplayError("REPLAY_SOURCE_COMPANIES_MISSING")
        companies: list[dict[str, Any]] = []
        for registry in registries:
            base = settings.finance_migration_database_url or settings.finance_company_database_url
            if base is None:
                raise ReplayError("REPLAY_SOURCE_COMPANY_DATABASE_URL_REQUIRED")
            company_engine = _readonly_source_engine(
                _database_url_for_name(base, str(registry["database_name"]))
            )
            try:
                companies.append(
                    _export_company(
                        engine=company_engine,
                        registry=registry,
                        package_root=target,
                    )
                )
            finally:
                company_engine.dispose()
        total_events = sum(
            int(company["checkpoints"]["effective_event_count"]) for company in companies
        )
        total_vouchers = sum(
            int(company["checkpoints"]["effective_voucher_count"]) for company in companies
        )
        system = {
            "format_version": _FORMAT_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "source_policy": {
                "catalog_and_business_databases_read_only": True,
                "technical_ids_regenerated": True,
                "organization_ids_preserved": True,
                "evidence_content_hashes_preserved": True,
                "arbitrary_voucher_lines_forbidden": True,
                "failed_and_rejected_actions_excluded": True,
            },
            "baseline_revisions": {
                "catalog": _CATALOG_REVISION,
                "business": _BUSINESS_REVISION,
            },
            "companies": companies,
            "checkpoints": {
                "company_count": len(companies),
                "effective_event_count": total_events,
                "effective_voucher_count": total_vouchers,
            },
        }
        _write_json(target / "system.json", system)
        manifest_sha256 = _seal_package(target)
        return {
            "status": "exported",
            "package": str(target),
            "company_count": len(companies),
            "effective_event_count": total_events,
            "effective_voucher_count": total_vouchers,
            "manifest_sha256": manifest_sha256,
        }
    except Exception:
        # The incomplete directory is deliberately retained for diagnosis; it is
        # never mistaken for a valid package because it lacks a verified manifest.
        raise
    finally:
        catalog_engine.dispose()


def _migration_config(config_file: str, url: URL) -> Config:
    config = Config(str(_ROOT / config_file))
    rendered = _render_url(url)
    config.set_main_option("sqlalchemy.url", rendered.replace("%", "%%"))
    config.attributes["database_url_override"] = rendered
    return config


def _database_exists(provisioning_engine: sa.Engine, database_name: str) -> bool:
    with provisioning_engine.connect() as connection:
        return (
            connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname=:database_name"),
                {"database_name": database_name},
            )
            is not None
        )


def _create_empty_database(provisioning_engine: sa.Engine, database_name: str) -> None:
    if not database_name or not database_name.replace("_", "").isalnum():
        raise ReplayError("REPLAY_TARGET_DATABASE_NAME_INVALID")
    with provisioning_engine.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')


def _assert_database_empty(url: URL) -> None:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            tables = set(sa_inspect(connection).get_table_names(schema="public"))
            if tables:
                raise ReplayError("REPLAY_TARGET_DATABASE_NOT_EMPTY")
    finally:
        engine.dispose()


def _ensure_empty_database(
    provisioning_engine: sa.Engine,
    *,
    url: URL,
) -> bool:
    database_name = url.database
    if not database_name:
        raise ReplayError("REPLAY_TARGET_DATABASE_NAME_INVALID")
    if _database_exists(provisioning_engine, database_name):
        _assert_database_empty(url)
        return False
    _create_empty_database(provisioning_engine, database_name)
    return True


def _grant_runtime_access(database_url: URL, runtime_role: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            grant_runtime_database_access(connection, runtime_role)
    finally:
        engine.dispose()


def _apply_account_controls(
    session: Session,
    *,
    org_id: uuid.UUID,
    controls: Sequence[Mapping[str, Any]],
) -> None:
    by_code = {
        item.code: item for item in session.scalars(select(Account).where(Account.org_id == org_id))
    }
    for control in controls:
        code = str(control["code"])
        business_class = control.get("business_class")
        if not business_class:
            raise ReplayError("REPLAY_ACCOUNT_BUSINESS_CLASS_REQUIRED")
        rule_account = get_business_class_template(session, org_id, str(business_class))
        if (control["category"], control["normal_side"]) != (
            rule_account.category,
            rule_account.normal_side,
        ):
            raise ReplayError("REPLAY_ACCOUNT_BUSINESS_CLASS_MISMATCH")
        account = by_code.get(code)
        if account is None:
            account = Account(
                org_id=org_id,
                code=code,
                name=str(control["name"]),
                category=str(control["category"]),
                normal_side=str(control["normal_side"]),
                system_role=control.get("system_role"),
                business_class=str(business_class),
            )
            session.add(account)
            by_code[code] = account
        else:
            if (
                account.category != control["category"]
                or account.normal_side != control["normal_side"]
                or account.system_role != control.get("system_role")
                or account.business_class != business_class
            ):
                raise ReplayError("REPLAY_ACCOUNT_CONTROL_CONFLICT")
            account.name = str(control["name"])
        account.active = bool(control["active"])
        # The public scope confirmation later creates the immutable scope action
        # and applies these same fields. Keep preparation free of business audit.
        account.requires_bank_reconciliation = False
        account.bank_reconciliation_start_date = None
        account.bank_reconciliation_end_date = None
        account.bank_reconciliation_configured_at = None


def _initialize_empty_company(
    *,
    company_url: URL,
    catalog_id: uuid.UUID,
    database_identity: uuid.UUID,
    descriptor: Mapping[str, Any],
) -> None:
    org_id = uuid.UUID(str(descriptor["org_id"]))
    organization = descriptor["organization"]
    engine = create_engine(company_url)
    try:
        with Session(engine) as session, session.begin():
            if session.scalar(select(func.count(Organization.id))) != 0:
                raise ReplayError("REPLAY_TARGET_COMPANY_NOT_EMPTY")
            session.execute(text("ALTER TABLE accounts DISABLE TRIGGER USER"))
            seeded = seed_organization(
                session,
                org_id=org_id,
                name=str(organization["name"]),
                taxpayer_identification_number=str(organization["taxpayer_identification_number"]),
                filing_cycle=str(organization["filing_cycle"]),
                jurisdiction=str(organization["jurisdiction"]),
                urban_maintenance_rate=Decimal(str(organization["urban_maintenance_rate"])),
            )
            session.add(
                OrganizationDatabaseMetadata(
                    singleton_key=1,
                    org_id=org_id,
                    database_identity=database_identity,
                    current_catalog_instance_id=catalog_id,
                    owner_approval_required=True,
                )
            )
            session.flush()
            session.execute(text("ALTER TABLE organization_profile_versions DISABLE TRIGGER USER"))
            session.add(
                OrganizationProfileVersion(
                    org_id=org_id,
                    effective_from=date.fromisoformat(str(organization["profile_effective_from"])),
                    name=str(organization["name"]),
                    taxpayer_identification_number=str(
                        organization["taxpayer_identification_number"]
                    ),
                    taxpayer_type=str(organization["taxpayer_type"]),
                    filing_cycle=str(organization["filing_cycle"]),
                    jurisdiction=str(organization["jurisdiction"]),
                    urban_maintenance_rate=Decimal(str(organization["urban_maintenance_rate"])),
                    accounting_standard=str(organization["accounting_standard"]),
                    confirmation_note=str(organization["profile_confirmation_note"]),
                    lifecycle_action_id=None,
                    execution_attribution_id=None,
                )
            )
            session.flush()
            session.execute(text("ALTER TABLE organization_profile_versions ENABLE TRIGGER USER"))
            _apply_account_controls(
                session,
                org_id=seeded.id,
                controls=descriptor["accounts"],
            )
            session.flush()
            session.execute(text("ALTER TABLE accounts ENABLE TRIGGER USER"))
    finally:
        engine.dispose()


def _default_state_path(package: Path) -> Path:
    return package.parent / f".{package.name}.replay-state.json"


def prepare_empty(package: Path, state_path: Path | None = None) -> dict[str, Any]:
    verification = verify_package(package)
    package_root = package.resolve(strict=True)
    system = _load_json(package_root / "system.json")
    state_file = (state_path or _default_state_path(package_root)).resolve()
    if state_file.exists():
        raise ReplayError("REPLAY_STATE_ALREADY_EXISTS")
    settings = get_settings()
    catalog_url = make_url(settings.database_url)
    provisioning_url = settings.finance_provisioning_database_url
    migration_base = settings.finance_migration_database_url
    if provisioning_url is None or migration_base is None:
        raise ReplayError("REPLAY_TARGET_PROVISIONING_NOT_CONFIGURED")
    if catalog_url.get_backend_name() != "postgresql":
        raise ReplayError("REPLAY_TARGET_POSTGRESQL_REQUIRED")
    if not catalog_url.database:
        raise ReplayError("REPLAY_TARGET_DATABASE_NAME_INVALID")
    company_runtime_base = settings.finance_company_database_url
    if company_runtime_base is None:
        raise ReplayError("REPLAY_TARGET_RUNTIME_ROLE_NOT_CONFIGURED")
    catalog_runtime_role = catalog_url.username
    company_runtime_url = make_url(company_runtime_base)
    company_runtime_role = company_runtime_url.username
    if catalog_runtime_role is None or company_runtime_role is None:
        raise ReplayError("REPLAY_TARGET_RUNTIME_ROLE_NOT_CONFIGURED")
    catalog_migration_url = _migration_url_for_runtime_database(migration_base, catalog_url)
    provisioning_engine = create_engine(provisioning_url, isolation_level="AUTOCOMMIT")
    created_databases: list[str] = []
    try:
        if _ensure_empty_database(provisioning_engine, url=catalog_url):
            created_databases.append(str(catalog_url.database))
        catalog_id = uuid.uuid4()
        catalog_config = _migration_config("catalog_alembic.ini", catalog_migration_url)
        catalog_config.attributes["catalog_instance_id"] = catalog_id
        command.upgrade(catalog_config, "head")
        _grant_runtime_access(catalog_migration_url, catalog_runtime_role)
        catalog_engine = create_engine(catalog_url)
        company_states: list[dict[str, Any]] = []
        try:
            with Session(catalog_engine) as catalog_session, catalog_session.begin():
                for company in system["companies"]:
                    org_id = uuid.UUID(str(company["org_id"]))
                    company_dir = package_root / str(company["directory"])
                    descriptor = _load_json(company_dir / "company.json")
                    database_name = f"finance_company_{org_id.hex}"
                    database_identity = uuid.uuid4()
                    company_url = _database_url_for_name(migration_base, database_name)
                    if _ensure_empty_database(provisioning_engine, url=company_url):
                        created_databases.append(database_name)
                    business_config = _migration_config("alembic.ini", company_url)
                    business_config.attributes.update(
                        {
                            "company_org_id": org_id,
                            "company_database_identity": database_identity,
                            "catalog_instance_id": catalog_id,
                            "identity_split_verified": True,
                            "identity_export_verified": True,
                        }
                    )
                    command.upgrade(business_config, "head")
                    _grant_runtime_access(company_url, company_runtime_role)
                    _initialize_empty_company(
                        company_url=company_url,
                        catalog_id=catalog_id,
                        database_identity=database_identity,
                        descriptor=descriptor,
                    )
                    catalog_session.add(
                        CompanyRegistry(
                            org_id=org_id,
                            database_name=database_name,
                            database_identity=database_identity,
                            status="active",
                            display_name=str(descriptor["organization"]["name"]),
                            taxpayer_identification_number=str(
                                descriptor["organization"]["taxpayer_identification_number"]
                            ),
                            profile_effective_from=date.fromisoformat(
                                str(descriptor["organization"]["profile_effective_from"])
                            ),
                            filing_cycle=str(descriptor["organization"]["filing_cycle"]),
                            urban_maintenance_rate=Decimal(
                                str(descriptor["organization"]["urban_maintenance_rate"])
                            ),
                            is_primary=bool(company["is_primary"]),
                        )
                    )
                    company_states.append(
                        {
                            "org_id": str(org_id),
                            "database_name": database_name,
                            "database_identity": str(database_identity),
                            "completed_operations": [],
                            "operation_results": {},
                        }
                    )
        finally:
            catalog_engine.dispose()
        primary = next(item for item in system["companies"] if item["is_primary"])
        state = {
            "state_version": _STATE_VERSION,
            "phase": "prepared",
            "package_manifest_sha256": verification["manifest_sha256"],
            "catalog_instance_id": str(catalog_id),
            "catalog_database": catalog_url.database,
            "primary_org_id": str(primary["org_id"]),
            "companies": company_states,
            "created_databases": created_databases,
        }
        _write_json(state_file, state)
        security = _request_replay_security(state, settings)
        return {
            "status": "prepared",
            "state_file": str(state_file),
            "catalog_database": catalog_url.database,
            "company_count": len(company_states),
            "primary_org_id": state["primary_org_id"],
            "owner_security_window": security,
            "next_action": "complete_owner_security_window_then_replay",
        }
    finally:
        provisioning_engine.dispose()


class _ReplayResolver:
    def __init__(
        self,
        *,
        engine: sa.Engine,
        org_id: uuid.UUID,
        results: dict[str, dict[str, Any]],
    ) -> None:
        self.engine = engine
        self.org_id = org_id
        self.results = results

    def materialize(self, value: Any) -> Any:
        if value == "${ORG_ID}":
            return str(self.org_id)
        if isinstance(value, list):
            return [self.materialize(item) for item in value]
        if not isinstance(value, dict):
            return value
        ref_type = value.get("$ref")
        if ref_type is None:
            return {key: self.materialize(item) for key, item in value.items()}
        if ref_type == "operation_result":
            result = self.results.get(str(value["operation_key"]))
            if result is None or str(value["field"]) not in result:
                raise ReplayError("REPLAY_OPERATION_RESULT_REFERENCE_MISSING")
            return result[str(value["field"])]
        if ref_type == "prepared_line_key":
            result = self.results.get(str(value["operation_key"]))
            lines = result.get("data", {}).get("lines", []) if result else []
            line_kind = str(value["line_kind"])
            with Session(self.engine) as session:
                if line_kind == "payroll":
                    identity_id = session.scalar(
                        text(
                            "SELECT id FROM employees WHERE org_id=:org_id AND employee_code=:code"
                        ),
                        {"org_id": self.org_id, "code": value["identity_code"]},
                    )
                    identity_field = "employee_id"
                elif line_kind == "labor":
                    identity_id = session.scalar(
                        text(
                            "SELECT id FROM labor_service_persons WHERE org_id=:org_id "
                            "AND person_code=:code"
                        ),
                        {"org_id": self.org_id, "code": value["identity_code"]},
                    )
                    identity_field = "labor_person_id"
                else:
                    raise ReplayError("REPLAY_PREPARED_LINE_KIND_INVALID")
            match = next(
                (item for item in lines if str(item.get(identity_field)) == str(identity_id)),
                None,
            )
            if match is None or not match.get("id"):
                raise ReplayError("REPLAY_PREPARED_LINE_REFERENCE_MISSING")
            return f"salary:{match['id']}" if line_kind == "payroll" else str(match["id"])
        with Session(self.engine) as session:
            if ref_type == "evidence":
                result = session.scalar(
                    select(Evidence.id).where(
                        Evidence.org_id == self.org_id,
                        Evidence.sha256 == str(value["sha256"]),
                    )
                )
            elif ref_type == "employee":
                result = session.scalar(
                    text("SELECT id FROM employees WHERE org_id=:org_id AND employee_code=:code"),
                    {"org_id": self.org_id, "code": value["employee_code"]},
                )
            elif ref_type in {"bank_transaction", "bank_transaction_reference"}:
                row = session.execute(
                    text(
                        "SELECT id, fingerprint FROM bank_transactions WHERE org_id=:org_id "
                        "AND bank_account_code=:account_code "
                        "AND external_id IS NOT DISTINCT FROM :external_id "
                        "AND fingerprint=:fingerprint"
                    ),
                    {
                        "org_id": self.org_id,
                        "account_code": value["bank_account_code"],
                        "external_id": value.get("external_id"),
                        "fingerprint": value["source_fingerprint"],
                    },
                ).first()
                # A normalized replay CSV intentionally has a new source hash and
                # therefore a new fingerprint. External ID plus account remains the
                # stable business identity when the old source fingerprint differs.
                if row is None and value.get("external_id") is not None:
                    row = session.execute(
                        text(
                            "SELECT id, fingerprint FROM bank_transactions "
                            "WHERE org_id=:org_id "
                            "AND bank_account_code=:account_code AND external_id=:external_id"
                        ),
                        {
                            "org_id": self.org_id,
                            "account_code": value["bank_account_code"],
                            "external_id": value["external_id"],
                        },
                    ).first()
                if row is not None and ref_type == "bank_transaction_reference":
                    return {"id": str(row.id), "fingerprint": str(row.fingerprint)}
                result = row.id if row is not None else None
            elif ref_type == "counterparty":
                result = session.scalar(
                    text(
                        "SELECT id FROM counterparties WHERE org_id=:org_id AND kind=:kind "
                        "AND name=:name AND external_ref IS NOT DISTINCT FROM :external_ref"
                    ),
                    {
                        "org_id": self.org_id,
                        "kind": value["kind"],
                        "name": value["name"],
                        "external_ref": value.get("external_ref"),
                    },
                )
            elif ref_type == "event":
                result_payload = self.results.get(str(value["replay_key"]))
                result = result_payload.get("event_id") if result_payload else None
            elif ref_type == "component":
                source = self.results.get(str(value["source_replay_key"]))
                event_id = source.get("event_id") if source else None
                if event_id is None:
                    raise ReplayError("REPLAY_COMPONENT_SOURCE_MISSING")
                result = session.execute(
                    text(
                        "SELECT id FROM business_event_components WHERE org_id=:org_id "
                        "AND event_id=:event_id AND key=:component_key"
                    ),
                    {
                        "org_id": self.org_id,
                        "event_id": event_id,
                        "component_key": value["component_key"],
                    },
                ).scalar_one_or_none()
            elif ref_type == "component_open_item":
                source = self.results.get(str(value["source_replay_key"]))
                event_id = source.get("event_id") if source else None
                if event_id is None:
                    raise ReplayError("REPLAY_COMPONENT_SOURCE_MISSING")
                result = session.execute(
                    text(
                        "SELECT item.id FROM open_items AS item "
                        "JOIN business_event_components AS component "
                        "ON component.org_id=item.org_id AND component.id=item.source_component_id "
                        "WHERE item.org_id=:org_id AND component.event_id=:event_id "
                        "AND component.key=:component_key AND item.component_key=:open_item_key"
                    ),
                    {
                        "org_id": self.org_id,
                        "event_id": event_id,
                        "component_key": value["component_key"],
                        "open_item_key": value["open_item_key"],
                    },
                ).scalar_one_or_none()
            elif ref_type == "component_income_tax":
                source = self.results.get(str(value["source_replay_key"]))
                event_id = source.get("event_id") if source else None
                if event_id is None:
                    raise ReplayError("REPLAY_COMPONENT_SOURCE_MISSING")
                table_name = {
                    "confirmation": "enterprise_income_tax_quarter_confirmations",
                    "result": "enterprise_income_tax_results",
                }.get(str(value["result_kind"]))
                if table_name is None:
                    raise ReplayError("REPLAY_COMPONENT_INCOME_TAX_KIND_INVALID")
                result = session.scalar(
                    text(
                        f'SELECT tax.id FROM "{table_name}" AS tax '
                        "JOIN business_event_components AS component "
                        "ON component.org_id=tax.org_id AND component.id=tax.component_id "
                        "WHERE tax.org_id=:org_id AND component.event_id=:event_id "
                        "AND component.key=:component_key"
                    ),
                    {
                        "org_id": self.org_id,
                        "event_id": event_id,
                        "component_key": value["component_key"],
                    },
                )
            elif ref_type == "open_item":
                source = self.results.get(str(value["source_replay_key"]))
                source_event_id = source.get("event_id") if source else None
                if source_event_id is None:
                    raise ReplayError("REPLAY_OPEN_ITEM_SOURCE_MISSING")
                result = session.scalar(
                    text(
                        "SELECT item.id FROM open_items AS item "
                        "JOIN counterparties AS counterparty "
                        "ON counterparty.org_id=item.org_id "
                        "AND counterparty.id=item.counterparty_id "
                        "WHERE item.org_id=:org_id "
                        "AND item.source_event_id=:source_event_id "
                        "AND item.item_type=:item_type "
                        "AND item.original_amount_fen=:amount "
                        "AND item.payable_category IS NOT DISTINCT FROM :category "
                        "AND item.payable_agency_code IS NOT DISTINCT FROM :agency "
                        "AND item.insurance_kind IS NOT DISTINCT FROM :insurance "
                        "AND counterparty.kind=:counterparty_kind "
                        "AND counterparty.external_ref IS NOT DISTINCT FROM "
                        ":counterparty_external_ref "
                        "AND (:counterparty_external_ref IS NOT NULL "
                        "OR counterparty.name=:counterparty_name)"
                    ),
                    {
                        "org_id": self.org_id,
                        "source_event_id": source_event_id,
                        "item_type": value["item_type"],
                        "amount": value["original_amount_fen"],
                        "category": value.get("payable_category"),
                        "agency": value.get("payable_agency_code"),
                        "insurance": value.get("insurance_kind"),
                        "counterparty_kind": value["counterparty_kind"],
                        "counterparty_name": value["counterparty_name"],
                        "counterparty_external_ref": value.get("counterparty_external_ref"),
                    },
                )
            elif ref_type in {"asset", "intangible", "labor_person", "borrowing"}:
                table_name, code_name = {
                    "asset": ("fixed_assets", "asset_code"),
                    "intangible": ("intangible_assets", "asset_code"),
                    "labor_person": ("labor_service_persons", "person_code"),
                    "borrowing": ("borrowings", "borrowing_code"),
                }[str(ref_type)]
                result = session.scalar(
                    text(
                        f'SELECT id FROM "{table_name}" WHERE org_id=:org_id '
                        f'AND "{code_name}"=:code'
                    ),
                    {"org_id": self.org_id, "code": value["code"]},
                )
            elif ref_type == "period":
                year, month = (int(part) for part in str(value["period_month"]).split("-"))
                result = session.scalar(
                    text(
                        "SELECT id FROM accounting_periods WHERE org_id=:org_id "
                        "AND calendar_year=:year AND calendar_month=:month"
                    ),
                    {"org_id": self.org_id, "year": year, "month": month},
                )
            elif ref_type == "bank_import_actions_for_period":
                result = list(
                    session.scalars(
                        text(
                            "SELECT DISTINCT import_action_id FROM bank_transactions "
                            "WHERE org_id=:org_id AND bank_account_code=:account_code "
                            "AND booking_date BETWEEN :start_date AND :end_date "
                            "AND import_action_id IS NOT NULL ORDER BY import_action_id"
                        ),
                        {
                            "org_id": self.org_id,
                            "account_code": value["bank_account_code"],
                            "start_date": value["coverage_start_date"],
                            "end_date": value["coverage_end_date"],
                        },
                    )
                )
                return [str(item) for item in result]
            elif ref_type == "voucher_line":
                source = self.results.get(str(value["source_replay_key"]))
                event_id = source.get("event_id") if source else None
                if event_id is None:
                    raise ReplayError("REPLAY_VOUCHER_LINE_SOURCE_MISSING")
                matches = session.scalars(
                    text(
                        "SELECT line.id FROM voucher_lines AS line "
                        "JOIN vouchers AS voucher ON voucher.org_id=line.org_id "
                        "AND voucher.id=line.voucher_id "
                        "JOIN accounts AS account ON account.org_id=line.org_id "
                        "AND account.id=line.account_id "
                        "JOIN business_event_components AS component "
                        "ON component.id=line.component_id "
                        "WHERE line.org_id=:org_id AND voucher.event_id=:event_id "
                        "AND component.key=:component_key AND account.code=:account_code "
                        "AND line.debit_fen=:debit_fen AND line.credit_fen=:credit_fen"
                    ),
                    {
                        "org_id": self.org_id,
                        "event_id": event_id,
                        "component_key": value["component_key"],
                        "account_code": value["account_code"],
                        "debit_fen": value["debit_fen"],
                        "credit_fen": value["credit_fen"],
                    },
                ).all()
                if len(matches) > 1:
                    raise ReplayError("REPLAY_COMPONENT_LINE_REFERENCE_AMBIGUOUS")
                result = matches[0] if matches else None
            else:
                raise ReplayError(f"REPLAY_REFERENCE_TYPE_UNSUPPORTED:{ref_type}")
        if result is None:
            raise ReplayError(f"REPLAY_STABLE_REFERENCE_NOT_FOUND:{ref_type}")
        return str(result)


def _load_state(package: Path, state_path: Path | None) -> tuple[Path, dict[str, Any]]:
    path = (state_path or _default_state_path(package)).resolve(strict=True)
    state = _load_json(path)
    if state.get("state_version") != _STATE_VERSION:
        raise ReplayError("REPLAY_STATE_FORMAT_UNSUPPORTED")
    verification = verify_package(package)
    if state.get("package_manifest_sha256") != verification["manifest_sha256"]:
        raise ReplayError("REPLAY_STATE_PACKAGE_MISMATCH")
    return path, state


def _tool_request_model(tool: Any) -> type[BaseModel]:
    hints = get_type_hints(tool.fn)
    model = hints.get("request")
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise ReplayError("REPLAY_TOOL_REQUEST_MODEL_INVALID")
    return model


def _call_tool(name: str, request: Mapping[str, Any]) -> dict[str, Any]:
    from . import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool(name)
    if tool is None or bool(tool.annotations and tool.annotations.destructiveHint):
        raise ReplayError(f"REPLAY_TOOL_NOT_ALLOWED:{name}")
    hints = get_type_hints(tool.fn)
    request_model = hints.get("request")
    if isinstance(request_model, type) and issubclass(request_model, BaseModel):
        parsed = request_model.model_validate(request)
        result = tool.fn(request=parsed)
    else:
        argument_model = tool.fn_metadata.arg_model
        if not isinstance(argument_model, type) or not issubclass(argument_model, BaseModel):
            raise ReplayError("REPLAY_TOOL_REQUEST_MODEL_INVALID")
        parsed = argument_model.model_validate(request)
        result = tool.fn(**parsed.model_dump())
    if inspect.isawaitable(result):
        raise ReplayError("REPLAY_ASYNC_TOOL_NOT_SUPPORTED")
    if not isinstance(result, dict):
        raise ReplayError("REPLAY_TOOL_RESULT_INVALID")
    return result


def _ordered_replay_companies(
    companies: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Run non-primary companies first so risky secondary replays fail early."""

    return sorted(
        companies,
        key=lambda company: (
            bool(company.get("is_primary")),
            str(company.get("display_name", "")),
            str(company["org_id"]),
        ),
    )


def _require_status(
    result: Mapping[str, Any],
    allowed: Sequence[str],
    *,
    operation_key: str,
) -> None:
    if str(result.get("status")) not in allowed:
        error_codes = [
            item.get("code") if isinstance(item, dict) else item
            for item in result.get("errors", [])
        ]
        suffix = ",".join(str(item) for item in error_codes if item)
        if not suffix:
            suffix = str(result.get("status", "unknown"))
        raise ReplayError(f"REPLAY_OPERATION_FAILED:{operation_key}:{suffix}")


def _preview_confirm(
    operation: Mapping[str, Any],
    resolver: _ReplayResolver,
) -> dict[str, Any]:
    preview_request = resolver.materialize(operation["preview_request"])
    preview = _call_tool(str(operation["preview_tool"]), preview_request)
    _require_status(
        preview,
        operation.get("allowed_preview_statuses", ["calculated"]),
        operation_key=str(operation["key"]),
    )
    confirm_request = dict(preview_request)
    confirm_request.update(resolver.materialize(operation.get("confirm_request", {})))
    if "calculation_hash" in preview:
        confirm_request["calculation_hash"] = preview["calculation_hash"]
    confirm_tool = str(operation["confirm_tool"])
    from . import mcp_server

    tool = mcp_server.mcp._tool_manager.get_tool(confirm_tool)
    if tool is None:
        raise ReplayError(f"REPLAY_TOOL_NOT_ALLOWED:{confirm_tool}")
    model = _tool_request_model(tool)
    for field_name in model.model_fields:
        if field_name not in confirm_request and field_name in preview:
            confirm_request[field_name] = preview[field_name]
    # Confirm models intentionally do not always repeat preview-only helper
    # fields. Pydantic's exact allowlist remains the final boundary.
    confirm_request = {
        key: value for key, value in confirm_request.items() if key in model.model_fields
    }
    confirmed = _call_tool(confirm_tool, confirm_request)
    _require_status(
        confirmed,
        operation.get("allowed_confirm_statuses", ["posted"]),
        operation_key=str(operation["key"]),
    )
    return confirmed


def _owner_workflow_snapshot(resolver: _ReplayResolver, period_month: str) -> dict[str, Any]:
    period_id = resolver.materialize({"$ref": "period", "period_month": period_month})
    result = _call_tool(
        "finance_get_owner_workflow",
        {"org_id": str(resolver.org_id), "period_id": period_id},
    )
    _require_status(result, ["ok"], operation_key=f"owner-workflow:{period_month}")
    return result


def _owner_control_operation(
    operation: Mapping[str, Any], resolver: _ReplayResolver
) -> dict[str, Any]:
    control = str(operation["control"])
    evidence = resolver.materialize(operation.get("evidence_references", []))
    common = {
        "org_id": str(resolver.org_id),
        "idempotency_key": operation["idempotency_key"],
        "confirmation_note": operation["confirmation_note"],
        "evidence_references": evidence,
    }
    if control in {"workforce_review", "non_bank_materials"}:
        workflow = _owner_workflow_snapshot(resolver, str(operation["period_month"]))
        gates = workflow["close_gates"]["gates"]
        period_id = workflow["period"]["id"]
        if control == "workforce_review":
            request = {
                **common,
                "period_id": period_id,
                "workforce_snapshot_hash": gates["workforce_review"]["source_snapshot_hash"],
                "change_state": operation["confirmation_state"],
                "regular_payroll_items": resolver.materialize(
                    operation.get("regular_payroll_items")
                ),
                "supersedes_confirmation_id": None,
            }
            tool = "finance_confirm_workforce_review"
        else:
            request = {
                **common,
                "period_id": period_id,
                "activity_snapshot_hash": gates["non_bank_materials"]["source_snapshot_hash"],
                "supersedes_confirmation_id": None,
            }
            tool = "finance_confirm_period_material_completeness"
    elif control == "contribution_assessment":
        period_id = resolver.materialize(
            {"$ref": "period", "period_month": operation["period_month"]}
        )
        preview = _call_tool(
            "finance_preview_payroll_contribution_assessment",
            {"org_id": str(resolver.org_id), "period_id": period_id},
        )
        _require_status(preview, ["calculated"], operation_key=str(operation["key"]))
        request = {
            **common,
            "period_id": period_id,
            "calculation_hash": preview["calculation_hash"],
            "declaration_status": operation["declaration_status"],
            "declaration_date": operation["declaration_date"],
            "external_reference": operation["external_reference"],
            "supersedes_confirmation_id": None,
        }
        tool = "finance_confirm_payroll_contribution_assessment"
    elif control == "external_obligation":
        workflow = _owner_workflow_snapshot(resolver, str(operation["scope_identity"]))
        gate = workflow["close_gates"]["gates"]["individual_income_tax_declaration"]
        if (
            gate.get("obligation_id") is None
            or operation["obligation_code"] != "individual_income_tax"
        ):
            raise ReplayError("REPLAY_EXTERNAL_OBLIGATION_NOT_DERIVED")
        request = {
            **common,
            "obligation_id": gate["obligation_id"],
            "source_snapshot_hash": gate["source_snapshot_hash"],
            "completion_status": operation["completion_status"],
            "completion_date": operation["completion_date"],
            "external_reference": operation["external_reference"],
            "supersedes_confirmation_id": None,
        }
        tool = "finance_confirm_external_obligation"
    elif control == "historical_obligation":
        with Session(resolver.engine) as session:
            latest = session.execute(
                text(
                    "SELECT calendar_year, calendar_month FROM accounting_periods "
                    "WHERE org_id=:org_id ORDER BY calendar_year DESC, "
                    "calendar_month DESC LIMIT 1"
                ),
                {"org_id": resolver.org_id},
            ).one()
        workflow = _owner_workflow_snapshot(
            resolver, f"{latest.calendar_year:04d}-{latest.calendar_month:02d}"
        )
        candidate = next(
            (
                item
                for item in workflow["historical_obligation_completion_candidates"]
                if item["obligation_code"] == operation["obligation_code"]
            ),
            None,
        )
        if (
            candidate is None
            or candidate["completion_through_identity"] != operation["completion_through_identity"]
        ):
            raise ReplayError("REPLAY_HISTORICAL_OBLIGATION_CANDIDATE_CHANGED")
        request = {
            **common,
            "obligation_code": operation["obligation_code"],
            "completion_through_identity": operation["completion_through_identity"],
            "source_snapshot_hash": candidate["source_snapshot_hash"],
            "completion_date_status": operation["completion_date_status"],
            "supersedes_confirmation_id": None,
        }
        tool = "finance_confirm_historical_obligation_completion"
    else:
        raise ReplayError(f"REPLAY_OWNER_CONTROL_UNSUPPORTED:{control}")
    result = _call_tool(tool, request)
    _require_status(result, ["confirmed"], operation_key=str(operation["key"]))
    return result


def _period_close_operation(
    operation: Mapping[str, Any], resolver: _ReplayResolver
) -> dict[str, Any]:
    preview_request = resolver.materialize(operation["preview_request"])
    preview = _call_tool("finance_preview_accounting_period_close", preview_request)
    _require_status(preview, ["calculated"], operation_key=str(operation["key"]))
    blocking = preview.get("data", {}).get("blocker_codes") or []
    if blocking:
        codes = ",".join(str(item) for item in blocking)
        raise ReplayError(f"REPLAY_PERIOD_CLOSE_BLOCKED:{operation['key']}:{codes}")
    checklist = preview.get("data", {}).get("assistant_review_checklist", {})
    commentary = checklist.get("management_commentary", {})
    context_hash = commentary.get("context_hash")
    if not context_hash:
        raise ReplayError("REPLAY_PERIOD_CLOSE_COMMENTARY_CONTEXT_MISSING")
    request = {
        **preview_request,
        "calculation_hash": preview["calculation_hash"],
        "management_commentary_context_hash": context_hash,
        "management_commentary": operation["management_commentary"],
        "owner_approval_id": None,
        "idempotency_key": _replay_idempotency(str(operation["key"])),
        "review_facts": operation["review_facts"],
        "confirmation_note": operation["confirmation_note"],
        "evidence_references": resolver.materialize(operation["evidence_references"]),
    }
    result = _call_tool("finance_confirm_historical_test_period_close", request)
    _require_status(result, ["posted"], operation_key=str(operation["key"]))
    return result


def _evidence_operation(
    operation: Mapping[str, Any],
    *,
    package_company_dir: Path,
    resolver: _ReplayResolver,
) -> dict[str, Any]:
    path = _safe_package_file(package_company_dir, str(operation["relative_path"]))
    request = dict(resolver.materialize(operation["request"]))
    expected = str(request.pop("expected_sha256"))
    digest, _ = _sha256_file(path)
    if digest != expected:
        raise ReplayError("REPLAY_PACKAGE_EVIDENCE_MISMATCH")
    request["content_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
    result = _call_tool("finance_register_evidence", request)
    _require_status(
        result,
        operation.get("allowed_statuses", ["registered"]),
        operation_key=str(operation["key"]),
    )
    if result.get("sha256") != expected:
        raise ReplayError("REPLAY_REGISTERED_EVIDENCE_MISMATCH")
    return result


def _bank_import_operation(
    operation: Mapping[str, Any],
    *,
    package_company_dir: Path,
    resolver: _ReplayResolver,
) -> dict[str, Any]:
    settings = get_settings()
    source = _safe_package_file(package_company_dir, str(operation["relative_path"]))
    digest, _ = _sha256_file(source)
    if digest != operation["expected_sha256"]:
        raise ReplayError("REPLAY_PACKAGE_BANK_FILE_MISMATCH")
    import_root = settings.finance_bank_import_dir.resolve()
    import_root.mkdir(parents=True, exist_ok=True)
    target = import_root / f"replay-{uuid.uuid4().hex}.csv"
    if target.exists():
        raise ReplayError("REPLAY_BANK_IMPORT_STAGING_CONFLICT")
    shutil.copyfile(source, target)
    try:
        preview_request = {
            "org_id": str(resolver.org_id),
            "bank_account_code": operation["bank_account_code"],
            "source_file_name": target.name,
            "file_format": "csv",
            "column_mapping": operation["column_mapping"],
            "date_format": "%Y-%m-%d",
            "missing_external_id_resolutions": [],
            "proceed_with_known_row_errors": False,
        }
        preview = _call_tool("finance_preview_bank_statement_import", preview_request)
        _require_status(preview, ["calculated"], operation_key=str(operation["key"]))
        confirm_request = dict(preview_request)
        confirm_request.update(
            {
                "calculation_hash": preview["calculation_hash"],
                "idempotency_key": _replay_idempotency(str(operation["key"])),
            }
        )
        result = _call_tool("finance_confirm_bank_statement_import", confirm_request)
        _require_status(
            result,
            ["posted", "partially_posted"],
            operation_key=str(operation["key"]),
        )
        imported_count = result.get(
            "imported_count", result.get("data", {}).get("imported_count", -1)
        )
        if int(imported_count) != int(operation["expected_row_count"]):
            raise ReplayError("REPLAY_BANK_IMPORT_ROW_COUNT_MISMATCH")
        return result
    finally:
        target.unlink(missing_ok=True)


def _execute_operation(
    operation: Mapping[str, Any],
    *,
    package_company_dir: Path,
    resolver: _ReplayResolver,
) -> dict[str, Any]:
    kind = str(operation.get("kind"))
    if kind == "tool":
        request = resolver.materialize(operation["request"])
        result = _call_tool(str(operation["tool"]), request)
        _require_status(
            result,
            operation.get("allowed_statuses", ["posted"]),
            operation_key=str(operation["key"]),
        )
        return result
    if kind == "preview_confirm":
        return _preview_confirm(operation, resolver)
    if kind == "composite_event":
        for preparation in operation.get("preparations", []):
            preview_request = resolver.materialize(preparation["preview_request"])
            preview = _call_tool(str(preparation["preview_tool"]), preview_request)
            _require_status(
                preview,
                preparation.get("allowed_statuses", ["calculated"]),
                operation_key=str(preparation["key"]),
            )
            resolver.results[str(preparation["key"])] = preview
        preview_request = resolver.materialize(operation["request"])
        preview = _call_tool("finance_preview_event", preview_request)
        _require_status(preview, ["calculated"], operation_key=str(operation["key"]))
        reviewed = preview.get("data", {}).get("reviewed_request")
        if not isinstance(reviewed, Mapping):
            raise ReplayError("REPLAY_COMPONENT_REVIEWED_REQUEST_MISSING")
        result = _call_tool("finance_record_event", reviewed)
        _require_status(
            result,
            operation.get("allowed_statuses", ["posted"]),
            operation_key=str(operation["key"]),
        )
        return result
    if kind == "evidence":
        return _evidence_operation(
            operation,
            package_company_dir=package_company_dir,
            resolver=resolver,
        )
    if kind == "bank_import":
        return _bank_import_operation(
            operation,
            package_company_dir=package_company_dir,
            resolver=resolver,
        )
    if kind == "owner_control":
        return _owner_control_operation(operation, resolver)
    if kind == "period_close":
        return _period_close_operation(operation, resolver)
    raise ReplayError(f"REPLAY_OPERATION_KIND_UNSUPPORTED:{kind}")


def _validate_replay_target(state: Mapping[str, Any], settings) -> None:
    """Verify persisted identities before opening authentication or touching business data."""
    if not settings.multi_company_enabled:
        raise ReplayError("REPLAY_TARGET_CATALOG_MISMATCH")
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            marker = session.get(CatalogMetadata, 1)
            if (
                marker is None
                or str(marker.catalog_instance_id) != state.get("catalog_instance_id")
                or make_url(settings.database_url).database != state.get("catalog_database")
            ):
                raise ReplayError("REPLAY_TARGET_CATALOG_MISMATCH")
            for company in state.get("companies", []):
                registry = session.get(CompanyRegistry, uuid.UUID(company["org_id"]))
                if (
                    registry is None
                    or registry.database_name != company["database_name"]
                    or str(registry.database_identity) != company["database_identity"]
                ):
                    raise ReplayError("REPLAY_TARGET_COMPANY_MISMATCH")
                for base in {
                    settings.finance_company_database_url,
                    settings.finance_migration_database_url,
                } - {None}:
                    business_engine = create_engine(
                        _database_url_for_name(base, registry.database_name)
                    )
                    try:
                        with Session(business_engine) as business:
                            binding = business.get(OrganizationDatabaseMetadata, 1)
                            if (
                                binding is None
                                or binding.org_id != registry.org_id
                                or binding.database_identity != registry.database_identity
                                or binding.current_catalog_instance_id != marker.catalog_instance_id
                            ):
                                raise ReplayError("REPLAY_TARGET_COMPANY_MISMATCH")
                    finally:
                        business_engine.dispose()
    finally:
        engine.dispose()


def _request_replay_security(state: Mapping[str, Any], settings) -> dict[str, Any]:
    from sqlalchemy.orm import sessionmaker

    from .identity import IdentityError
    from .owner_login_launcher import OwnerSecurityWindowLauncher
    from .owner_security import OwnerSecurityOperations, OwnerSecurityWindowRequest

    _validate_replay_target(state, settings)
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            owner = session.scalar(select(OwnerAccount).limit(1))
            request = OwnerSecurityWindowRequest(
                kind="bootstrap_owner" if owner is None else "login",
                org_id=state["primary_org_id"] if owner is None else None,
                login_name="owner" if owner is None else owner.login_name,
            )
        operations = OwnerSecurityOperations(settings=settings, factory=sessionmaker(engine))
        return OwnerSecurityWindowLauncher(operations=operations).request(request)
    except IdentityError as exc:
        return {"status": "failed", "error_code": exc.code, "next_action": "request_window_again"}
    except Exception:
        return {
            "status": "failed",
            "error_code": "OWNER_SECURITY_WINDOW_UNAVAILABLE",
            "next_action": "request_window_again",
        }
    finally:
        engine.dispose()


def replay_system(package: Path, state_path: Path | None = None) -> dict[str, Any]:
    package_root = package.resolve(strict=True)
    state_file, state = _load_state(package_root, state_path)
    if state["phase"] not in {"prepared", "replaying"}:
        raise ReplayError("REPLAY_STATE_PHASE_INVALID")
    settings = get_settings()
    _validate_replay_target(state, settings)
    from . import mcp_server

    mcp_server._initialize_mcp_credential_store(environment=settings.finance_environment)
    authentication = _call_tool("finance_list_companies", {"include_archived": True})
    if authentication.get("status") != "ok":
        security = _request_replay_security(state, settings)
        return {
            "status": "blocked" if security.get("status") == "failed" else "waiting_for_owner",
            "error_code": "REPLAY_AUTHENTICATION_REQUIRED",
            "state_file": str(state_file),
            "owner_security_window": security,
        }
    system = _load_json(package_root / "system.json")
    state["phase"] = "replaying"
    _write_json(state_file, state)
    completed_total = 0
    replay_companies = _ordered_replay_companies(system["companies"])
    for company in replay_companies:
        org_id = uuid.UUID(str(company["org_id"]))
        company_dir = package_root / str(company["directory"])
        operations = _read_jsonl(company_dir / "operations.jsonl")
        required_result_fields: dict[str, set[str]] = {
            str(operation["key"]): (
                {"status", "event_id"} if "replay_key" in operation else {"status"}
            )
            for operation in operations
        }
        for operation in operations:
            for value in _walk_package_values(operation):
                if isinstance(value, Mapping) and value.get("$ref") == "operation_result":
                    required_result_fields[str(value["operation_key"])].add(str(value["field"]))
        company_state = next(item for item in state["companies"] if item["org_id"] == str(org_id))
        completed = set(company_state["completed_operations"])
        results = company_state["operation_results"]
        base = settings.finance_migration_database_url or settings.finance_company_database_url
        if base is None:
            raise ReplayError("REPLAY_TARGET_COMPANY_DATABASE_URL_REQUIRED")
        engine = create_engine(_database_url_for_name(base, company_state["database_name"]))
        has_period_close = any(operation.get("kind") == "period_close" for operation in operations)
        historical_mode_enabled = False
        try:
            resolver = _ReplayResolver(engine=engine, org_id=org_id, results=results)
            for operation in operations:
                key = str(operation.get("key", ""))
                if not key:
                    raise ReplayError("REPLAY_OPERATION_KEY_REQUIRED")
                if key in completed:
                    continue
                if operation.get("kind") == "period_close" and not historical_mode_enabled:
                    mode_result = _call_tool(
                        "finance_configure_historical_test_close_mode",
                        {
                            "org_id": str(org_id),
                            "enabled": True,
                            "idempotency_key": _replay_idempotency(
                                f"historical-close-mode:{org_id}:enabled"
                            ),
                            "confirmation_note": (
                                "空库业务语义回放期间连续重建历史关账，完成或失败后立即关闭。"
                            ),
                        },
                    )
                    _require_status(
                        mode_result,
                        ["posted"],
                        operation_key="historical-close-mode:enabled",
                    )
                    historical_mode_enabled = True
                result = _execute_operation(
                    operation,
                    package_company_dir=company_dir,
                    resolver=resolver,
                )
                results[key] = _jsonable(
                    {
                        field: result[field]
                        for field in required_result_fields[key]
                        if field in result
                    }
                )
                company_state["completed_operations"].append(key)
                completed.add(key)
                completed_total += 1
                _write_json(state_file, state)
        finally:
            if has_period_close:
                disabled = _call_tool(
                    "finance_configure_historical_test_close_mode",
                    {
                        "org_id": str(org_id),
                        "enabled": False,
                        "idempotency_key": _replay_idempotency(
                            f"historical-close-mode:{org_id}:disabled"
                        ),
                        "confirmation_note": (
                            "历史重建批次已经结束，恢复逐月负责人专用授权和自动备份控制。"
                        ),
                    },
                )
                _require_status(
                    disabled,
                    ["posted"],
                    operation_key="historical-close-mode:disabled",
                )
            engine.dispose()
    state["phase"] = "replayed"
    _write_json(state_file, state)
    return {
        "status": "replayed",
        "company_count": len(system["companies"]),
        "completed_operations_this_run": completed_total,
        "state_file": str(state_file),
    }


def _balance_terminal_state(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "account_code": str(row["account_code"]),
                "ending_balance_fen": int(row["ending_balance_fen"]),
            }
            for row in rows
            if int(row["ending_balance_fen"]) != 0
        ),
        key=lambda row: row["account_code"],
    )


def verify_replay(package: Path, state_path: Path | None = None) -> dict[str, Any]:
    package_root = package.resolve(strict=True)
    state_file, state = _load_state(package_root, state_path)
    if state["phase"] not in {"replayed", "verified"}:
        raise ReplayError("REPLAY_STATE_NOT_REPLAYED")
    settings = get_settings()
    catalog_engine = create_engine(settings.database_url)
    system = _load_json(package_root / "system.json")
    reports: list[dict[str, Any]] = []
    try:
        with Session(catalog_engine) as session:
            registered = _query_rows(
                session,
                "SELECT org_id, database_name, is_primary FROM company_registry ORDER BY org_id",
            )
            expected_orgs = sorted(str(item["org_id"]) for item in system["companies"])
            actual_orgs = sorted(str(item["org_id"]) for item in registered)
            if actual_orgs != expected_orgs:
                raise ReplayError("REPLAY_VERIFY_CATALOG_COMPANIES_MISMATCH")
            if sum(bool(item["is_primary"]) for item in registered) != 1:
                raise ReplayError("REPLAY_VERIFY_PRIMARY_COMPANY_MISMATCH")
            revision = session.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != _current_schema_revision(catalog=True):
                raise ReplayError("REPLAY_VERIFY_CATALOG_REVISION_MISMATCH")
        for company in system["companies"]:
            org_id = uuid.UUID(str(company["org_id"]))
            company_state = next(
                item for item in state["companies"] if item["org_id"] == str(org_id)
            )
            base = settings.finance_migration_database_url or settings.finance_company_database_url
            if base is None:
                raise ReplayError("REPLAY_TARGET_COMPANY_DATABASE_URL_REQUIRED")
            engine = create_engine(_database_url_for_name(base, company_state["database_name"]))
            try:
                with Session(engine) as session:
                    tables = set(sa_inspect(session.bind).get_table_names())
                    if tables & _SENSITIVE_TABLES:
                        raise ReplayError("REPLAY_VERIFY_BUSINESS_IDENTITY_TABLE_FORBIDDEN")
                    organizations = [
                        str(item) for item in session.scalars(select(Organization.id)).all()
                    ]
                    if organizations != [str(org_id)]:
                        raise ReplayError("REPLAY_VERIFY_COMPANY_ISOLATION_MISMATCH")
                    revision = session.scalar(text("SELECT version_num FROM alembic_version"))
                    if revision != _current_schema_revision(catalog=False):
                        raise ReplayError("REPLAY_VERIFY_BUSINESS_REVISION_MISMATCH")
                    actual = _company_checkpoints(session, org_id)
                    descriptor = _load_json(
                        package_root / str(company["directory"]) / "company.json"
                    )
                    expected = descriptor["checkpoints"]
                    mismatches = {
                        key: {"expected": expected[key], "actual": actual.get(key)}
                        for key in expected
                        if key
                        not in {
                            "effective_event_count",
                            "effective_voucher_count",
                            "voucher_debit_total_fen",
                            "voucher_credit_total_fen",
                        }
                        and actual.get(key) != expected[key]
                    }
                    if mismatches:
                        raise ReplayError(
                            "REPLAY_VERIFY_CHECKPOINT_MISMATCH:" + ",".join(sorted(mismatches))
                        )
                    expected_balances = _load_json(
                        package_root
                        / str(company["directory"])
                        / descriptor["verification_files"]["account_balances"]
                    )
                    actual_balances = _account_balance_projection(session, org_id)
                    if _balance_terminal_state(actual_balances) != _balance_terminal_state(
                        expected_balances
                    ):
                        raise ReplayError("REPLAY_VERIFY_ACCOUNT_BALANCE_MISMATCH")
                    expected_open_items = _load_json(
                        package_root
                        / str(company["directory"])
                        / descriptor["verification_files"]["open_items"]
                    )
                    actual_open_items = _jsonable(_open_item_projection(session, org_id=org_id))
                    if actual_open_items != expected_open_items:
                        raise ReplayError("REPLAY_VERIFY_OPEN_ITEM_MISMATCH")
                    evidence_rows = session.scalars(
                        select(Evidence).where(Evidence.org_id == org_id)
                    ).all()
                    for evidence in evidence_rows:
                        digest, size = _sha256_file(Path(evidence.storage_path))
                        if digest != evidence.sha256 or size != evidence.size_bytes:
                            raise ReplayError("REPLAY_VERIFY_EVIDENCE_MISMATCH")
                    unbalanced = int(
                        session.scalar(
                            text(
                                "SELECT COUNT(*) FROM ("
                                "SELECT voucher.id FROM vouchers AS voucher "
                                "JOIN voucher_lines AS line ON line.org_id=voucher.org_id "
                                "AND line.voucher_id=voucher.id "
                                "WHERE voucher.org_id=:org_id AND voucher.status='posted' "
                                "GROUP BY voucher.id HAVING SUM(line.debit_fen)<>"
                                "SUM(line.credit_fen)) AS unbalanced"
                            ),
                            {"org_id": org_id},
                        )
                        or 0
                    )
                    if unbalanced:
                        raise ReplayError("REPLAY_VERIFY_VOUCHER_UNBALANCED")
                    reports.append(
                        {
                            "org_id": str(org_id),
                            "display_name": company["display_name"],
                            "checkpoints": actual,
                            "evidence_bytes": sum(item.size_bytes for item in evidence_rows),
                        }
                    )
            finally:
                engine.dispose()
        report = {
            "status": "verified",
            "verified_at": datetime.now(UTC).isoformat(),
            "package_manifest_sha256": state["package_manifest_sha256"],
            "companies": reports,
            "checkpoints": system["checkpoints"],
        }
        report_path = state_file.with_name(state_file.stem + ".verification.json")
        _write_json(report_path, report)
        state["phase"] = "verified"
        state["verification_report"] = str(report_path)
        _write_json(state_file, state)
        return {**report, "report_file": str(report_path)}
    finally:
        catalog_engine.dispose()


def _print_result(value: Mapping[str, Any]) -> None:
    print(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Export, validate, and replay typed accounting facts into empty databases"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-system", help="read-only export of the catalog companies")
    export.add_argument("--output", type=Path, required=True)
    verify_pkg = commands.add_parser("verify-package", help="offline package verification")
    verify_pkg.add_argument("--package", type=Path, required=True)
    prepare = commands.add_parser(
        "prepare-empty", help="create baselines only when every target is absent or empty"
    )
    prepare.add_argument("--package", type=Path, required=True)
    prepare.add_argument("--state-file", type=Path)
    replay = commands.add_parser("replay", help="authenticated typed, resumable replay")
    replay.add_argument("--package", type=Path, required=True)
    replay.add_argument("--state-file", type=Path)
    verify = commands.add_parser("verify", help="verify semantic target checkpoints")
    verify.add_argument("--package", type=Path, required=True)
    verify.add_argument("--state-file", type=Path)
    try:
        args = parser.parse_args(argv)
        if args.command == "export-system":
            result = export_system(args.output)
        elif args.command == "verify-package":
            result = verify_package(args.package)
        elif args.command == "prepare-empty":
            result = prepare_empty(args.package, args.state_file)
        elif args.command == "replay":
            result = replay_system(args.package, args.state_file)
        else:
            result = verify_replay(args.package, args.state_file)
        _print_result(result)
    except ReplayError as exc:
        print(exc.code, file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("REPLAY_COMMAND_FAILED", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
