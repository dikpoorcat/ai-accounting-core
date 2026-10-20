"""A real, isolated application assembly without production directories."""

from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog


@pytest.fixture
def assembled(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password-hash"))
    storage = Storage(
        catalog,
        repositories=[
            application_repository(),
            identity_repository(),
            operations_repository(),
            ledger_repository(),
            records_repository(),
        ],
    )
    company_id = str(uuid4())
    storage.create_company(
        CompanySpec(
            company_id,
            str(uuid4()),
            "913100000000000001",
            "合成应用验收公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    identity, ledger, operations = create_identity(), create_ledger(), create_operations()
    records = create_records(root_for=lambda read: storage.company_metadata(read).company_root)
    app = create_application(
        storage=storage,
        identity=identity,
        ledger=ledger,
        records=records,
        domains=[operations],
    )
    return app, company_id


def source(text="合成验收：维修费用为1000元，随后按新事实更正。"):
    return {
        "source_id": str(uuid4()),
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
        "archive_month": "2026-01",
    }


def initial_request(company_id, *, business_id=None, amount="100000"):
    party, evidence = str(uuid4()), source()
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company_id,
        "command_id": str(uuid4()),
        "objects": [
            {
                "object_id": party,
                "kind": "organization",
                "name": "合成维修商",
                "expected_seq": 0,
                "data": {},
            }
        ],
        "sources": [evidence],
        "mutations": [
            {
                "op": "create",
                "expected_seq": 0,
                "fact": {
                    "business_id": business_id or str(uuid4()),
                    "kind": "operations.expense",
                    "period": "2026-01",
                    "evidence_ids": [evidence["source_id"]],
                    "data": {
                        "creditor_id": party,
                        "creditor_kind": "supplier",
                        "amount_fen": amount,
                        "expense_class": "administration",
                    },
                },
            }
        ],
    }


def get(app, company_id, *ids):
    return app.accounting_get(
        {
            "company_id": company_id,
            "business_ids": list(ids),
            "include": ["facts", "results", "sources"],
        }
    )
