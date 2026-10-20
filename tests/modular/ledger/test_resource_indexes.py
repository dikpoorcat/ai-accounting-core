"""Resource work scales with source rows, without leaking across replacements."""

from collections import Counter
from copy import deepcopy
from uuid import uuid4

import pytest
from test_adoption import (
    BANK,
    BUSINESS,
    COMPANY,
    PAYMENT,
    SUPPLIER,
    _candidate,
    adopt_declared,
    expense,
    payment,
    seed_payment,
)
from test_adoption import store as store

from ai_accounting_modular.contracts.public import ContractError, ResourceCutoff
from ai_accounting_modular.ledger import _resource_view
from ai_accounting_modular.ledger._repository import LedgerRepository
from ai_accounting_modular.ledger.public import create_ledger


def split_resources(count=31):
    """Independent legal ledger rows; no domain calculator in this indexing test."""
    source, consumer = expense(str(count * 100)), payment()
    obligation = source["plans"][0]["obligations"][0]
    source["plans"][0]["obligations"] = [
        {
            **obligation,
            "slot": f"payable:{index}",
            "amount_fen": "100",
            "cashflow_weights": [{"category": "other_operating_payments", "weight": "1"}],
        }
        for index in range(count)
    ]
    source["plans"][0]["cost_lots"] = [
        {
            "business_id": BUSINESS,
            "slot": f"cost:{index}",
            "object_id": SUPPLIER,
            "category": "money_fund",
            "amount_fen": "100",
            "business_period": "2026-01",
        }
        for index in range(count)
    ]
    plan = consumer["plans"][0]
    amount = str(count * 100)
    fact = consumer["businesses"][0]["fact"]
    fact["data"]["amount_fen"] = amount
    fact["data"]["allocations"] = [
        {"source": {"business_id": BUSINESS, "slot": f"payable:{index}"}, "amount_fen": "100"}
        for index in range(count)
    ]
    plan["basis"]["input"] = deepcopy(fact["data"])
    plan["target"]["values"].update(amount_fen=amount, settled_fen=amount)
    for line in plan["lines"]:
        for side in ("debit_fen", "credit_fen"):
            if line[side] != "0":
                line[side] = amount
    plan["fund_movements"][0]["amount_fen"] = amount
    plan["fund_movements"][0]["cashflow_parts"][0]["amount_fen"] = amount
    settlement = plan["settlements"][0]
    plan["settlements"] = [
        {
            **settlement,
            "settlement_id": str(uuid4()),
            "source": {"business_id": BUSINESS, "slot": f"payable:{index}"},
            "consumer": {"business_id": PAYMENT, "slot": f"paid:{index}"},
            "amount_fen": "100",
        }
        for index in range(count)
    ]
    plan["cost_consumptions"] = [
        {
            "source": {"business_id": BUSINESS, "slot": f"cost:{index}"},
            "consumer": {"business_id": PAYMENT, "slot": f"used:{index}"},
            "period": "2026-01",
            "amount_fen": "40",
        }
        for index in range(count)
    ]
    return source, consumer


@pytest.mark.parametrize("persisted", [False, True])
def test_many_slots_parse_each_definition_once_and_load_incoming_once(
    store, monkeypatch, persisted
):
    ledger, count = create_ledger(), 31
    seed_payment(store)
    changes = split_resources(count)
    if persisted:
        with store.write_unit(COMPANY) as uow:
            adopt_declared(ledger, changes, uow)
    converted, incoming = Counter(), Counter()
    convert, read_incoming = _resource_view.resource_definition, LedgerRepository.incoming

    def counted_definition(kind, *args):
        converted[kind] += 1
        return convert(kind, *args)

    def counted_incoming(repo, table, ids, **kwargs):
        incoming[table] += 1
        return read_incoming(repo, table, ids, **kwargs)

    monkeypatch.setattr(_resource_view, "resource_definition", counted_definition)
    monkeypatch.setattr(LedgerRepository, "incoming", counted_incoming)
    with store.write_unit(COMPANY) as uow:
        candidate = (
            ledger.begin_candidate([], uow).complete()
            if persisted
            else _candidate(ledger, uow, changes, reverse=True)
        )
        for index in range(count):
            for kind, slot, remaining in (
                ("obligation", f"payable:{index}", 0),
                ("cost", f"cost:{index}", 60),
            ):
                ref = {"business_id": BUSINESS, "slot": slot}
                definition = candidate.resource_definitions([ref], kind)[0]
                actual = candidate.resource_states([ref], kind, ResourceCutoff("2026-01"))[0]
                assert actual.definition == definition
                assert actual.remaining_fen == remaining and len(actual.uses) == 1
                # Caller edits must never corrupt a later lookup from this snapshot.
                definition.value["amount_fen"] = "1"
                assert candidate.resource_definitions([ref], kind)[0].value["amount_fen"] == "100"
        assert converted["obligation"] == converted["cost"] == count
        assert incoming == {
            "settlement": 1,
            "obligation_reclassification": 1,
            "cost_consumption": 1,
        }


def test_formal_definition_view_reuses_parsed_source_within_one_snapshot(store, monkeypatch):
    ledger, count = create_ledger(), 31
    changes = split_resources(count)
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [changes[0]], uow)
    converted, fetches = Counter(), Counter()
    convert, children = _resource_view.resource_definition, LedgerRepository.children

    def counted_definition(kind, *args):
        converted[kind] += 1
        return convert(kind, *args)

    def counted_children(repo, table, ids):
        fetches[table] += 1
        return children(repo, table, ids)

    monkeypatch.setattr(_resource_view, "resource_definition", counted_definition)
    monkeypatch.setattr(LedgerRepository, "children", counted_children)
    for _ in range(2):
        with store.read_unit(COMPANY) as read:
            for index in range(count):
                definition = ledger.resource_definitions(
                    [{"business_id": BUSINESS, "slot": f"cost:{index}"}], "cost", read
                )[0]
                assert definition.value["amount_fen"] == "100"
    # Each independent read context owns a fresh view; neither does quadratic work.
    assert converted == {"cost": count * 2}
    assert fetches == {"cost_lot": 2}


def test_state_batch_loads_incoming_for_distinct_source_businesses_together(store, monkeypatch):
    ledger = create_ledger()
    business_ids = [BUSINESS, str(uuid4()), str(uuid4())]
    changes = []
    for business_id in business_ids:
        change = expense("100", business_id=business_id)
        change["plans"][0]["cost_lots"] = [
            {
                "business_id": business_id,
                "slot": "cost",
                "object_id": SUPPLIER,
                "category": "money_fund",
                "amount_fen": "100",
                "business_period": "2026-01",
            }
        ]
        changes.append(change)
    with store.write_unit(COMPANY) as uow:
        for business_id in business_ids[1:]:
            uow.repository("application").business(business_id)
        adopt_declared(ledger, changes, uow)
    calls, read_incoming = [], LedgerRepository.incoming

    def counted_incoming(repo, table, ids, **kwargs):
        calls.append((table, tuple(ids)))
        return read_incoming(repo, table, ids, **kwargs)

    monkeypatch.setattr(LedgerRepository, "incoming", counted_incoming)
    with store.write_unit(COMPANY) as uow:
        candidate = ledger.begin_candidate([], uow).complete()
        refs = [{"business_id": business_id, "slot": "cost"} for business_id in business_ids]
        states = candidate.resource_states(refs, "cost", ResourceCutoff("2026-01"))
        assert [state.remaining_fen for state in states] == [100, 100, 100]
        assert calls == [("cost_consumption", tuple(business_ids))]


@pytest.mark.parametrize("scope_first", [False, True])
def test_fund_scope_and_definition_share_exact_parsed_source_and_safe_copies(
    store, monkeypatch, scope_first
):
    ledger = create_ledger()
    seed_payment(store)
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense(), payment()], uow)
    converted, sources = [], []
    convert, source = _resource_view.resource_definition, _resource_view.stored_source_reference

    def counted_definition(kind, *args):
        converted.append(kind)
        return convert(kind, *args)

    def counted_source(row):
        sources.append(row["result_id"])
        return source(row)

    monkeypatch.setattr(_resource_view, "resource_definition", counted_definition)
    monkeypatch.setattr(_resource_view, "stored_source_reference", counted_source)
    with store.read_unit(COMPANY) as read:

        def definition():
            return ledger.resource_definitions(
                [{"business_id": PAYMENT, "slot": "funds"}], "funds", read
            )[0]

        def scope():
            return ledger.fund_movements(BANK, "2026-01", "2026-01", read)[0]

        first, second = (scope, definition) if scope_first else (definition, scope)
        one, two = first(), second()
        assert one == two
        one.value["amount_fen"] = "1"
        one.source["content_digest"] = "0" * 64
        assert first() == second() == two
        assert converted == ["funds"] and len(sources) == 1


def test_staging_another_result_invalidates_business_slot_map_and_duplicate_check(store):
    ledger, first = create_ledger(), expense()
    responsibility = deepcopy(first["businesses"][0])
    responsibility["complete_result_slots"] = ["main", "aux"]
    second = deepcopy(first["plans"][0])
    second["result_slot"] = second["target"]["result_slot"] = second["basis"]["result_slot"] = "aux"
    second["obligations"][0]["slot"] = "second"
    ref = {"business_id": BUSINESS, "slot": "primary"}
    with store.write_unit(COMPANY) as uow:
        candidate = ledger.begin_candidate(
            [responsibility],
            uow,
            publications=first["publications"]
            + [{**first["publications"][0], "result_slot": "aux"}],
        )
        candidate.stage(first["plans"][0])
        assert (
            candidate.resource_definitions([ref], "obligation")[0].value["amount_fen"] == "100000"
        )
        candidate.stage(second)
        second_ref = {"business_id": BUSINESS, "slot": "second"}
        assert len(candidate.resource_definitions([ref, second_ref], "obligation")) == 2
        candidate.complete()
    second["obligations"][0]["slot"] = "primary"
    with store.write_unit(COMPANY) as uow:
        candidate = ledger.begin_candidate(
            [responsibility],
            uow,
            publications=first["publications"]
            + [{**first["publications"][0], "result_slot": "aux"}],
        )
        candidate.stage(first["plans"][0])
        candidate.resource_definitions([ref], "obligation")
        candidate.stage(second)
        with pytest.raises(ContractError, match="公开资源槽不能重复"):
            candidate.complete()


def test_adoption_invalidates_existing_formal_and_candidate_views_in_same_unit(store):
    ledger = create_ledger()
    ref, cutoff = {"business_id": BUSINESS, "slot": "primary"}, ResourceCutoff("2026-01")
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense()], uow)
        old = ledger.resource_definitions([ref], "obligation", uow)[0]
        candidate = ledger.begin_candidate([], uow).complete()
        assert candidate.resource_states([ref], "obligation", cutoff)[0].remaining_fen == 100000
        changed = expense("80000", seq=1)
        changed["plans"][0]["basis"]["effective_on"] = "2026-01-20"
        changed["businesses"][0]["fact"]["effective_on"] = "2026-01-20"
        adopt_declared(ledger, [changed], uow)
        new = ledger.resource_definitions([ref], "obligation", uow)[0]
        assert new.value["amount_fen"] == "80000" and new.source != old.source
        actual = candidate.resource_states([ref], "obligation", cutoff)[0]
        assert actual.definition == new and actual.remaining_fen == 80000
        with pytest.raises(ContractError, match="尚未成立"):
            candidate.resource_states([ref], "obligation", ResourceCutoff("2026-01", "2026-01-10"))
        adopt_declared(
            ledger,
            [
                {
                    "domain": "operations",
                    "publications": [],
                    "businesses": [
                        {
                            "business_id": BUSINESS,
                            "expected_seq": 2,
                            "op": "withdraw",
                            "natural_key": None,
                            "complete_result_slots": [],
                        }
                    ],
                    "retained_results": [],
                    "plans": [],
                    "dependencies": [],
                    "issues": [],
                }
            ],
            uow,
        )
        with pytest.raises(ContractError, match="不存在"):
            ledger.resource_definitions([ref], "obligation", uow)
        with pytest.raises(ContractError, match="不存在"):
            candidate.resource_definitions([ref], "obligation")


def test_cached_candidate_cannot_outlive_its_original_work_unit(store):
    ledger = create_ledger()
    seed_payment(store)
    ref = {"business_id": BUSINESS, "slot": "primary"}
    cutoff = ResourceCutoff("2026-01")
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense(), payment()], uow)
        candidate = ledger.begin_candidate([], uow).complete()
        assert (
            candidate.resource_definitions([ref], "obligation")[0].value["amount_fen"] == "100000"
        )
        assert candidate.resource_states([ref], "obligation", cutoff)[0].remaining_fen == 0
        assert (
            candidate.fund_movements(BANK, "2026-01", "2026-01")[0].value["amount_fen"] == "100000"
        )

    # A later valid transaction changes the source. The old candidate must not
    # serve its warmed caches after the transaction that defined them has ended.
    with store.write_unit(COMPANY) as uow:
        adopt_declared(ledger, [expense("120000", seq=1)], uow)
        current = ledger.begin_candidate([], uow).complete()
        assert current.resource_definitions([ref], "obligation")[0].value["amount_fen"] == "120000"
        assert current.resource_states([ref], "obligation", cutoff)[0].remaining_fen == 20000

    operations = (
        lambda: candidate.resource_definitions([ref], "obligation"),
        lambda: candidate.resource_states([ref], "obligation", cutoff),
        lambda: candidate.fund_movements(BANK, "2026-01", "2026-01"),
        lambda: candidate.stage(expense("120000", seq=1)["plans"][0]),
        candidate.complete,
    )
    for operation in operations:
        with pytest.raises(ContractError, match="工作单元已结束") as rejected:
            operation()
        assert rejected.value.code == "invalid_operation"
