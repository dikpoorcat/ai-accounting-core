"""Public assembly of a synthetic opening and its real downstream consumers."""

from copy import deepcopy
from hashlib import sha256
from uuid import UUID, uuid4

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.domains.assets.public import create_assets
from ai_accounting_modular.domains.assets.public import repository_factory as assets_repository
from ai_accounting_modular.domains.financing.public import create_financing, create_investment
from ai_accounting_modular.domains.financing.public import (
    repository_factory as financing_repository,
)
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.opening.public import create_opening
from ai_accounting_modular.ledger.opening.public import repository_factory as opening_repository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.money.public import create_money
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog

SOURCE, BANK, CASH, PLATFORM, PERSON, PARTY, FUND, FIXED, INTANGIBLE = (
    str(UUID(int=n)) for n in range(6001, 6010)
)


def member(data, *, evidence=None, member_id=None):
    return {
        "member_id": member_id or str(uuid4()),
        "evidence_ids": evidence or [SOURCE],
        "data": data,
    }


def fact(kind, data, *, period="2026-07", actual=None, business_id=None, evidence=None):
    result = {
        "business_id": business_id or str(uuid4()),
        "kind": kind,
        "period": period,
        "evidence_ids": evidence or [SOURCE],
        "data": deepcopy(data),
    }
    if actual is not None:
        result["effective_on"] = actual
    return result


def opening(*members, basis="existing_ledger", period="2026-07"):
    return fact("opening.package", {"basis": basis, "members": list(members)}, period=period)


def bank_and_equity(value="200000"):
    return opening(
        member({"type": "bank", "funds_account_id": BANK, "balance_fen": value}),
        member(
            {
                "type": "equity",
                "equity_kind": "retained_earnings",
                "holder_or_basis_id": PARTY,
                "balance_fen": value,
            }
        ),
    )


def loan_opening(*, contract_period="2026-01", maturity="2027-01-01"):
    contract = fact(
        "financing.loan_contract",
        {
            "lender_id": PARTY,
            "lender_type": "licensed_financial_institution",
            "currency": "CNY",
            "rate_type": "fixed",
            "annual_rate": "0.036",
            "day_count": "actual_360",
            "matures_on": maturity,
            "repayment": "bullet",
            "term": "short_term",
        },
        period=contract_period,
    )
    loan = member(
        {
            "type": "loan",
            "contract_id": contract["business_id"],
            "lender_id": PARTY,
            "principal_fen": "10000",
            "accrued_interest_fen": "0",
            "interest_start_on": "2026-07-01",
        }
    )
    item = opening(loan, member({"type": "bank", "funds_account_id": BANK, "balance_fen": "10000"}))
    return contract, item, loan


def objects():
    return [
        {"object_id": oid, "kind": kind, "name": "合成期初对象", "expected_seq": 0, "data": data}
        for oid, kind, data in (
            (
                BANK,
                "bank_account",
                {
                    "bank_name": "合成银行",
                    "account_number": "opening-001",
                    "currency": "CNY",
                    "owner": "company",
                },
            ),
            (CASH, "cash_account", {"currency": "CNY"}),
            (
                PLATFORM,
                "platform_account",
                {
                    "platform": "合成平台",
                    "account_key": "opening",
                    "owner": "company",
                    "currency": "CNY",
                },
            ),
            (PERSON, "person", {}),
            (PARTY, "organization", {}),
            (FUND, "investment_product", {"category": "money_fund"}),
            (FIXED, "asset", {"asset_type": "fixed", "category": "合成设备"}),
            (INTANGIBLE, "asset", {"asset_type": "intangible", "category": "合成软件"}),
        )
    ]


def source(source_id=SOURCE):
    text = "合成期初余额及真实组成资料，空包表示已明确核实余额全零。" + source_id
    return {
        "source_id": source_id,
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": "2026-07",
    }


def request(company, *facts):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "objects": objects(),
        "sources": [source()],
        "mutations": [
            {"op": "create", "expected_seq": 0, "fact": deepcopy(item)} for item in facts
        ],
    }


def replace(company, *facts, seq=1):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [
            {"op": "replace", "expected_seq": seq, "fact": deepcopy(item)} for item in facts
        ],
    }


def get(app, company, *ids):
    return app.accounting_get(
        {
            "company_id": company,
            "business_ids": list(ids),
            "include": ["facts", "results", "sources"],
        }
    )


def result(app, company, item):
    return next(
        result
        for result in get(app, company, item["business_id"])["businesses"][0]["results"]
        if result["reference"]["result_slot"] == "main"
    )


def assemble(tmp_path, *, start_period="2026-07", asset_accounting=None):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-hash"))
    storage = Storage(
        catalog,
        repositories=[
            application_repository(),
            identity_repository(),
            ledger_repository(),
            opening_repository(),
            records_repository(),
            financing_repository(),
            assets_repository(),
            money_repository(),
        ],
    )
    company = str(uuid4())
    storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "合成期初公司",
            start_period,
            tmp_path / "company",
        )
    )
    identity, ledger = create_identity(), create_ledger()
    money = create_money(identity=identity, ledger=ledger)
    app = create_application(
        storage=storage,
        identity=identity,
        ledger=ledger,
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[
            create_opening(asset_accounting=asset_accounting),
            create_financing(),
            create_investment(),
            create_assets(),
            money,
        ],
        money=money,
    )
    return app, company
