"""Synthetic identities and public assembly, never real company resources."""

from copy import deepcopy
from hashlib import sha256
from uuid import UUID, uuid4

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.domains.financing.public import create_financing, create_investment
from ai_accounting_modular.domains.financing.public import (
    repository_factory as financing_repository,
)
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.domains.taxes.public import create_taxes
from ai_accounting_modular.domains.taxes.public import repository_factory as taxes_repository
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.money.public import create_money
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog

SOURCE, OWNER, LENDER, BANK, CASH, PLATFORM, PRODUCT, COUNTERPARTY = (
    str(UUID(int=n)) for n in range(401, 409)
)


def fact(kind, data, *, period="2026-01", actual=None, business_id=None):
    result = {
        "business_id": business_id or str(uuid4()),
        "kind": kind,
        "period": period,
        "evidence_ids": [SOURCE],
        "data": deepcopy(data),
    }
    if actual is not None:
        result["effective_on"] = actual
    return result


def six():
    owner = fact(
        "financing.owner_funding",
        {
            "owner_id": OWNER,
            "funding_kind": "loan",
            "amount_fen": "100000",
            "funds_account_id": BANK,
        },
        actual="2026-01-02",
    )
    contract = fact(
        "financing.loan_contract",
        {
            "lender_id": LENDER,
            "lender_type": "licensed_financial_institution",
            "currency": "CNY",
            "rate_type": "fixed",
            "annual_rate": "0.036",
            "day_count": "actual_360",
            "matures_on": "2026-12-31",
            "repayment": "bullet",
            "term": "short_term",
        },
    )
    drawdown = fact(
        "financing.loan_drawdown",
        {
            "contract_id": contract["business_id"],
            "principal_fen": "1000000",
            "funds_account_id": BANK,
        },
        actual="2026-01-01",
    )
    interest = fact(
        "financing.interest_period",
        {
            "principal_source": {"business_id": drawdown["business_id"], "slot": "principal"},
            "start_on": "2026-01-01",
            "end_before": "2026-02-01",
        },
    )
    purchase = fact(
        "investment.money_fund_purchase",
        {
            "fund_id": PRODUCT,
            "counterparty_id": COUNTERPARTY,
            "classification": "readily_redeemable_held_not_over_one_year",
            "cost_basis": "documented_cost",
            "purchase_price_fen": "100000",
            "acquisition_fees_fen": "0",
            "excludes_declared_unpaid_distributions": True,
        },
    )
    redemption = fact(
        "investment.money_fund_redemption",
        {
            "fund_id": PRODUCT,
            "counterparty_id": COUNTERPARTY,
            "classification": "readily_redeemable_held_not_over_one_year",
            "cost_basis": "documented_cost",
            "costs": [
                {
                    "source": {"business_id": purchase["business_id"], "slot": "cost"},
                    "amount_fen": "100000",
                }
            ],
            "net_proceeds_fen": "103000",
        },
    )
    return owner, contract, drawdown, interest, purchase, redemption


def payment(
    source, value, *, slot="principal", day="2026-01-16", party=LENDER, direction="outflow"
):
    return fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": direction,
            "amount_fen": str(value),
            "payment_method": "individual",
            "counterparty_id": party,
            "allocations": [
                {
                    "source": {"business_id": source["business_id"], "slot": slot},
                    "amount_fen": str(value),
                }
            ],
        },
        period=day[:7],
        actual=day,
    )


def objects():
    bank_data = {
        "bank_name": "合成银行",
        "account_number": "synthetic-001",
        "currency": "CNY",
        "owner": "company",
    }
    return [
        {"object_id": oid, "kind": kind, "name": "合成对象", "expected_seq": 0, "data": data}
        for oid, kind, data in (
            (OWNER, "person", {}),
            (LENDER, "organization", {}),
            (BANK, "bank_account", bank_data),
            (CASH, "cash_account", {"currency": "CNY"}),
            (
                PLATFORM,
                "platform_account",
                {
                    "platform": "合成平台",
                    "account_key": "synthetic",
                    "owner": "company",
                    "currency": "CNY",
                },
            ),
            (PRODUCT, "investment_product", {"category": "money_fund"}),
            (COUNTERPARTY, "organization", {}),
        )
    ]


def request(company_id, *facts):
    text = "本测试的合成股东资金、贷款合同、基金结算及实际还款资料。"
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company_id,
        "command_id": str(uuid4()),
        "objects": objects(),
        "sources": [
            {
                "source_id": SOURCE,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": "2026-01",
            }
        ],
        "mutations": [
            {"op": "create", "expected_seq": 0, "fact": deepcopy(item)} for item in facts
        ],
    }


def change(company_id, *facts, seq=1, correction_period=None):
    result = {
        "contract": "ai-accounting-modular/1",
        "company_id": company_id,
        "command_id": str(uuid4()),
        "mutations": [
            {"op": "replace", "expected_seq": seq, "fact": deepcopy(item)} for item in facts
        ],
    }
    if correction_period is not None:
        result["correction_period"] = correction_period
    return result


def get(app, company_id, *ids):
    return app.accounting_get(
        {
            "company_id": company_id,
            "business_ids": list(ids),
            "include": ["facts", "results", "sources"],
        }
    )


def result(app, company_id, item):
    return get(app, company_id, item["business_id"])["businesses"][0]["results"][0]


def assemble(tmp_path, *, investment=None, taxes=False):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-hash"))
    storage = Storage(
        catalog,
        repositories=[
            application_repository(),
            identity_repository(),
            ledger_repository(),
            records_repository(),
            financing_repository(),
            money_repository(),
            operations_repository(),
            *([taxes_repository()] if taxes else []),
        ],
    )
    company_id = str(uuid4())
    storage.create_company(
        CompanySpec(
            company_id,
            str(uuid4()),
            "913100000000000001",
            "合成融资公司",
            "2026-01",
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
            create_financing(),
            investment or create_investment(),
            money,
            create_operations(),
            *([create_taxes()] if taxes else []),
        ],
        money=money,
    )
    return app, company_id
