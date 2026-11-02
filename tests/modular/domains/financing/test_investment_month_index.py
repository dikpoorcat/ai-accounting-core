"""Month routing stays linear even when every event belongs to a different month."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from ai_accounting_modular.contracts.public import YearMonth, validate_contract
from ai_accounting_modular.domains.financing import _investment_position
from ai_accounting_modular.domains.financing._investment_candidate import (
    Prefix,
    ReplayResult,
    Snapshot,
)
from ai_accounting_modular.domains.financing._investment_description import dependencies
from ai_accounting_modular.domains.financing._investment_description_inputs import DescriptionInputs

from ._investment_support import terms
from ._support import SOURCE


class CountedFacts:
    def __init__(self, values):
        self.values, self.visits = values, 0

    def __iter__(self):
        for value in self.values:
            self.visits += 1
            yield value

    def __len__(self):
        return len(self.values)


def _months_and_events(count):
    months = tuple(
        str(YearMonth.from_ordinal(YearMonth("2026-01").ordinal + offset))
        for offset in range(count)
    )
    facts = CountedFacts(
        tuple(
            {"business_id": str(UUID(int=5000 + offset)), "period": month}
            for offset, month in enumerate(months)
        )
    )
    return months, facts


def _source(reference):
    return {
        "business_id": reference["business_id"],
        "result_id": str(UUID(int=9000)),
        "result_slot": reference["slot"],
        "result_generation": 1,
        "content_digest": "1" * 64,
    }


class IndexedDescription(DescriptionInputs):
    """Exercise the production month router with an independently counted input."""

    def __init__(self, context, facts):
        super().__init__(context)
        self.facts = facts

    def holding(self, terms_id):
        return self.facts, ()

    def investment_seed(self, terms_id):
        return None


@pytest.mark.parametrize("count", [50, 100, 200])
def test_description_routes_months_with_one_input_scan(count):
    months, events = _months_and_events(count)
    agreement = terms()
    context = IndexedDescription(
        SimpleNamespace(
            read_context=SimpleNamespace(start_period=months[0]),
            evaluation_period_to=months[-1],
            get_fact=lambda identity: agreement,
        ),
        events,
    )
    edges = dependencies(agreement, context)
    actual = {
        (row["consumer_result_slot"], row["producer_id"], row["producer_public_slot"])
        for row in edges
        if row["consumer_result_slot"].startswith("position:")
    }
    expected = set()
    for offset, (month, event) in enumerate(zip(months, events.values, strict=True)):
        slot = "position:" + month
        expected.add((slot, agreement["business_id"], "main"))
        expected.add((slot, event["business_id"], "main"))
        if offset:
            expected.add((slot, agreement["business_id"], "position:" + months[offset - 1]))
    assert actual == expected
    assert events.visits == count


@pytest.mark.parametrize("count", [50, 100, 200])
def test_position_routes_exact_sources_with_one_input_scan(count, monkeypatch):
    months, events = _months_and_events(count)
    agreement = terms()
    main = {"business_id": agreement["business_id"], "slot": "main"}
    boundary = Snapshot(Prefix(Decimal(0), 0, "current"), ())
    candidate = ReplayResult(
        events={},
        prefixes={},
        snapshots={month: boundary for month in months},
        opening=boundary,
        sources=(_source(main),),
        refs=(main,),
        facts=events,
    )
    adopted = SimpleNamespace(
        target={"kind": "investment.terms", "values": agreement["data"]},
        basis={"period": agreement["period"]},
    )
    context = SimpleNamespace(
        read_context=SimpleNamespace(start_period=months[0]),
        get_result=lambda identity, slot: adopted,
        require_result_sources=lambda references: tuple(_source(row) for row in references),
        resolve_sources=lambda identities: [{"source_id": SOURCE}],
    )
    monkeypatch.setattr(_investment_position, "inventory", lambda *args: candidate)
    for offset, (month, event) in enumerate(zip(months, events.values, strict=True)):
        plan = _investment_position.position_plan(agreement, "position:" + month, context)
        validate_contract("business-facts.schema.json", "PostingPlan", plan)
        expected = [_source(main)]
        if offset:
            expected.append(
                _source(
                    {
                        "business_id": agreement["business_id"],
                        "slot": "position:" + months[offset - 1],
                    }
                )
            )
        expected.append(_source({"business_id": event["business_id"], "slot": "main"}))
        assert plan["basis"]["source_results"] == expected
    assert events.visits == count
