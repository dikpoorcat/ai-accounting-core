"""Preserved pure regressions from tests/test_bank_statements.py; no legacy service fixtures."""

from __future__ import annotations

import uuid
from datetime import date
from io import BytesIO

import pytest
from openpyxl import Workbook

from ai_accounting.bank_statement_schemas import (
    PreviewBankStatementImportRequest,
)
from ai_accounting.bank_statements import (
    canonical_sha256,
    parse_bank_statement_bytes,
)


def _import_request(**changes: object) -> PreviewBankStatementImportRequest:
    values: dict[str, object] = {
        "org_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "bank_account_code": "1002",
        "file_format": "csv",
        "column_mapping": {
            "booking_date": "date",
            "amount": "amount",
            "counterparty": "counterparty",
            "memo": "memo",
            "external_id": "reference",
        },
    }
    values.update(changes)
    return PreviewBankStatementImportRequest.model_validate(values)


def test_xlsx_binary_float_amount_is_rejected_as_inexact() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["date", "amount", "reference"])
    sheet.append([date(2026, 8, 8), 10.01, "X-1"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    request = _import_request(
        file_format="xlsx",
        column_mapping={
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
        },
    )
    result = parse_bank_statement_bytes(request, output.getvalue())

    assert result.status == "parsed"
    assert result.rows == []
    assert [issue.code for issue in result.errors] == ["BANK_STATEMENT_INVALID_AMOUNT"]
    assert result.errors[0].field_path == "amount"


def test_canonical_hash_rejects_binary_floating_point() -> None:
    with pytest.raises(TypeError, match="binary floating point"):
        canonical_sha256({"amount": 0.1})


def test_parser_has_no_formal_hash_and_rejects_non_fen_precision() -> None:
    request = _import_request(
        column_mapping={
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
        }
    )

    parsed = parse_bank_statement_bytes(
        request,
        b"date,amount,reference\n2026-08-08,10.005,A001\n",
    )

    assert parsed.status == "parsed"
    assert not hasattr(parsed, "calculation_hash")
    assert parsed.rows == []
    assert parsed.errors[0].code == "BANK_STATEMENT_AMOUNT_NOT_EXACT_FEN"


@pytest.mark.parametrize(
    ("amount", "expected_code"),
    [
        ("1E+999999999", "BANK_STATEMENT_AMOUNT_OUT_OF_RANGE"),
        ("1E-999999999", "BANK_STATEMENT_AMOUNT_NOT_EXACT_FEN"),
    ],
)
def test_parser_rejects_extreme_decimal_exponents_without_expanding_them(
    amount: str,
    expected_code: str,
) -> None:
    request = _import_request(
        column_mapping={
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
        }
    )

    parsed = parse_bank_statement_bytes(
        request,
        f"date,amount,reference\n2026-08-08,{amount},A001\n".encode(),
    )

    assert parsed.status == "parsed"
    assert parsed.rows == []
    assert parsed.errors[0].code == expected_code
