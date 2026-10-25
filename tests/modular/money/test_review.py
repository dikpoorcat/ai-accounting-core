"""Narrow review keeps original evidence, proven links and true cash distinct."""

from copy import deepcopy
from json import dumps
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    loads_json,
)
from ai_accounting_modular.ledger.public import ReviewMovementQuery
from ai_accounting_modular.money.public import ReviewRowQuery, stable_occurrence_id
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, CASH, PLATFORM, fact, payment

from .test_read_activity import apply, bank_pair, funding, initialize


def test_movement_selection_is_account_and_actual_period_scoped(case, monkeypatch):
    initialize(case)
    first = funding("100")
    other = funding("200")
    other["data"]["funds_account_id"] = CASH
    apply(case, first, other)

    def forbidden(*args, **kwargs):
        raise AssertionError("narrow review must not call the company-wide movement reader")

    monkeypatch.setattr(case.kernel.ledger, "fund_movements", forbidden)
    query = ReviewMovementQuery("2026-01", "2026-01", account_id=BANK)
    with case.kernel.storage.read_unit(case.company) as read:
        selected = case.kernel.money.capture_review_movements(query, read)
        page = case.kernel.money.review_movement_page(selected, PageQuery(), read)
        assert selected.total_count == page.total_count == page.filtered_count == 1
        assert page.items[0]["movement"]["amount_fen"] == "100"
        assert page.items[0]["reference"]["business_id"] == first["business_id"]
        before = selected.version
    unrelated = funding("300")
    unrelated["data"]["funds_account_id"] = CASH
    apply(case, unrelated)
    with case.kernel.storage.read_unit(case.company) as read:
        selected = case.kernel.money.capture_review_movements(query, read)
        assert selected.version == before
        case.kernel.money.review_movement_page(selected, PageQuery(expected_version=before), read)


def test_real_movement_keeps_whole_amount_and_each_adopted_allocation(case):
    initialize(case)
    first, second = case.expense("100000"), case.expense("200000")
    paid = payment(first, 30000, slot="primary", party=case.party, day="2026-02-02")
    paid["data"]["allocations"] = [
        {"source": {"business_id": first["business_id"], "slot": "primary"}, "amount_fen": "10000"},
        {
            "source": {"business_id": second["business_id"], "slot": "primary"},
            "amount_fen": "20000",
        },
    ]
    apply(case, funding("30000"), first, second, paid)
    with case.kernel.storage.read_unit(case.company) as read:
        selected = case.kernel.money.capture_review_movements(
            ReviewMovementQuery("2026-02", "2026-02", business_ids=(paid["business_id"],)), read
        )
        actual = case.kernel.money.review_movement_page(selected, PageQuery(), read).items[0]
    assert actual["movement"]["amount_fen"] == "30000"
    assert {part.source["business_id"]: part.amount_fen for part in actual["allocations"]} == {
        first["business_id"]: 10000,
        second["business_id"]: 20000,
    }
    assert all(part.party_id == case.party for part in actual["allocations"])


def test_movement_selections_merge_without_losing_money_owner_allocations(case):
    initialize(case)
    funded, debt = funding("100000"), case.expense("100000")
    paid = payment(debt, 100000, slot="primary", party=case.party, day="2026-01-10")
    apply(case, funded, debt, paid)
    with case.kernel.storage.read_unit(case.company) as read:
        parts = [
            case.kernel.money.capture_review_movements(
                ReviewMovementQuery("2026-01", "2026-01", business_ids=(item["business_id"],)),
                read,
            )
            for item in (funded, paid)
        ]
        selected = case.kernel.money.merge_review_movements([parts[1], parts[0], parts[1]], read)
        page = case.kernel.money.review_movement_page(selected, PageQuery(), read)
    assert selected.total_count == page.total_count == 2
    by_business = {item["reference"]["business_id"]: item for item in page.items}
    allocation = by_business[paid["business_id"]]["allocations"][0]
    assert allocation.source["business_id"] == debt["business_id"]
    assert allocation.amount_fen == 100000


def two_rows(case, funded):
    statement, matched = bank_pair(case, funded)
    original = statement["data"]["rows"][0]
    original["amount_fen"] = "50"
    second = deepcopy(original)
    second.update(
        row_id=str(uuid4()),
        reference="synthetic-second",
        source={"source_id": case.source_id, "location": "second source row"},
    )
    statement["data"]["rows"].append(second)
    matched["data"]["matches"][0]["statement_row_ids"].append(second["row_id"])
    return statement, matched


def test_row_directory_is_complete_and_bodies_are_read_only_for_the_page(case, monkeypatch):
    initialize(case)
    funded = funding()
    statement, _ = two_rows(case, funded)
    _, statement = apply(case, funded, statement)
    with case.kernel.storage.read_unit(case.company) as read:
        repo = read.repository("money")
        original, calls = repo.review_rows, []

        def bodies(ids, kind):
            calls.append((ids, kind))
            return original(ids, kind)

        def forbidden(*args, **kwargs):
            raise AssertionError("row review must not reconstruct full statement facts")

        monkeypatch.setattr(repo, "review_rows", bodies)
        monkeypatch.setattr(repo, "get", forbidden)
        selected = case.kernel.money.capture_review_rows(
            ReviewRowQuery(account_id=BANK, period_from="2026-01", period_to="2026-01"), read
        )
        assert selected.total_count == 2 and calls == []
        assert selected.links == ()  # No formal matching says nothing about unrecorded cash.
        first = case.kernel.money.review_row_page(selected, PageQuery(1), read)
        assert (first.total_count, first.filtered_count, len(first.items)) == (2, 2, 1)
        assert calls == [((first.items[0].row_id,), "bank")]
        assert first.items[0].match_group_id is None
        second = case.kernel.money.review_row_page(selected, PageQuery(1, first.next_cursor), read)
        assert second.next_cursor is None
        assert second.items[0].row_id != first.items[0].row_id
        actual = case.kernel.money.capture_review_movements(
            ReviewMovementQuery("2026-01", "2026-01", account_id=BANK), read
        )
        assert actual.total_count == 1  # Payment/funding already exists without reconciliation.


def test_row_links_are_proven_group_members_not_same_amount_candidates(case):
    initialize(case)
    funded = funding()
    statement, matched = two_rows(case, funded)
    apply(case, funded, statement, matched)
    row_id = statement["data"]["rows"][0]["row_id"]
    movement_id = stable_occurrence_id(funded["business_id"], "funds")
    with case.kernel.storage.read_unit(case.company) as read:
        selected = case.kernel.money.capture_review_rows(
            ReviewRowQuery(statement_ids=(statement["business_id"],), row_ids=(row_id,)), read
        )
        assert selected.total_count == 1 and len(selected.links) == 1
        linked = case.kernel.money.review_row_link_page(selected, PageQuery(), read).items[0]
        assert linked.relation == "reconciliation"
        assert linked.movement_ids == (movement_id,)
        assert linked.linked_business_ids == (funded["business_id"],)
        assert linked.reconciliation["business_id"] == matched["business_id"]
        row = case.kernel.money.review_row_page(selected, PageQuery(), read).items[0]
        assert row.match_group_id == linked.group_id


def test_platform_links_include_actual_cash_owned_by_another_domain(case):
    initialize(case)
    row_id = str(uuid4())
    original = fact(
        "money.platform_activity",
        {
            "platform_account_id": PLATFORM,
            "rows": [
                {
                    "row_id": row_id,
                    "source": {"source_id": case.source_id},
                    "actual_on": "2026-01-02",
                    "direction": "inflow",
                    "amount_fen": "100",
                }
            ],
        },
    )
    funded = funding()
    funded["data"].update(funds_account_id=PLATFORM, platform_row_ids=[row_id])
    apply(case, original, funded)
    with case.kernel.storage.read_unit(case.company) as read:
        selected = case.kernel.money.capture_review_rows(
            ReviewRowQuery(statement_ids=(original["business_id"],), row_ids=(row_id,)), read
        )
        assert len(selected.links) == 1
        assert selected.links[0].relation == "platform_consumption"
        assert selected.links[0].linked_business_ids == (funded["business_id"],)
        assert selected.links[0].movement_ids == (
            stable_occurrence_id(funded["business_id"], "funds"),
        )


def internal_pair(case, account_id, period):
    row_id = str(uuid4())
    original = fact(
        "money.platform_activity",
        {
            "platform_account_id": account_id,
            "rows": [
                {
                    "row_id": row_id,
                    "source": {"source_id": case.source_id},
                    "actual_on": period + "-02",
                    "direction": "inflow",
                    "amount_fen": "100",
                }
            ],
        },
        period=period,
    )
    internal = fact(
        "money.reserve_internal",
        {
            "platform_account_id": account_id,
            "boundary_source": {"source_id": case.source_id},
            "platform_row_ids": [row_id],
        },
        period=period,
    )
    return original, internal


def test_internal_platform_links_have_no_payment_and_ignore_other_month_bodies(case):
    initialize(case)
    internals = []
    for period in ("2026-01", "2026-02"):
        original, internal = internal_pair(case, PLATFORM, period)
        internals.append(internal)
        apply(case, original, internal)
    # A damaged unrelated historical body must not be loaded merely to inspect February.
    with case.kernel.storage.write_unit(case.company) as unit:
        unit.repository("money")._session.execute(
            "UPDATE money_fact SET data_json=json_set(data_json,'$.data.platform_row_ids','bad') "
            "WHERE business_id=?",
            (internals[0]["business_id"],),
        )
    with case.kernel.storage.read_unit(case.company) as read:
        selected = case.kernel.money.capture_review_rows(
            ReviewRowQuery(account_id=PLATFORM, period_from="2026-02", period_to="2026-02"),
            read,
        )
        assert selected.total_count == 1
        assert selected.links[0].linked_business_ids == (internals[1]["business_id"],)
        assert selected.links[0].movement_ids == ()
        assert (
            case.kernel.money.capture_review_movements(
                ReviewMovementQuery("2026-02", "2026-02", account_id=PLATFORM), read
            ).total_count
            == 0
        )


def test_platform_internal_review_never_enumerates_other_accounts(case, monkeypatch):
    initialize(case)
    original, internal = internal_pair(case, PLATFORM, "2026-01")
    apply(case, original, internal)
    query = ReviewRowQuery(account_id=PLATFORM, period_from="2026-01", period_to="2026-01")
    with case.kernel.storage.read_unit(case.company) as read:
        before = case.kernel.money.capture_review_rows(query, read)

    other_account = str(uuid4())
    other, other_internal = internal_pair(case, other_account, "2026-01")
    apply(
        case,
        other,
        other_internal,
        objects=[
            {
                "object_id": other_account,
                "kind": "platform_account",
                "name": "另一合成平台账户",
                "expected_seq": 0,
                "data": {
                    "platform": "另一合成平台",
                    "account_key": "other",
                    "owner": "company",
                    "currency": "CNY",
                },
            }
        ],
    )
    requested = []
    get_refs = case.kernel.ledger.get_result_refs

    def narrow_refs(ids, read, **kwargs):
        requested.extend(ids)
        assert other["business_id"] not in ids and other_internal["business_id"] not in ids
        return get_refs(ids, read, **kwargs)

    def forbid_company_directory(*args, **kwargs):
        raise AssertionError("platform row review must not enumerate the company's internal stock")

    monkeypatch.setattr(case.kernel.ledger, "get_result_refs", narrow_refs)
    monkeypatch.setattr(case.kernel.ledger, "query_adopted_directory", forbid_company_directory)
    with case.kernel.storage.read_unit(case.company) as read:
        after = case.kernel.money.capture_review_rows(query, read)
        assert after.version == before.version
        assert after.links[0].linked_business_ids == (internal["business_id"],)
    assert set(requested) == {original["business_id"], internal["business_id"]}


def test_original_text_change_invalidates_only_the_selected_scope(case):
    initialize(case)
    funded = funding()
    statement, _ = bank_pair(case, funded)
    _, statement = apply(case, funded, statement)
    query = ReviewRowQuery(statement_ids=(statement["business_id"],))
    with case.kernel.storage.read_unit(case.company) as read:
        selected = case.kernel.money.capture_review_rows(query, read)
        before = selected.version
    unrelated = funding("200", day="2026-02-02")
    unrelated["period"] = "2026-02"
    apply(case, unrelated)
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.kernel.money.capture_review_rows(query, read).version == before
        with pytest.raises(ContractError, match="当前读取快照"):
            case.kernel.money.review_row_page(selected, PageQuery(), read)
    statement["data"]["rows"][0]["memo"] = "更清楚的真实原文"
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": statement}])
    with case.kernel.storage.read_unit(case.company) as read:
        selected = case.kernel.money.capture_review_rows(query, read)
        assert selected.version != before
        with pytest.raises(ContractError) as caught:
            case.kernel.money.review_row_page(selected, PageQuery(expected_version=before), read)
        assert caught.value.code == "snapshot_changed"
        assert (
            case.kernel.money.review_row_page(selected, PageQuery(), read).items[0].memo
            == "更清楚的真实原文"
        )


@pytest.mark.parametrize(
    "damage", ["duplicate_evidence", "missing_opening", "extra_data", "missing_digest"]
)
def test_original_parent_projection_uses_installed_field_contracts(case, damage):
    initialize(case)
    statement, _ = bank_pair(case, funding())
    apply(case, statement)
    with case.kernel.storage.write_unit(case.company) as unit:
        session = unit.repository("money")._session
        row = session.fetchone(
            "SELECT data_json FROM money_fact WHERE business_id=?", (statement["business_id"],)
        )
        parent = loads_json(row["data_json"])
        if damage == "duplicate_evidence":
            parent["evidence_ids"] *= 2
        elif damage == "missing_opening":
            del parent["data"]["opening_fen"]
        elif damage == "extra_data":
            parent["data"]["unexpected"] = "not a declared property"
        else:
            del parent["collection_digest"]
        session.execute(
            "UPDATE money_fact SET data_json=? WHERE business_id=?",
            (dumps(parent), statement["business_id"]),
        )
    with (
        case.kernel.storage.read_unit(case.company) as read,
        pytest.raises(ContractError) as caught,
    ):
        case.kernel.money.capture_review_rows(
            ReviewRowQuery(statement_ids=(statement["business_id"],)), read
        )
    assert caught.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"account_id": BANK},
        {"period_from": "2026-01", "period_to": "2026-01"},
        {"statement_ids": (BANK, BANK)},
        {"statement_ids": [BANK]},
        {"statement_ids": (BANK,), "period_from": "2026-01"},
        {"statement_ids": (BANK,), "kind": "other"},
    ],
)
def test_review_rows_requires_an_explicit_valid_narrow_scope(fields):
    with pytest.raises(ContractError) as caught:
        ReviewRowQuery(**fields)
    assert caught.value.code == "invalid_input"
