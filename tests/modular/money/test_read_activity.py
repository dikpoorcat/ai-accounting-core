"""Public month reads retain true cash, formal originals and current text separately."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.money.public import MovementQuery, StatementQuery, stable_occurrence_id
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, CASH, OWNER, fact, objects, payment


def scope(case, month="2026-01", view="current"):
    return {"company_id": case.company, "period_from": month, "period_to": month, "view": view}


def apply(case, *facts, **extra):
    values = deepcopy(facts)
    for item in values:
        item["evidence_ids"] = [case.source_id]
    actual = case.app.accounting_apply(case.request(*values, **extra))
    assert actual["completion"] == "complete", actual
    return values


def initialize(case):
    case.seed(expense=False)
    supplied = objects()
    next(obj for obj in supplied if obj["object_id"] == BANK)["data"].update(
        opened_period="2026-01", zero_at_opening=True
    )
    apply(case, objects=supplied)


def funding(value="100", day="2026-01-02"):
    return fact(
        "financing.owner_funding",
        {"owner_id": OWNER, "funding_kind": "loan", "amount_fen": value, "funds_account_id": BANK},
        actual=day,
    )


def bank_pair(case, funded):
    row = {
        "row_id": str(uuid4()),
        "source": {"source_id": case.source_id, "location": "合成银行第1行"},
        "actual_on": funded["effective_on"],
        "direction": "inflow",
        "amount_fen": funded["data"]["amount_fen"],
        "memo": "原件中的股东借入款",
        "reference": "synthetic-ref-1",
        "counterparty_text": "原件交易方",
    }
    statement = fact(
        "money.bank_statement",
        {
            "funds_account_id": BANK,
            "opening_fen": "0",
            "closing_fen": row["amount_fen"],
            "rows": [row],
        },
    )
    matched = fact(
        "money.reconciliation",
        {
            "statement_id": statement["business_id"],
            "matches": [
                {
                    "statement_row_ids": [row["row_id"]],
                    "fund_movement_ids": [stable_occurrence_id(funded["business_id"], "funds")],
                }
            ],
        },
    )
    return statement, matched


def test_internal_transfer_is_two_account_legs_and_one_company_transfer(case):
    initialize(case)
    transfer = fact(
        "money.transfer",
        {"type": "physical", "from_account_id": BANK, "to_account_id": CASH, "amount_fen": "300"},
        actual="2026-01-03",
    )
    apply(case, funding("1000"), transfer)
    with case.kernel.storage.read_unit(case.company) as read:
        actual = case.kernel.money.account_activity(scope(case), read)
        assert (
            actual.inflow_fen,
            actual.outflow_fen,
            actual.internal_transfer_fen,
            actual.movement_count,
        ) == (1000, 0, 300, 3)
        accounts = {item.account_id: item for item in actual.accounts}
        assert (accounts[BANK].inflow_fen, accounts[BANK].outflow_fen) == (1000, 300)
        assert (accounts[CASH].inflow_fen, accounts[CASH].outflow_fen) == (300, 0)
        first = case.kernel.money.query_movements(
            scope(case), MovementQuery(BANK), PageQuery(1), read
        )
        assert (first.total_count, first.filtered_count, len(first.items)) == (3, 2, 1)
        second = case.kernel.money.query_movements(
            scope(case), MovementQuery(BANK), PageQuery(1, first.next_cursor), read
        )
        assert (
            first.items[0]["movement"]["movement_id"] != second.items[0]["movement"]["movement_id"]
        )
        assert second.next_cursor is None
        with pytest.raises(ContractError) as caught:
            case.kernel.money.query_movements(
                scope(case), MovementQuery(CASH), PageQuery(1, first.next_cursor), read
            )
        assert caught.value.code == "snapshot_changed"


def test_allocation_reads_its_adopted_obligation_not_current_source_definition(case, monkeypatch):
    initialize(case)
    expense = case.expense()
    paid = payment(expense, 30000, slot="primary", party=case.party)
    apply(case, funding("200000"), expense, paid)
    saved = case.get(paid["business_id"])["businesses"][0]["results"][0]
    quantity = saved["basis"]["adopted_quantities"][0]
    assert quantity["party_id"] == case.party and quantity["amount_fen"] == "100000"

    def forbidden(*args, **kwargs):
        raise AssertionError("实际付款收款人须来自消费时的正式采用量")

    monkeypatch.setattr(case.kernel.ledger, "resource_definitions", forbidden)
    with case.kernel.storage.read_unit(case.company) as read:
        values = case.kernel.money.query_movements(
            scope(case), MovementQuery(BANK), PageQuery(500), read
        )
    actual = next(
        item for item in values.items if item["reference"]["business_id"] == paid["business_id"]
    )
    assert len(actual["allocations"]) == 1
    assert actual["allocations"][0].party_id == case.party
    assert actual["allocations"][0].amount_fen == 30000


def test_original_text_edit_preserves_adoption_and_frozen_row_reads(case):
    initialize(case)
    funded = funding()
    statement, matched = bank_pair(case, funded)
    funded, statement, matched = apply(case, funded, statement, matched)
    before = case.get(statement["business_id"])["businesses"][0]["results"][0]["reference"]
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    statement["data"]["rows"][0].update(
        memo="补充清楚的原文用途", reference="new-reference", counterparty_text="补全交易方"
    )
    changed = case.app.accounting_apply(
        case.request(mutations=[{"op": "replace", "expected_seq": 1, "fact": statement}])
    )
    assert changed["completion"] == "complete", changed
    assert changed["accounting"] == "unchanged"
    current = case.get(statement["business_id"])["businesses"][0]["results"][0]["reference"]
    assert (
        current["result_id"] == before["result_id"]
        and current["generation"] == before["generation"]
    )
    with case.kernel.storage.read_unit(case.company) as read:
        formal = case.kernel.money.query_statement_rows(
            scope(case, view="as_posted"), StatementQuery(BANK), PageQuery(), read
        ).items[0]
        assert (formal.actual_on, formal.amount_fen, formal.account_id) == ("2026-01-02", 100, BANK)
        assert (formal.memo, formal.reference, formal.counterparty_text) == (
            "补充清楚的原文用途",
            "new-reference",
            "补全交易方",
        )
        assert formal.source_location == statement["data"]["rows"][0]["source"]
        complete = case.kernel.money.account_activity(scope(case, view="as_posted"), read)
        assert complete.original_rows == (formal,)
        group = next(
            item
            for item in complete.reconciliation_groups
            if item.group_id == formal.match_group_id
        )
        assert group.movement_ids == (stable_occurrence_id(funded["business_id"], "funds"),)
        assert len(complete.movements) == complete.movement_count == 1


@pytest.mark.parametrize("new_source", (False, True))
def test_current_text_from_a_different_location_cannot_decorate_a_frozen_row(case, new_source):
    initialize(case)
    funded = funding()
    statement, matched = bank_pair(case, funded)
    _, statement, _ = apply(case, funded, statement, matched)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    sources = []
    source_id = case.source_id
    if new_source:
        source_id = str(uuid4())
        text = "二月补充的另一份合成资料"
        sources.append(
            {
                "source_id": source_id,
                "kind": "owner_statement",
                "text": text,
                "sha256": sha256(text.encode()).hexdigest(),
                "archive_month": "2026-02",
            }
        )
    statement["data"]["rows"][0].update(
        source={"source_id": source_id, "location": "另一份原件位置"},
        memo="另一来源的原文用途",
        reference="another-reference",
        counterparty_text="另一来源的交易方",
    )
    changed = case.app.accounting_apply(
        case.request(
            sources=sources,
            mutations=[{"op": "replace", "expected_seq": 1, "fact": statement}],
        )
    )
    assert changed["completion"] == "complete", changed
    assert changed["accounting"] == "unchanged"
    with case.kernel.storage.read_unit(case.company) as read:
        formal = case.kernel.money.query_statement_rows(
            scope(case, view="as_posted"), StatementQuery(BANK), PageQuery(), read
        ).items[0]
        assert (formal.amount_fen, formal.actual_on) == (100, "2026-01-02")
        assert (
            formal.memo,
            formal.reference,
            formal.counterparty_text,
            formal.source_location,
        ) == (None, None, None, None)
        assert case.kernel.money.account_activity(
            scope(case, view="as_posted"), read
        ).original_rows == (formal,)


def test_wrong_current_row_is_not_a_supplement_to_frozen_money(case):
    initialize(case)
    funded = funding()
    statement, matched = bank_pair(case, funded)
    funded, statement, matched = apply(case, funded, statement, matched)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    funded["data"]["amount_fen"] = "120"
    statement["data"]["closing_fen"] = "120"
    statement["data"]["rows"][0].update(amount_fen="120", memo="新金额对应文字")
    changed = case.app.accounting_apply(
        case.request(
            correction_period="2026-02",
            mutations=[
                {"op": "replace", "expected_seq": 1, "fact": funded},
                {"op": "replace", "expected_seq": 1, "fact": statement},
            ],
        )
    )
    assert changed["completion"] == "complete", changed
    with case.kernel.storage.read_unit(case.company) as read:
        original = case.kernel.money.query_statement_rows(
            scope(case, view="as_posted"), StatementQuery(BANK), PageQuery(), read
        ).items[0]
        latest = case.kernel.money.query_statement_rows(
            scope(case), StatementQuery(BANK), PageQuery(), read
        ).items[0]
        assert (original.amount_fen, original.memo, original.source_location) == (100, None, None)
        assert case.kernel.ledger.get_adopted_contents((original.adopted,), read)[0].basis[
            "source_locations"
        ]
        assert (latest.amount_fen, latest.memo) == (120, "新金额对应文字")


def test_many_original_rows_share_one_explicit_matching_group(case):
    initialize(case)
    funded = funding()
    statement, matched = bank_pair(case, funded)
    first = statement["data"]["rows"][0]
    first["amount_fen"] = "50"
    second = deepcopy(first)
    second.update(
        row_id=str(uuid4()),
        reference="synthetic-ref-2",
        source={"source_id": case.source_id, "location": "合成银行第2行"},
    )
    statement["data"]["rows"].append(second)
    matched["data"]["matches"][0]["statement_row_ids"].append(second["row_id"])
    apply(case, funded, statement, matched)
    with case.kernel.storage.read_unit(case.company) as read:
        complete = case.kernel.money.account_activity(scope(case), read)
        assert len(complete.original_rows) == 2
        assert len(complete.reconciliation_groups) == 1
        group = complete.reconciliation_groups[0]
        assert {row.match_group_id for row in complete.original_rows} == {group.group_id}
        assert set(group.statement_row_ids) == {first["row_id"], second["row_id"]}
        assert group.movement_ids == (stable_occurrence_id(funded["business_id"], "funds"),)
        assert all(row.source_location is not None for row in complete.original_rows)


def test_platform_text_round_trips_but_does_not_create_company_money(tmp_path):
    from .test_period_and_row_changes import row_system

    system, _, account_id, first, _ = row_system(tmp_path, "platform")
    app, storage, company, money, _ = system
    first["data"]["rows"][0].update(
        memo="平台原文用途", reference="platform-reference", counterparty_text="平台原交易方"
    )
    actual = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [{"op": "replace", "expected_seq": 1, "fact": first}],
        }
    )
    assert actual["completion"] == "complete", actual
    assert actual["accounting"] == "unchanged"
    with storage.read_unit(company) as read:
        assert money.export_current([first["business_id"]], read) == (first,)
        selected = {
            "company_id": company,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "view": "current",
        }
        page = money.query_statement_rows(selected, StatementQuery(account_id), PageQuery(), read)
        assert (page.items[0].memo, page.items[0].reference, page.items[0].counterparty_text) == (
            "平台原文用途",
            "platform-reference",
            "平台原交易方",
        )
        assert (
            money.query_movements(selected, MovementQuery(), PageQuery(), read).filtered_count == 0
        )
