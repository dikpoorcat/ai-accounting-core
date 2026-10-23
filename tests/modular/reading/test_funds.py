"""The existing funds page consumes complete formal months and indexed bounded pages."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from ai_accounting_modular.contracts.public import ContractError, content_digest, loads_json
from ai_accounting_modular.money.public import stable_occurrence_id
from ai_accounting_modular.reading import _funds, _monthly
from ai_accounting_modular.reading._context import period
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import (
    BANK,
    CASH,
    COUNTERPARTY,
    OWNER,
    fact,
    objects,
    payment,
    six,
)
from tests.modular.reading._support import source


def selection(case, month="2026-01"):
    return _monthly.normalize(
        {"company_id": case.company, "period_from": month, "period_to": month, "view": "as_posted"}
    )


def apply(case, *facts, **extra):
    values = deepcopy(facts)
    for item in values:
        item["evidence_ids"] = [case.source_id]
    actual = case.app.accounting_apply(case.request(*values, **extra))
    assert actual["completion"] == "complete", json.dumps(actual, ensure_ascii=True)
    return values


def initialize(case, *, opening=True):
    if opening:
        case.seed(expense=False)
    supplied = objects()
    next(obj for obj in supplied if obj["object_id"] == BANK)["data"].update(
        opened_period="2026-01", zero_at_opening=True
    )
    values = (
        {} if opening else {"sources": [source("funds-source") | {"source_id": case.source_id}]}
    )
    apply(case, objects=supplied, **values)


def funding(amount="2000", day="2026-01-02"):
    return fact(
        "financing.owner_funding",
        {"owner_id": OWNER, "funding_kind": "loan", "amount_fen": amount, "funds_account_id": BANK},
        actual=day,
        period=day[:7],
    )


def capture(case, month="2026-01"):
    scope = selection(case, month)
    with case.kernel.storage.read_unit(case.company) as read:
        model = _funds.prepare(case.kernel.reading, scope, read)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repo = unit.repository("reading")
        scope_key = _monthly.key(scope)
        repo.register(scope_key, "funds", unit.head, _monthly.version("funds"))
        repo.mark(scope_key, "funds", unit.head)
        assert repo.publish(scope_key, "funds", unit.head, _monthly.version("funds"), model)
    return model


def page(case, month="2026-01", **query):
    scope = selection(case, month)
    with case.kernel.storage.read_unit(case.company) as read:
        saved = loads_json(
            read.repository("reading").payload(_monthly.key(scope), "funds")["payload_json"]
        )
        snapshot = _monthly.MonthSnapshot(
            _monthly.key(scope),
            scope,
            content_digest(saved),
            {"funds": saved},
            period(month, case.kernel.ledger.closed_through(read)),
            read.company_id,
            read.database_id,
        )
        return _funds.build(snapshot, read, query)


def check_data(value):
    contract = json.loads(
        (
            Path(__file__).parents[3] / "docs/redesign/contracts/dashboard-response-v2.json"
        ).read_text(encoding="utf-8")
    )["dashboard_funds"]
    Draft202012Validator({"$defs": contract["$defs"], "$ref": "#/$defs/FundsData"}).validate(value)


def test_actual_company_account_and_mixed_investment_cash_are_distinct(case, monkeypatch):
    initialize(case)
    *_, purchase, _ = six()
    purchase["data"]["purchase_price_fen"] = "300"
    expense = case.expense("700")
    expense["data"]["creditor_id"] = COUNTERPARTY
    paid = payment(purchase, 1000, party=COUNTERPARTY, slot="primary", day="2026-01-15")
    paid["data"]["allocations"] = [
        {
            "source": {"business_id": purchase["business_id"], "slot": "primary"},
            "amount_fen": "300",
        },
        {"source": {"business_id": expense["business_id"], "slot": "primary"}, "amount_fen": "700"},
    ]
    transfer = fact(
        "money.transfer",
        {"type": "physical", "from_account_id": BANK, "to_account_id": CASH, "amount_fen": "300"},
        actual="2026-01-03",
    )
    apply(case, funding(), purchase, expense, paid, transfer)
    capture(case)
    first = page(case, movement_account_selection="first")
    data = first["data"]
    check_data(data)
    assert (data["opening_fen"], data["total_fen"], data["net_change_fen"]) == ("0", "1000", "1000")
    assert (
        data["inflow_fen"],
        data["outflow_fen"],
        data["internal_transfer_fen"],
        data["movement_count"],
    ) == ("2000", "1000", "300", 4)
    assert (
        data["bank_fen"],
        data["cash_fen"],
        data["bank_inflow_fen"],
        data["bank_outflow_fen"],
    ) == ("700", "300", "2000", "1300")
    assert data["investments"]["actual_payments_fen"] == "300"
    assert set(data["collections"]) == set(_funds.COLLECTIONS)
    assert data["selected_movement_account"] == {"type": "bank", "account_id": BANK}
    cards = {item["account_id"]: item for item in data["collections"]["accounts"]["items"]}
    assert all(
        item["active"] is None and item["attribution_adjustment_fen"] is None
        for item in cards.values()
    )
    assert cards[BANK]["statement"]["coverage_state"] == "missing"
    assert data["bank_statement"]["inflow_fen"] is None
    assert data["bank_statement"]["missing_account_count"] == 1
    assert all(
        item["account_id"] == BANK and item["correction"] is False
        for item in data["collections"]["movements"]["items"]
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("hot funds page called a business owner")

    for owner, method in (
        (case.kernel.money, "account_activity"),
        (case.kernel.reading.investment, "money_fund_activity"),
        (case.kernel.ledger, "account_balances"),
        (case.kernel.identity, "get_batch"),
    ):
        monkeypatch.setattr(owner, method, unexpected)
    assert page(case, movement_account_selection="first") == first
    cash = page(case, section="movements", movement_account_type="cash", movement_account_id=CASH)[
        "data"
    ]
    assert cash["total_fen"] == "1000"
    assert cash["collections"]["movements"]["page"]["total_count"] == 4
    assert cash["collections"]["movements"]["page"]["filtered_count"] == 1
    assert cash["collections"]["movements"]["items"][0]["amount_fen"] == "300"


def test_unknown_balance_is_neither_zero_nor_bank_original_balance(case):
    initialize(case, opening=False)
    apply(case, funding("100"))
    capture(case)
    data = page(case)["data"]
    check_data(data)
    assert data["opening_fen"] is data["total_fen"] is data["bank_fen"] is None
    bank = next(
        item for item in data["collections"]["accounts"]["items"] if item["account_id"] == BANK
    )
    assert bank["opening_fen"] is bank["closing_fen"] is None
    assert bank["inflow_fen"] == "100" and bank["negative_balance"] is False
    assert data["attention_account_count"] == 3


def test_unused_bank_and_explicit_empty_statement_are_different(case):
    initialize(case)
    capture(case)
    before = page(case)["data"]
    assert before["bank_statement"]["coverage_state"] == "not_applicable"
    assert before["bank_statement"]["expected_account_count"] == 0
    assert before["bank_statement"]["inflow_fen"] is None
    statement = fact(
        "money.bank_statement",
        {"funds_account_id": BANK, "opening_fen": "0", "closing_fen": "0", "rows": []},
    )
    reconcile = fact(
        "money.reconciliation", {"statement_id": statement["business_id"], "matches": []}
    )
    apply(case, statement, reconcile)
    capture(case)
    data = page(case)["data"]
    check_data(data)
    assert data["bank_statement"] == {
        "transaction_count": 0,
        "inflow_fen": "0",
        "outflow_fen": "0",
        "review_state": "complete",
        "coverage_state": "complete",
        "statement_count": 1,
        "expected_account_count": 1,
        "provided_account_count": 1,
        "missing_account_count": 0,
    }


def test_bank_original_text_and_many_rows_batch_stay_whole(case):
    initialize(case)
    first, second = funding("300"), funding("700")
    row_ids = [str(uuid4()), str(uuid4())]
    statement = fact(
        "money.bank_statement",
        {
            "funds_account_id": BANK,
            "opening_fen": "0",
            "closing_fen": "1000",
            "rows": [
                {
                    "row_id": ident,
                    "actual_on": "2026-01-02",
                    "direction": "inflow",
                    "amount_fen": amount,
                    "source": {
                        "source_id": case.source_id,
                        "location": "合成原件第" + str(index) + "行",
                    },
                    "memo": "原始用途" + str(index),
                    "counterparty_text": "银行原文交易方",
                }
                for index, (ident, amount) in enumerate(zip(row_ids, ("400", "600"), strict=True))
            ],
        },
    )
    reconciliation = fact(
        "money.reconciliation",
        {
            "statement_id": statement["business_id"],
            "matches": [
                {
                    "statement_row_ids": row_ids,
                    "fund_movement_ids": [
                        stable_occurrence_id(item["business_id"], "funds")
                        for item in (first, second)
                    ],
                }
            ],
        },
    )
    apply(case, first, second, statement, reconciliation)
    capture(case)
    data = page(case, statement_account_id=BANK)["data"]
    check_data(data)
    rows = data["collections"]["statements"]["items"]
    assert sorted(item["amount_fen"] for item in rows) == ["400", "600"]
    assert {item["memo"] for item in rows} == {"原始用途0", "原始用途1"}
    assert all(item["party"] == "银行原文交易方" for item in rows)
    for item in rows:
        assert item["batch_payment"]["bank_row_count"] == 2
        assert item["batch_payment"]["total_fen"] == "1000"
        assert sorted(part["amount_fen"] for part in item["batch_payment"]["items"]) == [
            "300",
            "700",
        ]


def test_frozen_month_and_month_only_correction_show_original_business_period(case):
    initialize(case)
    *_, purchase, _ = six()
    purchase["data"]["purchase_price_fen"] = "100"
    (purchase,) = apply(case, purchase)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    capture(case)
    before = page(case)
    purchase["data"]["purchase_price_fen"] = "90"
    actual = case.app.accounting_apply(
        case.request(
            correction_period="2026-02",
            mutations=[{"op": "replace", "expected_seq": 1, "fact": purchase}],
        )
    )
    assert actual["completion"] == "complete", actual
    capture(case)
    assert page(case) == before
    capture(case, "2026-02")
    data = page(case, "2026-02")["data"]
    check_data(data)
    events = data["collections"]["investment_events"]["items"]
    assert sorted(item["cost_fen"] for item in events) == ["-100", "90"]
    assert all(
        item["period"] == "2026-01"
        and item["date"] is None
        and item["type"].startswith("更正原业务")
        for item in events
    )
    assert data["investments"]["subscription_cost_fen"] == "-10"
    assert data["investments"]["actual_payments_fen"] == "0"


@pytest.mark.parametrize(
    "query",
    (
        {"movement_account_id": BANK},
        {"movement_account_type": "cash", "movement_account_id": BANK},
        {"statement_account_id": CASH},
        {"movement_account_selection": "first", "section": "movements"},
        {"cursor": "fake"},
    ),
)
def test_account_filters_and_first_have_exact_meaning(case, query):
    initialize(case)
    capture(case)
    with pytest.raises(ContractError) as error:
        case.kernel.reading.page("funds", selection(case), query)
    assert error.value.code in {"invalid_input", "not_found"}


def test_501_fund_sources_and_1002_events_have_full_totals_and_bounded_hot_pages(case, monkeypatch):
    initialize(case)
    *_, prototype, _ = six()
    prototype["data"]["purchase_price_fen"] = "1"
    purchases = [deepcopy(prototype) | {"business_id": str(uuid4())} for _ in range(501)]
    apply(case, *purchases[:500])
    apply(case, purchases[-1])
    paid = payment(purchases[0], 501, party=COUNTERPARTY, slot="primary")
    paid["data"]["allocations"] = [
        {"source": {"business_id": item["business_id"], "slot": "primary"}, "amount_fen": "1"}
        for item in purchases
    ]
    apply(case, paid)
    calls = []
    original = case.kernel.reading.investment.money_fund_activity

    def tracked(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(case.kernel.reading.investment, "money_fund_activity", tracked)
    capture(case)
    assert len(calls) == 1
    initial = page(case)["data"]
    assert (
        initial["investments"]["subscription_cost_fen"]
        == initial["investments"]["actual_payments_fen"]
        == "501"
    )
    collection = initial["collections"]["investment_events"]
    assert (len(collection["items"]), collection["page"]["total_count"]) == (20, 1002)
    found = list(collection["items"])
    while collection["page"]["has_more"]:
        collection = page(
            case, section="investment_events", limit=500, cursor=collection["page"]["next_cursor"]
        )["data"]["collections"]["investment_events"]
        found.extend(collection["items"])
    assert len(found) == len({item["id"] for item in found}) == 1002
    assert len(calls) == 1
