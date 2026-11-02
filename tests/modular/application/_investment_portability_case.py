"""A real host and sourced six-profile investment fixture, isolated from production files."""

from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

import pytest

from ai_accounting_modular.domains.financing.public import create_investment
from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog
from tests.modular.ledger.opening._support import (
    BANK,
    PARTY,
    SOURCE,
    fact,
    member,
    opening,
    request,
)
from tests.modular.ledger.opening.test_investment_cutover import (
    agency,
    coop_member,
    cooperation,
    equity,
    holding,
    investment_terms,
    open_trade,
)

from ._working_case import Case


@pytest.fixture
def invested(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成投资负责人", "synthetic-hash"))
    kernel, company = create_kernel(catalog), str(uuid4())
    kernel.storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000001",
            "合成六类投资公司",
            "2026-07",
            tmp_path / "company",
        )
    )
    try:
        yield Case(kernel, company, SOURCE, PARTY)
    finally:
        kernel.close()


def apply(case, *facts, **fields):
    response = case.app.accounting_apply(case.request(*facts, **fields))
    assert response.get("completion") == "complete", response.get("message", response)
    return response


def scope(company, month="2026-07", view="current"):
    return {"company_id": company, "period_from": month, "period_to": month, "view": view}


def movement(source, slot, amount, *, direction="inflow", day="2026-07-20"):
    return fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": direction,
            "counterparty_id": PARTY,
            "payment_method": "individual",
            "amount_fen": amount,
            "allocations": [
                {
                    "source": {"business_id": source["business_id"], "slot": slot},
                    "amount_fen": amount,
                }
            ],
        },
        period=day[:7],
        actual=day,
    )


def seed(case, *, settle=True):
    configurations = [
        ("money_fund", "short_term_cost", True),
        ("bank_wealth", "other_financial_cost", True),
        ("other_fund", "other_financial_cost", False),
        ("bond", "long_bond_cost", False),
        ("equity", "long_equity_cost", False),
    ]
    products = [(str(uuid4()), category) for category, _, _ in configurations]
    agreements = [
        investment_terms(product, category, method, current=current)
        for (product, _), (category, method, current) in zip(products, configurations, strict=True)
    ]
    agreements[3]["data"]["coupon_schedule"].insert(
        0, {"payment_on": "2026-07-31", "coupon_rate": "0.1"}
    )
    holdings = [holding(agreement) for agreement in agreements]
    project, contract = cooperation()
    cooperation_opening = coop_member(
        contract, [open_trade("old_sale", "revenue", "50"), open_trade("old_cost", "expense", "20")]
    )
    receivables = [
        member(
            {
                "type": "investment_receivable",
                "terms_id": agreements[index]["business_id"],
                "income_kind": nature,
                "counterparty_id": PARTY,
                "amount_fen": amount,
                "original_business_reference": "合成期初" + nature,
            }
        )
        for index, nature, amount in ((0, "dividend", "7"), (3, "interest", "3"))
    ]
    package = opening(
        *holdings,
        *receivables,
        cooperation_opening,
        member({"type": "bank", "funds_account_id": BANK, "balance_fen": "5000"}),
        equity(10088),
    )
    initial = request(case.company, *agreements, contract, package)
    initial["objects"].extend(
        {
            "object_id": product,
            "kind": "investment_product",
            "name": "合成" + category,
            "expected_seq": 0,
            "data": {"category": category},
        }
        for product, category in [*products, (project, "cooperation")]
    )
    response = case.app.accounting_apply(initial)
    assert response.get("completion") == "complete", response.get("message", response)
    purchases = [
        fact(
            "investment.acquire",
            {
                "terms_id": agreement["business_id"],
                "counterparty_id": PARTY,
                "quantity": "200" if index == 3 else "2",
                "purchase_price_fen": "200",
                "acquisition_fees_fen": "0",
                "separate_declared_income_fen": "0",
            },
            actual="2026-07-03",
        )
        for index, agreement in enumerate(agreements)
    ]
    incomes = [
        fact(
            "investment.income",
            {
                "terms_id": agreements[index]["business_id"],
                "counterparty_id": PARTY,
                "income_character": "dividend"
                if index == 4
                else "interest"
                if index == 1
                else "fund_distribution",
                "amount_fen": "10",
            },
            actual="2026-07-10",
        )
        for index in (0, 1, 2, 4)
    ]
    coupon = fact(
        "investment.debt_interest",
        {
            "terms_id": agreements[3]["business_id"],
            "counterparty_id": PARTY,
            "source": {
                "business_id": package["business_id"],
                "slot": "member:" + holdings[3]["member_id"] + ":cost",
            },
            "payment_on": "2026-07-31",
        },
        actual="2026-07-31",
    )
    sold = fact(
        "investment.dispose",
        {
            "terms_id": agreements[2]["business_id"],
            "counterparty_id": PARTY,
            "quantity": "1",
            "gross_proceeds_fen": "110",
            "disposal_fees_fen": "0",
            "exit_mode": "redemption",
        },
        actual="2026-07-12",
    )
    reinvested = fact(
        "investment.reinvest",
        {
            "terms_id": agreements[0]["business_id"],
            "counterparty_id": PARTY,
            "income_source": {"business_id": incomes[0]["business_id"], "slot": "income"},
            "quantity": "0.1",
            "amount_fen": "10",
            "acquisition_fees_fen": "0",
        },
        actual="2026-07-11",
    )
    agencies = [
        agency(contract, package, cooperation_opening, "old_sale", "revenue", "50", "2026-07-10"),
        agency(contract, package, cooperation_opening, "old_cost", "expense", "20", "2026-07-11"),
    ]
    if not settle:
        agencies = []
    apply(case, *purchases, *incomes, coupon, sold, reinvested, *agencies)
    payments = [movement(item, "primary", "200", direction="outflow") for item in purchases]
    receipts = [
        movement(package, "member:" + item["member_id"], item["data"]["amount_fen"])
        for item in receivables
    ]
    settlements = (
        [
            *payments,
            *receipts,
            movement(sold, "primary", "110"),
            movement(incomes[-1], "income", "10"),
        ]
        if settle
        else []
    )
    if settlements:
        apply(case, *settlements)
    corrected = deepcopy(incomes[1])
    corrected["data"]["amount_fen"] = "12"
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": corrected}])
    return {
        "package": package,
        "terms": agreements,
        "contract": contract,
        "purchases": purchases,
        "receivables": receivables,
        "incomes": incomes,
        "ids": [
            item["business_id"]
            for item in [
                *agreements,
                contract,
                package,
                *purchases,
                *incomes,
                coupon,
                sold,
                reinvested,
                *agencies,
                *settlements,
            ]
        ],
        "coupon": coupon,
    }


def reconcile(case):
    with case.kernel.storage.read_unit(case.company) as read:
        movements = case.app.ledger.fund_movements(BANK, "2026-07", "2026-07", read)
    assert len(movements) == 9
    assert (
        sum(
            int(item.value["amount_fen"])
            for item in movements
            if item.value["direction"] == "outflow"
        )
        == 1000
    )
    assert (
        sum(
            int(item.value["amount_fen"])
            for item in movements
            if item.value["direction"] == "inflow"
        )
        == 130
    )
    rows, matches = [], []
    for movement_ in movements:
        row_id, value = str(uuid4()), movement_.value
        rows.append(
            {
                "row_id": row_id,
                "source": {"source_id": SOURCE},
                "actual_on": value["actual_on"],
                "direction": value["direction"],
                "amount_fen": value["amount_fen"],
            }
        )
        matches.append({"statement_row_ids": [row_id], "fund_movement_ids": [value["movement_id"]]})
    statement = fact(
        "money.bank_statement",
        {"funds_account_id": BANK, "opening_fen": "5000", "closing_fen": "4130", "rows": rows},
    )
    matched = fact(
        "money.reconciliation", {"statement_id": statement["business_id"], "matches": matches}
    )
    apply(case, statement, matched)


def snapshot(app, company, read, *, view="current", month="2026-07"):
    return {
        key: value
        for key, value in app.ledger.financial_snapshot(scope(company, month, view), read).items()
        if key not in {"selection", "company_id", "database_id", "head", "scope"}
    }


def holdings_view(app, company, read):
    owner = create_investment(ledger=app.ledger, identity=app.identity)
    activity = owner.investment_activity(scope(company), read)
    return {
        "cash": {
            "payments_fen": activity.actual_payments_fen,
            "receipts_fen": activity.actual_receipts_fen,
            "income_receipts_fen": sum(
                event.settlement_fen for event in activity.events if event.kind == "income_receipt"
            ),
        },
        "products": sorted(
            (asdict(product) for product in activity.products),
            key=lambda product: product["fund_id"],
        ),
        "cooperation": [
            {
                key: value
                for key, value in asdict(position).items()
                if key not in {"reference", "source"}
            }
            for position in activity.cooperation_positions
        ],
    }
