"""Names remain ordered after serialization into SQLite TEXT indexes."""

from copy import deepcopy
from uuid import uuid4

import pytest

from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, PRODUCT, objects, six

from .test_funds import apply, capture, funding, initialize, page
from .test_funds_integrity import original_row, pair


@pytest.mark.parametrize("names", [('"甲银行', "'甲银行"), ("a", "a!")])
def test_all_five_collections_keep_real_name_order_and_first_account(case, names):
    initialize(case)
    supplied, facts, accounts = [], [], []
    template = next(obj for obj in objects() if obj["object_id"] == BANK)
    for index, name in enumerate(names):
        account_id, person = (BANK if index == 0 else str(uuid4())), str(uuid4())
        account = deepcopy(template)
        account.update(object_id=account_id, name=name, expected_seq=1 if index == 0 else 0)
        account["data"].update(opened_period="2026-01", zero_at_opening=True)
        supplied.extend(
            [
                account,
                {
                    "object_id": person,
                    "kind": "person",
                    "name": name,
                    "expected_seq": 0,
                    "data": {},
                },
            ]
        )
        actual = funding("10")
        actual["data"].update(funds_account_id=account_id, owner_id=person)
        row = original_row(case, str(uuid4()), "10", "2026-01-02", "inflow") | {
            "counterparty_text": name
        }
        facts.extend([actual, *pair(account_id, [row], [actual])])
        accounts.append(account_id)
    *_, purchase, _ = six()
    for index, name in enumerate(names):
        product = PRODUCT if index == 0 else str(uuid4())
        supplied.append(
            {
                "object_id": product,
                "kind": "investment_product",
                "name": name,
                "expected_seq": 1 if index == 0 else 0,
                "data": {"category": "money_fund"},
            }
        )
        value = deepcopy(purchase)
        value["business_id"] = str(uuid4())
        value["data"].update(fund_id=product, purchase_price_fen="1")
        facts.append(value)
    apply(case, *facts, objects=supplied)
    capture(case)
    first = page(case, movement_account_selection="first")["data"]
    assert [item["name"] for item in first["collections"]["accounts"]["items"][:2]] == list(names)
    assert first["selected_movement_account"] == {"type": "bank", "account_id": accounts[0]}
    full = page(case)["data"]["collections"]
    assert [item["party"] for item in full["movements"]["items"]] == list(names)
    assert [item["party"] for item in full["statements"]["items"]] == list(names)
    assert [item["name"] for item in full["investment_products"]["items"]] == list(names)
    assert [item["name"] for item in full["investment_events"]["items"]] == list(names)
