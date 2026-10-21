"""Only synthetic catalogs, actual public assembly and loopback ephemeral ports."""

from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import SecretStr

from ai_accounting_modular.host.runtime._client import request
from ai_accounting_modular.host.runtime._security import new_credentials
from ai_accounting_modular.host.runtime.public import create_runtime
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog

PASSWORD = "synthetic-owner-password"


@pytest.fixture
def local(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    password, recovery, code = new_credentials(PASSWORD)
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", password, recovery))
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>合成看板</title>", encoding="utf-8")
    runtime = create_runtime(catalog, static_directory=static, window_opener=lambda _: None)
    company = str(uuid4())
    metadata = runtime.kernel.storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "合成运行公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    yield runtime, metadata, SecretStr(code)
    runtime.close()


def native_login(runtime):
    window = request(
        runtime.metadata,
        "/private/security",
        {
            "operation": "request",
            "payload": {"kind": "login"},
        },
    )
    result = request(
        runtime.metadata,
        "/private/native",
        {
            "operation": "execute",
            "payload": {"request_id": window["request_id"], "password": PASSWORD},
        },
    )
    assert result["login_completed"]


def business_request(company):
    evidence, supplier, opening, expense = [str(uuid4()) for _ in range(4)]
    text = "合成公司明确新设全零期初，一月未付维修费1000元。"
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "objects": [
            {
                "object_id": supplier,
                "kind": "organization",
                "name": "合成维修商",
                "expected_seq": 0,
                "data": {},
            }
        ],
        "sources": [
            {
                "source_id": evidence,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": "2026-01",
            }
        ],
        "mutations": [
            {
                "op": "create",
                "expected_seq": 0,
                "fact": {
                    "business_id": opening,
                    "kind": "opening.package",
                    "period": "2026-01",
                    "evidence_ids": [evidence],
                    "data": {"basis": "newly_established", "members": []},
                },
            },
            {
                "op": "create",
                "expected_seq": 0,
                "fact": {
                    "business_id": expense,
                    "kind": "operations.expense",
                    "period": "2026-01",
                    "evidence_ids": [evidence],
                    "data": {
                        "creditor_id": supplier,
                        "creditor_kind": "supplier",
                        "amount_fen": "100000",
                        "expense_class": "administration",
                    },
                },
            },
        ],
    }
