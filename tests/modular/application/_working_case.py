"""Real production composition with synthetic facts and temporary company files."""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog


@dataclass
class Case:
    kernel: object
    company: str
    source_id: str
    party: str

    @property
    def app(self):
        return self.kernel.application

    def request(self, *facts, **fields):
        return {
            "contract": "ai-accounting-modular/1",
            "company_id": self.company,
            "command_id": str(uuid4()),
            "mutations": [
                {"op": "create", "expected_seq": 0, "fact": deepcopy(fact)} for fact in facts
            ],
            **fields,
        }

    def seed(self, *, expense=True):
        text = "合成验收：公司新设且期初确为零。" + ("维修费1000元尚未支付。" if expense else "")
        facts = [
            {
                "business_id": str(uuid4()),
                "kind": "opening.package",
                "period": "2026-01",
                "evidence_ids": [self.source_id],
                "data": {"basis": "newly_established", "members": []},
            }
        ]
        if expense:
            facts.append(self.expense())
        result = self.app.accounting_apply(
            self.request(
                *facts,
                objects=[
                    {
                        "object_id": self.party,
                        "kind": "organization",
                        "name": "合成供应商",
                        "expected_seq": 0,
                        "data": {},
                    }
                ],
                sources=[
                    {
                        "source_id": self.source_id,
                        "kind": "owner_statement",
                        "text": text,
                        "sha256": sha256(text.encode()).hexdigest(),
                        "archive_month": "2026-01",
                    }
                ],
            )
        )
        assert result["completion"] == "complete", result
        return facts[-1] if expense else facts[0]

    def expense(self, amount="100000", period="2026-01"):
        return {
            "business_id": str(uuid4()),
            "kind": "operations.expense",
            "period": period,
            "evidence_ids": [self.source_id],
            "data": {
                "creditor_id": self.party,
                "creditor_kind": "supplier",
                "amount_fen": amount,
                "expense_class": "administration",
            },
        }

    def get(self, *ids, **fields):
        return self.app.accounting_get(
            {
                "company_id": self.company,
                "business_ids": list(ids),
                "include": ["facts", "results", "sources"],
                **fields,
            }
        )

    def context(self, **fields):
        return self.app.accounting_context({"company_id": self.company, **fields})

    def note_head(self, period="2026-01"):
        result = self.context(note_scopes=["period:" + period], issue_scopes=[])
        return result["note_basis_heads"][0]["head"]

    def check(self, period="2026-01"):
        return self.app.accounting_period(
            {"company_id": self.company, "action": "check", "period": period}
        )

    def close(self, checked):
        return self.app.accounting_period(
            {
                "company_id": self.company,
                "action": "close",
                "period": checked["period"],
                "expected_head": checked["head"],
            }
        )


@pytest.fixture
def case(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-hash"))
    kernel = create_kernel(catalog)
    company = str(uuid4())
    kernel.storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "合成应用公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    try:
        yield Case(kernel, company, str(uuid4()), str(uuid4()))
    finally:
        kernel.close()
