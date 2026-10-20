"""An independent, synthetic public-app assembly for financial snapshot review."""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from uuid import NAMESPACE_DNS, uuid4, uuid5

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.contracts.public import validate_contract
from ai_accounting_modular.domains.assets.public import create_assets
from ai_accounting_modular.domains.assets.public import repository_factory as assets_repository
from ai_accounting_modular.domains.financing.public import create_financing, create_investment
from ai_accounting_modular.domains.financing.public import (
    repository_factory as financing_repository,
)
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.domains.people.public import create_people
from ai_accounting_modular.domains.people.public import repository_factory as people_repository
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


def identity(name):
    return str(uuid5(NAMESPACE_DNS, "independent-financial-review/" + name))


SOURCE, BANK, PARTY_A, PARTY_B, PERSON, ASSET = map(
    identity, ("source", "bank", "party-a", "party-b", "person", "asset")
)


def fact(name, kind, data, month="2026-01", *, on=None):
    result = {
        "business_id": identity(name),
        "kind": kind,
        "period": month,
        "evidence_ids": [SOURCE],
        "data": deepcopy(data),
    }
    if on is not None:
        result["effective_on"] = on
    return result


def member(name, data):
    return {"member_id": identity(name), "evidence_ids": [SOURCE], "data": deepcopy(data)}


def opening(
    name="opening", month="2026-01", *, cash="0", equity=None, basis="existing_ledger", members=()
):
    items = list(deepcopy(members))
    if cash != "0":
        items.append(
            member(name + "-bank", {"type": "bank", "funds_account_id": BANK, "balance_fen": cash})
        )
    equity = cash if equity is None else equity
    if equity != "0":
        items.append(
            member(
                name + "-equity",
                {
                    "type": "equity",
                    "equity_kind": "paid_in_capital",
                    "holder_or_basis_id": PARTY_A,
                    "balance_fen": equity,
                },
            )
        )
    return fact(name, "opening.package", {"basis": basis, "members": items}, month)


def expense(name, amount, month="2026-01", *, details=True):
    data = {
        "creditor_id": PARTY_A,
        "creditor_kind": "supplier",
        "amount_fen": amount,
        "expense_class": "administration",
    }
    if details:
        data["report_details"] = [{"code": "management_other", "amount_fen": amount}]
    return fact(name, "operations.expense", data, month)


def payment(name, source, value, *, on="2026-01-21", slot="primary", incoming=False):
    return fact(
        name,
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": "inflow" if incoming else "outflow",
            "amount_fen": value,
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": source["business_id"], "slot": slot},
                    "amount_fen": value,
                }
            ],
        },
        on[:7],
        on=on,
    )


def zero_rate_policy():
    return fact(
        "zero-rate-policy",
        "taxes.vat_policy",
        {
            "policy": {
                "version": "synthetic-report-only-1",
                "official_url": "https://shanghai.chinatax.gov.cn/zzzb/zcwj/202602/t479283.html",
                "effective_from": "2023-01-01",
                "effective_to": "2027-12-31",
            },
            "nominal_rate": "0",
            "tax_base_rate": "0",
            "payable_rate": "0",
            "form_version": "small_taxpayer_2026",
            "threshold_fen": "10000000",
            "threshold_period": "month",
            "threshold_operator": "strictly_below",
            "exemption_scope": "eligible_non_excluded_sales",
            "special_invoice_may_waive_exemption": True,
            "special_invoice_may_waive_rate_reduction": True,
        },
    )


def sale(name, value, month="2026-01", *, customer=PARTY_A):
    return fact(
        name,
        "operations.service_sale",
        {
            "customer_id": customer,
            "gross_fen": value,
            "vat": {
                "policy_id": identity("zero-rate-policy"),
                "exemption_eligible": False,
                "invoice_kind": "ordinary",
                "waive_exemption": False,
                "waive_rate_reduction": False,
            },
            "tax_point": {"type": "explicit", "tax_period": month, "source": {"source_id": SOURCE}},
        },
        month,
    )


def continuation(initial, *, may=False):
    # The R07/R08 input numbers are explicitly fixed, never produced by report equations.
    balance = {1: "300000", 15: "300000", 30: "300000", 48: "300000", 52: "300000", 53: "300000"}
    year_profit = {
        1: "300000" if may else "100000",
        21: "300000" if may else "100000",
        30: "300000" if may else "100000",
        32: "300000" if may else "100000",
    }
    quarter_profit = {1: "80000", 21: "80000", 30: "80000", 32: "80000"} if may else {}
    year_cash = {1: "200000", 7: "200000", 20: "200000", 21: "300000", 22: "500000"}
    quarter_cash = (
        {1: "50000", 7: "50000", 20: "50000", 21: "450000", 22: "500000"}
        if may
        else {21: "500000", 22: "500000"}
    )
    return fact(
        "continuation",
        "reports.continuation",
        {
            "accounting_standard": "small_enterprise",
            "bookkeeping_start": initial["period"],
            "opening_package_id": initial["business_id"],
            "year_beginning_balance": [
                {"line": i, "beginning_fen": balance.get(i, "0")} for i in range(1, 54)
            ],
            "prior_profit": [
                {
                    "line": i,
                    "quarter_to_date_fen": quarter_profit.get(i, "0"),
                    "year_to_date_fen": year_profit.get(i, "0"),
                }
                for i in range(1, 33)
            ],
            "prior_cash": [
                {
                    "line": i,
                    "quarter_to_date_fen": quarter_cash.get(i, "0"),
                    "year_to_date_fen": year_cash.get(i, "0"),
                }
                for i in range(1, 23)
            ],
        },
        initial["period"],
    )


def loan_contract(name, maturity, *, month="2026-01"):
    return fact(
        name,
        "financing.loan_contract",
        {
            "lender_id": PARTY_B,
            "lender_type": "licensed_financial_institution",
            "currency": "CNY",
            "rate_type": "fixed",
            "annual_rate": "0.036",
            "day_count": "actual_360",
            "matures_on": maturity,
            "repayment": "bullet",
            "term": "long_term",
        },
        month,
    )


def drawdown(name, contract, value, on="2026-01-01"):
    return fact(
        name,
        "financing.loan_drawdown",
        {"contract_id": contract["business_id"], "principal_fen": value, "funds_account_id": BANK},
        on[:7],
        on=on,
    )


@dataclass
class ReviewBook:
    app: object
    company: str

    def submit(self, *facts, mutations=(), correction_period=None, **extra):
        request = {
            "contract": "ai-accounting-modular/1",
            "company_id": self.company,
            "command_id": str(uuid4()),
            "mutations": [
                *({"op": "create", "expected_seq": 0, "fact": deepcopy(f)} for f in facts),
                *deepcopy(mutations),
            ],
            **extra,
        }
        if correction_period is not None:
            request["correction_period"] = correction_period
        result = self.app.accounting_apply(request)
        assert result.get("completion") == "complete", result
        return result

    def replace(self, *facts, seq=1, correction_period=None):
        return self.submit(
            mutations=[{"op": "replace", "expected_seq": seq, "fact": deepcopy(f)} for f in facts],
            correction_period=correction_period,
        )

    def freeze_formal_heads_for_test(self, period):
        # White-box fixture only: the public application close workflow is not yet delivered.
        # All frozen result bodies and vouchers were produced by the real public application.
        with self.app.storage.write_unit(self.company) as unit:
            unit.repository("ledger").freeze_heads(period)

    def snapshot(self, first="2026-01", last=None, *, view="current", expected_head=None):
        last = last or first
        scope = {"company_id": self.company, "period_from": first, "period_to": last, "view": view}
        if expected_head is not None:
            scope["expected_head"] = expected_head
        with self.app.storage.read_unit(
            self.company, view=view, as_of=last if view == "as_posted" else None
        ) as read:
            result = self.app.ledger.financial_snapshot(scope, read)
            validate_contract("reporting.schema.json", "FinancialSnapshot", result)
            assert result["head"] == read.head
            assert (
                result["company_id"] == read.company_id
                and result["database_id"] == read.database_id
            )
            return result


def assemble(root, start="2026-01", *, include_investment=False):
    catalog = root / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "独立验收负责人", "synthetic-password"))
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
            financing_repository(),
            assets_repository(),
            people_repository(),
            money_repository(),
            records_repository(),
        ],
    )
    company = str(uuid4())
    storage.create_company(
        CompanySpec(
            company, str(uuid4()), "913100000000000001", "独立财报验收公司", start, root / "company"
        )
    )
    identity_module, ledger = create_identity(), create_ledger()
    money = create_money(identity=identity_module, ledger=ledger)
    app = create_application(
        storage=storage,
        identity=identity_module,
        ledger=ledger,
        money=money,
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[
            create_opening(),
            create_reporting(),
            create_operations(),
            create_taxes(),
            create_financing(),
            *([create_investment()] if include_investment else []),
            create_assets(),
            create_people(),
            money,
        ],
    )
    book = ReviewBook(app, company)
    text = "本独立验收仅使用合成的期初、业务、现金流和报表接续来源。"
    book.submit(
        sources=[
            {
                "source_id": SOURCE,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": start,
            }
        ],
        objects=[
            {
                "object_id": oid,
                "kind": kind,
                "name": "独立合成对象",
                "expected_seq": 0,
                "data": data,
            }
            for oid, kind, data in (
                (
                    BANK,
                    "bank_account",
                    {
                        "bank_name": "合成银行",
                        "account_number": "independent-report",
                        "currency": "CNY",
                        "owner": "company",
                    },
                ),
                (PARTY_A, "organization", {}),
                (PARTY_B, "organization", {}),
                (PERSON, "person", {}),
                (ASSET, "asset", {"asset_type": "fixed", "category": "合成固定资产"}),
            )
        ],
    )
    return book
