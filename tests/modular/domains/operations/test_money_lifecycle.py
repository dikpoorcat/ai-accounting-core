"""Operating rights and real money share one candidate and one atomic commit."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_application
from ai_accounting_modular.application.public import repository_factory as application_repository
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
        domains=[create_operations(), money],
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
