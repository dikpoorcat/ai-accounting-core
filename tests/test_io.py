from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.bank_import import BankStatementInputError, import_bank_statement
from ai_accounting.config import Settings
from ai_accounting.evidence import register_evidence
from ai_accounting.models import BankTransaction, Organization
from ai_accounting.schemas import ImportBankStatementRequest, RegisterEvidenceRequest


def test_evidence_is_content_addressed_and_deduplicated(
    session: Session, organization: Organization, tmp_path: Path
) -> None:
    source = tmp_path / "receipt.txt"
    source.write_bytes("银行回单".encode())
    settings = Settings(
        database_url="sqlite://",
        finance_evidence_dir=tmp_path / "evidence",
        finance_max_evidence_bytes=1024,
    )
    request = RegisterEvidenceRequest(
        org_id=organization.id,
        source="bank_receipt",
        file_path=source,
        media_type="text/plain",
    )
    first = register_evidence(session, request, settings)
    second = register_evidence(session, request, settings)

    assert first.id == second.id
    assert first.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert Path(first.storage_path).read_bytes() == source.read_bytes()


def test_csv_bank_import_maps_columns_and_skips_duplicates(
    session: Session, organization: Organization, tmp_path: Path
) -> None:
    statement = tmp_path / "bank.csv"
    statement.write_text(
        "交易日,金额,对方,摘要,流水号\n2026-08-08,10100.00,甲客户,咨询费,A001\n",
        encoding="utf-8-sig",
    )
    request = ImportBankStatementRequest(
        org_id=organization.id,
        file_path=statement,
        column_mapping={
            "booking_date": "交易日",
            "amount": "金额",
            "counterparty": "对方",
            "memo": "摘要",
            "external_id": "流水号",
        },
    )
    first = import_bank_statement(session, request)
    second = import_bank_statement(session, request)

    assert first["imported_count"] == 1
    assert second["duplicate_count"] == 1
    transaction = session.scalar(select(BankTransaction))
    assert transaction.amount_fen == 1_010_000
    assert transaction.counterparty_name == "甲客户"


def test_legacy_bank_import_is_disabled_in_production(
    session: Session,
    organization: Organization,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_accounting import bank_import

    statement = tmp_path / "bank.csv"
    statement.write_text("date,amount\n2026-08-08,1.00\n", encoding="utf-8")
    request = ImportBankStatementRequest(
        org_id=organization.id,
        file_path=statement,
        column_mapping={"booking_date": "date", "amount": "amount"},
    )
    monkeypatch.setattr(
        bank_import,
        "get_settings",
        lambda: SimpleNamespace(finance_environment="production"),
    )

    with pytest.raises(
        BankStatementInputError,
        match="BANK_STATEMENT_PREVIEW_CONFIRM_REQUIRED",
    ):
        import_bank_statement(session, request)
