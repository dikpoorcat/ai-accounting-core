"""Real production composition with synthetic facts and temporary company files."""

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog
from tests.modular._period_support import checked_close_request


@dataclass
class Case:
    kernel: object
    company: str
    source_id: str
    party: str
    prepared_closes: dict = field(default_factory=dict, repr=False)
    coverage_sources: dict = field(default_factory=dict, repr=False)

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
        return self.app.accounting_period(deepcopy(self.prepared_closes[checked["head"]]))

    def prepare_close(self, period="2026-01"):
        """Explicitly declare the complete synthetic scenario, then adopt its version."""
        discovered = self.check(period)
        first = discovered["actual_range"]["period_from"]
        key = (first, period)
        source_id = self.coverage_sources.get(key)
        if source_id is None:
            source_id = str(uuid4())
            text = (
                f"合成测试资料交接：{first}至{period}本场景全部业务资料均由用例提供，"
                "无其他未交资料。"
            )
            result = self.app.accounting_apply(
                self.request(
                    sources=[
                        {
                            "source_id": source_id,
                            "kind": "owner_statement",
                            "text": text,
                            "sha256": sha256(text.encode()).hexdigest(),
                            "archive_month": period,
                        }
                    ]
                )
            )
            assert result["completion"] == "complete", result
            self.coverage_sources[key] = source_id
        checked, request = checked_close_request(self.app, self.company, period, source_id)
        self.prepared_closes[checked["head"]] = request
        return checked

    def recheck(self, checked):
        """Recheck exactly the already-reviewed material version without replacing it."""
        request = self.prepared_closes[checked["head"]]
        result = self.app.accounting_period(
            {
                "company_id": self.company,
                "action": "check",
                "period": request["actual_range"]["period_to"],
                "basis": deepcopy(request["basis"]),
                "expected_material_version": request["expected_material_version"],
            }
        )
        self.prepared_closes[result["head"]] = {
            **deepcopy(request),
            "expected_head": result["head"],
        }
        return result


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
