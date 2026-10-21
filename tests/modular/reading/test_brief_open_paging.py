"""Both group and member pagination retain full-scope totals and stable identities."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.reading._funds_items import name_key
from tests.modular.application._working_case import case as case

from .test_brief_open import apply, members, page, publish


def test_501_member_group_and_21_other_parties_keep_complete_totals_and_do_not_net_directions(
    case, monkeypatch
):
    case.seed(expense=False)
    large = 9007199254740993
    expenses = [case.expense(str(large if index == 0 else 1)) for index in range(501)]
    objects = [
        {
            "object_id": str(uuid4()),
            "kind": "organization",
            "name": "同名供应商" if index < 2 else f"其他供应商{index:02d}",
            "expected_seq": 0,
            "data": {},
        }
        for index in range(21)
    ]
    for obj in objects:
        item = case.expense("1")
        item["data"]["creditor_id"] = obj["object_id"]
        expenses.append(item)
    advance = {
        "business_id": str(uuid4()),
        "kind": "operations.advance",
        "period": "2026-01",
        "evidence_ids": [case.source_id],
        "data": {
            "side": "supplier",
            "counterparty_id": case.party,
            "amount_fen": "40",
            "purpose": "operating",
        },
    }
    for offset in range(0, len(expenses), 250):
        apply(case, *expenses[offset : offset + 250], objects=objects if offset == 0 else [])
    apply(case, advance)
    snapshot = publish(case)
    data = page(case, snapshot)
    summary, collection = data["open_items"], data["collections"]["open_items"]
    # An agreed, unpaid advance has both a right to application and its real payment obligation.
    assert summary["total_count"] == 524 and summary["group_count"] == 24
    assert summary["payable_fen"] == str(large + 561)
    assert summary["receivable_fen"] == "40" and summary["receivable_count"] == 1
    assert collection["page"]["total_count"] == 24 and len(collection["items"]) == 20
    tail = page(case, snapshot, cursor=collection["page"]["next_cursor"])["collections"][
        "open_items"
    ]
    groups = collection["items"] + tail["items"]
    assert len({row["group_key"] for row in groups}) == 24
    assert [
        (name_key(row["party"]), name_key(row["description"]), row["group_key"]) for row in groups
    ] == sorted(
        (name_key(row["party"]), name_key(row["description"]), row["group_key"]) for row in groups
    )
    assert len([row for row in groups if row["party"] == "同名供应商"]) == 2
    big = next(row for row in groups if row["member_count"] == 501)
    get = case.kernel.ledger.get_adopted_contents
    seen = []

    def bounded(references, read):
        seen.append(tuple(ref["result_id"] for ref in references))
        return get(references, read)

    monkeypatch.setattr(case.kernel.ledger, "get_adopted_contents", bounded)
    first = members(case, snapshot, big["group_key"])["collections"]
    assert first["members"]["page"]["total_count"] == 524
    assert first["members"]["page"]["filtered_count"] == 501
    assert len(first["members"]["items"]) == len(first["vouchers"]["items"]) == 20
    assert list(map(len, seen)) == [20]
    seen.clear()
    rest = members(
        case, snapshot, big["group_key"], limit=500, cursor=first["members"]["page"]["next_cursor"]
    )["collections"]
    assert len(rest["members"]["items"]) == len(rest["vouchers"]["items"]) == 481
    assert list(map(len, seen)) == [481]
    seen.clear()
    maximum = members(case, snapshot, big["group_key"], limit=500)["collections"]
    assert len(maximum["members"]["items"]) == len(maximum["vouchers"]["items"]) == 500
    assert maximum["members"]["items"] == first["members"]["items"] + rest["members"]["items"][:480]
    assert (
        maximum["vouchers"]["items"] == first["vouchers"]["items"] + rest["vouchers"]["items"][:480]
    )
    assert list(map(len, seen)) == [500]
    assert not rest["members"]["page"]["has_more"]
    all_members = first["members"]["items"] + rest["members"]["items"]
    assert len({row["id"] for row in all_members}) == 501
    assert sum(int(row["outstanding_fen"]) for row in all_members) == large + 500
    changed = case.expense("1")
    apply(case, changed)
    newer = publish(case)
    with pytest.raises(ContractError) as error:
        members(case, newer, big["group_key"], cursor=first["members"]["page"]["next_cursor"])
    assert error.value.code == "snapshot_changed"
