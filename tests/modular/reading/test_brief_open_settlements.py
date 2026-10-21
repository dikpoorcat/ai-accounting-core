"""Actual cash, offsets, debt transfers and investment agency keep their own meaning."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.reading import _brief_open
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._cooperation_support import (
    PROJECT,
    agency_payment,
    contract,
    funding,
)
from tests.modular.domains.financing._cooperation_support import (
    expense as cooperative_expense,
)
from tests.modular.domains.financing._support import BANK, COUNTERPARTY, fact
from tests.modular.domains.people.test_read_remuneration import bank_object

from .test_brief_open import apply, members, page, payment, publish
from .test_funds import apply as apply_financing
from .test_funds import initialize
from .test_funds_integrity import original_row, pair


def test_real_payment_offset_and_transfer_are_not_folded_into_cash(case):
    expense = case.seed()
    bank, owner = bank_object(), str(uuid4())
    advance = fact(
        "operations.advance",
        {
            "side": "supplier",
            "counterparty_id": case.party,
            "amount_fen": "10000",
            "purpose": "operating",
        },
    )
    advance["evidence_ids"] = [case.source_id]
    offset = fact(
        "money.transfer",
        {
            "type": "offset",
            "offset_kind": "advance_application",
            "first": {
                "source": {"business_id": advance["business_id"], "slot": "advance"},
                "amount_fen": "10000",
            },
            "second": {
                "source": {"business_id": expense["business_id"], "slot": "primary"},
                "amount_fen": "10000",
            },
        },
    )
    offset["evidence_ids"] = [case.source_id]
    accepted = fact(
        "operations.reimbursement",
        {
            "type": "acceptance",
            "payer_id": owner,
            "payer_kind": "owner",
            "sources": [
                {
                    "source": {"business_id": expense["business_id"], "slot": "primary"},
                    "amount_fen": "40000",
                    "recipient_id": case.party,
                }
            ],
        },
    )
    accepted["evidence_ids"] = [case.source_id]
    apply(
        case,
        advance,
        offset,
        accepted,
        payment(case, expense["business_id"], bank["object_id"], "20000"),
        objects=[
            bank,
            {
                "object_id": owner,
                "kind": "person",
                "name": "实际垫付人",
                "expected_seq": 0,
                "data": {},
            },
        ],
    )
    snapshot = publish(case)
    group = next(
        row
        for row in page(case, snapshot)["collections"]["open_items"]["items"]
        if row["description"] == "管理费用"
    )
    assert (
        group["source_amount_fen"],
        group["paid_fen"],
        group["other_settled_fen"],
        group["outstanding_fen"],
    ) == ("100000", "20000", "50000", "30000")
    item = members(case, snapshot, group["group_key"])["collections"]["members"]["items"][0]
    assert item["status"] == "partial" and item["other_settled_fen"] == "50000"


def test_corrected_80000_cost_with_100000_cash_lists_only_the_20000_claim(case):
    expense = case.seed()
    bank = bank_object()
    apply(case, payment(case, expense["business_id"], bank["object_id"], "100000"), objects=[bank])
    changed = deepcopy(expense)
    changed["data"]["amount_fen"] = "80000"
    claim = fact(
        "operations.overpayment",
        {
            "source": {"business_id": expense["business_id"], "slot": "primary"},
            "amount_fen": "20000",
        },
        period="2026-02",
        actual="2026-02-10",
    )
    claim["evidence_ids"] = [case.source_id]
    result = case.app.accounting_apply(
        case.request(
            mutations=[
                {"op": "replace", "expected_seq": 1, "fact": changed},
                {"op": "create", "expected_seq": 0, "fact": claim},
            ],
        )
    )
    assert result["completion"] == "complete", result
    snapshot = publish(case, "2026-02")
    summary = page(case, snapshot)
    assert (
        summary["open_items"]["payable_fen"] == "0"
        and summary["open_items"]["receivable_fen"] == "20000"
    )
    group = summary["collections"]["open_items"]["items"][0]
    assert group["description"] == "多付款追收" and group["outstanding_fen"] == "20000"


def cooperation_due(case):
    initialize(case)
    agreement = contract()
    agreement["data"]["contract_source"]["source_id"] = case.source_id
    charged = cooperative_expense(agreement, project="300", company="150")
    paid = agency_payment(agreement, charged, project="240", company="120")
    operator = fact(
        "investment.cooperation_operator_transfer",
        {
            "contract_id": agreement["business_id"],
            "direction": "into_pool",
            "purpose": "working_capital",
            "amount_fen": "140",
        },
        actual="2026-01-04",
    )
    product = {
        "object_id": PROJECT,
        "kind": "investment_product",
        "name": "合成合作投资",
        "expected_seq": 0,
        "data": {"category": "cooperation"},
    }
    funded = funding(agreement)
    apply_financing(case, agreement, funded, operator, charged, paid, objects=[product])
    return agreement, charged, paid, funded


def test_cooperation_agency_and_formal_company_supplement_share_open_items(case):
    agreement, charged, _, _ = cooperation_due(case)
    snapshot = publish(case)
    groups = page(case, snapshot)["collections"]["open_items"]["items"]
    trade = next(row for row in groups if row["description"] == "服务业务成本")
    assert (trade["paid_fen"], trade["other_settled_fen"], trade["outstanding_fen"]) == (
        "0",
        "120",
        "30",
    )
    supplement = next(
        row
        for row in groups
        if row["description"].startswith("合作投资公司应补")
        and row["category_key"] == "other_payables"
    )
    assert supplement["outstanding_fen"] == "20" and supplement["status"] == "current"
    assert (
        supplement["source_amount_fen"]
        is supplement["paid_fen"]
        is supplement["other_settled_fen"]
        is None
    )
    position = members(case, snapshot, supplement["group_key"])["collections"]["members"]["items"][
        0
    ]
    assert position["subject_id"] == agreement["business_id"]
    with case.kernel.storage.read_unit(case.company) as read:
        scope = _brief_open.member_scope(snapshot, read, position["id"])
    assert scope["position"]["contract_id"] == agreement["business_id"]
    assert scope["position"]["field"] == "company_due_fen"
    assert scope["obligations"] == scope["allocations"] == []
    detail = members(case, snapshot, trade["group_key"])["collections"]["members"]["items"][0]
    assert detail["subject_id"] == charged["business_id"]
    with case.kernel.storage.read_unit(case.company) as read:
        actual = case.kernel.identity.get_batch([COUNTERPARTY], read)[0]
    assert detail["party"] == actual["name"]


def test_closed_share_correction_can_clear_company_supplement_without_pretending_it_was_paid(case):
    agreement, charged, agency, funded = cooperation_due(case)
    apply_financing(
        case,
        *pair(BANK, [original_row(case, str(uuid4()), "100", "2026-01-03", "outflow")], [funded]),
    )
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    agreement["data"]["company_revenue_expense_share"] = "0.4"
    charged["data"]["amount_fen"] = "120"
    agency["data"]["company_amount_fen"] = "96"
    for item in (agreement, charged, agency):
        item["evidence_ids"] = [case.source_id]
    changed = case.app.accounting_apply(
        case.request(
            mutations=[
                {"op": "replace", "expected_seq": 1, "fact": item}
                for item in (agreement, charged, agency)
            ],
            correction_period="2026-02",
        )
    )
    assert changed["completion"] == "complete", changed
    snapshot = publish(case)
    groups = page(case, snapshot)["collections"]["open_items"]["items"]
    company = next(row for row in groups if row["description"].startswith("合作投资公司应补"))
    assert company["outstanding_fen"] == "20" and company["current_outstanding_fen"] == "0"
    assert company["status"] == company["current_status"] == "current"
    detail = members(case, snapshot, company["group_key"])["collections"]["members"]["items"][0]
    assert detail["paid_fen"] is detail["other_settled_fen"] is None
    february = page(case, publish(case, "2026-02"))["collections"]["open_items"]["items"]
    operator = next(row for row in february if row["description"].startswith("合作投资经营者应补"))
    assert operator["outstanding_fen"] == "4" and operator["category_key"] == "other_receivables"
    assert not any(row["description"].startswith("合作投资公司应补") for row in february)


def test_quiet_month_carries_only_the_last_formal_position_with_its_original_month(case):
    cooperation_due(case)
    snapshot = publish(case, "2026-02")
    data = page(case, snapshot)
    company = next(
        row
        for row in data["collections"]["open_items"]["items"]
        if row["description"].startswith("合作投资公司应补")
    )
    assert company["outstanding_fen"] == "20" and data["open_items"]["payable_fen"] == "50"
    detail = members(case, snapshot, company["group_key"])["collections"]["members"]["items"][0]
    assert detail["source_period"] == detail["recognition"]["period"] == "2026-01"
    assert detail["source_amount_fen"] is detail["paid_fen"] is None


@pytest.mark.parametrize("changed_field", ("project_id", "operator_id"))
def test_another_project_or_operator_position_is_not_progress_for_the_old_one(case, changed_field):
    agreement, _, _, funded = cooperation_due(case)
    apply_financing(
        case,
        *pair(BANK, [original_row(case, str(uuid4()), "100", "2026-01-03", "outflow")], [funded]),
    )
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    replacement = str(uuid4())
    agreement["data"][changed_field] = replacement
    agreement["evidence_ids"] = [case.source_id]
    changed = case.app.accounting_apply(
        case.request(
            objects=[
                {
                    "object_id": replacement,
                    "kind": "investment_product" if changed_field == "project_id" else "person",
                    "name": "另一合作项目" if changed_field == "project_id" else "另一经营者",
                    "expected_seq": 0,
                    "data": {"category": "cooperation"} if changed_field == "project_id" else {},
                }
            ],
            mutations=[{"op": "replace", "expected_seq": 1, "fact": agreement}],
            correction_period="2026-02",
        )
    )
    assert changed.get("completion") == "complete", changed
    january = publish(case)
    old = next(
        group
        for group in page(case, january)["collections"]["open_items"]["items"]
        if group["description"].startswith("合作投资公司应补")
    )
    assert old["outstanding_fen"] == "20" and old["status"] == "current"
    assert old["current_status"] is old["current_outstanding_fen"] is None
    detail = members(case, january, old["group_key"])["collections"]["members"]["items"][0]
    assert detail["source_amount_fen"] is detail["paid_fen"] is None
    new = next(
        group
        for group in page(case, publish(case, "2026-02"))["collections"]["open_items"]["items"]
        if group["description"].startswith("合作投资公司应补")
    )
    assert new["outstanding_fen"] == new["current_outstanding_fen"] == "20"
    if changed_field == "project_id":
        assert new["description"].endswith("另一合作项目")
    else:
        assert new["party"] == "另一经营者"


def test_same_operator_contracts_and_their_same_supplier_costs_keep_separate_groups(case):
    first, _, _, _ = cooperation_due(case)
    second = contract()
    project_id = str(uuid4())
    second["data"]["project_id"] = project_id
    second["data"]["contract_source"]["source_id"] = case.source_id
    charged = cooperative_expense(second, project="300", company="150")
    agency = agency_payment(second, charged, project="240", company="120")
    operator = fact(
        "investment.cooperation_operator_transfer",
        {
            "contract_id": second["business_id"],
            "direction": "into_pool",
            "purpose": "working_capital",
            "amount_fen": "140",
        },
        actual="2026-01-04",
    )
    product = {
        "object_id": project_id,
        "kind": "investment_product",
        "name": "第二合作项目",
        "expected_seq": 0,
        "data": {"category": "cooperation"},
    }
    apply_financing(case, second, funding(second), operator, charged, agency, objects=[product])
    snapshot = publish(case)
    data = page(case, snapshot)
    groups = data["collections"]["open_items"]["items"]
    assert data["open_items"]["payable_fen"] == "100" and len(groups) == 4
    supplements = [row for row in groups if row["description"].startswith("合作投资公司应补")]
    assert len(supplements) == 2 and len({row["party"] for row in supplements}) == 1
    assert len([row for row in groups if row["description"] == "服务业务成本"]) == 2
    contracts = set()
    for group in supplements:
        item = members(case, snapshot, group["group_key"])["collections"]["members"]["items"][0]
        with case.kernel.storage.read_unit(case.company) as read:
            scope = _brief_open.member_scope(snapshot, read, item["id"])
        contracts.add(scope["position"]["contract_id"])
    assert contracts == {first["business_id"], second["business_id"]}
