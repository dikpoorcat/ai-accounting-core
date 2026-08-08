from __future__ import annotations

import csv
import hashlib
import json
import uuid
from collections.abc import Iterable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BankTransaction, Organization
from .schemas import ImportBankStatementRequest

CANONICAL_COLUMNS = {
    "booking_date",
    "amount",
    "debit",
    "credit",
    "counterparty",
    "memo",
    "external_id",
    "currency",
}


def import_bank_statement(session: Session, request: ImportBankStatementRequest) -> dict[str, Any]:
    if session.get(Organization, request.org_id) is None:
        raise ValueError("ORGANIZATION_NOT_FOUND")
    path = request.file_path.resolve(strict=True)
    extension = path.suffix.lower()
    if extension not in {".csv", ".xlsx"}:
        raise ValueError("only CSV and XLSX bank statements are supported")
    _validate_mapping(request.column_mapping)
    source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = _read_csv(path) if extension == ".csv" else _read_xlsx(path, request.sheet_name)

    imported: list[str] = []
    duplicates: list[str] = []
    errors: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=2):
        try:
            normalized = _normalize_row(row, request)
            fingerprint = _fingerprint(
                request.org_id,
                request.bank_account_code,
                normalized,
            )
            existing = session.scalar(
                select(BankTransaction.id).where(
                    BankTransaction.org_id == request.org_id,
                    BankTransaction.fingerprint == fingerprint,
                )
            )
            if existing:
                duplicates.append(str(existing))
                continue
            transaction = BankTransaction(
                org_id=request.org_id,
                bank_account_code=request.bank_account_code,
                fingerprint=fingerprint,
                source_sha256=source_sha,
                **normalized,
            )
            session.add(transaction)
            session.flush()
            imported.append(str(transaction.id))
        except (ValueError, InvalidOperation) as exc:
            errors.append({"row": row_number, "error": str(exc)})
    return {
        "source_sha256": source_sha,
        "imported_count": len(imported),
        "duplicate_count": len(duplicates),
        "error_count": len(errors),
        "imported_ids": imported,
        "duplicate_ids": duplicates,
        "errors": errors,
    }


def _validate_mapping(mapping: dict[str, str]) -> None:
    unknown = set(mapping) - CANONICAL_COLUMNS
    if unknown:
        raise ValueError(f"unknown canonical columns: {sorted(unknown)}")
    if "booking_date" not in mapping:
        raise ValueError("column_mapping must include booking_date")
    if "amount" not in mapping and not ({"debit", "credit"} <= set(mapping)):
        raise ValueError("map amount, or map both debit and credit")


def _read_csv(path: Path) -> Iterable[dict[str, Any]]:
    raw = path.read_bytes()
    decoded: str | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise ValueError("CSV encoding must be UTF-8 or GB18030")
    return list(csv.DictReader(decoded.splitlines()))


def _read_xlsx(path: Path, sheet_name: str | None) -> Iterable[dict[str, Any]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        worksheet = workbook[sheet_name] if sheet_name else workbook.active
        rows = worksheet.iter_rows(values_only=True)
        headers = [str(value).strip() if value is not None else "" for value in next(rows)]
        return [dict(zip(headers, values, strict=True)) for values in rows]
    finally:
        workbook.close()


def _normalize_row(row: dict[str, Any], request: ImportBankStatementRequest) -> dict[str, Any]:
    def value(canonical: str, default: Any = None) -> Any:
        source_name = request.column_mapping.get(canonical)
        return row.get(source_name, default) if source_name else default

    booking_date = _parse_date(value("booking_date"), request.date_format)
    if "amount" in request.column_mapping:
        amount = _decimal(value("amount"))
    else:
        credit = _decimal(value("credit", 0), blank_zero=True)
        debit = _decimal(value("debit", 0), blank_zero=True)
        amount = credit - debit
    amount_fen = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if amount_fen == 0:
        raise ValueError("amount cannot be zero")
    currency = str(value("currency", "CNY") or "CNY").strip().upper()
    if currency != "CNY":
        raise ValueError("phase 1 supports CNY bank transactions only")
    return {
        "external_id": _optional_text(value("external_id")),
        "booking_date": booking_date,
        "amount_fen": amount_fen,
        "currency": currency,
        "counterparty_name": _optional_text(value("counterparty")),
        "memo": _optional_text(value("memo")) or "",
    }


def _parse_date(value: Any, date_format: str | None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if date_format:
        return datetime.strptime(text, date_format).date()
    return date.fromisoformat(text)


def _decimal(value: Any, *, blank_zero: bool = False) -> Decimal:
    if value is None or str(value).strip() == "":
        if blank_zero:
            return Decimal(0)
        raise ValueError("amount is blank")
    cleaned = str(value).strip().replace(",", "").replace("¥", "").replace("￥", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    return Decimal(cleaned)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fingerprint(org_id: uuid.UUID, bank_account_code: str, normalized: dict[str, Any]) -> str:
    identity = {
        "org_id": str(org_id),
        "bank_account_code": bank_account_code,
        "external_id": normalized["external_id"],
        "booking_date": normalized["booking_date"].isoformat(),
        "amount_fen": normalized["amount_fen"],
        "counterparty_name": normalized["counterparty_name"],
        "memo": normalized["memo"],
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
