"""Frozen fact proofs retain strict contracts, exact sources and detached reads."""

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from ai_accounting_modular.contracts import _fact_analysis
from ai_accounting_modular.contracts.public import (
    ContractError,
    FactAnalysis,
    analyze_fact,
    canonical_json,
    content_digest,
    fact_source_paths,
)
from tests.modular.contracts.test_fact_sources import _facts


def test_analysis_preserves_every_existing_fact_and_its_complete_source_paths():
    for original in _facts():
        before = deepcopy(original)
        analysis = analyze_fact(original)
        assert isinstance(analysis, FactAnalysis)
        assert analysis.canonical_json == canonical_json(original)
        assert analysis.digest == content_digest(original)
        assert analysis.fact == original == before
        assert analysis.source_paths == fact_source_paths(original)
        assert analysis.source_ids == tuple(analysis.source_paths)


def test_analysis_never_reuses_mutable_input_or_exposes_its_frozen_value(monkeypatch):
    original = deepcopy(next(fact for fact in _facts() if fact["kind"] == "money.bank_statement"))
    before = deepcopy(original)
    calls = []
    collect = _fact_analysis._validated_paths

    def observed(fact, definition):
        calls.append(definition)
        return collect(fact, definition)

    monkeypatch.setattr(_fact_analysis, "_validated_paths", observed)
    analysis = analyze_fact(original)
    original["data"]["rows"][0]["source"]["location"] = "已变化"
    original["evidence_ids"].clear()
    detached = analysis.fact
    detached["data"].clear()
    analysis.source_paths.clear()
    assert analysis.fact == before
    assert analysis.canonical_json == canonical_json(before)
    assert analysis.digest == content_digest(before)
    assert analysis.source_paths == fact_source_paths(before)
    assert analysis.source_ids == tuple(analysis.source_paths)
    assert calls == ["Fact"]
    with pytest.raises(FrozenInstanceError):
        analysis.digest = "0" * 64


def test_fact_analysis_constructor_cannot_create_an_unvalidated_proof():
    invalid = deepcopy(_facts()[0])
    invalid["data"]["not_a_declared_field"] = "invalid"
    for factory in (analyze_fact, FactAnalysis):
        with pytest.raises(ContractError):
            factory(invalid)
        with pytest.raises(ContractError):
            factory({"kind": "operations.expense"})
        with pytest.raises(ContractError):
            factory({"invalid": object()})


def test_fact_analysis_preserves_array_order_in_the_full_content_digest():
    fact = deepcopy(next(fact for fact in _facts() if fact["kind"] == "operations.expense"))
    fact["evidence_ids"] = [
        "10000000-0000-4000-8000-000000000001",
        "10000000-0000-4000-8000-000000000002",
    ]
    first = analyze_fact(fact)
    fact["evidence_ids"].reverse()
    second = analyze_fact(fact)
    assert first.digest != second.digest == content_digest(fact)
    assert first.source_ids == second.source_ids
    assert first.fact["evidence_ids"] == list(reversed(fact["evidence_ids"]))
