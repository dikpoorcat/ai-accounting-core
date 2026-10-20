from __future__ import annotations

from uuid import uuid4

import pytest

from ai_accounting_modular.storage.public import (
    CompanySpec,
    OwnerProfile,
    RepositoryFactory,
    Storage,
    create_catalog,
    module_schema,
)


class IdentityRepository:
    def __init__(self, session):
        self.session = session

    def add(self, ident=None, name="合成人员"):
        ident = ident or str(uuid4())
        self.session.execute(
            "INSERT INTO identity_object VALUES(?,'person',?,?,1,'{}',?,0,?)",
            (ident, name, name, "a" * 64, str(uuid4())),
        )
        return ident

    def names(self):
        return [
            row["name"]
            for row in self.session.fetchall("SELECT name FROM identity_object ORDER BY name")
        ]


class RecordsRepository:
    def __init__(self, session):
        self.session = session

    def publish(self, head):
        self.session.execute(
            "INSERT INTO document_set VALUES(1,?,'standard/current.json') "
            "ON CONFLICT(singleton) DO UPDATE SET published_set_id=excluded.published_set_id",
            (head,),
        )

    def source(self):
        self.session.execute(
            "INSERT INTO source(source_id,kind,sha256,statement_text) "
            "VALUES(?,'owner_statement',?,'当前事实')",
            (str(uuid4()), "b" * 64),
        )


@pytest.fixture
def storage_case(tmp_path):
    catalog_path = tmp_path / "catalog.sqlite3"
    create_catalog(catalog_path, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password-hash"))
    factories = [
        RepositoryFactory(module_schema("identity"), IdentityRepository),
        RepositoryFactory(module_schema("records"), RecordsRepository, RecordsRepository),
    ]
    storage = Storage(catalog_path, repositories=factories)
    company = storage.create_company(
        CompanySpec(
            company_id=str(uuid4()),
            database_id=str(uuid4()),
            credit_code="911111111111111111",
            legal_name="合成公司甲",
            start_period="2026-01",
            company_root=tmp_path / "company-a",
        )
    )
    return storage, company
