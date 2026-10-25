"""Scoped review identity captures and page payloads against real adopted resources."""

from dataclasses import replace
from uuid import uuid4

import pytest
from test_adoption import (
    BANK,
    BUSINESS,
    COMPANY,
    MOVEMENT,
    PAYMENT,
    SOURCE,
    SUPPLIER,
    _payment_at,
    adopt_declared,
    expense,
    seed_payment,
)
from test_adoption import store as store
from test_read_positions import corrected_paid
from test_resource_indexes import split_resources

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.ledger.public import (
    ReviewMovementQuery,
    ReviewObligationQuery,
    SettlementEventQuery,
    create_ledger,
)

REF = {"business_id": BUSINESS, "slot": "primary"}


def owing(ledger, read, **values):
    return ledger.capture_review_obligations(
        ReviewObligationQuery("2026-02", references=(REF,), **values), read
    )


def test_capture_501_slots_reads_no_payload_and_page_only_resolves_selected_resources(
    store, monkeypatch
):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [split_resources(501)[0]], unit)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        for name in ("results", "children", "read_event_payloads"):
            monkeypatch.setattr(repo, name, lambda *a, **k: pytest.fail("捕获读取了正文"))
        selected = ledger.capture_review_obligations(
            ReviewObligationQuery("2026-01", business_ids=(BUSINESS,)), read
        )
        monkeypatch.setattr(
            repo, "read_position_rows", lambda *a, **k: pytest.fail("分页重复汇总清偿")
        )
        first = ledger.review_obligation_page(selected, PageQuery(1), read)
        second = ledger.review_obligation_page(
            selected, PageQuery(2, first.next_cursor, selected.version), read
        )
    assert selected.total_count == 501
    assert first.total_count == second.total_count == 501
    assert len(first.items) == 1 and len(second.items) == 2
    assert first.items[0].definition.reference != second.items[0].definition.reference


def test_obligation_filters_use_obligation_period_not_origin_business_month(store):
    ledger = create_ledger()
    changed = split_resources(2)[0]
    changed["plans"][0]["obligations"][1].update(category="social", business_period="2026-02")
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [changed], unit)
    with store.read_unit(COMPANY) as read:
        selected = ledger.capture_review_obligations(
            ReviewObligationQuery(
                "2026-02",
                categories=("social",),
                party_ids=(SUPPLIER,),
                direction="payable",
                period_from="2026-02",
                period_to="2026-02",
            ),
            read,
        )
        wrong_month = ledger.capture_review_obligations(
            replace(selected.query[0], period_to="2026-01", period_from=None), read
        )
    assert selected.total_count == 1
    assert selected.items[0].business_period == "2026-02"
    assert selected.sources[0].reference["business_period"] == "2026-01"
    assert wrong_month.total_count == 0


def test_partial_and_settled_scopes_keep_exact_integer_amounts(store):
    ledger = create_ledger()
    seed_payment(store)
    value = 2**53 + 1
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(str(value)), _payment_at("1")], unit)
    with store.read_unit(COMPANY) as read:
        all_items = owing(ledger, read)
        opened = owing(ledger, read, status="unsettled")
        settled = owing(ledger, read, status="settled")
    assert all_items.items[0].amounts.remaining_fen == 2**53
    assert all_items.items == opened.items
    assert settled.total_count == 0


def test_complete_settlement_changes_capture_version_and_old_cursor(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.read_unit(COMPANY) as read:
        before = owing(ledger, read)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [_payment_at()], unit)
    with store.read_unit(COMPANY) as read:
        after = owing(ledger, read)
        assert after.version != before.version
        with pytest.raises(ContractError) as error:
            ledger.review_obligation_page(after, PageQuery(expected_version=before.version), read)
        assert error.value.code == "snapshot_changed"
        assert owing(ledger, read, status="settled").total_count == 1
        assert owing(ledger, read, status="unsettled").total_count == 0


def test_replaced_payment_same_total_still_changes_related_basis(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(), _payment_at(day="05")], unit)
    with store.read_unit(COMPANY) as read:
        before = owing(ledger, read)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [_payment_at(day="06", seq=1)], unit)
    with store.read_unit(COMPANY) as read:
        after = owing(ledger, read)
    assert before.items[0].amounts == after.items[0].amounts
    assert before.version != after.version


def test_unrelated_business_and_history_do_not_invalidate_scoped_basis(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.read_unit(COMPANY) as read:
        before = owing(ledger, read)
    unrelated_id = str(uuid4())
    unrelated = expense("123", business_id=unrelated_id)
    unrelated["plans"][0]["obligations"][0]["category"] = "reimbursement"
    with store.write_unit(COMPANY) as unit:
        unit.repository("application").business(unrelated_id)
        adopt_declared(ledger, [unrelated], unit)
    with store.read_unit(COMPANY) as read:
        after = owing(ledger, read)
        assert after.version == before.version
        page = ledger.review_obligation_page(
            after, PageQuery(expected_version=before.version), read
        )
        assert page.items[0].amounts.remaining_fen == 100000


def test_capture_events_unions_scopes_without_payloads_then_pages_actual_money(store, monkeypatch):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(), _payment_at("40000")], unit)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        original = repo.read_event_payloads
        monkeypatch.setattr(
            repo, "read_event_payloads", lambda *a: pytest.fail("捕获读取了事件正文")
        )
        selected = ledger.capture_review_settlements(
            (
                SettlementEventQuery("2026-01", "2026-02", source_refs=(REF,)),
                SettlementEventQuery("2026-01", "2026-02", consumer_business_ids=(PAYMENT,)),
            ),
            read,
        )
        assert selected.total_count == 1
        assert selected.source_refs == (REF,)
        assert selected.movement_ids == (MOVEMENT,)
        monkeypatch.setattr(repo, "read_event_payloads", original)
        page = ledger.review_settlement_page(selected, PageQuery(1), read)
    event = page.items[0]
    assert event.use.value["amount_fen"] == "40000"
    assert event.movement.value["movement_id"] == MOVEMENT
    assert event.movement.source == event.use.source


def test_excess_reclassification_uses_existing_signed_balance_and_has_no_fake_cash(store):
    ledger = corrected_paid(store)
    with store.read_unit(COMPANY) as read:
        selected = owing(ledger, read)
        events = ledger.capture_review_settlements(
            SettlementEventQuery("2026-01", "2026-02", source_refs=(REF,)), read
        )
        page = ledger.review_settlement_page(events, PageQuery(), read)
    assert selected.items[0].amounts.excess_reclassified_fen == 20000
    assert selected.items[0].amounts.remaining_fen == 0
    assert [item.use.component for item in page.items] == ["money", "reclassify_excess"]
    assert page.items[1].movement is None


def test_movement_capture_is_scoped_and_page_preserves_exact_allocations_source(store, monkeypatch):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(), _payment_at("40000")], unit)
    with store.read_unit(COMPANY) as read:
        repo = read.repository("ledger")
        monkeypatch.setattr(repo, "results", lambda *a: pytest.fail("资金捕获读取完整业务"))
        selected = ledger.capture_review_movements(
            ReviewMovementQuery("2026-01", "2026-02", account_id=BANK), read
        )
        by_business = ledger.capture_review_movements(
            ReviewMovementQuery("2026-01", "2026-02", business_ids=(PAYMENT,)), read
        )
        page = ledger.review_movement_page(selected, PageQuery(), read)
    assert selected.movement_ids == by_business.movement_ids == (MOVEMENT,)
    assert selected.items[0].amount_fen == 40000
    assert page.items[0].source == selected.items[0].source
    assert page.items[0].value["amount_fen"] == "40000"


def test_precise_review_sources_share_one_header_read_and_keep_each_location(store, monkeypatch):
    ledger = create_ledger()
    other_id, other_source = str(uuid4()), str(uuid4())
    first, second = expense(), expense("20000", business_id=other_id)
    first["plans"][0]["basis"]["source_locations"] = [{"source_id": SOURCE}]
    second["plans"][0]["basis"]["source_locations"] = [
        {"source_id": other_source, "location": "表1第2行"}
    ]
    with store.write_unit(COMPANY) as unit:
        unit.repository("application").business(other_id)
        unit.repository("records").source(other_source)
        adopt_declared(ledger, [first, second], unit)
    with store.read_unit(COMPANY) as read:
        selected = ledger.capture_review_obligations(
            ReviewObligationQuery("2026-02", business_ids=(BUSINESS, other_id)), read
        )
        repo = read.repository("ledger")
        monkeypatch.setattr(repo, "results", lambda *a: pytest.fail("来源定位加载完整业务"))
        original = repo.read_heads
        seen = []

        def read_heads(result_ids):
            seen.append(tuple(result_ids))
            return original(result_ids)

        monkeypatch.setattr(repo, "read_heads", read_heads)
        references = [item.source for item in reversed(selected.items)]
        sources = ledger.get_review_sources(references, read)
        assert seen == [tuple(source["result_id"] for source in references)]
        assert [item.source for item in sources] == references
        by_business = {item.reference["business_id"]: item for item in sources}
        assert by_business[BUSINESS].source_locations == ({"source_id": SOURCE},)
        assert by_business[other_id].source_locations == (
            {"source_id": other_source, "location": "表1第2行"},
        )
        assert by_business[BUSINESS].reference["business_period"] == "2026-01"
        with pytest.raises(ContractError) as error:
            ledger.get_review_sources(
                [dict(selected.items[0].source, result_generation=999)], read
            )
        assert error.value.code == "snapshot_changed"
        with pytest.raises(ContractError) as error:
            ledger.get_review_sources(
                [dict(selected.items[0].source, content_digest="0" * 64)], read
            )
        assert error.value.code == "snapshot_changed"


def test_union_keeps_criteria_membership_and_deduplicates_known_refs(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [split_resources(2)[0]], unit)
    scopes = (
        ReviewObligationQuery("2026-01", categories=("trade",), status="unsettled"),
        ReviewObligationQuery(
            "2026-01", references=({"business_id": BUSINESS, "slot": "payable:0"},)
        ),
    )
    with store.read_unit(COMPANY) as read:
        before = ledger.capture_review_obligations(scopes, read)
        reordered = ledger.capture_review_obligations(tuple(reversed(scopes)), read)
        assert before.version == reordered.version and before.total_count == 2
    revised = split_resources(2)[0]
    revised["businesses"][0].update(expected_seq=1, op="replace")
    revised["plans"][0]["obligations"][0]["category"] = "social"
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [revised], unit)
    with store.read_unit(COMPANY) as read:
        after = ledger.capture_review_obligations(scopes, read)
    # Exact refs keep both resources visible; the discovery membership still changed.
    assert after.references == before.references
    assert after.version != before.version


def test_withdrawn_payment_is_removed_from_events_funds_and_remaining_basis(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(), _payment_at("40000")], unit)
    with store.read_unit(COMPANY) as read:
        before = owing(ledger, read)
    withdrawal = {
        "domain": "money",
        "publications": [],
        "retained_results": [],
        "plans": [],
        "dependencies": [],
        "issues": [],
        "businesses": [
            {
                "business_id": PAYMENT,
                "expected_seq": 1,
                "op": "withdraw",
                "natural_key": None,
                "complete_result_slots": [],
            }
        ],
    }
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [withdrawal], unit)
    with store.read_unit(COMPANY) as read:
        after = owing(ledger, read)
        events = ledger.capture_review_settlements(
            SettlementEventQuery("2026-01", "2026-02", source_refs=(REF,)), read
        )
        funds = ledger.capture_review_movements(
            ReviewMovementQuery("2026-01", "2026-02", movement_ids=(MOVEMENT,)), read
        )
    assert after.version != before.version
    assert after.items[0].amounts.remaining_fen == 100000
    assert events.total_count == funds.total_count == 0


def test_transfer_is_in_balance_and_event_basis_without_expanding_target_history(store):
    ledger = create_ledger()
    recipient_id = str(uuid4())
    source = expense()
    target = expense("30000", business_id=recipient_id)
    target["plans"][0]["obligation_reclassifications"] = [
        {
            "relation_id": str(uuid4()),
            "owner_business_id": recipient_id,
            "source": REF,
            "target": {"business_id": recipient_id, "slot": "primary"},
            "amount_fen": "30000",
            "mode": "transfer",
        }
    ]
    with store.write_unit(COMPANY) as unit:
        unit.repository("application").business(recipient_id)
        adopt_declared(ledger, [source, target], unit)
    with store.read_unit(COMPANY) as read:
        actual = owing(ledger, read)
        events = ledger.capture_review_settlements(
            SettlementEventQuery("2026-01", "2026-02", source_refs=(REF,)), read
        )
        page = ledger.review_settlement_page(events, PageQuery(), read)
    assert actual.references == (REF,)
    assert actual.items[0].amounts.transferred_out_fen == 30000
    assert actual.items[0].amounts.remaining_fen == 70000
    assert page.items[0].use.component == "transfer" and page.items[0].movement is None


def test_empty_page_never_turns_into_an_unbounded_definition_read(store, monkeypatch):
    ledger = create_ledger()
    with store.read_unit(COMPANY) as read:
        selected = ledger.capture_review_obligations(
            ReviewObligationQuery("2026-09", categories=("social",)), read
        )
        monkeypatch.setattr(
            read.repository("ledger"),
            "read_review_obligation_definitions",
            lambda *a: pytest.fail("空页读取了定义"),
        )
        page = ledger.review_obligation_page(selected, PageQuery(), read)
    assert page.items == () and page.total_count == 0 and page.next_cursor is None


def test_movement_union_deduplicates_selection_and_preserves_range_membership(store):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense(), _payment_at()], unit)
    scopes = (
        ReviewMovementQuery("2026-01", "2026-02", account_id=BANK),
        ReviewMovementQuery("2026-01", "2026-03", movement_ids=(MOVEMENT,)),
    )
    with store.read_unit(COMPANY) as read:
        first = ledger.capture_review_movements(scopes, read)
        second = ledger.capture_review_movements(tuple(reversed(scopes)), read)
        page = ledger.review_movement_page(first, PageQuery(), read)
    assert first.version == second.version
    assert first.period_from == "2026-01" and first.period_to == "2026-03"
    assert first.total_count == len(page.items) == 1


def test_obligation_merge_reuses_balances_and_accepts_joint_capture_cursor(store, monkeypatch):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, split_resources(3), unit)
    refs = tuple({"business_id": BUSINESS, "slot": f"payable:{index}"} for index in range(3))
    queries = (
        ReviewObligationQuery("2026-02", references=refs[:2]),
        ReviewObligationQuery("2026-02", references=refs[1:]),
    )
    with store.read_unit(COMPANY) as read:
        direct = ledger.capture_review_obligations(queries, read)
        parts = [ledger.capture_review_obligations(query, read) for query in queries]
        repo = read.repository("ledger")
        for name in ("read_review_obligation_rows", "read_event_identities"):
            monkeypatch.setattr(repo, name, lambda *a, **k: pytest.fail("合并重新读取了余额或清偿"))
        merged = ledger.merge_review_obligations([parts[1], parts[0], parts[1]], read)
        repeated = ledger.merge_review_obligations([merged, parts[0]], read)
        first = ledger.review_obligation_page(direct, PageQuery(1), read)
        rest = ledger.review_obligation_page(
            repeated, PageQuery(2, first.next_cursor, direct.version), read
        )
    assert merged.version == repeated.version == direct.version
    assert merged.items == direct.items and merged.sources == direct.sources
    assert merged.total_count == merged.settlements.total_count == 3
    assert first.items[0].amounts.remaining_fen == 0
    assert [item.definition.reference for item in rest.items] == list(refs[1:])


def test_movement_merge_reuses_headers_and_pages_joint_scope_without_rereading(store, monkeypatch):
    ledger = create_ledger()
    seed_payment(store)
    other, movement = str(uuid4()), str(uuid4())
    with store.write_unit(COMPANY) as unit:
        unit.repository("application").business(other, family="money", kind="money.movement")
        unit.repository("money").session.execute(
            "INSERT INTO money_occurrence VALUES(?,?,?)", (movement, other, "funds")
        )
        adopt_declared(
            ledger,
            [
                expense(),
                _payment_at("30000"),
                _payment_at("20000", day="06", business_id=other, movement_id=movement),
            ],
            unit,
        )
    queries = (
        ReviewMovementQuery("2026-01", "2026-02", movement_ids=(MOVEMENT,)),
        ReviewMovementQuery("2026-01", "2026-03", movement_ids=(MOVEMENT, movement)),
    )
    with store.read_unit(COMPANY) as read:
        direct = ledger.capture_review_movements(queries, read)
        parts = [ledger.capture_review_movements(query, read) for query in queries]
        monkeypatch.setattr(
            read.repository("ledger"),
            "read_review_movement_rows",
            lambda *a: pytest.fail("合并重新读取了资金头"),
        )
        merged = ledger.merge_review_movements([parts[1], parts[0], parts[1]], read)
        first = ledger.review_movement_page(direct, PageQuery(1), read)
        second = ledger.review_movement_page(
            merged, PageQuery(1, first.next_cursor, direct.version), read
        )
    assert merged.version == direct.version and merged.items == direct.items
    assert merged.period_from == "2026-01" and merged.period_to == "2026-03"
    assert merged.total_count == 2
    assert first.items[0].value["movement_id"] == MOVEMENT
    assert second.items[0].value["movement_id"] == movement


def test_settlement_merge_preserves_source_and_consumer_scope_without_rereading(store, monkeypatch):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, split_resources(3), unit)
    queries = (
        SettlementEventQuery("2026-01", "2026-02", consumer_business_ids=(PAYMENT,)),
        SettlementEventQuery(
            "2026-01", "2026-02", source_refs=({"business_id": BUSINESS, "slot": "payable:0"},)
        ),
    )
    with store.read_unit(COMPANY) as read:
        direct = ledger.capture_review_settlements(queries, read)
        parts = [ledger.capture_review_settlements(query, read) for query in queries]
        monkeypatch.setattr(
            read.repository("ledger"),
            "read_event_identities",
            lambda *a: pytest.fail("合并重新读取了清偿头"),
        )
        merged = ledger.merge_review_settlements([parts[1], parts[0], parts[1]], read)
        first = ledger.review_settlement_page(direct, PageQuery(1), read)
        rest = ledger.review_settlement_page(
            merged, PageQuery(2, first.next_cursor, direct.version), read
        )
    assert merged.version == direct.version and merged.items == direct.items
    assert merged.total_count == 3 and len(rest.items) == 2
    assert {item.movement.value["movement_id"] for item in rest.items} == {MOVEMENT}


def test_obligation_merge_retains_initially_empty_discovery_scope(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    criteria = ReviewObligationQuery("2026-02", categories=("social",))
    exact = ReviewObligationQuery("2026-02", references=(REF,))

    def capture(read):
        return ledger.merge_review_obligations(
            [ledger.capture_review_obligations(query, read) for query in (criteria, exact)], read
        )

    with store.read_unit(COMPANY) as read:
        before = capture(read)
    new_id = str(uuid4())
    new = expense("20000", business_id=new_id)
    new["plans"][0]["obligations"][0]["category"] = "social"
    with store.write_unit(COMPANY) as unit:
        unit.repository("application").business(new_id)
        adopt_declared(ledger, [new], unit)
    with store.read_unit(COMPANY) as read:
        after = capture(read)
        assert after.version == ledger.capture_review_obligations((criteria, exact), read).version
    assert before.total_count == 1 and after.total_count == 2
    assert before.version != after.version


def test_obligation_merge_cannot_mix_economic_cutoffs(store):
    ledger = create_ledger()
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.read_unit(COMPANY) as read:
        selections = [
            ledger.capture_review_obligations(ReviewObligationQuery(month, references=(REF,)), read)
            for month in ("2026-01", "2026-02")
        ]
        with pytest.raises(ContractError) as error:
            ledger.merge_review_obligations(selections, read)
        assert error.value.code == "invalid_input"


@pytest.mark.parametrize("collection", ["obligations", "settlements", "movements"])
def test_merge_rejects_captures_from_an_earlier_read_snapshot(store, collection):
    ledger = create_ledger()
    query = {
        "obligations": ReviewObligationQuery("2026-02", references=(REF,)),
        "settlements": SettlementEventQuery("2026-01", "2026-02", source_refs=(REF,)),
        "movements": ReviewMovementQuery("2026-01", "2026-02", movement_ids=(MOVEMENT,)),
    }[collection]
    capture = getattr(ledger, f"capture_review_{collection}")
    merge = getattr(ledger, f"merge_review_{collection}")
    with store.read_unit(COMPANY) as read:
        before = capture(query, read)
    with store.write_unit(COMPANY) as unit:
        adopt_declared(ledger, [expense()], unit)
    with store.read_unit(COMPANY) as read:
        current = capture(query, read)
        with pytest.raises(ContractError) as error:
            merge([before, current], read)
        assert error.value.code == "snapshot_changed"


@pytest.mark.parametrize(
    "query",
    [
        ReviewObligationQuery("2026-01"),
        ReviewObligationQuery("2026-01", categories=("fake",)),
        ReviewObligationQuery("2026-01", categories=("social",), status="wrong"),
        ReviewObligationQuery(
            "2026-01", categories=("social",), period_from="2026-02", period_to="2026-01"
        ),
    ],
)
def test_invalid_or_unbounded_obligation_scope_is_rejected(store, query):
    with store.read_unit(COMPANY) as read:
        with pytest.raises(ContractError):
            create_ledger().capture_review_obligations(query, read)
