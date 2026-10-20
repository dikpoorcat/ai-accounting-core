"""The production assembly processes real typed business in an isolated company."""

from hashlib import sha256
from uuid import uuid4

from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog


def test_complete_public_assembly_adopts_and_archives_a_real_expense(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-only-hash"))
    kernel = create_kernel(catalog)
    company, business, opening, supplier, evidence = [str(uuid4()) for _ in range(5)]
    try:
        kernel.storage.create_company(
            CompanySpec(
                company,
                str(uuid4()),
                "913100000000000001",
                "合成装配公司",
                "2026-01",
                tmp_path / "company",
            )
        )
        text = "已核实新设公司期初全零，一月未付维修费用1000元。"
        outcome = kernel.application.accounting_apply(
            {
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
                            "business_id": business,
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
        )
        assert outcome["completion"] == "complete", outcome
        assert outcome["accounting"] == "saved"
        assert outcome["documents"] == "ready"
        with kernel.storage.read_unit(company) as read:
            snapshot = kernel.ledger.financial_snapshot(
                {
                    "company_id": company,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": "current",
                },
                read,
            )
            assert snapshot["pre_tax"]["profit_before_tax"]["amount_fen"] == "-100000"
            assert snapshot["balance_sheet"]["33"]["ending"]["amount_fen"] == "100000"
            assert kernel.records.document_status(read)["state"] == "ready"
        kinds = kernel.application.accounting_schema({})["supported_kinds"]
        assert "people.remuneration" in kinds
        assert "investment.money_fund_purchase" in kinds
        assert "opening.package" in kinds
        query = {"company_id": company, "year": "2026", "quarter": "1", "preparation": "deferred"}
        with kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
            assert response.status == 200
            assert response.body["state"] == "preparing"
        assert kernel.reading.run_pending(company)
        with kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
            assert response.body["state"] == "ready"
            before_version = response.body["model_version"]
            assert response.body["response"]["summary"]["current_net_profit_fen"] == "-100000"
        changed = kernel.application.accounting_get(
            {"company_id": company, "business_ids": [business], "include": ["facts"]}
        )
        fact = changed["businesses"][0]["fact"]
        fact["data"]["amount_fen"] = "120000"
        updated = kernel.application.accounting_apply(
            {
                "contract": "ai-accounting-modular/1",
                "company_id": company,
                "command_id": str(uuid4()),
                "mutations": [{"op": "replace", "expected_seq": 1, "fact": fact}],
            }
        )
        assert updated["completion"] == "complete", updated
        assert updated["reading"] == "preparing"
        with kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
            assert response.body["state"] == "preparing"
        assert kernel.reading.run_pending(company)
        with kernel.dashboard.get("/api/dashboard/quarterly-report", query) as response:
            assert response.body["state"] == "ready"
            assert response.body["model_version"] != before_version
            assert response.body["response"]["summary"]["current_net_profit_fen"] == "-120000"
    finally:
        kernel.close()
