"""Funds use relevant investment positions without unrelated tax or settled history work."""

import json
from collections import Counter
from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.ledger import _read_contents
from ai_accounting_modular.reading import _funds, _monthly
from tests.modular.application._investment_portability_case import invested as invested
from tests.modular.application._investment_portability_case import seed as seed_investments
from tests.modular.domains.financing._cooperation_support import PROJECT, contract
from tests.modular.domains.financing._cooperation_support import funding as cooperation_funding
from tests.modular.domains.financing._investment_support import acquire, dispose, terms
from tests.modular.domains.financing._support import BANK, COUNTERPARTY, PRODUCT, fact, payment, six

from .test_funds import apply, capture, check_data, funding, initialize, page, selection
from .test_funds import case as case
from .test_investments import _sources


def state(case, month="2026-01"):
    with case.kernel.storage.read_unit(case.company) as read:
        return read.repository("reading").state(_monthly.key(selection(case, month)), "funds")


def adopted(case, *facts, **fields):
    for value in facts:
        _sources(value, case.source_id)
    return apply(case, *facts, **fields)


def income(agreement, amount="100", day="2026-01-06"):
    return fact(
        "investment.income",
        {
            "terms_id": agreement["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "fund_distribution",
            "amount_fen": amount,
        },
        period=day[:7],
        actual=day,
    )


@pytest.mark.parametrize("change", ["income", "position", "loss", "receipt", "payment", "account"])
def test_real_funds_changes_still_invalidate_the_published_month(case, change):
    initialize(case)
    agreement = terms()
    bought, earned = acquire(agreement, "1000", "10"), income(agreement)
    adopted(case, funding("5000"), agreement, bought, earned)
    capture(case)
    before = page(case)["data"]
    assert state(case)["state"] == "ready"
    if change == "income":
        actual = income(agreement, "7", "2026-01-07")
    elif change == "position":
        actual = acquire(agreement, "200", "2", "2026-01-07")
    elif change == "loss":
        actual = fact(
            "investment.loss",
            {
                "terms_id": agreement["business_id"],
                "source": {"business_id": bought["business_id"], "slot": "cost"},
                "counterparty_id": COUNTERPARTY,
                "quantity": "2",
                "loss_basis": "legal_extinction",
                "recovered_fen": "0",
            },
            actual="2026-01-07",
        )
    elif change in {"receipt", "payment"}:
        actual = payment(
            earned if change == "receipt" else bought,
            100 if change == "receipt" else 500,
            slot="income" if change == "receipt" else "primary",
            party=COUNTERPARTY,
            direction="inflow" if change == "receipt" else "outflow",
            day="2026-01-07",
        )
    else:
        with case.kernel.storage.read_unit(case.company) as read:
            account = case.kernel.identity.get_batch((BANK,), read)[0]
        adopted(
            case,
            objects=[
                {
                    **{key: account[key] for key in ("object_id", "kind", "data")},
                    "expected_seq": account["seq"],
                    "name": "合成更名银行账户",
                }
            ],
        )
        actual = None
    if actual is not None:
        adopted(case, actual)
    assert state(case)["state"] == "preparing"
    capture(case)
    after = page(case)["data"]
    check_data(after)
    assert after != before
    (product,) = after["collections"]["investment_products"]["items"]
    expected = {
        "income": ("recognized_income_fen", "107"),
        "position": ("closing_cost_fen", "1200"),
        "loss": ("actual_loss_fen", "200"),
        "receipt": ("actual_receipts_fen", "100"),
        "payment": ("actual_payments_fen", "500"),
    }
    if change in expected:
        field, value = expected[change]
        assert product[field] == value
    else:
        bank = next(
            row for row in after["collections"]["accounts"]["items"] if row["account_id"] == BANK
        )
        assert bank["name"] == "合成更名银行账户"


def history(case, family, count):
    facts, objects = [], []
    for index in range(count):
        product = str(uuid4())
        objects.append(
            {
                "object_id": product,
                "kind": "investment_product",
                "name": f"合成已退出投资{index}",
                "expected_seq": 0,
                "data": {"category": "money_fund"},
            }
        )
        if family == "legacy":
            *_, bought, sold = six()
            bought["data"].update(fund_id=product, purchase_price_fen="100")
            sold["data"].update(fund_id=product, net_proceeds_fen="100")
            sold["data"]["costs"][0]["amount_fen"] = "100"
        else:
            agreement = terms()
            agreement["data"]["product_id"] = product
            bought, sold = acquire(agreement, "100", "3"), dispose(agreement, "3", "100")
            facts.append(agreement)
        facts.extend(
            (
                bought,
                sold,
                payment(bought, 100, slot="primary", party=COUNTERPARTY, day="2026-01-05"),
                payment(
                    sold,
                    100,
                    slot="primary",
                    party=COUNTERPARTY,
                    direction="inflow",
                    day="2026-01-06",
                ),
            )
        )
    adopted(case, *facts, objects=objects)


def measured_capture(case, monkeypatch, month):
    ledger, identity = case.kernel.ledger, case.kernel.identity
    with case.kernel.storage.read_unit(case.company) as read:
        repository = type(read.repository("ledger"))
    work = Counter(
        {
            key: 0
            for key in (
                "raw_cost_rows",
                "cost_rows",
                "stock_target_rows",
                "target_decodes",
                "identity_keys",
                "fund_balance_keys",
                "body_refs",
            )
        }
    )
    raw_costs, costs = repository.read_cost_directory_rows, ledger.capture_cost_directory
    identities, balances = identity.get_batch, ledger.account_balances
    contents, decode = ledger.get_adopted_contents, _read_contents._content

    def raw_cost_rows(owner, *args, **kwargs):
        rows = raw_costs(owner, *args, **kwargs)
        work["raw_cost_rows"] += len(rows)
        return rows

    def cost_rows(*args, **kwargs):
        rows = costs(*args, **kwargs)
        work["cost_rows"] += len(rows)
        return rows

    def names(keys, *args, **kwargs):
        work["identity_keys"] += len(keys)
        return identities(keys, *args, **kwargs)

    def amounts(keys, *args, **kwargs):
        work["fund_balance_keys"] += sum(
            key.account_code == "1101" and key.object_id is not None for key in keys
        )
        return balances(keys, *args, **kwargs)

    def bodies(refs, *args, **kwargs):
        work["body_refs"] += len(refs)
        return contents(refs, *args, **kwargs)

    def decoded(row, field, *args, **kwargs):
        work["target_decodes"] += field == "target"
        return decode(row, field, *args, **kwargs)

    def targets(original, *, only_positions=False):
        def selected(*args, **kwargs):
            rows = original(*args, **kwargs)
            if not only_positions or args[1] == ("investment.position",):
                work["stock_target_rows"] += len(rows.items)
            return rows

        return selected

    prepare = _funds.prepare

    def measured_prepare(*args, **kwargs):
        with monkeypatch.context() as observed:
            observed.setattr(repository, "read_cost_directory_rows", raw_cost_rows)
            observed.setattr(ledger, "capture_cost_directory", cost_rows)
            observed.setattr(identity, "get_batch", names)
            observed.setattr(ledger, "account_balances", amounts)
            observed.setattr(ledger, "get_adopted_contents", bodies)
            observed.setattr(_read_contents, "_content", decoded)
            observed.setattr(
                ledger,
                "query_adopted_targets",
                targets(ledger.query_adopted_targets, only_positions=True),
            )
            observed.setattr(
                ledger, "query_investment_holdings", targets(ledger.query_investment_holdings)
            )
            return prepare(*args, **kwargs)

    with monkeypatch.context() as prepared:
        prepared.setattr(_funds, "prepare", measured_prepare)
        capture(case, month)
    result = page(case, month)["data"]
    check_data(result)
    return result, dict(work)


@pytest.mark.parametrize("family", ["legacy", "modern"])
def test_settled_history_does_not_increase_current_position_work_or_hide_tail_cash(
    case, monkeypatch, family, record_property
):
    initialize(case)
    tail, held = terms(), terms(measurement="other_financial_cost")
    bought, sold = acquire(tail, "100", "3"), dispose(tail, "3", "120")
    ongoing = acquire(held, "200", "2")
    adopted(
        case,
        funding("10000"),
        tail,
        held,
        bought,
        sold,
        ongoing,
        payment(bought, 100, slot="primary", party=COUNTERPARTY, day="2026-01-05"),
        payment(ongoing, 200, slot="primary", party=COUNTERPARTY, day="2026-01-05"),
        funding("100", "2026-02-01"),
        payment(
            sold, 120, slot="primary", party=COUNTERPARTY, direction="inflow", day="2026-02-03"
        ),
    )
    history(case, family, 1)
    before, work_before = measured_capture(case, monkeypatch, "2026-02")
    products = {
        row["investment_id"]: row for row in before["collections"]["investment_products"]["items"]
    }
    assert set(products) == {tail["business_id"], held["business_id"]}
    assert all(row["fund_id"] == PRODUCT for row in products.values())
    assert products[tail["business_id"]]["closing_cost_fen"] == "0"
    assert products[tail["business_id"]]["pending_receipts_fen"] == "0"
    assert products[tail["business_id"]]["actual_receipts_fen"] == "120"
    assert products[held["business_id"]]["closing_cost_fen"] == "200"
    assert before["investments"] == {"closing_cost_fen": "200"}
    history(case, family, 4)
    after, work_after = measured_capture(case, monkeypatch, "2026-02")
    record_property("one_history", json.dumps(work_before, sort_keys=True))
    record_property("five_histories", json.dumps(work_after, sort_keys=True))
    assert after == before
    assert work_after == work_before, {"one_history": work_before, "five_histories": work_after}


def test_unknown_investment_balance_is_not_replaced_by_selected_cost(case):
    initialize(case, opening=False)
    *_, bought, _ = six()
    bought["data"]["purchase_price_fen"] = "300"
    adopted(case, bought)
    capture(case)
    result = page(case)["data"]
    check_data(result)
    assert result["investments"]["closing_cost_fen"] is None
    (product,) = result["collections"]["investment_products"]["items"]
    assert product["closing_cost_fen"] is None
    assert product["actual_payments_fen"] == product["actual_receipts_fen"] == "0"


def test_opening_investments_and_receipts_keep_their_exact_professional_positions(invested):
    inputs = seed_investments(invested)
    scope = selection(invested, "2026-07")
    with invested.kernel.storage.read_unit(invested.company) as read:
        owner = invested.kernel.reading.investment
        full = owner.investment_activity(scope, read)
        positions = owner.investment_positions(scope, read)
    assert positions.closing_cost == full.closing_cost
    assert positions.products == full.products
    assert positions.cooperation_positions == full.cooperation_positions
    assert positions.issues == full.issues
    by_terms = {item.terms_id: item for item in positions.products}
    assert set(by_terms) == {item["business_id"] for item in inputs["terms"]}
    assert by_terms[inputs["terms"][0]["business_id"]].actual_receipts_fen == 7
    assert by_terms[inputs["terms"][3]["business_id"]].actual_receipts_fen == 3
    assert all(item.actual_payments_fen == 200 for item in by_terms.values())
    capture(invested, "2026-07")
    result = page(invested, "2026-07")["data"]
    check_data(result)
    rows = {
        item["investment_id"]: item
        for item in result["collections"]["investment_products"]["items"]
    }
    assert rows[inputs["terms"][0]["business_id"]]["actual_receipts_fen"] == "7"
    assert rows[inputs["terms"][3]["business_id"]]["actual_receipts_fen"] == "3"
    assert rows[inputs["contract"]["business_id"]]["product_type"] == "cooperation"


def test_closed_income_correction_keeps_frozen_funds_and_only_changes_later_month(case):
    initialize(case)
    agreement = terms()
    original = income(agreement)
    adopted(case, agreement, original)
    with case.kernel.storage.write_unit(case.company) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    capture(case)
    before = page(case)["data"]
    changed = deepcopy(original)
    changed["evidence_ids"] = [case.source_id]
    changed["data"]["amount_fen"] = "150"
    outcome = case.app.accounting_apply(
        case.request(
            correction_period="2026-02",
            mutations=[{"op": "replace", "expected_seq": 1, "fact": changed}],
        )
    )
    assert outcome["completion"] == "complete", outcome
    assert state(case)["state"] == "ready"
    assert page(case)["data"] == before
    capture(case, "2026-02")
    result = page(case, "2026-02")["data"]
    check_data(result)
    (product,) = result["collections"]["investment_products"]["items"]
    assert product["investment_id"] == agreement["business_id"]
    assert product["recognized_income_fen"] == "50"
    assert product["actual_receipts_fen"] == "0"
    assert product["closing_cost_fen"] is None


@pytest.mark.parametrize("category", ["money_fund", "cooperation"])
def test_product_rename_invalidates_a_static_month_and_preserves_amounts(case, category):
    initialize(case)
    if category == "cooperation":
        agreement = contract()
        adopted(
            case,
            agreement,
            cooperation_funding(agreement),
            objects=[
                {
                    "object_id": PROJECT,
                    "kind": "investment_product",
                    "name": "合成合作项目",
                    "expected_seq": 0,
                    "data": {"category": category},
                }
            ],
        )
    else:
        agreement = terms()
        adopted(case, agreement, acquire(agreement, "100", "3"))
    adopted(case, funding("10", "2026-02-01"))
    capture(case, "2026-02")
    before = page(case, "2026-02")["data"]
    rows = before["collections"]["investment_products"]["items"]
    product_id = next(row["fund_id"] for row in rows if row["product_type"] == category)
    with case.kernel.storage.read_unit(case.company) as read:
        original = case.kernel.identity.get_batch((product_id,), read)[0]
    name = "合成更名后的投资产品"
    adopted(
        case,
        objects=[
            {
                **{key: original[key] for key in ("object_id", "kind", "data")},
                "name": name,
                "expected_seq": original["seq"],
            }
        ],
    )
    assert state(case, "2026-02")["state"] == "preparing"
    capture(case, "2026-02")
    after = page(case, "2026-02")["data"]
    check_data(after)
    assert after["investments"] == before["investments"]
    assert {
        row["investment_id"]: row for row in after["collections"]["investment_products"]["items"]
    } == {
        row["investment_id"]: row | ({"name": name} if row["fund_id"] == product_id else {})
        for row in rows
    }
