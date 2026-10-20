"""Real modules and explicit prior report figures for a synthetic company."""

from copy import deepcopy
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.domains.taxes.public import create_taxes
from ai_accounting_modular.domains.taxes.public import repository_factory as taxes_repository
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.opening.public import create_opening
from ai_accounting_modular.ledger.opening.public import repository_factory as opening_repository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.ledger.reporting.public import create_reporting
from ai_accounting_modular.ledger.reporting.public import repository_factory as reporting_repository
from ai_accounting_modular.money.public import create_money
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog


def uid(name):
    return str(uuid5(NAMESPACE_URL, "reporting-owner-tests/" + name))


SOURCE, CARRY_SOURCE, PARTY, BANK = (uid(name) for name in ("source", "carry", "party", "bank"))


def fact(kind, data, *, period="2026-07", name=None):
    return {
        "business_id": uid(name) if name else str(uuid4()),
        "kind": kind,
        "period": period,
        "evidence_ids": [SOURCE],
        "data": deepcopy(data),
    }


def opening(*, period="2026-07", value="500000", basis="existing_ledger"):
    members = (
        []
        if value == "0"
        else [
            {
                "member_id": uid("bank-member"),
                "evidence_ids": [SOURCE],
                "data": {"type": "bank", "funds_account_id": BANK, "balance_fen": value},
            },
            {
                "member_id": uid("equity-member"),
                "evidence_ids": [SOURCE],
                "data": {
                    "type": "equity",
                    "equity_kind": "retained_earnings",
                    "holder_or_basis_id": PARTY,
                    "balance_fen": value,
                },
            },
        ]
    )
    return fact("opening.package", {"basis": basis, "members": members}, period=period)


def prior_rows(count, columns, values):
    return [
        {"line": line, **{column: str(values.get((line, column), 0)) for column in columns}}
        for line in range(1, count + 1)
    ]


def continuation(current, *, carry=None):
    """R07/R08 amounts fixed independently, with YTD tax expense deliberately unlike carry."""
    may = current["period"] == "2026-05"
    year = "year_to_date_fen"
    quarter = "quarter_to_date_fen"
    revenue = 300000 if may else 100000
    values = {(i, "beginning_fen"): 300000 for i in (1, 15, 30, 51, 52, 53)}
    profit = {(i, year): revenue for i in (1, 21, 30)}
    profit.update({(31, year): 10000, (32, year): revenue - 10000})
    if may:
        profit.update({(i, quarter): 80000 for i in (1, 21, 30, 32)})
    cash = {(i, year): 200000 for i in (1, 7, 20)}
    cash.update(
        {
            (21, year): 300000,
            (22, year): 500000,
            (21, quarter): 450000 if may else 500000,
            (22, quarter): 500000,
        }
    )
    if may:
        cash.update({(i, quarter): 50000 for i in (1, 7, 20)})
    data = {
        "accounting_standard": "small_enterprise",
        "bookkeeping_start": current["period"],
        "opening_package_id": current["business_id"],
        "year_beginning_balance": prior_rows(53, ("beginning_fen",), values),
        "prior_profit": prior_rows(32, (quarter, year), profit),
        "prior_cash": prior_rows(22, (quarter, year), cash),
    }
    if carry is not None:
        data["income_tax_carry"] = {
            "tax_year": 2026,
            "period_to": "2026-04" if may else "2026-06",
            "cumulative_tax_fen": carry,
            "source": {"source_id": CARRY_SOURCE, "location": "累计已计税资料"},
        }
    return fact("reports.continuation", data, period=current["period"])


def source(identity):
    text = "合成接续三表及独立累计已计税资料。" + identity
    return {
        "source_id": identity,
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": "2026-07",
    }


def request(company, *facts, op="create", seq=0, seed=True):
    submitted = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": op, "expected_seq": seq, "fact": deepcopy(row)} for row in facts],
    }
    if seed:
        submitted.update(
            sources=[source(SOURCE), source(CARRY_SOURCE)],
            objects=[
                {
                    "object_id": PARTY,
                    "kind": "organization",
                    "name": "合成交易方",
                    "expected_seq": 0,
                    "data": {},
                },
                {
                    "object_id": BANK,
                    "kind": "bank_account",
                    "name": "合成银行",
                    "expected_seq": 0,
                    "data": {
                        "bank_name": "合成银行",
                        "account_number": "reporting-test",
                        "currency": "CNY",
                        "owner": "company",
                    },
                },
            ],
        )
    return submitted


def withdraw(company, row, *, seq=1):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "withdraw", "business_id": row["business_id"], "expected_seq": seq}],
    }


def get(app, company, row):
    return app.accounting_get(
        {
            "company_id": company,
            "business_ids": [row["business_id"]],
            "include": ["facts", "results", "sources"],
        }
    )["businesses"][0]


def main(app, company, row):
    return next(
        result
        for result in get(app, company, row)["results"]
        if result["reference"]["result_slot"] == "main"
    )


def expense(value="20000", *, period="2026-07"):
    return fact(
        "operations.expense",
        {
            "creditor_id": PARTY,
            "creditor_kind": "supplier",
            "amount_fen": value,
            "expense_class": "administration",
        },
        period=period,
    )


def assemble(tmp_path, start="2026-07"):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成负责人", "synthetic-hash"))
    storage = Storage(
        catalog,
        repositories=[
            application_repository(),
            identity_repository(),
            ledger_repository(),
            opening_repository(),
            reporting_repository(),
            operations_repository(),
            taxes_repository(),
            money_repository(),
            records_repository(),
        ],
    )
    company = str(uuid4())
    storage.create_company(
        CompanySpec(
            company, str(uuid4()), "913100000000000001", "合成接续公司", start, tmp_path / "company"
        )
    )
    identity, ledger = create_identity(), create_ledger()
    money = create_money(identity=identity, ledger=ledger)
    app = create_application(
        storage=storage,
        identity=identity,
        ledger=ledger,
        money=money,
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[create_reporting(), create_opening(), create_operations(), create_taxes(), money],
    )
    return app, company
