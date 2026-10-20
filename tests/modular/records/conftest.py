from __future__ import annotations

from uuid import uuid4

import pytest

from ai_accounting_modular.records.public import create_records, repository_factory
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog


@pytest.fixture
def records_case(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password-hash"))
    storage = Storage(catalog, repositories=(repository_factory(),))
    companies = tuple(
        storage.create_company(
            CompanySpec(
                str(uuid4()),
                str(uuid4()),
                "91111111111111111" + str(index),
                "资料合成公司" + str(index),
                "2026-01",
                tmp_path / f"company-{index}",
            )
        )
        for index in (1, 2)
    )
    records = create_records(lambda read: storage.company_metadata(read).company_root)
    return storage, companies, records
