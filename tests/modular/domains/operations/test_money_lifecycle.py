"""Operating rights and real money share one candidate and one atomic commit."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
from ai_accounting_modular.domains.financing.public import create_investment
from ai_accounting_modular.domains.financing.public import (
    repository_factory as financing_repository,
)
from ai_accounting_modular.domains.operations.public import create_operations
from ai_accounting_modular.domains.operations.public import (
    repository_factory as operations_repository,
)
from ai_accounting_modular.identity.public import create_identity
from ai_accounting_modular.identity.public import repository_factory as identity_repository
from ai_accounting_modular.ledger.public import create_ledger
from ai_accounting_modular.ledger.public import repository_factory as ledger_repository
from ai_accounting_modular.money.public import create_money
from ai_accounting_modular.money.public import repository_factory as money_repository
from ai_accounting_modular.records.public import create_records
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, Storage, create_catalog


@pytest.fixture
def system(tmp_path):
    catalog = tmp_path / "catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-password-hash"))
    storage = Storage(
        catalog,
        repositories=[
            application_repository(),
            identity_repository(),
            ledger_repository(),
            records_repository(),
            operations_repository(),
            financing_repository(),
            money_repository(),
        ],
    )
    company = str(uuid4())
    storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            "913100000000000002",
            "合成经营资金公司",
            "2026-01",
            tmp_path / "company",
        )
    )
    identity, ledger = create_identity(), create_ledger()
    money = create_money(ledger=ledger, identity=identity)
    app = create_application(
        storage=storage,
        identity=identity,
        ledger=ledger,
        records=create_records(root_for=lambda read: storage.company_metadata(read).company_root),
        domains=[create_operations(), create_investment(), money],
        money=money,
    )
    ids = {key: str(uuid4()) for key in ("source", "party", "beneficiary", "bank")}
    text = "合成资料：真实经营往来、收付款及更正。"
    source = {
        "source_id": ids["source"],
        "kind": "owner_statement",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "archive_month": "2026-01",
    }
    objects = [
        {
            "object_id": ids[key],
            "kind": "organization",
            "name": "合成" + key,
            "expected_seq": 0,
            "data": {},
        }
        for key in ("party", "beneficiary")
    ]
    objects.append(
        {
            "object_id": ids["bank"],
            "kind": "bank_account",
            "name": "合成公司银行",
            "expected_seq": 0,
            "data": {
                "bank_name": "合成银行",
                "account_number": "synthetic",
                "currency": "CNY",
                "owner": "company",
                "opened_period": "2026-01",
                "zero_at_opening": True,
            },
        }
    )
    result = app.accounting_apply(command(company, objects=objects, sources=[source]))
    assert result["completion"] == "complete", result
    return app, company, ids


def command(company, *facts, mutations=None, **participants):
    return {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": mutations
        if mutations is not None
        else [{"op": "create", "expected_seq": 0, "fact": fact} for fact in facts],
        **participants,
    }


def fact(ids, kind, data, *, period="2026-01", actual=None):
    value = {
        "business_id": str(uuid4()),
        "kind": kind,
        "period": period,
        "evidence_ids": [ids["source"]],
        "data": data,
    }
    if actual is not None:
        value["effective_on"] = actual
    return value


def movement(ids, source, slot, amount, *, direction="outflow", day="2026-01-15"):
    return fact(
        ids,
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": ids["bank"],
            "direction": direction,
            "amount_fen": str(amount),
            "payment_method": "individual",
            "counterparty_id": ids["party"],
            "allocations": [
                {
                    "source": {"business_id": source["business_id"], "slot": slot},
                    "amount_fen": str(amount),
                }
            ],
        },
        period=day[:7],
        actual=day,
    )


def expense(ids, amount=100000):
    return fact(
        ids,
        "operations.expense",
        {
            "creditor_id": ids["party"],
            "creditor_kind": "supplier",
            "amount_fen": str(amount),
            "expense_class": "administration",
        },
    )


def pass_through(ids):
    return fact(
        ids,
        "operations.pass_through",
        {"payer_id": ids["party"], "beneficiary_id": ids["beneficiary"], "amount_fen": "100000"},
    )


def refund(ids, original, amount=20000, *, period="2026-01", actual=None):
    return fact(
        ids,
        "operations.pass_through_return",
        {"pass_through_id": original["business_id"], "amount_fen": str(amount)},
        period=period,
        actual=actual,
    )


def recovery(ids, original, amount=20000, *, period="2026-01"):
    return fact(
        ids,
        "operations.overpayment",
        {
            "source": {"business_id": original["business_id"], "slot": "primary"},
            "counterparty_id": ids["party"],
            "amount_fen": str(amount),
        },
        period=period,
    )


def get(system, *facts):
    app, company, _ = system
    return app.accounting_get(
        {
            "company_id": company,
            "business_ids": [value["business_id"] for value in facts],
            "include": ["facts", "results", "sources"],
        }
    )


def test_paid_expense_can_be_corrected_to_zero_with_exact_recovery_and_real_refund(system):
    app, company, ids = system
    original = expense(ids, 100)
    paid = movement(ids, original, "primary", 100)
    saved = app.accounting_apply(command(company, paid, original))
    assert saved["completion"] == "complete", saved.get("message", saved)
    with app.storage.read_unit(company) as read:
        original_occurrence = app.ledger.fund_movements(ids["bank"], "2026-01", "2026-01", read)[
            0
        ].value
    revised = deepcopy(original)
    revised["data"]["amount_fen"] = "0"
    claim = recovery(ids, original, 100)
    corrected = app.accounting_apply(
        command(
            company,
            mutations=[
                {"op": "create", "expected_seq": 0, "fact": claim},
                {"op": "replace", "expected_seq": 1, "fact": revised},
            ],
        )
    )
    assert corrected["completion"] == "complete", corrected.get("message", corrected)
    current = get(system, original)["businesses"][0]
    assert current["fact"]["data"]["amount_fen"] == "0"
    assert current["results"][0]["target"]["values"]["report_detail_state"] == "known"
    returned = movement(ids, claim, "recovery", 100, direction="inflow", day="2026-01-16")
    refunded = app.accounting_apply(command(company, returned))
    assert refunded["completion"] == "complete", refunded.get("message", refunded)
    with app.storage.read_unit(company) as read:
        movements = app.ledger.fund_movements(ids["bank"], "2026-01", "2026-01", read)
        assert (
            next(item.value for item in movements if item.value["direction"] == "outflow")
            == original_occurrence
        )
        balances = app.ledger.resolve(
            [
                {"business_id": original["business_id"], "slot": "primary"},
                {"business_id": claim["business_id"], "slot": "recovery"},
            ],
            "obligation",
            read,
        )
        assert [item["balance"]["remaining_fen"] for item in balances] == ["0", "0"]
        assert (
            sum(
                int(item.value["amount_fen"]) * (1 if item.value["direction"] == "inflow" else -1)
                for item in movements
            )
            == 0
        )


def test_paid_expense_zero_without_recovery_rolls_back_fact_and_keeps_real_money(system):
    app, company, ids = system
    original = expense(ids, 100)
    paid = movement(ids, original, "primary", 100)
    assert app.accounting_apply(command(company, original, paid))["completion"] == "complete"
    before = get(system, original, paid)
    revised = deepcopy(original)
    revised["data"]["amount_fen"] = "0"
    rejected = app.accounting_apply(
        command(company, mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}])
    )
    assert rejected["completion"] == "not_saved" and rejected["error"] == "dependency_conflict", (
        rejected
    )
    assert get(system, original, paid) == before


def test_overpayment_reuses_original_party_when_counterparty_is_omitted(system):
    app, company, ids = system
    original = expense(ids, 100)
    paid = movement(ids, original, "primary", 101)
    claim = recovery(ids, original, 1)
    del claim["data"]["counterparty_id"]
    saved = app.accounting_apply(command(company, claim, paid, original))
    assert saved["completion"] == "complete", saved
    with app.storage.read_unit(company) as read:
        (definition,) = app.ledger.resource_definitions(
            [{"business_id": claim["business_id"], "slot": "recovery"}], "obligation", read
        )
        assert definition.value["party_id"] == ids["party"]
        assert definition.value["direction"] == "receivable"
        found = create_operations().query_current(
            {
                "company_id": company,
                "family": "operations",
                "period_from": "2026-01",
                "period_to": "2026-01",
                "kind": "operations.overpayment",
                "object_id": ids["party"],
                "limit": 100,
            },
            read,
        )
        assert [item["business_id"] for item in found] == [claim["business_id"]]


@pytest.mark.parametrize("wrong_party", [False, True])
def test_fund_redemption_correction_preserves_real_receipt_and_refunds_actual_excess(
    system, wrong_party
):
    app, company, ids = system
    product = str(uuid4())
    registered = app.accounting_apply(
        command(
            company,
            objects=[
                {
                    "object_id": product,
                    "kind": "investment_product",
                    "name": "合成货币基金",
                    "expected_seq": 0,
                    "data": {"category": "money_fund"},
                }
            ],
        )
    )
    assert registered["completion"] == "complete", registered
    common = {
        "fund_id": product,
        "counterparty_id": ids["party"],
        "classification": "readily_redeemable_held_not_over_one_year",
        "cost_basis": "documented_cost",
    }
    purchased = fact(
        ids,
        "investment.money_fund_purchase",
        {
            **common,
            "purchase_price_fen": "100",
            "acquisition_fees_fen": "0",
            "excludes_declared_unpaid_distributions": True,
        },
    )
    redeemed = fact(
        ids,
        "investment.money_fund_redemption",
        {
            **common,
            "costs": [
                {
                    "source": {"business_id": purchased["business_id"], "slot": "cost"},
                    "amount_fen": "100",
                }
            ],
            "net_proceeds_fen": "101",
        },
    )
    received = movement(ids, redeemed, "primary", 101, direction="inflow")
    saved = app.accounting_apply(command(company, received, redeemed, purchased))
    assert saved["completion"] == "complete", saved
    before = get(system, redeemed, received)
    with app.storage.read_unit(company) as read:
        (original_occurrence,) = app.ledger.fund_movements(ids["bank"], "2026-01", "2026-01", read)
    revised = deepcopy(redeemed)
    revised["data"]["net_proceeds_fen"] = "100"
    claim = recovery(ids, redeemed, 1)
    if wrong_party:
        claim["data"]["counterparty_id"] = ids["beneficiary"]
    else:
        del claim["data"]["counterparty_id"]
    returned = movement(ids, claim, "recovery", 1, day="2026-01-16")
    changed = app.accounting_apply(
        command(
            company,
            mutations=[
                {"op": "create", "expected_seq": 0, "fact": returned},
                {"op": "create", "expected_seq": 0, "fact": claim},
                {"op": "replace", "expected_seq": 1, "fact": revised},
            ],
        )
    )
    if wrong_party:
        assert changed["completion"] == "not_saved", changed
        assert changed["error"] == "dependency_conflict", changed
        assert get(system, redeemed, received) == before
        assert get(system, claim, returned)["businesses"] == []
        return
    assert changed["completion"] == "complete", changed
    current = get(system, claim)["businesses"][0]
    assert current["results"][0]["target"]["values"]["treatment"] == "overreceipt_refund"
    with app.storage.read_unit(company) as read:
        definitions = app.ledger.resource_definitions(
            [{"business_id": claim["business_id"], "slot": "recovery"}], "obligation", read
        )
        assert definitions[0].value["direction"] == "payable"
        assert definitions[0].value["account_code"] == "2241"
        assert definitions[0].value["party_id"] == ids["party"]
        assert definitions[0].value["cashflow_weights"] == [
            {"category": "investment_recovery", "weight": "1"}
        ]
        actual = app.ledger.fund_movements(ids["bank"], "2026-01", "2026-01", read)
        assert next(item.value for item in actual if item.value["direction"] == "inflow") == (
            original_occurrence.value
        )
        assert (
            sum(
                int(item.value["amount_fen"]) * (1 if item.value["direction"] == "inflow" else -1)
                for item in actual
            )
            == 100
        )
        assert all(
            item.value["cashflow_parts"]
            == [{"category": "investment_recovery", "amount_fen": item.value["amount_fen"]}]
            for item in actual
        )
        balances = app.ledger.resolve(
            [
                {"business_id": redeemed["business_id"], "slot": "primary"},
                {"business_id": claim["business_id"], "slot": "recovery"},
            ],
            "obligation",
            read,
        )
        assert [item["balance"]["remaining_fen"] for item in balances] == ["0", "0"]
    settled = get(system, revised, received, claim, returned)
    replayed = app.accounting_apply(command(company, revised, received, claim, returned))
    assert replayed["changes"] == "none"
    assert get(system, revised, received, claim, returned) == settled


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0)])
def test_new_collection_money_and_refund_are_order_independent(system, order):
    app, company, ids = system
    original = pass_through(ids)
    received = movement(ids, original, "collection", 30000, direction="inflow")
    returned = refund(ids, original)
    items = [original, received, returned]
    result = app.accounting_apply(command(company, *(items[index] for index in order)))
    assert result["completion"] == "complete", result
    assert len(get(system, *items)["businesses"]) == 3
    before = get(system, *items)
    retried = app.accounting_apply(command(company, *items))
    assert retried["changes"] == "none" and retried["accounting"] == "unchanged", retried
    assert get(system, *items) == before


def test_two_refund_responsibilities_cannot_spend_the_same_actual_receipt(system):
    app, company, ids = system
    original = pass_through(ids)
    received = movement(ids, original, "collection", 30000, direction="inflow")
    assert app.accounting_apply(command(company, original, received))["completion"] == "complete"
    refunds = [refund(ids, original), refund(ids, original)]
    before = get(system, original, received)
    result = app.accounting_apply(command(company, *refunds))
    assert result["error"] == "dependency_conflict", result
    assert get(system, *refunds)["businesses"] == []
    assert get(system, original, received) == before


@pytest.mark.parametrize("day,refund_day", [("2026-02-01", None), ("2026-01-20", "2026-01-10")])
def test_future_actual_receipt_cannot_support_earlier_refund(system, day, refund_day):
    app, company, ids = system
    original = pass_through(ids)
    received = movement(ids, original, "collection", 30000, direction="inflow", day=day)
    returned = refund(ids, original, actual=refund_day)
    result = app.accounting_apply(command(company, returned, received, original))
    assert result["error"] == "dependency_conflict", result
    assert get(system, original, received, returned)["businesses"] == []


def test_replacing_and_withdrawing_old_refunds_uses_only_current_capacity(system):
    app, company, ids = system
    original = pass_through(ids)
    received = movement(ids, original, "collection", 30000, direction="inflow")
    first, second = refund(ids, original), refund(ids, original, 5000)
    assert (
        app.accounting_apply(command(company, original, received, first, second))["completion"]
        == "complete"
    )
    changed = deepcopy(first)
    changed["data"]["amount_fen"] = "30000"
    result = app.accounting_apply(
        command(company, mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}])
    )
    assert result["error"] == "dependency_conflict", result
    changed["data"]["amount_fen"] = "15000"
    third = refund(ids, original, 15000)
    result = app.accounting_apply(
        command(
            company,
            mutations=[
                {"op": "create", "expected_seq": 0, "fact": third},
                {"op": "withdraw", "expected_seq": 1, "business_id": second["business_id"]},
                {"op": "replace", "expected_seq": 1, "fact": changed},
            ],
        )
    )
    assert result["completion"] == "complete", result


@pytest.mark.parametrize("reverse", [False, True])
def test_same_unit_payment_overpayment_and_recovery_receipt_never_cycle(system, reverse):
    app, company, ids = system
    original = expense(ids, 80000)
    paid = movement(ids, original, "primary", 100000)
    excess = recovery(ids, original)
    collected = movement(ids, excess, "recovery", 20000, direction="inflow", day="2026-01-16")
    values = [original, paid, excess, collected]
    result = app.accounting_apply(
        command(company, *(list(reversed(values)) if reverse else values))
    )
    assert result["completion"] == "complete", result
    with app.storage.read_unit(company) as read:
        state = app.ledger.resolve(
            [{"business_id": original["business_id"], "slot": "primary"}], "obligation", read
        )[0]["balance"]
        assert state["money_settled_fen"] == "100000" and state["remaining_fen"] == "0"
        moves = app.ledger.fund_movements(ids["bank"], "2026-01", "2026-01", read)
        assert sorted(value.value["amount_fen"] for value in moves) == ["100000", "20000"]


def test_paid_expense_correction_preserves_real_money_and_requires_whole_recovery(system):
    app, company, ids = system
    original = expense(ids)
    paid = movement(ids, original, "primary", 100000)
    assert app.accounting_apply(command(company, original, paid))["completion"] == "complete"
    before = get(system, paid)
    changed = deepcopy(original)
    changed["data"]["amount_fen"] = "80000"
    replacement = {"op": "replace", "expected_seq": 1, "fact": changed}
    missing = app.accounting_apply(command(company, mutations=[replacement]))
    assert missing["error"] == "dependency_conflict", missing
    excess = recovery(ids, changed)
    result = app.accounting_apply(
        command(
            company, mutations=[{"op": "create", "expected_seq": 0, "fact": excess}, replacement]
        )
    )
    assert result["completion"] == "complete", result
    assert get(system, paid)["businesses"][0]["fact"] == before["businesses"][0]["fact"]
    assert (
        get(system, paid)["businesses"][0]["results"][0]["target"]["values"]["amount_fen"]
        == "100000"
    )


def test_future_payment_cannot_prove_january_overpayment(system):
    app, company, ids = system
    original = expense(ids, 80000)
    paid = movement(ids, original, "primary", 100000, day="2026-02-01")
    excess = recovery(ids, original)
    result = app.accounting_apply(command(company, excess, paid, original))
    assert result["error"] == "dependency_conflict", result
    assert get(system, original, paid, excess)["businesses"] == []


def test_other_income_has_one_stable_movement_and_retry_does_not_advance_head(system):
    app, company, ids = system
    current = fact(
        ids,
        "operations.other_income",
        {"income_kind": "bank_interest", "amount_fen": "1000", "funds_account_id": ids["bank"]},
        actual="2026-01-15",
    )
    result = app.accounting_apply(command(company, current))
    assert result["completion"] == "complete", result
    before = get(system, current)
    retry = app.accounting_apply(command(company, current))
    assert retry["changes"] == "none" and retry["accounting"] == "unchanged", retry
    assert get(system, current) == before
    with app.storage.read_unit(company) as read:
        moves = app.ledger.fund_movements(ids["bank"], "2026-01", "2026-01", read)
        assert len(moves) == 1 and moves[0].value["amount_fen"] == "1000"
    lines = before["businesses"][0]["results"][0]["vouchers"][0]["lines"]
    assert [(line["account_code"], line["debit_fen"], line["credit_fen"]) for line in lines] == [
        ("1002", "1000", "0"),
        ("5603", "0", "1000"),
    ]


def cost_return(ids, original, amount, *, period="2026-01"):
    return fact(
        ids,
        "operations.expense_recovery",
        {
            "expense_id": original["business_id"],
            "counterparty_id": ids["party"],
            "amount_fen": str(amount),
        },
        period=period,
    )


def test_expense_returns_use_current_collection_and_inherit_cashflow_and_details(system):
    app, company, ids = system
    original = expense(ids, 7)
    original["data"]["report_details"] = [
        {"code": "management_startup", "amount_fen": "3"},
        {"code": "management_research", "amount_fen": "4"},
    ]
    first, second = cost_return(ids, original, 3), cost_return(ids, original, 4, period="2026-02")
    received = movement(ids, second, "primary", 4, direction="inflow", day="2026-02-05")
    result = app.accounting_apply(command(company, received, second, first, original))
    assert result["completion"] == "complete", result
    saved = get(system, first, second)
    positions = []
    for item in saved["businesses"]:
        result = item["results"][0]
        assert result["target"]["values"]["report_detail_state"] == "known"
        quantities = result["basis"]["adopted_quantities"]
        adopted = next(item for item in quantities if item["type"] == "expense")
        assert adopted["report_details"] == [
            {"code": "management_research", "amount_fen": "4"},
            {"code": "management_startup", "amount_fen": "3"},
        ]
        positions.extend(
            (item["from_fen"], item["to_fen"])
            for item in quantities
            if item["type"] == "cumulative_range"
        )
    assert sorted(positions) == [("0", "3"), ("3", "7")]
    with app.storage.read_unit(company) as read:
        actual = app.ledger.fund_movements(ids["bank"], "2026-02", "2026-02", read)
        assert actual[0].value["cashflow_parts"] == [
            {"category": "other_operating_payments", "amount_fen": "4"}
        ]
    before = get(system, original, first, second, received)
    assert (
        app.accounting_apply(command(company, original, first, second, received))["changes"]
        == "none"
    )
    assert get(system, original, first, second, received) == before
    extra = cost_return(ids, original, 1)
    rejected = app.accounting_apply(command(company, extra))
    assert rejected["error"] == "dependency_conflict", rejected
    assert get(system, original, first, second, received) == before


def test_expense_return_replacement_and_withdrawal_have_no_stale_cumulative_amount(system):
    app, company, ids = system
    original = expense(ids, 100)
    first, second = cost_return(ids, original, 50), cost_return(ids, original, 50)
    assert (
        app.accounting_apply(command(company, first, original, second))["completion"] == "complete"
    )
    revised = deepcopy(first)
    revised["data"]["amount_fen"] = "100"
    result = app.accounting_apply(
        command(
            company,
            mutations=[
                {"op": "replace", "expected_seq": 1, "fact": revised},
                {"op": "withdraw", "expected_seq": 1, "business_id": second["business_id"]},
            ],
        )
    )
    assert result["completion"] == "complete", result
    assert (
        get(system, first)["businesses"][0]["results"][0]["target"]["values"]["amount_fen"] == "100"
    )


def test_expense_return_same_party_and_original_month_are_required(system):
    app, company, ids = system
    original = expense(ids, 100)
    original["period"] = "2026-02"
    returned = cost_return(ids, original, 10)
    result = app.accounting_apply(command(company, original, returned))
    assert result["error"] == "dependency_conflict", result
    original["period"] = "2026-01"
    returned["data"]["counterparty_id"] = ids["beneficiary"]
    result = app.accounting_apply(command(company, returned, original))
    assert result["error"] == "dependency_conflict", result


def test_untaxed_advance_refund_and_actual_payment_share_gross_capacity(system):
    app, company, ids = system
    original = fact(
        ids,
        "operations.advance",
        {
            "side": "customer",
            "counterparty_id": ids["party"],
            "amount_fen": "100",
            "tax_status": "not_due",
        },
    )
    returned = fact(
        ids,
        "operations.advance_refund",
        {"advance_id": original["business_id"], "gross_fen": "100"},
    )
    paid = movement(ids, returned, "refund", 100)
    result = app.accounting_apply(command(company, paid, returned, original))
    assert result["completion"] == "complete", result
    before = get(system, original, returned, paid)
    assert app.accounting_apply(command(company, returned, original, paid))["changes"] == "none"
    assert get(system, original, returned, paid) == before
    with app.storage.read_unit(company) as read:
        views = app.ledger.resolve(
            [
                {"business_id": original["business_id"], "slot": "advance"},
                {"business_id": returned["business_id"], "slot": "refund"},
            ],
            "obligation",
            read,
        )
        assert [item["balance"]["remaining_fen"] for item in views] == ["0", "0"]


def test_platform_income_consumes_one_original_row_and_revalidates_changes(system):
    app, company, ids = system
    platform, row_id = str(uuid4()), str(uuid4())
    obj = {
        "object_id": platform,
        "kind": "platform_account",
        "name": "合成平台账户",
        "expected_seq": 0,
        "data": {
            "platform": "合成平台",
            "account_key": "synthetic",
            "currency": "CNY",
            "owner": "company",
            "opened_period": "2026-01",
            "zero_at_opening": True,
        },
    }
    original = fact(
        ids,
        "money.platform_activity",
        {
            "platform_account_id": platform,
            "rows": [
                {
                    "row_id": row_id,
                    "source": {"source_id": ids["source"], "location": "第1行"},
                    "actual_on": "2026-01-15",
                    "direction": "inflow",
                    "amount_fen": "100",
                }
            ],
        },
    )
    income = fact(
        ids,
        "operations.other_income",
        {
            "income_kind": "company_reward",
            "amount_fen": "100",
            "funds_account_id": platform,
            "platform_row_ids": [row_id],
        },
        actual="2026-01-15",
    )
    result = app.accounting_apply(command(company, income, original, objects=[obj]))
    assert result.get("completion") == "complete", result
    saved = get(system, income)["businesses"][0]["results"][0]
    assert {"source_id": ids["source"], "location": "第1行"} in saved["basis"]["source_locations"]
    duplicate = deepcopy(income)
    duplicate["business_id"] = str(uuid4())
    result = app.accounting_apply(command(company, duplicate))
    assert result["error"] == "dependency_conflict", result
    before = get(system, original, income)
    revised = deepcopy(original)
    revised["data"]["rows"][0]["amount_fen"] = "99"
    result = app.accounting_apply(
        command(company, mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}])
    )
    assert result["error"] == "dependency_conflict", result
    assert get(system, original, income) == before


def test_separate_known_refund_days_use_receipts_available_at_each_day(system):
    app, company, ids = system
    original = pass_through(ids)
    first_receipt = movement(
        ids, original, "collection", 10000, direction="inflow", day="2026-01-01"
    )
    later_receipt = movement(
        ids, original, "collection", 10000, direction="inflow", day="2026-01-15"
    )
    first = refund(ids, original, 10000, actual="2026-01-10")
    later = refund(ids, original, 10000, actual="2026-01-20")
    result = app.accounting_apply(
        command(company, later, first_receipt, first, later_receipt, original)
    )
    assert result.get("completion") == "complete", result


def test_reimbursement_weights_use_transferred_amounts_and_single_category_is_one(system):
    app, company, ids = system
    first, second = expense(ids, 100), expense(ids, 1)
    second["data"]["expense_class"] = "service"
    payer_id = str(uuid4())
    payer = {
        "object_id": payer_id,
        "kind": "person",
        "name": "合成垫付人",
        "expected_seq": 0,
        "data": {},
    }
    accepted = fact(
        ids,
        "operations.reimbursement",
        {
            "type": "acceptance",
            "payer_id": payer_id,
            "payer_kind": "owner",
            "sources": [
                {
                    "source": {"business_id": item["business_id"], "slot": "primary"},
                    "amount_fen": item["data"]["amount_fen"],
                    "recipient_id": ids["party"],
                }
                for item in (first, second)
            ],
        },
    )
    result = app.accounting_apply(command(company, accepted, second, first, objects=[payer]))
    assert result["completion"] == "complete", result
    with app.storage.read_unit(company) as read:
        definitions = app.ledger.resource_definitions(
            [
                {"business_id": first["business_id"], "slot": "primary"},
                {"business_id": second["business_id"], "slot": "primary"},
                {"business_id": accepted["business_id"], "slot": "reimbursement"},
            ],
            "obligation",
            read,
        )
        assert definitions[0].value["cashflow_weights"] == [
            {"category": "other_operating_payments", "weight": "1"}
        ]
        assert definitions[1].value["cashflow_weights"] == [
            {"category": "materials_services_payments", "weight": "1"}
        ]
        assert definitions[2].value["cashflow_weights"] == [
            {"category": "materials_services_payments", "weight": "1"},
            {"category": "other_operating_payments", "weight": "100"},
        ]


@pytest.mark.parametrize("consumer", ["overpayment", "reimbursement"])
def test_resource_tax_period_does_not_delay_its_real_establishment(system, consumer):
    app, company, ids = system
    original = expense(ids, 80)
    assert app.accounting_apply(command(company, original))["completion"] == "complete"

    change, plan = expense_program_plan(original)
    # Program-controlled formal resource: the label is a future tax period,
    # while its actual production period remains January. No AI API accepts it.
    plan["obligations"][0]["business_period"] = "2026-02"
    with app.storage.write_unit(company) as unit:
        app.ledger.adopt([{**change, "plans": [plan]}], unit)
    if consumer == "overpayment":
        paid = movement(ids, original, "primary", 100)
        excess = recovery(ids, original, 20)
        result = app.accounting_apply(command(company, excess, paid))
    else:
        payer_id = str(uuid4())
        payer = {
            "object_id": payer_id,
            "kind": "person",
            "name": "合成垫付人",
            "expected_seq": 0,
            "data": {},
        }
        accepted = fact(
            ids,
            "operations.reimbursement",
            {
                "type": "acceptance",
                "payer_id": payer_id,
                "payer_kind": "owner",
                "sources": [
                    {
                        "source": {"business_id": original["business_id"], "slot": "primary"},
                        "amount_fen": "80",
                        "recipient_id": ids["party"],
                    }
                ],
            },
        )
        result = app.accounting_apply(command(company, accepted, objects=[payer]))
    assert result["completion"] == "complete", result


def test_real_future_resource_cannot_explain_earlier_payment_and_overpayment(system):
    app, company, ids = system
    original = expense(ids, 80)
    original["period"] = "2026-02"
    paid = movement(ids, original, "primary", 100)
    excess = recovery(ids, original, 20)
    result = app.accounting_apply(command(company, excess, paid, original))
    assert result["completion"] == "not_saved" and result["error"] == "dependency_conflict", result
    assert get(system, original, paid, excess)["businesses"] == []


def expense_program_plan(original):
    """Build a controlled internal source; external commands cannot submit it."""

    class ProgramContext:
        def get_object(self, object_id):
            return {"object_id": object_id, "kind": "organization"}

        def get_fact(self, business_id):
            return original

        def resolve_sources(self, source_ids):
            return [{"source_id": source_id} for source_id in source_ids]

    owner, context = create_operations(), ProgramContext()
    change = owner.describe_change(
        [{"op": "recalculate", "business_id": original["business_id"], "expected_seq": 1}], context
    )
    return change, owner.evaluate_slot(original["business_id"], "main", context)


@pytest.mark.parametrize("nominal,mixed", [(1, True), (2**63 - 1, False)])
def test_offset_never_occupies_real_cash_classification_interval(system, nominal, mixed):
    app, company, ids = system
    original = expense(ids, nominal)
    assert app.accounting_apply(command(company, original))["completion"] == "complete"
    if mixed:
        change, plan = expense_program_plan(original)
        plan["obligations"][0]["cashflow_weights"] = [
            {"category": "employee_payments", "weight": "1"},
            {"category": "other_operating_payments", "weight": "1"},
        ]
        with app.storage.write_unit(company) as unit:
            app.ledger.adopt([{**change, "plans": [plan]}], unit)
    prepaid = fact(
        ids,
        "operations.advance",
        {
            "side": "supplier",
            "counterparty_id": ids["party"],
            "amount_fen": str(nominal),
            "purpose": "operating",
        },
    )
    offset = fact(
        ids,
        "money.transfer",
        {
            "type": "offset",
            "offset_kind": "advance_application",
            "first": {
                "source": {"business_id": prepaid["business_id"], "slot": "advance"},
                "amount_fen": str(nominal),
            },
            "second": {
                "source": {"business_id": original["business_id"], "slot": "primary"},
                "amount_fen": str(nominal),
            },
        },
    )
    paid = movement(ids, original, "primary", 1)
    excess = recovery(ids, original, 1)
    received = movement(ids, excess, "recovery", 1, direction="inflow", day="2026-01-16")
    result = app.accounting_apply(command(company, received, excess, paid, offset, prepaid))
    assert result["completion"] == "complete", result
    saved = get(system, excess)["businesses"][0]["results"][0]
    span = next(
        item for item in saved["basis"]["adopted_quantities"] if item["type"] == "cumulative_range"
    )
    assert (span["from_fen"], span["to_fen"]) == ("0", "1")
    with app.storage.read_unit(company) as read:
        category = "employee_payments" if mixed else "other_operating_payments"
        recovered = app.ledger.resource_definitions(
            [{"business_id": excess["business_id"], "slot": "recovery"}], "obligation", read
        )[0].value
        assert recovered["cashflow_weights"] == [{"category": category, "weight": "1"}]
        movements = app.ledger.fund_movements(ids["bank"], "2026-01", "2026-01", read)
        assert len(movements) == 2
        assert all(
            item.value["cashflow_parts"] == [{"category": category, "amount_fen": "1"}]
            for item in movements
        )
    before = get(system, original, prepaid, offset, paid, excess, received)
    assert (
        app.accounting_apply(command(company, received, excess, paid, offset, prepaid))["changes"]
        == "none"
    )
    assert get(system, original, prepaid, offset, paid, excess, received) == before


def test_final_candidate_rejects_cash_basis_inconsistent_with_actual_settlements(
    system, monkeypatch
):
    app, company, ids = system
    original = expense(ids, 80)
    paid = movement(ids, original, "primary", 100)
    excess = recovery(ids, original, 20)
    monkeypatch.setattr(app.money, "actual_settled_fen", lambda source, cutoff, context: 101)
    result = app.accounting_apply(command(company, original, paid, excess))
    assert result["error"] == "dependency_conflict" and result["completion"] == "not_saved", result
    assert get(system, original, paid, excess)["businesses"] == []


def test_later_overpayment_can_follow_an_already_recovered_earlier_overpayment(system):
    app, company, ids = system
    original = expense(ids, 80)
    first_paid = movement(ids, original, "primary", 100)
    first_claim = recovery(ids, original, 20)
    first_refund = movement(ids, first_claim, "recovery", 20, direction="inflow", day="2026-01-16")
    result = app.accounting_apply(command(company, original, first_paid, first_claim, first_refund))
    assert result["completion"] == "complete", result
    later_paid = movement(ids, original, "primary", 20, day="2026-02-05")
    later_claim = recovery(ids, original, 20, period="2026-02")
    later_refund = movement(ids, later_claim, "recovery", 20, direction="inflow", day="2026-02-06")
    result = app.accounting_apply(command(company, later_refund, later_claim, later_paid))
    assert result["completion"] == "complete", result
    with app.storage.read_unit(company) as read:
        states = app.ledger.resolve(
            [
                {"business_id": original["business_id"], "slot": "primary"},
                {"business_id": first_claim["business_id"], "slot": "recovery"},
                {"business_id": later_claim["business_id"], "slot": "recovery"},
            ],
            "obligation",
            read,
        )
        assert all(item["balance"]["remaining_fen"] == "0" for item in states)
        actual = app.ledger.fund_movements(ids["bank"], "2026-01", "2026-02", read)
        assert len(actual) == 4
        assert (
            sum(
                int(item.value["amount_fen"]) * (1 if item.value["direction"] == "outflow" else -1)
                for item in actual
            )
            == 80
        )


def supplier_offset(ids, original, amount, *, period="2026-01", actual=None):
    advance = fact(
        ids,
        "operations.advance",
        {
            "side": "supplier",
            "counterparty_id": ids["party"],
            "amount_fen": str(amount),
            "purpose": "operating",
        },
        period=period,
    )
    offset = fact(
        ids,
        "money.transfer",
        {
            "type": "offset",
            "offset_kind": "advance_application",
            "first": {
                "source": {"business_id": advance["business_id"], "slot": "advance"},
                "amount_fen": str(amount),
            },
            "second": {
                "source": {"business_id": original["business_id"], "slot": "primary"},
                "amount_fen": str(amount),
            },
        },
        period=period,
        actual=actual,
    )
    return advance, offset


def saved_cash_ranges(system, claim):
    result = get(system, claim)["businesses"][0]["results"][0]
    return sorted(
        (int(item["from_fen"]), int(item["to_fen"]))
        for item in result["basis"]["adopted_quantities"]
        if item["type"] == "cumulative_range"
    )


def test_later_offset_and_claim_cannot_reclassify_an_earlier_actual_refund(system):
    app, company, ids = system
    original = expense(ids, 1)
    assert app.accounting_apply(command(company, original))["completion"] == "complete"
    change, plan = expense_program_plan(original)
    plan["obligations"][0]["cashflow_weights"] = [
        {"category": "employee_payments", "weight": "1"},
        {"category": "other_operating_payments", "weight": "1"},
    ]
    with app.storage.write_unit(company) as unit:
        app.ledger.adopt([{**change, "plans": [plan]}], unit)
    paid = movement(ids, original, "primary", 2)
    first = recovery(ids, original, 1)
    returned = movement(ids, first, "recovery", 1, direction="inflow", day="2026-01-16")
    assert app.accounting_apply(command(company, returned, first, paid))["completion"] == "complete"
    before = get(system, first, returned)["businesses"]
    advance, offset = supplier_offset(ids, original, 1, period="2026-02")
    later = recovery(ids, original, 1, period="2026-02")
    result = app.accounting_apply(command(company, later, offset, advance))
    assert result["completion"] == "complete", result
    assert get(system, first, returned)["businesses"] == before
    assert saved_cash_ranges(system, first) == [(1, 2)]
    assert saved_cash_ranges(system, later) == [(0, 1)]
    with app.storage.read_unit(company) as read:
        actual = app.ledger.fund_movements(ids["bank"], "2026-01", "2026-01", read)
        net = {}
        for occurred in actual:
            for part in occurred.value["cashflow_parts"]:
                sign = 1 if occurred.value["direction"] == "outflow" else -1
                net[part["category"]] = net.get(part["category"], 0) + sign * int(
                    part["amount_fen"]
                )
        assert {key: value for key, value in net.items() if value} == {"employee_payments": 1}


@pytest.mark.parametrize("reverse", [False, True])
def test_late_correction_preserves_earlier_claims_and_can_use_fragmented_cash(system, reverse):
    app, company, ids = system
    original = expense(ids, 4)
    paid = movement(ids, original, "primary", 5)
    first = recovery(ids, original, 1)
    assert app.accounting_apply(command(company, first, paid, original))["completion"] == "complete"
    assert saved_cash_ranges(system, first) == [(4, 5)]
    revised = deepcopy(original)
    revised["data"]["amount_fen"] = "1"
    later_paid = movement(ids, original, "primary", 3, day="2026-02-05")
    second = recovery(ids, original, 2, period="2026-02")
    late = recovery(ids, original, 4, period="2026-03")
    mutations = [
        {"op": "create", "expected_seq": 0, "fact": item} for item in (later_paid, second, late)
    ] + [{"op": "replace", "expected_seq": 1, "fact": revised}]
    result = app.accounting_apply(
        command(company, mutations=list(reversed(mutations)) if reverse else mutations)
    )
    assert result["completion"] == "complete", result
    assert saved_cash_ranges(system, first) == [(4, 5)]
    assert saved_cash_ranges(system, second) == [(6, 8)]
    assert saved_cash_ranges(system, late) == [(1, 4), (5, 6)]
    values = (revised, paid, first, later_paid, second, late)
    before = get(system, *values)
    assert app.accounting_apply(command(company, *reversed(values)))["changes"] == "none"
    assert get(system, *values) == before


def test_unknown_sibling_day_is_not_required_when_every_order_has_enough_excess(system):
    app, company, ids = system
    original = expense(ids, 60)
    paid = movement(ids, original, "primary", 100, day="2026-01-01")
    known = recovery(ids, original, 20)
    known["effective_on"] = "2026-01-10"
    unknown = recovery(ids, original, 20)
    result = app.accounting_apply(command(company, unknown, known, paid, original))
    assert result["completion"] == "complete", result
    assert saved_cash_ranges(system, known) == [(80, 100)]
    assert saved_cash_ranges(system, unknown) == [(60, 80)]


@pytest.mark.parametrize(
    "actual,error", [(None, "needs_information"), ("2026-01-20", "dependency_conflict")]
)
def test_only_a_materially_uncertain_offset_day_requires_information(system, actual, error):
    app, company, ids = system
    original = expense(ids, 1)
    paid = movement(ids, original, "primary", 1, day="2026-01-01")
    advance, offset = supplier_offset(ids, original, 1, actual=actual)
    claim = recovery(ids, original, 1)
    claim["effective_on"] = "2026-01-10"
    result = app.accounting_apply(command(company, claim, paid, offset, advance, original))
    assert result["completion"] == "not_saved" and result["error"] == error, result
    assert get(system, original, paid, claim, advance, offset)["businesses"] == []
    if actual is None:
        assert [(item["business_id"], item["field"]) for item in result["fact_issues"]] == [
            (offset["business_id"], "/effective_on")
        ]
    offset["effective_on"] = "2026-01-05"
    result = app.accounting_apply(command(company, original, advance, offset, paid, claim))
    assert result["completion"] == "complete", result


def test_definite_pool_conflict_is_not_hidden_by_an_unknown_sibling_day(system):
    app, company, ids = system
    original = expense(ids, 1)
    paid = movement(ids, original, "primary", 2, day="2026-01-01")
    first, second = recovery(ids, original, 1), recovery(ids, original, 1)
    first["effective_on"] = "2026-01-10"
    result = app.accounting_apply(command(company, first, original, second, paid))
    assert result["error"] == "dependency_conflict" and result["completion"] == "not_saved", result


def test_same_day_claims_use_stable_identity_and_include_all_same_day_cash(system):
    app, company, ids = system
    original = expense(ids, 60)
    paid = movement(ids, original, "primary", 100, day="2026-01-10")
    first, second = recovery(ids, original, 20), recovery(ids, original, 20)
    first["business_id"] = "10000000-0000-4000-8000-000000000001"
    second["business_id"] = "10000000-0000-4000-8000-000000000002"
    first["effective_on"] = second["effective_on"] = "2026-01-10"
    result = app.accounting_apply(command(company, second, paid, first, original))
    assert result["completion"] == "complete", result
    assert saved_cash_ranges(system, first) == [(80, 100)]
    assert saved_cash_ranges(system, second) == [(60, 80)]
    before = get(system, original, paid, first, second)
    assert (
        app.accounting_apply(command(company, original, first, paid, second))["changes"] == "none"
    )
    assert get(system, original, paid, first, second) == before


def test_changing_and_withdrawing_claims_rebuilds_pool_without_erasing_actual_refund(system):
    app, company, ids = system
    original = expense(ids, 1)
    paid = movement(ids, original, "primary", 5)
    first, second = recovery(ids, original, 2), recovery(ids, original, 2)
    first["business_id"] = "10000000-0000-4000-8000-000000000001"
    second["business_id"] = "10000000-0000-4000-8000-000000000002"
    returned = movement(ids, first, "recovery", 1, direction="inflow", day="2026-01-16")
    result = app.accounting_apply(command(company, second, original, returned, first, paid))
    assert result["completion"] == "complete", result
    with app.storage.read_unit(company) as read:
        original_money_ids = {
            item.value["movement_id"]
            for item in app.ledger.fund_movements(ids["bank"], "2026-01", "2026-01", read)
        }
    assert saved_cash_ranges(system, first) == [(3, 5)]
    assert saved_cash_ranges(system, second) == [(1, 3)]
    first_changed, second_changed = deepcopy(first), deepcopy(second)
    first_changed["data"]["amount_fen"], second_changed["data"]["amount_fen"] = "1", "3"
    result = app.accounting_apply(
        command(
            company,
            mutations=[
                {"op": "replace", "expected_seq": 1, "fact": second_changed},
                {"op": "replace", "expected_seq": 1, "fact": first_changed},
            ],
        )
    )
    assert result["completion"] == "complete", result
    assert saved_cash_ranges(system, first_changed) == [(4, 5)]
    assert saved_cash_ranges(system, second_changed) == [(1, 4)]
    saved = get(system, first_changed, second_changed, returned)["businesses"]
    seq = {item["state"]["business_id"]: item["state"]["seq"] for item in saved}
    last_claim, returned_changed = deepcopy(second_changed), deepcopy(returned)
    last_claim["data"]["amount_fen"] = "4"
    returned_changed["data"]["allocations"][0]["source"]["business_id"] = last_claim["business_id"]
    result = app.accounting_apply(
        command(
            company,
            mutations=[
                {
                    "op": "withdraw",
                    "expected_seq": seq[first["business_id"]],
                    "business_id": first["business_id"],
                },
                {"op": "replace", "expected_seq": seq[second["business_id"]], "fact": last_claim},
                {
                    "op": "replace",
                    "expected_seq": seq[returned["business_id"]],
                    "fact": returned_changed,
                },
            ],
        )
    )
    assert result["completion"] == "complete", result
    assert saved_cash_ranges(system, last_claim) == [(1, 5)]
    with app.storage.read_unit(company) as read:
        actual = app.ledger.fund_movements(ids["bank"], "2026-01", "2026-01", read)
        assert {item.value["movement_id"] for item in actual} == original_money_ids
        assert sorted(
            (item.value["direction"], item.value["amount_fen"], item.value["actual_on"])
            for item in actual
        ) == [("inflow", "1", "2026-01-16"), ("outflow", "5", "2026-01-15")]
