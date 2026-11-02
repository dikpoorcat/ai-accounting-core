"""Candidate-local sharing cannot outlive a command or bypass a source boundary."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.application._changes import classify
from ai_accounting_modular.application._context import SubmissionContext
from ai_accounting_modular.application._planning import collect_changes, evaluate
from ai_accounting_modular.contracts.public import ContractError

from .conftest import initial_request


def context(app, unit):
    return SubmissionContext(
        unit,
        app.registry,
        app.identity,
        app.ledger,
        app.records,
        evaluation_period_to="2026-01",
    )


def begin(view, dependencies=()):
    view.begin_candidate(
        [(None, {"businesses": [], "dependencies": dependencies, "publications": []})], None
    )


def test_workspace_shares_across_slots_but_never_across_candidates(assembled):
    app, company = assembled
    first, second = (str(uuid4()), "main"), (str(uuid4()), "main")
    builds = []

    def build():
        builds.append(object())
        return builds[-1]

    with app.storage.write_unit(company) as unit:
        view = context(app, unit)
        with view.result_scope((first,)):
            with pytest.raises(ContractError, match="当前候选"):
                view.candidate_memo("investment.inventory", "holding", build)
        begin(view)
        with view.result_scope((first,)):
            value = view.candidate_memo("investment.inventory", "holding", build)
        with view.result_scope((second,)):
            assert view.candidate_memo("investment.inventory", "holding", build) is value
            assert view.candidate_memo("other.inventory", "holding", build) is not value
        view.end_candidate()
        begin(view)
        with view.result_scope((first,)):
            assert view.candidate_memo("investment.inventory", "holding", build) is not value
        assert len(builds) == 3
        view.end_candidate()


def test_failed_or_recursive_build_never_publishes_a_partial_shared_value(assembled):
    app, company = assembled
    with app.storage.write_unit(company) as unit:
        view = context(app, unit)
        begin(view)
        with view.result_scope(((str(uuid4()), "main"),)):

            def fail():
                raise ContractError("dependency_conflict", "来源尚未就绪")

            with pytest.raises(ContractError, match="尚未就绪"):
                view.candidate_memo("investment.inventory", "holding", fail)
            with pytest.raises(ContractError, match="循环"):
                view.candidate_memo(
                    "investment.inventory",
                    "holding",
                    lambda: view.candidate_memo("investment.inventory", "holding", lambda: None),
                )
            assert view.candidate_memo("investment.inventory", "holding", lambda: ("ready",)) == (
                "ready",
            )
            with pytest.raises(ContractError, match="不能更换事实"):
                view.add_fact({"business_id": str(uuid4())})
        view.end_candidate()


def test_cached_source_guard_checks_each_slot_and_reads_only_exact_heads(assembled, monkeypatch):
    app, company = assembled
    request = initial_request(company)
    producer = request["mutations"][0]["fact"]["business_id"]
    assert app.accounting_apply(request)["completion"] == "complete"
    consumer, forbidden = str(uuid4()), str(uuid4())
    dependency = {
        "consumer_business_id": consumer,
        "consumer_result_slot": "main",
        "producer_kind": "business",
        "producer_id": producer,
        "producer_public_slot": "main",
        "read_mode": "result",
        "relation_kind": "reference",
    }
    ref = {"business_id": producer, "slot": "main"}
    calls = []
    original = app.ledger.get_result_source

    def counted(*args, **kwargs):
        calls.append(args[:2])
        return original(*args, **kwargs)

    def body_forbidden(*args, **kwargs):
        raise AssertionError("a source guard must not load formal bodies")

    monkeypatch.setattr(app.ledger, "get_result_source", counted)
    monkeypatch.setattr(app.ledger, "get_results", body_forbidden)
    monkeypatch.setattr(app.ledger, "get_result", body_forbidden)
    with app.storage.write_unit(company) as unit:
        view = context(app, unit)
        begin(view, (dependency,))
        with view.result_scope(((consumer, "main"),)):
            assert view.declared_result_refs(producer, "reference") == (ref,)
            assert view.declared_result_refs(producer, "cost") == ()
            first = view.require_result_sources((ref,))
            assert view.require_result_sources((ref,)) == first
        assert calls == [(producer, "main")]
        with view.result_scope(((forbidden, "main"),)):
            assert view.declared_result_refs(producer, "reference") == ()
            with pytest.raises(ContractError, match="未声明"):
                view.require_result_sources((ref,))
        view.end_candidate()
        view.declare_slots(
            {"op": "replace", "business_id": producer, "complete_result_slots": ["main"]}
        )
        begin(view, (dependency,))
        with view.result_scope(((consumer, "main"),)):
            with pytest.raises(ContractError, match="尚未完成"):
                view.require_result_sources((ref,))
        view.end_candidate()


def test_failed_apply_closes_the_workspace_and_rolls_back_the_command(assembled, monkeypatch):
    app, company = assembled
    owner = app.registry.owner("operations.expense")
    observed = []

    def fail(business_id, slot, view):
        observed.append(view)
        view.candidate_memo("investment.inventory", "holding", object)
        raise ContractError("invalid_input", "合成候选失败")

    monkeypatch.setattr(owner, "evaluate_slot", fail)
    assert app.accounting_apply(initial_request(company))["completion"] == "not_saved"
    with observed[0].result_scope(((str(uuid4()), "main"),)):
        with pytest.raises(ContractError, match="当前候选"):
            observed[0].candidate_memo("investment.inventory", "holding", object)
    with app.storage.read_unit(company) as read:
        assert read.head == 0


def test_changed_fact_metadata_excludes_noops_and_keeps_withdrawn_identities(
    assembled, monkeypatch
):
    app, company = assembled
    original = initial_request(company)
    first = original["mutations"][0]["fact"]["business_id"]
    assert app.accounting_apply(original)["completion"] == "complete"
    owner = app.registry.owner("operations.expense")
    describe, observed = owner.describe_change, []

    def inspect(changes, view):
        observed.append(view.changed_business_ids)
        return describe(changes, view)

    monkeypatch.setattr(owner, "describe_change", inspect)
    request = deepcopy(original)
    request["command_id"] = str(uuid4())
    second = str(uuid4())
    created = deepcopy(request["mutations"][0])
    created["fact"].update(business_id=second)
    created["fact"]["data"]["amount_fen"] = "500"
    request["mutations"].append(created)
    assert app.accounting_apply(request)["completion"] == "complete"
    assert observed and all(value == frozenset((second,)) for value in observed)
    observed.clear()
    assert (
        app.accounting_apply(
            {
                "contract": "ai-accounting-modular/1",
                "company_id": company,
                "command_id": str(uuid4()),
                "mutations": [{"op": "withdraw", "business_id": first, "expected_seq": 1}],
            }
        )["completion"]
        == "complete"
    )
    assert observed and all(value == frozenset((first,)) for value in observed)


def test_rebuilt_candidate_cannot_read_a_predecessors_unadopted_stage(assembled):
    app, company = assembled
    request = initial_request(company)
    original = request["mutations"][0]["fact"]
    assert app.accounting_apply(request)["completion"] == "complete"
    replacement = deepcopy(original)
    replacement["data"]["amount_fen"] = "120000"
    identity = original["business_id"]
    with app.storage.write_unit(company) as unit:
        before = unit.repository("application").heads((identity,))[identity]
        change = classify({"op": "replace", "expected_seq": 1, "fact": replacement}, before)
        view = SubmissionContext(
            unit,
            app.registry,
            app.identity,
            app.ledger,
            app.records,
            facts={identity: replacement},
            changed_business_ids=(identity,),
            evaluation_period_to="2026-01",
        )
        descriptions = collect_changes((change,), (), view, app.registry, {})
        plans = evaluate(descriptions, view, app.ledger, None)
        assert plans[0][1]["plans"][0]["target"]["values"]["amount_fen"] == "120000"
        view.begin_candidate(descriptions, None)
        with pytest.raises(ContractError, match="尚未完成"):
            view.get_result(identity, "main")
        view.end_candidate()
