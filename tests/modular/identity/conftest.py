from __future__ import annotations

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import content_digest
from ai_accounting_modular.identity.public import create_identity, repository_factory
from ai_accounting_modular.storage.public import (
    CompanySpec,
    OwnerProfile,
    RepositoryFactory,
    Storage,
    create_catalog,
    module_schema,
)


class SourceEdges:
    """Synthetic records owner mirrors the plan's source uses as derived indexes."""

    def __init__(self, session):
        self.session = session

    def save(self, source_uses):
        for object_id, ids in source_uses.items():
            self.session.execute(
                "DELETE FROM source_use WHERE owner_kind='object' AND owner_key=?", (object_id,)
            )
            for source_id in ids:
                self.session.execute(
                    "INSERT OR IGNORE INTO source(source_id,kind,sha256,statement_text) "
                    "VALUES(?,'owner_statement',?,'合成老板说明')",
                    (source_id, content_digest("合成老板说明")),
                )
                self.session.execute(
                    "INSERT INTO source_use(owner_kind,owner_key,source_id) VALUES('object',?,?)",
                    (object_id, source_id),
                )


@pytest.fixture
def identity_case(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password-hash"))
    storage = Storage(
        catalog,
        repositories=[
            repository_factory(),
            RepositoryFactory(module_schema("records"), SourceEdges),
        ],
    )
    companies = tuple(
        storage.create_company(
            CompanySpec(
                str(uuid4()),
                str(uuid4()),
                "91111111111111111" + str(index),
                "合成公司" + str(index),
                "2026-01",
                tmp_path / f"company-{index}",
            )
        )
        for index in (1, 2)
    )
    return storage, companies, create_identity()
