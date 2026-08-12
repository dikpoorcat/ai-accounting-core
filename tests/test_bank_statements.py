from __future__ import annotations

import uuid
from datetime import date
from decimal import localcontext
from io import BytesIO

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from ai_accounting.bank_statement_schemas import (
    BankReconciliationSystemFacts,
    BankStatementImportPreview,
    BankStatementImportSystemFacts,
    BankStatementNormalizedRow,
    ConfirmBankStatementImportRequest,
    ParsedBankStatement,
    PreviewBankReconciliationRequest,
    PreviewBankStatementImportRequest,
)
from ai_accounting.bank_statements import (
    calculate_bank_reconciliation,
    canonical_sha256,
    parse_bank_statement_bytes,
    preview_bank_statement_import,
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


def _reconciliation_request(**changes: object) -> PreviewBankReconciliationRequest:
    values: dict[str, object] = {
        "org_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "period_id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "bank_account_code": "1002",
        "coverage_start_date": "2026-08-01",
        "coverage_end_date": "2026-08-31",
        "statement_opening_balance_fen": 1_000,
        "statement_closing_balance_fen": 1_200,
        "statement_import_action_ids": [
            uuid.UUID("33333333-3333-3333-3333-333333333333")
        ],
        "statement_evidence_references": [
            uuid.UUID("44444444-4444-4444-4444-444444444444")
        ],
    }
    values.update(changes)
    return PreviewBankReconciliationRequest.model_validate(values)


def _system_facts(**changes: object) -> BankReconciliationSystemFacts:
    values: dict[str, object] = {
        "org_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "period_id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "bank_account_code": "1002",
        "period_start_date": "2026-08-01",
        "period_end_date": "2026-08-31",
        "book_closing_balance_fen": 1_200,
        "unmatched_transaction_count": 0,
        "pending_late_transaction_count": 0,
        "import_actions": [
            {
                "action_id": "33333333-3333-3333-3333-333333333333",
                "org_id": "11111111-1111-1111-1111-111111111111",
                "bank_account_code": "1002",
                "status": "posted",
                "request_payload_hash": "a" * 64,
                "calculation_hash": "b" * 64,
                "transactions": [
                    {
                        "transaction_id": "99999999-9999-9999-9999-999999999999",
                        "booking_date": "2026-08-08",
                        "amount_fen": 200,
                    }
                ],
            }
        ],
        "statement_evidence": [
            {
                "evidence_id": "44444444-4444-4444-4444-444444444444",
                "org_id": "11111111-1111-1111-1111-111111111111",
                "sha256": "c" * 64,
            }
        ],
    }
    values.update(changes)
    return BankReconciliationSystemFacts.model_validate(values)


def _import_system_facts(
    request: PreviewBankStatementImportRequest,
    parsed: ParsedBankStatement,
    *,
    as_of_date: str = "2026-08-11",
    period_status: str = "open",
    period_id: str | None = "aaaaaaaa-1111-1111-1111-111111111111",
    close_hash: str = "d" * 64,
    existing_external_by_row: dict[str, dict[str, object]] | None = None,
    manual_duplicate_by_row: dict[str, dict[str, object]] | None = None,
    evidence_org_id: uuid.UUID | None = None,
    evidence_sha_by_id: dict[uuid.UUID, str] | None = None,
) -> BankStatementImportSystemFacts:
    parsed_rows = parsed.rows
    existing_external_by_row = existing_external_by_row or {}
    manual_duplicate_by_row = manual_duplicate_by_row or {}
    if period_status == "closed":
        period: dict[str, object] = {
            "status": "closed",
            "period_start_date": "2026-08-01",
            "period_end_date": "2026-08-31",
            "period_id": period_id,
            "close_id": "aaaaaaaa-2222-2222-2222-222222222222",
            "close_hash": close_hash,
            "closed_at": "2026-09-05T08:00:00+00:00",
        }
    elif period_status == "open":
        period = {
            "status": "open",
            "period_start_date": "2026-08-01",
            "period_end_date": "2026-08-31",
            "period_id": period_id,
        }
    else:
        period = {
            "status": period_status,
            "period_start_date": "2026-08-01",
            "period_end_date": "2026-08-31",
        }
    evidence_ids = {
        evidence_id
        for resolution in request.missing_external_id_resolutions
        for evidence_id in resolution.evidence_references
    }
    evidence_sha_by_id = evidence_sha_by_id or {}
    values = {
        "org_id": request.org_id,
        "bank_account_code": request.bank_account_code,
        "as_of_date": as_of_date,
        "rows": [
            {
                "row_identity_sha256": row.row_identity_sha256,
                "period": period,
                "existing_external_id_transaction": existing_external_by_row.get(
                    row.row_identity_sha256
                ),
                "manual_duplicate_target": manual_duplicate_by_row.get(
                    row.row_identity_sha256
                ),
            }
            for row in parsed_rows
        ],
        "resolution_evidence": [
            {
                "evidence_id": evidence_id,
                "org_id": evidence_org_id or request.org_id,
                "sha256": evidence_sha_by_id.get(evidence_id, "e" * 64),
            }
            for evidence_id in sorted(evidence_ids, key=str)
        ],
    }
    return BankStatementImportSystemFacts.model_validate(values)


def _transaction_snapshot(
    request: PreviewBankStatementImportRequest,
    row: BankStatementNormalizedRow,
    *,
    transaction_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    bank_account_code: str | None = None,
    **changes: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "transaction_id": transaction_id or uuid.uuid4(),
        "org_id": org_id or request.org_id,
        "bank_account_code": bank_account_code or request.bank_account_code,
        "external_id": row.external_id,
        "booking_date": row.booking_date,
        "amount_fen": row.amount_fen,
        "currency": row.currency,
        "counterparty_name": row.counterparty_name,
        "memo": row.memo,
        "fingerprint": "f" * 64,
        "source_sha256": "1" * 64,
    }
    values.update(changes)
    return values


def _preview_import(
    request: PreviewBankStatementImportRequest,
    statement: bytes,
    **system_changes: object,
) -> BankStatementImportPreview:
    parsed = parse_bank_statement_bytes(request, statement)
    facts = _import_system_facts(request, parsed, **system_changes)
    return preview_bank_statement_import(request, parsed, facts)


def test_statement_bytes_are_hashed_and_parsed_deterministically() -> None:
    statement = (
        "date,amount,counterparty,memo,reference\n"
        "2026-08-08,10.01,甲客户,咨询费,A001\n"
    ).encode()
    request = _import_request()

    first = _preview_import(request, statement)
    second = _preview_import(request, statement)

    assert first.status == "calculated"
    assert first.calculation_hash == second.calculation_hash
    assert first.rows[0].amount_fen == 1001
    assert first.rows[0].disposition == "ready"
    assert first.data["planned_confirm_status"] == "posted"
    changed = _preview_import(request, statement.replace(b"A001", b"A002"))
    assert changed.calculation_hash != first.calculation_hash
    assert changed.rows[0].row_identity_sha256 != first.rows[0].row_identity_sha256


def test_bank_account_and_date_format_are_explicit_deterministic_facts() -> None:
    import_values = _import_request().model_dump()
    import_values.pop("bank_account_code")
    with pytest.raises(ValidationError):
        PreviewBankStatementImportRequest.model_validate(import_values)

    reconciliation_values = _reconciliation_request().model_dump()
    reconciliation_values.pop("bank_account_code")
    with pytest.raises(ValidationError):
        PreviewBankReconciliationRequest.model_validate(reconciliation_values)

    with pytest.raises(ValidationError):
        _import_request(date_format="%d %B %Y")


def test_missing_external_id_requires_explicit_evidence_backed_resolution() -> None:
    statement = (
        "date,amount,counterparty,memo,reference\n"
        "2026-08-08,100.00,同名客户,同额交易,\n"
    ).encode()
    unresolved = _preview_import(_import_request(), statement)

    assert unresolved.status == "needs_information"
    assert unresolved.rows[0].disposition == "needs_external_id_resolution"
    assert {
        item.code for item in unresolved.missing_information
    } == {"BANK_STATEMENT_EXTERNAL_ID_RESOLUTION_REQUIRED"}
    row = unresolved.rows[0]
    resolution = {
        "row_number": row.row_number,
        "row_identity_sha256": row.row_identity_sha256,
        "decision": "confirm_new",
        "explanation": "银行未提供稳定流水号，已按回单逐笔确认是独立交易",
        "evidence_references": ["55555555-5555-5555-5555-555555555555"],
    }
    resolved = _preview_import(
        _import_request(missing_external_id_resolutions=[resolution]), statement
    )

    assert resolved.status == "calculated"
    assert resolved.rows[0].disposition == "manual_new"
    assert resolved.data["planned_import_count"] == 1
    changed_explanation = _preview_import(
        _import_request(
            missing_external_id_resolutions=[
                {**resolution, "explanation": "另一份人工确认说明"}
            ]
        ),
        statement,
    )
    assert changed_explanation.calculation_hash != resolved.calculation_hash


def test_missing_external_id_duplicate_resolution_names_exact_existing_row() -> None:
    statement = b"date,amount,reference\n2026-08-08,100.00,\n"
    mapping = {"booking_date": "date", "amount": "amount", "external_id": "reference"}
    unresolved = _preview_import(_import_request(column_mapping=mapping), statement)
    row = unresolved.rows[0]
    duplicate_id = uuid.UUID("66666666-6666-6666-6666-666666666666")
    resolved_request = _import_request(
        column_mapping=mapping,
        missing_external_id_resolutions=[
            {
                "row_number": row.row_number,
                "row_identity_sha256": row.row_identity_sha256,
                "decision": "confirm_duplicate",
                "duplicate_bank_transaction_id": duplicate_id,
                "explanation": "已与原银行回单逐项核对为同一交易",
                "evidence_references": ["77777777-7777-7777-7777-777777777777"],
            }
        ],
    )
    parsed = parse_bank_statement_bytes(resolved_request, statement)
    resolved = preview_bank_statement_import(
        resolved_request,
        parsed,
        _import_system_facts(
            resolved_request,
            parsed,
            manual_duplicate_by_row={
                row.row_identity_sha256: _transaction_snapshot(
                    resolved_request,
                    parsed.rows[0],
                    transaction_id=duplicate_id,
                    external_id=None,
                )
            },
        ),
    )

    assert resolved.status == "calculated"
    assert resolved.rows[0].disposition == "manual_duplicate"
    assert resolved.rows[0].duplicate_bank_transaction_id == duplicate_id
    assert resolved.data["planned_import_count"] == 0
    assert resolved.data["planned_duplicate_count"] == 1


def test_row_errors_need_explicit_partial_import_choice_and_do_not_echo_values() -> None:
    sentinel = "PRIVATE-BAD-DATE"
    statement = (
        "date,amount,reference\n"
        "2026-08-08,100.00,A001\n"
        f"{sentinel},200.00,A002\n"
    ).encode()
    default = _preview_import(
        _import_request(
            column_mapping={
                "booking_date": "date",
                "amount": "amount",
                "external_id": "reference",
            }
        ),
        statement,
    )

    assert default.status == "needs_information"
    assert default.errors[0].model_dump() == {
        "code": "BANK_STATEMENT_INVALID_DATE",
        "row_number": 3,
        "field_path": "booking_date",
    }
    assert sentinel not in str(default.errors)
    assert default.calculation_hash is None
    assert default.data == {}

    explicit = _preview_import(
        _import_request(
            column_mapping={
                "booking_date": "date",
                "amount": "amount",
                "external_id": "reference",
            },
            proceed_with_known_row_errors=True,
        ),
        statement,
    )
    assert explicit.status == "calculated"
    assert explicit.data["partial_import_expected"] is True
    assert explicit.data["planned_confirm_status"] == "partially_posted"


def test_xlsx_is_parsed_from_the_same_in_memory_bytes() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["date", "amount", "reference"])
    sheet.append([date(2026, 8, 8), "10.01", "X-1"])
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
    result = _preview_import(
        request,
        output.getvalue(),
    )

    assert result.status == "calculated"
    assert result.rows[0].amount_fen == 1001


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


def test_public_confirm_schema_has_no_caller_supplied_actor() -> None:
    with pytest.raises(ValidationError):
        ConfirmBankStatementImportRequest.model_validate(
            {
                **_import_request().model_dump(mode="json"),
                "calculation_hash": "a" * 64,
                "idempotency_key": "import-1",
                "actor_id": "caller-supplied-actor",
            }
        )
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


def test_formal_preview_requires_exactly_one_system_fact_per_normalized_row() -> None:
    request = _import_request(
        column_mapping={
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
        }
    )
    statement = (
        b"date,amount,reference\n"
        b"2026-08-08,10.00,A001\n"
        b"2026-08-09,20.00,A002\n"
    )
    parsed = parse_bank_statement_bytes(request, statement)
    complete = _import_system_facts(request, parsed)
    incomplete_values = complete.model_dump(mode="json")
    incomplete_values["rows"] = incomplete_values["rows"][:1]

    result = preview_bank_statement_import(
        request,
        parsed,
        BankStatementImportSystemFacts.model_validate(incomplete_values),
    )

    assert result.status == "rejected"
    assert {item.code for item in result.errors} == {
        "BANK_STATEMENT_SYSTEM_ROW_FACTS_MISMATCH"
    }
    assert result.calculation_hash is None
    assert result.data == {}


def test_stable_external_id_duplicate_requires_same_immutable_facts_and_scope() -> None:
    request = _import_request()
    statement = (
        "date,amount,counterparty,memo,reference\n"
        "2026-08-08,10.01,甲客户,咨询费,A001\n"
    ).encode()
    parsed = parse_bank_statement_bytes(request, statement)
    row = parsed.rows[0]
    exact_snapshot = _transaction_snapshot(request, row)

    duplicate = preview_bank_statement_import(
        request,
        parsed,
        _import_system_facts(
            request,
            parsed,
            existing_external_by_row={row.row_identity_sha256: exact_snapshot},
        ),
    )
    assert duplicate.status == "calculated"
    assert duplicate.rows[0].disposition == "stable_duplicate"

    conflict = preview_bank_statement_import(
        request,
        parsed,
        _import_system_facts(
            request,
            parsed,
            existing_external_by_row={
                row.row_identity_sha256: {**exact_snapshot, "amount_fen": 1002}
            },
        ),
    )
    assert conflict.status == "rejected"
    assert "BANK_STATEMENT_EXTERNAL_ID_FACT_CONFLICT" in {
        item.code for item in conflict.errors
    }
    assert conflict.data == {}

    cross_org_id = uuid.UUID("99999999-1111-1111-1111-111111111111")
    cross_org_snapshot = {
        **exact_snapshot,
        "org_id": cross_org_id,
        "transaction_id": "99999999-2222-2222-2222-222222222222",
    }
    cross_org = preview_bank_statement_import(
        request,
        parsed,
        _import_system_facts(
            request,
            parsed,
            existing_external_by_row={row.row_identity_sha256: cross_org_snapshot},
        ),
    )
    assert cross_org.status == "rejected"
    assert "BANK_STATEMENT_EXTERNAL_ID_TRANSACTION_SCOPE_MISMATCH" in {
        item.code for item in cross_org.errors
    }
    assert cross_org.data == {}
    assert "99999999-2222-2222-2222-222222222222" not in str(cross_org.trace)


def test_duplicate_external_ids_inside_one_source_are_rejected() -> None:
    request = _import_request(
        column_mapping={
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
        }
    )
    statement = (
        b"date,amount,reference\n"
        b"2026-08-08,10.00,A001\n"
        b"2026-08-08,10.00,A001\n"
    )

    result = _preview_import(request, statement)

    assert result.status == "rejected"
    assert {item.code for item in result.errors} == {
        "BANK_STATEMENT_DUPLICATE_EXTERNAL_ID_IN_SOURCE"
    }


def test_closed_period_is_server_derived_late_projection_and_frozen_in_hash() -> None:
    request = _import_request()
    statement = (
        b"date,amount,counterparty,memo,reference\n"
        b"2026-08-08,10.01,customer,fee,A001\n"
    )

    first = _preview_import(
        request,
        statement,
        period_status="closed",
        as_of_date="2026-09-10",
        close_hash="a" * 64,
    )
    changed_close = _preview_import(
        request,
        statement,
        period_status="closed",
        as_of_date="2026-09-10",
        close_hash="b" * 64,
    )

    assert first.status == "calculated"
    assert first.rows[0].is_late is True
    assert first.rows[0].period_status == "closed"
    assert first.rows[0].original_close_id is not None
    assert first.rows[0].original_close_hash == "a" * 64
    assert first.data["late_import_count"] == 1
    assert changed_close.calculation_hash != first.calculation_hash


def test_future_and_unavailable_period_rows_are_not_importable() -> None:
    request = _import_request(
        column_mapping={
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
        }
    )
    future = _preview_import(
        request,
        b"date,amount,reference\n2026-08-12,10.00,A001\n",
        as_of_date="2026-08-11",
    )
    unavailable = _preview_import(
        request,
        b"date,amount,reference\n2026-08-08,10.00,A001\n",
        period_status="not_generated",
    )

    assert future.status == "rejected"
    assert future.errors[0].code == "BANK_STATEMENT_FUTURE_BOOKING_DATE_NOT_ALLOWED"
    assert future.data == {}
    assert unavailable.status == "needs_information"
    assert unavailable.missing_information[0].code == "BANK_STATEMENT_PERIOD_NOT_GENERATED"
    assert unavailable.data == {}


def test_resolution_evidence_scope_and_sha_are_formal_hash_inputs() -> None:
    statement = b"date,amount,reference\n2026-08-08,10.00,\n"
    base_request = _import_request(
        column_mapping={
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
        }
    )
    row = parse_bank_statement_bytes(base_request, statement).rows[0]
    evidence_id = uuid.UUID("55555555-5555-5555-5555-555555555555")
    request = _import_request(
        column_mapping=base_request.column_mapping,
        missing_external_id_resolutions=[
            {
                "row_number": row.row_number,
                "row_identity_sha256": row.row_identity_sha256,
                "decision": "confirm_new",
                "explanation": "逐笔核对为独立交易",
                "evidence_references": [evidence_id],
            }
        ],
    )
    parsed = parse_bank_statement_bytes(request, statement)
    first = preview_bank_statement_import(
        request,
        parsed,
        _import_system_facts(
            request,
            parsed,
            evidence_sha_by_id={evidence_id: "a" * 64},
        ),
    )
    changed_sha = preview_bank_statement_import(
        request,
        parsed,
        _import_system_facts(
            request,
            parsed,
            evidence_sha_by_id={evidence_id: "b" * 64},
        ),
    )
    cross_org = preview_bank_statement_import(
        request,
        parsed,
        _import_system_facts(
            request,
            parsed,
            evidence_org_id=uuid.UUID("99999999-1111-1111-1111-111111111111"),
        ),
    )

    assert first.status == "calculated"
    assert changed_sha.calculation_hash != first.calculation_hash
    assert cross_org.status == "rejected"
    assert "BANK_STATEMENT_RESOLUTION_EVIDENCE_SCOPE_MISMATCH" in {
        item.code for item in cross_org.errors
    }
    assert cross_org.data == {}


def test_as_of_date_is_part_of_formal_import_hash() -> None:
    request = _import_request()
    statement = (
        b"date,amount,counterparty,memo,reference\n"
        b"2026-08-08,10.01,customer,fee,A001\n"
    )

    first = _preview_import(request, statement, as_of_date="2026-08-11")
    second = _preview_import(request, statement, as_of_date="2026-08-12")

    assert first.status == second.status == "calculated"
    assert first.calculation_hash != second.calculation_hash


def test_statement_amount_and_text_are_bounded_before_persistence() -> None:
    request = _import_request(
        column_mapping={
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
        }
    )
    too_large = _preview_import(
        request,
        b"date,amount,reference\n2026-08-08,92233720368547758.08,A001\n",
    )
    assert too_large.status == "needs_information"
    assert too_large.errors[0].code == "BANK_STATEMENT_AMOUNT_OUT_OF_RANGE"

    long_external_id = (
        "date,amount,reference\n2026-08-08,1.00," + "x" * 101 + "\n"
    ).encode()
    long_text = _preview_import(request, long_external_id)
    assert long_text.errors[0].code == "BANK_STATEMENT_TEXT_TOO_LONG"
    assert long_text.errors[0].field_path == "external_id"


def test_debit_credit_calculation_does_not_inherit_global_decimal_precision() -> None:
    request = _import_request(
        column_mapping={
            "booking_date": "date",
            "debit": "debit",
            "credit": "credit",
            "external_id": "reference",
        }
    )
    statement = (
        b"date,debit,credit,reference\n"
        b"2026-08-08,1234567890123456.12,1234567890123456.13,A001\n"
    )
    results: list[int] = []
    for precision in (6, 16, 28, 50):
        with localcontext() as context:
            context.prec = precision
            result = _preview_import(request, statement)
        assert result.status == "calculated"
        results.append(result.rows[0].amount_fen)
    assert results == [1, 1, 1, 1]


def test_reconciliation_calculates_zero_differences_and_stable_hash() -> None:
    request = _reconciliation_request()
    facts = _system_facts()

    first = calculate_bank_reconciliation(request, facts)
    second = calculate_bank_reconciliation(request, facts)

    assert first.status == "calculated"
    assert first.calculation_hash == second.calculation_hash
    assert first.warnings == []
    calculation = first.data["calculation"]
    assert calculation["statement_integrity_difference_fen"] == 0
    assert calculation["statement_to_book_difference_fen"] == 0


def test_reconciliation_requires_missing_coverage_balances_and_evidence() -> None:
    request = PreviewBankReconciliationRequest(
        org_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        period_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        bank_account_code="1002",
    )

    result = calculate_bank_reconciliation(
        request,
        _system_facts(import_actions=[], statement_evidence=[]),
    )

    assert result.status == "needs_information"
    fields = {
        field
        for requirement in result.missing_information
        for field in requirement.fields
    }
    assert {
        "coverage_start_date",
        "coverage_end_date",
        "statement_opening_balance_fen",
        "statement_closing_balance_fen",
        "statement_evidence_references",
    } <= fields
    assert result.calculation_hash is None


def test_explained_statement_to_book_difference_warns_but_can_be_confirmed() -> None:
    evidence_id = "88888888-8888-8888-8888-888888888888"
    request = _reconciliation_request(
        statement_closing_balance_fen=1_250,
        statement_evidence_references=[
            "44444444-4444-4444-4444-444444444444",
            evidence_id,
        ],
        difference_explanations=[
            {
                "difference_kind": "statement_to_book",
                "amount_fen": 50,
                "explanation": "银行计息在途，次月补入",
                "evidence_references": [evidence_id],
            },
        ],
    )

    result = calculate_bank_reconciliation(
        request,
        _system_facts(
            import_actions=[
                {
                    **_system_facts().import_actions[0].model_dump(mode="json"),
                    "transactions": [
                        {
                            "transaction_id": "99999999-9999-9999-9999-999999999999",
                            "booking_date": "2026-08-08",
                            "amount_fen": 250,
                        }
                    ],
                }
            ],
            statement_evidence=[
                *_system_facts().model_dump(mode="json")["statement_evidence"],
                {
                    "evidence_id": evidence_id,
                    "org_id": "11111111-1111-1111-1111-111111111111",
                    "sha256": "8" * 64,
                },
            ],
        ),
    )

    assert result.status == "calculated"
    assert result.calculation_hash is not None
    assert [warning["code"] for warning in result.warnings] == [
        "BANK_RECONCILIATION_EXPLAINED_DIFFERENCE"
    ]


def test_reconciliation_rejects_explanation_total_that_does_not_match() -> None:
    request = _reconciliation_request(
        statement_closing_balance_fen=1_250,
        statement_evidence_references=[
            "44444444-4444-4444-4444-444444444444",
            "88888888-8888-8888-8888-888888888888",
        ],
        difference_explanations=[
            {
                "difference_kind": "statement_to_book",
                "amount_fen": 40,
                "explanation": "银行计息在途",
                "evidence_references": ["88888888-8888-8888-8888-888888888888"],
            },
        ],
    )

    result = calculate_bank_reconciliation(
        request,
        _system_facts(
            import_actions=[
                {
                    **_system_facts().import_actions[0].model_dump(mode="json"),
                    "transactions": [
                        {
                            "transaction_id": "99999999-9999-9999-9999-999999999999",
                            "booking_date": "2026-08-08",
                            "amount_fen": 250,
                        }
                    ],
                }
            ],
            statement_evidence=[
                *_system_facts().model_dump(mode="json")["statement_evidence"],
                {
                    "evidence_id": "88888888-8888-8888-8888-888888888888",
                    "org_id": "11111111-1111-1111-1111-111111111111",
                    "sha256": "8" * 64,
                },
            ],
        ),
    )

    assert result.status == "rejected"
    assert "BANK_RECONCILIATION_DIFFERENCE_EXPLANATION_MISMATCH" in {
        issue.code for issue in result.errors
    }
    assert result.calculation_hash is None


def test_statement_rollforward_mismatch_cannot_be_explained_away() -> None:
    result = calculate_bank_reconciliation(
        _reconciliation_request(statement_closing_balance_fen=1_250),
        _system_facts(),
    )

    assert result.status == "rejected"
    assert {
        issue.code for issue in result.errors
    } == {"BANK_RECONCILIATION_STATEMENT_ROLLFORWARD_MISMATCH"}
    assert result.calculation_hash is None


def test_reconciliation_rejects_unresolved_or_cross_org_action_and_evidence_ids() -> None:
    facts = _system_facts()
    values = facts.model_dump(mode="json")
    values["import_actions"][0]["org_id"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    values["statement_evidence"] = []

    result = calculate_bank_reconciliation(
        _reconciliation_request(),
        BankReconciliationSystemFacts.model_validate(values),
    )

    assert result.status == "rejected"
    assert {
        issue.code for issue in result.errors
    } >= {
        "BANK_RECONCILIATION_IMPORT_ACTION_SCOPE_MISMATCH",
        "BANK_RECONCILIATION_EVIDENCE_FACTS_MISMATCH",
    }
    assert result.data == {}
    rendered_trace = str(result.trace)
    assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" not in rendered_trace
    assert "2026-08-08" not in rendered_trace
    assert "a" * 64 not in rendered_trace


def test_reconciliation_explanation_evidence_must_be_resolved_in_same_org() -> None:
    explanation_evidence_id = "88888888-8888-8888-8888-888888888888"
    request = _reconciliation_request(
        statement_closing_balance_fen=1_250,
        statement_evidence_references=[
            "44444444-4444-4444-4444-444444444444",
            explanation_evidence_id,
        ],
        difference_explanations=[
            {
                "difference_kind": "statement_to_book",
                "amount_fen": 50,
                "explanation": "银行计息在途",
                "evidence_references": [explanation_evidence_id],
            }
        ],
    )
    facts = _system_facts(
        import_actions=[
            {
                **_system_facts().import_actions[0].model_dump(mode="json"),
                "transactions": [
                    {
                        "transaction_id": "99999999-9999-9999-9999-999999999999",
                        "booking_date": "2026-08-08",
                        "amount_fen": 250,
                    }
                ],
            }
        ],
        statement_evidence=[
            *_system_facts().model_dump(mode="json")["statement_evidence"],
            {
                "evidence_id": explanation_evidence_id,
                "org_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "sha256": "8" * 64,
            },
        ],
    )

    result = calculate_bank_reconciliation(request, facts)

    assert result.status == "rejected"
    assert "BANK_RECONCILIATION_EVIDENCE_SCOPE_MISMATCH" in {
        item.code for item in result.errors
    }
    assert result.data == {}


def test_reconciliation_coverage_must_equal_the_natural_month_exactly() -> None:
    request = _reconciliation_request(
        coverage_start_date="2026-07-31",
        coverage_end_date="2026-09-01",
    )

    result = calculate_bank_reconciliation(request, _system_facts())

    assert result.status == "needs_information"
    assert result.missing_information[0].code == (
        "BANK_RECONCILIATION_COVERAGE_MUST_MATCH_PERIOD"
    )
    assert result.data == {}


def test_zero_transaction_month_can_be_reconciled_with_balances_and_evidence() -> None:
    request = _reconciliation_request(
        statement_opening_balance_fen=1_000,
        statement_closing_balance_fen=1_000,
        statement_import_action_ids=[],
    )
    facts = _system_facts(
        book_closing_balance_fen=1_000,
        import_actions=[],
    )

    result = calculate_bank_reconciliation(request, facts)

    assert result.status == "calculated"
    assert result.data["calculation"]["statement_transaction_count"] == 0
    assert result.data["calculation"]["statement_movement_fen"] == 0


def test_reconciliation_current_unmatched_and_pending_late_counts_are_warnings() -> None:
    result = calculate_bank_reconciliation(
        _reconciliation_request(),
        _system_facts(unmatched_transaction_count=2, pending_late_transaction_count=1),
    )

    assert result.status == "calculated"
    assert [warning["code"] for warning in result.warnings] == [
        "BANK_RECONCILIATION_UNMATCHED_TRANSACTIONS_REVIEW",
        "BANK_RECONCILIATION_PENDING_LATE_EVIDENCE_REVIEW",
    ]


def test_reconciliation_hash_uses_period_dates_resolved_sha_and_sorted_evidence_ids() -> None:
    first_evidence = "44444444-4444-4444-4444-444444444444"
    second_evidence = "88888888-8888-8888-8888-888888888888"

    def request_for(order: list[str]) -> PreviewBankReconciliationRequest:
        return _reconciliation_request(
            statement_closing_balance_fen=1_250,
            statement_evidence_references=order,
            difference_explanations=[
                {
                    "difference_kind": "statement_to_book",
                    "amount_fen": 50,
                    "explanation": "银行计息在途",
                    "evidence_references": order,
                }
            ],
        )

    transaction_action = {
        **_system_facts().import_actions[0].model_dump(mode="json"),
        "transactions": [
            {
                "transaction_id": "99999999-9999-9999-9999-999999999999",
                "booking_date": "2026-08-08",
                "amount_fen": 250,
            }
        ],
    }
    evidence = [
        {
            "evidence_id": first_evidence,
            "org_id": "11111111-1111-1111-1111-111111111111",
            "sha256": "4" * 64,
        },
        {
            "evidence_id": second_evidence,
            "org_id": "11111111-1111-1111-1111-111111111111",
            "sha256": "8" * 64,
        },
    ]
    first = calculate_bank_reconciliation(
        request_for([first_evidence, second_evidence]),
        _system_facts(import_actions=[transaction_action], statement_evidence=evidence),
    )
    reordered = calculate_bank_reconciliation(
        request_for([second_evidence, first_evidence]),
        _system_facts(
            import_actions=[transaction_action],
            statement_evidence=list(reversed(evidence)),
        ),
    )
    changed_sha = calculate_bank_reconciliation(
        request_for([first_evidence, second_evidence]),
        _system_facts(
            import_actions=[transaction_action],
            statement_evidence=[
                evidence[0],
                {**evidence[1], "sha256": "9" * 64},
            ],
        ),
    )

    assert first.status == reordered.status == changed_sha.status == "calculated"
    assert first.calculation_hash == reordered.calculation_hash
    assert first.calculation_hash != changed_sha.calculation_hash
    assert first.data["calculation"]["period_start_date"] == date(2026, 8, 1)
    assert first.data["calculation"]["period_end_date"] == date(2026, 8, 31)
